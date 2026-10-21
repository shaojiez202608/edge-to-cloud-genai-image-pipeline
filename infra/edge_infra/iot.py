"""AWS IoT Core resources: device and claim policies, thing groups, the fleet provisioning template
with its pre-provisioning hook, topic rules, the Greengrass token-exchange role alias, per-core
Greengrass policies (literal thing names: thing policy variables are not supported for core devices)
and the secrets that hold certificate material for the simulators."""

from __future__ import annotations

import json

from aws_cdk import Aws, RemovalPolicy
from aws_cdk import aws_iam as iam
from aws_cdk import aws_iot as iot
from aws_cdk import aws_lambda as lambda_
from aws_cdk import aws_secretsmanager as sm

from edge_pipeline import policy, rules

from .params import gg_core_names

GG_COMPONENT = "com.example.EdgeModel"


def _arn(service: str, rest: str) -> str:
    return f"arn:{Aws.PARTITION}:{service}:{Aws.REGION}:{Aws.ACCOUNT_ID}:{rest}"


def _trust(principal: str, source_arn: str) -> iam.PrincipalBase:
    return iam.ServicePrincipal(principal).with_conditions({
        "StringEquals": {"aws:SourceAccount": Aws.ACCOUNT_ID},
        "ArnLike": {"aws:SourceArn": source_arn},
    })


def build(stack) -> None:
    d, prefix = stack.deploy, stack.prefix
    pu = prefix.replace("-", "_")
    stack.device_policy_name = f"{prefix}-device"
    stack.template_name = f"{prefix}-sim-fleet"
    stack.sim_group, stack.gg_group = f"{prefix}-sim-fleet", f"{prefix}-gg-fleet"

    iot.CfnPolicy(stack, "DevicePolicy", policy_name=stack.device_policy_name,
                  policy_document=policy.device_policy(Aws.PARTITION, Aws.REGION, Aws.ACCOUNT_ID))
    stack.claim_policy = iot.CfnPolicy(
        stack, "ClaimPolicy", policy_name=f"{prefix}-claim",
        policy_document=policy.claim_policy(Aws.PARTITION, Aws.REGION, Aws.ACCOUNT_ID, prefix, stack.template_name))
    for cid, name, desc in (("SimGroup", stack.sim_group, "lightweight simulators"),
                            ("GgGroup", stack.gg_group, "Greengrass core devices")):
        iot.CfnThingGroup(stack, cid, thing_group_name=name,
                          thing_group_properties=iot.CfnThingGroup.ThingGroupPropertiesProperty(
                              thing_group_description=f"{desc} (prototype)"))

    # ---------------------------------------------------------------- fleet provisioning by claim
    template_arn = _arn("iot", f"provisioningtemplate/{stack.template_name}")
    prov_role = iam.Role(
        stack, "ProvisioningRole", role_name=f"{prefix}-provisioning",
        assumed_by=_trust("iot.amazonaws.com", template_arn),
        managed_policies=[iam.ManagedPolicy.from_aws_managed_policy_name("service-role/AWSIoTThingsRegistration")],
        description="Fleet provisioning template role (AWS managed registration policy)")
    stack.provisioning_role = prov_role
    hook = stack.fn["provisioning_hook"]
    lambda_.CfnPermission(stack, "HookInvokeByIot", action="lambda:InvokeFunction", function_name=hook.function_arn,
                          principal="iot.amazonaws.com", source_arn=template_arn, source_account=Aws.ACCOUNT_ID)
    body = {
        "Parameters": {"SerialNumber": {"Type": "String"}, "AWS::IoT::Certificate::Id": {"Type": "String"}},
        "Resources": {
            "thing": {"Type": "AWS::IoT::Thing", "OverrideSettings": {
                "AttributePayload": "MERGE", "ThingTypeName": "REPLACE", "ThingGroups": "DO_NOTHING"},
                "Properties": {"ThingName": {"Fn::Join": ["", [f"{prefix}-sim-", {"Ref": "SerialNumber"}]]},
                               "AttributePayload": {"kind": "sim"}, "ThingGroups": [stack.sim_group]}},
            "certificate": {"Type": "AWS::IoT::Certificate",
                            "Properties": {"CertificateId": {"Ref": "AWS::IoT::Certificate::Id"}, "Status": "ACTIVE"}},
            "policy": {"Type": "AWS::IoT::Policy", "Properties": {"PolicyName": stack.device_policy_name}},
        },
    }
    tmpl = iot.CfnProvisioningTemplate(
        stack, "ProvisioningTemplate", template_name=stack.template_name, enabled=True,
        description="Simulated edge boxes: claim certificate -> own certificate, thing and device policy",
        provisioning_role_arn=prov_role.role_arn, template_body=json.dumps(body), template_type="FLEET_PROVISIONING",
        pre_provisioning_hook=iot.CfnProvisioningTemplate.ProvisioningHookProperty(
            target_arn=hook.function_arn, payload_version="2020-04-01"))
    tmpl.node.add_dependency(prov_role)
    for child in ("DevicePolicy", "SimGroup", "HookInvokeByIot"):
        tmpl.node.add_dependency(stack.node.find_child(child))

    # ---------------------------------------------------------------- topic rules
    rule_role = iam.Role(stack, "RuleRole", role_name=f"{prefix}-iot-rules",
                         assumed_by=_trust("iot.amazonaws.com", _arn("iot", f"rule/{pu}_*")),
                         description="IoT rule actions: send to this stack's queues, write presence")
    for q in (stack.ingest, stack.telemetry, stack.rule_errors):
        q.grant_send_messages(rule_role)
    rule_role.add_to_policy(iam.PolicyStatement(actions=["dynamodb:PutItem"], resources=[stack.presence.table_arn]))
    error_action = iot.CfnTopicRule.ActionProperty(sqs=iot.CfnTopicRule.SqsActionProperty(
        queue_url=stack.rule_errors.queue_url, role_arn=rule_role.role_arn, use_base64=False))
    stack.rule_names = []
    for r in rules.device_rules(prefix):
        kind, target = r.action.split(":")
        if kind == "sqs":
            q = {"ingest": stack.ingest, "telemetry": stack.telemetry}[target]
            action = iot.CfnTopicRule.ActionProperty(sqs=iot.CfnTopicRule.SqsActionProperty(
                queue_url=q.queue_url, role_arn=rule_role.role_arn, use_base64=False))
        elif kind == "lambda":
            action = iot.CfnTopicRule.ActionProperty(lambda_=iot.CfnTopicRule.LambdaActionProperty(
                function_arn=stack.fn[target].function_arn))
        else:
            action = iot.CfnTopicRule.ActionProperty(dynamo_d_bv2=iot.CfnTopicRule.DynamoDBv2ActionProperty(
                role_arn=rule_role.role_arn,
                put_item=iot.CfnTopicRule.PutItemInputProperty(table_name=stack.presence.table_name)))
        name = rules.deployed_rule_name(prefix, r)
        stack.rule_names.append(name)
        cid = "Rule" + r.name.title().replace("_", "")
        iot.CfnTopicRule(stack, cid, rule_name=name, topic_rule_payload=iot.CfnTopicRule.TopicRulePayloadProperty(
            sql=r.sql(), aws_iot_sql_version=rules.SQL_VERSION, rule_disabled=False, actions=[action],
            error_action=error_action, description=f"{r.name} ({prefix})"))
        if kind == "lambda":
            lambda_.CfnPermission(stack, f"{cid}Invoke", action="lambda:InvokeFunction",
                                  function_name=stack.fn[target].function_arn, principal="iot.amazonaws.com",
                                  source_arn=_arn("iot", f"rule/{name}"), source_account=Aws.ACCOUNT_ID)

    # ---------------------------------------------------------------- Greengrass
    stack.role_alias_name = f"{prefix}-gg-tes"
    alias_arn = _arn("iot", f"rolealias/{stack.role_alias_name}")
    tes = iam.Role(stack, "GreengrassTesRole", role_name=f"{prefix}-gg-tes",
                   assumed_by=_trust("credentials.iot.amazonaws.com", alias_arn),
                   description="Greengrass token exchange role: read this project's component artifacts")
    tes.add_to_policy(iam.PolicyStatement(actions=["s3:GetObject"], resources=[f"{stack.bucket.bucket_arn}/gg-artifacts/*"]))
    # Found in testing: the nucleus calls GetBucketLocation before downloading an artifact
    # (deployment error S3_GET_BUCKET_LOCATION_ACCESS_DENIED without it).
    tes.add_to_policy(iam.PolicyStatement(actions=["s3:GetBucketLocation"], resources=[stack.bucket.bucket_arn]))
    stack.tes_role = tes
    iot.CfnRoleAlias(stack, "GreengrassRoleAlias", role_alias=stack.role_alias_name, role_arn=tes.role_arn,
                     credential_duration_seconds=3600)
    stack.gg_cores = gg_core_names(prefix, d["greengrass_core_count"])
    for i, thing in enumerate(stack.gg_cores, start=1):
        iot.CfnPolicy(stack, f"GgCorePolicy{i:02d}", policy_name=f"{thing}-policy",
                      policy_document=_gg_core_policy(thing, stack.role_alias_name))

    # ---------------------------------------------------------------- certificate material
    rp = RemovalPolicy.DESTROY if d["removal_policy"] == "destroy" else RemovalPolicy.RETAIN
    stack.claim_secret = sm.Secret(stack, "ClaimSecret", secret_name=f"{prefix}/claim",
                                   description="Fleet provisioning claim certificate (written by scripts/setup_identities.py)",
                                   removal_policy=rp)
    stack.gg_secrets = [sm.Secret(stack, f"GgSecret{i:02d}", secret_name=f"{prefix}/gg/{thing}",
                                  description=f"Greengrass core certificate for {thing}", removal_policy=rp)
                        for i, thing in enumerate(stack.gg_cores, start=1)]


def _gg_core_policy(thing: str, role_alias: str) -> dict:
    """Minimal core-device policy from the Greengrass developer guide, with this core's literal thing
    name and artifact access narrowed to this project's component."""
    a = lambda kind, rest: _arn("iot", f"{kind}/{rest}")  # noqa: E731
    gg = f"$aws/things/{thing}"
    return {
        "Version": "2012-10-17",
        "Statement": [
            {"Effect": "Allow", "Action": "iot:Connect", "Resource": a("client", f"{thing}*")},
            {"Effect": "Allow", "Action": ["iot:Publish", "iot:Receive"], "Resource": [
                a("topic", f"{gg}/greengrass/health/json"), a("topic", f"{gg}/greengrassv2/health/json"),
                a("topic", f"{gg}/jobs/*"), a("topic", f"{gg}/shadow/*")]},
            {"Effect": "Allow", "Action": "iot:Subscribe", "Resource": [
                a("topicfilter", f"{gg}/jobs/*"), a("topicfilter", f"{gg}/shadow/*")]},
            {"Effect": "Allow", "Action": "iot:AssumeRoleWithCertificate", "Resource": a("rolealias", role_alias)},
            {"Effect": "Allow", "Action": "greengrass:GetComponentVersionArtifact",
             "Resource": _arn("greengrass", f"components:{GG_COMPONENT}:versions:*")},
            {"Effect": "Allow", "Action": "greengrass:ListThingGroupsForCoreDevice", "Resource": a("thing", thing)},
            {"Effect": "Allow", "Action": ["greengrass:ResolveComponentCandidates",
                                           "greengrass:GetDeploymentConfiguration"], "Resource": "*"},
        ],
    }
