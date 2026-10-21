"""One stack: storage -> functions/workflow -> IoT -> simulator compute -> observability, then the
data-bucket resource policy, trust-policy conditions, outputs and cdk-nag acknowledgements."""

from __future__ import annotations

from aws_cdk import Aws, CfnOutput, Stack
from aws_cdk import aws_iam as iam
from constructs import Construct

from edge_pipeline.config import PipelineConfig

from . import compute, data, functions, iot, nag, observe


class EdgePipelineStack(Stack):
    def __init__(self, scope: Construct, cid: str, *, deploy: dict, pipeline: dict, code_dir: str,
                 image_src_dir: str, operator_arns: list[str] | None = None, **kw) -> None:
        super().__init__(scope, cid, **kw)
        self.deploy, self.prefix = deploy, deploy["resource_prefix"]
        self.pipeline_cfg = PipelineConfig.from_dict(pipeline)
        self.operator_arns = list(operator_arns or [])
        data.build(self)
        functions.build(self, code_dir)
        iot.build(self)
        compute.build(self, image_src_dir)
        observe.build(self)
        self._bucket_policy()
        self.trust_changed = _trust_conditions(self)
        self._outputs()
        nag.acknowledge(self)

    def _bucket_policy(self) -> None:
        """Object reads and writes only by this stack's roles and the named operators; everyone else
        is explicitly denied (on top of IAM)."""
        allowed = [r.role_arn for r in self.fn_roles.values()] + [
            self.tes_role.role_arn, self.task_roles["simulator"].role_arn] + self.operator_arns
        self.bucket.add_to_resource_policy(iam.PolicyStatement(
            sid="OnlyPipelineRolesAndOperators", effect=iam.Effect.DENY, principals=[iam.AnyPrincipal()],
            actions=["s3:GetObject", "s3:PutObject"], resources=[self.bucket.arn_for_objects("*")],
            conditions={"ArnNotLike": {"aws:PrincipalArn": allowed}}))

    def _outputs(self) -> None:
        out = {
            "Bucket": self.bucket.bucket_name, "ItemsTable": self.items.table_name,
            "PresenceTable": self.presence.table_name, "IngestQueueUrl": self.ingest.queue_url,
            "IngestDlqUrl": self.ingest_dlq.queue_url, "TelemetryQueueUrl": self.telemetry.queue_url,
            "RuleErrorsQueueUrl": self.rule_errors.queue_url, "StateMachineArn": self.state_machine.state_machine_arn,
            "Cluster": self.cluster.cluster_name, "SecurityGroup": self.sg.security_group_id,
            "Subnets": ",".join(s.subnet_id for s in self.vpc.public_subnets),
            "SimulatorTaskDef": self.task_defs["simulator"].task_definition_arn,
            "GreengrassTaskDef": self.task_defs["greengrass"].task_definition_arn,
            "ImageBuildProject": self.image_build.project_name, "ImageTag": self.image_tag,
            "ProvisioningTemplate": self.template_name, "ClaimPolicy": self.claim_policy.policy_name,
            "DevicePolicy": self.device_policy_name, "SimThingGroup": self.sim_group, "GgThingGroup": self.gg_group,
            "RoleAlias": self.role_alias_name, "GgCores": ",".join(self.gg_cores) or "-",
            "ClaimSecret": self.claim_secret.secret_name, "Dashboard": self.dashboard.dashboard_name,
            "AlarmTopic": self.alarm_topic.topic_arn,
            "Guardrail": self.guardrail.attr_guardrail_id if self.guardrail is not None else "-",
        }
        for k, v in out.items():
            CfnOutput(self, f"Out{k}", value=v, key=k)


# Services whose trust policies document aws:SourceAccount + aws:SourceArn (see SECURITY-NOTES.md).
def _source_arn(service: str, role_path: str, stack: Stack) -> str | None:
    p = stack.prefix
    if service == "codebuild.amazonaws.com":
        return f"arn:{Aws.PARTITION}:codebuild:{Aws.REGION}:{Aws.ACCOUNT_ID}:project/{p}-*"
    if service == "ecs-tasks.amazonaws.com" and "TaskRole" in role_path:
        return f"arn:{Aws.PARTITION}:ecs:{Aws.REGION}:{Aws.ACCOUNT_ID}:*"
    if service == "vpc-flow-logs.amazonaws.com":
        return f"arn:{Aws.PARTITION}:ec2:{Aws.REGION}:{Aws.ACCOUNT_ID}:vpc-flow-log/*"
    return None


def _trust_conditions(stack: Stack) -> list[str]:
    changed = []
    for node in stack.node.find_all():
        if not isinstance(node, iam.CfnRole):
            continue
        doc = stack.resolve(node.assume_role_policy_document)
        for idx, st in enumerate(doc.get("Statement", []) if isinstance(doc, dict) else []):
            svc = (st.get("Principal") or {}).get("Service")
            if not isinstance(svc, str) or st.get("Condition"):
                continue
            arn = _source_arn(svc, node.node.path, stack)
            if arn is None:
                continue
            node.add_property_override(f"AssumeRolePolicyDocument.Statement.{idx}.Condition", {
                "StringEquals": {"aws:SourceAccount": Aws.ACCOUNT_ID}, "ArnLike": {"aws:SourceArn": arn}})
            changed.append(node.node.path)
    return changed
