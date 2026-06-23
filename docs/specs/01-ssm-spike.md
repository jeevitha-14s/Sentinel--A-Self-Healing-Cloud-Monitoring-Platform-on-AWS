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







**The analogy**

Normal SSH is like calling someone's phone directly — you need their number (IP), they need to pick up (open port 22), and you need a key to prove it's you.

SSM is like a company Slack. The employee (SSM agent on EC2) logs into Slack themselves when they start work. The manager (you/Lambda) posts a message. The employee sees it, does the task, and posts the result back. The manager never called the employee directly — the employee came to the manager.

---

**What actually happened in the spike, in plain English**

1. **Instance profile** — when the EC2 instance launched, we attached an IAM role to it. This gives the instance an identity, like a badge. Without it, AWS wouldn't trust anything the instance says.

2. **SSM agent registers** — Amazon Linux 2023 starts the SSM agent automatically on boot. The agent uses that badge (instance profile) to call AWS and say "I'm here, I'm online." This is why the instance showed up in Fleet Manager with no SSH involved.

3. **Outbound only** — the agent only makes outbound calls over HTTPS (port 443). It never opens any inbound ports. This is why we launched with no security group rules and it still worked.

4. **send-command** — when you ran `aws ssm send-command`, you posted a task to AWS. AWS queued it.

5. **Agent picks it up** — the agent is constantly polling AWS through the `ec2messages` channel asking "any tasks for me?" It got the `docker restart sentinel-app` command, ran it as root, and sent the result back.

6. **CloudTrail gap** — the management stuff (registering, heartbeat) goes through the normal AWS API and shows in CloudTrail. The actual command delivery goes through a separate messaging channel (ssmmessages/ec2messages) which doesn't show in CloudTrail — but is still required for commands to work.

---

**The one sentence that matters for interviews**

> "SSM works by having the agent on the instance reach out to AWS, not the other way around — so there are no inbound ports, no SSH keys, and the Lambda can restart a container just by calling send-command."

---

Now try saying it back to me in your own words — doesn't need to be perfect, just the core idea. Once it clicks, we commit and move on.





❯ Walk me through what just happened in plain English — not the commands, the mechanism.
  Specifically: how does SSM Run Command actually reach the container without SSH?
  What role does the instance profile play? Why did the agent appear in Fleet Manager
  without us opening any inbound ports? And what's the significance of the data-plane
  actions (ssmmessages/ec2messages) not showing up in CloudTrail?

The core reversal: SSM flips the connection direction

With SSH, you initiate a connection inward to the machine. A poro match, and if the machine is behind a NAT or firewall you'restuck.

SSM works the opposite way. The agent on the instance is the one that initiates connections — outward, to AWS. It boots up, grabs credentials from the instance
profile, and opens a persistent HTTPS connection to the SSM messint on it sits there polling: "any commands for me?" When you ran aws ssm send-command, you didn't connect to the instance at all. You dropped a message with AWS. The agent picked it up, ran docker restart sentinel-app
locally on the box, and streamed the output back through the sam

This is why no inbound ports were needed. The security group onl instance from outside. Since every connection was initiated bythe agent going out, the security group never came into play. The TCP sessions all started from inside the instance.

---
What the instance profile actually does

The SSM agent has no hardcoded credentials. When the instance bometadata service — a special link-local address (169.254.169.254)that only exists inside the instance. That service hands back temporary credentials that belong to whatever IAM role is attached to the instance's profile.

The instance profile is the mechanism that lets you attach an IAM role to an EC2 instance. The role is the identity. The policy defines what that identity can do. The profile is just the wrapper EC2 requires.

Without it, the agent boots up, tries to authenticate with AWS, ce never appears in Fleet Manager. That's the most common reasonfor an instance not registering — not a network problem, not an agent problem, just a missing or wrong identity.

---
Why Fleet Manager showed the instance without any inbound access

Fleet Manager is just a view of instances that have successfullystance and are continuing to call ssm:UpdateInstanceInformation.Both of those are outbound calls from the agent.

Think of it like a check-in desk. The agent walks up and says "I'm here, here are my details." Fleet Manager records the check-in. At no point did Fleet
Manager reach into the instance to discover it — the instance an

---
The two planes, and why the data-plane actions are invisible to CloudTrail

This is the most important conceptual piece.

AWS has two distinct layers for SSM:

Management plane — the standard AWS API. This is where you call send-command, where the agent calls RegisterManagedInstance, where it calls
UpdateInstanceInformation. These go through the normal API endpot of these and records every call. That's why those three actionsshowed up in the CloudTrail query.

Data plane — the SSM messaging infrastructure. This is a separate system entirely: ssmmessages and ec2messages. The agent uses this to open a persistent
command channel, receive the actual payload of your command, exederr back. These calls are operational and constant — the agent is pinging these endpoints every few seconds just to stay connected. AWS deliberately does not route them through CloudTrail. They're too high-frequency to log
usefully, and they're not "management decisions" — they're plumb

The significance for your project is twofold.

For Feature 8's IAM policy: CloudTrail can only tell you about tIf you built the least-privilege policy purely from what youobserved in CloudTrail, you'd have a policy that lets the agent register and heartbeat, but the agent would silently fail to receive or execute any commands —
because the ssmmessages and ec2messages permissions would be mishe architecture that those exist, which is exactly whydocs/iam-scratch.md documents them separately with an explicit note.

For security auditing: CloudTrail tells you that a command was sent. It does not tell you what the command contained or what it produced. The actual payload
and output travel over the data plane and never touch CloudTrail audit — which production systems do — you'd enable SSM SessionManager logging to CloudWatch or S3. That's a legitimate "what I'd do differently at scale" answer if it comes up.

---
The short version

The instance profile gave the agent an identity. The agent used ound connection to AWS and announce itself. When you calledsend-command, you put a message into AWS's queue. The agent picked it up, ran the command locally, and sent the result back — all over the same outbound channel. No inbound connection was ever made. The data-plane calmmand and output are invisible to CloudTrail because they run on a separate messaging layer that AWS doesn't route through its audit system.help me understand this
