"""Fleet provisioning by claim over MQTT (``$aws/certificates/create/json`` then
``$aws/provisioning-templates/<template>/provision/json``). The new private key exists only in the
simulator's memory; the claim certificate can only reach these topics (policy.claim_policy) and the
pre-provisioning hook decides whether a serial number may register.
"""

from __future__ import annotations

import asyncio
import json
from dataclasses import dataclass

from .transport import Transport, decode

CREATE = "$aws/certificates/create/json"


@dataclass
class Identity:
    thing: str
    certificate_id: str
    cert_pem: str
    key_pem: str


class ProvisioningFailed(Exception):
    def __init__(self, code: str):
        super().__init__(code)
        self.code = code


async def provision(t: Transport, template: str, serial: str, timeout_s: float = 20) -> Identity:
    loop = asyncio.get_running_loop()
    waiters: dict[str, asyncio.Future] = {}
    prov = f"$aws/provisioning-templates/{template}/provision/json"

    def on(name: str):
        def handler(topic: str, payload: bytes) -> None:
            fut = waiters.get(name)
            if fut is not None and not fut.done():
                fut.set_result((topic.rsplit("/", 1)[-1], decode(payload)))
        return handler

    await t.connect()
    try:
        for name, base in (("create", CREATE), ("provision", prov)):
            for suffix in ("accepted", "rejected"):
                r = await t.subscribe(f"{base}/{suffix}", on(name))
                if r != "granted":
                    raise ProvisioningFailed(f"subscribe_{name}_{r}")
        waiters["create"] = loop.create_future()
        if await t.publish(CREATE, b"{}") != "success":
            raise ProvisioningFailed("publish_create")
        kind, body = await asyncio.wait_for(waiters["create"], timeout_s)
        if kind != "accepted":
            raise ProvisioningFailed(f"create_rejected_{body.get('statusCode', 'x')}")
        waiters["provision"] = loop.create_future()
        req = {"certificateOwnershipToken": body["certificateOwnershipToken"],
               "parameters": {"SerialNumber": serial}}
        if await t.publish(prov, json.dumps(req).encode()) != "success":
            raise ProvisioningFailed("publish_provision")
        kind2, body2 = await asyncio.wait_for(waiters["provision"], timeout_s)
        if kind2 != "accepted":
            raise ProvisioningFailed(f"provision_rejected_{body2.get('statusCode', 'x')}")
        return Identity(body2["thingName"], body["certificateId"], body["certificatePem"], body["privateKey"])
    except TimeoutError:
        raise ProvisioningFailed("timeout") from None
    finally:
        await t.disconnect()
