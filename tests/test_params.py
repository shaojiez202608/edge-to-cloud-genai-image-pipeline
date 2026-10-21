import copy
import json
from pathlib import Path

import pytest
from edge_infra import params

ROOT = Path(__file__).resolve().parents[1]
BASE = json.loads((ROOT / "config" / "sandbox.example.json").read_text())


def mutated(**changes):
    p = copy.deepcopy(BASE)
    for path, value in changes.items():
        d = p
        keys = path.split("__")
        for k in keys[:-1]:
            d = d[k]
        d[keys[-1]] = value
    return p


def test_examples_valid():
    for f in ("sandbox", "handover"):
        params.validate(json.loads((ROOT / "config" / f"{f}.example.json").read_text()))


@pytest.mark.parametrize("change,needle", [
    ({"deploy__resource_prefix": "Bad_Prefix"}, "resource_prefix"),
    ({"deploy__stack_name": "other-stack"}, "must start with"),
    ({"deploy__availability_zones": ["us-west-2a", "us-west-2b"]}, "belong to deploy.region"),
    ({"deploy__removal_policy": "keep"}, "removal_policy"),
    ({"deploy__log_retention_days": 4}, "log_retention_days"),
    ({"deploy__data_retention_days__work": 10}, "work must not exceed uploads"),
    ({"deploy__model_regions": ["us-west-2"]}, "include deploy.region"),
    ({"deploy__simulator_task": {"cpu": 256, "memory_mb": 8192}}, "Fargate task sizes"),
    ({"deploy__greengrass__nucleus_sha256": "abc"}, "nucleus_sha256"),
    ({"deploy__budget": {"enabled": True}}, "monthly_limit_usd"),
    ({"deploy__alarm_email": "nope"}, "alarm_email"),
    ({"deploy__tags": {"Project": "x"}}, "Purpose"),
    ({"deploy__removal_policy": "retain"}, "contradicts"),
    ({"deploy__tags__Owner": "123456789012"}, "12-digit"),
    ({"pipeline__model_mode": "x"}, "pipeline"),
])
def test_invalid(change, needle):
    with pytest.raises(params.ParamsInvalid) as e:
        params.validate(mutated(**change))
    assert needle in str(e.value)


def test_operator_arns():
    assert params.operator_arns("arn:aws:iam::111122223333:role/Ops") == ["arn:aws:iam::111122223333:role/Ops"]
    with pytest.raises(params.ParamsInvalid):
        params.operator_arns("arn:aws:sts::111122223333:assumed-role/Ops/session")
