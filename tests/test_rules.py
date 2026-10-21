import json

from edge_pipeline import rules

R = {r.name: r for r in rules.device_rules("proto-edge")}
A = "proto-edge-sim-000001"


def test_sql_requires_topic_identity_equals_client_id():
    sql = R["image_uploaded"].sql()
    assert "FROM 'edge/+/images/uploaded'" in sql
    assert "WHERE topic(2) = clientid() AND startswith(topic(2), 'proto-edge-')" in sql
    assert "topic(2) AS thing_name" in sql


def test_local_apply_uses_topic_not_payload_identity():
    out = R["image_uploaded"].apply(f"edge/{A}/images/uploaded", A,
                                    json.dumps({"image_id": "a" * 32, "thing_name": "proto-edge-sim-000002"}).encode(), 5)
    assert out["thing_name"] == A and out["image_id"] == "a" * 32 and out["received_ms"] == 5


def test_local_apply_rejects_mismatch_and_foreign_prefix():
    assert R["heartbeat"].apply(f"edge/{A}/telemetry/heartbeat", "proto-edge-sim-000002", b"{}", 1) is None
    assert R["heartbeat"].apply("edge/other-sim-1/telemetry/heartbeat", "other-sim-1", b"{}", 1) is None
    assert R["presence"].apply("$aws/events/presence/connected/someone", "$service",
                               json.dumps({"clientId": "someone"}).encode(), 1) is None


def test_missing_fields_omitted_and_bad_json_tolerated():
    out = R["upload_request"].apply(f"edge/{A}/upload/request", A, b"not json", 1)
    assert out == {"thing_name": A}


def test_topic_matching():
    assert rules.topic_matches("edge/+/results", "edge/x/results")
    assert not rules.topic_matches("edge/+/results", "edge/x/y/results")
    assert rules.topic_matches("$aws/things/+/jobs/#", "$aws/things/t/jobs/x/update")


def test_rule_names_are_valid():
    for r in rules.device_rules("proto-edge"):
        name = rules.deployed_rule_name("proto-edge", r)
        assert name.replace("_", "").isalnum()
