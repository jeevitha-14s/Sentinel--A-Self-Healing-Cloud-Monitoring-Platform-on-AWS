import json
import os

import boto3

INSTANCE_ID = os.environ["INSTANCE_ID"]
ALERTS_TOPIC_ARN = os.environ["ALERTS_TOPIC_ARN"]

ssm = boto3.client("ssm")
sns = boto3.client("sns")


def handler(event: dict, context: object) -> None:
    try:
        resp = ssm.send_command(
            InstanceIds=[INSTANCE_ID],
            DocumentName="AWS-RunShellScript",
            Parameters={"commands": ["docker restart sentinel-app"]},
        )
        command_id = resp["Command"]["CommandId"]
        sns.publish(
            TopicArn=ALERTS_TOPIC_ARN,
            Subject="Sentinel: Auto-restart attempted",
            Message="Auto-restart attempted — check dashboard",
        )
        print(json.dumps({"event": "invoked", "outcome": "attempted", "command_id": command_id}))
    except Exception as exc:
        sns.publish(
            TopicArn=ALERTS_TOPIC_ARN,
            Subject="Sentinel: Human needed",
            Message="Human needed — auto-remediation failed",
        )
        print(json.dumps({"event": "invoked", "outcome": "human_needed", "error": str(exc)}))
        raise
