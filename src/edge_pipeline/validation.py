"""Validation of model output. The model's answer is untrusted: it may have been steered by text in
the image. Everything that reaches the shelf passes the schema, numeric sanity checks and content
rules here; failures are reported as (field path, keyword) pairs only, never with the offending text,
so logs and results stay free of model text.

Decisions:
  accepted  schema and content rules pass, no soft flags
  review    passes, but the model reported instruction-like text or poor quality: the shelf entry gets
            neutral copy and status needs_review (the model's copy is not published)
  rejected  any hard failure: the pipeline takes the fallback path
"""

from __future__ import annotations

import json
import math
import re
import unicodedata
from dataclasses import dataclass, field
from typing import Any

from jsonschema import Draft202012Validator

from .config import PipelineConfig
from .prompts import output_schema

MAX_OUTPUT_BYTES = 8192
MIN_BOX_SIDE = 0.05
BOX_EPS = 0.01

# Content rules on free-text fields. Each pattern has a fixed keyword used in results and logs.
_RULES: list[tuple[str, re.Pattern[str]]] = [
    ("url", re.compile(r"(https?:|www\.|\b[a-z0-9-]{2,}\.(?:com|net|org|io|cn|xyz|top|shop|info|biz|ru|co)\b)", re.I)),
    ("email", re.compile(r"[\w.+-]+@[\w-]+\.[\w.]+")),
    ("phone", re.compile(r"\+?\d[\d\s().-]{7,}\d")),
    ("markup", re.compile(r"(<\s*[a-z/!]|\{\{|\}\}|\[[^\]]*\]\([^)]*\)|```|\$\{)", re.I)),
    ("instruction_like", re.compile(
        r"(ignore|disregard|override|forget)\b.{0,40}\b(instruction|rule|prompt|above|previous)|system prompt|"
        r"developer mode|jailbreak|you are now|as an ai|new instructions?|"
        r"忽略|无视|指令|提示词|系统提示|越狱", re.I)),
    ("identifier_leak", re.compile(
        r"(\b[a-z0-9]+-[a-z0-9]+-(?:sim|gg|claim)-[a-z0-9]+\b|\barn:aws|\bthing[ _-]?name\b|"
        r"\bdevice[ _-]?id\b|\bcertificate\b|\bclient[ _-]?id\b|设备编号|证书)", re.I)),
    ("price_or_promo", re.compile(r"([$€£¥]\s?\d|\d\s?(?:usd|eur|rmb|cny)\b|\bfree\b|\bdiscount\b|%\s?off\b|"
                                  r"免费|折扣|优惠)", re.I)),
]
_BAD_CATEGORIES = ("Cc", "Cf", "Co", "Cs")  # control, format (zero-width, bidi overrides), private use


@dataclass
class ValidationResult:
    decision: str  # accepted | review | rejected
    asset: dict | None
    issues: list[dict[str, str]] = field(default_factory=list)

    @property
    def codes(self) -> list[str]:
        return sorted({i["keyword"] for i in self.issues})


def _path(parts: Any) -> str:
    out = "$"
    for p in parts:
        out += f"[{p}]" if isinstance(p, int) else f".{p}"
    return out


def _walk_numbers(value: Any, path: list, issues: list[dict[str, str]]) -> None:
    if isinstance(value, float) and not math.isfinite(value):
        issues.append({"path": _path(path), "keyword": "not_finite"})
    elif isinstance(value, dict):
        for k, v in value.items():
            _walk_numbers(v, [*path, str(k)[:40]], issues)
    elif isinstance(value, list):
        for i, v in enumerate(value):
            _walk_numbers(v, [*path, i], issues)


def check_text(text: str) -> list[str]:
    """Keywords of the content rules a text violates (empty list = clean)."""
    found = []
    norm = unicodedata.normalize("NFKC", text)
    if any(unicodedata.category(c) in _BAD_CATEGORIES for c in norm if c not in "\n"):
        found.append("control_chars")
    for keyword, pattern in _RULES:
        if pattern.search(norm):
            found.append(keyword)
    return found


def validate_output(cfg: PipelineConfig, raw: Any) -> ValidationResult:
    issues: list[dict[str, str]] = []
    if raw is None:
        return ValidationResult("rejected", None, [{"path": "$", "keyword": "no_tool_call"}])
    if not isinstance(raw, dict):
        return ValidationResult("rejected", None, [{"path": "$", "keyword": "type"}])
    _walk_numbers(raw, [], issues)
    if issues:
        return ValidationResult("rejected", None, issues)
    try:
        size = len(json.dumps(raw, ensure_ascii=False, allow_nan=False).encode())
    except (TypeError, ValueError):
        return ValidationResult("rejected", None, [{"path": "$", "keyword": "not_serializable"}])
    if size > MAX_OUTPUT_BYTES:
        return ValidationResult("rejected", None, [{"path": "$", "keyword": "too_large"}])

    for err in sorted(Draft202012Validator(output_schema(cfg)).iter_errors(raw), key=lambda e: list(e.path)):
        issues.append({"path": _path(err.absolute_path), "keyword": str(err.validator)})
    if issues:
        return ValidationResult("rejected", None, issues[:20])

    box = raw["subject_box"]
    if box["x"] + box["w"] > 1 + BOX_EPS or box["y"] + box["h"] > 1 + BOX_EPS:
        issues.append({"path": "$.subject_box", "keyword": "box_out_of_bounds"})
    if box["w"] < MIN_BOX_SIDE or box["h"] < MIN_BOX_SIDE:
        issues.append({"path": "$.subject_box", "keyword": "box_too_small"})

    texts = [("$.title", raw["title"])] + [(f"$.bullets[{i}]", b) for i, b in enumerate(raw["bullets"])]
    texts += [(f"$.tags[{i}]", t) for i, t in enumerate(raw["tags"])]
    for path, text in texts:
        for kw in check_text(text):
            issues.append({"path": path, "keyword": kw})
        if not text.strip():
            issues.append({"path": path, "keyword": "blank"})
    if issues:
        return ValidationResult("rejected", None, issues[:20])

    asset = {
        "category": raw["category"],
        "background": raw["background"],
        "quality": raw["quality"],
        "visible_text": raw["visible_text"],
        "subject_box": {k: round(min(max(float(box[k]), 0.0), 1.0), 4) for k in ("x", "y", "w", "h")},
        "title": " ".join(raw["title"].split()),
        "bullets": [" ".join(b.split()) for b in raw["bullets"]],
        "tags": sorted(set(raw["tags"])),
    }
    soft = []
    if raw["visible_text"] == "instructions_or_long_text":
        soft.append({"path": "$.visible_text", "keyword": "instruction_text_reported"})
    if raw["quality"] != "good":
        soft.append({"path": "$.quality", "keyword": "quality_not_good"})
    return ValidationResult("review" if soft else "accepted", asset, soft)


def neutral_copy(cfg: PipelineConfig, category: str) -> dict:
    """Copy used when the model's copy is not published (review or fallback)."""
    label = category.replace("_", " ")
    if cfg.copy_locale == "zh-CN":
        return {"title": f"商品图片（{label}）", "bullets": ["文案待人工审核"]}
    return {"title": f"Product photo ({label})", "bullets": ["Copy pending manual review"]}
