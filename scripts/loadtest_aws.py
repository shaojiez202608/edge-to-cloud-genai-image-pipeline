"""Connection load test and reconnect storm on AWS IoT Core (paid: ALLOW_PAID=1).

The scale is derived from the account's applied quotas (Service Quotas, read at run time; nothing is
hard-coded): total devices are kept to a fraction of the concurrent-connection quota and of the
project's max_fleet_size, the connect and provisioning rates to a fraction of the per-account rate
quotas, and the number of Fargate tasks to a fraction of the Fargate vCPU quota. The fractions leave
room for other users of a shared sandbox account. --devices caps the total further.

    ALLOW_PAID=1 uv run python scripts/loadtest_aws.py [--devices N] [--per-task 100]
        [--strategies jitter immediate] [--report out/loadtest_aws.json]
"""

from __future__ import annotations

import argparse
import math
import sys
import time

from aws_ops import Ctx, run_id
from guards import mask_text, require_paid, write_report

from edge_pipeline.simulator.loadtest import loss, percentile

QUOTAS = {  # service code, quota code -> meaning
    ("iotcore", "L-AEA1E176"): "connect_per_s_account",
    ("iotcore", "L-75CAB723"): "max_connections_account",
    ("iotcore", "L-4B33E451"): "inbound_publish_per_s_account",
    ("iotcore", "L-CAFFCA46"): "fp_create_keys_tps",
    ("iotcore", "L-B2BBBD8C"): "fp_register_thing_tps",
    ("fargate", "L-3032A538"): "fargate_vcpu",
}
SHARE = 0.25  # fraction of each account-level rate quota this test may use


def read_quotas(ctx: Ctx) -> dict:
    sq = ctx.client("service-quotas")
    out = {}
    for (svc, code), name in QUOTAS.items():
        try:
            out[name] = sq.get_service_quota(ServiceCode=svc, QuotaCode=code)["Quota"]["Value"]
        except Exception:  # noqa: BLE001 - fall back to the AWS default value
            try:
                out[name] = sq.get_aws_default_service_quota(ServiceCode=svc, QuotaCode=code)["Quota"]["Value"]
            except Exception:  # noqa: BLE001
                out[name] = None
    return out


def plan(ctx: Ctx, q: dict, devices: int | None, per_task: int) -> dict:
    d = ctx.params["deploy"]
    cpu = d["simulator_task"]["cpu"] / 1024
    limits = [d["max_fleet_size"]]
    if q.get("max_connections_account"):
        limits.append(int(q["max_connections_account"] * SHARE))
    if devices:
        limits.append(devices)
    total = max(1, min(limits))
    tasks = math.ceil(total / per_task)
    if q.get("fargate_vcpu"):
        tasks = max(1, min(tasks, int(q["fargate_vcpu"] * 0.5 / cpu)))
    per = math.ceil(total / tasks)
    connect = (q.get("connect_per_s_account") or 100) * SHARE / tasks
    prov = min(q.get("fp_create_keys_tps") or 10, q.get("fp_register_thing_tps") or 10) * SHARE / tasks
    return {"devices": per * tasks, "tasks": tasks, "devices_per_task": per,
            "connect_rate_per_task": round(max(connect, 0.5), 2), "provision_rate_per_task": round(max(prov, 0.2), 2),
            "share_of_rate_quotas": SHARE}


def run_one(ctx: Ctx, p: dict, strategy: str, outage_s: float) -> dict:
    rid = run_id(f"lt-{strategy}")
    provisioning_s = p["devices_per_task"] / p["provision_rate_per_task"] + 60
    start_at = time.time() + 90 + provisioning_s  # task start-up + provisioning, then all tasks ramp together
    ramp_s = p["devices_per_task"] / p["connect_rate_per_task"]
    storm_at = start_at + ramp_s + 60
    envs = [{"SCENARIO": "loadtest", "RUN_ID": rid, "TASK_INDEX": str(i), "IOT_ENDPOINT": ctx.iot_endpoint,
             "DEVICES": p["devices_per_task"], "CONNECT_RATE": p["connect_rate_per_task"],
             "PROVISION_RATE": p["provision_rate_per_task"], "RECONNECT_STRATEGY": strategy, "OUTAGE_S": outage_s,
             "HEARTBEATS": 3, "HEARTBEAT_INTERVAL_S": 5, "START_AT": int(start_at), "STORM_AT": int(storm_at)}
            for i in range(p["tasks"])]
    print(f"== {rid}: {p['tasks']} task(s) x {p['devices_per_task']} devices, strategy {strategy}")
    arns = ctx.run_tasks("simulator", envs, started_by=rid)
    status = ctx.wait_tasks(arns, timeout_s=int(storm_at - time.time() + 1800))
    reps = ctx.reports(rid)
    received = [m for m in ctx.drain_queue(ctx.out["TelemetryQueueUrl"]) if m.get("run_id") == rid]
    merged = {"generated_keys": [tuple(k) for r in reps for k in r.get("generated_keys", [])]}
    lo = loss(merged, received, rid)
    rec = [x for r in reps for x in r.get("reconnect_samples", [])]
    agg = {
        "run_id": rid, "strategy": strategy, "tasks": len(arns), "task_exit_codes": [s["exit"] for s in status.values()],
        "task_stop_reasons": sorted({s["reason"][:40] for s in status.values()}), "tasks_reported": len(reps),
        "devices_requested": p["devices"], "provisioned": sum(r.get("provisioned", 0) for r in reps),
        "initial_connected": sum(r.get("initial_connected", 0) for r in reps),
        "reconnect_attempted": sum(r.get("reconnect_attempted", 0) for r in reps),
        "reconnected": sum(r.get("reconnected", 0) for r in reps),
        "reconnect_s_per_task": [r.get("reconnect_s") for r in reps],
        "connect_failures": _sum_dicts(r.get("connect_failures", {}) for r in reps),
        "provisioning_failures": _sum_dicts(r.get("provisioning_failures", {}) for r in reps),
        "heartbeats_generated": sum(r.get("heartbeats_generated", 0) for r in reps),
        "heartbeats_acked": sum(r.get("heartbeats_acked", 0) for r in reps),
        "loss": lo, "phase_s": [r.get("phase_s") for r in reps],
    }
    agg["connect_success_rate"] = round(agg["initial_connected"] / max(1, agg["provisioned"]), 4)
    agg["reconnect_success_rate"] = round(agg["reconnected"] / max(1, agg["reconnect_attempted"]), 4)
    if rec:
        agg["reconnect_s"] = {"p50": percentile(rec, 50), "p95": percentile(rec, 95), "max": percentile(rec, 100)}
    return agg


def _sum_dicts(ds) -> dict:
    out: dict = {}
    for d in ds:
        for k, v in d.items():
            out[k] = out.get(k, 0) + v
    return out


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--params")
    ap.add_argument("--devices", type=int)
    ap.add_argument("--per-task", type=int, default=100)
    ap.add_argument("--outage-s", type=float, default=10)
    ap.add_argument("--strategies", nargs="+", default=["jitter", "immediate"])
    ap.add_argument("--report", default="out/loadtest_aws.json")
    args = ap.parse_args()
    require_paid("The load test")
    ctx = Ctx(args.params)
    q = read_quotas(ctx)
    p = plan(ctx, q, args.devices, args.per_task)
    print(mask_text(f"quotas (applied values): {q}"))
    print(f"plan: {p}")
    runs = [run_one(ctx, p, s, args.outage_s) for s in args.strategies]
    for r in runs:
        print(mask_text(f"  {r['strategy']}: provisioned {r['provisioned']}, connect success {r['connect_success_rate']}, "
                        f"reconnect success {r['reconnect_success_rate']}, reconnect {r.get('reconnect_s')}, "
                        f"loss {r['loss']['loss_rate']}, failures {r['connect_failures']}"))
    write_report(args.report, {"region": ctx.region, "quotas_read": q, "plan": p, "runs": runs})
    sys.exit(0 if all(r["provisioned"] > 0 for r in runs) else 1)


if __name__ == "__main__":
    main()
