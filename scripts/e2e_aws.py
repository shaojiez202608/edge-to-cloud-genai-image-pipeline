"""Cloud end-to-end check (paid: ALLOW_PAID=1). Runs the e2e simulator scenario as a Fargate task and
then verifies, from the operator side:

  D1  devices provisioned by claim, connected, heartbeat routed to the telemetry queue, presence rows
  D2  benign uploads processed by the workflow, result files, shelf manifest entries
  D3  cross-device publish/subscribe attempts refused (MQTT 5 reason codes) and the AWS/IoT AuthError
      metrics; payload spoofing is impossible because rules take identity from topic == client id
  D6  (with --injection) the synthetic prompt-injection set against the real model
  plus input checks (type spoof, oversize request), the asynchronous workflow executions, and a
  certificate revocation probe run from this workstation (ACTIVE -> INACTIVE -> REVOKED).

    ALLOW_PAID=1 uv run python scripts/e2e_aws.py [--params FILE] [--injection] [--report out/e2e_aws.json]
"""

from __future__ import annotations

import argparse
import asyncio
import datetime as dt
import json
import sys
import time

from aws_ops import Ctx, run_id
from guards import mask_text, require_paid, write_report

from edge_pipeline import naming, synthetic


def check(results: list, name: str, ok: bool, detail=None) -> None:
    results.append({"check": name, "ok": bool(ok), "detail": detail})
    print(mask_text(f"  {'PASS' if ok else 'FAIL'}  {name}" + (f"  {json.dumps(detail, default=str)[:300]}" if detail is not None else "")))


def revocation_probe(ctx: Ctx) -> dict:
    """Create a throw-away device identity, connect, deactivate, connect again, revoke, clean up."""
    from edge_pipeline.simulator.transport import AwsIotTransport, ConnectError

    iot = ctx.client("iot")
    thing = f"{ctx.prefix}-sim-rev{int(time.time()) % 100000:05d}"
    out: dict = {"thing": thing}
    c = iot.create_keys_and_certificate(setAsActive=True)
    arn, cid = c["certificateArn"], c["certificateId"]
    try:
        iot.create_thing(thingName=thing)
        iot.attach_policy(policyName=ctx.out["DevicePolicy"], target=arn)

        async def connect(client_id: str) -> str:
            t = AwsIotTransport(ctx.iot_endpoint, c["certificatePem"], c["keyPair"]["PrivateKey"], client_id)
            try:
                await t.connect(timeout_s=15)
                await t.disconnect()
                return "connected"
            except ConnectError as exc:
                return exc.code

        out["cert_not_attached_to_thing"] = asyncio.run(connect(thing))  # IsAttached condition
        iot.attach_thing_principal(thingName=thing, principal=arn)
        time.sleep(5)
        out["active"] = asyncio.run(connect(thing))
        out["active_other_client_id"] = asyncio.run(connect(f"{ctx.prefix}-sim-000001"))
        iot.update_certificate(certificateId=cid, newStatus="INACTIVE")
        time.sleep(10)
        out["inactive"] = asyncio.run(connect(thing))
        iot.update_certificate(certificateId=cid, newStatus="REVOKED")
        time.sleep(5)
        out["revoked"] = asyncio.run(connect(thing))
    finally:
        try:
            iot.detach_thing_principal(thingName=thing, principal=arn)
        except Exception:  # noqa: BLE001
            out["cleanup_detach_thing"] = "skipped"
        iot.detach_policy(policyName=ctx.out["DevicePolicy"], target=arn)
        if iot.describe_certificate(certificateId=cid)["certificateDescription"]["status"] == "ACTIVE":
            iot.update_certificate(certificateId=cid, newStatus="INACTIVE")
        for _ in range(10):  # detach is eventually consistent
            try:
                iot.delete_certificate(certificateId=cid)
                break
            except iot.exceptions.DeleteConflictException:
                time.sleep(3)
        try:
            iot.delete_thing(thingName=thing)
        except iot.exceptions.ResourceNotFoundException:
            pass
        out["cleaned_up"] = True
    return out


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--params")
    ap.add_argument("--injection", action="store_true", help="also run the synthetic prompt-injection set")
    ap.add_argument("--report", default="out/e2e_aws.json")
    args = ap.parse_args()
    require_paid("The end-to-end check")
    ctx = Ctx(args.params)
    rid = run_id("e2e")
    started = dt.datetime.now(dt.UTC)
    results: list = []
    print(f"== e2e task (run {rid})")
    arns = ctx.run_tasks("simulator", [{"SCENARIO": "e2e", "RUN_ID": rid, "TASK_INDEX": "90",
                                        "IOT_ENDPOINT": ctx.iot_endpoint, "INJECTION_SET": "1" if args.injection else "0"}],
                         started_by=rid)
    status = ctx.wait_tasks(arns, timeout_s=3600 if args.injection else 1200)
    check(results, "task finished", all(s["exit"] == 0 for s in status.values()), list(status.values()))
    reps = ctx.reports(rid)
    rep = reps[0] if reps else {}
    if not rep or "error" in rep:
        check(results, "task report", False, rep)
        write_report(args.report, {"run_id": rid, "results": results, "report": rep})
        sys.exit(1)
    a, b = rep["devices"]

    # D1
    check(results, "D1 both devices provisioned by claim and connected", rep["connected"] == [True, True])
    check(results, "D1 heartbeat acknowledged (QoS 1)", rep["heartbeat"] == "success")
    beats = [m for m in ctx.drain_queue(ctx.out["TelemetryQueueUrl"]) if m.get("run_id") == rid]
    check(results, "D1 heartbeat routed by the rule to the telemetry queue", any(m.get("thing_name") == a for m in beats),
          len(beats))
    ddb = ctx.client("dynamodb")
    pres = ddb.get_item(TableName=ctx.out["PresenceTable"], Key={"thing_name": {"S": a}}).get("Item", {})
    check(results, "D1 presence row from lifecycle events", bool(pres), {k: list(v.values())[0] for k, v in pres.items()})

    # D2
    s3 = ctx.client("s3")
    processed = [r for r in rep["benign"] if r.get("stage") == "processed"]
    check(results, "D2 benign uploads processed", len(processed) == len(rep["benign"]),
          [(r.get("status"), r.get("reason_codes")) for r in rep["benign"]])
    manifest = json.loads(s3.get_object(Bucket=ctx.out["Bucket"], Key=naming.SHELF_MANIFEST_KEY)["Body"].read())
    ids = {i["image_id"] for i in manifest["items"]}
    shelved = [r for r in processed if r.get("status") in ("listed", "needs_review")]
    check(results, "D2 shelf manifest lists the published images", all(r["image_id"] in ids for r in shelved),
          {"published": len(shelved), "manifest_items": len(ids)})
    for r in processed:
        res = json.loads(s3.get_object(Bucket=ctx.out["Bucket"], Key=naming.result_key(a, r["image_id"]))["Body"].read())
        check(results, f"D2 result file {r['image_id'][:8]}", res["status"] == r["status"],
              {"status": res["status"], "decision": res["decision"], "codes": res["reason_codes"]})
    sfn = ctx.client("stepfunctions")
    execs = [sfn.describe_execution(executionArn=f"{ctx.out['StateMachineArn'].replace(':stateMachine:', ':execution:')}:{r['image_id']}")
             for r in processed]
    check(results, "async path: one Standard execution per image, all SUCCEEDED",
          all(e["status"] == "SUCCEEDED" for e in execs), [e["status"] for e in execs])

    # input checks
    ts = rep["type_spoof"]
    check(results, "input: declared PNG carrying JPEG bytes ends on fallback (magic_mismatch)",
          ts.get("status") == "fallback" and ts.get("reason_codes") == ["magic_mismatch"], ts)
    check(results, "input: oversize upload request refused before any URL is issued",
          rep["oversize_request"] == {"stage": "request", "code": "size_out_of_range"}, rep["oversize_request"])

    # D3
    probe = rep["cross_device_probe"]
    check(results, "D3 every cross-device publish/subscribe refused", set(probe.values()) == {"not_authorized"}, probe)
    time.sleep(90)  # AWS/IoT metrics are published per minute
    cw = ctx.client("cloudwatch")
    metrics = {}
    for m in ("PublishIn.AuthError", "Subscribe.AuthError"):
        pts = cw.get_metric_statistics(Namespace="AWS/IoT", MetricName=m, StartTime=started,
                                       EndTime=dt.datetime.now(dt.UTC), Period=60, Statistics=["Sum"])["Datapoints"]
        metrics[m] = sum(p["Sum"] for p in pts)
    check(results, "D3 denials visible in AWS/IoT AuthError metrics (account-wide)", all(v > 0 for v in metrics.values()), metrics)

    # revocation (workstation)
    rev = revocation_probe(ctx)
    check(results, "identity: certificate not attached to a thing cannot connect", rev["cert_not_attached_to_thing"] != "connected", rev)
    check(results, "identity: active certificate connects as its own thing", rev["active"] == "connected")
    check(results, "identity: same certificate with another client id refused", rev["active_other_client_id"] != "connected")
    check(results, "identity: INACTIVE and REVOKED certificates refused",
          rev["inactive"] != "connected" and rev["revoked"] != "connected")

    # D6
    if args.injection:
        rows = {}
        for case in synthetic.CASES:
            r = rep["injection"].get(case.case_id, {})
            res = None
            if r.get("image_id"):
                res = json.loads(s3.get_object(Bucket=ctx.out["Bucket"], Key=naming.result_key(a, r["image_id"]))["Body"].read())
            from edge_pipeline.evaluation import judge
            rows[case.case_id] = judge(case, res)
        counted = [x for x in rows.values() if x["pass"] is not None]
        check(results, "D6 prompt-injection set (real model): expected outcomes", all(x["pass"] for x in counted),
              {k: [v["status"], v["reason_codes"], v["pass"]] for k, v in rows.items()})
        rep["injection_judged"] = rows
    failed = [r["check"] for r in results if not r["ok"]]
    write_report(args.report, {"run_id": rid, "date": started.isoformat(), "region": ctx.region,
                               "model_id": ctx.params["pipeline"]["model_id"], "results": results, "task_report": rep,
                               "revocation": rev, "failed": failed})
    print(f"== {len(results) - len(failed)}/{len(results)} checks passed")
    sys.exit(1 if failed else 0)


if __name__ == "__main__":
    main()
