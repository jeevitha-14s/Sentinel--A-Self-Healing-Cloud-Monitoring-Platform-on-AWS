## Overview

Replace the current inline-HTML dashboard (Feature 13) with a richer,
demo-optimised monitoring UI served from a separate `dashboard/index.html`
file. The new dashboard shows a live pipeline animation, sparkline history
bars, a scrolling event log, and three one-click demo controls — making the
self-heal story immediately legible to a technical interviewer without any
narration. All state is held in a Python dict in memory; no database is
introduced.

## Requirements

### Backend — new and changed endpoints

1. **`GET /dashboard`** — serves `dashboard/index.html` via
   `send_from_directory("dashboard", "index.html")`. Replaces the current
   `render_template_string(DASHBOARD_HTML)` implementation; `DASHBOARD_HTML`
   constant and its import of `render_template_string` are removed from
   `app.py`.

2. **`GET /api/status`** — new endpoint replacing `/status` for dashboard
   use. Returns JSON with the following shape:
   ```json
   {
     "system_ok":               true | false,
     "app_status":              "healthy" | "restarting" | "down",
     "heartbeat_alarm":         "OK" | "ALARM" | "INSUFFICIENT_DATA",
     "error_alarm":             "OK" | "ALARM" | "INSUFFICIENT_DATA",
     "error_rate":              0,
     "auto_heals":              0,
     "seconds_since_heartbeat": 42,
     "incidents":               [{"ts": "…", "event": "…"}],
     "pipeline_stage":          "idle" | "error" | "alarm" | "lambda" | "ssm" | "alert",
     "last_updated":            "2026-06-24T13:00:00Z"
   }
   ```
   - `system_ok` — `true` iff both alarms are `"OK"`.
   - `app_status` — `"restarting"` for 90 s after a crash simulation;
     `"healthy"` otherwise (process is alive by definition if this returns).
   - `error_rate` — count of simulated errors fired since last reset.
   - `auto_heals` — count of times `heal_reset` has been called or a crash
     has recovered (incremented in memory).
   - `pipeline_stage` — drives the pipeline animation; set by the simulate
     mode and cleared to `"idle"` after 90 s.
   - CloudWatch call uses the same boto3 lazy-import and 4 s server-side
     cache (reduced from 25 s to make 5 s client polling meaningful).
   - On CloudWatch failure, returns HTTP 200 with alarm fields set to
     `"INSUFFICIENT_DATA"` and `"cw_error": "<message>"`. Must never 5xx.

3. **`POST /api/simulate`** — new endpoint accepting a JSON body
   `{"mode": "<mode>"}` or a form field `mode`. Three modes:
   - `error_burst` — fires 5 `logging.error` calls (same as existing
     `simulate_failure?mode=error`), increments `error_rate`,
     sets `pipeline_stage = "error"`, appends to incident log.
   - `silent_crash` — sets `app_status = "restarting"`, `pipeline_stage =
     "alarm"`, calls `os._exit(1)`. Incident log entry written before exit
     (won't survive crash — expected).
   - `heal_reset` — resets `error_rate = 0`, `pipeline_stage = "idle"`,
     clears incident log, increments `auto_heals`.
     Returns `{"ok": true}`. Does **not** restart the container — it is a
     demo state reset only.

4. **Keep existing `/status` and `GET /simulate-failure` routes unchanged.**
   They remain functional for backwards compatibility (CI smoke-tests, curl
   checks). No removal, no redirect.

### Frontend — `dashboard/index.html`

5. **System status pill** — top of page; green "All systems healthy" /
   red "Incident in progress" driven by `system_ok`.

6. **Five metric cards**:
   | Card | Field | Display |
   |------|-------|---------|
   | App Status | `app_status` | "Healthy" / "Restarting…" / "Down" with colour |
   | Heartbeat | `heartbeat_alarm` | OK / ALARM / INSUFFICIENT_DATA |
   | Error Rate | `error_rate` | integer count since last reset |
   | Auto-Heals | `auto_heals` | integer count (persists across resets) |
   | Last Heartbeat | `seconds_since_heartbeat` | seconds ago, coloured by age |

7. **Pipeline diagram** — horizontal flow:
   `Flask App → CloudWatch → Alarm → Lambda → SSM → Alert`
   Each stage is a labelled node connected by arrows. The node matching
   `pipeline_stage` is highlighted (animated pulse); upstream nodes show
   as active, downstream as idle. Resets to all-idle when
   `pipeline_stage == "idle"`.

8. **Sparkline bars** — two compact bar-history strips below the cards:
   - Heartbeat bar: last 10 `seconds_since_heartbeat` samples, coloured
     green < 70 s, amber 70–119 s, red ≥ 120 s.
   - Error rate bar: last 10 `error_rate` samples, coloured by magnitude.
   Both histories are maintained client-side from successive `/api/status`
   polls; they reset on page reload.

9. **Scrolling event log** — newest-first list of `incidents` entries.
   Maximum 10 entries shown. Each row: timestamp + event text + a tag badge
   ("logged" / "human needed").

10. **Three demo buttons**:
    - "Error Burst" → `POST /api/simulate {"mode":"error_burst"}`
    - "Silent Crash" → `POST /api/simulate {"mode":"silent_crash"}`
    - "Heal & Reset" → `POST /api/simulate {"mode":"heal_reset"}`
    Each button shows inline feedback text for 2.5 s after the response.
    The crash button treats a network error as confirmation (container died).

11. **Polling** — `GET /api/status` every 5 s via `fetch()`. No page reload.
    On fetch failure, the status pill turns grey ("Connection lost") and
    retries silently. Reconnects automatically when the container restarts.

12. **No external JS libraries or CDN links.** All CSS and JS is inline in
    `dashboard/index.html`. No `<script src="...">` pointing outside the
    container.

### Infrastructure

13. **Dockerfile** — add `COPY dashboard/ /app/dashboard/` after the
    existing `COPY app.py /app` line so the HTML file is available inside
    the container.

14. **No new IAM permissions** — the existing `cloudwatch:DescribeAlarms`
    policy (Feature 13) covers the only AWS call in `/api/status`.

15. **No Terraform changes** — no new AWS resources.

## Out of scope

- Removing or changing `/status`, `/simulate-failure`, or `/dashboard`'s
  existing render logic until this feature is verified end-to-end.
- Any Terraform resource changes.
- Persistent storage of heals, error counts, or the incident log across
  container restarts (in-memory only; reset on crash/restart).
- Authentication on any dashboard or API endpoint.
- Mobile responsiveness.
- LLM-generated incident summaries (must never be in the alert path per
  CLAUDE.md hard rule).

## Acceptance criteria

### Endpoints
- `GET /dashboard` returns HTTP 200, `Content-Type: text/html`, and serves
  the `dashboard/index.html` file (not an inline Python string).
- `GET /api/status` returns HTTP 200, `Content-Type: application/json`,
  with all 11 required fields present and correctly typed.
- `GET /api/status` returns HTTP 200 (not 5xx) when the CloudWatch call
  fails; `"cw_error"` key present in that case.
- `GET /api/status` responds in < 200 ms on a cache hit (no AWS call).
- `POST /api/simulate` with `{"mode":"error_burst"}` returns HTTP 200 and
  increments `error_rate` by 5.
- `POST /api/simulate` with `{"mode":"heal_reset"}` returns HTTP 200,
  resets `error_rate` to 0, and clears the incident log.
- `POST /api/simulate` with an unknown mode returns HTTP 400.
- Existing `GET /status` and `GET /simulate-failure` still return HTTP 200
  after this feature is deployed (no regressions).

### Frontend
- Status pill is green on page load when both alarms are OK.
- Status pill turns red within 5 s of `error_alarm` or `heartbeat_alarm`
  entering ALARM state.
- Clicking "Error Burst" shows inline feedback within 1 s; `error_rate`
  card increments on the next poll.
- Clicking "Silent Crash": browser shows "crash triggered — container
  restarting"; status pill turns grey ("Connection lost"); pill recovers
  to green automatically once the container is back.
- Clicking "Heal & Reset": `error_rate` card resets to 0 on the next poll;
  incident log clears.
- Pipeline diagram highlights the correct node for each `pipeline_stage`
  value; returns to all-idle when stage is `"idle"`.
- Sparkline history bars accumulate samples visually across multiple polls.
- No AWS credentials, account IDs, or secret keys appear anywhere in the
  HTML source returned by `GET /dashboard`.

### Docker and deploy
- `docker build` succeeds with the updated Dockerfile.
- `GET http://<ec2-ip>:8000/dashboard` returns HTTP 200 after `git push`
  triggers the GitHub Actions deploy, with no manual steps.
- `terraform plan` shows zero changes (no Terraform files modified).

## Notes

- **`send_from_directory` import**: add `send_from_directory` to the
  existing `from flask import ...` line in `app.py`. The `dashboard/`
  directory path is relative to the Flask app's root (`/app` in the
  container); use `os.path.join(os.path.dirname(__file__), "dashboard")`
  as the directory argument to avoid working-directory ambiguity.

- **In-memory state dict**: a single module-level dict in `app.py` (e.g.
  `_sim_state`) holding `error_rate`, `auto_heals`, `pipeline_stage`, and
  `app_status`. Reset semantics for `heal_reset` must not touch `auto_heals`
  (it counts lifetime heals, not per-incident).

- **`pipeline_stage` is set only by `/api/simulate`** — real CloudWatch
  alarm state (`error_alarm`, `heartbeat_alarm`) does NOT drive it. If a
  genuine alarm fires in AWS without a simulate call, `pipeline_stage`
  stays `"idle"` and the diagram stays unlit. This is intentional and
  acceptable for a demo tool: the pipeline animation exists to narrate a
  triggered demo, not to reflect live AWS event flow. Do not attempt to
  infer `pipeline_stage` from alarm state.

- **`pipeline_stage` auto-clear**: a background timer or a TTL check inside
  `/api/status` resets `pipeline_stage` to `"idle"` and `app_status` to
  `"healthy"` 90 s after a simulate call. A TTL field in `_sim_state`
  (timestamp + 90 s) checked at read time is simpler than a background
  thread.

- **Cache TTL reduction**: `/api/status` uses 4 s cache (down from 25 s in
  `/status`). At 5 s polling this means every other client poll hits AWS;
  acceptable for a demo. Do not reduce the existing `/status` cache — it
  is unrelated to this feature.

- **`POST /api/simulate` content-type**: accept both
  `application/json` (for the dashboard `fetch()`) and
  `application/x-www-form-urlencoded` (for curl testing). Use
  `request.get_json(silent=True) or request.form` to parse.

- **Crash mode ordering**: for `silent_crash`, append the incident log
  entry and set `_sim_state` fields *before* calling `os._exit(1)` —
  the state won't survive, but the ordering is correct and consistent
  with the existing `simulate_failure` behaviour.

- **Dockerfile line order matters**: `COPY dashboard/ /app/dashboard/`
  must come after `COPY app.py /app` to avoid invalidating the pip cache
  layer on every HTML edit.

- **IAM — no changes needed**: `cloudwatch:DescribeAlarms` is already
  granted by `aws_iam_role_policy.cloudwatch_describe` (Feature 13).
