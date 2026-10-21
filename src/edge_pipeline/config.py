"""Pipeline configuration (the ``pipeline`` section of the parameter file). Validated by the same
code at synth time (infra/edge_infra/params.py) and at Lambda cold start (from an environment
variable), so a value that would fail at run time already fails ``cdk synth``.
"""

from __future__ import annotations

import json
import os
import re
from dataclasses import asdict, dataclass, field

MODEL_ID_RE = re.compile(r"^(?:(?:us|eu|apac|global|jp|au|ca)\.)?[a-z0-9-]+\.[a-z0-9.:-]+$")
CATEGORIES = ("apparel", "footwear", "bag", "home", "kitchen", "toy", "electronics_accessory", "beauty", "other")
LOCALES = ("en-US", "zh-CN")


@dataclass(frozen=True)
class PipelineConfig:
    model_mode: str = "bedrock"  # "bedrock" | "mock"
    model_id: str = "us.amazon.nova-2-lite-v1:0"
    max_tokens: int = 1024
    temperature: float = 0.0
    copy_locale: str = "en-US"
    allowed_content_types: tuple[str, ...] = ("image/jpeg", "image/png")
    max_upload_bytes: int = 4_000_000
    min_upload_bytes: int = 64
    max_image_pixels: int = 16_000_000
    max_image_side: int = 4096
    min_image_side: int = 64
    model_image_side: int = 1024  # longest side of the sanitized copy sent to the model
    shelf_image_side: int = 1024
    title_max_chars: int = 80
    bullet_max_chars: int = 120
    max_bullets: int = 5
    max_tags: int = 8
    presign_expiry_s: int = 300
    uploads_per_device_per_hour: int = 60
    guardrail_enabled: bool = True
    shelf_manifest_max_items: int = 500
    categories: tuple[str, ...] = field(default=CATEGORIES)

    @classmethod
    def from_dict(cls, d: dict) -> PipelineConfig:
        if not isinstance(d, dict):
            raise ValueError("pipeline must be an object")
        known = set(cls.__dataclass_fields__)
        unknown = sorted(set(d) - known)
        if unknown:
            raise ValueError(f"unknown pipeline keys: {unknown}")
        values = dict(d)
        for k in ("allowed_content_types", "categories"):
            if k in values:
                if not isinstance(values[k], list | tuple):
                    raise ValueError(f"pipeline.{k} must be a list")
                values[k] = tuple(values[k])
        cfg = cls(**values)
        cfg.check()
        return cfg

    @classmethod
    def from_env(cls, name: str = "PIPELINE_CONFIG") -> PipelineConfig:
        raw = os.environ.get(name)
        return cls.from_dict(json.loads(raw)) if raw else cls()

    def to_json(self) -> str:
        return json.dumps(asdict(self), sort_keys=True)

    def check(self) -> None:
        errors: list[str] = []

        def rng(name: str, lo: float, hi: float, integer: bool = True) -> None:
            v = getattr(self, name)
            ok_type = isinstance(v, int) and not isinstance(v, bool) if integer else (
                isinstance(v, int | float) and not isinstance(v, bool))
            if not ok_type or not lo <= v <= hi:
                errors.append(f"pipeline.{name} must be {'an integer' if integer else 'a number'} in [{lo}, {hi}]")

        if self.model_mode not in ("bedrock", "mock"):
            errors.append("pipeline.model_mode must be 'bedrock' or 'mock'")
        if not isinstance(self.model_id, str) or not MODEL_ID_RE.match(self.model_id):
            errors.append("pipeline.model_id is not a Bedrock model or inference profile id")
        rng("max_tokens", 256, 4096)
        rng("temperature", 0.0, 1.0, integer=False)
        if self.copy_locale not in LOCALES:
            errors.append(f"pipeline.copy_locale must be one of {list(LOCALES)}")
        if not self.allowed_content_types or not set(self.allowed_content_types) <= {"image/jpeg", "image/png"}:
            errors.append("pipeline.allowed_content_types must be a non-empty subset of image/jpeg, image/png")
        # Bedrock image-size limits are documented per model; keep the upload cap in the same range and
        # always send a re-encoded, downscaled copy (model_image_side) rather than the original.
        rng("max_upload_bytes", 1024, 10_000_000)
        rng("min_upload_bytes", 1, 1024)
        rng("max_image_pixels", 1_000_000, 50_000_000)
        rng("max_image_side", 512, 8000)
        rng("min_image_side", 16, 512)
        rng("model_image_side", 256, 2048)
        rng("shelf_image_side", 256, 2048)
        rng("title_max_chars", 10, 200)
        rng("bullet_max_chars", 20, 300)
        rng("max_bullets", 1, 10)
        rng("max_tags", 0, 20)
        rng("presign_expiry_s", 30, 900)
        rng("uploads_per_device_per_hour", 1, 3600)
        rng("shelf_manifest_max_items", 10, 5000)
        if not isinstance(self.guardrail_enabled, bool):
            errors.append("pipeline.guardrail_enabled must be true or false")
        if not self.categories or "other" not in self.categories or not all(
                isinstance(c, str) and re.match(r"^[a-z_]{2,32}$", c) for c in self.categories):
            errors.append("pipeline.categories must be lower_snake names and include 'other'")
        if not errors and self.model_image_side > self.max_image_side:
            errors.append("pipeline.model_image_side must not exceed pipeline.max_image_side")
        if not errors and self.min_upload_bytes >= self.max_upload_bytes:
            errors.append("pipeline.min_upload_bytes must be below pipeline.max_upload_bytes")
        if errors:
            raise ValueError("; ".join(errors))
