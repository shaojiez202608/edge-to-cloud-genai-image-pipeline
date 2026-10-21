"""AWS IoT Core policy documents for devices, plus a small evaluator used by the local broker.

The same functions build the documents deployed by CDK (with CloudFormation tokens for partition,
region and account) and the documents the in-process broker enforces in local mode, so the
cross-device denial tested locally is the policy that is deployed.

Device isolation uses the documented thing policy variable ``${iot:Connection.Thing.ThingName}``:
it resolves to the name of the thing attached to the certificate when the MQTT client id equals the
thing name. No statement grants a resource outside the connecting device's own name space; the only
``*`` is the job-id segment of the device's own AWS IoT Jobs topics (job ids are generated).
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from . import naming

THING_VAR = "${iot:Connection.Thing.ThingName}"  # noqa: S105 - IoT policy variable, not a secret


def _arn(partition: str, region: str, account: str, kind: str, rest: str) -> str:
    return f"arn:{partition}:iot:{region}:{account}:{kind}/{rest}"


def device_policy(partition: str, region: str, account: str) -> dict:
    """Policy attached to every simulated edge box certificate.

    AWS IoT limits a policy document to 2048 characters (found in testing; see the IoT Core quota
    page), so the document lists only the topics the device uses and carries no statement ids.
    """
    a = lambda kind, rest: _arn(partition, region, account, kind, rest)  # noqa: E731
    t = THING_VAR
    jobs = f"$aws/things/{t}/jobs"
    pub = [f"{naming.TOPIC_ROOT}/{t}/{s}" for s in (naming.HEARTBEAT, naming.UPLOAD_REQUEST, naming.IMAGE_UPLOADED)]
    down = [f"{naming.TOPIC_ROOT}/{t}/{s}" for s in (naming.UPLOAD_URL, naming.RESULTS)]
    job_down = [f"{jobs}/notify-next", f"{jobs}/start-next/accepted"]
    return {
        "Version": "2012-10-17",
        "Statement": [
            {"Effect": "Allow", "Action": "iot:Connect", "Resource": a("client", t),
             "Condition": {"Bool": {"iot:Connection.Thing.IsAttached": "true"}}},
            {"Effect": "Allow", "Action": "iot:Publish",
             "Resource": [a("topic", x) for x in pub] + [a("topic", f"{jobs}/start-next"), a("topic", f"{jobs}/*/update")]},
            {"Effect": "Allow", "Action": "iot:Subscribe", "Resource": [a("topicfilter", x) for x in down + job_down]},
            {"Effect": "Allow", "Action": "iot:Receive", "Resource": [a("topic", x) for x in down + job_down]},
        ],
    }


def claim_policy(partition: str, region: str, account: str, prefix: str, template_name: str) -> dict:
    """Policy of the shared fleet-provisioning claim certificate: it can only reach the certificate
    creation and provisioning-template topics. ``client/<prefix>-claim-*`` is a prefix wildcard because
    the claim certificate is shared by design; the pre-provisioning hook validates each request."""
    a = lambda kind, rest: _arn(partition, region, account, kind, rest)  # noqa: E731
    create = "$aws/certificates/create/json"
    prov = f"$aws/provisioning-templates/{template_name}/provision/json"
    replies = [f"{create}/accepted", f"{create}/rejected", f"{prov}/accepted", f"{prov}/rejected"]
    return {
        "Version": "2012-10-17",
        "Statement": [
            {"Sid": "ConnectAsClaimClient", "Effect": "Allow", "Action": "iot:Connect",
             "Resource": a("client", f"{prefix}-claim-*")},
            {"Sid": "RequestCertificateAndRegistration", "Effect": "Allow", "Action": "iot:Publish",
             "Resource": [a("topic", create), a("topic", prov)]},
            {"Sid": "ReceiveReplies", "Effect": "Allow", "Action": "iot:Receive",
             "Resource": [a("topic", r) for r in replies]},
            {"Sid": "SubscribeReplies", "Effect": "Allow", "Action": "iot:Subscribe",
             "Resource": [a("topicfilter", r) for r in replies]},
        ],
    }


# --------------------------------------------------------------------------- local evaluator


@dataclass(frozen=True)
class Principal:
    """What the broker knows from the TLS handshake: the certificate's attached thing (if any)."""

    certificate_id: str
    attached_thing: str | None
    client_id: str


def _variables(p: Principal) -> dict[str, str]:
    # The thing variables resolve only for a certificate attached to a thing whose name equals the
    # client id; otherwise they stay unresolved and the statement cannot match.
    if p.attached_thing and p.attached_thing == p.client_id:
        return {THING_VAR: p.attached_thing}
    return {}


def _pattern(resource: str) -> re.Pattern[str]:
    # IoT policy wildcards: '*' any sequence, '?' one character. MQTT '+'/'#' are literal here.
    out = "".join(".*" if c == "*" else "." if c == "?" else re.escape(c) for c in resource)
    return re.compile(f"^{out}$")


def _conditions_hold(cond: dict | None, p: Principal) -> bool:
    if not cond:
        return True
    for op, kv in cond.items():
        for key, want in kv.items():
            if key == "iot:Connection.Thing.IsAttached" and op == "Bool":
                have = "true" if p.attached_thing else "false"
                if have != str(want).lower():
                    return False
            else:
                return False  # unknown condition: fail closed
    return True


def is_allowed(policies: list[dict], p: Principal, action: str, resource_arn: str) -> bool:
    """Default deny; an explicit Deny wins; an Allow needs a matching action, resource and condition."""
    variables = _variables(p)
    allowed = False
    for doc in policies:
        for st in doc["Statement"]:
            actions = st["Action"] if isinstance(st["Action"], list) else [st["Action"]]
            if action not in actions and "iot:*" not in actions:
                continue
            resources = st["Resource"] if isinstance(st["Resource"], list) else [st["Resource"]]
            hit = False
            for r in resources:
                if "${" in r:
                    for var, value in variables.items():
                        r = r.replace(var, value)
                    if "${" in r:
                        continue  # unresolved variable never matches
                if _pattern(r).match(resource_arn):
                    hit = True
                    break
            if not hit or not _conditions_hold(st.get("Condition"), p):
                continue
            if st["Effect"] == "Deny":
                return False
            allowed = True
    return allowed
