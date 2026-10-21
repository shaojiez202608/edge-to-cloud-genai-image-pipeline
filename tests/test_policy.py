import json

from edge_pipeline import naming, policy

ARN = "arn:aws:iot:us-east-1:111122223333"
DOC = policy.device_policy("aws", "us-east-1", "111122223333")
A, B = "proto-edge-sim-000001", "proto-edge-sim-000002"
P_A = policy.Principal("cert-a", A, A)


def allowed(p, action, kind, rest):
    return policy.is_allowed([DOC], p, action, f"{ARN}:{kind}/{rest}")


def test_own_topics_allowed():
    assert allowed(P_A, "iot:Connect", "client", A)
    for s in (naming.HEARTBEAT, naming.UPLOAD_REQUEST, naming.IMAGE_UPLOADED):
        assert allowed(P_A, "iot:Publish", "topic", f"edge/{A}/{s}")
    for s in (naming.UPLOAD_URL, naming.RESULTS):
        assert allowed(P_A, "iot:Subscribe", "topicfilter", f"edge/{A}/{s}")
        assert allowed(P_A, "iot:Receive", "topic", f"edge/{A}/{s}")
    assert allowed(P_A, "iot:Publish", "topic", f"$aws/things/{A}/jobs/abc/update")
    assert allowed(P_A, "iot:Subscribe", "topicfilter", f"$aws/things/{A}/jobs/notify-next")


def test_other_device_denied():
    assert not allowed(P_A, "iot:Connect", "client", B)
    assert not allowed(P_A, "iot:Publish", "topic", f"edge/{B}/{naming.HEARTBEAT}")
    assert not allowed(P_A, "iot:Subscribe", "topicfilter", f"edge/{B}/{naming.UPLOAD_URL}")
    assert not allowed(P_A, "iot:Subscribe", "topicfilter", "edge/#")
    assert not allowed(P_A, "iot:Subscribe", "topicfilter", "edge/+/results")
    assert not allowed(P_A, "iot:Receive", "topic", f"edge/{B}/{naming.RESULTS}")
    assert not allowed(P_A, "iot:Publish", "topic", f"$aws/things/{B}/jobs/abc/update")
    # device cannot publish on cloud-to-device topics, even its own
    assert not allowed(P_A, "iot:Publish", "topic", f"edge/{A}/{naming.UPLOAD_URL}")


def test_variable_unresolved_when_client_id_differs_or_cert_unattached():
    impostor = policy.Principal("cert-a", A, B)  # own cert, other client id
    assert not allowed(impostor, "iot:Connect", "client", B)
    assert not allowed(impostor, "iot:Publish", "topic", f"edge/{B}/{naming.HEARTBEAT}")
    unattached = policy.Principal("cert-x", None, A)
    assert not allowed(unattached, "iot:Connect", "client", A)


def test_no_wildcard_outside_own_namespace():
    for st in DOC["Statement"]:
        res = st["Resource"] if isinstance(st["Resource"], list) else [st["Resource"]]
        for r in res:
            tail = r.split(":", 5)[5]
            assert policy.THING_VAR in tail, r
            if "*" in tail:  # only the job-id segment of the device's own job topics
                assert tail.startswith(("topic/$aws/things/${iot:Connection.Thing.ThingName}/jobs/*/",)), r
    assert "iot:*" not in json.dumps(DOC)


def test_explicit_deny_wins():
    deny = {"Version": "2012-10-17", "Statement": [{"Effect": "Deny", "Action": "iot:Publish",
                                                    "Resource": f"{ARN}:topic/edge/*"}]}
    assert not policy.is_allowed([DOC, deny], P_A, "iot:Publish", f"{ARN}:topic/edge/{A}/{naming.HEARTBEAT}")


def test_claim_policy_limited_to_provisioning_topics():
    doc = policy.claim_policy("aws", "us-east-1", "111122223333", "proto-edge", "proto-edge-sim-fleet")
    claim = policy.Principal("claim", None, "proto-edge-claim-000001")
    assert policy.is_allowed([doc], claim, "iot:Connect", f"{ARN}:client/proto-edge-claim-000001")
    assert policy.is_allowed([doc], claim, "iot:Publish", f"{ARN}:topic/$aws/certificates/create/json")
    assert not policy.is_allowed([doc], claim, "iot:Publish", f"{ARN}:topic/edge/{A}/{naming.HEARTBEAT}")
    assert not policy.is_allowed([doc], claim, "iot:Connect", f"{ARN}:client/{A}")


def test_thing_names():
    assert naming.thing_name("proto-edge", "sim", "000001") == A
    for bad in ("../x", "A", "x" * 13):
        try:
            naming.thing_name("proto-edge", "sim", bad)
        except ValueError:
            continue
        raise AssertionError(bad)


def test_device_policy_fits_the_iot_policy_size_limit():
    # Longest commercial region name and a 12-digit account: still below the 2048-character limit.
    doc = policy.device_policy("aws", "ap-southeast-5", "111122223333")
    assert len(json.dumps(doc, separators=(",", ":"))) < 2048
    claim = policy.claim_policy("aws", "ap-southeast-5", "111122223333", "a" * 16, "a" * 16 + "-sim-fleet")
    assert len(json.dumps(claim, separators=(",", ":"))) < 2048
