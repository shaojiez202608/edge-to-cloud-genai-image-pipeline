"""Staged rollout and rollback drill with AWS IoT Jobs on lightweight simulators (paid: ALLOW_PAID=1).

1. A Fargate task runs N simulated devices with a jobs agent (all start on version 1.0.0).
2. A deliberately broken release (2.0.0: the model fails its self-test) is published as a job with an
   exponential rollout and abort criteria. Devices that receive it fail the health check, stay on
   (roll back to) 1.0.0 and report FAILED; the abort cancels the executions still queued, so the rest
   of the fleet never receives it.
3. A good release (1.1.0) is published with the same rollout and completes.
The report shows execution states per job and the version each device runs at the end.

    ALLOW_PAID=1 uv run python scripts/rollout_drill_jobs.py [--devices 20] [--report out/rollout_jobs_aws.json]
"""

from __future__ import annotations

import argparse
import sys
import time

from aws_ops import Ctx, run_id
from guards import mask_text, require_paid, write_report

from edge_pipeline.simulator.device import job_document

ROLLOUT = {"exponentialRate": {"baseRatePerMinute": 2, "incrementFactor": 2.0,
                               "rateIncreaseCriteria": {"numberOfSucceededThings": 2}},
           "maximumPerMinute": 50}
ABORT = {"criteriaList": [{"failureType": "FAILED", "action": "CANCEL", "thresholdPercentage": 50.0,
                           "minNumberOfExecutedThings": 2}]}


def wait_online(ctx: Ctx, things: list[str], timeout_s: int = 900) -> int:
    ddb = ctx.client("dynamodb")
    deadline = time.time() + timeout_s
    n = 0
    while time.time() < deadline:
        n = 0
        for i in range(0, len(things), 100):
            keys = [{"thing_name": {"S": t}} for t in things[i:i + 100]]
            got = ddb.batch_get_item(RequestItems={ctx.out["PresenceTable"]: {"Keys": keys}})
            n += sum(1 for it in got["Responses"][ctx.out["PresenceTable"]] if it["last_event"]["S"] == "connected")
        if n == len(things):
            break
        time.sleep(15)
    return n


def run_job(ctx: Ctx, job_id: str, things: list[str], doc: dict, timeout_s: int = 900) -> dict:
    iot = ctx.client("iot")
    part, _, region, acct = ctx.out["StateMachineArn"].split(":")[1:5]
    targets = [f"arn:{part}:iot:{region}:{acct}:thing/{t}" for t in things]
    iot.create_job(jobId=job_id, targets=targets, document=__import__("json").dumps(doc),
                   description=f"edge-model {doc['version']} (drill)", targetSelection="SNAPSHOT",
                   jobExecutionsRolloutConfig=ROLLOUT, abortConfig=ABORT,
                   timeoutConfig={"inProgressTimeoutInMinutes": 5},
                   tags=[{"Key": k, "Value": v} for k, v in ctx.params["deploy"]["tags"].items()])
    t0 = time.time()
    timeline = []
    while time.time() - t0 < timeout_s:
        job = iot.describe_job(jobId=job_id)["job"]
        counts = job.get("jobProcessDetails", {})
        snap = {k: counts.get(k, 0) for k in ("numberOfQueuedThings", "numberOfInProgressThings", "numberOfSucceededThings",
                                             "numberOfFailedThings", "numberOfCanceledThings", "numberOfTimedOutThings")}
        timeline.append({"t_s": int(time.time() - t0), "status": job["status"], **snap})
        if job["status"] in ("COMPLETED", "CANCELED", "DELETION_IN_PROGRESS"):
            break
        time.sleep(20)
    execs = {}
    for page in iot.get_paginator("list_job_executions_for_job").paginate(jobId=job_id):
        for e in page["executionSummaries"]:
            st = e["jobExecutionSummary"]["status"]
            execs[st] = execs.get(st, 0) + 1
    return {"job_id": job_id, "final_status": timeline[-1]["status"] if timeline else None,
            "abort_reason": iot.describe_job(jobId=job_id)["job"].get("reasonCode"),
            "executions": execs, "timeline": timeline}


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--params")
    ap.add_argument("--devices", type=int, default=20)
    ap.add_argument("--report", default="out/rollout_jobs_aws.json")
    args = ap.parse_args()
    require_paid("The rollout drill")
    ctx = Ctx(args.params)
    rid = run_id("jobs")
    idx = 50
    things = [f"{ctx.prefix}-sim-{idx:02d}{i:04d}" for i in range(args.devices)]
    arns = ctx.run_tasks("simulator", [{"SCENARIO": "jobs-agent", "RUN_ID": rid, "TASK_INDEX": idx,
                                        "IOT_ENDPOINT": ctx.iot_endpoint, "DEVICES": args.devices,
                                        "PROVISION_RATE": 2, "DURATION_S": 1800}], started_by=rid)
    online = wait_online(ctx, things)
    print(f"devices online: {online}/{len(things)}")
    stamp = time.strftime("%Y%m%d%H%M%S", time.gmtime())
    bad = run_job(ctx, f"{ctx.prefix}-edge-model-2-0-0-{stamp}", things, job_document("2.0.0", healthy=False))
    print(mask_text(f"bad release: {bad['final_status']} {bad['executions']} reason={bad['abort_reason']}"))
    good = run_job(ctx, f"{ctx.prefix}-edge-model-1-1-0-{stamp}", things, job_document("1.1.0"))
    print(mask_text(f"good release: {good['final_status']} {good['executions']}"))
    ctx.stop_tasks(started_by=rid)  # SIGTERM: the agent task stops its loop and writes its report
    status = ctx.wait_tasks(arns, timeout_s=300)
    reps = ctx.reports(rid)
    checks = {
        "bad release canceled by the abort criteria": bad["final_status"] == "CANCELED",
        "no device succeeded with the bad release": bad["executions"].get("SUCCEEDED", 0) == 0,
        "devices that failed rolled back (report FAILED, keep running)": bad["executions"].get("FAILED", 0) >= 1,
        # Executions are created only for devices the rollout reached; the rest never saw the release.
        "part of the fleet never received the bad release": sum(bad["executions"].values()) < len(things),
        "good release completed on every device": good["final_status"] == "COMPLETED"
        and good["executions"].get("SUCCEEDED", 0) == len(things),
    }
    for k, v in checks.items():
        print(f"  {'PASS' if v else 'FAIL'}  {k}")
    bad["devices_never_notified"] = len(things) - sum(bad["executions"].values())
    write_report(args.report, {"run_id": rid, "region": ctx.region, "devices": len(things), "online": online,
                               "rollout": ROLLOUT, "abort": ABORT, "bad": bad, "good": good, "checks": checks,
                               "task_status": list(status.values()), "task_reports": reps})
    sys.exit(0 if all(checks.values()) else 1)


if __name__ == "__main__":
    main()
