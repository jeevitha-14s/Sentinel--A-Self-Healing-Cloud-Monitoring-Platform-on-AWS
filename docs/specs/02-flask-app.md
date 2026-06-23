# Spec: Flask App with Structured Logging and Heartbeat

## Overview

A minimal Flask service that is the application Sentinel monitors and heals.
It emits structured JSON logs to stdout (shipped later by the `awslogs` Docker
log driver), exposes health and demo-failure endpoints to exercise both alarm
paths, and publishes a CloudWatch heartbeat metric so silent crashes can be
detected in Feature 10.

## Requirements

1. `GET /` → 200, JSON body `{"status": "ok", "service": "sentinel-app"}`.
2. `GET /health` → 200, JSON body `{"status": "healthy"}`.
3. `GET /simulate-failure?mode=error` → logs 5 structured JSON lines at level
   `ERROR` to stdout, then returns 200 with `{"triggered": "error", "count": 5}`.
4. `GET /simulate-failure?mode=crash` → logs one final structured JSON line
   (`{"level": "ERROR", "message": "simulate-failure crash triggered"}`) and
   calls `sys.exit(1)`, terminating the process so the container stops.
5. Every log line is a single-line JSON object written to stdout containing at
   minimum: `timestamp` (ISO 8601), `level` (uppercase string), `message`
   (string), `request_id` (UUID per request, absent only for background threads).
6. A background thread publishes `Sentinel/Heartbeat` = `1` to CloudWatch every
   60 seconds. The thread starts only when the `HEARTBEAT_ENABLED` env var is set
   to `"true"` (case-insensitive); otherwise it is a complete no-op.
7. All Flask request/response lifecycle events (startup, each request) are logged
   at `INFO` level using the same structured format.

## Out of scope

- Dockerfile and container runtime (Feature 3).
- AWS infrastructure wiring: ECR, EC2, log group, `awslogs` driver config (Feature 4+).
- The heartbeat CloudWatch Alarm (Feature 10). Only the metric emitter is here.
- Any database, session state, auth, or HTML templating.
- Multiple concurrent workers or production WSGI server (Gunicorn/uWSGI).

## Acceptance criteria

- `GET /` returns HTTP 200 with a JSON body parseable by `jq` and containing a
  `"status"` key.
- `GET /health` returns HTTP 200.
- `GET /simulate-failure?mode=error` returns HTTP 200 and produces exactly 5
  log lines on stdout, each valid single-line JSON with `.level == "ERROR"`,
  verifiable with `python app.py & curl ... | jq .level`.
- `GET /simulate-failure?mode=crash` causes the Python process to exit with a
  non-zero exit code (verify with `echo $?` after a local run).
- Every log line emitted during a request contains a `request_id` field that is
  consistent across all lines for that request (same UUID for mode=error's 5 lines).
- All log lines across all endpoints are parseable by `jq` with no parse errors:
  `python app.py ... | jq .` produces no `Invalid numeric literal` or similar errors.
- `.level` is exactly the uppercase string `"ERROR"` on error lines — not `"error"`,
  `"Error"`, or any other variant. (The CloudWatch metric filter in Feature 5
  uses `{ $.level = "ERROR" }` and is case-sensitive.)
- `timestamp` in every log line parses as a valid ISO 8601 datetime
  (e.g. `2026-06-23T12:00:00.000Z`).
- When `HEARTBEAT_ENABLED` is unset or any value other than `"true"`, running
  the app imports and starts with zero boto3 calls and no AWS credentials required.
- When `HEARTBEAT_ENABLED=true` and a boto3 `PutMetricData` call raises any
  exception, the heartbeat thread logs the error as a structured JSON line and
  sleeps until the next interval — it never propagates the exception or crashes
  the app.
- The heartbeat thread is a daemon thread; the process exits cleanly on
  `SIGTERM`/`KeyboardInterrupt` without waiting for the next 60-second tick.

## Notes

- Use `python-json-logger` (preferred) or `json.dumps` to stdout. Do not use
  Flask's default Werkzeug logger — it emits plain-text lines the `awslogs`
  driver cannot parse as JSON.
- The `request_id` must be injected via a `logging.Filter` subclass attached
  to the **root logger** (not per-handler). The filter reads the UUID from
  Flask's `g` object (set in a `@app.before_request` hook) and adds it as an
  attribute to every `LogRecord`. This guarantees every log call — whether from
  app code, a library, or Flask internals — carries the same `request_id`
  without any call-site changes.
- `sys.exit(1)` is the correct termination call for `mode=crash`; it ensures
  Docker sees a non-zero exit code, which is how the heartbeat missing-data
  alarm (Feature 10) detects silent death.
- The heartbeat thread needs `cloudwatch:PutMetricData` scoped to the
  `Sentinel/*` namespace. Add a row to `docs/iam-scratch.md` when that
  `AccessDenied` is hit in Feature 10.
- `mode=error` exercises the **log-pattern alarm** path (Feature 5/6);
  `mode=crash` exercises the **heartbeat missing-data alarm** path (Feature 10).
  Both paths must work end-to-end before Day 3 demo.
- Default port is `8000` (matches `CLAUDE.md` run command and future Docker
  `-p 8000:8000` flag).
- Keep the Flask app in a single file (`app.py`) at the repo root for now.
  Splitting into modules is out of scope for this feature.
