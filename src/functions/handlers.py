"""Lambda entry points (arm64, one code bundle). Each function has its own IAM role; the handler
name selects the step. Clients are created lazily so that each function only touches the services
its role allows.

    handlers.upload_url        IoT rule (upload request)  -> presigned POST reply over MQTT
    handlers.dispatch          SQS ingest queue           -> Step Functions StartExecution
    handlers.validate_input    workflow step 1
    handlers.analyze           workflow step 2 (Bedrock + validation + optional guardrail)
    handlers.finalize          workflow step 3 (shelf image, result, manifest, notification)
    handlers.fallback          workflow catch target
    handlers.provisioning_hook fleet provisioning pre-provisioning hook
"""

from __future__ import annotations

import json
import os
import re
from functools import cache

import boto3
from botocore.config import Config

from edge_pipeline import pipeline
from edge_pipeline.config import PipelineConfig
from edge_pipeline.guardrail import BedrockGuardrail, NoGuardrail
from edge_pipeline.metrics import log, metric
from edge_pipeline.model import BedrockModel, MockModel
from edge_pipeline.store import DynamoItems, IotNotifier, S3Store

_CFG = Config(retries={"mode": "standard", "max_attempts": 3}, connect_timeout=5, read_timeout=60)


@cache
def _cfg() -> PipelineConfig:
    return PipelineConfig.from_env()


@cache
def _notifier() -> IotNotifier:
    addr = boto3.client("iot", config=_CFG).describe_endpoint(endpointType="iot:Data-ATS")["endpointAddress"]
    return IotNotifier(boto3.client("iot-data", endpoint_url=f"https://{addr}", config=_CFG))


class _Lazy:
    """Defers creating a client until first use (keeps unrelated permissions out of each role)."""

    def __init__(self, factory):
        self._factory, self._obj = factory, None

    def __getattr__(self, name):
        if self._obj is None:
            self._obj = self._factory()
        return getattr(self._obj, name)


@cache
def _deps() -> pipeline.Deps:
    cfg = _cfg()
    model = guard = None
    if cfg.model_mode == "bedrock":
        model = _Lazy(lambda: BedrockModel(cfg, boto3.client("bedrock-runtime", config=_CFG)))
    else:
        model = MockModel(cfg)
    gid = os.environ.get("GUARDRAIL_ID", "")
    guard = _Lazy(lambda: BedrockGuardrail(boto3.client("bedrock-runtime", config=_CFG), gid,
                                           os.environ.get("GUARDRAIL_VERSION", "DRAFT"))) if gid else NoGuardrail()
    sfn_arn = os.environ.get("STATE_MACHINE_ARN", "")

    def start(name: str, payload: dict) -> None:
        sfn = boto3.client("stepfunctions", config=_CFG)
        try:
            sfn.start_execution(stateMachineArn=sfn_arn, name=name, input=json.dumps(payload))
        except sfn.exceptions.ExecutionAlreadyExists:
            log("dispatch_duplicate", image_id=name)

    return pipeline.Deps(
        cfg=cfg, prefix=os.environ["PREFIX"],
        objects=_Lazy(lambda: S3Store(boto3.client("s3", config=_CFG), os.environ["BUCKET"])),
        items=_Lazy(lambda: DynamoItems(boto3.client("dynamodb", config=_CFG), os.environ["ITEMS_TABLE"])),
        notifier=_Lazy(_notifier), model=model, guardrail=guard, start_workflow=start,
    )


def upload_url(event, context):
    pipeline.issue_upload(_deps(), event if isinstance(event, dict) else {})
    return None


def dispatch(event, context):
    """SQS batch with partial failures: a message is retried only for errors that may pass
    (throttling, service errors); malformed messages are logged and dropped."""
    failures = []
    for rec in event.get("Records", []):
        try:
            body = json.loads(rec.get("body") or "{}")
        except ValueError:
            log("dispatch_dropped", codes=["malformed"])
            continue
        try:
            pipeline.dispatch(_deps(), body if isinstance(body, dict) else {})
        except Exception as exc:  # noqa: BLE001 - retried via the queue, then the DLQ
            log("dispatch_retry", error=type(exc).__name__)
            failures.append({"itemIdentifier": rec["messageId"]})
    metric("DispatchBatch", len(event.get("Records", [])))
    return {"batchItemFailures": failures}


def validate_input(event, context):
    return pipeline.validate_input(_deps(), event)


def analyze(event, context):
    return pipeline.analyze(_deps(), event)


def finalize(event, context):
    return pipeline.finalize(_deps(), event)


def fallback(event, context):
    return pipeline.finalize_fallback(_deps(), event)


def provisioning_hook(event, context):
    """Allow a claim-based registration only for the expected serial format and fleet size, and
    only when the claim client id matches the serial (one serial per claim connection)."""
    prefix = os.environ["PREFIX"]
    max_fleet = int(os.environ.get("MAX_FLEET_SIZE", "1000"))
    params = event.get("parameters") or {}
    serial = params.get("SerialNumber")
    ok = (isinstance(serial, str) and re.fullmatch(r"[0-9]{6}", serial) is not None
          and int(serial[2:]) < max_fleet and event.get("clientId") == f"{prefix}-claim-{serial}")
    log("provisioning_hook", status="allowed" if ok else "denied")
    metric("ProvisioningHook", 1, Result="allowed" if ok else "denied")
    return {"allowProvisioning": bool(ok)}
