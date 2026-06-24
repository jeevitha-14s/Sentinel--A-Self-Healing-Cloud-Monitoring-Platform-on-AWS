import datetime
import logging
import os
import sys
import threading
import time
import uuid
from collections import deque

import click
from flask import Flask, Response, g, jsonify, render_template_string, request
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
        cw = boto3.client("cloudwatch", region_name=region)
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
        time.sleep(60)


def start_heartbeat() -> None:
    if os.environ.get("HEARTBEAT_ENABLED", "").lower() != "true":
        logging.info("heartbeat disabled (HEARTBEAT_ENABLED not set)")
        return
    t = threading.Thread(target=_heartbeat_loop, daemon=True, name="heartbeat")
    t.start()
    logging.info("heartbeat thread started")


DASHBOARD_HTML = """<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="UTF-8">
  <title>Sentinel Dashboard</title>
  <style>
    body { font-family: sans-serif; max-width: 860px; margin: 40px auto; padding: 0 20px; background: #f5f5f5; }
    h1 { margin-bottom: 4px; }
    .subtitle { color: #666; margin-bottom: 24px; font-size: 0.9em; }
    .cards { display: flex; gap: 16px; flex-wrap: wrap; margin-bottom: 28px; }
    .card { flex: 1 1 180px; border-radius: 8px; padding: 16px 20px; background: #fff; box-shadow: 0 1px 3px rgba(0,0,0,.12); }
    .card .label { font-size: 0.8em; color: #555; text-transform: uppercase; letter-spacing: .05em; }
    .card .value { font-size: 1.5em; font-weight: 700; margin-top: 6px; }
    .ok    { color: #1a8a3a; }
    .alarm { color: #c0392b; }
    .other { color: #888; }
    .buttons { display: flex; gap: 12px; margin-bottom: 28px; flex-wrap: wrap; }
    button { padding: 10px 20px; border: none; border-radius: 6px; cursor: pointer; font-size: 0.95em; font-weight: 600; }
    #btn-error { background: #e67e22; color: #fff; }
    #btn-crash { background: #c0392b; color: #fff; }
    .feedback { font-size: 0.85em; margin-top: 6px; color: #555; min-height: 1.2em; }
    table { width: 100%; border-collapse: collapse; background: #fff; border-radius: 8px; box-shadow: 0 1px 3px rgba(0,0,0,.12); overflow: hidden; }
    th { background: #e8e8e8; text-align: left; padding: 10px 14px; font-size: 0.8em; text-transform: uppercase; letter-spacing: .05em; }
    td { padding: 9px 14px; border-top: 1px solid #eee; font-size: 0.9em; }
    .empty { color: #aaa; font-style: italic; }
    .poll-note { font-size: 0.78em; color: #aaa; margin-top: 8px; }
  </style>
</head>
<body>
  <h1>Sentinel Dashboard</h1>
  <p class="subtitle">Live alarm states · auto-refresh every 30 s</p>

  <div class="cards">
    <div class="card">
      <div class="label">Error Alarm</div>
      <div class="value other" id="error-alarm">—</div>
    </div>
    <div class="card">
      <div class="label">Heartbeat Alarm</div>
      <div class="value other" id="heartbeat-alarm">—</div>
    </div>
    <div class="card">
      <div class="label">App Healthy</div>
      <div class="value ok" id="app-healthy">—</div>
    </div>
    <div class="card">
      <div class="label">Last Heartbeat</div>
      <div class="value other" id="heartbeat-age">—</div>
    </div>
  </div>

  <div class="buttons">
    <div>
      <button id="btn-error">Trigger Error Flood</button>
      <div class="feedback" id="fb-error"></div>
    </div>
    <div>
      <button id="btn-crash">Trigger Crash</button>
      <div class="feedback" id="fb-crash"></div>
    </div>
  </div>

  <table>
    <thead><tr><th>Time (UTC)</th><th>Event</th></tr></thead>
    <tbody id="incident-body">
      <tr><td colspan="2" class="empty">No events yet</td></tr>
    </tbody>
  </table>
  <p class="poll-note" id="last-updated"></p>

  <script>
    const COLOR = { OK: 'ok', ALARM: 'alarm' };
    function stateClass(v) { return COLOR[v] || 'other'; }

    let heartbeatAge = null;

    function updateStatus() {
      fetch('/status')
        .then(r => r.json())
        .then(d => {
          function setCard(id, val) {
            const el = document.getElementById(id);
            el.textContent = val;
            el.className = 'value ' + stateClass(val);
          }
          setCard('error-alarm', d.error_alarm);
          setCard('heartbeat-alarm', d.heartbeat_alarm);

          const healthy = document.getElementById('app-healthy');
          healthy.textContent = d.app_healthy ? 'Healthy' : 'Unhealthy';
          healthy.className = 'value ' + (d.app_healthy ? 'ok' : 'alarm');

          heartbeatAge = d.seconds_since_heartbeat;
          renderHeartbeat();

          const tbody = document.getElementById('incident-body');
          if (d.incidents && d.incidents.length > 0) {
            tbody.innerHTML = d.incidents.map(ev =>
              '<tr><td>' + ev.ts + '</td><td>' + ev.event + '</td></tr>'
            ).join('');
          } else {
            tbody.innerHTML = '<tr><td colspan="2" class="empty">No events yet</td></tr>';
          }
          document.getElementById('last-updated').textContent =
            'Last fetched: ' + d.last_updated;
        })
        .catch(() => {
          document.getElementById('last-updated').textContent =
            'Fetch failed — retrying in 30 s';
        });
    }

    function renderHeartbeat() {
      const el = document.getElementById('heartbeat-age');
      if (heartbeatAge === null) {
        el.textContent = 'disabled';
        el.className = 'value other';
      } else {
        el.textContent = heartbeatAge + ' s ago';
        el.className = 'value ' + (heartbeatAge < 120 ? 'ok' : 'alarm');
      }
    }

    // increment ticker every second between polls
    setInterval(() => {
      if (heartbeatAge !== null) { heartbeatAge += 1; renderHeartbeat(); }
    }, 1000);

    document.getElementById('btn-error').addEventListener('click', () => {
      document.getElementById('fb-error').textContent = 'triggering…';
      fetch('/simulate-failure?mode=error')
        .then(r => r.json())
        .then(() => { document.getElementById('fb-error').textContent = 'triggered — watch alarm card'; })
        .catch(() => { document.getElementById('fb-error').textContent = 'error'; });
    });

    document.getElementById('btn-crash').addEventListener('click', () => {
      document.getElementById('fb-crash').textContent = 'triggering…';
      fetch('/simulate-failure?mode=crash')
        .then(() => { document.getElementById('fb-crash').textContent = 'crash triggered'; })
        .catch(() => {
          // connection dropped = crash happened
          document.getElementById('fb-crash').textContent =
            'crash triggered — container restarting, dashboard reconnects automatically';
        });
    });

    updateStatus();
    setInterval(updateStatus, 30000);
  </script>
</body>
</html>"""


@app.get("/dashboard")
def dashboard() -> Response:
    return render_template_string(DASHBOARD_HTML)


if __name__ == "__main__":
    configure_logging()
    start_heartbeat()
    app.run(host="0.0.0.0", port=8000)
