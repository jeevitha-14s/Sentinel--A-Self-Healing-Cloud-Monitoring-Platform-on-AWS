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
body{font-family:-apple-system,BlinkMacSystemFont,"Segoe UI",Roboto,sans-serif;background:#1a1a1a;color:#e8e8e6;min-height:100vh;padding:1.5rem}
.wrap{max-width:780px;margin:0 auto}
.header{display:flex;align-items:center;justify-content:space-between;padding-bottom:1.5rem}
.h-title{font-size:1.25rem;font-weight:600;color:#fafaf9}
.h-sub{font-size:.8rem;color:#a1a1aa;margin-top:2px}
.badge{font-size:.72rem;padding:4px 12px;border-radius:20px;font-weight:500;transition:all .3s}
.badge-ok{background:#14321f;color:#4ade80}
.badge-alarm{background:#3a1515;color:#f87171}
.alarm-grid{display:grid;grid-template-columns:1fr 1fr;gap:12px;margin-bottom:1.25rem}
.card{background:#222220;border:0.5px solid #34342f;border-radius:12px;padding:1rem 1.25rem;transition:border-color .4s,background .4s}
.card.ok{border-left:3px solid #22c55e}
.card.alarm{border-left:3px solid #ef4444;background:#2a1a1a}
.c-name{font-size:.8rem;color:#a1a1aa;margin-bottom:6px;display:flex;align-items:center;gap:6px}
.c-val{font-size:1.35rem;font-weight:600;transition:color .4s}
.c-val.ok{color:#4ade80}
.c-val.alarm{color:#f87171}
.c-val.unknown{color:#a1a1aa}
.c-meta{font-size:.75rem;color:#71717a;margin-top:4px}
.hb-card{background:#222220;border:0.5px solid #34342f;border-radius:12px;padding:1rem 1.25rem;margin-bottom:1.25rem}
.hb-row{display:flex;align-items:center;justify-content:space-between}
.hb-label{font-size:.8rem;color:#a1a1aa}
.hb-hint{font-size:.72rem;color:#71717a;margin-top:2px}
.hb-counter{font-size:1.75rem;font-weight:600;font-variant-numeric:tabular-nums;text-align:right;transition:color .3s}
.hb-counter.ok{color:#4ade80}
.hb-counter.warn{color:#fbbf24}
.hb-counter.danger{color:#f87171}
.hb-status{font-size:.72rem;color:#71717a;margin-top:2px;text-align:right}
.hb-track{margin-top:10px;height:5px;background:#34342f;border-radius:3px;overflow:hidden}
.hb-fill{height:5px;border-radius:3px;transition:width .5s,background .3s}
.stat-grid{display:grid;grid-template-columns:repeat(3,1fr);gap:12px;margin-bottom:1.25rem}
.stat{background:#1e1e1c;border-radius:10px;padding:1rem}
.stat-label{font-size:.75rem;color:#a1a1aa;margin-bottom:4px}
.stat-val{font-size:1.4rem;font-weight:600;color:#fafaf9}
.sec-label{font-size:.72rem;font-weight:500;color:#a1a1aa;text-transform:uppercase;letter-spacing:.06em;margin-bottom:10px}
.btn-grid{display:grid;grid-template-columns:1fr 1fr;gap:8px;margin-bottom:1.25rem}
.btn{padding:10px 16px;font-size:.82rem;font-weight:500;border-radius:8px;border:0.5px solid #44443e;background:#222220;color:#e8e8e6;cursor:pointer;display:flex;align-items:center;gap:8px;transition:background .15s,transform .1s;width:100%}
.btn:hover{background:#2a2a27}
.btn:active{transform:scale(.98)}
.btn.danger{border-color:#5a2a2a;color:#f87171}
.btn.danger:hover{background:#2a1a1a}
.btn-fb{font-size:.7rem;color:#4ade80;margin-left:auto;opacity:0;transition:opacity .3s}
.btn-fb.show{opacity:1}
.log-list{display:flex;flex-direction:column;gap:8px}
.log-empty{font-size:.82rem;color:#71717a;padding:8px 0}
.log-row{background:#222220;border:0.5px solid #34342f;border-radius:8px;padding:10px 14px;display:flex;align-items:center;justify-content:space-between;animation:slideIn .3s ease}
@keyframes slideIn{from{opacity:0;transform:translateY(-6px)}to{opacity:1;transform:none}}
.log-event{font-size:.82rem;font-weight:500;color:#fafaf9}
.log-time{font-size:.72rem;color:#a1a1aa;margin-top:2px;font-variant-numeric:tabular-nums}
.log-tag{font-size:.72rem;padding:3px 8px;border-radius:20px;white-space:nowrap}
.tag-ok{background:#14321f;color:#4ade80}
.tag-warn{background:#3a2e15;color:#fbbf24}
.footer{display:flex;align-items:center;justify-content:space-between;margin-top:1.5rem;padding-top:1rem;border-top:0.5px solid #34342f}
.foot-text{font-size:.72rem;color:#71717a}
.foot-badge{font-size:.72rem;padding:3px 10px;border-radius:20px;background:#1e1e1c;color:#a1a1aa}
</style>
</head>
<body>
<div class="wrap">
  <div class="header">
    <div>
      <div class="h-title">Sentinel</div>
      <div class="h-sub">Self-healing platform &bull; ap-south-1</div>
    </div>
    <span class="badge badge-ok" id="global-badge">All systems healthy</span>
  </div>

  <div class="alarm-grid">
    <div class="card ok" id="card-errors">
      <div class="c-name">&#9636; Error alarm</div>
      <div class="c-val ok" id="errors-val">OK</div>
      <div class="c-meta">AppErrors &ge; 1 in 60s &rarr; Lambda restart</div>
    </div>
    <div class="card ok" id="card-heartbeat">
      <div class="c-name">&#9825; Heartbeat alarm</div>
      <div class="c-val ok" id="hb-alarm-val">OK</div>
      <div class="c-meta">Missing data &times;2 &rarr; silent death caught</div>
    </div>
  </div>

  <div class="hb-card">
    <div class="hb-row">
      <div>
        <div class="hb-label">Last heartbeat</div>
        <div class="hb-hint">publishes every 60s &bull; alarm fires after 2 missed</div>
      </div>
      <div>
        <div class="hb-counter ok" id="hb-counter">--</div>
        <div class="hb-status" id="hb-status">waiting</div>
      </div>
    </div>
    <div class="hb-track"><div class="hb-fill" id="hb-fill" style="width:0%;background:#22c55e"></div></div>
  </div>

  <div class="stat-grid">
    <div class="stat"><div class="stat-label">Incidents today</div><div class="stat-val" id="stat-total">0</div></div>
    <div class="stat"><div class="stat-label">Auto-healed</div><div class="stat-val" id="stat-healed">0</div></div>
    <div class="stat"><div class="stat-label">Human needed</div><div class="stat-val" id="stat-human">0</div></div>
  </div>

  <div class="sec-label">Demo controls</div>
  <div class="btn-grid">
    <button class="btn danger" onclick="triggerError()">&#9888; Trigger error flood<span class="btn-fb" id="fb-error">triggered</span></button>
    <button class="btn danger" onclick="triggerCrash()">&#9632; Trigger silent crash<span class="btn-fb" id="fb-crash">crash triggered</span></button>
    <button class="btn" onclick="checkHealth()">&#9825; Check /health<span class="btn-fb" id="fb-health"></span></button>
    <button class="btn" onclick="updateStatus(true)">&#8635; Refresh alarms<span class="btn-fb" id="fb-refresh">done</span></button>
  </div>

  <div class="sec-label">Incident log</div>
  <div class="log-list" id="log-list">
    <div class="log-empty">No incidents yet &mdash; trigger one above</div>
  </div>

  <div class="footer">
    <span class="foot-text" id="foot-text">Auto-refreshes every 30s</span>
    <span class="foot-badge">sentinel-app &bull; 35.154.34.86</span>
  </div>
</div>

<script>
var hbSeconds = null;
var hbTicker = null;
var crashed = false;

function setCard(cardId, valId, state) {
  var card = document.getElementById(cardId);
  var val = document.getElementById(valId);
  if (state === "ALARM") {
    card.className = "card alarm";
    val.className = "c-val alarm";
  } else if (state === "OK") {
    card.className = "card ok";
    val.className = "c-val ok";
  } else {
    card.className = "card ok";
    val.className = "c-val unknown";
  }
  val.textContent = state;
}

function setBadge(err, hb) {
  var b = document.getElementById("global-badge");
  if (err === "ALARM" || hb === "ALARM") {
    b.className = "badge badge-alarm";
    b.textContent = "Incident in progress";
  } else {
    b.className = "badge badge-ok";
    b.textContent = "All systems healthy";
  }
}

function updateHb() {
  var c = document.getElementById("hb-counter");
  var f = document.getElementById("hb-fill");
  var s = document.getElementById("hb-status");
  if (hbSeconds === null) {
    c.textContent = "--"; c.className = "hb-counter ok";
    f.style.width = "0%"; s.textContent = "heartbeat not yet received";
    return;
  }
  c.textContent = hbSeconds + "s ago";
  var pct = Math.min((hbSeconds / 120) * 100, 100);
  if (hbSeconds < 70) {
    c.className = "hb-counter ok"; f.style.background = "#22c55e";
    f.style.width = Math.min((hbSeconds / 60) * 60, 60) + "%"; s.textContent = "healthy";
  } else if (hbSeconds < 120) {
    c.className = "hb-counter warn"; f.style.background = "#f59e0b";
    f.style.width = pct + "%"; s.textContent = "first window missed — watching";
  } else {
    c.className = "hb-counter danger"; f.style.background = "#ef4444";
    f.style.width = "100%"; s.textContent = "alarm firing — Lambda restarting";
  }
}

function startTicker(serverSeconds) {
  clearInterval(hbTicker);
  hbSeconds = serverSeconds;
  hbTicker = setInterval(function(){ if (hbSeconds !== null){ hbSeconds++; updateHb(); } }, 1000);
  updateHb();
}

function renderLog(incidents) {
  var list = document.getElementById("log-list");
  if (!incidents || incidents.length === 0) {
    list.innerHTML = "<div class=\\"log-empty\\">No incidents yet &mdash; trigger one above</div>";
    return;
  }
  list.innerHTML = incidents.map(function(inc){
    var human = inc.event.toLowerCase().indexOf("human") !== -1;
    var tag = human ? "<span class=\\"log-tag tag-warn\\">human needed</span>" : "<span class=\\"log-tag tag-ok\\">logged</span>";
    var t = inc.ts.replace("T"," ").replace("Z","");
    return "<div class=\\"log-row\\"><div><div class=\\"log-event\\">" + inc.event + "</div><div class=\\"log-time\\">" + t + "</div></div>" + tag + "</div>";
  }).join("");
}

function updateStats(incidents) {
  if (!incidents) return;
  var total = incidents.length;
  var human = incidents.filter(function(i){ return i.event.toLowerCase().indexOf("human") !== -1; }).length;
  document.getElementById("stat-total").textContent = total;
  document.getElementById("stat-healed").textContent = total - human;
  document.getElementById("stat-human").textContent = human;
}

function updateStatus(manual) {
  fetch("/status").then(function(r){ return r.json(); }).then(function(d){
    setCard("card-errors","errors-val", d.error_alarm || "?");
    setCard("card-heartbeat","hb-alarm-val", d.heartbeat_alarm || "?");
    setBadge(d.error_alarm, d.heartbeat_alarm);
    if (d.seconds_since_heartbeat !== null && d.seconds_since_heartbeat !== undefined) {
      crashed = false;
      startTicker(d.seconds_since_heartbeat);
    }
    renderLog(d.incidents);
    updateStats(d.incidents);
    document.getElementById("foot-text").textContent = "Last updated: " + new Date().toLocaleTimeString() + " · auto-refreshes every 30s";
    if (manual) feedback("fb-refresh","done");
  }).catch(function(e){ console.error(e); });
}

function feedback(id, text) {
  var el = document.getElementById(id);
  if (text) el.textContent = text;
  el.classList.add("show");
  setTimeout(function(){ el.classList.remove("show"); }, 2500);
}

function triggerError() {
  feedback("fb-error","triggered — watch alarm");
  fetch("/simulate-failure?mode=error").catch(function(){});
}

function triggerCrash() {
  feedback("fb-crash","crash triggered");
  crashed = true;
  hbSeconds = 60;
  updateHb();
  fetch("/simulate-failure?mode=crash").catch(function(){});
}

function checkHealth() {
  fetch("/health").then(function(r){
    feedback("fb-health", r.ok ? "200 OK" : "down");
  }).catch(function(){ feedback("fb-health","restarting..."); });
}

updateStatus();
setInterval(function(){ updateStatus(false); }, 30000);
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
