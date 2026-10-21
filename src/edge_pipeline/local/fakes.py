"""In-memory stand-ins for S3 (including presigned POST checks and conditional writes), DynamoDB,
SQS, the IoT rules engine and the Step Functions workflow."""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import os
import threading
import time
from collections import deque
from pathlib import Path
from typing import Any

from .. import pipeline, rules
from ..image_checks import InputRejected
from ..model import ModelTransientError
from ..store import Conflict
from .broker import LocalBroker


class LocalObjectStore:
    """S3 stand-in. Presigned POST grants are HMAC-signed policies checked in ``post``."""

    def __init__(self, root: Path | None = None):
        self.objects: dict[str, tuple[bytes, str, str]] = {}
        self.root = root
        self.secret = os.urandom(16)
        self.lock = threading.Lock()

    def get(self, key: str) -> bytes:
        with self.lock:
            if key not in self.objects:
                raise KeyError("NoSuchKey")
            return self.objects[key][0]

    def get_versioned(self, key: str) -> tuple[bytes | None, str | None]:
        with self.lock:
            if key not in self.objects:
                return None, None
            data, _, etag = self.objects[key]
            return data, etag

    def put(self, key: str, data: bytes, content_type: str) -> None:
        with self.lock:
            self.objects[key] = (data, content_type, f'"{hashlib.md5(data, usedforsecurity=False).hexdigest()}"')
        if self.root is not None:
            p = self.root / key
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_bytes(data)

    def put_if(self, key: str, data: bytes, content_type: str, etag: str | None) -> None:
        with self.lock:
            cur = self.objects.get(key)
            if (etag is None and cur is not None) or (etag is not None and (cur is None or cur[2] != etag)):
                raise Conflict(key)
        self.put(key, data, content_type)

    def presigned_post(self, key: str, content_type: str, size: int, expiry_s: int) -> dict:
        pol = {"key": key, "content_type": content_type, "size": size, "exp": time.time() + expiry_s}
        raw = base64.b64encode(json.dumps(pol, sort_keys=True).encode()).decode()
        sig = hmac.new(self.secret, raw.encode(), hashlib.sha256).hexdigest()
        return {"url": "local://uploads", "fields": {"key": key, "Content-Type": content_type,
                                                    "policy": raw, "x-local-signature": sig}}

    def post(self, fields: dict, data: bytes) -> int:
        """Returns an HTTP-like status: 204 stored, 403 policy violation."""
        raw, sig = fields.get("policy", ""), fields.get("x-local-signature", "")
        if not hmac.compare_digest(hmac.new(self.secret, raw.encode(), hashlib.sha256).hexdigest(), sig):
            return 403
        pol = json.loads(base64.b64decode(raw))
        if (fields.get("key") != pol["key"] or fields.get("Content-Type") != pol["content_type"]
                or len(data) != pol["size"] or time.time() > pol["exp"]):
            return 403
        self.put(pol["key"], data, pol["content_type"])
        return 204


class LocalItems:
    def __init__(self):
        self.items: dict[tuple[str, str], dict] = {}
        self.lock = threading.Lock()

    def put_issued(self, thing: str, image_id: str, declared: dict, expires_ms: int) -> None:
        with self.lock:
            k = (thing, f"img#{image_id}")
            if k in self.items:
                raise Conflict("exists")
            self.items[k] = {"status": "url_issued", "declared": dict(declared), "url_expires_ms": expires_ms}

    def claim(self, thing: str, image_id: str, now_ms: int) -> dict | None:
        with self.lock:
            it = self.items.get((thing, f"img#{image_id}"))
            if it is None or it["status"] != "url_issued":
                return None
            it["status"], it["queued_ms"] = "queued", now_ms
            return dict(it["declared"])

    def set_status(self, thing: str, image_id: str, status: str, fields: dict) -> None:
        with self.lock:
            self.items.setdefault((thing, f"img#{image_id}"), {}).update(status=status, **fields)

    def count_upload(self, thing: str, hour: str, limit: int, ttl_s: int) -> bool:
        with self.lock:
            it = self.items.setdefault((thing, f"quota#{hour}"), {"n": 0})
            if it["n"] >= limit:
                return False
            it["n"] += 1
            return True

    def get(self, thing: str, image_id: str) -> dict | None:
        return self.items.get((thing, f"img#{image_id}"))


class BrokerNotifier:
    def __init__(self, broker: LocalBroker):
        self.broker = broker

    def publish(self, topic: str, payload: dict) -> None:
        self.broker.publish_as_service(topic, json.dumps(payload).encode())


class LocalQueue:
    def __init__(self, name: str):
        self.name = name
        self.messages: deque[dict] = deque()
        self.lock = threading.Lock()

    def send(self, body: dict) -> None:
        with self.lock:
            self.messages.append(body)

    def drain(self, max_n: int = 10_000) -> list[dict]:
        with self.lock:
            out = []
            while self.messages and len(out) < max_n:
                out.append(self.messages.popleft())
            return out


class LocalWorkflow:
    """Runs the steps with the same retry/catch structure as the Step Functions definition
    (infra/edge_infra/workflow.py): ModelTransientError is retried ``analyze_retries`` times, any
    error goes to the fallback step."""

    def __init__(self, deps: pipeline.Deps, analyze_retries: int = 3):
        self.deps, self.analyze_retries = deps, analyze_retries
        self.executions: dict[str, dict] = {}

    def start(self, name: str, payload: dict) -> None:
        if name in self.executions:  # execution names are unique, like Standard workflows
            return
        state: dict[str, Any] = dict(payload)
        try:
            state = pipeline.validate_input(self.deps, state)
            for attempt in range(self.analyze_retries + 1):
                try:
                    state = pipeline.analyze(self.deps, state)
                    break
                except ModelTransientError:
                    if attempt == self.analyze_retries:
                        raise
            out = pipeline.finalize(self.deps, state)
        except Exception as exc:  # noqa: BLE001 - mirrors the workflow's catch-all to the fallback
            cause = json.dumps({"errorMessage": exc.code if isinstance(exc, InputRejected) else type(exc).__name__})
            out = pipeline.finalize_fallback(self.deps, {**payload, "error": {"Error": type(exc).__name__,
                                                                             "Cause": cause}})
        self.executions[name] = out


class LocalRules:
    """Binds the rule definitions (edge_pipeline.rules) to local actions."""

    def __init__(self, broker: LocalBroker, prefix: str, actions: dict[str, Any]):
        self.rules = rules.device_rules(prefix)
        self.actions = actions
        self.errors = 0
        self.fired: dict[str, int] = {r.name: 0 for r in self.rules}
        broker.add_rules_hook(self.on_message)

    def on_message(self, topic: str, client_id: str, payload: bytes) -> None:
        now = int(time.time() * 1000)
        for r in self.rules:
            out = r.apply(topic, client_id, payload, now)
            if out is None:
                continue
            self.fired[r.name] += 1
            try:
                self.actions[r.action](out)
            except Exception:  # noqa: BLE001 - rule error action: count, like the error queue
                self.errors += 1
