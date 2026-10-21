"""Create the device identities that CloudFormation cannot create with a private key:

- the fleet-provisioning claim certificate (claim policy attached), stored in <prefix>/claim;
- one thing + certificate + core policy per Greengrass core, stored in <prefix>/gg/<thing>.

Private keys go straight from CreateKeysAndCertificate into Secrets Manager; nothing is written to
disk or printed. Idempotent: existing identities are kept. Simulator identities are not created
here: each simulated device obtains its own certificate through fleet provisioning by claim.

    uv run python scripts/setup_identities.py [--params FILE]
"""

from __future__ import annotations

import argparse
import json

from aws_ops import Ctx


def _has_cert(sm, secret_id: str) -> bool:
    try:
        v = json.loads(sm.get_secret_value(SecretId=secret_id)["SecretString"])
        return isinstance(v, dict) and "certificatePem" in v
    except (ValueError, sm.exceptions.ResourceNotFoundException):
        return False


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--params")
    args = ap.parse_args()
    ctx = Ctx(args.params)
    iot, sm = ctx.client("iot"), ctx.client("secretsmanager")

    claim_secret = ctx.out["ClaimSecret"]
    if _has_cert(sm, claim_secret):
        print("claim certificate: present")
    else:
        c = iot.create_keys_and_certificate(setAsActive=True)
        iot.attach_policy(policyName=ctx.out["ClaimPolicy"], target=c["certificateArn"])
        sm.put_secret_value(SecretId=claim_secret, SecretString=json.dumps(
            {"certificatePem": c["certificatePem"], "privateKey": c["keyPair"]["PrivateKey"],
             "certificateId": c["certificateId"]}))
        print("claim certificate: created and stored")

    cores = [] if ctx.out["GgCores"] == "-" else ctx.out["GgCores"].split(",")
    for thing in cores:
        secret = f"{ctx.prefix}/gg/{thing}"
        try:
            iot.describe_thing(thingName=thing)
        except iot.exceptions.ResourceNotFoundException:
            iot.create_thing(thingName=thing, attributePayload={"attributes": {"kind": "gg"}})
        iot.add_thing_to_thing_group(thingGroupName=ctx.out["GgThingGroup"], thingName=thing)
        if _has_cert(sm, secret) and iot.list_thing_principals(thingName=thing)["principals"]:
            print(f"{thing}: present")
            continue
        c = iot.create_keys_and_certificate(setAsActive=True)
        iot.attach_policy(policyName=f"{thing}-policy", target=c["certificateArn"])
        iot.attach_thing_principal(thingName=thing, principal=c["certificateArn"])
        sm.put_secret_value(SecretId=secret, SecretString=json.dumps(
            {"certificatePem": c["certificatePem"], "privateKey": c["keyPair"]["PrivateKey"],
             "certificateId": c["certificateId"]}))
        print(f"{thing}: created")


if __name__ == "__main__":
    main()
