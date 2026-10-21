"""Model clients. ``BedrockModel`` calls the Converse API with the image and one forced tool;
``MockModel`` stands in for local mode and tests.

Errors are split into transient (retried by Step Functions) and permanent (fallback). The raw model
answer is returned only to the validator; it is never logged or stored.
"""

from __future__ import annotations

import io
import math
from dataclasses import dataclass
from typing import Any, Protocol

from PIL import Image

from . import markers, synthetic
from .config import PipelineConfig
from .prompts import TOOL_NAME, USER_PROMPT, system_prompt, tool_config


class ModelTransientError(Exception):
    """Throttling or service errors: safe to retry."""


class ModelPermanentError(Exception):
    """Request rejected (validation, access, model not available): take the fallback path."""

    def __init__(self, code: str):
        super().__init__(code)
        self.code = code


@dataclass
class ModelAnswer:
    tool_input: dict | None  # None when the model did not call the tool
    stop_reason: str
    input_tokens: int = 0
    output_tokens: int = 0


class Model(Protocol):
    def analyze(self, jpeg: bytes) -> ModelAnswer: ...


TRANSIENT_CODES = {"ThrottlingException", "ServiceUnavailableException", "ModelNotReadyException",
                   "InternalServerException", "ModelTimeoutException", "ServiceQuotaExceededException"}


class BedrockModel:
    def __init__(self, cfg: PipelineConfig, client: Any):
        self.cfg = cfg
        self.client = client

    def analyze(self, jpeg: bytes) -> ModelAnswer:
        from botocore.exceptions import BotoCoreError, ClientError

        try:
            resp = self.client.converse(
                modelId=self.cfg.model_id,
                system=[{"text": system_prompt(self.cfg)}],
                messages=[{"role": "user", "content": [
                    {"image": {"format": "jpeg", "source": {"bytes": jpeg}}},
                    {"text": USER_PROMPT},
                ]}],
                toolConfig=tool_config(self.cfg),
                inferenceConfig={"maxTokens": self.cfg.max_tokens, "temperature": self.cfg.temperature},
            )
        except ClientError as exc:
            code = exc.response.get("Error", {}).get("Code", "ClientError")
            if code in TRANSIENT_CODES:
                raise ModelTransientError(code) from None
            raise ModelPermanentError(code) from None
        except BotoCoreError as exc:
            raise ModelTransientError(type(exc).__name__) from None
        usage = resp.get("usage", {})
        tool_input = None
        for block in resp.get("output", {}).get("message", {}).get("content", []):
            tu = block.get("toolUse")
            if tu and tu.get("name") == TOOL_NAME:
                tool_input = tu.get("input")
                break
        return ModelAnswer(tool_input, resp.get("stopReason", "unknown"),
                           usage.get("inputTokens", 0), usage.get("outputTokens", 0))


class MockModel:
    """Deterministic stand-in.

    mode="robust": behaves like a model that follows the system prompt (ignores scene text, reports
    instruction text). mode="naive": returns what an *obeying* model would return for the synthetic
    injection cases. Images without a fixture marker get an answer from pixel analysis.
    ``fail`` injects errors for tests: "transient" or "permanent".
    """

    def __init__(self, cfg: PipelineConfig, mode: str = "robust", fail: str | None = None):
        if mode not in ("robust", "naive"):
            raise ValueError(mode)
        self.cfg, self.mode, self.fail = cfg, mode, fail
        self.calls = 0

    def analyze(self, jpeg: bytes) -> ModelAnswer:
        self.calls += 1
        if self.fail == "transient":
            raise ModelTransientError("ThrottlingException")
        if self.fail == "permanent":
            raise ModelPermanentError("ValidationException")
        img = Image.open(io.BytesIO(jpeg)).convert("RGB")
        case = synthetic.BY_CODE.get(markers.read(img) or -1)
        if case is not None:
            out = self._for_case(case)
        else:
            out = self._from_pixels(img)
        if out is not None and out.get("__no_tool__"):
            return ModelAnswer(None, "end_turn", 900, 40)
        out = _with_box(out, img) if out is not None else None
        return ModelAnswer(out, "tool_use", 900, 160)

    def _for_case(self, case: synthetic.Case) -> dict | None:
        naive = dict(case.naive_output or {})
        if self.mode == "naive":
            return naive
        # robust: the model ignores scene text; reports it when it looks like instructions
        base = synthetic._benign(case.category, f"{case.shape.capitalize()} product photo",
                                 quality="too_dark" if case.dark else "good", background=case.background)
        if case.kind == "injection" and case.scene_text:
            base["visible_text"] = "instructions_or_long_text"
        elif case.scene_text:
            base["visible_text"] = "label_or_logo"
        return base

    def _from_pixels(self, img: Image.Image) -> dict:
        return synthetic._benign("other", "Product photo")


def _with_box(out: dict, img: Image.Image) -> dict:
    """Replace the placeholder box by the bounding box of non-background pixels (keeps any box an
    injected answer set on purpose, e.g. out-of-range or NaN values)."""
    box = out.get("subject_box")
    if box != {"x": 0.25, "y": 0.25, "w": 0.5, "h": 0.5}:
        return out
    small = img.resize((100, 100))
    px = small.load()
    xs, ys = [], []
    for y in range(8, 60):  # skip the marker strip and the table surface
        for x in range(100):
            r, g, b = px[x, y]
            if abs(r - 245) + abs(g - 245) + abs(b - 242) > 60:
                xs.append(x)
                ys.append(y)
    if xs:
        x0, x1, y0, y1 = min(xs) / 100, (max(xs) + 1) / 100, min(ys) / 100, (max(ys) + 1) / 100
        if not any(math.isnan(v) for v in (x0, x1, y0, y1)) and x1 - x0 >= 0.05 and y1 - y0 >= 0.05:
            out = {**out, "subject_box": {"x": x0, "y": y0, "w": round(x1 - x0, 3), "h": round(y1 - y0, 3)}}
    return out
