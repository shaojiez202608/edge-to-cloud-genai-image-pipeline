"""Pipeline steps. The Lambda handlers (src/functions/handlers.py) and the local runner call the same
functions with different adapters.

    upload request  -> issue_upload      (rule -> Lambda)        presigned POST for one exact key
    image uploaded  -> dispatch          (rule -> SQS -> Lambda) claim the issued id, start workflow
    workflow        -> validate_input -> analyze -> finalize     (Step Functions Standard)
                       any failure after retries -> finalize_fallback

Identity: ``thing_name`` always comes from the rule output (topic segment that the rule requires to
equal the authenticated client id) and is re-checked against the thing-name pattern in every step.
Object keys are computed here from thing name and image id; nothing from the model or the device
payload becomes a key.
"""

from __future__ import annotations

import json
import re
import time
import uuid
from dataclasses import dataclass, field
from typing import Any

from . import image_checks, naming, shelf
from .config import PipelineConfig
from .guardrail import Guardrail, GuardrailUnavailable, NoGuardrail
from .metrics import log, metric
from .model import Model, ModelPermanentError, ModelTransientError
from .store import ItemTable, Notifier, ObjectStore
from .validation import neutral_copy, validate_output

REQUEST_ID_RE = re.compile(r"^[A-Za-z0-9-]{1,40}$")
RESULT_SCHEMA_VERSION = 1
KNOWN_INPUT_CODES = {
    "content_type_not_allowed", "size_out_of_range", "size_mismatch", "sha256_mismatch", "sha256_malformed",
    "magic_mismatch", "format_mismatch", "dimensions_out_of_range", "too_many_pixels", "animated_not_allowed",
    "decompression_bomb", "decode_failed", "upload_missing",
}


class StepError(Exception):
    """Raised by a step for a non-retryable problem; the workflow catches it and runs the fallback."""


class BadIdentity(StepError):
    pass


@dataclass
class Deps:
    cfg: PipelineConfig
    prefix: str
    objects: ObjectStore
    items: ItemTable
    notifier: Notifier
    model: Model | None = None
    guardrail: Guardrail = field(default_factory=NoGuardrail)
    start_workflow: Any = None  # callable(name: str, payload: dict) -> None
    # Output validator; replaced only by the negative-control evaluation (which must then fail).
    validator: Any = field(default=validate_output)
    clock_ms: Any = field(default=lambda: int(time.time() * 1000))


def _check_identity(deps: Deps, thing: Any, image_id: Any = None) -> None:
    if not isinstance(thing, str) or not naming.thing_name_re(deps.prefix).match(thing):
        raise BadIdentity("thing_name")
    if image_id is not None and (not isinstance(image_id, str) or not naming.IMAGE_ID_RE.match(image_id)):
        raise BadIdentity("image_id")


# --------------------------------------------------------------------------- upload URL


def issue_upload(deps: Deps, msg: dict) -> dict:
    """Rule output -> reply published on edge/<thing>/upload/url. Never raises for bad input."""
    thing = msg.get("thing_name")
    try:
        _check_identity(deps, thing)
    except BadIdentity:
        log("upload_refused", codes=["bad_identity"])
        return {}
    request_id = msg.get("request_id")
    if not isinstance(request_id, str) or not REQUEST_ID_RE.match(request_id):
        request_id = "invalid"
    reply_topic = naming.device_topic(thing, naming.UPLOAD_URL)

    def refuse(code: str) -> dict:
        reply = {"request_id": request_id, "error": code}
        deps.notifier.publish(reply_topic, reply)
        log("upload_refused", thing_name=thing, request_id=request_id, codes=[code])
        metric("UploadRefused", 1, Reason=code)
        return reply

    if request_id == "invalid":
        return refuse("request_id_invalid")
    try:
        image_checks.check_declared(deps.cfg, msg.get("content_type"), msg.get("size"), msg.get("sha256"))
    except image_checks.InputRejected as exc:
        return refuse(exc.code)
    now = deps.clock_ms()
    hour = time.strftime("%Y%m%d%H", time.gmtime(now / 1000))
    if not deps.items.count_upload(thing, hour, deps.cfg.uploads_per_device_per_hour, ttl_s=7200):
        return refuse("rate_limited")
    image_id = uuid.uuid4().hex
    key = naming.upload_key(thing, image_id, msg["content_type"])
    post = deps.objects.presigned_post(key, msg["content_type"], msg["size"], deps.cfg.presign_expiry_s)
    deps.items.put_issued(thing, image_id, {"content_type": msg["content_type"], "size": msg["size"],
                                            "sha256": msg["sha256"], "key": key},
                          now + deps.cfg.presign_expiry_s * 1000)
    reply = {"request_id": request_id, "image_id": image_id, "url": post["url"], "fields": post["fields"],
             "expires_s": deps.cfg.presign_expiry_s}
    deps.notifier.publish(reply_topic, reply)
    log("upload_issued", thing_name=thing, request_id=request_id, image_id=image_id)
    metric("UploadUrlIssued", 1)
    return reply


# --------------------------------------------------------------------------- dispatcher


def dispatch(deps: Deps, msg: dict) -> str:
    """Rule output (from the ingest queue) -> start one workflow execution. Returns a status string.
    Unknown, foreign or replayed image ids are dropped with a log line (not retried)."""
    thing, image_id = msg.get("thing_name"), msg.get("image_id")
    try:
        _check_identity(deps, thing, image_id)
    except BadIdentity as exc:
        log("dispatch_dropped", codes=[f"bad_{exc}"])
        metric("DispatchDropped", 1, Reason="bad_identity")
        return "dropped"
    declared = deps.items.claim(thing, image_id, deps.clock_ms())
    if declared is None:
        # Not issued to this device, already processed, or a replay: nothing to do.
        log("dispatch_dropped", thing_name=thing, image_id=image_id, codes=["not_claimable"])
        metric("DispatchDropped", 1, Reason="not_claimable")
        return "dropped"
    payload = {"thing_name": thing, "image_id": image_id, "declared": declared,
               "received_ms": msg.get("received_ms") if isinstance(msg.get("received_ms"), int) else None}
    deps.start_workflow(image_id, payload)
    log("dispatch_started", thing_name=thing, image_id=image_id)
    return "started"


# --------------------------------------------------------------------------- workflow steps


def validate_input(deps: Deps, event: dict) -> dict:
    thing, image_id = event.get("thing_name"), event.get("image_id")
    _check_identity(deps, thing, image_id)
    d = event.get("declared") or {}
    key = naming.upload_key(thing, image_id, d.get("content_type", "image/jpeg")) \
        if d.get("content_type") in naming.EXTENSIONS else None
    if key is None or d.get("key") != key:
        raise image_checks.InputRejected("content_type_not_allowed")
    try:
        data = deps.objects.get(key)
    except Exception:  # noqa: BLE001 - missing object or access problem: fixed code, no detail
        raise image_checks.InputRejected("upload_missing") from None
    side = max(deps.cfg.model_image_side, deps.cfg.shelf_image_side)
    clean = image_checks.sanitize(deps.cfg, data, d["content_type"], d.get("size"), d.get("sha256"), side)
    wkey = naming.work_key(thing, image_id)
    deps.objects.put(wkey, clean.jpeg, "image/jpeg")
    log("input_valid", thing_name=thing, image_id=image_id, step="validate_input")
    return {**event, "work_key": wkey, "width": clean.width, "height": clean.height}


def analyze(deps: Deps, event: dict) -> dict:
    """Model call + validation. Model output problems never raise: they become decision 'rejected'.
    Transient model or guardrail errors raise (the workflow retries them, then falls back)."""
    thing, image_id = event.get("thing_name"), event.get("image_id")
    _check_identity(deps, thing, image_id)
    if event.get("work_key") != naming.work_key(thing, image_id):
        raise BadIdentity("work_key")
    jpeg = deps.objects.get(event["work_key"])
    started = time.monotonic()
    out = {**event, "guardrail": []}
    if deps.cfg.guardrail_enabled:
        try:
            g_in = deps.guardrail.check_image(jpeg)
        except GuardrailUnavailable as exc:
            raise ModelTransientError(f"guardrail:{exc}") from None
        if g_in.intervened:
            log("analysis", thing_name=thing, image_id=image_id, decision="rejected",
                codes=["guardrail_input"], categories=g_in.categories)
            metric("Decision", 1, Decision="rejected")
            return {**out, "decision": "rejected", "codes": ["guardrail_input"], "guardrail": g_in.categories}
    model_jpeg = jpeg
    if max(event.get("width", 0), event.get("height", 0)) > deps.cfg.model_image_side:
        model_jpeg = image_checks.sanitize(deps.cfg, jpeg, "image/jpeg", target_side=deps.cfg.model_image_side).jpeg
    try:
        answer = deps.model.analyze(model_jpeg)
    except ModelPermanentError as exc:
        log("analysis", thing_name=thing, image_id=image_id, decision="rejected", codes=["model_error"],
            error=exc.code)
        metric("Decision", 1, Decision="rejected")
        return {**out, "decision": "rejected", "codes": ["model_error"]}
    result = deps.validator(deps.cfg, answer.tool_input)
    codes = result.codes
    if result.asset is not None and deps.cfg.guardrail_enabled:
        try:
            g_out = deps.guardrail.check_text([result.asset["title"], *result.asset["bullets"]])
        except GuardrailUnavailable as exc:
            raise ModelTransientError(f"guardrail:{exc}") from None
        if g_out.intervened:
            log("analysis", thing_name=thing, image_id=image_id, decision="rejected",
                codes=["guardrail_output"], categories=g_out.categories)
            metric("Decision", 1, Decision="rejected")
            return {**out, "decision": "rejected", "codes": ["guardrail_output"], "guardrail": g_out.categories}
    elapsed = int((time.monotonic() - started) * 1000)
    log("analysis", thing_name=thing, image_id=image_id, decision=result.decision, codes=codes,
        duration_ms=elapsed, input_tokens=answer.input_tokens, output_tokens=answer.output_tokens)
    metric("Decision", 1, Decision=result.decision)
    metric("ModelLatency", elapsed, "Milliseconds")
    if result.decision == "rejected":
        metric("ValidationFailure", 1)
        # Paths and keywords only (no values): safe to keep in the result for troubleshooting.
        return {**out, "decision": "rejected", "codes": codes, "issues": result.issues[:10]}
    return {**out, "decision": result.decision, "codes": codes, "asset": result.asset}


def finalize(deps: Deps, event: dict) -> dict:
    thing, image_id = event.get("thing_name"), event.get("image_id")
    _check_identity(deps, thing, image_id)
    decision = event.get("decision")
    asset = event.get("asset") if decision in ("accepted", "review") else None
    now = deps.clock_ms()
    result: dict[str, Any] = {
        "schema_version": RESULT_SCHEMA_VERSION, "thing_name": thing, "image_id": image_id,
        "decision": decision if decision in ("accepted", "review", "rejected") else "rejected",
        "reason_codes": [c for c in event.get("codes", []) if isinstance(c, str)][:20],
        "model_id": deps.cfg.model_id if deps.cfg.model_mode == "bedrock" else "mock",
        "processed_ms": now,
    }
    if event.get("issues"):
        result["validation_issues"] = event["issues"][:10]
    if asset is None:
        return _write_fallback(deps, thing, image_id, result, event)
    jpeg = deps.objects.get(naming.work_key(thing, image_id))
    image = shelf.render_shelf_image(jpeg, asset["subject_box"], deps.cfg.shelf_image_side)
    ikey = naming.shelf_image_key(thing, image_id)
    deps.objects.put(ikey, image, "image/jpeg")
    if decision == "accepted":
        status, copy = "listed", {"title": asset["title"], "bullets": asset["bullets"], "tags": asset["tags"]}
    else:
        status, copy = "needs_review", {**neutral_copy(deps.cfg, asset["category"]), "tags": []}
    result.update(status=status, category=asset["category"], shelf_image_key=ikey, copy=copy,
                  copy_source="model" if status == "listed" else "neutral_template")
    _store_result(deps, thing, image_id, result, event, now)
    return {"thing_name": thing, "image_id": image_id, "status": status}


def finalize_fallback(deps: Deps, event: dict) -> dict:
    """Catch target: the error from Step Functions ({"Error", "Cause"}) becomes a fixed reason code."""
    thing, image_id = event.get("thing_name"), event.get("image_id")
    _check_identity(deps, thing, image_id)
    err = event.get("error") or {}
    name = err.get("Error", "")
    code = "internal_error"
    if name == "InputRejected":
        try:
            msg = json.loads(err.get("Cause", "{}")).get("errorMessage", "")
        except (ValueError, AttributeError):
            msg = ""
        code = msg if msg in KNOWN_INPUT_CODES else "input_rejected"
    elif name == "ModelTransientError":
        code = "model_unavailable"
    elif name in ("States.Timeout", "Lambda.TooManyRequestsException"):
        code = "timeout_or_throttled"
    elif name == "BadIdentity":
        code = "bad_identity"
    result = {"schema_version": RESULT_SCHEMA_VERSION, "thing_name": thing, "image_id": image_id,
              "decision": "rejected", "reason_codes": [code], "processed_ms": deps.clock_ms(),
              "model_id": deps.cfg.model_id if deps.cfg.model_mode == "bedrock" else "mock"}
    return _write_fallback(deps, thing, image_id, result, event)


def _write_fallback(deps: Deps, thing: str, image_id: str, result: dict, event: dict) -> dict:
    result.update(status="fallback", copy=None, shelf_image_key=None)
    _store_result(deps, thing, image_id, result, event, result["processed_ms"])
    return {"thing_name": thing, "image_id": image_id, "status": "fallback"}


def _store_result(deps: Deps, thing: str, image_id: str, result: dict, event: dict, now: int) -> None:
    deps.objects.put(naming.result_key(thing, image_id),
                     json.dumps(result, ensure_ascii=False, indent=1).encode(), "application/json")
    if result["status"] in ("listed", "needs_review"):
        entry = {"thing_name": thing, "image_id": image_id, "status": result["status"],
                 "category": result["category"], "title": result["copy"]["title"],
                 "image_key": result["shelf_image_key"], "item_key": naming.shelf_item_key(thing, image_id),
                 "updated_ms": now}
        deps.objects.put(naming.shelf_item_key(thing, image_id),
                         json.dumps({**entry, "copy": result["copy"]}, ensure_ascii=False, indent=1).encode(),
                         "application/json")
        shelf.update_manifest(deps.objects, entry, deps.cfg.shelf_manifest_max_items)
    deps.items.set_status(thing, image_id, result["status"], {"reason_codes": result["reason_codes"]})
    deps.notifier.publish(naming.device_topic(thing, naming.RESULTS),
                          {"image_id": image_id, "status": result["status"], "reason_codes": result["reason_codes"]})
    latency = now - event["received_ms"] if isinstance(event.get("received_ms"), int) else None
    log("finalized", thing_name=thing, image_id=image_id, status=result["status"], codes=result["reason_codes"],
        latency_ms=latency)
    metric("Processed", 1, Status=result["status"])
    if latency is not None and latency >= 0:
        metric("EndToEndLatency", latency, "Milliseconds")
