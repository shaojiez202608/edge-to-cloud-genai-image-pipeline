"""Offline demo (local simulation mode): D1-D6 without AWS, Docker or a broker process.

    uv run python scripts/local_demo.py [--devices 5] [--storm-devices 200] [--out out/demo_local.json]

Everything runs in-process (edge_pipeline.local). Connection throttling, delivery loss and job
rollout timing are simulated; the numbers show the behaviour of the device and pipeline code, not
AWS IoT Core performance.
"""

from __future__ import annotations

import argparse
import asyncio
import io
import logging
import sys
import time
from contextlib import redirect_stdout
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import guards  # noqa: E402,F401  (adds src/ to sys.path)

from edge_pipeline.evaluation import evaluate  # noqa: E402
from edge_pipeline.local.runtime import LocalEnvironment  # noqa: E402
from edge_pipeline.simulator.device import ReconnectPolicy, job_document  # noqa: E402
from edge_pipeline.simulator.loadtest import loss, run_load  # noqa: E402


async def d1_d3(env: LocalEnvironment, n: int) -> dict:
    boxes = [env.device(f"{i:06d}") for i in range(1, n + 1)]
    connected = [await b.connect() for b in boxes]
    for b in boxes:
        await b.heartbeat()
    online = sorted(t for t, p in env.presence.items() if p.get("last_event") == "connected")
    d1 = {"devices": n, "connected": sum(connected), "online_in_presence_table": len(online),
          "heartbeats_in_telemetry_queue": len(env.telemetry.messages)}
    a, b = boxes[0], boxes[1]
    res = await a.capture_and_upload(wait_result_s=10)
    manifest = env.shelf_manifest()
    d2 = {"upload": res, "manifest_items": len(manifest["items"]),
          "shelf_entry": next((i for i in manifest["items"] if i["image_id"] == res.get("image_id")), None)}
    probe = await a.probe_foreign(b.thing)
    # Connecting with another device's client id (own certificate) is refused by iot:Connect.
    from edge_pipeline.simulator.transport import ConnectError, LocalTransport

    t = LocalTransport(env.broker, env.objects, a.t.cert_id, b.thing)
    try:
        await t.connect()
        impersonation = "connected"
    except ConnectError as exc:
        impersonation = exc.code
    # A presigned POST grant cannot be reused for another key or size.
    grant = env.objects.presigned_post(f"uploads/{a.thing}/x.jpg", "image/jpeg", 10, 60)
    tampered = dict(grant["fields"], key=f"uploads/{b.thing}/x.jpg")
    d3 = {"probe": probe, "impersonate_other_client_id": impersonation,
          "post_other_key_status": env.objects.post(tampered, b"0123456789"),
          "post_other_size_status": env.objects.post(grant["fields"], b"01234567890123"),
          "denials_logged": len(env.broker.denials),
          "denial_example": {k: v for k, v in env.broker.denials[0].items() if k != "ts"} if env.broker.denials else None,
          "auth_error_counters": dict(env.broker.auth_errors)}
    # Revocation: an INACTIVE certificate is disconnected and cannot reconnect.
    env.broker.set_status(b.t.cert_id, "INACTIVE")
    revoked = {"online_after_inactive": b.online, "reconnect": await b.connect(deadline_s=1)}
    for x in boxes:
        await x.disconnect()
    return {"D1": d1, "D2": d2, "D3": d3, "revocation": revoked}


async def d4(env: LocalEnvironment, n: int) -> dict:
    boxes = [env.device(f"{i:06d}") for i in range(300, 300 + n)]
    for b in boxes:
        await b.connect()
        await b.start_jobs_agent()
    things = [b.thing for b in boxes]
    rollout = {"exponentialRate": {"baseRatePerMinute": 2, "incrementFactor": 2.0,
                                   "rateIncreaseCriteria": {"numberOfSucceededThings": 2}}, "maximumPerMinute": 50}
    abort = {"criteriaList": [{"failureType": "FAILED", "action": "CANCEL", "thresholdPercentage": 50,
                               "minNumberOfExecutedThings": 2}]}

    async def drive(job_id: str) -> dict:
        for _ in range(30):
            env.jobs.tick(job_id)
            await asyncio.sleep(0.05)
            if env.jobs.check_abort(job_id) or env.jobs.jobs[job_id].status != "IN_PROGRESS":
                break
        return env.jobs.summary(job_id)

    bad = job_document("2.0.0", healthy=False)  # the model fails its self-test on the device
    env.jobs.create_job("edge-model-2-0-0", things, bad, rollout, abort)
    s_bad = await drive("edge-model-2-0-0")
    after_bad = {v: sum(1 for b in boxes if b.version == v) for v in {b.version for b in boxes}}
    good = job_document("1.1.0")
    env.jobs.create_job("edge-model-1-1-0", things, good, rollout, abort)
    s_good = await drive("edge-model-1-1-0")
    after_good = {v: sum(1 for b in boxes if b.version == v) for v in {b.version for b in boxes}}
    for b in boxes:
        await b.disconnect()
    return {"devices": n, "bad_release": s_bad, "versions_after_bad": after_bad,
            "rolled_back_devices": sum(1 for b in boxes for j in b.stats.jobs if j.get("rolled_back") == "true"),
            "good_release": s_good, "versions_after_good": after_good}


async def d5(n: int, strategy: str) -> dict:
    env = LocalEnvironment(seed=3)
    env.broker.max_connects_per_second = max(5, n // 4)  # simulated account connect-rate limit
    run_id = f"local-{strategy}"
    boxes = [env.device(f"{i:06d}", run_id=run_id, reconnect=ReconnectPolicy(strategy=strategy, base_s=0.2, cap_s=3))
             for i in range(1000, 1000 + n)]
    rep = await run_load(boxes, connect_rate_per_s=env.broker.max_connects_per_second * 0.8, heartbeats_before=2,
                         heartbeats_after=2, heartbeat_interval_s=0.2, outage_s=0.5, reconnect_deadline_s=30)
    rep["loss"] = loss(rep, env.telemetry.drain(), run_id)
    rep["simulated_connect_limit_per_s"] = env.broker.max_connects_per_second
    rep.pop("generated_keys")
    return rep


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--devices", type=int, default=5)
    ap.add_argument("--jobs-devices", type=int, default=20)
    ap.add_argument("--storm-devices", type=int, default=200)
    ap.add_argument("--out", default="out/demo_local.json")
    ap.add_argument("--verbose", action="store_true")
    args = ap.parse_args()
    logging.basicConfig(level=logging.WARNING)
    t0 = time.time()
    sink = sys.stdout if args.verbose else io.StringIO()  # pipeline JSON logs are hidden unless --verbose
    with redirect_stdout(sink):
        env = LocalEnvironment()
        report = asyncio.run(_with_pump(env, d1_d3(env, args.devices)))
        report["D4"] = asyncio.run(_with_pump(env, d4(env, args.jobs_devices)))
        report["D5"] = {s: asyncio.run(d5(args.storm_devices, s)) for s in ("immediate", "jitter")}
        ev = evaluate("naive")
        report["D6"] = {k: ev[k] for k in ("model", "cases", "counted", "passed", "failed", "known_gaps")}
        report["D6"]["outcomes"] = {r["case_id"]: [r["status"], r["reason_codes"]] for r in ev["rows"]}
        neg = evaluate("naive", negative_control=True)
        report["D6_negative_control"] = {"passed": neg["passed"], "counted": neg["counted"], "failed": len(neg["failed"])}
    report["elapsed_s"] = round(time.time() - t0, 1)
    guards.write_report(args.out, report)
    _summary(report)


async def _with_pump(env: LocalEnvironment, coro):
    pump = asyncio.create_task(env.pump_forever())
    try:
        return await coro
    finally:
        pump.cancel()


def _summary(r: dict) -> None:
    d1, d2, d3, d4r, d6 = r["D1"], r["D2"], r["D3"], r["D4"], r["D6"]
    probe_ok = all(v == "not_authorized" for v in d3["probe"].values())
    lines = [
        "== Offline demo (local simulation mode) ==",
        f"D1 devices connected {d1['connected']}/{d1['devices']}, presence online {d1['online_in_presence_table']}, "
        f"heartbeats routed {d1['heartbeats_in_telemetry_queue']}",
        f"D2 upload -> {d2['upload'].get('status')} ; shelf manifest items {d2['manifest_items']}",
        f"D3 cross-device attempts refused: {probe_ok} ({len(d3['probe'])} attempts), impersonation: "
        f"{d3['impersonate_other_client_id']}, tampered POST: {d3['post_other_key_status']}/{d3['post_other_size_status']}, "
        f"denials logged {d3['denials_logged']}",
        f"   revocation: online after INACTIVE={r['revocation']['online_after_inactive']}, reconnect={r['revocation']['reconnect']}",
        f"D4 bad release -> {d4r['bad_release']['status']} {d4r['bad_release']['executions']}; versions {d4r['versions_after_bad']}; "
        f"good release -> {d4r['good_release']['status']} versions {d4r['versions_after_good']}",
    ]
    for s, x in r["D5"].items():
        lines.append(f"D5 [{s}] devices {x['devices']}: reconnect success {x['reconnect_success_rate']}, "
                     f"reconnect p50/p95 {x['reconnect_s']['p50']}/{x['reconnect_s']['p95']} s, "
                     f"connect attempts {x['connect_attempts']}, throttled {x['connect_failures'].get('throttled', 0)}, "
                     f"loss rate {x['loss']['loss_rate']}")
    lines.append(f"D6 injection set: {d6['passed']}/{d6['counted']} expected outcomes, failed {d6['failed']}, "
                 f"known gaps {[g['case_id'] for g in d6['known_gaps']]}")
    nc = r["D6_negative_control"]
    lines.append(f"   negative control (validation disabled): {nc['failed']} failures detected (must be > 0)")
    lines.append(f"elapsed {r['elapsed_s']} s")
    print("\n".join(lines))


if __name__ == "__main__":
    main()
