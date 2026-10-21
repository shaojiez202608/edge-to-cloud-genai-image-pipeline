"""Small helpers shared by the stack modules."""

from __future__ import annotations

from aws_cdk import RemovalPolicy
from aws_cdk import aws_logs as logs

RETENTION = {1: logs.RetentionDays.ONE_DAY, 3: logs.RetentionDays.THREE_DAYS, 5: logs.RetentionDays.FIVE_DAYS,
             7: logs.RetentionDays.ONE_WEEK, 14: logs.RetentionDays.TWO_WEEKS, 30: logs.RetentionDays.ONE_MONTH,
             60: logs.RetentionDays.TWO_MONTHS, 90: logs.RetentionDays.THREE_MONTHS,
             180: logs.RetentionDays.SIX_MONTHS, 365: logs.RetentionDays.ONE_YEAR}


def removal(deploy: dict) -> RemovalPolicy:
    return RemovalPolicy.DESTROY if deploy["removal_policy"] == "destroy" else RemovalPolicy.RETAIN


def log_group(scope, cid: str, name: str, deploy: dict) -> logs.LogGroup:
    return logs.LogGroup(scope, cid, log_group_name=name, retention=RETENTION[deploy["log_retention_days"]],
                         removal_policy=removal(deploy))


def foundation_model_id(model_id: str) -> str:
    """'us.amazon.nova-2-lite-v1:0' -> 'amazon.nova-2-lite-v1:0' (strip a geographic profile prefix)."""
    head, _, rest = model_id.partition(".")
    return rest if head in ("us", "eu", "apac", "global", "jp", "au", "ca") and rest else model_id
