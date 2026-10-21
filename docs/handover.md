# Handover

For the team that takes this prototype into its own non-production account.

## What you get

A deployable reference (README "Deploy"), a local simulation mode, drills for device access, the image
pipeline, cross-device denial, staged rollout/rollback and reconnect storms, a synthetic prompt-injection
test set, and the documents: architecture with fact check, threat model, security notes, decisions,
open questions, verification and deployment log.

## Profiles and removal policy

| | Sandbox (`config/sandbox.example.json`) | Handover (`config/handover.example.json`) |
|---|---|---|
| `removal_policy` | `destroy`: buckets (auto-emptied), tables, repositories, secrets go with the stack | `retain`: buckets and tables survive stack deletion; delete them deliberately |
| Retention | short | longer for results and shelf |
| Budget | enabled (sandbox-only limit) | disabled: agree a budget and recipients with the account owner |
| Greengrass cores | 3 | 0 until a drill is planned |
| Tag `Teardown` | `after-test` | `manual-review` |

Change values only in your copy (`config/handover.json`); validation runs at synth.

## Operating notes

- Identities: `make identities` creates the claim certificate and Greengrass core certificates; to
  revoke a device, set its certificate to `INACTIVE` (`aws iot update-certificate`), then follow the
  cleanup order (detach, delete). Rotating the claim certificate does not affect provisioned devices.
- Model: change `pipeline.model_id` and `deploy.model_regions` together; preflight checks they match.
- Rate limits: `uploads_per_device_per_hour`, `dispatch_max_concurrency`, the Bedrock invocation alarm.
- Releases: adapt `scripts/rollout_drill_*.py` (rollout rates, abort thresholds) to your release policy.
- Logs never contain image content or model text; troubleshoot with reason codes and validation paths.

## Hardening checklist before production

1. **Identity at manufacturing**: replace the shared claim with per-device certificates (your CA with
   JITP/JITR, or provisioning by trusted user); make the pre-provisioning hook check an allow-list.
2. **Network**: private subnets with VPC interface endpoints (IoT data, credentials, Greengrass,
   S3 gateway, ECR, Secrets Manager, CloudWatch Logs) instead of public IPs.
3. **Encryption keys**: customer managed KMS keys for S3, SQS, DynamoDB, Secrets Manager, CodeBuild and
   the alarm topic, with key policies for CloudWatch and Budgets.
4. **Review workflow**: human review for `needs_review` and for new devices/sellers; tighten copy rules
   to an allow-list per category; extend the injection test set with real-world-like samples and other
   languages; repeat evaluations on every model change.
5. **Guardrails**: tune filter strengths on representative data; add denied topics and word filters for
   your catalogue; monitor guardrail interventions.
6. **Artifacts and releases**: sign component artifacts, restrict who can create deployments/jobs,
   require approvals, keep rollout and abort settings in code review.
7. **Monitoring**: alarm subscriptions, per-device anomaly detection (AWS IoT Device Defender),
   enable AWS IoT logging at an appropriate level (account-level setting; agree with the account owner).
8. **Data**: retention per data class, deletion on request, region choice (China regions differ:
   docs/architecture.md), personal data in images (faces, plates) handled before storage.
9. **Dependencies and images**: patch Pillow and the SDKs; scan images (ECR scan on push is on);
   review third-party licences (THIRD-PARTY-LICENSES).
10. **Load**: request quota increases where needed and repeat the load test at the target scale in a
    dedicated account; persistent sessions and shared subscriptions if your devices need them.
11. **Use the handover profile** (retain data) and agree the teardown process with the data owner.
