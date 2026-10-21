# Demo script

All data is synthetic. Nothing in the demo shows customer information. One-command entry points:

| Purpose | Command |
|---|---|
| Offline demo (default, no AWS) | `./scripts/demo.sh` (same as `make demo`) |
| Cloud demo (deployed sandbox, paid) | `ALLOW_PAID=1 ./scripts/demo.sh --cloud` |
| Reset the offline demo output | `./scripts/demo.sh --reset` |
| Reset the cloud demo | `make destroy && make verify-cleanup`, then `make deploy` |

## Audience and length

- **10 minutes** (decision makers, solution architects): architecture slide → D2 → D3 → D6 → D4 summary.
  Use the offline path, or reports from a cloud run made beforehand.
- **30 minutes** (engineering teams): all six scenarios, with the AWS console views listed below and the
  code for policy, validation and the workflow.

## Preparation checklist

1. `make init test` on the demo laptop (the offline path needs nothing else).
2. For the cloud path, the day before: `make preflight deploy`, then `ALLOW_PAID=1 make verify
   verify-rollout verify-loadtest` once to warm up images and identities and keep the reports in `out/`.
3. `make eval-data` and open two images from `sample-data/injection/` (`b01-mug.png`, `i01-url-promo.png`).
4. Console tabs: IoT Core → MQTT test client (subscribe `edge/+/telemetry/heartbeat`), IoT → Jobs,
   Greengrass → Core devices, Step Functions → `proto-edge-workflow`, S3 → `shelf/manifest.json`,
   CloudWatch → dashboard `proto-edge-pipeline`.
5. Fallback: if the network or account is unavailable, use the offline path; every scenario has an
   offline equivalent.

## Steps

Each step: action → expected result → talking points → if it fails.

### 0. Architecture (2 min)
- Action: show the diagram in the README.
- Talking points: identity from the certificate only; images by presigned POST; queue in front of the
  workflow; model output is untrusted; two layers of devices.

### D1 Device access (3 min)
- Action: offline `./scripts/demo.sh` (first summary line), or cloud `make verify` and the MQTT test client.
- Expected: devices connected, heartbeats routed, presence rows (`proto-edge-presence` table).
- Talking points: fleet provisioning by claim — the private key never leaves the device; the
  pre-provisioning hook restricts serials; lifecycle events are on by default.
- If it fails: provisioning failures in the task log → `make identities`; serial outside `max_fleet_size`.

### D2 From shutter to shelf (4 min)
- Action: show `out/e2e_aws.json` → `D2` checks, the Step Functions execution graph, `shelf/manifest.json`
  and one shelf image.
- Expected: `listed` items with a shelf image and draft copy; one execution per image.
- Talking points: the background tidy-up is a deterministic crop from a validated box; copy is
  published only when all checks pass; fallback results are stored, never dropped.
- If it fails: `fallback` with `model_error` → model access; show the offline result instead.

### D3 Cross-device attempt (3 min)
- Action: show the D3 lines (all `not_authorized`), the CloudWatch `PublishIn.AuthError` metric, and the
  revocation probe (INACTIVE/REVOKED certificate refused).
- Talking points: one policy for every device thanks to `${iot:Connection.Thing.ThingName}`; rules also
  require topic == client id; presigned POST pinned to one key.
- If it fails: show `tests/test_policy.py` and the offline D3 line (same policy document).

### D4 Staged rollout and rollback (6 min)
- Action: `ALLOW_PAID=1 make verify-rollout` before the session; show `out/rollout_jobs_aws.json` and
  `out/rollout_gg_aws.json`, the job page (status CANCELED, abort reason) and the Greengrass core
  devices' installed component versions.
- Expected: bad release fails on the first devices, they stay on the old version, the abort stops the
  rollout; the good release reaches every device.
- Talking points: health check = component self-test; ROLLBACK is per device, the abort protects the
  fleet; queued devices never saw the bad version.
- If it fails: the offline D4 line shows the same mechanism with the Jobs stand-in.

### D5 Reconnect storm (6 min)
- Action: show `out/loadtest_aws.json` (quota values read, plan, per-strategy results) and the
  dashboard's connection widget for the run window.
- Expected: connect success rate, reconnect p50/p95/max, message loss for jitter vs immediate.
- Talking points: the scale comes from Service Quotas at run time; immediate reconnects cause retries
  and throttles; full jitter spreads the load. Numbers describe this run only.
- If it fails: offline D5 lines (simulated connect limit) show the herd effect.

### D6 Prompt injection (6 min)
- Action: open `i01-url-promo.png`; show the D6 outcomes (offline: mock model that obeys the
  instructions; cloud: the real model) and the negative control.
- Expected: injected cases end on `fallback` or `needs_review`; nothing injected is published; the
  negative control (validation disabled) reports failures; `i12-subtle-claim` is a known gap.
- Talking points: defence in depth — metadata stripped, forced tool, schema and content rules,
  Guardrails; honest gaps.
- If it fails: show the offline result and `tests/test_validation.py`.

## After the demo

`make destroy` then `make verify-cleanup` (cloud path). The offline path leaves only `out/demo_local.json`
(`./scripts/demo.sh --reset`).
