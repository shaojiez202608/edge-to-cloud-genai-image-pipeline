"""Lambda functions (arm64, one bundle, one role each), the Step Functions workflow and the Bedrock
guardrail."""

from __future__ import annotations

import json

from aws_cdk import Aws, Duration
from aws_cdk import aws_bedrock as bedrock
from aws_cdk import aws_iam as iam
from aws_cdk import aws_lambda as lambda_
from aws_cdk import aws_lambda_event_sources as les
from aws_cdk import aws_stepfunctions as sfn
from aws_cdk import aws_stepfunctions_tasks as tasks

from edge_pipeline.config import PipelineConfig

from .common import foundation_model_id, log_group

FUNCTIONS = ("upload_url", "dispatch", "validate_input", "analyze", "finalize", "fallback", "provisioning_hook")


def _guardrail(stack) -> None:
    filters = [
        bedrock.CfnGuardrail.ContentFilterConfigProperty(
            type=t, input_strength="HIGH", output_strength="HIGH",
            input_modalities=["TEXT", "IMAGE"], output_modalities=["TEXT"])
        for t in ("HATE", "INSULTS", "SEXUAL", "VIOLENCE", "MISCONDUCT")
    ] + [bedrock.CfnGuardrail.ContentFilterConfigProperty(
        type="PROMPT_ATTACK", input_strength="HIGH", output_strength="NONE", input_modalities=["TEXT", "IMAGE"],
        output_modalities=["TEXT"])]
    stack.guardrail = bedrock.CfnGuardrail(
        stack, "Guardrail", name=f"{stack.prefix}-guardrail",
        description="Image input and draft copy checks for the edge image pipeline (prototype)",
        blocked_input_messaging="Blocked.", blocked_outputs_messaging="Blocked.",
        content_policy_config=bedrock.CfnGuardrail.ContentPolicyConfigProperty(filters_config=filters))
    stack.guardrail_version = bedrock.CfnGuardrailVersion(
        stack, "GuardrailVersion", guardrail_identifier=stack.guardrail.attr_guardrail_id,
        description="pinned version used by the analyze function")


def _role(stack, name: str, log_arn: str) -> iam.Role:
    role = iam.Role(stack, f"{name.title().replace('_', '')}Role", role_name=f"{stack.prefix}-fn-{name.replace('_', '-')}",
                    assumed_by=iam.ServicePrincipal("lambda.amazonaws.com"),
                    description=f"{name} function (least privilege, inline)")
    role.add_to_policy(iam.PolicyStatement(actions=["logs:CreateLogStream", "logs:PutLogEvents"],
                                           resources=[f"{log_arn}:*"]))
    return role


def build(stack, code_dir: str) -> None:
    d, prefix, cfg = stack.deploy, stack.prefix, stack.pipeline_cfg
    if cfg.guardrail_enabled:
        _guardrail(stack)
    else:
        stack.guardrail = None
    bucket_arn = stack.bucket.bucket_arn
    code = lambda_.Code.from_asset(code_dir)
    env = {"PREFIX": prefix, "BUCKET": stack.bucket.bucket_name, "ITEMS_TABLE": stack.items.table_name,
           "PIPELINE_CONFIG": cfg.to_json(), "METRICS_NAMESPACE": f"{prefix}/pipeline",
           "MAX_FLEET_SIZE": str(d["max_fleet_size"])}
    if stack.guardrail is not None:
        env.update(GUARDRAIL_ID=stack.guardrail.attr_guardrail_id, GUARDRAIL_VERSION=stack.guardrail_version.attr_version)

    stack.fn, stack.fn_roles, stack.fn_logs = {}, {}, {}
    timeouts = {"analyze": d["analyze_timeout_s"], "dispatch": 30, "provisioning_hook": 5}
    for name in FUNCTIONS:
        fname = f"{prefix}-{name.replace('_', '-')}"
        lg = log_group(stack, f"{name.title().replace('_', '')}Logs", f"/aws/lambda/{fname}", d)
        role = _role(stack, name, lg.log_group_arn)
        stack.fn_logs[name], stack.fn_roles[name] = lg, role
        stack.fn[name] = lambda_.Function(
            stack, f"{name.title().replace('_', '')}Fn", function_name=fname, code=code,
            handler=f"handlers.{name}", runtime=lambda_.Runtime.PYTHON_3_14,
            architecture=lambda_.Architecture.ARM_64, memory_size=d["lambda_memory_mb"] if name in (
                "validate_input", "analyze", "finalize", "fallback") else 256,
            timeout=Duration.seconds(timeouts.get(name, 60)), role=role, log_group=lg, environment=env,
            description=f"edge image pipeline: {name}")

    obj = lambda p: f"{bucket_arn}/{p}"  # noqa: E731
    iot_topic = lambda t: f"arn:{Aws.PARTITION}:iot:{Aws.REGION}:{Aws.ACCOUNT_ID}:topic/{t}"  # noqa: E731
    items_arn = stack.items.table_arn
    P = iam.PolicyStatement  # noqa: N806
    r = stack.fn_roles
    r["upload_url"].add_to_policy(P(actions=["s3:PutObject"], resources=[obj("uploads/*")]))
    r["upload_url"].add_to_policy(P(actions=["dynamodb:PutItem", "dynamodb:UpdateItem"], resources=[items_arn]))
    r["upload_url"].add_to_policy(P(actions=["iot:Publish"], resources=[iot_topic("edge/*/upload/url")]))
    r["dispatch"].add_to_policy(P(actions=["dynamodb:UpdateItem"], resources=[items_arn]))
    r["validate_input"].add_to_policy(P(actions=["s3:GetObject"], resources=[obj("uploads/*")]))
    r["validate_input"].add_to_policy(P(actions=["s3:PutObject"], resources=[obj("work/*")]))
    r["analyze"].add_to_policy(P(actions=["s3:GetObject"], resources=[obj("work/*")]))
    model_id = cfg.model_id
    fm = foundation_model_id(model_id)
    models = [f"arn:{Aws.PARTITION}:bedrock:{reg}::foundation-model/{fm}" for reg in d["model_regions"]]
    if fm != model_id:
        models.append(f"arn:{Aws.PARTITION}:bedrock:{Aws.REGION}:{Aws.ACCOUNT_ID}:inference-profile/{model_id}")
    if cfg.model_mode == "bedrock":
        r["analyze"].add_to_policy(P(actions=["bedrock:InvokeModel"], resources=models))
    if stack.guardrail is not None:
        r["analyze"].add_to_policy(P(actions=["bedrock:ApplyGuardrail"], resources=[stack.guardrail.attr_guardrail_arn]))
    for name in ("finalize", "fallback"):
        r[name].add_to_policy(P(actions=["s3:GetObject"], resources=[obj("work/*"), obj("shelf/manifest.json")]))
        r[name].add_to_policy(P(actions=["s3:PutObject"], resources=[obj("results/*"), obj("shelf/*")]))
        r[name].add_to_policy(P(actions=["s3:ListBucket"], resources=[bucket_arn],
                                conditions={"StringEquals": {"s3:prefix": "shelf/manifest.json"}}))
        r[name].add_to_policy(P(actions=["dynamodb:UpdateItem"], resources=[items_arn]))
        r[name].add_to_policy(P(actions=["iot:Publish"], resources=[iot_topic("edge/*/results")]))
    for name in ("upload_url", "finalize", "fallback"):
        # iot:DescribeEndpoint has no resource-level permissions.
        r[name].add_to_policy(P(actions=["iot:DescribeEndpoint"], resources=["*"]))

    _workflow(stack)
    stack.fn["dispatch"].add_environment("STATE_MACHINE_ARN", stack.state_machine.state_machine_arn)
    r["dispatch"].add_to_policy(P(actions=["states:StartExecution"], resources=[stack.state_machine.state_machine_arn]))
    stack.fn["dispatch"].add_event_source(les.SqsEventSource(
        stack.ingest, batch_size=10, report_batch_item_failures=True,
        max_concurrency=d["dispatch_max_concurrency"]))


def _workflow(stack) -> None:
    d = stack.deploy

    def step(name: str, fn_key: str, timeout_s: int) -> tasks.LambdaInvoke:
        return tasks.LambdaInvoke(stack, name, lambda_function=stack.fn[fn_key], payload_response_only=True,
                                  retry_on_service_exceptions=True,
                                  task_timeout=sfn.Timeout.duration(Duration.seconds(timeout_s)))

    validate = step("ValidateInput", "validate_input", 60)
    analyze = step("Analyze", "analyze", d["analyze_timeout_s"])
    analyze.add_retry(errors=["ModelTransientError"], interval=Duration.seconds(5), max_attempts=3, backoff_rate=2.0,
                      jitter_strategy=sfn.JitterType.FULL)
    finalize = step("Finalize", "finalize", 60)
    fallback = step("Fallback", "fallback", 60)
    for s in (validate, analyze, finalize):
        s.add_catch(fallback, errors=["States.ALL"], result_path="$.error")
    lg = log_group(stack, "WorkflowLogs", f"/aws/vendedlogs/states/{stack.prefix}-workflow", d)
    stack.workflow_logs = lg
    stack.state_machine = sfn.StateMachine(
        stack, "Workflow", state_machine_name=f"{stack.prefix}-workflow",
        state_machine_type=sfn.StateMachineType.STANDARD,
        definition_body=sfn.DefinitionBody.from_chainable(validate.next(analyze).next(finalize)),
        timeout=Duration.minutes(15), tracing_enabled=True,
        # Level ALL without execution data: state transitions are logged, payloads are not.
        logs=sfn.LogOptions(destination=lg, level=sfn.LogLevel.ALL, include_execution_data=False))


def model_resources_doc(stack) -> str:  # used by tests
    return json.dumps(stack.resolve(stack.fn_roles["analyze"].node.find_child("DefaultPolicy").document.to_json()))


__all__ = ["FUNCTIONS", "build", "PipelineConfig"]
