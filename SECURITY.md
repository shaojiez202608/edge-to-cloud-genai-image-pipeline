# Security

## Reporting a vulnerability

If you discover a potential security issue in this project, notify AWS Security through the
[AWS vulnerability reporting page](https://aws.amazon.com/security/vulnerability-reporting/) or by
email to [aws-security@amazon.com](mailto:aws-security@amazon.com). **Do not open a public GitHub
issue.** Do not include real images, device certificates, personal data, account ids or credentials
in the report.

## What this project deploys

One CloudFormation stack (`infra/edge_infra/`) in a non-production account:

- an S3 data bucket (uploads, sanitized work copies, results, shelf images and manifest, load-test
  reports, Greengrass artifacts) and an access-log bucket;
- DynamoDB tables (issued uploads and per-device counters; device presence);
- SQS queues (ingest with dead-letter queue, telemetry, rule errors);
- seven arm64 Lambda functions and a Step Functions Standard workflow;
- an Amazon Bedrock guardrail and model access through the configured inference profile;
- AWS IoT resources: device, claim and per-core policies, thing groups, a fleet provisioning template
  with a pre-provisioning hook, four topic rules, a Greengrass token-exchange role alias;
- simulator compute: ECS cluster, ARM64 Fargate task definitions, two ECR repositories, an ARM
  CodeBuild project, a security group without inbound rules (and a VPC unless an existing one is used);
- Secrets Manager secrets for the claim and core certificates, CloudWatch alarms, dashboard, an SNS
  topic and an optional AWS Budgets budget.

The scripts additionally create, and `make destroy` removes: things and certificates, IoT jobs,
Greengrass component versions and deployments, Fargate task runs.

There is no public endpoint: devices use AWS IoT Core (mutual TLS) and S3 presigned POSTs; operators
use IAM.

## How data is handled

- Images are untrusted input. They are decoded under limits and re-encoded without metadata before the
  model sees them. Only synthetic images are used; the repository contains none.
- Lifecycle rules expire each data prefix (days in the parameter file); logs have retention.
- Logs and metrics carry identifiers, enums, reason codes, counts and timings only — never image
  content, model text or declared metadata.
- Model requests go to the configured inference profile (default `us.`: US regions only).
- Private keys never leave the simulator process (fleet provisioning) or go straight into Secrets Manager.

## Controls and where they are implemented

| Control | Implementation |
|---|---|
| Per-device isolation with the thing policy variable; client id bound to the certificate's thing | `src/edge_pipeline/policy.py` |
| Identity from topic == client id in every rule; dispatcher claims `(thing, image_id)` once | `src/edge_pipeline/rules.py`, `src/edge_pipeline/store.py` |
| Presigned POST pinned to one key, type and exact size; per-device hourly limit | `src/edge_pipeline/store.py`, `src/edge_pipeline/pipeline.py` |
| Input checks: declared type/size/hash, magic bytes, decode limits, re-encode without metadata | `src/edge_pipeline/image_checks.py` |
| Forced-tool output, schema and content validation, neutral copy for review, fixed reason codes | `src/edge_pipeline/prompts.py`, `src/edge_pipeline/validation.py`, `src/edge_pipeline/pipeline.py` |
| Bedrock Guardrails on the image (input) and the draft copy (output) | `infra/edge_infra/functions.py`, `src/edge_pipeline/guardrail.py` |
| Downstream failures become stored fallback results, never 5xx or silent drops | `src/edge_pipeline/pipeline.py`, `infra/edge_infra/functions.py` |
| Least-privilege roles per function; trust conditions; bucket resource policy | `infra/edge_infra/*.py`, SECURITY-NOTES.md |
| S3 Block Public Access, TLS only, SSE; SQS SSE and TLS; no inbound security-group rules | `infra/edge_infra/data.py`, `infra/edge_infra/compute.py` |
| Fleet provisioning hook; certificate revocation and cleanup | `src/functions/handlers.py`, `scripts/cleanup_iot.py` |
| Staged rollouts with abort and rollback; component health check | `scripts/rollout_drill_*.py`, `greengrass/` |
| Alarms, optional budget, invocation-volume alarm | `infra/edge_infra/observe.py` |
| Parameter validation; preflight; destructive-change and paid-command guards | `infra/edge_infra/params.py`, `scripts/preflight.sh`, `scripts/deploy.sh`, `scripts/guards.py` |
| cdk-nag with per-construct acknowledgements; secret scanning with a canary | `infra/edge_infra/nag.py`, `.github/workflows/ci.yml`, `scripts/secret_canary.sh` |

## Known caveats

See SECURITY-NOTES.md (R1–R8): prompt injection is reduced, not removed; the claim certificate is
shared; Pillow parses untrusted bytes; tasks run in public subnets without inbound rules; Greengrass
runs as root in its container; orphaned fleet-provisioning certificates cannot be attributed; some
signals are account-wide; artifacts are not signed.

## Before production

Follow the hardening checklist in [docs/handover.md](docs/handover.md).

## Cleanup

`make destroy` then `make verify-cleanup` (README, "Clean up"). The residual check exits non-zero if
anything of this project is left.
