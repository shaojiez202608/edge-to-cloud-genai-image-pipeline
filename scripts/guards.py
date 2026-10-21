"""Helpers shared by the scripts that touch AWS: output masking, paid-command guard, stack outputs."""

from __future__ import annotations

import json
import os
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
_ACCOUNT = re.compile(r"(?<!\d)\d{12}(?!\d)")


def mask_text(text: str) -> str:
    """Replace 12-digit numbers (account ids) before anything is printed or written."""
    return _ACCOUNT.sub("111122223333", text)


def mask(obj):
    return json.loads(mask_text(json.dumps(obj, default=str)))


def require_paid(what: str) -> None:
    if os.environ.get("ALLOW_PAID") != "1":
        sys.exit(f"{what} uses paid AWS services (Bedrock, Fargate, CodeBuild). Re-run with ALLOW_PAID=1.")


def load_params(path: str | None) -> dict:
    p = Path(path) if path else ROOT / "config" / "sandbox.json"
    if not p.is_file():
        p = ROOT / "config" / "sandbox.example.json"
    return json.loads(p.read_text())


def outputs(session, stack: str) -> dict:
    cfn = session.client("cloudformation")
    st = cfn.describe_stacks(StackName=stack)["Stacks"][0]
    return {o["OutputKey"]: o["OutputValue"] for o in st.get("Outputs", [])}


def write_report(path: str, report: dict) -> None:
    out = ROOT / path
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(mask_text(json.dumps(report, indent=1, ensure_ascii=False, default=str)))
    print(f"report: {path}")
