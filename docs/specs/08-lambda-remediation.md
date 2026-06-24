## Overview
A Lambda function triggered by the `sentinel-incidents` SNS topic that closes the
self-heal loop: it issues an SSM `docker restart` command against the EC2 instance and
publishes a human-readable outcome ("attempted" or "human needed") to `sentinel-alerts`.
This is the feature everything else was built toward — detection fires once, Lambda acts
once, a person gets one clear decision in their inbox.

## Requirements

1. **Lambda function** (`infra/lambda.tf`):
   - Runtime: `python3.12`; handler: `remediation.handler`
   - Source: `lambda/remediation.py`, packaged as a ZIP via Terraform's
     `archive_file` data source
   - Triggered by `sentinel-incidents` SNS topic via `aws_sns_topic_subscription`
     (protocol = `"lambda"`)
   - On invoke: call `ssm:SendCommand` with document `AWS-RunShellScript`,
     command `docker restart sentinel-app`, targeting the EC2 instance by ID
   - If `SendCommand` is accepted → publish
     `"Auto-restart attempted — check dashboard"` to `sentinel-alerts`
   - If `SendCommand` raises any exception → publish
     `"Human needed — auto-remediation failed"` to `sentinel-alerts`,
     then re-raise the exception so Lambda marks the invocation as an error
   - Structured JSON logs to stdout on every invocation (event summary, outcome,
     SSM command ID if available)

2. **Lambda execution role** (hand-written IAM, `infra/lambda.tf`):
   - `ssm:SendCommand` scoped to two resources:
     - document ARN: `arn:aws:ssm:ap-south-1::document/AWS-RunShellScript`
     - instance ARN: `arn:aws:ec2:ap-south-1:262439760394:instance/i-071b05ca4482807f2`
   - `ssm:GetCommandInvocation` scoped to `*` (no resource-level scope available)
   - `sns:Publish` scoped to `sentinel-alerts` ARN only:
     `arn:aws:sns:ap-south-1:262439760394:sentinel-alerts`
   - `logs:CreateLogGroup`, `logs:CreateLogStream`, `logs:PutLogEvents` — basic
     Lambda logging (scoped to `/aws/lambda/<function-name>`)
   - Nothing else — no wildcards, no managed policies

3. **SNS → Lambda subscription** (`infra/lambda.tf`):
   - `aws_sns_topic_subscription` with `protocol = "lambda"` on `sentinel-incidents`
   - `aws_lambda_permission` granting SNS the right to invoke the function
     (`Principal = "sns.amazonaws.com"`, scoped to the `sentinel-incidents` ARN)

4. **Lambda package** (`lambda/remediation.py`):
   - `archive_file` data source zips `lambda/remediation.py` into a deployment
     package; Terraform passes the ZIP path to `aws_lambda_function.filename`
   - The `lambda/` directory is created at the project root (not inside `infra/`)

## Out of scope

- Verify-and-cap loop — deliberately cut; "attempted, check dashboard" is the
  intentional scope boundary (say so in review: it's a decision, not an omission)
- DLQ on the Lambda async invocation config (cut from the build plan)
- LLM incident summary enrichment (stretch goal, not building)
- Any retry logic inside the function — SNS async invocation already retries
  twice on Lambda errors; the function should not obscure failures by catching
  and swallowing

## Acceptance criteria

**Happy path (end-to-end):**
- `curl /simulate-failure?mode=error` on the EC2 app triggers the alarm
- CloudWatch alarm transitions from OK → ALARM exactly once
- Lambda is invoked (visible in Lambda monitoring → invocations graph)
- `docker ps` on the instance shows `sentinel-app` with a fresh "Up X seconds" uptime
- `"Auto-restart attempted — check dashboard"` email arrives at `sjeevitha679@gmail.com`

**Failure path:**
- Pointing the SSM command at a non-existent instance ID (or revoking the
  `ssm:SendCommand` permission temporarily) causes Lambda to catch the error,
  publish `"Human needed — auto-remediation failed"` to `sentinel-alerts`, and
  then raise — the Lambda invocation shows as an error in CloudWatch
- `"Human needed"` email arrives at `sjeevitha679@gmail.com`
- Lambda error is visible in `/aws/lambda/<function-name>` log group with a
  structured JSON error line

**Infrastructure:**
- `aws_lambda_function` exists in the AWS console (ap-south-1) after `terraform apply`
- `aws_sns_topic_subscription` for the Lambda on `sentinel-incidents` shows status
  `Confirmed` (Lambda subscriptions are auto-confirmed — no manual step needed)
- `aws_lambda_permission` for SNS → Lambda invocation exists in Terraform state
- Lambda execution role policy contains only the four scoped permission sets
  listed in Requirement 2 — no wildcards beyond those explicitly noted
- `terraform plan` shows zero changes immediately after `terraform apply`

**Logging:**
- Every Lambda invocation writes at least one structured JSON log line to
  `/aws/lambda/<function-name>` in CloudWatch containing the outcome
  (`attempted` or `human_needed`) and the SSM command ID (when available)

## Notes

- **SNS async invocation**: SNS calls Lambda asynchronously. The function does not
  need to return a meaningful value; Lambda automatically retries twice on error
  before giving up. Do not catch and swallow exceptions on the failure path — the
  re-raise keeps the retry/error semantics intact.
- **IAM build sequence**: Start with an empty role (just the assume-role policy for
  `lambda.amazonaws.com`). Add each permission only when Terraform apply + a test
  invocation produces a real `AccessDenied`. Record every error→permission mapping
  in `docs/iam-scratch.md` — this is interview evidence.
- **ssm:GetCommandInvocation**: not strictly required by the current implementation
  (we don't poll for command completion — we only check that `SendCommand` was
  accepted), but including it now avoids a second IAM iteration if polling is added.
  Remove it if it never produces an `AccessDenied`.
- **Hard rule**: per `CLAUDE.md`, the alert path must never be blocked by any
  enrichment. The function is already enrichment-free, so this is satisfied
  automatically — but don't add any blocking call (HTTP, model API) later without
  wrapping it in a timeout + try/except that logs and continues.
- **Terraform resource layout**: keep Lambda IAM, function, and SNS wiring in a
  single new file `infra/lambda.tf` to avoid `observability.tf` or `ec2.tf` growing
  beyond their scope.
- **Hardcoded ARNs**: instance ARN and `sentinel-alerts` ARN are hardcoded in the
  Lambda environment variables (passed via Terraform `environment` block), not
  baked into `remediation.py`. This keeps the function testable with a different
  target without code changes.
