import asyncio
import hashlib
import json

import pytest

from edge_pipeline import metrics, naming, pipeline, synthetic
from edge_pipeline.config import PipelineConfig
from edge_pipeline.guardrail import GuardrailUnavailable, GuardrailVerdict
from edge_pipeline.local.runtime import LocalEnvironment
from edge_pipeline.model import MockModel
from edge_pipeline.store import Conflict

CFG = PipelineConfig(model_mode="mock", guardrail_enabled=False, uploads_per_device_per_hour=3)


def run(coro):
    return asyncio.run(coro)


async def submit(env, dev, data=None, ctype="image/jpeg", **kw):
    pump = asyncio.create_task(env.pump_forever())
    try:
        return await dev.capture_and_upload(data, content_type=ctype, wait_result_s=5, **kw)
    finally:
        pump.cancel()


def test_happy_path_writes_result_shelf_and_manifest():
    env = LocalEnvironment(CFG)
    dev = env.device("000001")

    async def go():
        await dev.connect()
        return await submit(env, dev)

    out = run(go())
    assert out["stage"] == "processed" and out["status"] == "listed"
    res = env.result(dev.thing, out["image_id"])
    assert res["copy_source"] == "model" and res["shelf_image_key"].startswith(f"shelf/images/{dev.thing}/")
    assert env.objects.get(res["shelf_image_key"]).startswith(b"\xff\xd8")
    assert env.shelf_manifest()["items"][0]["image_id"] == out["image_id"]
    assert env.items.get(dev.thing, out["image_id"])["status"] == "listed"


def test_upload_refusals_rate_limit_and_type_spoof():
    env = LocalEnvironment(CFG)
    dev = env.device("000002")
    jpeg = synthetic.product_photo(1, 1)

    async def go():
        await dev.connect()
        r = [await submit(env, dev, declared_size=10**9)]
        r.append(await submit(env, dev, jpeg, ctype="image/png"))  # declared PNG, bytes are JPEG
        r += [await submit(env, dev) for _ in range(3)]
        return r

    r = run(go())
    assert r[0] == {"stage": "request", "code": "size_out_of_range"}
    assert r[1]["status"] == "fallback" and r[1]["reason_codes"] == ["magic_mismatch"]
    assert r[-1] == {"stage": "request", "code": "rate_limited"}  # 4th request within the hour


def test_dispatch_drops_foreign_replayed_and_malformed_ids():
    env = LocalEnvironment(CFG)
    deps = env.deps
    a = "proto-edge-sim-000001"
    issued = pipeline.issue_upload(deps, {"thing_name": a, "request_id": "r1", "content_type": "image/jpeg",
                                          "size": 100, "sha256": "a" * 64})
    iid = issued["image_id"]
    env.workflow.start = lambda name, payload: None
    deps.start_workflow = env.workflow.start
    assert pipeline.dispatch(deps, {"thing_name": "proto-edge-sim-000002", "image_id": iid}) == "dropped"
    assert pipeline.dispatch(deps, {"thing_name": a, "image_id": iid}) == "started"
    assert pipeline.dispatch(deps, {"thing_name": a, "image_id": iid}) == "dropped"  # replay
    assert pipeline.dispatch(deps, {"thing_name": a, "image_id": "../../x"}) == "dropped"
    assert pipeline.dispatch(deps, {"thing_name": "other-sim-1", "image_id": iid}) == "dropped"


def test_issue_upload_never_raises_on_garbage():
    env = LocalEnvironment(CFG)
    assert pipeline.issue_upload(env.deps, {"thing_name": 5}) == {}
    r = pipeline.issue_upload(env.deps, {"thing_name": "proto-edge-sim-000001", "request_id": "x" * 99})
    assert r["error"] == "request_id_invalid"


def _workflow_env(model=None, guardrail=None, cfg=CFG):
    env = LocalEnvironment(cfg, model=model)
    if guardrail is not None:
        env.deps.guardrail = guardrail
    return env


def _process(env):
    dev = env.device("000003")

    async def go():
        await dev.connect()
        return await submit(env, dev)

    out = run(go())
    return out, env.result(dev.thing, out["image_id"])


def test_transient_model_errors_retry_then_fallback():
    model = MockModel(CFG, fail="transient")
    out, res = _process(_workflow_env(model))
    assert res["status"] == "fallback" and res["reason_codes"] == ["model_unavailable"]
    assert model.calls == 4  # first try + 3 retries, as in the workflow definition


def test_permanent_model_error_fallback_not_500():
    out, res = _process(_workflow_env(MockModel(CFG, fail="permanent")))
    assert res["status"] == "fallback" and res["reason_codes"] == ["model_error"]


class Guard:
    def __init__(self, image=False, text=False, fail=False):
        self.image, self.text, self.fail = image, text, fail

    def check_image(self, jpeg):
        if self.fail:
            raise GuardrailUnavailable("x")
        return GuardrailVerdict(self.image, ["content:PROMPT_ATTACK"] if self.image else [])

    def check_text(self, texts):
        return GuardrailVerdict(self.text, ["content:INSULTS"] if self.text else [])


@pytest.mark.parametrize("guard,code", [(Guard(image=True), "guardrail_input"), (Guard(text=True), "guardrail_output"),
                                        (Guard(fail=True), "model_unavailable")])
def test_guardrail_paths(guard, code):
    cfg = PipelineConfig(model_mode="mock", guardrail_enabled=True)
    out, res = _process(_workflow_env(MockModel(cfg), guard, cfg))
    assert res["status"] == "fallback" and res["reason_codes"] == [code]


def test_fallback_code_mapping():
    env = LocalEnvironment(CFG)
    t, i = "proto-edge-sim-000001", "b" * 32
    for err, code in [({"Error": "InputRejected", "Cause": json.dumps({"errorMessage": "magic_mismatch"})}, "magic_mismatch"),
                      ({"Error": "InputRejected", "Cause": json.dumps({"errorMessage": "evil text"})}, "input_rejected"),
                      ({"Error": "InputRejected", "Cause": "not json"}, "input_rejected"),
                      ({"Error": "States.Timeout"}, "timeout_or_throttled"),
                      ({"Error": "Whatever"}, "internal_error")]:
        pipeline.finalize_fallback(env.deps, {"thing_name": t, "image_id": i, "error": err})
        assert env.result(t, i)["reason_codes"] == [code]


def test_steps_reject_bad_identity_and_keys():
    env = LocalEnvironment(CFG)
    with pytest.raises(pipeline.BadIdentity):
        pipeline.validate_input(env.deps, {"thing_name": "x", "image_id": "a" * 32})
    with pytest.raises(pipeline.BadIdentity):
        pipeline.analyze(env.deps, {"thing_name": "proto-edge-sim-000001", "image_id": "a" * 32,
                                    "work_key": "work/proto-edge-sim-000002/" + "a" * 32 + ".jpg"})


def test_manifest_conditional_write_conflict_is_retried():
    from edge_pipeline import shelf
    from edge_pipeline.local.fakes import LocalObjectStore

    store = LocalObjectStore()
    real = store.put_if
    calls = {"n": 0}

    def flaky(key, data, ctype, etag):
        calls["n"] += 1
        if calls["n"] == 1:
            raise Conflict(key)
        return real(key, data, ctype, etag)

    store.put_if = flaky
    assert shelf.update_manifest(store, {"thing_name": "t", "image_id": "i", "updated_ms": 1}, 10) == 2
    with pytest.raises(Conflict):
        store.put_if(naming.SHELF_MANIFEST_KEY, b"{}", "application/json", None)  # exists: If-None-Match fails


def test_logs_are_content_free():
    metrics._SINK.clear()
    model = MockModel(CFG, mode="naive")
    env = _workflow_env(model)
    case = next(c for c in synthetic.CASES if c.case_id == "i01-url-promo")
    dev = env.device("000004")
    data, _ = synthetic.render(case)

    async def go():
        await dev.connect()
        return await submit(env, dev, data, ctype="image/png")

    run(go())
    blob = json.dumps(metrics._SINK)
    assert "example.com" not in blob.lower() and "free gift" not in blob.lower()
    assert hashlib.sha256(data).hexdigest() not in blob
    assert all(set(r) <= metrics.ALLOWED_FIELDS | {"ts"} for r in metrics._SINK)
