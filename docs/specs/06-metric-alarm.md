## Overview
A CloudWatch alarm on the `Sentinel/AppErrors` metric that transitions from OK
to ALARM exactly once per incident and publishes to the `sentinel-incidents` SNS
topic. The alarm's state-machine (OK→ALARM fires the action; ALARM→ALARM does
not) gives deduplication for free — no DynamoDB or external state store needed.

## Requirements
1. CloudWatch alarm (Terraform, `infra/observability.tf`) on metric
   `Sentinel/AppErrors`, statistic `Sum`, threshold `>= 1`, over a single 60s
   evaluation period.
2. `treat_missing_data = "notBreaching"` — silence between incidents is healthy
   and must not trigger the alarm.
3. `alarm_actions` points to the `sentinel-incidents` SNS topic ARN. Because the
   topic is created in Feature 7, use a Terraform variable
   `var.incidents_topic_arn` (default `""`) so the block is valid now and wired
   properly in Feature 7.
4. No `ok_actions` and no `insufficient_data_actions` — the alarm notifies once
   on incident entry only.
5. The alarm self-resets to OK when a full 60s window passes with zero errors
   (possible because the metric filter already sets `default_value = "0"`).

## Out of scope
- Creation of the `sentinel-incidents` SNS topic (Feature 7).
- Lambda function, email subscriptions, or DLQ (Features 7–9).
- Re-notification on sustained errors — the alarm stays in ALARM state silently
  until errors clear.
- Any IAM changes — CloudWatch alarms publish to SNS using a service-linked
  trust; no role additions are needed here.

## Acceptance criteria
- `terraform plan` is clean with `var.incidents_topic_arn = ""` (empty string,
  no action configured yet).
- After `terraform apply`, the alarm exists in CloudWatch console in OK state.
- Hitting `/simulate-failure?mode=error` once drives the alarm to ALARM state
  within ~60–90 seconds (one evaluation period plus propagation delay).
- Holding `/simulate-failure?mode=error` calls does **not** trigger a second SNS
  publish — the alarm remains in ALARM without re-firing.
- Stopping errors causes the alarm to return to OK within one 60s period
  (validated via CloudWatch console state history).
- A healthy app with no errors keeps the alarm in OK indefinitely (missing data
  treated as notBreaching, confirmed by inspecting the alarm's history).

## Notes
- **Dedup mechanism**: CloudWatch alarm actions fire only on state *transitions*.
  OK→ALARM fires the `alarm_actions` list once. While the alarm stays in ALARM,
  no further publishes occur. This is the "no DynamoDB" interview sentence — be
  ready to explain it precisely.
- **`default_value = "0"`** on the metric filter (already set in Feature 5) is
  what makes the OK reset reliable. Without it, periods with no log activity emit
  no data point; the alarm would sit in INSUFFICIENT_DATA rather than returning
  to OK. Do not remove that default.
- **Statistic = Sum**: we're counting discrete error events, not averaging a rate.
  Sum over 60s correctly reflects "any errors occurred this minute."
- **Terraform variable pattern**: declare `variable "incidents_topic_arn"` in
  `provider.tf` (or a `variables.tf`) with `default = ""`. The alarm resource
  uses `alarm_actions = var.incidents_topic_arn != "" ? [var.incidents_topic_arn] : []`.
  Feature 7 passes the real ARN.
- No new IAM permissions required in this feature. CloudWatch's service principal
  already has authority to publish to SNS when the alarm action is configured.
