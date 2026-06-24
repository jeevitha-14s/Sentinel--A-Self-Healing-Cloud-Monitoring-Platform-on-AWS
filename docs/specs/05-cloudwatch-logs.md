# Feature 5 — Logs → CloudWatch + metric filter

## Overview
Ship the container's JSON logs to CloudWatch Logs and create a metric filter
that counts ERROR lines into a custom metric. This is the detection layer —
without it the alarm in Feature 6 has nothing to watch. All three pieces must
land together: the IAM permissions, the log group, and the awslogs driver flag
on the container.

## Requirements
1. CloudWatch log group `/sentinel/app` (Terraform, `infra/observability.tf`),
   retention 7 days.
2. Inline IAM policy on the EC2 instance role (`infra/ec2.tf`) granting the
   minimum permissions the awslogs Docker log driver needs:
   `logs:CreateLogStream` and `logs:DescribeLogStreams` scoped to the log group
   ARN; `logs:PutLogEvents` scoped to `<log-group-arn>:log-stream:*`. No broader
   grants. (`AmazonSSMManagedInstanceCore` does not include any logs permissions.)
3. The `docker run` command in `.github/workflows/deploy.yml` must include the
   awslogs driver flags:
   `--log-driver awslogs --log-opt awslogs-region=${{ secrets.AWS_REGION }}
   --log-opt awslogs-group=/sentinel/app --log-opt awslogs-stream=sentinel-app`
   (The log group must already exist before the container starts, or Docker will
   refuse to launch.)
4. Metric filter on the log group: JSON pattern `{ $.level = "ERROR" }` →
   metric `Sentinel/AppErrors`, namespace `Sentinel`, unit `Count`, value `1`
   per match, `default_value = 0`.

## Out of scope
- The CloudWatch alarm on `AppErrors` (Feature 6). Filter and metric only.
- Log Insights queries, dashboards.
- Log encryption (KMS). Note as a production hardening step.

## Acceptance criteria
1. `terraform plan` is clean (no changes) after `terraform apply`.
2. Log group `/sentinel/app` exists in `ap-south-1` with retention = 7 days
   (visible in CloudWatch console or via `aws logs describe-log-groups`).
3. After the next deploy, `docker logs sentinel-app` on the EC2 instance
   returns empty output (all stdout is now routed to CloudWatch, not the
   local daemon).
4. Hitting `/simulate-failure?mode=error` causes `Sentinel/AppErrors` to
   increment in CloudWatch Metrics → Custom namespaces → Sentinel (allow up
   to 1 minute for propagation).
5. Log lines are visible in CloudWatch Log Insights for group `/sentinel/app`
   and are parseable as JSON with a `.level` field
   (query: `fields @timestamp, level, message | limit 20`).
6. The metric filter uses the JSON syntax `{ $.level = "ERROR" }`, not a plain
   text pattern. Verify in the console under Metric filters on the log group.
7. No IAM permission broader than the three listed in Requirement 2 is added to
   the EC2 role for this feature.

## Notes
- **Order of operations matters:** `terraform apply` must complete before the
  next deploy. If the container starts with `--log-driver awslogs` before the
  log group exists, Docker will fail to start the container.
- Use the JSON metric filter syntax (`{ $.level = "ERROR" }`), not a plain text
  pattern. The JSON form matches only records where the `level` field is exactly
  `"ERROR"`, preventing false positives from the word appearing in message text.
- `default_value = 0` on the metric transformation ensures CloudWatch receives
  a zero data-point during quiet periods, so the alarm in Feature 6 stays in
  OK state rather than `INSUFFICIENT_DATA`.
- IAM for awslogs: `AmazonSSMManagedInstanceCore` (already attached) grants
  zero CloudWatch Logs permissions. A new inline policy is required. Scope it
  to the specific log group ARN — use the Terraform resource reference so the
  ARN is not hardcoded.
- After `terraform apply`, push a trivial commit to main (or manually run the
  SSM deploy command) to restart the container with the new awslogs flags.
- Retain the `EC2_INSTANCE_ID` GitHub secret — it is still used in the SSM
  send-command step for the redeploy trigger.
