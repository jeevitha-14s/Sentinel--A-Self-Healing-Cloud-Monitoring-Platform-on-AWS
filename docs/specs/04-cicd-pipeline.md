# Spec: CI/CD Pipeline — ECR Push + SSM Deploy

## Overview

On every push to `main`, GitHub Actions builds the Docker image (from Feature 3),
tags it with the git SHA, pushes it to ECR, then deploys it to EC2 by issuing an
SSM `SendCommand` — the same `AWS-RunShellScript` primitive proven in Feature 1.
This closes the loop between a committed change and a running container, with no
SSH access required anywhere in the path.

## Requirements

1. **ECR repository** (`infra/ecr.tf`): private ECR repo named `sentinel`; image
   scanning on push enabled; `force_delete = true` so `terraform destroy` is
   unblocked during development.
2. **EC2 instance** (`infra/ec2.tf`): Amazon Linux 2023, `t3.micro`, in a public
   subnet with an internet gateway so it can reach ECR and the SSM endpoints.
   Docker installed via user-data. SSM agent pre-installed (AL2023 default).
   Security group: inbound 8000 open (for manual curl checks); inbound 22 closed
   (SSH not used — SSM only). Instance profile attached at launch.
3. **Instance profile** (`infra/ec2.tf`): attach `AmazonSSMManagedInstanceCore`
   managed policy to get the instance registered in Fleet Manager. Note the exact
   IAM actions used; hand-write least-privilege in Feature 8.
4. **CI IAM user** (hand-written, not Terraform): `ecr:GetAuthorizationToken`
   (global), ECR push actions (`ecr:BatchCheckLayerAvailability`,
   `ecr:CompleteLayerUpload`, `ecr:InitiateLayerUpload`, `ecr:PutImage`,
   `ecr:UploadLayerPart`) scoped to the ECR repo ARN,
   `ssm:SendCommand` scoped to the EC2 instance ARN and the
   `AWS-RunShellScript` document ARN. Store the access key as GitHub Actions
   secrets `AWS_ACCESS_KEY_ID` and `AWS_SECRET_ACCESS_KEY`. Add `ECR_REPO`
   (the full registry/repo URI) and `EC2_INSTANCE_ID` as additional secrets.
5. **GitHub Actions workflow** (`.github/workflows/deploy.yml`), triggered on
   `push` to `main`:
   a. Checkout code.
   b. Configure AWS credentials from secrets (no OIDC).
   c. Authenticate Docker to ECR via `aws ecr get-login-password`.
   d. Build image, tag as `$ECR_REPO:${{ github.sha }}`.
   e. Push tagged image to ECR.
   f. Issue `aws ssm send-command` with `AWS-RunShellScript` to the EC2 instance;
      wait for command completion and fail the workflow if the command status is
      not `Success`.
6. **SSM deploy command** (idempotent shell script sent via `send-command`):
   ```
   docker pull <ECR_REPO>:<SHA>
   docker stop sentinel-app || true
   docker rm   sentinel-app || true
   docker run -d \
     --name sentinel-app \
     --restart unless-stopped \
     -p 8000:8000 \
     --log-driver awslogs \
     --log-opt awslogs-region=<region> \
     --log-opt awslogs-group=/sentinel/app \
     --log-opt awslogs-create-group=false \
     <ECR_REPO>:<SHA>
   ```
   The `|| true` guards make stop/rm safe on first deploy (no container yet).
   `--restart unless-stopped` keeps the container alive across instance reboots.
7. **Image tagging**: tag with `github.sha` only — never `latest`. This makes
   the live commit always inspectable via `docker inspect sentinel-app`.

## Out of scope

- CloudWatch log group, metric filters, alarms, Lambda, SNS (Features 5–7).
- OIDC federation — long-lived GitHub secrets are acceptable for a portfolio
  project. Noted as "what I'd do differently" in the Notes section.
- Multi-AZ, load balancer, auto-scaling, or any HA configuration.
- Gunicorn / production WSGI server (already out of scope from Feature 2).
- Terraform remote state backend (S3 + DynamoDB lock) — local state only.
  Do not commit `*.tfstate`.
- ECR lifecycle policies (image retention cleanup).

## Acceptance criteria

- A push to `main` triggers the workflow; every step completes green (exit 0).
- The workflow fails and does **not** attempt an SSM deploy if the Docker build
  or ECR push step fails.
- The SSM command status polled by the workflow is `Success` (not `TimedOut`,
  `Failed`, or `Cancelled`); a non-`Success` status fails the workflow job.
- `docker inspect sentinel-app --format '{{.Config.Image}}'` on the EC2 instance
  returns an image URI ending with `:${{ github.sha }}` for the most recent push.
- The deploy command is idempotent: running the exact same SSM script a second
  time with the same SHA exits 0 and leaves one running container named
  `sentinel-app`.
- No AWS credentials (`AWS_ACCESS_KEY_ID`, `AWS_SECRET_ACCESS_KEY`, account ID,
  or any secret value) appear in workflow logs, in `.github/workflows/deploy.yml`,
  or in any committed file.
- `*.tfstate` and `*.tfstate.backup` are absent from git history (`.gitignore`
  enforces this; verify with `git ls-files infra/*.tfstate`).
- `GET http://<EC2-public-IP>:8000/health` returns HTTP 200 from outside the
  instance after a successful deploy (confirms port binding and security group).
- `docker logs sentinel-app | jq .` on the instance produces no parse errors
  (container is emitting the same structured JSON from Feature 2).
- Container is running as `appuser` (non-root): `docker exec sentinel-app whoami`
  returns `appuser`.
- `docker inspect sentinel-app` shows `--log-driver awslogs` and the
  `awslogs-group` log option set to `/sentinel/app` (even though the group does
  not exist yet — the driver fails silently until Feature 5 creates it).
- `docker inspect sentinel-app` shows `RestartPolicy.Name == "unless-stopped"`.
- The EC2 instance appears in SSM Fleet Manager → Managed Instances before any
  workflow run (prerequisite check — if it does not appear, the SSM deploy step
  will time out).
- `terraform plan` in `infra/` shows no diff after `terraform apply` (state is
  consistent with what was deployed).

## Notes

- **Terraform structure**: start with `infra/provider.tf` (AWS provider, region
  variable), `infra/ecr.tf`, `infra/ec2.tf`. Outputs to expose: ECR repo URI,
  EC2 instance ID, EC2 public IP. Run `terraform init` in `infra/` before the
  first `plan`.
- **SSM command wait**: use `aws ssm wait command-executed --command-id <id>
  --instance-id <id>` in the workflow, then check
  `aws ssm get-command-invocation --query Status` — the `wait` subcommand polls
  until terminal state; a non-Success status should `exit 1` the step.
- **ECR auth in the deploy script**: the SSM command runs as `root` on the
  instance. The instance role must include `ecr:GetAuthorizationToken` and the
  ECR pull actions (`ecr:BatchGetImage`, `ecr:GetDownloadUrlForLayer`) scoped
  to the repo. Add these to the instance profile; record them in
  `docs/iam-scratch.md` for the Feature 8 least-privilege pass.
- **awslogs silent failure**: `docker run` with `--log-driver awslogs` will start
  the container even if the log group `/sentinel/app` does not exist — log lines
  are simply dropped. This is acceptable here; Feature 5 creates the group and
  the driver begins shipping immediately without a container restart.
- **OIDC (what I'd do differently)**: replace the long-lived CI IAM user with an
  OIDC identity provider in IAM and a role with a trust policy scoped to
  `repo:jeevitha-14s/Sentinel:ref:refs/heads/main`. No stored secrets, no
  rotation needed. Build it this way in any production system.
- **GitHub secrets to add after Terraform apply**:
  - `AWS_ACCESS_KEY_ID` / `AWS_SECRET_ACCESS_KEY` — CI IAM user key
  - `ECR_REPO` — full ECR URI (e.g. `123456789012.dkr.ecr.us-east-1.amazonaws.com/sentinel`)
  - `EC2_INSTANCE_ID` — from Terraform output
  - `AWS_REGION` — the region used throughout
- **Do not pre-grant broad policies**: follow the CLAUDE.md rule — add IAM
  actions only in response to a real `AccessDenied`. Start with the explicit
  lists above and expand only if `send-command` or `docker pull` fails with a
  permission error in practice.
- **`.gitignore` additions for this feature**: `.terraform/`, `*.tfstate`,
  `*.tfstate.backup`, `.terraform.lock.hcl` is safe to commit (pin provider
  versions), `infra/.terraform/` if the top-level rule doesn't catch it.
