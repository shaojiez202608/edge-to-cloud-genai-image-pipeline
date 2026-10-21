"""Simulator compute: a small VPC (public subnets only, no inbound rules, no NAT), an ECS cluster,
ARM64 Fargate task definitions for the simulator and the Greengrass core, two ECR repositories and
an ARM CodeBuild project that builds both images (no local Docker is needed)."""

from __future__ import annotations

from aws_cdk import Aws, Duration
from aws_cdk import aws_codebuild as codebuild
from aws_cdk import aws_ec2 as ec2
from aws_cdk import aws_ecr as ecr
from aws_cdk import aws_ecs as ecs
from aws_cdk import aws_iam as iam
from aws_cdk import aws_s3_assets as assets

from .common import log_group, removal

CODEBUILD_IMAGE = "aws/codebuild/amazonlinux-aarch64-standard:3.0"


def build(stack, image_src_dir: str) -> None:
    d, prefix = stack.deploy, stack.prefix
    if stack.existing_network:
        # Deploy-time option (context existing_vpc_id / existing_subnet_ids): place the tasks in an
        # existing VPC's public subnets without changing that VPC; only this stack's security group is
        # created in it. Used when the account cannot create another VPC (DEPLOYMENT-LOG).
        vpc_id, subnet_ids = stack.existing_network
        stack.vpc = ec2.Vpc.from_vpc_attributes(stack, "Vpc", vpc_id=vpc_id,
                                                availability_zones=d["availability_zones"],
                                                public_subnet_ids=subnet_ids)
    else:
        flow = log_group(stack, "VpcFlowLogs", f"/{prefix}/vpc-flow", d)
        stack.vpc = ec2.Vpc(
            stack, "Vpc", vpc_name=f"{prefix}-vpc", ip_addresses=ec2.IpAddresses.cidr("10.42.0.0/24"),
            availability_zones=d["availability_zones"], nat_gateways=0, restrict_default_security_group=True,
            subnet_configuration=[ec2.SubnetConfiguration(name="public", subnet_type=ec2.SubnetType.PUBLIC,
                                                          cidr_mask=26, map_public_ip_on_launch=False)],
            flow_logs={"all": ec2.FlowLogOptions(destination=ec2.FlowLogDestination.to_cloud_watch_logs(flow))})
    stack.sg = ec2.SecurityGroup(stack, "SimulatorSg", vpc=stack.vpc, allow_all_outbound=False,
                                 security_group_name=f"{prefix}-simulators",
                                 description="Simulator and Greengrass tasks: no inbound; outbound TLS and MQTT only")
    stack.sg.add_egress_rule(ec2.Peer.any_ipv4(), ec2.Port.tcp(443), "HTTPS to AWS endpoints")
    stack.sg.add_egress_rule(ec2.Peer.any_ipv4(), ec2.Port.tcp(8883), "MQTT over TLS to AWS IoT Core")
    # Found in testing: the Greengrass nucleus reaches its data plane (greengrass-ats.iot.<region>) on 8443.
    stack.sg.add_egress_rule(ec2.Peer.any_ipv4(), ec2.Port.tcp(8443), "Greengrass data plane (TLS)")
    stack.cluster = ecs.Cluster(stack, "Cluster", cluster_name=f"{prefix}-sim", vpc=stack.vpc,
                                container_insights_v2=ecs.ContainerInsights.ENABLED)

    rp = removal(d)
    destroy = d["removal_policy"] == "destroy"
    stack.repos = {
        k: ecr.Repository(stack, f"{k.title()}Repo", repository_name=f"{prefix}-{k}", image_scan_on_push=True,
                          image_tag_mutability=ecr.TagMutability.IMMUTABLE, removal_policy=rp,
                          empty_on_delete=destroy,
                          lifecycle_rules=[ecr.LifecycleRule(max_image_count=5)])
        for k in ("simulator", "greengrass")}

    src = assets.Asset(stack, "ImageSource", path=image_src_dir)
    stack.image_tag = src.asset_hash[:32]
    gg = d["greengrass"]
    cb_logs = log_group(stack, "ImageBuildLogs", f"/aws/codebuild/{prefix}-images", d)
    stack.image_build = codebuild.Project(
        stack, "ImageBuild", project_name=f"{prefix}-images",
        description="Builds the arm64 simulator and Greengrass images",
        source=codebuild.Source.s3(bucket=src.bucket, path=src.s3_object_key),
        build_spec=codebuild.BuildSpec.from_source_filename("containers/buildspec.yml"),
        environment=codebuild.BuildEnvironment(
            build_image=codebuild.LinuxArmBuildImage.from_code_build_image_id(CODEBUILD_IMAGE),
            compute_type=codebuild.ComputeType.SMALL, privileged=True),
        environment_variables={k: codebuild.BuildEnvironmentVariable(value=v) for k, v in {
            "IMAGE_TAG": stack.image_tag,
            "SIM_REPO": stack.repos["simulator"].repository_uri,
            "GG_REPO": stack.repos["greengrass"].repository_uri,
            "NUCLEUS_VERSION": gg["nucleus_version"], "NUCLEUS_SHA256": gg["nucleus_sha256"],
            "GG_DOCKER_COMMIT": gg["docker_repo_commit"]}.items()},
        logging=codebuild.LoggingOptions(cloud_watch=codebuild.CloudWatchLoggingOptions(log_group=cb_logs)),
        timeout=Duration.minutes(45))
    for repo in stack.repos.values():
        repo.grant_pull_push(stack.image_build)

    stack.task_defs = {}
    # Simulator: reads the claim secret, writes reports under loadtest/.
    sim_role = iam.Role(stack, "SimulatorTaskRole", role_name=f"{prefix}-task-simulator",
                        assumed_by=iam.ServicePrincipal("ecs-tasks.amazonaws.com"))
    stack.claim_secret.grant_read(sim_role)
    sim_role.add_to_policy(iam.PolicyStatement(actions=["s3:PutObject"],
                                               resources=[f"{stack.bucket.bucket_arn}/loadtest/*"]))
    gg_role = iam.Role(stack, "GreengrassTaskRole", role_name=f"{prefix}-task-greengrass",
                       assumed_by=iam.ServicePrincipal("ecs-tasks.amazonaws.com"))
    for s in stack.gg_secrets:
        s.grant_read(gg_role)
    stack.task_roles = {"simulator": sim_role, "greengrass": gg_role}

    common_env = {"PREFIX": prefix, "AWS_REGION": Aws.REGION, "AWS_DEFAULT_REGION": Aws.REGION}
    specs = {
        "simulator": (d["simulator_task"], sim_role, {
            **common_env, "PROVISIONING_TEMPLATE": stack.template_name, "CLAIM_SECRET_ID": f"{prefix}/claim",
            "REPORT_BUCKET": stack.bucket.bucket_name}),
        "greengrass": (d["greengrass_task"], gg_role, {
            **common_env, "ROLE_ALIAS": stack.role_alias_name, "GGC_ROOT_PATH": "/greengrass/v2",
            "PROVISION": "false", "COMPONENT_DEFAULT_USER": "ggc_user:ggc_group", "INIT_CONFIG": "/tmp/config/config.yaml"}),  # noqa: S108 - path inside the container
    }
    for kind, (size, role, env) in specs.items():
        td = ecs.FargateTaskDefinition(
            stack, f"{kind.title()}Task", family=f"{prefix}-{kind}", cpu=size["cpu"], memory_limit_mib=size["memory_mb"],
            task_role=role, runtime_platform=ecs.RuntimePlatform(
                cpu_architecture=ecs.CpuArchitecture.ARM64, operating_system_family=ecs.OperatingSystemFamily.LINUX))
        lg = log_group(stack, f"{kind.title()}TaskLogs", f"/{prefix}/ecs/{kind}", d)
        td.add_container(kind, image=ecs.ContainerImage.from_ecr_repository(stack.repos[kind], stack.image_tag),
                         logging=ecs.LogDrivers.aws_logs(stream_prefix=kind, log_group=lg), environment=env,
                         essential=True)
        stack.task_defs[kind] = td
