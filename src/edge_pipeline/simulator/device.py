"""One simulated edge box: heartbeats with sequence numbers, store-and-forward while offline,
image capture -> presigned upload -> metadata message, reconnect with a configurable strategy, and
an AWS IoT Jobs agent that installs a simulated component version with a health check and a local
rollback.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import random
import time
import uuid
from dataclasses import dataclass, field

from .. import naming, synthetic
from .transport import ConnectError, Transport, decode


def model_artifact(version: str) -> bytes:
    """Content of the simulated model file shipped with a component version."""
    return f"edge-model {version}\n".encode() * 64


def job_document(version: str, healthy: bool = True) -> dict:
    """AWS IoT Jobs document for a simulated component update. An unhealthy release ships a model whose
    self-test fails on the device (threshold outside the healthy range)."""
    return {"operation": "edge_component_update", "component": "edge-model", "version": version,
            "artifact_sha256": hashlib.sha256(model_artifact(version)).hexdigest(),
            "self_test_threshold": 0.5 if healthy else 0.95}


@dataclass
class ReconnectPolicy:
    strategy: str = "jitter"  # "jitter" (full-jitter exponential backoff) | "immediate"
    base_s: float = 0.5
    cap_s: float = 20.0
    max_attempts: int = 12

    def delay(self, attempt: int, rnd: random.Random) -> float:
        if self.strategy == "immediate":
            return 0.0
        return rnd.uniform(0, min(self.cap_s, self.base_s * (2 ** attempt)))


@dataclass
class DeviceStats:
    connect_attempts: int = 0
    connect_failures: dict = field(default_factory=dict)
    connected_at: list[float] = field(default_factory=list)
    reconnect_s: list[float] = field(default_factory=list)
    heartbeats_generated: int = 0
    heartbeats_acked: int = 0
    heartbeat_results: dict = field(default_factory=dict)
    uploads: dict = field(default_factory=dict)
    results: dict = field(default_factory=dict)
    denied: dict = field(default_factory=dict)
    jobs: list[dict] = field(default_factory=list)


class EdgeBox:
    def __init__(self, thing: str, transport: Transport, *, run_id: str = "", seed: int = 0,
                 reconnect: ReconnectPolicy | None = None, version: str = "1.0.0"):
        self.thing, self.t, self.run_id = thing, transport, run_id
        self.rnd = random.Random(seed)  # noqa: S311 - simulation jitter
        self.reconnect = reconnect or ReconnectPolicy()
        self.stats = DeviceStats()
        self.online = False
        self.seq = 0
        self.pending: list[int] = []  # heartbeat seqs waiting for an ack (store-and-forward)
        self.version, self.previous_version = version, None
        self._replies: dict[str, asyncio.Future] = {}
        self._results: dict[str, asyncio.Future] = {}
        self._stopping = False
        self._job_lock = asyncio.Lock()
        self.t.on_lost = self._on_lost

    # ---------------------------------------------------------------- connection
    def _on_lost(self, reason: str) -> None:
        if self._stopping:
            return
        self.online = False
        self.stats.connect_failures[f"lost:{reason}"] = self.stats.connect_failures.get(f"lost:{reason}", 0) + 1

    async def connect(self, deadline_s: float = 120) -> bool:
        """Connect (with retries per the reconnect policy) and subscribe. Returns True when online."""
        start = time.monotonic()
        for attempt in range(self.reconnect.max_attempts):
            self.stats.connect_attempts += 1
            try:
                await self.t.connect()
            except ConnectError as exc:
                self.stats.connect_failures[exc.code] = self.stats.connect_failures.get(exc.code, 0) + 1
                if time.monotonic() - start > deadline_s:
                    return False
                await asyncio.sleep(max(self.reconnect.delay(attempt, self.rnd), 0.2))
                continue
            self._stopping = False
            self.online = True
            self.stats.connected_at.append(time.time())
            await self._subscribe_all()
            await self.flush()
            return True
        return False

    async def _subscribe_all(self) -> None:
        await self.t.subscribe(naming.device_topic(self.thing, naming.UPLOAD_URL), self._on_upload_url)
        await self.t.subscribe(naming.device_topic(self.thing, naming.RESULTS), self._on_result)

    async def disconnect(self) -> None:
        self._stopping = True
        self.online = False
        await self.t.disconnect()

    async def outage_and_reconnect(self, outage_s: float, deadline_s: float) -> float | None:
        """Simulated site outage: drop the connection, wait, then reconnect per policy. Returns the
        time from the end of the outage to being online again (None if it never came back)."""
        await self.disconnect()
        await asyncio.sleep(outage_s)
        await asyncio.sleep(self.reconnect.delay(0, self.rnd))
        t0 = time.monotonic()
        ok = await self.connect(deadline_s)
        if not ok:
            return None
        dt = time.monotonic() - t0
        self.stats.reconnect_s.append(dt)
        return dt

    # ---------------------------------------------------------------- heartbeats
    async def heartbeat(self) -> str:
        self.seq += 1
        self.stats.heartbeats_generated += 1
        self.pending.append(self.seq)
        if not self.online:
            return "buffered"
        return await self._send_heartbeat(self.seq)

    async def _send_heartbeat(self, seq: int) -> str:
        body = json.dumps({"seq": seq, "run_id": self.run_id, "sent_ms": int(time.time() * 1000),
                           "version": self.version}).encode()
        r = await self.t.publish(naming.device_topic(self.thing, naming.HEARTBEAT), body)
        self.stats.heartbeat_results[r] = self.stats.heartbeat_results.get(r, 0) + 1
        if r == "success":
            self.stats.heartbeats_acked += 1
            if seq in self.pending:
                self.pending.remove(seq)
        return r

    async def flush(self) -> None:
        for seq in list(self.pending):
            if not self.online:
                return
            await self._send_heartbeat(seq)

    # ---------------------------------------------------------------- image path
    def _on_upload_url(self, topic: str, payload: bytes) -> None:
        msg = decode(payload)
        fut = self._replies.pop(str(msg.get("request_id")), None)
        if fut is not None and not fut.done():
            fut.set_result(msg)

    def _on_result(self, topic: str, payload: bytes) -> None:
        msg = decode(payload)
        fut = self._results.pop(str(msg.get("image_id")), None)
        if fut is not None and not fut.done():
            fut.set_result(msg)

    async def capture_and_upload(self, data: bytes | None = None, content_type: str = "image/jpeg",
                                 wait_result_s: float = 0, declared_size: int | None = None) -> dict:
        """Returns {"stage": ..., "code"/"image_id"/"status": ...} describing how far the image got."""
        data = data if data is not None else synthetic.product_photo(abs(hash(self.thing)) % 97, self.seq)
        rid = uuid.uuid4().hex[:16]
        loop = asyncio.get_running_loop()
        fut = loop.create_future()
        self._replies[rid] = fut
        req = {"request_id": rid, "content_type": content_type,
               "size": declared_size if declared_size is not None else len(data),
               "sha256": hashlib.sha256(data).hexdigest()}
        r = await self.t.publish(naming.device_topic(self.thing, naming.UPLOAD_REQUEST), json.dumps(req).encode())
        if r != "success":
            return self._count({"stage": "request", "code": r})
        try:
            reply = await asyncio.wait_for(fut, 20)
        except TimeoutError:
            return self._count({"stage": "request", "code": "no_reply"})
        if "error" in reply:
            return self._count({"stage": "request", "code": str(reply["error"])})
        status = await self.t.http_post(reply["url"], reply["fields"], data, content_type)
        if status not in (200, 201, 204):
            return self._count({"stage": "upload", "code": f"http_{status}"})
        image_id = reply["image_id"]
        res_fut = loop.create_future()
        self._results[image_id] = res_fut
        meta = {"image_id": image_id, "capture_ms": int(time.time() * 1000)}
        r = await self.t.publish(naming.device_topic(self.thing, naming.IMAGE_UPLOADED), json.dumps(meta).encode())
        if r != "success":
            return self._count({"stage": "metadata", "code": r})
        out = {"stage": "submitted", "image_id": image_id}
        if wait_result_s:
            try:
                res = await asyncio.wait_for(res_fut, wait_result_s)
                out = {"stage": "processed", "image_id": image_id, "status": res.get("status"),
                       "reason_codes": res.get("reason_codes", [])}
            except TimeoutError:
                out = {"stage": "submitted", "image_id": image_id, "code": "no_result_in_time"}
        return self._count(out)

    def _count(self, out: dict) -> dict:
        k = f"{out['stage']}:{out.get('status') or out.get('code') or 'ok'}"
        self.stats.uploads[k] = self.stats.uploads.get(k, 0) + 1
        return out

    # ---------------------------------------------------------------- authorization probes (D3)
    async def probe_foreign(self, other_thing: str) -> dict:
        """Try to act as / read another device. Every attempt must be refused by the policy."""
        out = {}
        out["publish_other_heartbeat"] = await self.t.publish(
            naming.device_topic(other_thing, naming.HEARTBEAT), b'{"seq":1}')
        out["publish_other_upload_request"] = await self.t.publish(
            naming.device_topic(other_thing, naming.UPLOAD_REQUEST), b"{}")
        out["subscribe_other_upload_url"] = await self.t.subscribe(
            naming.device_topic(other_thing, naming.UPLOAD_URL), lambda t, p: None)
        out["subscribe_other_results"] = await self.t.subscribe(
            naming.device_topic(other_thing, naming.RESULTS), lambda t, p: None)
        out["subscribe_wildcard_all_devices"] = await self.t.subscribe(f"{naming.TOPIC_ROOT}/#", lambda t, p: None)
        out["publish_other_job_update"] = await self.t.publish(
            f"$aws/things/{other_thing}/jobs/x/update", b'{"status":"SUCCEEDED"}')
        for k, v in out.items():
            self.stats.denied[k] = v
        return out

    # ---------------------------------------------------------------- AWS IoT Jobs agent
    async def start_jobs_agent(self) -> None:
        base = f"$aws/things/{self.thing}/jobs"
        await self.t.subscribe(f"{base}/notify-next", lambda t, p: self._spawn(self._next_job()))
        await self.t.subscribe(f"{base}/start-next/accepted", self._on_start_next)
        await self.t.subscribe(f"{base}/start-next/rejected", lambda t, p: None)
        await self.t.subscribe(f"{base}/+/update/accepted", lambda t, p: None)
        await self.t.subscribe(f"{base}/+/update/rejected", lambda t, p: None)
        await self._next_job()

    def _spawn(self, coro) -> None:
        asyncio.get_running_loop().create_task(coro)

    async def _next_job(self) -> None:
        await self.t.publish(f"$aws/things/{self.thing}/jobs/start-next",
                             json.dumps({"clientToken": uuid.uuid4().hex[:16]}).encode())

    def _on_start_next(self, topic: str, payload: bytes) -> None:
        execution = decode(payload).get("execution")
        if isinstance(execution, dict) and execution.get("jobId"):
            self._spawn(self._run_job(execution))

    async def _run_job(self, execution: dict) -> None:
        async with self._job_lock:
            job_id, doc = str(execution["jobId"]), execution.get("jobDocument") or {}
            status, details = self.apply_update(doc)
            await self.t.publish(f"$aws/things/{self.thing}/jobs/{job_id}/update",
                                 json.dumps({"status": status, "statusDetails": details}).encode())
            self.stats.jobs.append({"job_id": job_id, "status": status, **details})
        await self._next_job()

    def apply_update(self, doc: dict) -> tuple[str, dict]:
        """Install a simulated component version; health check = artifact hash + self-test.
        On failure the device returns to the version it ran before (local rollback)."""
        if doc.get("operation") != "edge_component_update" or not isinstance(doc.get("version"), str):
            return "REJECTED", {"reason": "unsupported_document", "running": self.version}
        new = doc["version"][:20]
        before = self.version
        artifact = model_artifact(doc.get("artifact_version", new))
        healthy = hashlib.sha256(artifact).hexdigest() == doc.get("artifact_sha256")
        threshold = doc.get("self_test_threshold", 0.5)
        if healthy and not (isinstance(threshold, int | float) and 0.2 <= threshold <= 0.8):
            healthy = False  # self-test: the simulated model misclassifies the reference sample
        if not healthy:
            return "FAILED", {"reason": "health_check_failed", "attempted": new, "running": before,
                              "rolled_back": "true"}
        self.previous_version, self.version = before, new
        return "SUCCEEDED", {"running": new, "previous": before}
