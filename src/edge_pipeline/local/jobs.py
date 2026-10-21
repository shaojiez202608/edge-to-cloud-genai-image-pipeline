"""Stand-in for AWS IoT Jobs on the local broker: job executions, ``notify-next``, ``start-next``,
``<jobId>/update``, an exponential rollout evaluated per simulated minute, and abort criteria that
cancel the job and its queued executions. It models the documented configuration fields used by
scripts/rollout_drill_jobs.py so the drill can run offline; timing is simulated, not measured.
"""

from __future__ import annotations

import json
import threading
from dataclasses import dataclass, field

from .broker import LocalBroker

TERMINAL = ("SUCCEEDED", "FAILED", "REJECTED", "TIMED_OUT", "CANCELED", "REMOVED")


@dataclass
class Job:
    job_id: str
    document: dict
    targets: list[str]
    rollout: dict
    abort: dict
    status: str = "IN_PROGRESS"
    executions: dict[str, str] = field(default_factory=dict)  # thing -> status
    released: list[str] = field(default_factory=list)  # things that have been notified
    minute: int = 0
    rate: float = 0.0


class LocalJobs:
    def __init__(self, broker: LocalBroker):
        self.broker = broker
        self.jobs: dict[str, Job] = {}
        self.lock = threading.RLock()
        broker.add_reserved_handler("$aws/things/+/jobs/#", self._on_message)

    def create_job(self, job_id: str, targets: list[str], document: dict, rollout: dict, abort: dict) -> Job:
        with self.lock:
            job = Job(job_id, document, list(targets), rollout, abort)
            job.executions = {t: "QUEUED" for t in targets}
            exp = rollout.get("exponentialRate")
            job.rate = float(exp["baseRatePerMinute"]) if exp else float(rollout.get("maximumPerMinute", len(targets)))
            self.jobs[job_id] = job
            return job

    # ---------------------------------------------------------------- rollout
    def tick(self, job_id: str) -> list[str]:
        """Advance the rollout by one simulated minute; notify newly released devices."""
        with self.lock:
            job = self.jobs[job_id]
            if job.status != "IN_PROGRESS":
                return []
            job.minute += 1
            remaining = [t for t in job.targets if t not in job.released and job.executions[t] == "QUEUED"]
            batch = remaining[: max(1, int(job.rate))]
            job.released += batch
            exp = job.rollout.get("exponentialRate")
            if exp:
                crit = exp.get("rateIncreaseCriteria", {})
                succeeded = sum(1 for s in job.executions.values() if s == "SUCCEEDED")
                notified = len(job.released)
                need_s, need_n = crit.get("numberOfSucceededThings"), crit.get("numberOfNotifiedThings")
                if (need_s and succeeded >= need_s * (job.minute)) or (need_n and notified >= need_n):
                    job.rate = min(job.rate * float(exp["incrementFactor"]),
                                   float(job.rollout.get("maximumPerMinute", 1000)))
        for t in batch:
            self._notify(t)
        return batch

    def _notify(self, thing: str) -> None:
        nxt = self._next_for(thing)
        body = {"execution": {"jobId": nxt.job_id, "status": "QUEUED"}} if nxt else {}
        self.broker.publish_as_service(f"$aws/things/{thing}/jobs/notify-next", json.dumps(body).encode())

    def _next_for(self, thing: str) -> Job | None:
        for job in self.jobs.values():
            if job.status == "IN_PROGRESS" and thing in job.released and job.executions.get(thing) == "QUEUED":
                return job
        return None

    def check_abort(self, job_id: str) -> bool:
        with self.lock:
            job = self.jobs[job_id]
            if job.status != "IN_PROGRESS":
                return job.status == "CANCELED"
            executed = [s for s in job.executions.values() if s in TERMINAL]
            for c in job.abort.get("criteriaList", []):
                kind = c["failureType"]
                failed = [s for s in executed if kind == "ALL" and s in ("FAILED", "REJECTED", "TIMED_OUT")
                          or s == kind]
                if len(executed) >= c["minNumberOfExecutedThings"] and executed and \
                        100.0 * len(failed) / len(executed) >= c["thresholdPercentage"]:
                    job.status = "CANCELED"
                    for t, s in job.executions.items():
                        if s == "QUEUED":
                            job.executions[t] = "CANCELED"
                    return True
            if all(s in TERMINAL for s in job.executions.values()):
                job.status = "COMPLETED"
            return False

    # ---------------------------------------------------------------- device API (reserved topics)
    def _on_message(self, topic: str, client_id: str, payload: bytes) -> None:
        parts = topic.split("/")
        thing = parts[2]
        if client_id not in (thing, "$service"):
            return
        if parts[4:] == ["start-next"]:
            with self.lock:
                job = self._next_for(thing)
                if job:
                    job.executions[thing] = "IN_PROGRESS"
            body = {"execution": {"jobId": job.job_id, "jobDocument": job.document, "status": "IN_PROGRESS"}} \
                if job else {}
            self.broker.publish_as_service(f"$aws/things/{thing}/jobs/start-next/accepted", json.dumps(body).encode())
        elif len(parts) == 6 and parts[5] == "update":
            try:
                status = json.loads(payload).get("status")
            except ValueError:
                status = None
            with self.lock:
                job = self.jobs.get(parts[4])
                ok = job is not None and job.executions.get(thing) == "IN_PROGRESS" and status in TERMINAL
                if ok:
                    job.executions[thing] = status
            suffix = "accepted" if ok else "rejected"
            self.broker.publish_as_service(f"$aws/things/{thing}/jobs/{parts[4]}/update/{suffix}", b"{}")

    def summary(self, job_id: str) -> dict:
        job = self.jobs[job_id]
        counts: dict[str, int] = {}
        for s in job.executions.values():
            counts[s] = counts.get(s, 0) + 1
        return {"job_id": job_id, "status": job.status, "minutes": job.minute, "released": len(job.released),
                "executions": counts}
