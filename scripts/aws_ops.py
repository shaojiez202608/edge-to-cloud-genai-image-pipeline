"""Shared AWS helpers for the operation scripts (stack outputs, Fargate task runs, reports)."""

from __future__ import annotations

import json
import time
import uuid

import boto3
from guards import load_params, mask_text, outputs


class Ctx:
    def __init__(self, params_path: str | None):
        self.params = load_params(params_path)
        d = self.params["deploy"]
        self.stack, self.prefix, self.region = d["stack_name"], d["resource_prefix"], d["region"]
        self.session = boto3.session.Session(region_name=self.region)
        self.out = outputs(self.session, self.stack)
        self._iot_endpoint = None

    def client(self, name: str):
        return self.session.client(name)

    @property
    def iot_endpoint(self) -> str:
        if self._iot_endpoint is None:
            self._iot_endpoint = self.client("iot").describe_endpoint(endpointType="iot:Data-ATS")["endpointAddress"]
        return self._iot_endpoint

    def cred_endpoint(self) -> str:
        return self.client("iot").describe_endpoint(endpointType="iot:CredentialProvider")["endpointAddress"]

    # ---------------------------------------------------------------- Fargate
    def run_tasks(self, kind: str, envs: list[dict], started_by: str) -> list[str]:
        ecs = self.client("ecs")
        td = self.out["SimulatorTaskDef" if kind == "simulator" else "GreengrassTaskDef"]
        arns = []
        for env in envs:
            for attempt in range(5):
                r = ecs.run_task(
                    cluster=self.out["Cluster"], taskDefinition=td, launchType="FARGATE", count=1,
                    startedBy=started_by[:36], platformVersion="LATEST", propagateTags="TASK_DEFINITION",
                    networkConfiguration={"awsvpcConfiguration": {
                        "subnets": self.out["Subnets"].split(","), "securityGroups": [self.out["SecurityGroup"]],
                        "assignPublicIp": "ENABLED"}},
                    overrides={"containerOverrides": [{"name": kind, "environment": [
                        {"name": k, "value": str(v)} for k, v in env.items()]}]})
                if r["tasks"]:
                    arns.append(r["tasks"][0]["taskArn"])
                    break
                print(mask_text(f"run_task failure: {r.get('failures')}"))
                time.sleep(5 * (attempt + 1))
        return arns

    def wait_tasks(self, arns: list[str], timeout_s: int) -> dict:
        ecs = self.client("ecs")
        deadline = time.time() + timeout_s
        status: dict = {}
        while time.time() < deadline:
            status = {}
            for i in range(0, len(arns), 100):
                for t in ecs.describe_tasks(cluster=self.out["Cluster"], tasks=arns[i:i + 100])["tasks"]:
                    c = t["containers"][0]
                    status[t["taskArn"]] = {"last": t["lastStatus"], "exit": c.get("exitCode"),
                                            "reason": t.get("stoppedReason", "")[:120]}
            if status and all(s["last"] == "STOPPED" for s in status.values()):
                break
            time.sleep(10)
        return status

    def stop_tasks(self, started_by: str | None = None) -> int:
        ecs = self.client("ecs")
        n = 0
        for page in ecs.get_paginator("list_tasks").paginate(cluster=self.out["Cluster"], desiredStatus="RUNNING"):
            arns = page["taskArns"]
            if not arns:
                continue
            for t in ecs.describe_tasks(cluster=self.out["Cluster"], tasks=arns)["tasks"]:
                if started_by is None or t.get("startedBy") == started_by[:36]:
                    ecs.stop_task(cluster=self.out["Cluster"], task=t["taskArn"], reason="drill finished")
                    n += 1
        return n

    def reports(self, run_id: str) -> list[dict]:
        s3 = self.client("s3")
        out = []
        for page in s3.get_paginator("list_objects_v2").paginate(Bucket=self.out["Bucket"], Prefix=f"loadtest/{run_id}/"):
            for o in page.get("Contents", []):
                out.append(json.loads(s3.get_object(Bucket=self.out["Bucket"], Key=o["Key"])["Body"].read()))
        return out

    def drain_queue(self, url: str, idle_rounds: int = 3) -> list[dict]:
        sqs = self.client("sqs")
        msgs, idle = [], 0
        while idle < idle_rounds:
            r = sqs.receive_message(QueueUrl=url, MaxNumberOfMessages=10, WaitTimeSeconds=2)
            batch = r.get("Messages", [])
            if not batch:
                idle += 1
                continue
            idle = 0
            for m in batch:
                try:
                    msgs.append(json.loads(m["Body"]))
                except ValueError:
                    msgs.append({})
            sqs.delete_message_batch(QueueUrl=url, Entries=[
                {"Id": str(i), "ReceiptHandle": m["ReceiptHandle"]} for i, m in enumerate(batch)])
        return msgs


def run_id(kind: str) -> str:
    return f"{kind}-{time.strftime('%Y%m%dT%H%M%S', time.gmtime())}-{uuid.uuid4().hex[:4]}"
