"""AWS IoT rule definitions as data: rendered to IoT SQL for CDK, interpreted by the local rules
engine. Every device rule copies the device identity from the topic *and* requires it to equal the
authenticated client id, so a payload can never name another device.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from typing import Any

from . import naming

SQL_VERSION = "2016-03-23"
_TOPIC_FILTER_RE = re.compile(r"^[A-Za-z0-9$_+#/-]{1,256}$")
_PREFIX_RE = re.compile(r"^[a-z][a-z0-9-]{1,30}$")
_ALIAS_RE = re.compile(r"^[a-z][a-z0-9_]{0,40}$")


@dataclass(frozen=True)
class Expr:
    kind: str  # "topic" | "clientid" | "field" | "timestamp"
    arg: Any = None

    def sql(self) -> str:
        if self.kind == "topic":
            return f"topic({int(self.arg)})"
        if self.kind == "clientid":
            return "clientid()"
        if self.kind == "timestamp":
            return "timestamp()"
        if self.kind == "field":
            if not str(self.arg).replace("_", "").isalnum():
                raise ValueError("field names are alphanumeric")
            return str(self.arg)
        raise ValueError(self.kind)

    def evaluate(self, topic: str, client_id: str, payload: dict, now_ms: int) -> Any:
        if self.kind == "topic":
            parts = topic.split("/")
            return parts[self.arg - 1] if len(parts) >= self.arg else None
        if self.kind == "clientid":
            return client_id
        if self.kind == "timestamp":
            return now_ms
        return payload.get(self.arg)


@dataclass(frozen=True)
class Rule:
    name: str  # logical name; deployed rule name is <prefix_underscored>_<name>
    topic_filter: str
    select: tuple[tuple[Expr, str], ...]
    action: str  # "sqs:ingest" | "sqs:telemetry" | "lambda:upload_url" | "dynamodb:presence"
    device_scoped: bool = True  # adds WHERE topic(2) = clientid()
    client_prefix: str | None = None  # adds WHERE startswith(<expr>, '<prefix>-')
    prefix_expr: Expr = field(default_factory=lambda: Expr("topic", 2))

    def sql(self) -> str:
        # Every fragment is whitelisted before it is placed in the statement (the SQL is IoT rule SQL built
        # from the fixed definitions in this module, never from device or user input).
        if not _TOPIC_FILTER_RE.match(self.topic_filter):
            raise ValueError("topic filter outside the allowed character set")
        if self.client_prefix is not None and not _PREFIX_RE.match(self.client_prefix):
            raise ValueError("client prefix outside the allowed character set")
        if not all(_ALIAS_RE.match(alias) for _, alias in self.select):
            raise ValueError("alias outside the allowed character set")
        cols = ", ".join(f"{e.sql()} AS {alias}" for e, alias in self.select)
        where = []
        if self.device_scoped:
            where.append("topic(2) = clientid()")
        if self.client_prefix:
            where.append(f"startswith({self.prefix_expr.sql()}, '{self.client_prefix}-')")
        sql = f"SELECT {cols} FROM '{self.topic_filter}'"  # noqa: S608  # nosec B608 - IoT rule SQL from whitelisted fixed fragments (see checks above)
        return sql + (" WHERE " + " AND ".join(where) if where else "")

    def apply(self, topic: str, client_id: str, payload_bytes: bytes, now_ms: int) -> dict | None:
        """Local interpretation: returns the rule output, or None when the rule does not fire."""
        if not topic_matches(self.topic_filter, topic):
            return None
        try:
            payload = json.loads(payload_bytes or b"{}")
        except (ValueError, UnicodeDecodeError):
            payload = {}
        if not isinstance(payload, dict):
            payload = {}
        if self.device_scoped and Expr("topic", 2).evaluate(topic, client_id, payload, now_ms) != client_id:
            return None
        if self.client_prefix:
            v = self.prefix_expr.evaluate(topic, client_id, payload, now_ms)
            if not isinstance(v, str) or not v.startswith(f"{self.client_prefix}-"):
                return None
        out = {}
        for e, alias in self.select:
            v = e.evaluate(topic, client_id, payload, now_ms)
            if v is not None:  # IoT SQL omits undefined values
                out[alias] = v
        return out


def topic_matches(topic_filter: str, topic: str) -> bool:
    f, t = topic_filter.split("/"), topic.split("/")
    for i, part in enumerate(f):
        if part == "#":
            return True
        if i >= len(t) or (part != "+" and part != t[i]):
            return False
    return len(f) == len(t)


def device_rules(prefix: str) -> list[Rule]:
    T, F = Expr("topic", 2), Expr  # noqa: N806
    root = naming.TOPIC_ROOT
    return [
        Rule("heartbeat", f"{root}/+/{naming.HEARTBEAT}",
             ((T, "thing_name"), (F("field", "seq"), "seq"), (F("field", "run_id"), "run_id"),
              (F("field", "sent_ms"), "sent_ms"), (F("timestamp"), "received_ms")),
             "sqs:telemetry", client_prefix=prefix),
        Rule("upload_request", f"{root}/+/{naming.UPLOAD_REQUEST}",
             ((T, "thing_name"), (F("field", "request_id"), "request_id"),
              (F("field", "content_type"), "content_type"), (F("field", "size"), "size"),
              (F("field", "sha256"), "sha256")),
             "lambda:upload_url", client_prefix=prefix),
        Rule("image_uploaded", f"{root}/+/{naming.IMAGE_UPLOADED}",
             ((T, "thing_name"), (F("field", "image_id"), "image_id"), (F("field", "capture_ms"), "capture_ms"),
              (F("timestamp"), "received_ms")),
             "sqs:ingest", client_prefix=prefix),
        Rule("presence", "$aws/events/presence/+/+",
             ((F("field", "clientId"), "thing_name"), (F("field", "eventType"), "last_event"),
              (F("field", "timestamp"), "event_ms"), (F("field", "disconnectReason"), "disconnect_reason")),
             "dynamodb:presence", device_scoped=False, client_prefix=prefix,
             prefix_expr=Expr("field", "clientId")),
    ]


def deployed_rule_name(prefix: str, rule: Rule) -> str:
    # IoT rule names allow [a-zA-Z0-9_] only.
    return f"{prefix.replace('-', '_')}_{rule.name}"
