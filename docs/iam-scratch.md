# IAM Scratch — AccessDenied Log

Each row = one real AccessDenied hit during the spike (and later features).
This list becomes the hand-written least-privilege policies in Feature 8.

| Feature | Error / Action denied | Permission added | Scoped to |
|---|---|---|---|
| Spike | (none — used AmazonSSMManagedInstanceCore to observe; see notes below) | — | — |
| Flask app (Feature 2) | **(anticipated)** cloudwatch:PutMetricData denied (heartbeat thread) | cloudwatch:PutMetricData | `Sentinel/*` namespace only — confirm in Feature 10 when heartbeat is enabled in production |
| CI/CD (Feature 4) | `logs:CreateLogStream` denied — awslogs driver calls CreateLogStream at container start even with `awslogs-create-group=false`; container startup aborts | `logs:CreateLogStream`, `logs:PutLogEvents` | `arn:aws:logs:ap-south-1:262439760394:log-group:/sentinel/app:*` — add in Feature 5 when log group is created |
| CloudWatch logs (Feature 5) | `logs:CreateLogStream` AccessDenied — policy scoped `CreateLogStream` to the log group ARN; IAM evaluates it against the **log stream ARN** (`log-group:/sentinel/app:log-stream:sentinel-app`). Container refused to start. | Moved `CreateLogStream` (and `PutLogEvents`) to `:log-stream:*` resource; kept `DescribeLogStreams` on log group ARN | `aws_cloudwatch_log_group.sentinel.arn` for DescribeLogStreams; `${arn}:log-stream:*` for CreateLogStream + PutLogEvents |

---

## Spike: SSM agent actions observed via CloudTrail

Instance `i-01b6c3c82889b8432` called these management-plane SSM actions while
registering and staying connected. These showed up in CloudTrail under the
`sentinel-spike-role` principal (session name = instance ID):

| Action | When called |
|---|---|
| `ssm:RegisterManagedInstance` | Once at first boot (agent self-registers) |
| `ssm:UpdateInstanceInformation` | Repeatedly (heartbeat to SSM control plane) |
| `ssm:ListInstanceAssociations` | Repeatedly (checks for pending State Manager jobs) |

**Data-plane actions (required but NOT in CloudTrail):**
These are called over the SSM messaging channel, not the management API, so they
do not appear in CloudTrail. They are still required for `send-command` to work:

| Action | Purpose |
|---|---|
| `ssmmessages:CreateControlChannel` | Agent opens the persistent command channel |
| `ssmmessages:OpenControlChannel` | Agent keeps the command channel alive |
| `ssmmessages:CreateDataChannel` | Agent opens a channel per command execution |
| `ssmmessages:OpenDataChannel` | Agent streams command stdout/stderr back |
| `ec2messages:GetMessages` | Agent polls for queued commands |
| `ec2messages:AcknowledgeMessage` | Agent ACKs a received command |
| `ec2messages:SendReply` | Agent sends command result back |
| `ec2messages:DeleteMessage` | Agent cleans up after execution |

**Feature 8 note:** The hand-written least-privilege policy must include all of the
above. The management-plane actions can be scoped to `*` (no resource-level support);
the ssmmessages/ec2messages actions also have no resource-level scoping available.

---

## Feature 4: Infrastructure lessons learned

### AL2023 AMI does not pre-install amazon-ssm-agent
`ami-0f2f85bcae7ec46bd` (ap-south-1, AL2023) does not include the SSM agent.
`systemctl enable --now amazon-ssm-agent` fails with "Unit file does not exist."
**Fix:** `dnf install -y amazon-ssm-agent` in user data before enabling the service.
Instance never appears in Fleet Manager without this.

### Default root volume on this AMI is 2 GB — too small for Docker
The AMI snapshot default is 2 GB. `docker pull` fills the disk during the first
image download (~130 MB compressed expands to ~400 MB on disk; two pulls exceeded 2 GB).
**Fix:** Always declare `root_block_device { volume_size = 20 }` in `aws_instance`.
If you forget, resize live with `growpart /dev/xvda 1 && xfs_growfs /` via SSM —
no instance replacement required for EBS volume expansion.

### awslogs log driver does NOT fail silently when the log group is missing
The spec assumed `--log-driver awslogs` with `awslogs-create-group=false` would
start the container and silently drop logs if `/sentinel/app` didn't exist.
**Actual behaviour:** the driver calls `logs:CreateLogStream` synchronously during
container startup. If the call fails (missing log group → ResourceNotFoundException,
or missing IAM → AccessDeniedException), Docker aborts the container before it starts.
**Fix for Feature 4:** use `--log-driver json-file` (default) until Feature 5 creates
the log group and adds the IAM permissions.
**Fix for Feature 5:** create `/sentinel/app` log group in Terraform, add
`logs:CreateLogStream` and `logs:PutLogEvents` to the instance role scoped to that
log group ARN, then switch the deploy command back to `--log-driver awslogs`.
