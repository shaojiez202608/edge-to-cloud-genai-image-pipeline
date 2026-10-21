"""Simulator entry point (Fargate task or a workstation) against AWS IoT Core.

    python -m edge_pipeline.simulator.cli --scenario {e2e,loadtest,jobs-agent}

Configuration comes from environment variables (the task definition sets the stable ones; the
scripts pass the run-specific ones as container overrides):

  IOT_ENDPOINT       ATS data endpoint                     CLAIM_SECRET_ID  claim cert secret
  PROVISIONING_TEMPLATE  fleet provisioning template name  PREFIX           resource prefix
  REPORT_BUCKET      bucket for the JSON report (loadtest/<run_id>/...)    RUN_ID, TASK_INDEX
  DEVICES, CONNECT_RATE, PROVISION_RATE, OUTAGE_S, HEARTBEATS, HEARTBEAT_INTERVAL_S,
  RECONNECT_STRATEGY (jitter|immediate), DURATION_S (jobs-agent), INJECTION_SET (e2e: 1 = also
  upload the synthetic prompt-injection cases)

Reports contain counts, codes and timings only; no certificate material is ever written.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
import time
from collections import Counter

from .. import synthetic
from .device import EdgeBox, ReconnectPolicy
from .loadtest import run_load
from .provisioning import ProvisioningFailed, provision
from .transport import AwsIotTransport


def env(name: str, default: str | None = None) -> str:
    v = os.environ.get(name, default)
    if v is None:
        sys.exit(f"missing environment variable {name}")
    return v


def claim_material() -> tuple[str, str]:
    import boto3

    secret = boto3.client("secretsmanager").get_secret_value(SecretId=env("CLAIM_SECRET_ID"))
    doc = json.loads(secret["SecretString"])
    return doc["certificatePem"], doc["privateKey"]


async def provision_fleet(n: int, task_index: int, rate: float) -> tuple[list[EdgeBox], Counter]:
    endpoint, prefix, template = env("IOT_ENDPOINT"), env("PREFIX"), env("PROVISIONING_TEMPLATE")
    claim_cert, claim_key = claim_material()
    failures: Counter[str] = Counter()
    strategy = env("RECONNECT_STRATEGY", "jitter")
    run_id = env("RUN_ID", "manual")
    sem = asyncio.Semaphore(8)

    async def one(i: int) -> EdgeBox | None:
        serial = f"{task_index:02d}{i:04d}"
        async with sem:
            for attempt in range(5):
                t = AwsIotTransport(endpoint, claim_cert, claim_key, f"{prefix}-claim-{serial}")
                try:
                    ident = await provision(t, template, serial)
                    break
                except ProvisioningFailed as exc:
                    failures[exc.code] += 1
                    await asyncio.sleep(min(10, 0.5 * 2 ** attempt))
            else:
                return None
        dt = AwsIotTransport(endpoint, ident.cert_pem, ident.key_pem, ident.thing)
        return EdgeBox(ident.thing, dt, run_id=run_id, seed=int(serial),
                       reconnect=ReconnectPolicy(strategy=strategy))

    tasks = []
    for i in range(n):
        tasks.append(asyncio.create_task(one(i)))
        await asyncio.sleep(1.0 / rate)
    boxes = [b for b in await asyncio.gather(*tasks) if b is not None]
    return boxes, failures


def write_report(report: dict) -> None:
    run_id, idx = env("RUN_ID", "manual"), env("TASK_INDEX", "0")
    body = json.dumps(report, indent=1, default=str)
    bucket = os.environ.get("REPORT_BUCKET")
    if bucket:
        import boto3

        key = f"loadtest/{run_id}/{env('SCENARIO_NAME', 'report')}-{idx}.json"
        boto3.client("s3").put_object(Bucket=bucket, Key=key, Body=body.encode(), ContentType="application/json")
        print(json.dumps({"event": "report_written", "key": key}))
    else:
        print(body)


async def scenario_loadtest() -> dict:
    n, idx = int(env("DEVICES", "10")), int(env("TASK_INDEX", "0"))
    t0 = time.time()
    boxes, prov_fail = await provision_fleet(n, idx, float(env("PROVISION_RATE", "5")))
    t_prov = time.time() - t0
    report = await run_load(
        boxes, connect_rate_per_s=float(env("CONNECT_RATE", "10")),
        heartbeats_before=int(env("HEARTBEATS", "3")), heartbeats_after=int(env("HEARTBEATS", "3")),
        heartbeat_interval_s=float(env("HEARTBEAT_INTERVAL_S", "2")), outage_s=float(env("OUTAGE_S", "5")),
        reconnect_deadline_s=float(env("RECONNECT_DEADLINE_S", "180")))
    report.update(requested_devices=n, provisioned=len(boxes), provisioning_failures=dict(prov_fail),
                  provisioning_s=round(t_prov, 2), task_index=idx)
    return report


async def scenario_e2e() -> dict:
    boxes, prov_fail = await provision_fleet(2, int(env("TASK_INDEX", "90")), 2)
    if len(boxes) < 2:
        return {"error": "provisioning_failed", "provisioning_failures": dict(prov_fail)}
    a, b = boxes
    out: dict = {"devices": [a.thing, b.thing], "provisioning_failures": dict(prov_fail)}
    out["connected"] = [await a.connect(), await b.connect()]
    out["heartbeat"] = await a.heartbeat()
    out["benign"] = [await a.capture_and_upload(wait_result_s=120) for _ in range(2)]
    # Input checks: a declared PNG that is really a JPEG, and a declared size above the limit.
    jpeg = synthetic.product_photo(1, 1)
    out["type_spoof"] = await a.capture_and_upload(jpeg, content_type="image/png", wait_result_s=120)
    out["oversize_request"] = await a.capture_and_upload(jpeg, declared_size=50_000_000)
    if env("INJECTION_SET", "0") == "1":
        out["injection"] = {}
        for case in synthetic.CASES:
            data, _ = synthetic.render(case)
            res = await a.capture_and_upload(data, content_type="image/png", wait_result_s=180)
            out["injection"][case.case_id] = res
    out["cross_device_probe"] = await a.probe_foreign(b.thing)
    out["still_connected_after_probe"] = a.online
    await a.disconnect()
    await b.disconnect()
    return out


async def scenario_jobs_agent() -> dict:
    n, idx = int(env("DEVICES", "10")), int(env("TASK_INDEX", "50"))
    boxes, prov_fail = await provision_fleet(n, idx, float(env("PROVISION_RATE", "5")))
    await asyncio.gather(*(b.connect() for b in boxes))
    await asyncio.gather(*(b.start_jobs_agent() for b in boxes))
    deadline = time.time() + float(env("DURATION_S", "600"))
    while time.time() < deadline:
        await asyncio.gather(*(b.heartbeat() for b in boxes if b.online))
        await asyncio.sleep(10)
    await asyncio.gather(*(b.disconnect() for b in boxes))
    return {"devices": len(boxes), "provisioning_failures": dict(prov_fail),
            "versions": dict(Counter(b.version for b in boxes)),
            "jobs": [{"thing": b.thing, **j} for b in boxes for j in b.stats.jobs]}


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--scenario", choices=["e2e", "loadtest", "jobs-agent"], default=os.environ.get("SCENARIO"))
    args = ap.parse_args()
    os.environ.setdefault("SCENARIO_NAME", args.scenario)
    fn = {"e2e": scenario_e2e, "loadtest": scenario_loadtest, "jobs-agent": scenario_jobs_agent}[args.scenario]
    report = asyncio.run(fn())
    report.pop("generated_keys", None) if args.scenario != "loadtest" else None
    report["scenario"], report["run_id"] = args.scenario, env("RUN_ID", "manual")
    write_report(report)


if __name__ == "__main__":
    main()
