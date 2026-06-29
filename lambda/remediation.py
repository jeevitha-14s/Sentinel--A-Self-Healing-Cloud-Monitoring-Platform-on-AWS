import json
import logging
import os
import sys

import boto3

INSTANCE_ID = os.environ["INSTANCE_ID"]
ALERTS_TOPIC_ARN = os.environ["ALERTS_TOPIC_ARN"]

ssm = boto3.client("ssm")
sns = boto3.client("sns")

_handler = logging.StreamHandler(sys.stdout)
_handler.setFormatter(logging.Formatter("%(message)s"))
logging.getLogger().addHandler(_handler)
logging.getLogger().setLevel(logging.INFO)


def _log(level: str, event: str, **fields: object) -> None:
    logging.log(
        getattr(logging, level.upper()),
        json.dumps({"level": level, "event": event, **fields}),
    )


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
        _log("info", "remediation_attempted", command_id=command_id)
    except Exception as exc:
        sns.publish(
            TopicArn=ALERTS_TOPIC_ARN,
            Subject="Sentinel: Human needed",
            Message="Human needed — auto-remediation failed",
        )
        _log("error", "remediation_failed", error=str(exc))
        raise
