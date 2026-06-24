## Overview

Tear the entire stack down and rebuild it from code to prove the Terraform
actually provisions a working platform from zero — not just a snapshot of
clicked-together infrastructure. Every resource in `infra/` must be owned by
Terraform, and the end-to-end self-heal loop must work on the rebuilt stack
without any manual intervention beyond confirming the SNS email subscription.
This is the proof step that backs the interview claim "destroyed and rebuilt to
verify it actually works."

## Requirements

1. `terraform destroy` removes all Sentinel-managed resources cleanly with no
   orphaned resources left in the AWS console.
2. `terraform apply` rebuilds the full stack from an empty account state — EC2
   instance, ECR repository, IAM roles and policies, CloudWatch log group,
   metric filters, alarms (error + heartbeat), SNS topics, Lambda function, DLQ,
   and all subscriptions.
3. After rebuild, the SNS email subscription must be re-confirmed (new topic ARN
   = new confirmation link).
4. After rebuild, GitHub Actions secrets `EC2_INSTANCE_ID` must be updated to
   the new instance ID. `ECR_REPO` does not change (ECR repository URLs are
   stable across destroy/apply cycles).
5. After rebuild, the full end-to-end self-heal demo must pass:
   - `/simulate-failure?mode=error` → alarm fires → Lambda SSM-restarts container → alert email arrives.
   - `/simulate-failure?mode=crash` → heartbeat stops → heartbeat alarm fires → Lambda SSM-restarts container → alert email arrives.
6. `terraform plan` run immediately after `terraform apply` must show zero
   changes (no drift between code and live state).

## Out of scope

- Migrating to a remote state backend (S3 + DynamoDB lock table). This is the
  correct production upgrade but is deliberately out of scope for this portfolio
  project. Note: that DynamoDB lock table is for *state locking* — completely
  distinct from the deduplication DynamoDB that was cut from the alarm design
  (Feature 9). Do not conflate them in interviews.
- Any new AWS resources or feature work.
- Terraform modules, workspaces, or CI-driven apply.

## Acceptance criteria

- `terraform destroy` exits 0 and reports "Destroy complete! Resources: N destroyed."
- AWS console shows no Sentinel resources remaining after destroy (check EC2,
  Lambda, SNS, CloudWatch, IAM, ECR).
- `terraform apply` exits 0 and reports "Apply complete! Resources: N added,
  0 changed, 0 destroyed."
- `GET http://<new_public_ip>:8000/health` returns HTTP 200 after the Docker
  container is running on the rebuilt instance.
- `GET /simulate-failure?mode=error` triggers the full error-alarm → SNS →
  Lambda → SSM restart → alert email path on the rebuilt stack.
- `GET /simulate-failure?mode=crash` triggers the heartbeat-missing alarm →
  SNS → Lambda → SSM restart → alert email path on the rebuilt stack.
- SNS alert email is received at the address stored in `var.alert_email` after
  each simulation (confirms the new subscription is live and confirmed).
- `terraform plan` after apply exits 0 and reports "No changes. Your
  infrastructure matches the configuration."
- GitHub Actions CI workflow passes on a push made after updating the
  `EC2_INSTANCE_ID` secret to the new instance ID.

## Notes

- **ECR `force_delete = true` is what makes destroy clean.** Without it,
  Terraform cannot delete the repository while images are present and destroy
  will fail. This flag is already set in `ecr.tf`.
- **Lambda INSTANCE_ID is wired automatically.** The Lambda's
  `INSTANCE_ID` environment variable references `aws_instance.sentinel.id`
  in Terraform, so after `apply` it holds the new instance ID without any
  manual step.
- **Heartbeat alarm fires immediately after rebuild.** The
  `sentinel-heartbeat-missing` alarm uses `treat_missing_data = "breaching"`
  and `evaluation_periods = 2`, so it will fire during the startup window
  before the container is running and publishing heartbeats. This is expected
  behavior — wait for the container to be healthy before running the demo.
- **Lambda ZIP is generated automatically.** The `archive_file` data source
  in `lambda.tf` produces `remediation.zip` at plan time from the source file.
  No manual packaging step is needed after destroy.
- **Check for orphaned resources before destroy.** Any resource created
  manually outside Terraform (e.g. the Feature 1 SSM spike instance) will not
  be in state and will survive destroy. Confirm the console is clean of those
  before treating destroy as a proof of full cleanup.
- **No IAM changes needed.** This feature is purely operational verification —
  no new AWS resources, no new permissions.
- **SNS email re-confirmation is mandatory.** Destroy creates a new topic ARN;
  the old subscription is gone. Check the inbox for a new "AWS Notification —
  Subscription Confirmation" email and click it before running the demo, or
  alerts will be silently dropped.
