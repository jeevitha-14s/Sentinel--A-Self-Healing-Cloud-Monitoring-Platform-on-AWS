# Sentinel — Self-Healing Cloud Monitoring Platform

A production-grade AWS platform that detects application failures and automatically restarts the container — with no human intervention required. Built with Python, Docker, GitHub Actions, AWS (EC2, CloudWatch, Lambda, SNS, SQS), and Terraform.

---

## What it does

Sentinel closes a complete detect → heal → alert loop:

- A code push triggers GitHub Actions, which builds a Docker image tagged with the git SHA, pushes it to ECR, and deploys it to EC2 via SSM Run Command — the same primitive that heals the app also deploys it
- When the app fails noisily (errors logged), a CloudWatch metric filter counts ERROR lines, an alarm fires exactly once per incident, SNS triggers the Lambda, SSM restarts the container, and you get a human-readable email
- When the app dies silently (container exits with no logs), the heartbeat metric stops publishing, CloudWatch treats missing data as breaching, and the same Lambda restarts it — no errors logged, no alarm missed
- If remediation itself fails, you get a "human needed" email and the Lambda error is visible in CloudWatch logs

---

## Architecture

```
Code push
  → GitHub Actions (build + tag with git SHA)
  → ECR (Docker image)
  → EC2 via SSM Run Command (deploy)
  → Flask app (structured JSON logs to stdout)
  → awslogs driver → CloudWatch log group /sentinel/app
  → metric filter { $.level = "ERROR" } → Sentinel/AppErrors
  → CloudWatch alarm (OK→ALARM fires once — free dedup)
  → sentinel-incidents SNS topic
  → Lambda (sentinel-remediation)
  → SSM Run Command (docker restart sentinel-app)
  → sentinel-alerts SNS topic → email

Flask app also publishes Sentinel/Heartbeat=1 every 60s
  → heartbeat alarm (TreatMissingData=breaching)
  → fires on silence — catches container death with no error logs
  → same Lambda → SSM → email path
```

---

## Key design decisions

**Dedup is free — no DynamoDB needed**
CloudWatch alarms are state machines. The SNS action fires on the OK→ALARM *transition*, not on every evaluation that finds the threshold crossed. A sustained error condition keeps the alarm in ALARM without re-triggering it. One incident = one notification.

**Two detection paths, one remediation path**
The error alarm catches noisy failures (app logs errors). The heartbeat alarm catches silent failures (container exits with no output). Both route to the same `sentinel-incidents` topic and the same Lambda. Detection is specialised; remediation is generic.

**Two SNS topics, not one**
`sentinel-incidents` is machine-to-machine (alarm → Lambda). `sentinel-alerts` is machine-to-human (Lambda → email). Without this separation, raw CloudWatch alarm JSON would land in your inbox unfiltered, and you'd be paging yourself for noise instead of decisions.

**Hand-written least-privilege IAM**
Every IAM permission was added in response to a real `AccessDenied` error, documented in `docs/iam-scratch.md`. No managed policies in the remediation path. No wildcards except where AWS does not support resource-level scope.

**SSM Run Command — not SSH**
The deploy and heal primitives are identical: SSM Run Command with `AWS-RunShellScript`. No bastion host, no open port 22, no key pairs. The instance profile is the identity.

**Infrastructure as code from day one**
Terraform covers the entire stack. `terraform destroy && apply` from a clean checkout produces a working platform — verified in Feature 12.

---

## Demo

### 1. Noisy failure — error alarm path

```bash
curl "http://<EC2_IP>:8000/simulate-failure?mode=error"
```

- App logs 5 structured ERROR lines to CloudWatch
- Metric filter increments `Sentinel/AppErrors`
- Alarm transitions OK → ALARM (fires once — dedup)
- SNS publishes to `sentinel-incidents`
- Lambda calls `ssm:SendCommand` → `docker restart sentinel-app`
- Email arrives: "Auto-restart attempted — check dashboard"

### 2. Silent failure — heartbeat alarm path (the impressive one)

```bash
curl "http://<EC2_IP>:8000/simulate-failure?mode=crash"
```

- Container process calls `os._exit(1)` — no logs, no errors, just gone
- Heartbeat stops publishing to CloudWatch
- Alarm requires 2 × 60s missing-data periods to breach, but CloudWatch's own
  evaluation delay for `TreatMissingData=breaching` alarms means this takes
  **~7-8 minutes in practice**, not ~2 minutes — verified end-to-end on
  2026-07-08 (crash at 09:40:00 UTC, alarm at 09:47:22, healed by 09:48:22).
  Budget for this when timing a live demo.
- Same Lambda → SSM → email path fires
- Email arrives without a single error ever being logged

### 3. Remediation failure path

Force an IAM `AccessDenied` on `ssm:SendCommand`:

- Lambda publishes "Human needed — auto-remediation failed"
- Lambda re-raises the exception → visible as error in CloudWatch logs
- SNS retries the Lambda automatically (async invocation)

### 4. Infrastructure proof

```bash
cd infra
terraform destroy -auto-approve
terraform apply -auto-approve
terraform plan  # must show "No changes"
```

Rebuilt stack passes the full demo from zero.

---

## Tech stack

| Layer | Technology |
|---|---|
| App | Python 3.12, Flask, python-json-logger |
| Container | Docker (non-root, exec-form CMD, stdout JSON) |
| Registry | AWS ECR |
| Compute | AWS EC2 (Amazon Linux 2023, t3.micro) |
| Deploy | GitHub Actions → ECR → SSM Run Command |
| Logs | CloudWatch Logs (awslogs driver, JSON metric filter) |
| Alarms | CloudWatch metric alarms (2 — errors + heartbeat) |
| Messaging | AWS SNS (2 topics — incidents + alerts) |
| Remediation | AWS Lambda (Python 3.12, boto3) |
| Remote exec | AWS SSM Run Command |
| IaC | Terraform (local state) |

---

## Project structure

```
sentinel/
├── app.py                    # Flask app — health, simulate-failure, heartbeat publisher
├── requirements.txt
├── Dockerfile
├── .dockerignore
├── .github/
│   └── workflows/
│       └── deploy.yml        # GitHub Actions — build → ECR → SSM deploy
├── lambda/
│   └── remediation.py        # Lambda handler — SSM restart + SNS alert
├── infra/
│   ├── provider.tf           # AWS provider, variables, caller identity
│   ├── ecr.tf                # ECR repository
│   ├── ec2.tf                # EC2 instance, IAM role + policies, security group
│   ├── sns.tf                # SNS topics + subscriptions
│   ├── observability.tf      # CloudWatch log group, metric filter, alarms
│   └── lambda.tf             # Lambda function, IAM, archive, SNS permission
├── docs/
│   ├── specs/                # Feature specs (01–12)
│   └── iam-scratch.md        # AccessDenied → permission mapping (interview evidence)
└── CLAUDE.md                 # Project context and hard rules
```

---

## IAM design

Each permission is scoped to the minimum required resource. From `docs/iam-scratch.md`:

| Principal | Permission | Scoped to |
|---|---|---|
| EC2 instance role | `ecr:GetAuthorizationToken` + push actions | This ECR repo only |
| EC2 instance role | `logs:CreateLogStream`, `DescribeLogStreams`, `PutLogEvents` | `/sentinel/app` log group |
| EC2 instance role | `cloudwatch:PutMetricData` | `Sentinel` namespace only (condition key) |
| EC2 instance role | SSM agent actions | Managed by `AmazonSSMManagedInstanceCore` |
| Lambda execution role | `ssm:SendCommand` | `AWS-RunShellScript` document + this instance |
| Lambda execution role | `ssm:GetCommandInvocation` | `*` (AWS does not support resource-level scope) |
| Lambda execution role | `sns:Publish` | `sentinel-alerts` topic only |
| Lambda execution role | `logs:*` | `/aws/lambda/sentinel-remediation` log group only |
| CI IAM user | ECR push actions | This ECR repo only |
| CI IAM user | `ssm:SendCommand` | This EC2 instance only |

---

## Running locally

```bash
# Build and run the container
docker build -t sentinel .
docker run -p 8000:8000 sentinel

# Test endpoints
curl http://localhost:8000/health
curl "http://localhost:8000/simulate-failure?mode=error"

# Heartbeat is disabled locally by default (no HEARTBEAT_ENABLED=true)
```

---

## Deploying

```bash
# Prerequisites: AWS credentials, Terraform, Docker

cd infra
terraform init
terraform apply

# Set GitHub Actions secrets:
# AWS_ACCESS_KEY_ID, AWS_SECRET_ACCESS_KEY, AWS_REGION
# ECR_REPO, EC2_INSTANCE_ID

# Push to main to trigger deploy
git push origin main
```

---

## What I'd do differently in production

**OIDC instead of long-lived CI keys**
GitHub Actions supports OpenID Connect federation with AWS. The CI job assumes an IAM role directly without storing `AWS_ACCESS_KEY_ID` or `AWS_SECRET_ACCESS_KEY` as secrets. Eliminates the risk of key rotation failures and credential leaks.

**S3 + DynamoDB for Terraform state**
Local state works for a portfolio project but breaks for a team. The production upgrade is an S3 backend with a DynamoDB lock table to prevent concurrent applies. Note: this DynamoDB is for *state locking* — unrelated to any deduplication concern.

**Verify-and-cap loop in Lambda**
`ssm.send_command()` is fire-and-forget — it returns a command ID immediately without confirming the container actually restarted. The production fix is to poll `ssm:GetCommandInvocation` until the command reaches a terminal status, then report success or failure accordingly.

**SQS Dead Letter Queue on Lambda**
If Lambda fails after SNS retries are exhausted, the failure is currently lost. Adding an SQS DLQ on the Lambda's async invocation config captures every unrecoverable failure for later inspection or replay.

**CloudWatch Dashboard**
A live dashboard showing both alarm states, the heartbeat metric, and recent Lambda invocations would make the platform self-explanatory without console access.

