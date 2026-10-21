"""Transports used by the simulated device: in-process (local mode) and AWS IoT Core (MQTT 5 over
mutual TLS with the AWS IoT Device SDK v2). Both return the same reason strings:
publish -> success | not_authorized | lost | not_connected | timeout | error
subscribe -> granted | not_authorized | not_connected | timeout | error
"""

from __future__ import annotations

import asyncio
import json
import urllib.request
import uuid
from collections.abc import Callable
from typing import Protocol

Handler = Callable[[str, bytes], None]


class ConnectError(Exception):
    def __init__(self, code: str):
        super().__init__(code)
        self.code = code


class Transport(Protocol):
    client_id: str
    on_lost: Callable[[str], None] | None

    async def connect(self, timeout_s: float = 10) -> None: ...
    async def disconnect(self) -> None: ...
    async def publish(self, topic: str, payload: bytes, timeout_s: float = 10) -> str: ...
    async def subscribe(self, topic_filter: str, handler: Handler, timeout_s: float = 10) -> str: ...
    async def http_post(self, url: str, fields: dict, data: bytes, content_type: str) -> int: ...


class LocalTransport:
    def __init__(self, broker, store, cert_id: str, client_id: str):
        self.broker, self.store, self.cert_id, self.client_id = broker, store, cert_id, client_id
        self.session = None
        self.on_lost: Callable[[str], None] | None = None
        self._handlers: dict[str, Handler] = {}

    async def connect(self, timeout_s: float = 10) -> None:
        from ..local.broker import ConnectRefused

        await asyncio.sleep(0)
        try:
            self.session = self.broker.connect(self.cert_id, self.client_id, self._lost)
        except ConnectRefused as exc:
            raise ConnectError(exc.code) from None

    def _lost(self, reason: str) -> None:
        if self.on_lost:
            self.on_lost(reason)

    async def disconnect(self) -> None:
        if self.session is not None:
            self.broker.disconnect(self.session)
        await asyncio.sleep(0)

    async def publish(self, topic: str, payload: bytes, timeout_s: float = 10) -> str:
        await asyncio.sleep(0)
        if self.session is None:
            return "not_connected"
        return self.broker.publish(self.session, topic, payload)

    async def subscribe(self, topic_filter: str, handler: Handler, timeout_s: float = 10) -> str:
        await asyncio.sleep(0)
        if self.session is None:
            return "not_connected"
        loop = asyncio.get_running_loop()

        def deliver(topic: str, payload: bytes) -> None:  # broker callbacks run on the caller's thread
            loop.call_soon(handler, topic, payload)

        return self.broker.subscribe(self.session, topic_filter, deliver)

    async def http_post(self, url: str, fields: dict, data: bytes, content_type: str) -> int:
        await asyncio.sleep(0)
        return self.store.post(fields, data)


def multipart(fields: dict, data: bytes, content_type: str) -> tuple[bytes, str]:
    """multipart/form-data body for an S3 presigned POST (fields first, file last)."""
    boundary = uuid.uuid4().hex
    parts = []
    for k, v in fields.items():
        parts.append(f'--{boundary}\r\nContent-Disposition: form-data; name="{k}"\r\n\r\n{v}\r\n'.encode())
    parts.append(f'--{boundary}\r\nContent-Disposition: form-data; name="file"; filename="image"\r\n'
                 f"Content-Type: {content_type}\r\n\r\n".encode() + data + b"\r\n")
    parts.append(f"--{boundary}--\r\n".encode())
    return b"".join(parts), f"multipart/form-data; boundary={boundary}"


class AwsIotTransport:
    """MQTT 5 client from the AWS IoT Device SDK v2. The SDK's own reconnect is disabled during
    measured storms by stopping the client; the simulator controls reconnect timing itself."""

    def __init__(self, endpoint: str, cert_pem: str, key_pem: str, client_id: str):
        self.endpoint, self.cert_pem, self.key_pem, self.client_id = endpoint, cert_pem, key_pem, client_id
        self.on_lost: Callable[[str], None] | None = None
        self._client = None
        self._handlers: dict[str, Handler] = {}
        self._loop: asyncio.AbstractEventLoop | None = None
        self._connected: asyncio.Future | None = None

    def _build(self):
        from awscrt import mqtt5
        from awsiot import mqtt5_client_builder

        def on_success(data):
            fut = self._connected
            if fut is not None:
                self._loop.call_soon_threadsafe(lambda: fut.done() or fut.set_result(True))

        def on_failure(data):
            code = "connect_failed"
            try:
                rc = data.connack_packet.reason_code if data.connack_packet else None
                code = rc.name.lower() if rc is not None else getattr(data.exception, "name", type(data.exception).__name__)
            except Exception:  # noqa: BLE001 - keep the generic code
                code = "connect_failed"
            fut = self._connected
            if fut is not None:
                self._loop.call_soon_threadsafe(lambda: fut.done() or fut.set_exception(ConnectError(code)))

        def on_disconnection(data):
            reason = "connection_lost"
            try:
                if data.disconnect_packet is not None:
                    reason = data.disconnect_packet.reason_code.name.lower()
            except Exception:  # noqa: BLE001 - keep the generic reason
                reason = "connection_lost"
            if self.on_lost and self._loop:
                self._loop.call_soon_threadsafe(self.on_lost, reason)

        def on_publish(data):
            pkt = data.publish_packet
            for flt, h in list(self._handlers.items()):
                from ..rules import topic_matches

                if topic_matches(flt, pkt.topic):
                    self._loop.call_soon_threadsafe(h, pkt.topic, bytes(pkt.payload or b""))

        return mqtt5_client_builder.mtls_from_bytes(
            endpoint=self.endpoint, port=8883,
            cert_bytes=self.cert_pem.encode(), pri_key_bytes=self.key_pem.encode(),
            client_id=self.client_id,
            session_behavior=mqtt5.ClientSessionBehaviorType.CLEAN,
            on_lifecycle_connection_success=on_success,
            on_lifecycle_connection_failure=on_failure,
            on_lifecycle_disconnection=on_disconnection,
            on_publish_received=on_publish,
            min_reconnect_delay_ms=1000, max_reconnect_delay_ms=30000,
            retry_jitter_mode=mqtt5.ExponentialBackoffJitterMode.FULL,
            connack_timeout_ms=10000,
        )

    async def connect(self, timeout_s: float = 10) -> None:
        self._loop = asyncio.get_running_loop()
        if self._client is None:
            self._client = self._build()
        self._connected = self._loop.create_future()
        self._client.start()
        try:
            await asyncio.wait_for(self._connected, timeout_s)
        except TimeoutError:
            self._client.stop()
            raise ConnectError("timeout") from None
        except ConnectError:
            self._client.stop()  # stop the SDK's own retries: the caller decides when to retry
            raise

    async def disconnect(self) -> None:
        if self._client is not None:
            self._client.stop()
            await asyncio.sleep(0.05)

    async def publish(self, topic: str, payload: bytes, timeout_s: float = 10) -> str:
        from awscrt import mqtt5

        if self._client is None:
            return "not_connected"
        try:
            fut = self._client.publish(mqtt5.PublishPacket(topic=topic, payload=payload,
                                                           qos=mqtt5.QoS.AT_LEAST_ONCE))
            res = await asyncio.wait_for(asyncio.wrap_future(fut), timeout_s)
        except TimeoutError:
            return "timeout"
        except Exception:  # noqa: BLE001 - offline queue failure or client stopped
            return "lost"
        rc = getattr(getattr(res, "puback", None), "reason_code", None)
        if rc is None or rc.name in ("SUCCESS", "NO_MATCHING_SUBSCRIBERS"):
            return "success"
        return "not_authorized" if rc.name == "NOT_AUTHORIZED" else rc.name.lower()

    async def subscribe(self, topic_filter: str, handler: Handler, timeout_s: float = 10) -> str:
        from awscrt import mqtt5

        if self._client is None:
            return "not_connected"
        self._handlers[topic_filter] = handler
        try:
            fut = self._client.subscribe(mqtt5.SubscribePacket(subscriptions=[
                mqtt5.Subscription(topic_filter=topic_filter, qos=mqtt5.QoS.AT_LEAST_ONCE)]))
            res = await asyncio.wait_for(asyncio.wrap_future(fut), timeout_s)
        except TimeoutError:
            return "timeout"
        except Exception:  # noqa: BLE001
            return "error"
        codes = [c.name for c in res.reason_codes]
        if codes and codes[0].startswith("GRANTED"):
            return "granted"
        self._handlers.pop(topic_filter, None)
        return "not_authorized" if codes and codes[0] == "NOT_AUTHORIZED" else (codes[0].lower() if codes else "error")

    async def http_post(self, url: str, fields: dict, data: bytes, content_type: str) -> int:
        if not url.startswith("https://"):
            return 400
        body, ctype = multipart(fields, data, content_type)

        def post() -> int:
            req = urllib.request.Request(url, data=body, method="POST", headers={"Content-Type": ctype})  # noqa: S310
            try:
                with urllib.request.urlopen(req, timeout=30) as r:  # noqa: S310  # nosec B310 - only https:// presigned S3 URLs reach here (checked above); file:// and other schemes are refused
                    return r.status
            except urllib.error.HTTPError as e:
                return e.code
            except OSError:
                return 599

        return await asyncio.to_thread(post)


def decode(payload: bytes) -> dict:
    try:
        v = json.loads(payload)
        return v if isinstance(v, dict) else {}
    except (ValueError, UnicodeDecodeError):
        return {}
