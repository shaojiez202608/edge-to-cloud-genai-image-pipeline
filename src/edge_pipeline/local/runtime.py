"""Wires the local stand-ins into the same topology as the deployed stack:

    devices --MQTT--> LocalBroker --rules--> upload_url handler / ingest queue / telemetry queue / presence
    ingest queue --pump--> dispatch --> LocalWorkflow (validate_input -> analyze -> finalize | fallback)
"""

from __future__ import annotations

import asyncio
import uuid
from pathlib import Path

from .. import naming, pipeline, policy
from ..config import PipelineConfig
from ..model import MockModel
from ..simulator.device import EdgeBox, ReconnectPolicy
from ..simulator.transport import LocalTransport
from .broker import LocalBroker
from .fakes import BrokerNotifier, LocalItems, LocalObjectStore, LocalQueue, LocalRules, LocalWorkflow
from .jobs import LocalJobs

PREFIX = "proto-edge"


class LocalEnvironment:
    def __init__(self, cfg: PipelineConfig | None = None, *, prefix: str = PREFIX, model=None,
                 out_dir: Path | None = None, validator=None, seed: int = 1):
        self.cfg = cfg or PipelineConfig(model_mode="mock", guardrail_enabled=False)
        self.prefix = prefix
        self.broker = LocalBroker(seed=seed)
        self.objects = LocalObjectStore(out_dir)
        self.items = LocalItems()
        self.ingest = LocalQueue("ingest")
        self.telemetry = LocalQueue("telemetry")
        self.presence: dict[str, dict] = {}
        self.jobs = LocalJobs(self.broker)
        self.deps = pipeline.Deps(self.cfg, prefix, self.objects, self.items, BrokerNotifier(self.broker),
                                  model=model or MockModel(self.cfg, "robust"))
        if validator is not None:  # negative control only: replace the output validator
            self.deps.validator = validator
        self.workflow = LocalWorkflow(self.deps)
        self.deps.start_workflow = self.workflow.start
        self.rules = LocalRules(self.broker, prefix, {
            "lambda:upload_url": lambda out: pipeline.issue_upload(self.deps, out),
            "sqs:ingest": self.ingest.send,
            "sqs:telemetry": self.telemetry.send,
            "dynamodb:presence": lambda out: self.presence.__setitem__(out["thing_name"], out),
        })
        self.device_policy = policy.device_policy("aws", self.broker.region, self.broker.account)

    # ---------------------------------------------------------------- provisioning
    def provision(self, kind: str, serial: str) -> tuple[str, str]:
        """Register a thing and an ACTIVE certificate with the device policy. Returns (thing, cert_id)."""
        thing = naming.thing_name(self.prefix, kind, serial)
        cert_id = uuid.uuid4().hex
        self.broker.register(cert_id, thing, [self.device_policy])
        return thing, cert_id

    def device(self, serial: str, *, run_id: str = "", reconnect: ReconnectPolicy | None = None,
               client_id: str | None = None, cert_id: str | None = None) -> EdgeBox:
        thing, cid = self.provision("sim", serial)
        t = LocalTransport(self.broker, self.objects, cert_id or cid, client_id or thing)
        return EdgeBox(thing, t, run_id=run_id, seed=int(serial) if serial.isdigit() else 0, reconnect=reconnect)

    # ---------------------------------------------------------------- processing
    def pump(self) -> int:
        n = 0
        for msg in self.ingest.drain():
            pipeline.dispatch(self.deps, msg)
            n += 1
        return n

    async def pump_forever(self, interval_s: float = 0.02) -> None:
        while True:
            self.pump()
            await asyncio.sleep(interval_s)

    def shelf_manifest(self) -> dict:
        import json

        raw, _ = self.objects.get_versioned(naming.SHELF_MANIFEST_KEY)
        return json.loads(raw) if raw else {"items": []}

    def result(self, thing: str, image_id: str) -> dict | None:
        import json

        raw, _ = self.objects.get_versioned(naming.result_key(thing, image_id))
        return json.loads(raw) if raw else None
