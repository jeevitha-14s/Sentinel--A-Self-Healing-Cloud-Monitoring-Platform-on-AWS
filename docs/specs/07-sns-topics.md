## Overview
Two SNS topics are created in this feature: `sentinel-incidents` (machine-to-machine,
alarm→Lambda) and `sentinel-alerts` (machine-to-human, Lambda→email). Keeping them
separate means your inbox receives a human-readable decision ("auto-restart attempted")
rather than raw CloudWatch alarm JSON, and the Lambda can be subscribed to the incidents
topic independently without touching the alert delivery path.

## Requirements
1. **`sentinel-incidents` topic** (`infra/sns.tf`):
   - Standard SNS topic, no FIFO, no encryption required at this stage.
   - Lambda will subscribe in Feature 8 — no subscription resource here.
2. **`sentinel-alerts` topic** (`infra/sns.tf`):
   - Standard SNS topic.
   - Email subscription to `sjeevitha679@gmail.com` via `aws_sns_topic_subscription`
     (protocol = `"email"`).
3. **Wire the alarm** (`infra/observability.tf`):
   - Both topics are in the same Terraform root module (`infra/`), so replace the
     `var.incidents_topic_arn` conditional in `observability.tf` with a direct
     reference to `aws_sns_topic.incidents.arn`.
   - The `incidents_topic_arn` variable in `provider.tf` can be removed (it was a
     placeholder for this exact moment).
4. **Email confirmation**: after `terraform apply`, manually click the confirmation
   link that AWS sends to `sjeevitha679@gmail.com`. Subscription stays in
   `PendingConfirmation` until confirmed — test publish only after confirming.
5. **IAM scratch update**: add an anticipated row to `docs/iam-scratch.md` for
   `sns:Publish` on the `sentinel-alerts` ARN — Lambda will need it in Feature 8.

## Out of scope
- Lambda code or Lambda subscription to `sentinel-incidents` (Feature 8).
- Dead-letter queue (Feature 9).
- SMS, HTTP, or any other subscription protocol.
- SNS access policies or resource-based policies beyond AWS defaults.
- Encryption (KMS) on either topic.

## Acceptance criteria
- Both topics (`sentinel-incidents`, `sentinel-alerts`) exist in `ap-south-1` in
  the AWS console after `terraform apply`.
- `aws_sns_topic_subscription` for `sjeevitha679@gmail.com` exists in Terraform
  state; its status shows `PendingConfirmation` before the email link is clicked.
- After clicking the confirmation link, the subscription status in the SNS console
  changes to `Confirmed`.
- A manual test publish to `sentinel-alerts` (via AWS console or CLI) delivers an
  email to `sjeevitha679@gmail.com` within ~60 seconds.
- The `alarm_actions` list on `sentinel-app-errors` (CloudWatch console) contains
  the `sentinel-incidents` ARN — not an empty list.
- `terraform plan` is clean (zero changes) immediately after `terraform apply`.
- `sentinel-incidents` has no subscriptions yet (Lambda subscription deferred to
  Feature 8) — confirm in the SNS console subscriptions tab.
- `docs/iam-scratch.md` has a new anticipated row for `sns:Publish` on
  `sentinel-alerts` ARN scoped to the Lambda execution role.

## Notes
- **Two topics, not one.** `incidents` is the alarm's action target — it carries
  raw SNS notification JSON. `alerts` is what humans read. Conflating them would
  mean every CloudWatch alarm JSON lands in your inbox unfiltered.
- **Variable removal.** `var.incidents_topic_arn` in `provider.tf` was a temporary
  scaffold added in Feature 6 so `observability.tf` would be valid before the topic
  existed. Since both resources now live in the same root module, replace the
  conditional with `[aws_sns_topic.incidents.arn]` directly and delete the variable.
- **Email confirmation is manual and one-time.** AWS will not deliver to an
  unconfirmed endpoint; skip confirmation and the test publish will silently succeed
  on the API side but nothing arrives in your inbox.
- **Run `terraform apply` twice if needed.** First apply creates the topics and
  email subscription. If the alarm update (removing the variable) is in the same
  apply it all runs together — but if you split it, re-apply after updating
  `observability.tf` to confirm alarm_actions is non-empty.
- **No IAM changes required for the topics themselves.** CloudWatch's service
  principal can publish to SNS via alarm actions without additional resource policy.
  The Lambda→`sentinel-alerts` `sns:Publish` permission is scoped to Feature 8.
