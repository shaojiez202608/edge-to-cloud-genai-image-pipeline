import types

import cleanup_iot
import guards
import loadtest_aws


def test_mask_text():
    assert guards.mask_text("arn:aws:iam::" + "1234567890" + "12" + ":role/x 1234567890123") == \
        "arn:aws:iam::111122223333:role/x 1234567890123"


def test_require_paid(monkeypatch):
    monkeypatch.delenv("ALLOW_PAID", raising=False)
    try:
        guards.require_paid("x")
    except SystemExit as e:
        assert "ALLOW_PAID=1" in str(e)
    else:
        raise AssertionError


def test_loadtest_plan_respects_quotas_and_fleet_size():
    ctx = types.SimpleNamespace(params=guards.load_params("config/sandbox.example.json"))
    q = {"max_connections_account": 400, "connect_per_s_account": 100, "fp_create_keys_tps": 10,
         "fp_register_thing_tps": 10, "fargate_vcpu": 4}
    p = loadtest_aws.plan(ctx, q, None, 50)
    assert p["devices"] <= 100 + p["tasks"]  # 25 % of the connection quota (rounded up per task)
    assert p["tasks"] <= 2  # half of the Fargate vCPU quota with 1-vCPU tasks
    assert p["connect_rate_per_task"] * p["tasks"] <= 25 + 1e-6
    p2 = loadtest_aws.plan(ctx, q, 10, 50)
    assert p2["devices"] == 10


def test_pagination_follows_both_token_styles():
    calls = []

    class Fake:
        def list_policies(self, **kw):
            calls.append(kw)
            return {"policies": [1]} | ({"nextMarker": "m"} if "marker" not in kw else {})

        def list_things(self, **kw):
            calls.append(kw)
            return {"things": [1]} | ({"nextToken": "t"} if "nextToken" not in kw else {})

    assert list(cleanup_iot._pages(Fake(), "list_policies", "policies")) == [1, 1]
    assert list(cleanup_iot._pages(Fake(), "list_things", "things")) == [1, 1]
    assert {"marker": "m"} in calls and {"nextToken": "t"} in calls
