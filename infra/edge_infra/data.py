"""Storage and queues: data bucket (+ access-log bucket), DynamoDB tables, SQS queues."""

from __future__ import annotations

from aws_cdk import Duration
from aws_cdk import aws_dynamodb as ddb
from aws_cdk import aws_s3 as s3
from aws_cdk import aws_sqs as sqs

from .common import removal


def build(stack) -> None:
    d, prefix = stack.deploy, stack.prefix
    rp = removal(d)
    destroy = d["removal_policy"] == "destroy"
    stack.access_logs = s3.Bucket(
        stack, "AccessLogs", block_public_access=s3.BlockPublicAccess.BLOCK_ALL, enforce_ssl=True,
        encryption=s3.BucketEncryption.S3_MANAGED, object_ownership=s3.ObjectOwnership.BUCKET_OWNER_PREFERRED,
        removal_policy=rp, auto_delete_objects=destroy,
        lifecycle_rules=[s3.LifecycleRule(expiration=Duration.days(max(d["data_retention_days"].values())))])
    ret = d["data_retention_days"]
    stack.bucket = s3.Bucket(
        stack, "Data", block_public_access=s3.BlockPublicAccess.BLOCK_ALL, enforce_ssl=True,
        encryption=s3.BucketEncryption.S3_MANAGED, object_ownership=s3.ObjectOwnership.BUCKET_OWNER_ENFORCED,
        server_access_logs_bucket=stack.access_logs, server_access_logs_prefix="data/",
        removal_policy=rp, auto_delete_objects=destroy, minimum_tls_version=1.2,
        lifecycle_rules=[s3.LifecycleRule(id=f"expire-{p}", prefix=f"{p}/", expiration=Duration.days(ret[p]))
                         for p in ret]
        + [s3.LifecycleRule(id="abort-mpu", abort_incomplete_multipart_upload_after=Duration.days(1))])

    pitr = ddb.PointInTimeRecoverySpecification(point_in_time_recovery_enabled=True)
    stack.items = ddb.TableV2(
        stack, "Items", table_name=f"{prefix}-items",
        partition_key=ddb.Attribute(name="thing_name", type=ddb.AttributeType.STRING),
        sort_key=ddb.Attribute(name="sk", type=ddb.AttributeType.STRING),
        time_to_live_attribute="ttl", point_in_time_recovery_specification=pitr, removal_policy=rp)
    stack.presence = ddb.TableV2(
        stack, "Presence", table_name=f"{prefix}-presence",
        partition_key=ddb.Attribute(name="thing_name", type=ddb.AttributeType.STRING),
        point_in_time_recovery_specification=pitr, removal_policy=rp)

    def queue(cid: str, name: str, **kw) -> sqs.Queue:
        return sqs.Queue(stack, cid, queue_name=f"{prefix}-{name}", enforce_ssl=True,
                         encryption=sqs.QueueEncryption.SQS_MANAGED, removal_policy=rp, **kw)

    stack.ingest_dlq = queue("IngestDlq", "ingest-dlq", retention_period=Duration.days(14))
    stack.ingest = queue("Ingest", "ingest", visibility_timeout=Duration.seconds(60),
                         retention_period=Duration.days(4),
                         dead_letter_queue=sqs.DeadLetterQueue(queue=stack.ingest_dlq, max_receive_count=5))
    stack.telemetry = queue("Telemetry", "telemetry",
                            retention_period=Duration.seconds(d["telemetry_retention_s"]))
    stack.rule_errors = queue("RuleErrors", "rule-errors", retention_period=Duration.days(14))
