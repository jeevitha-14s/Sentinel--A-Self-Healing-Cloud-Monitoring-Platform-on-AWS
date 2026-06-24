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


DASHBOARD_HTML = '''<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8"/>
<meta name="viewport" content="width=device-width, initial-scale=1.0"/>
<title>Sentinel Dashboard</title>
<style>
*,*::before,*::after{box-sizing:border-box;margin:0;padding:0}
body{font-family:-apple-system,BlinkMacSystemFont,"Segoe UI",sans-serif;background:#f5f5f4;color:#1c1917;min-height:100vh}
.header{background:#fff;border-bottom:1px solid #e7e5e4;padding:1rem 2rem;display:flex;align-items:center;justify-content:space-between}
.header-title{font-size:1.25rem;font-weight:600;color:#1c1917}
.header-sub{font-size:.8rem;color:#78716c;margin-top:2px}
.badge{font-size:.75rem;padding:4px 12px;border-radius:20px;font-weight:500}
.badge-ok{background:#dcfce7;color:#166534}
.badge-alarm{background:#fee2e2;color:#991b1b}
.main{max-width:900px;margin:2rem auto;padding:0 1.5rem}
.alarm-grid{display:grid;grid-template-columns:repeat(3,1fr);gap:1rem;margin-bottom:1.5rem}
.card{background:#fff;border:1px solid #e7e5e4;border-radius:12px;padding:1.25rem;position:relative;overflow:hidden;transition:border-color .3s}
.card-label{font-size:.75rem;font-weight:500;color:#78716c;text-transform:uppercase;letter-spacing:.05em;margin-bottom:.5rem}
.card-value{font-size:1.5rem;font-weight:600;transition:color .3s}
.card.ok{border-left:4px solid #22c55e}
.card.alarm{border-left:4px solid #ef4444;background:#fff8f8}
.card.healthy{border-left:4px solid #22c55e}
.val-ok{color:#16a34a}
.val-alarm{color:#dc2626}
.val-healthy{color:#16a34a}
.val-unknown{color:#78716c}
.hb-card{background:#fff;border:1px solid #e7e5e4;border-radius:12px;padding:1.25rem;margin-bottom:1.5rem}
.hb-row{display:flex;align-items:center;justify-content:space-between;margin-bottom:.75rem}
.hb-left .card-label{margin-bottom:.25rem}
.hb-hint{font-size:.75rem;color:#a8a29e}
.hb-value{font-size:2rem;font-weight:600;font-variant-numeric:tabular-nums;transition:color .3s}
.hb-ok{color:#16a34a}
.hb-warn{color:#d97706}
.hb-danger{color:#dc2626}
.hb-bar-track{height:6px;background:#f5f5f4;border-radius:3px;overflow:hidden}
.hb-bar-fill{height:6px;border-radius:3px;transition:width .5s,background .3s}
.btn-row{display:grid;grid-template-columns:1fr 1fr;gap:.75rem;margin-bottom:1.5rem}
.btn{padding:.75rem 1.25rem;border-radius:8px;border:none;font-size:.875rem;font-weight:500;cursor:pointer;display:flex;align-items:center;gap:.5rem;justify-content:center;transition:opacity .15s,transform .1s}
.btn:active{transform:scale(.98)}
.btn-error{background:#ef4444;color:#fff}
.btn-error:hover{background:#dc2626}
.btn-crash{background:#f97316;color:#fff}
.btn-crash:hover{background:#ea6c00}
.btn-feedback{font-size:.75rem;opacity:0;margin-left:auto;transition:opacity .3s}
.btn-feedback.show{opacity:1}
.section-label{font-size:.75rem;font-weight:500;color:#78716c;text-transform:uppercase;letter-spacing:.05em;margin-bottom:.75rem}
.log-card{background:#fff;border:1px solid #e7e5e4;border-radius:12px;overflow:hidden}
.log-header{display:grid;grid-template-columns:180px 1fr;padding:.625rem 1rem;background:#f9f9f8;border-bottom:1px solid #e7e5e4;font-size:.75rem;font-weight:500;color:#78716c;text-transform:uppercase;letter-spacing:.05em}
.log-empty{padding:1.25rem 1rem;font-size:.875rem;color:#a8a29e}
.log-row{display:grid;grid-template-columns:180px 1fr;padding:.75rem 1rem;border-bottom:1px solid #f5f5f4;font-size:.875rem;animation:fadeIn .3s ease}
.log-row:last-child{border-bottom:none}
.log-time{color:#78716c;font-variant-numeric:tabular-nums}
.log-event{color:#1c1917}
@keyframes fadeIn{from{opacity:0;transform:translateY(-4px)}to{opacity:1;transform:none}}
.footer{text-align:center;font-size:.75rem;color:#a8a29e;margin-top:1.5rem;padding-bottom:2rem}
</style>
</head>
<body>
<div class="header">
  <div>
    <div class="header-title">Sentinel</div>
    <div class="header-sub">Self-healing cloud platform &bull; ap-south-1</div>
  </div>
  <span class="badge badge-ok" id="global-badge">All systems healthy</span>
</div>
<div class="main">
  <div class="alarm-grid">
    <div class="card ok" id="card-errors">
      <div class="card-label">Error alarm</div>
      <div class="card-value val-ok" id="errors-val">OK</div>
    </div>
    <div class="card ok" id="card-heartbeat">
      <div class="card-label">Heartbeat alarm</div>
      <div class="card-value val-ok" id="hb-alarm-val">OK</div>
    </div>
    <div class="card healthy" id="card-health">
      <div class="card-label">App health</div>
      <div class="card-value val-healthy" id="health-val">Healthy</div>
    </div>
  </div>

  <div class="hb-card">
    <div class="hb-row">
      <div class="hb-left">
        <div class="card-label">Last heartbeat</div>
        <div class="hb-hint">publishes every 60s &bull; alarm fires after 2 missed</div>
      </div>
      <div style="text-align:right">
        <div class="hb-value hb-ok" id="hb-counter">--</div>
        <div class="hb-hint" id="hb-status-text" style="margin-top:4px">waiting</div>
      </div>
    </div>
    <div class="hb-bar-track">
      <div class="hb-bar-fill" id="hb-bar" style="width:0%;background:#22c55e"></div>
    </div>
  </div>

  <div class="btn-row">
    <button class="btn btn-error" onclick="triggerError()">
      &#9888; Trigger error flood
      <span class="btn-feedback" id="fb-error">triggered</span>
    </button>
    <button class="btn btn-crash" onclick="triggerCrash()">
      &#9632; Trigger crash
      <span class="btn-feedback" id="fb-crash">crash triggered</span>
    </button>
  </div>

  <div class="section-label">Incident log</div>
  <div class="log-card">
    <div class="log-header">
      <span>Time (UTC)</span>
      <span>Event</span>
    </div>
    <div id="log-body">
      <div class="log-empty">No incidents yet &mdash; trigger one above</div>
    </div>
  </div>

  <div class="footer" id="footer-ts">Auto-refreshes every 30s</div>
</div>

<script>
var hbSeconds = null;
var hbTicker = null;

function setAlarmCard(cardId, valId, state) {
  var card = document.getElementById(cardId);
  var val = document.getElementById(valId);
  card.className = "card " + (state === "OK" ? "ok" : state === "ALARM" ? "alarm" : "ok");
  val.className = "card-value " + (state === "OK" ? "val-ok" : state === "ALARM" ? "val-alarm" : "val-unknown");
  val.textContent = state;
}

function updateGlobalBadge(errState, hbState) {
  var badge = document.getElementById("global-badge");
  if (errState === "ALARM" || hbState === "ALARM") {
    badge.className = "badge badge-alarm";
    badge.textContent = "Incident in progress";
  } else {
    badge.className = "badge badge-ok";
    badge.textContent = "All systems healthy";
  }
}

function updateHbUI() {
  if (hbSeconds === null) {
    document.getElementById("hb-counter").textContent = "--";
    document.getElementById("hb-status-text").textContent = "heartbeat not yet received";
    document.getElementById("hb-bar").style.width = "0%";
    return;
  }
  var counter = document.getElementById("hb-counter");
  var bar = document.getElementById("hb-bar");
  var statusText = document.getElementById("hb-status-text");
  counter.textContent = hbSeconds + "s ago";
  var pct = Math.min((hbSeconds / 120) * 100, 100);
  if (hbSeconds < 70) {
    counter.className = "hb-value hb-ok";
    bar.style.background = "#22c55e";
    bar.style.width = Math.min((hbSeconds / 60) * 60, 60) + "%";
    statusText.textContent = "healthy";
  } else if (hbSeconds < 120) {
    counter.className = "hb-value hb-warn";
    bar.style.background = "#f59e0b";
    bar.style.width = pct + "%";
    statusText.textContent = "first window missed — watching";
  } else {
    counter.className = "hb-value hb-danger";
    bar.style.background = "#ef4444";
    bar.style.width = "100%";
    statusText.textContent = "alarm firing";
  }
}

function startHbTicker(serverSeconds) {
  clearInterval(hbTicker);
  hbSeconds = serverSeconds;
  hbTicker = setInterval(function() {
    if (hbSeconds !== null) { hbSeconds++; updateHbUI(); }
  }, 1000);
  updateHbUI();
}

function renderLog(incidents) {
  var body = document.getElementById("log-body");
  if (!incidents || incidents.length === 0) {
    body.innerHTML = "<div class=\\"log-empty\\">No incidents yet &mdash; trigger one above</div>";
    return;
  }
  body.innerHTML = incidents.map(function(inc) {
    return "<div class=\\"log-row\\"><span class=\\"log-time\\">" + inc.ts.replace("T"," ").replace("Z","") + "</span><span class=\\"log-event\\">" + inc.event + "</span></div>";
  }).join("");
}

function updateStatus() {
  fetch("/status")
    .then(function(r){ return r.json(); })
    .then(function(data) {
      setAlarmCard("card-errors", "errors-val", data.error_alarm || "?");
      setAlarmCard("card-heartbeat", "hb-alarm-val", data.heartbeat_alarm || "?");
      updateGlobalBadge(data.error_alarm, data.heartbeat_alarm);
      if (data.seconds_since_heartbeat !== null && data.seconds_since_heartbeat !== undefined) {
        startHbTicker(data.seconds_since_heartbeat);
      }
      renderLog(data.incidents);
      document.getElementById("footer-ts").textContent = "Last updated: " + new Date().toLocaleTimeString() + " · auto-refreshes every 30s";
    })
    .catch(function(e){ console.error("status fetch failed", e); });
}

function feedback(id, text) {
  var el = document.getElementById(id);
  el.textContent = text;
  el.classList.add("show");
  setTimeout(function(){ el.classList.remove("show"); }, 2500);
}

function triggerError() {
  fetch("/simulate-failure?mode=error")
    .then(function(){ feedback("fb-error", "triggered — watch alarm"); })
    .catch(function(){ feedback("fb-error", "sent"); });
}

function triggerCrash() {
  fetch("/simulate-failure?mode=crash")
    .then(function(){ feedback("fb-crash", "crash triggered"); })
    .catch(function(){ feedback("fb-crash", "crash triggered — container restarting"); });
}

updateStatus();
setInterval(updateStatus, 30000);
</script>
</body>
</html>'''


@app.get("/dashboard")
def dashboard() -> Response:
    return render_template_string(DASHBOARD_HTML)


if __name__ == "__main__":
    configure_logging()
    start_heartbeat()
    app.run(host="0.0.0.0", port=8000)
