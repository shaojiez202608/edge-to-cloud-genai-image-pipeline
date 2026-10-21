"""CDK assertions for both profiles, synthesised without AWS credentials (tokens for the account)."""

import json
from pathlib import Path

import aws_cdk as cdk
import pytest
from aws_cdk.assertions import Match, Template
from cdk_nag import AwsSolutionsChecks
from edge_infra.stack import EdgePipelineStack

ROOT = Path(__file__).resolve().parents[1]


def params(profile):
    return json.loads((ROOT / "config" / f"{profile}.example.json").read_text())


def synth(tmp, p, operators=None):
    code = tmp / "code"
    code.mkdir(exist_ok=True)
    (code / "handlers.py").write_text("def upload_url(e, c):\n    return None\n")
    img = tmp / "img"
    (img / "containers").mkdir(parents=True, exist_ok=True)
    (img / "containers" / "buildspec.yml").write_text("version: 0.2\n")
    out = tmp / "cdk.out"
    app = cdk.App(outdir=str(out), context={"@aws-cdk/core:enablePartitionLiterals": True,
                                           "@aws-cdk/aws-iam:minimizePolicies": True})
    d = p["deploy"]
    stack = EdgePipelineStack(app, d["stack_name"], deploy=d, pipeline=p["pipeline"], code_dir=str(code),
                              image_src_dir=str(img), operator_arns=operators,
                              env=cdk.Environment(region=d["region"]))
    for k, v in d["tags"].items():
        cdk.Tags.of(app).add(k, v)
    cdk.Validations.of(app).add_plugins(AwsSolutionsChecks(app))
    app.synth()
    report = json.loads((out / "validation-report.json").read_text())
    return stack, Template.from_stack(stack), next(r for r in report["pluginReports"] if r["pluginName"] == "AwsSolutions")


@pytest.fixture(scope="module")
def sandbox(tmp_path_factory):
    return synth(tmp_path_factory.mktemp("sb"), params("sandbox"), ["arn:aws:iam::111122223333:role/Operator"])


@pytest.fixture(scope="module")
def handover(tmp_path_factory):
    return synth(tmp_path_factory.mktemp("ho"), params("handover"))


def test_nag_clean_with_reasons(sandbox, handover):
    for _, _, nag in (sandbox, handover):
        assert nag["conclusion"] == "success" and nag["violations"] == []
        assert all(len(s["reason"]) > 30 for s in nag["suppressedViolations"])


def test_nag_still_clean_with_other_prefix_and_core_count(tmp_path):
    p = params("sandbox")
    p["deploy"].update(stack_name="demo-xy-pipeline", resource_prefix="demo-xy", greengrass_core_count=1)
    _, t, nag = synth(tmp_path, p)
    assert nag["violations"] == []
    t.resource_count_is("AWS::IoT::Policy", 3)  # device, claim, one core


def test_compute_is_arm64(sandbox):
    _, t, _ = sandbox
    fns = t.find_resources("AWS::Lambda::Function", {"Properties": {"Handler": Match.string_like_regexp("^handlers\\.")}})
    assert len(fns) == 7 and all(f["Properties"]["Architectures"] == ["arm64"] for f in fns.values())
    for td in t.find_resources("AWS::ECS::TaskDefinition").values():
        assert td["Properties"]["RuntimePlatform"] == {"CpuArchitecture": "ARM64", "OperatingSystemFamily": "LINUX"}
    t.has_resource_properties("AWS::CodeBuild::Project", {"Environment": Match.object_like(
        {"Type": "ARM_CONTAINER", "Image": "aws/codebuild/amazonlinux-aarch64-standard:3.0", "PrivilegedMode": True})})


def test_device_policy_uses_thing_variable_and_no_open_wildcards(sandbox):
    _, t, _ = sandbox
    pol = t.find_resources("AWS::IoT::Policy", {"Properties": {"PolicyName": "proto-edge-device"}})
    doc = json.dumps(next(iter(pol.values()))["Properties"]["PolicyDocument"])
    assert doc.count("${iot:Connection.Thing.ThingName}") >= 10
    assert '"iot:*"' not in doc and ":topic/*" not in doc and ":client/*" not in doc
    assert "iot:Connection.Thing.IsAttached" in doc


def test_greengrass_core_policies_use_literal_names(sandbox):
    _, t, _ = sandbox
    pols = t.find_resources("AWS::IoT::Policy", {"Properties": {"PolicyName": Match.string_like_regexp("-gg-0[1-3]-policy$")}})
    assert len(pols) == 3
    for p in pols.values():
        doc = json.dumps(p["Properties"]["PolicyDocument"])
        assert "${iot:" not in doc and "greengrass:*" not in doc


def test_rules_and_error_action(sandbox):
    _, t, _ = sandbox
    rules = t.find_resources("AWS::IoT::TopicRule")
    assert len(rules) == 4
    for r in rules.values():
        payload = r["Properties"]["TopicRulePayload"]
        assert "ErrorAction" in payload
        if "presence" not in payload["Sql"]:
            assert "topic(2) = clientid()" in payload["Sql"]


def test_trust_policies_carry_source_conditions(sandbox):
    stack, t, _ = sandbox
    for name, service in (("proto-edge-iot-rules", "iot.amazonaws.com"), ("proto-edge-provisioning", "iot.amazonaws.com"),
                          ("proto-edge-gg-tes", "credentials.iot.amazonaws.com"),
                          ("proto-edge-task-simulator", "ecs-tasks.amazonaws.com")):
        role = next(r for r in t.find_resources("AWS::IAM::Role").values() if r["Properties"].get("RoleName") == name)
        st = role["Properties"]["AssumeRolePolicyDocument"]["Statement"][0]
        assert st["Principal"]["Service"] == service
        assert "aws:SourceAccount" in st["Condition"]["StringEquals"] and "aws:SourceArn" in st["Condition"]["ArnLike"]
    assert any("ImageBuild" in p for p in stack.trust_changed)


def test_bucket_controls(sandbox):
    _, t, _ = sandbox
    for b in t.find_resources("AWS::S3::Bucket").values():
        pab = b["Properties"]["PublicAccessBlockConfiguration"]
        assert all(pab[k] for k in ("BlockPublicAcls", "BlockPublicPolicy", "IgnorePublicAcls", "RestrictPublicBuckets"))
        assert b["Properties"]["BucketEncryption"]
    policies = json.dumps(t.find_resources("AWS::S3::BucketPolicy"))
    assert '"aws:SecureTransport": "false"' in policies
    assert "OnlyPipelineRolesAndOperators" in policies and "role/Operator" in policies


def test_no_open_ingress(sandbox):
    _, t, _ = sandbox
    for sg in t.find_resources("AWS::EC2::SecurityGroup").values():
        assert not sg["Properties"].get("SecurityGroupIngress")
        for e in sg["Properties"].get("SecurityGroupEgress", []):
            assert e.get("FromPort") in (443, 8883, None) or e.get("CidrIp") == "255.255.255.255/32"


def test_removal_policy_parameter(sandbox, handover):
    for (_, t, _), want in ((sandbox, "Delete"), (handover, "Retain")):
        for b in t.find_resources("AWS::S3::Bucket").values():
            assert b["DeletionPolicy"] == want
        for q in t.find_resources("AWS::DynamoDB::GlobalTable").values():
            assert q["DeletionPolicy"] == want


def test_log_groups_have_retention_and_budget_switch(sandbox, handover):
    for (_, t, _), budget in ((sandbox, 1), (handover, 0)):
        for lg in t.find_resources("AWS::Logs::LogGroup").values():
            assert lg["Properties"]["RetentionInDays"] > 0
        t.resource_count_is("AWS::Budgets::Budget", budget)


def test_bedrock_permissions_are_specific(sandbox):
    _, t, _ = sandbox
    doc = json.dumps(t.find_resources("AWS::IAM::Policy"))
    assert "foundation-model/amazon.nova-2-lite-v1:0" in doc and "inference-profile/us.amazon.nova-2-lite-v1:0" in doc
    assert "bedrock:*" not in doc and "arn:aws:bedrock:*" not in doc


def test_guardrail_filters_images_and_prompt_attacks(sandbox):
    _, t, _ = sandbox
    g = next(iter(t.find_resources("AWS::Bedrock::Guardrail").values()))
    filters = g["Properties"]["ContentPolicyConfig"]["FiltersConfig"]
    pa = next(f for f in filters if f["Type"] == "PROMPT_ATTACK")
    assert pa["OutputStrength"] == "NONE" and "IMAGE" in pa["InputModalities"]


def test_tags(sandbox):
    _, t, _ = sandbox
    q = next(iter(t.find_resources("AWS::SQS::Queue").values()))
    tags = {x["Key"]: x["Value"] for x in q["Properties"]["Tags"]}
    assert tags == {"Project": "edge-genai-pipeline", "Purpose": "prototype", "Teardown": "after-test"}


def test_template_has_no_account_id(sandbox):
    _, t, _ = sandbox
    import re
    blob = json.dumps(t.to_json())
    assert not [m for m in re.findall(r"(?<!\d)\d{12}(?!\d)", blob) if m != "111122223333"]
