"""Remove what CloudFormation does not own, before `cdk destroy`: running Fargate tasks, Greengrass
deployments / core devices / component versions, IoT jobs, things created by fleet provisioning and
setup_identities.py, and their certificates (detach policies and things, set INACTIVE, then delete).
Everything is matched by this project's prefix and its own policies, groups and component name; all
list APIs are paginated. A failing step prints a warning and the script continues.

    uv run python scripts/cleanup_iot.py [--params FILE] [--dry-run]

``find_residuals`` is also used read-only by scripts/verify_cleanup.py.
"""

from __future__ import annotations

import argparse
import time

import boto3
from guards import load_params, mask_text

COMPONENT = "com.example.EdgeModel"


def _pages(client, op: str, key: str, **kw):
    while True:
        r = getattr(client, op)(**kw)
        yield from r.get(key, [])
        tok = r.get("nextToken") or r.get("nextMarker")
        if not tok:
            return
        kw["nextToken" if "nextToken" in r else "marker"] = tok


def find_residuals(sess, prefix: str) -> dict:
    iot, gg = sess.client("iot"), sess.client("greengrassv2")
    out: dict = {"things": [], "certificates": {}, "jobs": [], "gg_deployments": [], "gg_core_devices": [],
                 "gg_components": [], "policies": [], "thing_groups": [], "role_aliases": [], "provisioning_templates": [],
                 "rules": []}
    for t in _pages(iot, "list_things", "things"):
        if t["thingName"].startswith(f"{prefix}-"):
            out["things"].append(t["thingName"])
    policies = [p["policyName"] for p in _pages(iot, "list_policies", "policies") if p["policyName"].startswith(f"{prefix}-")]
    out["policies"] = policies
    for name in out["things"]:
        for pr in _pages(iot, "list_thing_principals", "principals", thingName=name):
            out["certificates"].setdefault(pr, set()).add(f"thing:{name}")
    for pol in policies:
        for tgt in _pages(iot, "list_targets_for_policy", "targets", policyName=pol):
            if ":cert/" in tgt:
                out["certificates"].setdefault(tgt, set()).add(f"policy:{pol}")
    out["certificates"] = {k: sorted(v) for k, v in out["certificates"].items()}
    for j in _pages(iot, "list_jobs", "jobs"):
        if j["jobId"].startswith(f"{prefix}-") and j.get("status") != "DELETION_IN_PROGRESS":
            out["jobs"].append(j["jobId"])
    groups = {g["groupName"]: g["groupArn"] for g in _pages(iot, "list_thing_groups", "thingGroups")
              if g["groupName"].startswith(f"{prefix}-")}
    out["thing_groups"] = sorted(groups)
    for d in _pages(gg, "list_deployments", "deployments", historyFilter="ALL"):
        if d.get("targetArn") in groups.values() or d.get("deploymentName", "").startswith(f"{prefix}-"):
            out["gg_deployments"].append({"id": d["deploymentId"], "status": d.get("deploymentStatus"),
                                          "jobs_for_group": d.get("targetArn")})
    for c in _pages(gg, "list_core_devices", "coreDevices"):
        if c["coreDeviceThingName"].startswith(f"{prefix}-"):
            out["gg_core_devices"].append(c["coreDeviceThingName"])
    for c in _pages(gg, "list_components", "components", scope="PRIVATE"):
        if c["componentName"] == COMPONENT:
            for v in _pages(gg, "list_component_versions", "componentVersions", arn=c["arn"]):
                out["gg_components"].append(v["arn"])
    for ra in _pages(iot, "list_role_aliases", "roleAliases"):
        if ra.startswith(f"{prefix}-"):
            out["role_aliases"].append(ra)
    for t in _pages(iot, "list_provisioning_templates", "templates"):
        if t["templateName"].startswith(f"{prefix}-"):
            out["provisioning_templates"].append(t["templateName"])
    for r in _pages(iot, "list_topic_rules", "rules"):
        if r["ruleName"].startswith(prefix.replace("-", "_") + "_"):
            out["rules"].append(r["ruleName"])
    return out


def _try(what: str, fn, *a, **kw):
    try:
        return fn(*a, **kw)
    except Exception as exc:  # noqa: BLE001 - warn and continue
        print(mask_text(f"WARNING: {what}: {type(exc).__name__} {str(exc)[:160]} (continuing)"))
        return None


def stop_tasks(sess, prefix: str) -> None:
    ecs = sess.client("ecs")
    cluster = f"{prefix}-sim"
    try:
        arns = [a for page in ecs.get_paginator("list_tasks").paginate(cluster=cluster) for a in page["taskArns"]]
    except Exception:  # noqa: BLE001 - cluster already gone
        return
    for a in arns:
        _try("stop task", ecs.stop_task, cluster=cluster, task=a, reason="cleanup")
    print(f"stopped tasks: {len(arns)}")


def clean(sess, prefix: str, dry_run: bool) -> dict:
    res = find_residuals(sess, prefix)
    print(mask_text(f"found: things={len(res['things'])} certificates={len(res['certificates'])} jobs={len(res['jobs'])} "
                    f"gg_deployments={len(res['gg_deployments'])} gg_cores={len(res['gg_core_devices'])} "
                    f"gg_component_versions={len(res['gg_components'])}"))
    if dry_run:
        return res
    stop_tasks(sess, prefix)
    iot, gg = sess.client("iot"), sess.client("greengrassv2")
    for d in res["gg_deployments"]:
        if d["status"] == "ACTIVE":
            _try("cancel deployment", gg.cancel_deployment, deploymentId=d["id"])
        _try("delete deployment", gg.delete_deployment, deploymentId=d["id"])
    for c in res["gg_core_devices"]:
        _try("delete core device", gg.delete_core_device, coreDeviceThingName=c)
    for arn in res["gg_components"]:
        _try("delete component version", gg.delete_component, arn=arn)
    # Jobs created by the drills and by Greengrass deployments on this project's groups.
    jobs = set(res["jobs"])
    for g in res["thing_groups"]:
        for j in _pages(iot, "list_jobs", "jobs", thingGroupName=g):
            jobs.add(j["jobId"])
    for j in jobs:
        _try("cancel job", iot.cancel_job, jobId=j, force=True)
        _try("delete job", iot.delete_job, jobId=j, force=True)
    for cert_arn, attached in res["certificates"].items():
        cid = cert_arn.rsplit("/", 1)[-1]
        for pol in [p["policyName"] for p in _pages(iot, "list_attached_policies", "policies", target=cert_arn)]:
            _try("detach policy", iot.detach_policy, policyName=pol, target=cert_arn)
        for a in attached:
            if a.startswith("thing:"):
                _try("detach thing", iot.detach_thing_principal, thingName=a[6:], principal=cert_arn)
        _try("deactivate certificate", iot.update_certificate, certificateId=cid, newStatus="INACTIVE")
    time.sleep(5)  # detach operations are eventually consistent
    for cert_arn in res["certificates"]:
        cid = cert_arn.rsplit("/", 1)[-1]
        for attempt in range(6):
            try:
                iot.delete_certificate(certificateId=cid)
                break
            except iot.exceptions.DeleteConflictException:
                time.sleep(5 * (attempt + 1))
            except iot.exceptions.ResourceNotFoundException:
                break
            except Exception as exc:  # noqa: BLE001
                print(mask_text(f"WARNING: delete certificate: {type(exc).__name__} (continuing)"))
                break
    for t in res["things"]:
        _try("delete thing", iot.delete_thing, thingName=t)
    after = find_residuals(sess, prefix)
    print(mask_text(f"left after IoT cleanup: things={len(after['things'])} certificates={len(after['certificates'])} "
                    f"jobs={len(after['jobs'])} gg_cores={len(after['gg_core_devices'])} "
                    f"gg_component_versions={len(after['gg_components'])}"))
    return after


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--params")
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()
    d = load_params(args.params)["deploy"]
    clean(boto3.session.Session(region_name=d["region"]), d["resource_prefix"], args.dry_run)


if __name__ == "__main__":
    main()
