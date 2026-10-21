"""CDK app entry. Parameters come from one file (default config/sandbox.json, created from the
example by `make init`); the account comes from the environment, never from the file. Use
`make deploy` (README) so the preflight and the destructive-change guard run first.

Context: params=<file>  operator_principal_arns=<arn,arn>  code_dir=<dir>  image_src_dir=<dir>
"""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path

import aws_cdk as cdk
from cdk_nag import AwsSolutionsChecks

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(ROOT / "src"))
from edge_infra import params as params_check  # noqa: E402
from edge_infra.stack import EdgePipelineStack  # noqa: E402

app = cdk.App()
params_path = ROOT / (app.node.try_get_context("params") or "config/sandbox.json")
if not params_path.is_file():
    sys.exit(f"{params_path.name} not found. Run `make init` or pass -c params=<file>.")
params = json.loads(params_path.read_text(encoding="utf-8"))
params_check.validate(params)
deploy = params["deploy"]
code_dir = ROOT / (app.node.try_get_context("code_dir") or "build/lambda")
image_src = ROOT / (app.node.try_get_context("image_src_dir") or "build/image-src")
for p in (code_dir, image_src):
    if not p.is_dir():
        sys.exit(f"{p.relative_to(ROOT)} missing: run scripts/build_bundle.sh first")

EdgePipelineStack(
    app, deploy["stack_name"], deploy=deploy, pipeline=params["pipeline"], code_dir=str(code_dir),
    image_src_dir=str(image_src),
    operator_arns=params_check.operator_arns(app.node.try_get_context("operator_principal_arns")),
    existing_network=params_check.existing_network(app.node.try_get_context("existing_vpc_id"),
                                                   app.node.try_get_context("existing_subnet_ids")),
    env=cdk.Environment(account=os.environ.get("CDK_DEFAULT_ACCOUNT") or None, region=deploy["region"]),
    description="Prototype: edge-to-cloud generative image pipeline (simulated devices, AWS IoT Core, Amazon Bedrock)",
)
for k, v in deploy["tags"].items():
    cdk.Tags.of(app).add(k, v)
cdk.Validations.of(app).add_plugins(AwsSolutionsChecks(app))
app.synth()
