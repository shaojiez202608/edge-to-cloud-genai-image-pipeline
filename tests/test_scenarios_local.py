"""Demo scenarios D1-D6 in local simulation mode (scripts/local_demo.py uses the same functions)."""

import asyncio

import local_demo

from edge_pipeline.evaluation import evaluate
from edge_pipeline.local.runtime import LocalEnvironment
from edge_pipeline.simulator.device import ReconnectPolicy
from edge_pipeline.simulator.loadtest import loss, percentile, run_load


def with_pump(env, coro):
    return asyncio.run(local_demo._with_pump(env, coro))


def test_d1_to_d3_and_revocation():
    env = LocalEnvironment()
    r = with_pump(env, local_demo.d1_d3(env, 4))
    assert r["D1"] == {"devices": 4, "connected": 4, "online_in_presence_table": 4, "heartbeats_in_telemetry_queue": 4}
    assert r["D2"]["upload"]["status"] == "listed" and r["D2"]["shelf_entry"]
    assert set(r["D3"]["probe"].values()) == {"not_authorized"}
    assert r["D3"]["impersonate_other_client_id"] == "not_authorized"
    assert (r["D3"]["post_other_key_status"], r["D3"]["post_other_size_status"]) == (403, 403)
    assert r["D3"]["denials_logged"] >= 7
    assert r["revocation"] == {"online_after_inactive": False, "reconnect": False}


def test_d4_bad_release_aborts_and_rolls_back_good_release_completes():
    env = LocalEnvironment()
    r = with_pump(env, local_demo.d4(env, 12))
    assert r["bad_release"]["status"] == "CANCELED"
    assert r["bad_release"]["executions"].get("SUCCEEDED", 0) == 0
    assert r["bad_release"]["executions"]["CANCELED"] >= 6  # most devices never received it
    assert r["versions_after_bad"] == {"1.0.0": 12}
    assert r["rolled_back_devices"] == r["bad_release"]["executions"]["FAILED"]
    assert r["good_release"]["status"] == "COMPLETED" and r["versions_after_good"] == {"1.1.0": 12}


def test_d5_storm_metrics_and_loss_accounting():
    env = LocalEnvironment(seed=5)
    boxes = [env.device(f"{i:06d}", run_id="t", reconnect=ReconnectPolicy(base_s=0.05, cap_s=0.5)) for i in range(30)]
    rep = asyncio.run(run_load(boxes, connect_rate_per_s=200, heartbeats_before=2, heartbeats_after=2,
                               heartbeat_interval_s=0.05, outage_s=0.2, reconnect_deadline_s=10))
    assert rep["initial_connect_success_rate"] == 1.0 and rep["reconnect_success_rate"] == 1.0
    lo = loss(rep, env.telemetry.drain(), "t")
    assert lo["lost"] == 0 and lo["generated"] == rep["heartbeats_generated"]


def test_d5_loss_is_detected_when_messages_drop():
    env = LocalEnvironment(seed=9)
    env.broker.drop_rate = 0.3
    boxes = [env.device(f"{i:06d}", run_id="d") for i in range(10)]
    rep = asyncio.run(run_load(boxes, connect_rate_per_s=200, heartbeats_before=3, heartbeats_after=0,
                               heartbeat_interval_s=0.01, storm=False))
    lo = loss(rep, env.telemetry.drain(), "d")
    assert lo["lost"] > 0 and rep["heartbeat_publish_results"].get("lost", 0) > 0


def test_percentile():
    assert percentile([], 50) is None
    assert percentile([1, 2, 3, 4], 100) == 4 and percentile([1, 2, 3], 50) == 2


def test_d6_injection_set_with_naive_and_robust_models():
    for mode in ("naive", "robust"):
        r = evaluate(mode)
        assert r["failed"] == [], (mode, r["failed"])
        assert r["counted"] == r["passed"] == len(r["rows"]) - len(r["known_gaps"])


def test_d6_negative_control_must_fail():
    r = evaluate("naive", negative_control=True)
    assert len(r["failed"]) >= 8  # the evaluation detects a disabled defence


def test_provisioning_connect_failure_is_a_provisioning_failure():
    import pytest

    from edge_pipeline.simulator.provisioning import ProvisioningFailed, provision
    from edge_pipeline.simulator.transport import ConnectError

    class Down:
        on_lost = None

        async def connect(self, timeout_s=10):
            raise ConnectError("AWS_IO_SOCKET_TIMEOUT")

        async def disconnect(self):
            return None

    with pytest.raises(ProvisioningFailed) as e:
        asyncio.run(provision(Down(), "tmpl", "000001"))
    assert e.value.code == "connect_AWS_IO_SOCKET_TIMEOUT"
