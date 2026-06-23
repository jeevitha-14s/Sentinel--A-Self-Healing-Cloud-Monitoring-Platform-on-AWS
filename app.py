import logging
import os
import sys
import threading
import time
import uuid

import click
from flask import Flask, Response, g, jsonify, request
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


@app.get("/simulate-failure")
def simulate_failure() -> ResponseReturnValue:
    mode = request.args.get("mode")
    if mode == "error":
        for i in range(5):
            logging.error("simulated error %d of 5", i + 1)
        return jsonify({"triggered": "error", "count": 5})
    if mode == "crash":
        logging.error("simulate-failure crash triggered")
        os._exit(1)
    return jsonify({"error": "unknown mode", "mode": mode}), 400


def _heartbeat_loop() -> None:
    import boto3  # lazy — only executed when HEARTBEAT_ENABLED=true

    region = os.environ.get("AWS_DEFAULT_REGION", "us-east-1")
    client = boto3.client("cloudwatch", region_name=region)
    while True:
        try:
            client.put_metric_data(
                Namespace="Sentinel",
                MetricData=[{"MetricName": "Heartbeat", "Value": 1, "Unit": "Count"}],
            )
            logging.info("heartbeat published")
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


if __name__ == "__main__":
    configure_logging()
    start_heartbeat()
    app.run(host="0.0.0.0", port=8000)
