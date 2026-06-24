## Overview

The Flask app already contains a heartbeat publisher that emits `Sentinel/Heartbeat=1`
to CloudWatch every 60 seconds, but it is disabled in production. This feature enables
it and adds a CloudWatch alarm that treats **missing data as breaching** — so when the
container dies silently (no error logs, just gone), the alarm fires and routes the
incident through the exact same Lambda → SSM → restart path as the error alarm.
This closes the one blind spot the error-metric alarm cannot cover.

## Requirements

1. **Enable the heartbeat in the deployed container** (`deploy.yml`): add
   `-e HEARTBEAT_ENABLED=true` to the `docker run` command in the GitHub Actions
   deploy step. No changes to `app.py` — the publisher is already there.

2. **EC2 instance role: `cloudwatch:PutMetricData`** (`infra/ec2.tf`): add an inline
   `aws_iam_role_policy` on `sentinel_ec2` that allows `cloudwatch:PutMetricData`
   scoped to the `Sentinel` namespace via the `cloudwatch:namespace` condition key.
   Resource must be `"*"` (CloudWatch does not support resource-level scoping for
   PutMetricData), but the condition key limits it to the `Sentinel` namespace.
   Add this permission before enabling the heartbeat or the thread will log
   `heartbeat publish failed` on every tick.

3. **Heartbeat alarm** (`infra/observability.tf`): new
   `aws_cloudwatch_metric_alarm` resource with:
   - `alarm_name`: `"sentinel-heartbeat-missing"`
   - `namespace`: `"Sentinel"`, `metric_name`: `"Heartbeat"`
   - `statistic`: `"Average"`, `period`: `60`, `evaluation_periods`: `2`
   - `comparison_operator`: `"LessThanThreshold"`, `threshold`: `1`
   - `treat_missing_data`: `"breaching"` — this is the mechanism, not a config
     detail; omitting it leaves the alarm sitting silent on a dead app
   - `alarm_actions`: `[aws_sns_topic.incidents.arn]`
   - `ok_actions`: `[aws_sns_topic.incidents.arn]` is NOT needed — no action on
     recovery; the Lambda handles one incident start, not the recovery

4. **Alarm reuses existing heal path entirely**: Lambda → SSM → `docker restart
   sentinel-app` → "Auto-restart attempted" email. No new Lambda, no new SNS topic,
   no new subscription. The incidents topic already has the Lambda subscribed.

## Out of scope

- Any changes to `app.py` — the heartbeat publisher and its daemon-thread guard
  already exist and are correct.
- A separate remediation path for silent death — Feature 8's Lambda handles both
  error-burst and missing-heartbeat incidents identically.
- `ok_actions` on the heartbeat alarm — recovery is silent; only the initial
  ALARM transition matters for paging.
- Alerting on heartbeat *value* anomalies (e.g. duplicate publishes) — the only
  signal we care about is absence.

## Acceptance criteria

**IAM (verify before enabling heartbeat):**
- `aws iam get-role-policy --role-name sentinel-ec2 --policy-name heartbeat-metric`
  returns a policy containing `cloudwatch:PutMetricData` with condition
  `cloudwatch:namespace = "Sentinel"`.
- The Lambda execution role does NOT get `cloudwatch:PutMetricData` — the heartbeat
  runs in the container on EC2, not in Lambda.

**Heartbeat publishing (verify after deploy):**
- CloudWatch console → Metrics → Custom namespaces → `Sentinel` → `Heartbeat`
  shows data points arriving approximately every 60 seconds.
- The container log stream in CloudWatch shows structured JSON lines:
  `{"level": "INFO", "message": "heartbeat published", ...}` every ~60s.
- No `heartbeat publish failed` lines appear in the logs.

**Alarm configuration (verify in console or via CLI):**
- `aws cloudwatch describe-alarms --alarm-names sentinel-heartbeat-missing`
  shows `TreatMissingData: breaching`, `ComparisonOperator: LessThanThreshold`,
  `Threshold: 1.0`, `EvaluationPeriods: 2`, `Period: 60`.
- Alarm state is `OK` while the container is running and publishing.

**End-to-end silent-death path (the headline demo):**
- `curl /simulate-failure?mode=crash` terminates the container (exit code 1,
  `docker ps` shows it gone).
- Within 2–3 minutes (≤ 2 missed 60s windows + CloudWatch propagation delay)
  the alarm transitions `OK → ALARM`.
- Lambda is invoked (visible in Lambda → Monitoring → Invocations).
- `docker ps` on the instance shows `sentinel-app` with a fresh "Up X seconds"
  uptime after remediation.
- "Auto-restart attempted — check dashboard" email arrives at
  `sjeevitha679@gmail.com`.

**No regression on the error path:**
- Triggering `?mode=error` still fires `sentinel-app-errors` alarm → Lambda →
  email as before. The heartbeat alarm is unaffected by error bursts.

**Terraform hygiene:**
- `terraform plan` shows exactly two new resources: the `cloudwatch:PutMetricData`
  inline policy and the `sentinel-heartbeat-missing` alarm. No other changes.
- `terraform plan` is clean (zero diff) immediately after `terraform apply`.

## Notes

- **`TreatMissingData = breaching` is the mechanism, not a config knob.** The
  default is `notBreaching`, which would leave the alarm in `INSUFFICIENT_DATA` or
  `OK` when the app dies — exactly the blind spot this feature exists to close.
  CloudWatch will not automatically fill in zeros for a missing custom metric.

- **`evaluation_periods = 2`**: two consecutive 60s windows with no data before
  firing. One missed publish (network blip, slow boot) does not page anyone. Two
  consecutive missing windows means the app is definitely not running.

- **IAM condition key for PutMetricData**: `cloudwatch:namespace` is the only
  available scope for this action (no resource-level ARN support). Use:
  ```
  Condition = { StringEquals = { "cloudwatch:namespace" = "Sentinel" } }
  ```
  This is less obvious than resource-level scoping — worth calling out in review
  as the least-privilege approach for metrics.

- **Daemon thread**: the heartbeat thread in `app.py` is a `daemon=True` thread.
  It dies automatically when the Flask process exits — no cleanup needed, and no
  risk of the thread keeping a crashed process alive.

- **`--restart unless-stopped`**: the EC2 container is already configured with this
  flag in the deploy command. After Lambda issues `docker restart sentinel-app`, the
  container comes back up and the heartbeat resumes — the alarm self-heals to OK
  within the next 60s window.
