# IAM Scratch — AccessDenied Log

Each row = one real AccessDenied hit during the spike (and later features).
This list becomes the hand-written least-privilege policies in Feature 8.

| Feature | Error / Action denied | Permission added | Scoped to |
|---|---|---|---|
| Spike | (none — used AmazonSSMManagedInstanceCore to observe; see notes below) | — | — |
| Flask app (Feature 2) | **(anticipated)** cloudwatch:PutMetricData denied (heartbeat thread) | cloudwatch:PutMetricData | `Sentinel/*` namespace only — confirm in Feature 10 when heartbeat is enabled in production |

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
