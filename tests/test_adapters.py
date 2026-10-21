"""AWS adapters against the real service models (botocore Stubber), so parameter names and shapes
are checked by botocore rather than by a hand-written fake."""

import json

import boto3
import pytest
from botocore.stub import ANY, Stubber

from edge_pipeline.config import PipelineConfig
from edge_pipeline.guardrail import BedrockGuardrail, GuardrailUnavailable
from edge_pipeline.model import BedrockModel, ModelPermanentError, ModelTransientError
from edge_pipeline.prompts import TOOL_NAME
from edge_pipeline.store import Conflict, DynamoItems, S3Store

CFG = PipelineConfig()
KW = {"region_name": "us-east-1", "aws_access_key_id": "testing", "aws_secret_access_key": "testing"}  # noqa: S106


def client(name):
    return boto3.client(name, **KW)


def converse_response(content):
    return {"output": {"message": {"role": "assistant", "content": content}}, "stopReason": "tool_use",
            "usage": {"inputTokens": 10, "outputTokens": 5, "totalTokens": 15}, "metrics": {"latencyMs": 1}}


def test_bedrock_model_parses_forced_tool_call():
    c = client("bedrock-runtime")
    with Stubber(c) as s:
        s.add_response("converse", converse_response([{"toolUse": {"toolUseId": "t1", "name": TOOL_NAME,
                                                                  "input": {"title": "x"}}}]),
                       {"modelId": CFG.model_id, "system": ANY, "messages": ANY, "toolConfig": ANY,
                        "inferenceConfig": {"maxTokens": CFG.max_tokens, "temperature": 0.0}})
        ans = BedrockModel(CFG, c).analyze(b"\xff\xd8\xff")
    assert ans.tool_input == {"title": "x"} and ans.input_tokens == 10


def test_bedrock_model_tool_choice_and_text_answer():
    c = client("bedrock-runtime")
    captured = {}
    with Stubber(c) as s:
        s.add_response("converse", converse_response([{"text": "plain text, no tool"}]), None)
        c.meta.events.register("provide-client-params.bedrock-runtime.Converse",
                               lambda params, **k: captured.update(params))
        ans = BedrockModel(CFG, c).analyze(b"\xff\xd8\xff")
    assert ans.tool_input is None
    assert captured["toolConfig"]["toolChoice"] == {"tool": {"name": TOOL_NAME}}
    img = captured["messages"][0]["content"][0]["image"]
    assert img["format"] == "jpeg"


@pytest.mark.parametrize("code,exc", [("ThrottlingException", ModelTransientError),
                                      ("ValidationException", ModelPermanentError),
                                      ("AccessDeniedException", ModelPermanentError)])
def test_bedrock_errors_classified(code, exc):
    c = client("bedrock-runtime")
    with Stubber(c) as s:
        s.add_client_error("converse", service_error_code=code, http_status_code=400)
        with pytest.raises(exc):
            BedrockModel(CFG, c).analyze(b"\xff\xd8\xff")


def test_guardrail_reports_categories_only():
    c = client("bedrock-runtime")
    resp = {"usage": {"topicPolicyUnits": 0, "contentPolicyUnits": 1, "wordPolicyUnits": 0,
                      "sensitiveInformationPolicyUnits": 0, "sensitiveInformationPolicyFreeUnits": 0,
                      "contextualGroundingPolicyUnits": 0},
            "action": "GUARDRAIL_INTERVENED", "outputs": [{"text": "Blocked."}],
            "assessments": [{"contentPolicy": {"filters": [{"type": "PROMPT_ATTACK", "confidence": "HIGH",
                                                            "action": "BLOCKED"}]}}]}
    with Stubber(c) as s:
        s.add_response("apply_guardrail", resp, {"guardrailIdentifier": "g", "guardrailVersion": "1",
                                                 "source": "INPUT", "content": ANY})
        v = BedrockGuardrail(c, "g", "1").check_image(b"\xff\xd8\xff")
        s.add_client_error("apply_guardrail", service_error_code="ThrottlingException")
        with pytest.raises(GuardrailUnavailable):
            BedrockGuardrail(c, "g", "1").check_text(["x"])
    assert v.intervened and v.categories == ["content:PROMPT_ATTACK"]


def test_s3_conditional_write_and_conflict():
    c = client("s3")
    with Stubber(c) as s:
        s.add_response("put_object", {"ETag": '"x"'}, {"Bucket": "b", "Key": "k", "Body": b"1",
                                                       "ContentType": "application/json", "IfNoneMatch": "*"})
        s.add_client_error("put_object", service_error_code="PreconditionFailed", http_status_code=412)
        st = S3Store(c, "b")
        st.put_if("k", b"1", "application/json", None)
        with pytest.raises(Conflict):
            st.put_if("k", b"1", "application/json", '"old"')


def test_s3_presigned_post_pins_key_type_and_size():
    st = S3Store(client("s3"), "b")
    post = st.presigned_post("uploads/t/i.jpg", "image/jpeg", 1234, 60)
    import base64
    pol = json.loads(base64.b64decode(post["fields"]["policy"]))
    conds = pol["conditions"]
    assert ["content-length-range", 1234, 1234] in conds
    assert {"Content-Type": "image/jpeg"} in conds and {"key": "uploads/t/i.jpg"} in conds


def test_dynamo_claim_is_conditional():
    c = client("dynamodb")
    with Stubber(c) as s:
        s.add_response("update_item", {"Attributes": {"declared": {"S": json.dumps({"key": "k"})}}}, None)
        s.add_client_error("update_item", service_error_code="ConditionalCheckFailedException")
        d = DynamoItems(c, "t")
        assert d.claim("thing", "i" * 32, 1) == {"key": "k"}
        assert d.claim("thing", "i" * 32, 1) is None


def test_dynamo_rate_limit_counter():
    c = client("dynamodb")
    with Stubber(c) as s:
        s.add_response("update_item", {}, None)
        s.add_client_error("update_item", service_error_code="ConditionalCheckFailedException")
        d = DynamoItems(c, "t")
        assert d.count_upload("thing", "2026102100", 5, 60) is True
        assert d.count_upload("thing", "2026102100", 5, 60) is False
