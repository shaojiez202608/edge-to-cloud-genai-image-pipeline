import json

import handlers
import pytest


@pytest.fixture(autouse=True)
def env(monkeypatch):
    monkeypatch.setenv("PREFIX", "proto-edge")
    monkeypatch.setenv("MAX_FLEET_SIZE", "100")


@pytest.mark.parametrize("serial,client,allowed", [
    ("010005", "proto-edge-claim-010005", True),
    ("010005", "proto-edge-claim-010006", False),  # one serial per claim connection
    ("010100", "proto-edge-claim-010100", False),  # beyond the fleet size
    ("../x", "proto-edge-claim-../x", False),
    (None, "proto-edge-claim-", False),
])
def test_provisioning_hook(serial, client, allowed):
    ev = {"clientId": client, "parameters": {"SerialNumber": serial} if serial else {}}
    assert handlers.provisioning_hook(ev, None) == {"allowProvisioning": allowed}


def test_dispatch_partial_batch_failure(monkeypatch):
    calls = []

    def fake(deps, body):
        calls.append(body)
        if body.get("image_id") == "boom":
            raise RuntimeError("throttled")
        return "started"

    monkeypatch.setattr(handlers.pipeline, "dispatch", fake)
    monkeypatch.setattr(handlers, "_deps", lambda: None)
    ev = {"Records": [{"messageId": "1", "body": json.dumps({"image_id": "ok"})},
                      {"messageId": "2", "body": json.dumps({"image_id": "boom"})},
                      {"messageId": "3", "body": "not json"}]}
    assert handlers.dispatch(ev, None) == {"batchItemFailures": [{"itemIdentifier": "2"}]}
    assert len(calls) == 2
