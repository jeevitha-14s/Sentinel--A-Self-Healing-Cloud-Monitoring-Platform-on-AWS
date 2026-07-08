import datetime
import json
import logging
import os
import sys
import threading
import time
import uuid
from collections import deque

import click
from flask import Flask, Response, g, jsonify, request, send_from_directory
from flask.typing import ResponseReturnValue
from pythonjsonlogger import jsonlogger


class RequestIdFilter(logging.Filter):
    """Injects request_id from Flask g into every LogRecord in request context."""

    def filter(self, record: logging.LogRecord) -> bool:
        try:
            record.request_id = g.request_id
        except RuntimeError:
            pass  # background thread: field absent from output per spec
        return True


def configure_logging() -> None:
    formatter = jsonlogger.JsonFormatter(
        fmt="%(asctime)s %(levelname)s %(message)s",
        rename_fields={"levelname": "level", "asctime": "timestamp"},
        datefmt="%Y-%m-%dT%H:%M:%S",
    )
    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(formatter)

    root = logging.getLogger()
    root.addHandler(handler)
    root.setLevel(logging.INFO)
    root.addFilter(RequestIdFilter())

    # Suppress Werkzeug's plain-text access log; our after_request hook replaces it.
    logging.getLogger("werkzeug").setLevel(logging.ERROR)

    # Flask's startup banner uses click.echo() which defaults to stdout.
    # Redirect it to stderr so stdout remains JSON-only (required by awslogs driver).
    _orig_echo = click.echo

    def _echo_to_stderr(msg: object = None, *args: object, **kwargs: object) -> None:
        if "file" not in kwargs:
            kwargs["file"] = sys.stderr
        _orig_echo(msg, *args, **kwargs)

    click.echo = _echo_to_stderr


app = Flask(__name__)

_last_heartbeat_ts: float | None = None
_status_cache: dict = {}
_last_alarm_states: dict = {}
_incident_log: deque = deque(maxlen=10)
_sim_state: dict = {
    "error_rate": 0,
    "auto_heals": 0,
    "incidents_today": 0,
    "pipeline_stage": "idle",
    "app_status": "healthy",
    "pipeline_expires_at": 0.0,
    "hb_alarm_sim": None,
}
_api_cache: dict = {}

_STATE_FILE = "/tmp/sentinel_demo_state.json"


def _load_state() -> dict:
    try:
        with open(_STATE_FILE) as f:
            data = json.load(f)
        if not isinstance(data, dict):
            return {}
        return data
    except Exception:
        return {}


def _save_state() -> None:
    try:
        with open(_STATE_FILE, "w") as f:
            json.dump(
                {
                    "incidents_today": _sim_state.get("incidents_today", 0),
                    "incident_log": list(_incident_log),
                    "hb_alarm_sim": _sim_state.get("hb_alarm_sim"),
                    "auto_heals": _sim_state.get("auto_heals", 0),
                },
                f,
            )
    except Exception:
        pass


# Restore counter and log from previous session (survives os._exit crashes).
_saved = _load_state()
if isinstance(_saved.get("incidents_today"), int):
    _sim_state["incidents_today"] = _saved["incidents_today"]
if isinstance(_saved.get("auto_heals"), int):
    _sim_state["auto_heals"] = _saved["auto_heals"]
if _saved.get("hb_alarm_sim") in ("ALARM", "OK"):
    _sim_state["hb_alarm_sim"] = _saved["hb_alarm_sim"]
for _entry in reversed(_saved.get("incident_log") or []):
    if isinstance(_entry, dict) and "ts" in _entry and "event" in _entry:
        _incident_log.appendleft(_entry)
del _saved


@app.before_request
def set_request_id() -> None:
    g.request_id = str(uuid.uuid4())


@app.after_request
def log_request(response: Response) -> Response:
    logging.info(
        "request complete",
        extra={"method": request.method, "path": request.path, "status": response.status_code},
    )
    return response


@app.get("/")
def index() -> Response:
    return jsonify({"status": "ok", "service": "sentinel-app"})


@app.get("/health")
def health() -> Response:
    return jsonify({"status": "healthy"})


def _utcnow() -> str:
    return datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


@app.get("/simulate-failure")
def simulate_failure() -> ResponseReturnValue:
    mode = request.args.get("mode")
    if mode == "error":
        for i in range(5):
            logging.error("simulated error %d of 5", i + 1)
        _incident_log.appendleft({"ts": _utcnow(), "event": "simulate-failure triggered: error flood"})
        return jsonify({"triggered": "error", "count": 5})
    if mode == "crash":
        _incident_log.appendleft({"ts": _utcnow(), "event": "simulate-failure triggered: crash"})
        logging.error("simulate-failure crash triggered")
        os._exit(1)
    return jsonify({"error": "unknown mode", "mode": mode}), 400


@app.get("/status")
def status() -> Response:
    import boto3  # lazy — only when a request arrives; mirrors _heartbeat_loop pattern

    now = time.time()
    if _status_cache.get("expires_at", 0) > now:
        return jsonify(_status_cache["data"])

    region = os.environ.get("AWS_DEFAULT_REGION", "ap-south-1")
    error_alarm = "INSUFFICIENT_DATA"
    heartbeat_alarm = "INSUFFICIENT_DATA"
    extra: dict = {}

    try:
        cw = boto3.client(
            "cloudwatch",
            region_name=region,
            config=boto3.session.Config(connect_timeout=3, read_timeout=5),
        )
        resp = cw.describe_alarms(
            AlarmNames=["sentinel-app-errors", "sentinel-heartbeat-missing"]
        )
        for alarm in resp.get("MetricAlarms", []):
            if alarm["AlarmName"] == "sentinel-app-errors":
                error_alarm = alarm["StateValue"]
            elif alarm["AlarmName"] == "sentinel-heartbeat-missing":
                heartbeat_alarm = alarm["StateValue"]
    except Exception as exc:
        extra["cw_error"] = str(exc)

    new_states = {"error_alarm": error_alarm, "heartbeat_alarm": heartbeat_alarm}
    for key, state in new_states.items():
        prev = _last_alarm_states.get(key)
        if prev is not None and prev != state:
            _incident_log.appendleft({"ts": _utcnow(), "event": f"{key} changed {prev} → {state}"})
    _last_alarm_states.update(new_states)

    seconds_since_heartbeat = (
        int(time.time() - _last_heartbeat_ts) if _last_heartbeat_ts is not None else None
    )

    data: dict = {
        "error_alarm": error_alarm,
        "heartbeat_alarm": heartbeat_alarm,
        "app_healthy": True,
        "last_updated": _utcnow(),
        "seconds_since_heartbeat": seconds_since_heartbeat,
        "incidents": list(_incident_log),
        **extra,
    }
    _status_cache["data"] = data
    _status_cache["expires_at"] = now + 25
    return jsonify(data)


@app.get("/api/status")
def api_status() -> Response:
    import boto3  # lazy — mirrors _heartbeat_loop pattern

    now = time.time()
    if _sim_state["pipeline_expires_at"] > 0 and _sim_state["pipeline_expires_at"] < now:
        _sim_state["pipeline_stage"] = "idle"
        _sim_state["app_status"] = "healthy"
        _sim_state["hb_alarm_sim"] = None
        _sim_state["pipeline_expires_at"] = 0.0
        _api_cache.clear()

    if _api_cache.get("expires_at", 0) > now:
        return jsonify(_api_cache["data"])

    region = os.environ.get("AWS_DEFAULT_REGION", "ap-south-1")
    error_alarm = "INSUFFICIENT_DATA"
    heartbeat_alarm = "INSUFFICIENT_DATA"
    extra: dict = {}

    try:
        cw = boto3.client(
            "cloudwatch",
            region_name=region,
            config=boto3.session.Config(connect_timeout=3, read_timeout=5),
        )
        resp = cw.describe_alarms(
            AlarmNames=["sentinel-app-errors", "sentinel-heartbeat-missing"]
        )
        for alarm in resp.get("MetricAlarms", []):
            if alarm["AlarmName"] == "sentinel-app-errors":
                error_alarm = alarm["StateValue"]
            elif alarm["AlarmName"] == "sentinel-heartbeat-missing":
                heartbeat_alarm = alarm["StateValue"]
    except Exception as exc:
        extra["cw_error"] = str(exc)

    # Apply hb_alarm_sim override so the demo can show HB alarm ALARM during a silent crash.
    # Cleared by: heal_reset, or error_alarm ALARM→OK (recovery detected in the loop below).
    if _sim_state["hb_alarm_sim"] is not None:
        heartbeat_alarm = _sim_state["hb_alarm_sim"]

    new_states = {"error_alarm": error_alarm, "heartbeat_alarm": heartbeat_alarm}
    for key, state in new_states.items():
        prev = _last_alarm_states.get(key)
        if prev is not None and prev != state:
            _incident_log.appendleft({"ts": _utcnow(), "event": f"{key} changed {prev} → {state}"})
            if state == "ALARM":
                _sim_state["incidents_today"] += 1
                _save_state()
            if key == "error_alarm" and prev == "ALARM" and state == "OK":
                _sim_state["hb_alarm_sim"] = None  # container recovered; clear sim override
                _sim_state["auto_heals"] += 1
                _sim_state["pipeline_stage"] = "ssm"
                _sim_state["pipeline_expires_at"] = time.time() + 15
                _incident_log.appendleft({"ts": _utcnow(), "event": "Auto-healed — Lambda restarted container via SSM"})
                _save_state()
    _last_alarm_states.update(new_states)

    seconds_since_heartbeat = (
        int(time.time() - _last_heartbeat_ts) if _last_heartbeat_ts is not None else None
    )

    data: dict = {
        "system_ok": error_alarm == "OK" and heartbeat_alarm == "OK",
        "app_status": _sim_state["app_status"],
        "heartbeat_alarm": heartbeat_alarm,
        "error_alarm": error_alarm,
        "error_rate": _sim_state["error_rate"],
        "auto_heals": _sim_state["auto_heals"],
        "incidents_today": _sim_state["incidents_today"],
        "seconds_since_heartbeat": seconds_since_heartbeat,
        "incidents": list(_incident_log),
        "pipeline_stage": _sim_state["pipeline_stage"],
        "last_updated": _utcnow(),
        **extra,
    }
    _api_cache["data"] = data
    _api_cache["expires_at"] = now + 4
    return jsonify(data)


@app.post("/api/simulate")
def api_simulate() -> ResponseReturnValue:
    body = request.get_json(silent=True) or {}
    mode = body.get("mode") or request.form.get("mode")

    if mode == "error_burst":
        for i in range(5):
            logging.error("api simulated error %d of 5", i + 1)
        _sim_state["error_rate"] += 5
        _sim_state["pipeline_stage"] = "error"
        _sim_state["pipeline_expires_at"] = time.time() + 90
        _incident_log.appendleft({"ts": _utcnow(), "event": "error burst triggered: 5 errors logged"})
        _api_cache.clear()
        return jsonify({"triggered": "error_burst"})

    if mode == "silent_crash":
        _sim_state["app_status"] = "restarting"
        _sim_state["pipeline_stage"] = "alarm"
        _sim_state["pipeline_expires_at"] = time.time() + 90
        _sim_state["hb_alarm_sim"] = "ALARM"
        _incident_log.appendleft({"ts": _utcnow(), "event": "silent crash triggered"})
        logging.error("api simulate: silent crash triggered")
        _save_state()  # persist before exit so log and counter survive the crash
        os._exit(1)

    if mode == "human_needed":
        _sim_state["pipeline_stage"] = "alert"
        _sim_state["pipeline_expires_at"] = 0.0  # persists until Reset Demo — no auto-clear TTL
        _incident_log.appendleft({"ts": _utcnow(), "event": "auto-heal failed — human needed"})
        _api_cache.clear()
        logging.error("escalation: auto-heal failed, human needed")
        return jsonify({"triggered": "human_needed"})

    if mode == "pipeline_reset":
        _sim_state["pipeline_stage"] = "idle"
        _sim_state["pipeline_expires_at"] = 0.0
        _api_cache.clear()
        return jsonify({"ok": True})

    if mode == "heal_reset":
        _sim_state["error_rate"] = 0
        _sim_state["pipeline_stage"] = "idle"
        _sim_state["pipeline_expires_at"] = 0.0
        _sim_state["incidents_today"] = 0
        _sim_state["hb_alarm_sim"] = None
        _incident_log.clear()
        _api_cache.clear()
        _save_state()
        return jsonify({"ok": True})

    return jsonify({"error": "unknown mode", "mode": mode}), 400


def _heartbeat_loop() -> None:
    import boto3  # lazy — only executed when HEARTBEAT_ENABLED=true

    region = os.environ.get("AWS_DEFAULT_REGION", "ap-south-1")
    client = boto3.client("cloudwatch", region_name=region)
    while True:
        try:
            client.put_metric_data(
                Namespace="Sentinel",
                MetricData=[{"MetricName": "Heartbeat", "Value": 1, "Unit": "Count"}],
            )
            logging.info("heartbeat published")
            global _last_heartbeat_ts
            _last_heartbeat_ts = time.time()
        except Exception as exc:
            logging.error("heartbeat publish failed", extra={"error": str(exc)})
        time.sleep(int(os.environ.get("HEARTBEAT_INTERVAL_SECONDS", "60")))


def start_heartbeat() -> None:
    if os.environ.get("HEARTBEAT_ENABLED", "").lower() != "true":
        logging.info("heartbeat disabled (HEARTBEAT_ENABLED not set)")
        return
    t = threading.Thread(target=_heartbeat_loop, daemon=True, name="heartbeat")
    t.start()
    logging.info("heartbeat thread started")


@app.get("/ready")
def ready() -> ResponseReturnValue:
    for t in threading.enumerate():
        if t.name == "heartbeat" and t.is_alive():
            return jsonify({"ready": True})
    if os.environ.get("HEARTBEAT_ENABLED", "").lower() != "true":
        return jsonify({"ready": True})
    return jsonify({"ready": False, "reason": "heartbeat thread not running"}), 503


@app.get("/dashboard")
def dashboard() -> Response:
    return send_from_directory(
        os.path.join(os.path.dirname(__file__), "dashboard"), "index.html"
    )


if __name__ == "__main__":
    configure_logging()
    start_heartbeat()
    app.run(host="0.0.0.0", port=8000)
