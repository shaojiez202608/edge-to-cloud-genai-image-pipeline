"""Optional second layer: Amazon Bedrock Guardrails through the ApplyGuardrail API.

- ``check_image``: the sanitized image, source INPUT (image content filters, including the prompt
  attack category where the guardrail enables it for images).
- ``check_text``: the validated draft copy, source OUTPUT (content filters on text).
Only filter categories are returned (no text, no scores), so results and logs stay content-free.
Local mode has no guardrail (``NoGuardrail``): only the code checks run there.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Protocol


@dataclass
class GuardrailVerdict:
    intervened: bool
    categories: list[str] = field(default_factory=list)


class Guardrail(Protocol):
    def check_image(self, jpeg: bytes) -> GuardrailVerdict: ...
    def check_text(self, texts: list[str]) -> GuardrailVerdict: ...


class NoGuardrail:
    def check_image(self, jpeg: bytes) -> GuardrailVerdict:
        return GuardrailVerdict(False)

    def check_text(self, texts: list[str]) -> GuardrailVerdict:
        return GuardrailVerdict(False)


class GuardrailUnavailable(Exception):
    """ApplyGuardrail failed; the caller treats this as a transient error (retry, then fallback)."""


def _categories(resp: dict) -> list[str]:
    out = set()
    for a in resp.get("assessments", []):
        for f in a.get("contentPolicy", {}).get("filters", []):
            if f.get("action") == "BLOCKED" or f.get("detected"):
                out.add(f"content:{f.get('type', 'unknown')}")
        for t in a.get("topicPolicy", {}).get("topics", []):
            out.add(f"topic:{t.get('name', 'unknown')[:32]}")
        if a.get("wordPolicy", {}).get("customWords") or a.get("wordPolicy", {}).get("managedWordLists"):
            out.add("word")
        if a.get("sensitiveInformationPolicy", {}).get("piiEntities") or \
                a.get("sensitiveInformationPolicy", {}).get("regexes"):
            out.add("sensitive_information")
    return sorted(out)


class BedrockGuardrail:
    def __init__(self, client: Any, guardrail_id: str, version: str):
        self.client, self.guardrail_id, self.version = client, guardrail_id, version

    def _apply(self, source: str, content: list[dict]) -> GuardrailVerdict:
        from botocore.exceptions import BotoCoreError, ClientError

        try:
            resp = self.client.apply_guardrail(guardrailIdentifier=self.guardrail_id,
                                               guardrailVersion=self.version, source=source, content=content)
        except (ClientError, BotoCoreError) as exc:
            raise GuardrailUnavailable(type(exc).__name__) from None
        return GuardrailVerdict(resp.get("action") == "GUARDRAIL_INTERVENED", _categories(resp))

    def check_image(self, jpeg: bytes) -> GuardrailVerdict:
        return self._apply("INPUT", [{"image": {"format": "jpeg", "source": {"bytes": jpeg}}}])

    def check_text(self, texts: list[str]) -> GuardrailVerdict:
        return self._apply("OUTPUT", [{"text": {"text": "\n".join(texts)}}])
