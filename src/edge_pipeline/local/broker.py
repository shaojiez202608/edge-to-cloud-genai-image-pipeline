"""In-process MQTT stand-in with AWS IoT-style authorization.

- Clients authenticate with a registered certificate id (stands in for the TLS client certificate).
- Every CONNECT / PUBLISH / SUBSCRIBE / delivery is checked with ``policy.is_allowed`` against the
  policies attached to the certificate, with the thing policy variables resolved as IoT Core does.
- Unauthorized PUBLISH/SUBSCRIBE get a "not_authorized" reason code (as an MQTT 5 PUBACK/SUBACK
  would) and are counted in ``auth_errors`` (stands in for the AWS/IoT *.AuthError metrics).
- A second connection with the same client id disconnects the first (DUPLICATE_CLIENTID).
- Lifecycle events are published on ``$aws/events/presence/{connected|disconnected}/<clientId>``.
- Optional fault injection: drop a fraction of deliveries/acks, or disconnect everyone at once.
"""

from __future__ import annotations

import itertools
import json
import random
import threading
import time
from collections import Counter
from collections.abc import Callable
from dataclasses import dataclass, field

from .. import policy
from ..rules import topic_matches

Handler = Callable[[str, bytes], None]


class ConnectRefused(Exception):
    def __init__(self, code: str):
        super().__init__(code)
        self.code = code


@dataclass
class Certificate:
    cert_id: str
    thing: str | None
    status: str = "ACTIVE"
    policies: list[dict] = field(default_factory=list)


@dataclass
class Session:
    sid: int
    client_id: str
    principal: policy.Principal
    on_disconnect: Callable[[str], None] | None
    subscriptions: dict[str, Handler] = field(default_factory=dict)
    connected: bool = True


class LocalBroker:
    def __init__(self, region: str = "us-east-1", account: str = "111122223333", partition: str = "aws",
                 seed: int = 1):
        self.region, self.account, self.partition = region, account, partition
        self.certs: dict[str, Certificate] = {}
        self.sessions: dict[str, Session] = {}
        self.lock = threading.RLock()
        self.rules_hooks: list[Callable[[str, str, bytes], None]] = []
        self.service_hooks: list[tuple[str, Callable[[str, str, bytes], None]]] = []
        self.auth_errors: Counter[str] = Counter()
        self.metrics: Counter[str] = Counter()
        self.denials: list[dict] = []
        self.drop_rate = 0.0
        self.rnd = random.Random(seed)  # noqa: S311 - fault-injection simulation
        self._ids = itertools.count(1)
        self.max_connects_per_second: int | None = None
        self._connect_window: list[float] = []

    # ---------------------------------------------------------------- registry
    def register(self, cert_id: str, thing: str | None, policies: list[dict]) -> None:
        with self.lock:
            self.certs[cert_id] = Certificate(cert_id, thing, "ACTIVE", list(policies))

    def set_status(self, cert_id: str, status: str) -> None:
        with self.lock:
            self.certs[cert_id].status = status
            if status != "ACTIVE":  # IoT Core closes connections of deactivated certificates
                for s in list(self.sessions.values()):
                    if s.principal.certificate_id == cert_id:
                        self._drop(s, "AUTH_ERROR")

    def arn(self, kind: str, rest: str) -> str:
        return f"arn:{self.partition}:iot:{self.region}:{self.account}:{kind}/{rest}"

    def _deny(self, s_or_client: str, action: str, resource: str) -> None:
        self.auth_errors[action] += 1
        self.denials.append({"client_id": s_or_client, "action": action, "resource": resource,
                             "ts": int(time.time() * 1000)})

    # ---------------------------------------------------------------- client API
    def connect(self, cert_id: str, client_id: str, on_disconnect: Callable[[str], None] | None = None) -> Session:
        with self.lock:
            if self.max_connects_per_second is not None:
                now = time.monotonic()
                self._connect_window = [t for t in self._connect_window if now - t < 1.0]
                if len(self._connect_window) >= self.max_connects_per_second:
                    self.metrics["Connect.Throttle"] += 1
                    raise ConnectRefused("throttled")
                self._connect_window.append(now)
            cert = self.certs.get(cert_id)
            if cert is None or cert.status != "ACTIVE":
                self.metrics["Connect.AuthError"] += 1
                raise ConnectRefused("bad_certificate")
            principal = policy.Principal(cert_id, cert.thing, client_id)
            if not policy.is_allowed(cert.policies, principal, "iot:Connect", self.arn("client", client_id)):
                self._deny(client_id, "iot:Connect", self.arn("client", client_id))
                self.metrics["Connect.AuthError"] += 1
                raise ConnectRefused("not_authorized")
            old = self.sessions.get(client_id)
            if old is not None:
                self._drop(old, "DUPLICATE_CLIENTID")
            s = Session(next(self._ids), client_id, principal, on_disconnect)
            self.sessions[client_id] = s
            self.metrics["Connect.Success"] += 1
        self._lifecycle("connected", client_id, None)
        return s

    def disconnect(self, s: Session, reason: str = "CLIENT_INITIATED_DISCONNECT") -> None:
        with self.lock:
            if not s.connected:
                return
            s.connected = False
            if self.sessions.get(s.client_id) is s:
                del self.sessions[s.client_id]
        self._lifecycle("disconnected", s.client_id, reason)

    def _drop(self, s: Session, reason: str) -> None:
        self.disconnect(s, reason)
        if s.on_disconnect:
            s.on_disconnect(reason)

    def drop_all(self, reason: str = "CONNECTION_LOST") -> int:
        with self.lock:
            victims = list(self.sessions.values())
        for s in victims:
            self._drop(s, reason)
        return len(victims)

    def publish(self, s: Session, topic: str, payload: bytes, qos: int = 1) -> str:
        """Returns "success" (PUBACK), "not_authorized", "not_connected" or "lost" (no ack)."""
        if not s.connected:
            return "not_connected"
        cert = self.certs[s.principal.certificate_id]
        arn = self.arn("topic", topic)
        if not policy.is_allowed(cert.policies, s.principal, "iot:Publish", arn):
            self._deny(s.client_id, "iot:Publish", arn)
            self.metrics["PublishIn.AuthError"] += 1
            return "not_authorized"
        if self.drop_rate and self.rnd.random() < self.drop_rate:
            return "lost"
        self.metrics["PublishIn.Success"] += 1
        self._route(topic, s.client_id, payload)
        return "success"

    def subscribe(self, s: Session, topic_filter: str, handler: Handler) -> str:
        if not s.connected:
            return "not_connected"
        cert = self.certs[s.principal.certificate_id]
        arn = self.arn("topicfilter", topic_filter)
        if not policy.is_allowed(cert.policies, s.principal, "iot:Subscribe", arn):
            self._deny(s.client_id, "iot:Subscribe", arn)
            self.metrics["Subscribe.AuthError"] += 1
            return "not_authorized"
        s.subscriptions[topic_filter] = handler
        return "granted"

    # ---------------------------------------------------------------- service side
    def publish_as_service(self, topic: str, payload: bytes) -> None:
        """Cloud-side publish (rules republish, Lambda via the data plane): IAM, not device policy."""
        self._route(topic, "$service", payload)

    def add_rules_hook(self, hook: Callable[[str, str, bytes], None]) -> None:
        self.rules_hooks.append(hook)

    def add_reserved_handler(self, topic_filter: str, hook: Callable[[str, str, bytes], None]) -> None:
        """Service endpoints on reserved topics (e.g. AWS IoT Jobs)."""
        self.service_hooks.append((topic_filter, hook))

    def _route(self, topic: str, client_id: str, payload: bytes) -> None:
        for flt, hook in self.service_hooks:
            if topic_matches(flt, topic):
                hook(topic, client_id, payload)
        for hook in self.rules_hooks:
            hook(topic, client_id, payload)
        with self.lock:
            targets = [(s, h) for s in self.sessions.values() for f, h in s.subscriptions.items()
                       if topic_matches(f, topic)]
        for s, handler in targets:
            cert = self.certs[s.principal.certificate_id]
            if not policy.is_allowed(cert.policies, s.principal, "iot:Receive", self.arn("topic", topic)):
                self._deny(s.client_id, "iot:Receive", self.arn("topic", topic))
                continue
            if self.drop_rate and self.rnd.random() < self.drop_rate:
                continue
            handler(topic, payload)

    def _lifecycle(self, kind: str, client_id: str, reason: str | None) -> None:
        msg = {"clientId": client_id, "eventType": kind, "timestamp": int(time.time() * 1000)}
        if reason:
            msg["disconnectReason"] = reason
        for hook in self.rules_hooks:
            hook(f"$aws/events/presence/{kind}/{client_id}", "$service", json.dumps(msg).encode())
