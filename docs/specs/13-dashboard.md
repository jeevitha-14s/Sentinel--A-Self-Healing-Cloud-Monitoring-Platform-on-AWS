## Overview

A live dashboard served by the existing Flask app at `/dashboard` that shows
alarm states, a heartbeat ticker, demo trigger buttons, and a rolling incident
log. The goal is to make the self-heal demo self-explanatory during a
portfolio review — no AWS console narration required. All data flows through
two new Flask endpoints; the browser never touches AWS directly.

## Requirements

1. **GET /dashboard** — returns HTTP 200 with the dashboard HTML page. Served
   via `render_template_string()` (no separate `templates/` directory needed).

2. **GET /status** — returns HTTP 200 with a JSON body containing exactly:
   ```json
   {
     "error_alarm":      "OK" | "ALARM" | "INSUFFICIENT_DATA",
     "heartbeat_alarm":  "OK" | "ALARM" | "INSUFFICIENT_DATA",
     "app_healthy":      true | false,
     "last_updated":     "<ISO-8601 UTC timestamp>"
   }
   ```
   - `error_alarm` / `heartbeat_alarm` — fetched via a single
     `cloudwatch.describe_alarms(AlarmNames=["sentinel-app-errors",
     "sentinel-heartbeat-missing"])` call using boto3 with instance-profile
     credentials. No hardcoded keys.
   - `app_healthy` — result of an in-process call to the `/health` route logic
     (not an outbound HTTP request to self).
   - `last_updated` — UTC timestamp of when the cached response was built.
   - The response is cached in a module-level dict for **25 seconds** to keep
     the endpoint fast and avoid CloudWatch rate-limit concerns.
   - If the CloudWatch call fails, `/status` still returns HTTP 200 with
     `error_alarm` and `heartbeat_alarm` set to `"INSUFFICIENT_DATA"` and an
     additional `"cw_error": "<message>"` field. It must never return 5xx or
     raise an unhandled exception.

3. **Auto-polling** — the dashboard HTML polls `/status` every 30 seconds via
   `fetch()` and updates alarm cards in-place without a full page reload. The
   30-second client interval plus the 25-second server cache means worst-case
   staleness is 55 seconds; this is acceptable for a demo tool.

4. **Demo buttons** — two buttons that POST (or GET) to the existing endpoints:
   - `[Trigger Error Flood]` → `GET /simulate-failure?mode=error`
   - `[Trigger Crash]` → `GET /simulate-failure?mode=crash`
   Each button shows inline feedback ("triggered…" / "error") without
   navigating away from the dashboard.

5. **Heartbeat ticker** — displays seconds elapsed since the last known
   heartbeat publish. Sourced from a module-level `_last_heartbeat_ts`
   variable (a `float` holding `time.time()`) updated by the existing
   `_heartbeat_loop()` in `app.py` on every successful `put_metric_data` call.
   When `HEARTBEAT_ENABLED=false`, the ticker shows "heartbeat disabled" rather
   than a stale counter.

6. **Incident log** — an in-memory `deque(maxlen=10)` of event dicts
   (`{"ts": ..., "event": ...}`). Entries are appended:
   - When `/simulate-failure` is called (any mode), record the trigger.
   - When `/status` detects that an alarm state has *changed* from the previous
     poll (OK→ALARM or ALARM→OK), record the transition.
   The log is displayed newest-first on the dashboard. It is lost on container
   restart; this is expected and acceptable per the out-of-scope list.

7. **No hardcoded AWS credentials** — all AWS calls use the EC2 instance-profile
   credentials automatically provided by boto3. No keys in HTML, JS, or env vars.

## Out of scope

- Authentication or access control on `/dashboard` or `/status`.
- Persistent incident storage across container restarts (in-memory only).
- Mobile responsiveness or CSS frameworks.
- WebSocket / server-sent events (polling is sufficient for a demo).
- Exposing `/status` as a public health-check API (it may include alarm names
  that reveal infrastructure details; restrict in production if ever promoted).
- Any change to the Lambda, SNS, or alerting path — the dashboard is
  read-only plus simulation triggers.

## Acceptance criteria

### Endpoints
- `GET /dashboard` returns HTTP 200 and `Content-Type: text/html`.
- `GET /status` returns HTTP 200 and `Content-Type: application/json` with all
  four required fields present and correctly typed.
- `/status` returns HTTP 200 (not 5xx) even when the CloudWatch API call fails;
  response contains `"cw_error"` field in that case.
- `/status` served from cache within 25 s of a prior call completes in under
  200 ms (no outbound AWS call on cache hit).

### Dashboard behavior
- Alarm cards visually distinguish OK / ALARM / INSUFFICIENT_DATA states (e.g.
  green / red / grey).
- Alarm cards update within 55 seconds of a real CloudWatch alarm state change
  (30 s poll + 25 s cache worst case).
- Heartbeat ticker increments visually in the browser without a page reload.
- "Trigger Error Flood" button fires `GET /simulate-failure?mode=error` and
  shows inline feedback without navigating away.
- "Trigger Crash" button fires `GET /simulate-failure?mode=crash` and shows
  inline feedback. The container will die; the browser auto-reconnects on the
  next 30-second poll when the container is back.
- Incident log shows the most recent entry at the top; maximum 10 entries.
- After a crash-and-restart, the dashboard reconnects automatically (no manual
  page reload required beyond the polling interval).

### Security and credentials
- No AWS access key, secret key, or account ID appears anywhere in the HTML or
  JavaScript source returned by `/dashboard`.
- `/status` does not expose raw boto3 credential objects or session tokens in
  its JSON response.

### Infrastructure
- `terraform plan` after adding the new IAM inline policy shows exactly one
  resource added (`aws_iam_role_policy.cloudwatch_describe`) and zero changes
  to existing resources.
- `GET http://<ec2-ip>:8000/dashboard` returns HTTP 200 after a standard
  `terraform apply` + GitHub Actions deploy with no manual steps.
- The security group requires no changes (dashboard is served on the existing
  port 8000).

### CLAUDE.md hard rules
- Adding `/status` and `/dashboard` routes does not affect the Lambda → SNS
  alert path; alarms fire independently of Flask request handling.
- No DynamoDB or external state store is introduced for any dashboard feature.

## Notes

- **IAM — new inline policy required in `infra/ec2.tf`**: add
  `aws_iam_role_policy.cloudwatch_describe` on `sentinel_ec2` allowing
  `cloudwatch:DescribeAlarms` scoped to the two alarm ARNs:
  `aws_cloudwatch_metric_alarm.app_errors.arn` and
  `aws_cloudwatch_metric_alarm.heartbeat_missing.arn`. Do not use `"*"` as
  the resource — these ARNs are known at plan time.

- **Cache implementation**: a module-level `dict` with keys `data` (the JSON
  payload) and `expires_at` (`time.time() + 25`). Thread-safe for read-only
  access because dict assignment is atomic under CPython's GIL; no `Lock`
  needed for this use case.

- **Heartbeat timestamp thread-safety**: `_last_heartbeat_ts` is a
  module-level `float`. Assigning a new `float` is atomic under CPython.
  No `Lock` required.

- **`_heartbeat_loop` modification**: add one line after the successful
  `put_metric_data` call — `global _last_heartbeat_ts; _last_heartbeat_ts =
  time.time()`. This is the only change to existing heartbeat logic.

- **`app_healthy` in `/status`**: hardcode `"app_healthy": True` — a literal
  constant. If the Flask process were dead it could not serve `/status` at all,
  so the field is always `True` by the time the handler runs. Do not call
  `health()` (unnecessary indirection) and do not make an outbound HTTP request
  to `localhost:8000/health` (adds a socket round-trip and fails identically to
  the process dying). The field exists in the JSON so the dashboard can display
  a green "App healthy" card; its value is proven by the response itself.

- **Port in acceptance criteria**: the app runs on port **8000** (not 80).
  The URL is `http://<ec2-ip>:8000/dashboard`, matching the existing security
  group ingress rule.

- **Crash button UX**: because `mode=crash` calls `os._exit(1)`, the fetch()
  call from the browser will receive a connection error. The button handler
  should treat network errors as "crash triggered — container restarting"
  rather than showing a generic error.

- **boto3 lazy import**: follow the existing pattern in `_heartbeat_loop` —
  import boto3 inside the `/status` view function (or in a helper called by
  it), not at module top-level, so the app starts cleanly in environments
  without boto3 credentials (e.g. local Docker without AWS env vars).
