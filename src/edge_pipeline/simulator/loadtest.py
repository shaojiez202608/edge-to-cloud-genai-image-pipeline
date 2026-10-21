"""Connection load test and reconnect storm, shared by local mode and the Fargate simulator task.

Phases: ramp (connect at a bounded rate) -> steady heartbeats -> storm (every device drops at the
same moment, stays offline for ``outage_s`` while buffering heartbeats, then reconnects per its
reconnect policy) -> heartbeats after the storm -> disconnect.

Device-side metrics: connect success rate, reconnect success rate, reconnect time distribution,
heartbeats generated / acknowledged. Loss is measured end to end by the caller: heartbeats that
reached the cloud (rule -> telemetry queue, de-duplicated by (thing, seq)) versus generated.
"""

from __future__ import annotations

import asyncio
import time
from collections import Counter

from .device import EdgeBox


def percentile(values: list[float], p: float) -> float | None:
    if not values:
        return None
    v = sorted(values)
    k = min(len(v) - 1, max(0, round(p / 100 * (len(v) - 1))))
    return round(v[k], 3)


async def _ramp(boxes: list[EdgeBox], rate_per_s: float, deadline_s: float) -> list[bool]:
    tasks = []
    for b in boxes:
        tasks.append(asyncio.create_task(b.connect(deadline_s)))
        await asyncio.sleep(1.0 / rate_per_s)
    return list(await asyncio.gather(*tasks))


async def _beats(boxes: list[EdgeBox], count: int, interval_s: float) -> None:
    for _ in range(count):
        await asyncio.gather(*(b.heartbeat() for b in boxes))
        await asyncio.sleep(interval_s)


async def run_load(boxes: list[EdgeBox], *, connect_rate_per_s: float, heartbeats_before: int = 3,
                   heartbeats_after: int = 3, heartbeat_interval_s: float = 1.0, outage_s: float = 2.0,
                   reconnect_deadline_s: float = 120.0, storm: bool = True) -> dict:
    t0 = time.time()
    initial = await _ramp(boxes, connect_rate_per_s, reconnect_deadline_s)
    t_ramp = time.time()
    await _beats([b for b in boxes if b.online], heartbeats_before, heartbeat_interval_s)
    reconnect: list[float | None] = []
    t_storm = time.time()
    if storm:
        async def offline_beats() -> None:
            n = max(1, int(outage_s / max(heartbeat_interval_s, 0.01)))
            await asyncio.sleep(0.01)
            for _ in range(n):
                await asyncio.gather(*(b.heartbeat() for b in boxes if not b.online))
                await asyncio.sleep(heartbeat_interval_s)

        buffered = asyncio.create_task(offline_beats())
        reconnect = list(await asyncio.gather(*(b.outage_and_reconnect(outage_s, reconnect_deadline_s)
                                                 for b in boxes if b.stats.connected_at)))
        await buffered
    t_after = time.time()
    await _beats([b for b in boxes if b.online], heartbeats_after, heartbeat_interval_s)
    await asyncio.gather(*(b.flush() for b in boxes if b.online))
    await asyncio.sleep(0.5)
    await asyncio.gather(*(b.disconnect() for b in boxes))

    failures: Counter[str] = Counter()
    hb: Counter[str] = Counter()
    for b in boxes:
        failures.update(b.stats.connect_failures)
        hb.update(b.stats.heartbeat_results)
    ok = [r for r in reconnect if r is not None]
    return {
        "devices": len(boxes),
        "strategy": boxes[0].reconnect.strategy if boxes else None,
        "connect_rate_per_s": connect_rate_per_s,
        "initial_connected": sum(initial),
        "initial_connect_success_rate": round(sum(initial) / len(boxes), 4) if boxes else None,
        "storm": storm,
        "outage_s": outage_s,
        "reconnect_attempted": len(reconnect),
        "reconnected": len(ok),
        "reconnect_success_rate": round(len(ok) / len(reconnect), 4) if reconnect else None,
        "reconnect_s": {"p50": percentile(ok, 50), "p95": percentile(ok, 95), "max": percentile(ok, 100)},
        "connect_attempts": sum(b.stats.connect_attempts for b in boxes),
        "connect_failures": dict(failures),
        "heartbeats_generated": sum(b.stats.heartbeats_generated for b in boxes),
        "heartbeats_acked": sum(b.stats.heartbeats_acked for b in boxes),
        "heartbeats_unacked_at_end": sum(len(b.pending) for b in boxes),
        "heartbeat_publish_results": dict(hb),
        "phase_s": {"ramp": round(t_ramp - t0, 2), "steady": round(t_storm - t_ramp, 2),
                    "storm": round(t_after - t_storm, 2), "total": round(time.time() - t0, 2)},
        "generated_keys": [(b.thing, s) for b in boxes for s in range(1, b.seq + 1)],
    }


def loss(report: dict, received: list[dict], run_id: str) -> dict:
    """End-to-end loss: generated heartbeats that never reached the telemetry queue."""
    generated = {(t, s) for t, s in report["generated_keys"]}
    got = {(m.get("thing_name"), m.get("seq")) for m in received if m.get("run_id") == run_id}
    lost = generated - got
    return {"generated": len(generated), "received_unique": len(got & generated),
            "duplicates_or_foreign": len([m for m in received if m.get("run_id") == run_id]) - len(got & generated),
            "lost": len(lost), "loss_rate": round(len(lost) / len(generated), 6) if generated else None}
