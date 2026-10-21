"""Synth-time validation of the parameter file (config/*.json). Every problem is listed at once and
``cdk synth`` fails before any construct is created. The ``pipeline`` section is validated by the
runtime code itself (PipelineConfig.from_dict).
"""

from __future__ import annotations

import json
import re
from typing import Any

PREFIX_RE = re.compile(r"^[a-z][a-z0-9-]{1,14}[a-z0-9]$")
STACK_RE = re.compile(r"^[A-Za-z][A-Za-z0-9-]{0,127}$")
REGION_RE = re.compile(r"^[a-z]{2}(-gov)?-[a-z]+-\d$")
AZ_RE = re.compile(r"^[a-z]{2}(-gov)?-[a-z]+-\d[a-z]$")
TAG_RE = re.compile(r"^[A-Za-z0-9 _.:/=+@-]{1,128}$")
ACCOUNT_RE = re.compile(r"(?<!\d)\d{12}(?!\d)")
SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
SHA1_RE = re.compile(r"^[0-9a-f]{40}$")
VERSION_RE = re.compile(r"^\d+\.\d+\.\d+$")
EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[a-z]{2,}$")
RETENTION_DAYS = (1, 3, 5, 7, 14, 30, 60, 90, 180, 365)
REQUIRED_TAGS = ("Project", "Purpose", "Teardown")
DATA_PREFIXES = ("uploads", "work", "results", "shelf", "loadtest")
TASK_SIZES = {256: (512, 2048), 512: (1024, 4096), 1024: (2048, 8192), 2048: (4096, 16384), 4096: (8192, 30720)}


class ParamsInvalid(ValueError):
    pass


def _int(d: dict, path: str, key: str, lo: int, hi: int, errors: list[str]) -> int | None:
    v = d.get(key)
    if not isinstance(v, int) or isinstance(v, bool) or not lo <= v <= hi:
        errors.append(f"{path}.{key} must be an integer in [{lo}, {hi}] (got {v!r})")
        return None
    return v


def validate(params: dict[str, Any]) -> None:
    errors: list[str] = []
    if not isinstance(params, dict) or not isinstance(params.get("deploy"), dict) \
            or not isinstance(params.get("pipeline"), dict):
        raise ParamsInvalid("parameter file needs 'deploy' and 'pipeline' objects")
    d = params["deploy"]
    if ACCOUNT_RE.search(json.dumps(params)):
        errors.append("parameter file contains a 12-digit number; account ids come from the environment, never the file")
    if not isinstance(d.get("stack_name"), str) or not STACK_RE.match(d["stack_name"]):
        errors.append("deploy.stack_name must match [A-Za-z][A-Za-z0-9-]{0,127}")
    prefix = d.get("resource_prefix")
    if not isinstance(prefix, str) or not PREFIX_RE.match(prefix):
        errors.append("deploy.resource_prefix must be 3-16 chars of a-z, 0-9 and '-' (thing names and IoT rule names derive from it)")
    elif isinstance(d.get("stack_name"), str) and not d["stack_name"].startswith(prefix):
        errors.append("deploy.stack_name must start with deploy.resource_prefix (cleanup matches on it)")
    region = d.get("region")
    if not isinstance(region, str) or not REGION_RE.match(region):
        errors.append("deploy.region must look like us-east-1")
    azs = d.get("availability_zones")
    if not isinstance(azs, list) or len(azs) != 2 or not all(isinstance(a, str) and AZ_RE.match(a) for a in azs):
        errors.append("deploy.availability_zones must list exactly two AZ names (fixed so synth needs no AWS lookup)")
    elif isinstance(region, str) and not all(a.startswith(region) for a in azs):
        errors.append("deploy.availability_zones must belong to deploy.region")
    if d.get("removal_policy") not in ("destroy", "retain"):
        errors.append("deploy.removal_policy must be 'destroy' or 'retain'")
    if d.get("log_retention_days") not in RETENTION_DAYS:
        errors.append(f"deploy.log_retention_days must be one of {list(RETENTION_DAYS)}")
    ret = d.get("data_retention_days")
    if not isinstance(ret, dict) or set(ret) != set(DATA_PREFIXES):
        errors.append(f"deploy.data_retention_days needs exactly the keys {list(DATA_PREFIXES)}")
    else:
        for k in DATA_PREFIXES:
            _int(ret, "deploy.data_retention_days", k, 1, 3650, errors)
        if all(isinstance(ret.get(k), int) for k in ("uploads", "work")) and ret["work"] > ret["uploads"]:
            errors.append("deploy.data_retention_days.work must not exceed uploads")
    _int(d, "deploy", "max_fleet_size", 1, 10000, errors)
    _int(d, "deploy", "dispatch_max_concurrency", 2, 1000, errors)
    _int(d, "deploy", "lambda_memory_mb", 512, 10240, errors)
    _int(d, "deploy", "analyze_timeout_s", 30, 900, errors)
    _int(d, "deploy", "telemetry_retention_s", 60, 1209600, errors)
    _int(d, "deploy", "greengrass_core_count", 0, 10, errors)
    _int(d, "deploy", "bedrock_invocations_alarm_per_5min", 1, 1_000_000, errors)
    mr = d.get("model_regions")
    if not isinstance(mr, list) or not mr or not all(isinstance(r, str) and REGION_RE.match(r) for r in mr):
        errors.append("deploy.model_regions must list the regions the inference profile routes to")
    elif isinstance(region, str) and region not in mr:
        errors.append("deploy.model_regions must include deploy.region")
    for key in ("simulator_task", "greengrass_task"):
        t = d.get(key)
        if not isinstance(t, dict) or t.get("cpu") not in TASK_SIZES:
            errors.append(f"deploy.{key}.cpu must be one of {sorted(TASK_SIZES)}")
        else:
            lo, hi = TASK_SIZES[t["cpu"]]
            if not isinstance(t.get("memory_mb"), int) or not lo <= t["memory_mb"] <= hi:
                errors.append(f"deploy.{key}.memory_mb must be in [{lo}, {hi}] for cpu {t['cpu']} (Fargate task sizes)")
    gg = d.get("greengrass")
    if not isinstance(gg, dict) or not VERSION_RE.match(str(gg.get("nucleus_version", ""))) \
            or not SHA256_RE.match(str(gg.get("nucleus_sha256", ""))) \
            or not SHA1_RE.match(str(gg.get("docker_repo_commit", ""))):
        errors.append("deploy.greengrass needs nucleus_version (x.y.z), nucleus_sha256 and docker_repo_commit (40-hex)")
    b = d.get("budget")
    if not isinstance(b, dict) or not isinstance(b.get("enabled"), bool):
        errors.append("deploy.budget.enabled must be true or false")
    elif b["enabled"] and (not isinstance(b.get("monthly_limit_usd"), int | float) or isinstance(b.get("monthly_limit_usd"), bool)
                           or not b["monthly_limit_usd"] > 0):
        errors.append("deploy.budget.monthly_limit_usd must be a positive number when the budget is enabled")
    email = d.get("alarm_email")
    if email is not None and (not isinstance(email, str) or not EMAIL_RE.match(email)):
        errors.append("deploy.alarm_email must be null or an e-mail address")
    tags = d.get("tags", {})
    if not isinstance(tags, dict) or not all(t in tags for t in REQUIRED_TAGS) or not all(
            isinstance(k, str) and isinstance(v, str) and TAG_RE.match(k) and TAG_RE.match(v) for k, v in tags.items()):
        errors.append(f"deploy.tags must be string pairs including {list(REQUIRED_TAGS)}")
    # Cross-field rules
    if d.get("removal_policy") == "retain" and isinstance(tags, dict) and tags.get("Teardown") == "after-test":
        errors.append("deploy.removal_policy 'retain' contradicts tag Teardown=after-test; use another Teardown value")
    try:
        from edge_pipeline.config import PipelineConfig

        cfg = PipelineConfig.from_dict(params["pipeline"])
        if cfg.model_mode == "bedrock" and isinstance(mr, list) and cfg.model_id.split(".")[0] not in (
                "us", "eu", "apac", "global", "jp", "au", "ca") and len(mr) != 1:
            errors.append("deploy.model_regions must be [deploy.region] for an in-region model id")
    except (ValueError, TypeError) as exc:
        errors.append(f"pipeline: {exc}")
    if errors:
        raise ParamsInvalid("invalid parameter file:\n  - " + "\n  - ".join(errors))


OPERATOR_ARN_RE = re.compile(r"^arn:aws[a-z-]*:iam::\d{12}:(user|role)/[A-Za-z0-9+=,.@_/-]{1,512}$")


def operator_arns(raw: str | list | None) -> list[str]:
    """``-c operator_principal_arns=a,b``: IAM user/role ARNs (not STS session ARNs) that may use the
    data bucket besides the stack's own roles."""
    if not raw:
        return []
    items = raw if isinstance(raw, list) else [x.strip() for x in str(raw).split(",") if x.strip()]
    bad = [x for x in items if not OPERATOR_ARN_RE.match(x)]
    if bad:
        raise ParamsInvalid("operator_principal_arns must be IAM user or role ARNs (arn:aws:iam::111122223333:role/NAME)")
    return items


def gg_core_names(prefix: str, count: int) -> list[str]:
    return [f"{prefix}-gg-{i:02d}" for i in range(1, count + 1)]


VPC_RE = re.compile(r"^vpc-[0-9a-f]{8,17}$")
SUBNET_RE = re.compile(r"^subnet-[0-9a-f]{8,17}$")


def existing_network(vpc_id: str | None, subnet_ids: str | None) -> tuple[str, list[str]] | None:
    """``-c existing_vpc_id=vpc-... -c existing_subnet_ids=subnet-a,subnet-b`` (two public subnets in the
    configured AZs, same order). Ids are account-specific, so they are never stored in the parameter file."""
    if not vpc_id and not subnet_ids:
        return None
    subnets = [x.strip() for x in str(subnet_ids or "").split(",") if x.strip()]
    if not VPC_RE.match(str(vpc_id or "")) or len(subnets) != 2 or not all(SUBNET_RE.match(x) for x in subnets):
        raise ParamsInvalid("existing_vpc_id needs a vpc id and existing_subnet_ids exactly two subnet ids")
    return str(vpc_id), subnets
