# Spec: SSM Restart Spike

## Overview

A throwaway proof that AWS Systems Manager Run Command can restart a running
Docker container on an EC2 instance without SSH. De-risks the core remediation
mechanism before any real infrastructure is built.

## Requirements

1. One EC2 instance (Amazon Linux 2023, t3.micro) with the SSM agent running
   and registered as a managed instance.
2. An instance profile granting only the SSM-agent permissions needed to register
   and receive commands.
3. A dummy container running on the box (e.g. nginx) named `sentinel-app`.
4. A single SSM SendCommand using the `AWS-RunShellScript` document that runs
   `docker restart sentinel-app`.

## Out of scope

- Lambda, alarms, CI/CD, Terraform. All manual/CLI here.
- Any production hardening. This instance gets deleted after the spike.

## Acceptance criteria

- `aws ssm send-command` targeting the instance completes without a client-side
  error (command is accepted by SSM).
- Command status in SSM history shows **Success** (not Pending, InProgress, or Failed).
- `docker ps` on the instance shows `sentinel-app` with a fresh "Up X seconds"
  uptime after the command — confirming the restart actually executed.
- SSM command output (stdout/stderr) is captured in the command history and shows
  no `docker restart` error (e.g. no "No such container" message).
- The command runs as root inside the instance (verify via `whoami` in a test
  command); this confirms the same privilege level the Lambda will use in production.

## Notes

- Start by attaching `AmazonSSMManagedInstanceCore` to the instance profile so the
  agent registers. Then note in a scratch file which exact IAM actions the agent
  actually used. That list becomes the hand-written least-privilege policy in Feature 8.
- Amazon Linux 2023 ships the SSM agent pre-installed. The agent needs outbound
  internet (or VPC endpoints for ssm, ssmmessages, ec2messages) to register.
- Install Docker: `dnf install -y docker && systemctl enable --now docker`.
- If the instance never appears in Fleet Manager → Managed Instances, the cause is
  almost always (a) no instance profile attached at launch, or (b) no network path
  to the SSM endpoints.
- This instance gets deleted after the spike. Do not build anything permanent here.






