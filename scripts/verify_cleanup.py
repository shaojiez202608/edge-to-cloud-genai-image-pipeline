"""Residual-resource check after destroy (read-only). Exits 1 if anything of this project is left.

    uv run python scripts/verify_cleanup.py [--params FILE] [--regions us-east-1 ...]

Checked per region: the stack; S3 buckets (with object versions); log groups (explicit ones and the
ones services create: Lambda custom-resource handlers, Container Insights); Lambda; SQS; DynamoDB;
Step Functions; SNS; CloudWatch alarms and dashboards; ECR; ECS clusters and active task
definitions; CodeBuild; Secrets Manager (including secrets scheduled for deletion); Bedrock
guardrails; VPCs and security groups tagged with the project; AWS IoT (things, certificates found via
things or this project's policies, policies, thing groups, rules, role aliases, provisioning templates,
jobs) and Greengrass (deployments, core devices, component versions); IAM roles (global, once);
Budgets; resources found through the tagging API (each confirmed with the owning service, because the
tag index lags behind deletions); and this project's objects in the CDK bootstrap assets bucket. Output
masks 12-digit numbers.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

import boto3
from botocore.exceptions import ClientError
from cleanup_iot import find_residuals
from guards import load_params, mask_text

GENERIC_ASSET_MARKERS = ("Custom::", "AWS679f53fac002430cb0da5b7982bd2287", "BucketNotificationsHandler")


def project_asset_keys(stack: str) -> list[str]:
    """Object keys of this project's own assets (from cdk.out and the manifests saved by deploy.sh);
    CDK's generic provider packages are shared with other stacks and are excluded."""
    keys: list[str] = []
    manifests = [Path("cdk.out") / f"{stack}.assets.json", *sorted(Path(".deploy").glob(f"{stack}.*.assets.json"))]
    for m in (m for m in manifests if m.is_file()):
        for asset in json.loads(m.read_text())["files"].values():
            if any(x in asset.get("displayName", "") for x in GENERIC_ASSET_MARKERS):
                continue
            keys += [d["objectKey"] for d in asset["destinations"].values()]
    return sorted(set(keys))


def check_region(sess, region: str, d: dict, asset_keys: list[str], check_global: bool) -> list[str]:
    stack, prefix, project = d["stack_name"], d["resource_prefix"], d["tags"]["Project"]
    left: list[str] = []

    def add(kind: str, name: str) -> None:
        left.append(f"{region} {kind}: {name}")

    try:
        st = sess.client("cloudformation").describe_stacks(StackName=stack)["Stacks"][0]["StackStatus"]
        if st != "DELETE_COMPLETE":
            add("stack", f"{stack} {st}")
    except ClientError:
        pass
    s3 = sess.client("s3")
    for b in s3.list_buckets()["Buckets"]:
        if b["Name"].startswith((stack.lower(), f"{prefix}-")):
            add("s3 bucket", b["Name"])
    logs = sess.client("logs")
    for p in (f"/aws/lambda/{prefix}-", f"/aws/lambda/{stack}", f"/{prefix}/", f"/aws/vendedlogs/states/{prefix}",
              f"/aws/codebuild/{prefix}", f"/aws/ecs/containerinsights/{prefix}"):
        for page in logs.get_paginator("describe_log_groups").paginate(logGroupNamePrefix=p):
            for g in page["logGroups"]:
                add("log group", g["logGroupName"])
    for page in sess.client("lambda").get_paginator("list_functions").paginate():
        for f in page["Functions"]:
            if f["FunctionName"].startswith((f"{prefix}-", stack)):
                add("lambda", f["FunctionName"])
    for url in sess.client("sqs").list_queues(QueueNamePrefix=f"{prefix}-").get("QueueUrls", []):
        add("sqs", url.rsplit("/", 1)[-1])
    for page in sess.client("dynamodb").get_paginator("list_tables").paginate():
        for t in page["TableNames"]:
            if t.startswith(f"{prefix}-"):
                add("dynamodb", t)
    for page in sess.client("stepfunctions").get_paginator("list_state_machines").paginate():
        for m in page["stateMachines"]:
            if m["name"].startswith(f"{prefix}-"):
                add("state machine", m["name"])
    for page in sess.client("sns").get_paginator("list_topics").paginate():
        for t in page["Topics"]:
            if t["TopicArn"].rsplit(":", 1)[-1].startswith(f"{prefix}-"):
                add("sns", t["TopicArn"].rsplit(":", 1)[-1])
    cw = sess.client("cloudwatch")
    for page in cw.get_paginator("describe_alarms").paginate(AlarmNamePrefix=f"{prefix}-"):
        for a in page["MetricAlarms"]:
            add("alarm", a["AlarmName"])
    for db in cw.list_dashboards(DashboardNamePrefix=f"{prefix}-").get("DashboardEntries", []):
        add("dashboard", db["DashboardName"])
    ecr = sess.client("ecr")
    for page in ecr.get_paginator("describe_repositories").paginate():
        for r in page["repositories"]:
            if r["repositoryName"].startswith(f"{prefix}-"):
                add("ecr", r["repositoryName"])
    ecs = sess.client("ecs")
    for c in ecs.describe_clusters(clusters=[f"{prefix}-sim"]).get("clusters", []):
        if c["status"] != "INACTIVE":
            add("ecs cluster", f"{c['clusterName']} {c['status']}")
    for fam in (f"{prefix}-simulator", f"{prefix}-greengrass"):
        for page in ecs.get_paginator("list_task_definitions").paginate(familyPrefix=fam, status="ACTIVE"):
            for arn in page["taskDefinitionArns"]:
                add("task definition", arn.rsplit("/", 1)[-1])
    for name in sess.client("codebuild").list_projects().get("projects", []):
        if name.startswith(f"{prefix}-"):
            add("codebuild", name)
    sm = sess.client("secretsmanager")
    for page in sm.get_paginator("list_secrets").paginate(IncludePlannedDeletion=True,
                                                         Filters=[{"Key": "name", "Values": [f"{prefix}/"]}]):
        for s in page["SecretList"]:
            add("secret", s["Name"] + (" (scheduled for deletion)" if s.get("DeletedDate") else ""))
    for g in sess.client("bedrock").list_guardrails().get("guardrails", []):
        if g["name"].startswith(f"{prefix}-"):
            add("guardrail", g["name"])
    ec2 = sess.client("ec2")
    for v in ec2.describe_vpcs(Filters=[{"Name": "tag:Project", "Values": [project]}])["Vpcs"]:
        add("vpc", v["VpcId"])
    for sg in ec2.describe_security_groups(Filters=[{"Name": "group-name", "Values": [f"{prefix}-*"]}])["SecurityGroups"]:
        add("security group", sg["GroupId"])
    iot = find_residuals(sess, prefix)
    for k, v in iot.items():
        for item in (v if isinstance(v, list) else list(v)):
            add(f"iot {k}", item if isinstance(item, str) else json.dumps(item))
    if check_global:
        iam = sess.client("iam")
        for page in iam.get_paginator("list_roles").paginate():
            for r in page["Roles"]:
                if r["RoleName"].startswith((f"{prefix}-", stack[:30])):
                    add("iam role", r["RoleName"])
        account = sess.client("sts").get_caller_identity()["Account"]
        for b in sess.client("budgets").describe_budgets(AccountId=account).get("Budgets", []):
            if b["BudgetName"].startswith(f"{prefix}-"):
                add("budget", b["BudgetName"])
    tagging = sess.client("resourcegroupstaggingapi")
    for page in tagging.get_paginator("get_resources").paginate(TagFilters=[{"Key": "Project", "Values": [project]}]):
        for m in page["ResourceTagMappingList"]:
            if exists(sess, m["ResourceARN"]):
                add("tagged resource (confirmed)", m["ResourceARN"])
    if asset_keys:
        account = sess.client("sts").get_caller_identity()["Account"]
        bucket = f"cdk-hnb659fds-assets-{account}-{region}"
        for key in asset_keys:
            try:
                v = s3.list_object_versions(Bucket=bucket, Prefix=key)
            except ClientError:
                continue
            n = sum(1 for x in v.get("Versions", []) + v.get("DeleteMarkers", []) if x["Key"] == key)
            if n:
                add("bootstrap asset object", f"{key} versions={n}")
    return left


def exists(sess, arn: str) -> bool:
    svc, name = arn.split(":")[2], arn.split(":")[-1].split("/")[-1]
    try:
        if svc == "lambda":
            sess.client("lambda").get_function(FunctionName=arn)
        elif svc == "s3":
            sess.client("s3").head_bucket(Bucket=name)
        elif svc == "sqs":
            sess.client("sqs").get_queue_url(QueueName=name)
        elif svc == "logs":
            g = arn.split(":log-group:")[-1].removesuffix(":*")
            return bool(sess.client("logs").describe_log_groups(logGroupNamePrefix=g)["logGroups"])
        elif svc == "dynamodb":
            sess.client("dynamodb").describe_table(TableName=name)
        elif svc == "states":
            sess.client("stepfunctions").describe_state_machine(stateMachineArn=arn)
        elif svc == "ecs" and ":task-definition/" in arn:
            return sess.client("ecs").describe_task_definition(taskDefinition=arn)["taskDefinition"]["status"] == "ACTIVE"
        elif svc == "ecs":
            cl = sess.client("ecs").describe_clusters(clusters=[arn]).get("clusters", [])
            return bool(cl) and cl[0]["status"] != "INACTIVE"
        elif svc == "secretsmanager":
            sess.client("secretsmanager").describe_secret(SecretId=arn)
        elif svc == "iot" and ":thinggroup/" in arn:
            sess.client("iot").describe_thing_group(thingGroupName=name)
        elif svc == "iot" and ":rule/" in arn:
            sess.client("iot").get_topic_rule(ruleName=name)
        elif svc == "iot" and ":job/" in arn:
            return sess.client("iot").describe_job(jobId=name)["job"]["status"] != "DELETION_IN_PROGRESS"
        elif svc == "greengrass":
            sess.client("greengrassv2").describe_component(arn=arn)
        elif svc == "codebuild":
            return bool(sess.client("codebuild").batch_get_projects(names=[name])["projects"])
        elif svc == "ecr":
            sess.client("ecr").describe_repositories(repositoryNames=[name])
        elif svc == "sns":
            sess.client("sns").get_topic_attributes(TopicArn=arn)
        elif svc == "cloudwatch":
            return bool(sess.client("cloudwatch").describe_alarms(AlarmNames=[name])["MetricAlarms"])
        elif svc == "ec2" and ":vpc/" in arn:
            sess.client("ec2").describe_vpcs(VpcIds=[name])
        elif svc == "ec2":
            return True
        elif svc == "bedrock":
            sess.client("bedrock").get_guardrail(guardrailIdentifier=arn)
        elif svc == "iam":
            sess.client("iam").get_role(RoleName=name)
        else:
            return True  # unknown type: report it
        return True
    except ClientError:
        return False


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--params")
    ap.add_argument("--regions", nargs="*")
    args = ap.parse_args()
    d = load_params(args.params)["deploy"]
    regions = args.regions or [d["region"]]
    keys = project_asset_keys(d["stack_name"])
    print(f"stack={d['stack_name']} prefix={d['resource_prefix']} regions={regions} project_asset_keys={len(keys)}")
    left: list[str] = []
    for i, r in enumerate(regions):
        left += check_region(boto3.session.Session(region_name=r), r, d, keys, check_global=(i == 0))
    for item in left:
        print(mask_text("LEFT  " + item))
    if left:
        print(f"Residual resources remain: {len(left)}")
        sys.exit(1)
    print("Cleaned up: nothing of this project remains in the checked regions.")


if __name__ == "__main__":
    main()
