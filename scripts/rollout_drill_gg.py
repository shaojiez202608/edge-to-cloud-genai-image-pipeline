"""Staged rollout and automatic rollback drill on AWS IoT Greengrass V2 cores running in ARM64
Fargate tasks (paid: ALLOW_PAID=1). Run scripts/setup_identities.py first.

1. Upload the component artifacts and create com.example.EdgeModel 1.0.0 / 1.1.0 / 2.0.0 (2.0.0
   ships a model that fails the start-up self-test).
2. Start one Fargate task per core and wait until each core device reports HEALTHY.
3. Baseline deployment 1.0.0 to the core thing group.
4. Broken deployment 2.0.0 with failureHandlingPolicy ROLLBACK, an exponential job rollout and abort
   criteria: the first core that gets it fails, rolls back to 1.0.0, and the abort stops the rollout.
5. Good deployment 1.1.0 completes on every core.
Evidence per phase: each core's effective deployment status and installed component version.

    ALLOW_PAID=1 uv run python scripts/rollout_drill_gg.py [--report out/rollout_gg_aws.json]
"""

from __future__ import annotations

import argparse
import json
import sys
import time

from aws_ops import Ctx, run_id
from guards import ROOT, mask_text, require_paid, write_report

COMPONENT = "com.example.EdgeModel"
VERSIONS = {"1.0.0": 0.5, "1.1.0": 0.55, "2.0.0": 0.95}  # model threshold; 0.95 fails the self-test
ROLLOUT = {"exponentialRate": {"baseRatePerMinute": 1, "incrementFactor": 2.0,
                               "rateIncreaseCriteria": {"numberOfSucceededThings": 1}}, "maximumPerMinute": 10}
ABORT = {"criteriaList": [{"failureType": "FAILED", "action": "CANCEL", "thresholdPercentage": 30.0,
                           "minNumberOfExecutedThings": 1}]}


def recipe(bucket: str, version: str) -> dict:
    base = f"s3://{bucket}/gg-artifacts/{COMPONENT}/{version}"
    return {"RecipeFormatVersion": "2020-01-25", "ComponentName": COMPONENT, "ComponentVersion": version,
            "ComponentDescription": "Simulated edge model with a start-up self-test (prototype)",
            "ComponentPublisher": "Example",
            "Manifests": [{"Platform": {"os": "linux"},
                           "Lifecycle": {"Startup": {"Script": "python3 -u {artifacts:path}/selftest.py "
                                                               "{artifacts:path}/model.json", "Timeout": 60}},
                           "Artifacts": [{"URI": f"{base}/model.json"}, {"URI": f"{base}/selftest.py"}]}]}


def ensure_components(ctx: Ctx) -> None:
    s3, gg = ctx.client("s3"), ctx.client("greengrassv2")
    selftest = (ROOT / "greengrass" / COMPONENT / "selftest.py").read_bytes()
    part, _, region, acct = ctx.out["StateMachineArn"].split(":")[1:5]
    tags = ctx.params["deploy"]["tags"]
    for v, thr in VERSIONS.items():
        arn = f"arn:{part}:greengrass:{region}:{acct}:components:{COMPONENT}:versions:{v}"
        try:
            gg.describe_component(arn=arn)
            continue
        except gg.exceptions.ResourceNotFoundException:
            pass
        key = f"gg-artifacts/{COMPONENT}/{v}"
        s3.put_object(Bucket=ctx.out["Bucket"], Key=f"{key}/model.json",
                      Body=json.dumps({"version": v, "threshold": thr}).encode())
        s3.put_object(Bucket=ctx.out["Bucket"], Key=f"{key}/selftest.py", Body=selftest)
        gg.create_component_version(inlineRecipe=json.dumps(recipe(ctx.out["Bucket"], v)).encode(), tags=tags)
        for _ in range(30):
            st = gg.describe_component(arn=arn)["status"]["componentState"]
            if st in ("DEPLOYABLE", "FAILED"):
                break
            time.sleep(5)
        print(f"component {v}: {st}")


def core_state(ctx: Ctx, core: str) -> dict:
    gg = ctx.client("greengrassv2")
    try:
        status = gg.get_core_device(coreDeviceThingName=core)["status"]
    except gg.exceptions.ResourceNotFoundException:
        return {"status": "NOT_REGISTERED"}
    inst = {c["componentName"]: (c["componentVersion"], c["lifecycleState"])
            for c in gg.list_installed_components(coreDeviceThingName=core).get("installedComponents", [])}
    eff = gg.list_effective_deployments(coreDeviceThingName=core).get("effectiveDeployments", [])
    return {"status": status, "edge_model": inst.get(COMPONENT),
            "deployments": {e["deploymentId"]: e["coreDeviceExecutionStatus"] for e in eff}}


def deploy(ctx: Ctx, cores: list[str], name: str, version: str, staged: bool, wait_s: int = 900) -> dict:
    gg = ctx.client("greengrassv2")
    part, _, region, acct = ctx.out["StateMachineArn"].split(":")[1:5]
    group_arn = f"arn:{part}:iot:{region}:{acct}:thinggroup/{ctx.out['GgThingGroup']}"
    kw = {"iotJobConfiguration": {"jobExecutionsRolloutConfig": ROLLOUT, "abortConfig": ABORT,
                                  "timeoutConfig": {"inProgressTimeoutInMinutes": 10}}} if staged else {}
    dep = gg.create_deployment(
        targetArn=group_arn, deploymentName=f"{ctx.prefix}-{name}",
        components={COMPONENT: {"componentVersion": version}},
        deploymentPolicies={"failureHandlingPolicy": "ROLLBACK",
                            "componentUpdatePolicy": {"timeoutInSeconds": 60, "action": "NOTIFY_COMPONENTS"}},
        tags=ctx.params["deploy"]["tags"], **kw)
    did = dep["deploymentId"]
    t0, timeline = time.time(), []
    while time.time() - t0 < wait_s:
        states = {c: core_state(ctx, c) for c in cores}
        per = {c: s["deployments"].get(did, "NOT_RECEIVED") for c, s in states.items()}
        job = gg.get_deployment(deploymentId=did)
        timeline.append({"t_s": int(time.time() - t0), "deployment": job["deploymentStatus"], "cores": per})
        terminal = all(v in ("SUCCEEDED", "FAILED", "CANCELED", "TIMED_OUT", "REJECTED", "NOT_RECEIVED") for v in per.values())
        if job["deploymentStatus"] in ("COMPLETED", "CANCELED", "FAILED") or (terminal and time.time() - t0 > 300):
            break
        time.sleep(20)
    time.sleep(30)
    final = {c: core_state(ctx, c) for c in cores}
    return {"deployment_id": did, "version": version, "staged": staged,
            "deployment_status": gg.get_deployment(deploymentId=did)["deploymentStatus"],
            "per_core": {c: {"execution": s["deployments"].get(did, "NOT_RECEIVED"), "edge_model": s["edge_model"]}
                         for c, s in final.items()},
            "timeline": timeline}


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--params")
    ap.add_argument("--report", default="out/rollout_gg_aws.json")
    args = ap.parse_args()
    require_paid("The Greengrass rollout drill")
    ctx = Ctx(args.params)
    cores = [] if ctx.out["GgCores"] == "-" else ctx.out["GgCores"].split(",")
    if not cores:
        sys.exit("greengrass_core_count is 0 in this parameter file")
    rid = run_id("gg")
    ensure_components(ctx)
    envs = [{"THING_NAME": c, "GG_SECRET_ID": f"{ctx.prefix}/gg/{c}", "IOT_DATA_ENDPOINT": ctx.iot_endpoint,
             "IOT_CRED_ENDPOINT": ctx.cred_endpoint()} for c in cores]
    arns = ctx.run_tasks("greengrass", envs, started_by=rid)
    report: dict = {"run_id": rid, "region": ctx.region, "cores": cores, "rollout": ROLLOUT, "abort": ABORT}
    deadline = time.time() + 900
    while time.time() < deadline:
        st = {c: core_state(ctx, c)["status"] for c in cores}
        if all(v == "HEALTHY" for v in st.values()):
            break
        time.sleep(20)
    report["cores_started"] = st
    print(f"core status: {st}")
    if not all(v == "HEALTHY" for v in st.values()):
        report["tasks"] = list(ctx.wait_tasks(arns, timeout_s=5).values())
        write_report(args.report, report)
        ctx.stop_tasks(started_by=rid)
        sys.exit("cores did not become HEALTHY; see the /<prefix>/ecs/greengrass log group")
    report["baseline"] = deploy(ctx, cores, "baseline-1-0-0", "1.0.0", staged=False)
    print(mask_text(f"baseline: {report['baseline']['per_core']}"))
    report["bad"] = deploy(ctx, cores, "bad-2-0-0", "2.0.0", staged=True)
    print(mask_text(f"bad: {report['bad']['deployment_status']} {report['bad']['per_core']}"))
    report["good"] = deploy(ctx, cores, "good-1-1-0", "1.1.0", staged=True)
    print(mask_text(f"good: {report['good']['deployment_status']} {report['good']['per_core']}"))
    ctx.stop_tasks(started_by=rid)
    bad, good = report["bad"]["per_core"], report["good"]["per_core"]
    checks = {
        "baseline 1.0.0 running on every core": all(
            (v["edge_model"] or [None])[0] == "1.0.0" for v in report["baseline"]["per_core"].values()),
        "bad release failed on at least one core": any(v["execution"] == "FAILED" for v in bad.values()),
        "no core runs the bad version afterwards": all((v["edge_model"] or [None])[0] != "2.0.0" for v in bad.values()),
        "failed cores rolled back to 1.0.0": all((v["edge_model"] or [None])[0] == "1.0.0"
                                               for v in bad.values() if v["execution"] == "FAILED"),
        "good release 1.1.0 running on every core": all((v["edge_model"] or [None])[0] == "1.1.0" for v in good.values()),
    }
    report["checks"] = checks
    for k, v in checks.items():
        print(f"  {'PASS' if v else 'FAIL'}  {k}")
    write_report(args.report, report)
    sys.exit(0 if all(checks.values()) else 1)


if __name__ == "__main__":
    main()
