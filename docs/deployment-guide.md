# Deployment guide

The commands are in the [README](../README.md#deploy) (single place). This guide explains the
prerequisites, the parameter file and what each step does.

## Prerequisites

- Non-production account; a principal that can create the resources in [SECURITY.md](../SECURITY.md).
  The account id is never written to a file: it comes from your credentials at deploy time.
- The region is bootstrapped for CDK (`CDKToolkit` exists; this project does not modify it).
- Amazon Bedrock access to the configured model (default `us.amazon.nova-2-lite-v1:0`, a US inference
  profile) — `make preflight` checks it and compares the profile's regions with `model_regions`.
- Tools: uv, Python 3.13, Node.js, AWS CLI v2. Docker is not required.

## Parameter file

`make init` copies `config/sandbox.example.json` and `config/handover.example.json` to git-ignored live
files. Select one with `PARAMS=config/<profile>.json`. Validation runs at synth
(`infra/edge_infra/params.py`) and lists every problem at once.

| Key | Meaning | Sandbox | Handover |
|---|---|---|---|
| `deploy.stack_name`, `resource_prefix` | Names; the stack name must start with the prefix (cleanup matches on it) | `proto-edge-pipeline`, `proto-edge` | same |
| `deploy.region`, `availability_zones` | Region and two fixed AZs (synth needs no AWS lookup) | `us-east-1`, a/b | same |
| `deploy.removal_policy` | `destroy` deletes data with the stack; `retain` keeps buckets and tables | `destroy` | `retain` |
| `deploy.log_retention_days`, `data_retention_days` | Log retention; S3 lifecycle per prefix | short | longer results/shelf |
| `deploy.max_fleet_size` | Highest simulator serial the provisioning hook accepts | prototype value | same |
| `deploy.dispatch_max_concurrency` | SQS event-source maximum concurrency in front of the workflow | small | same |
| `deploy.greengrass_core_count` | Number of Greengrass core identities/policies | 3 | 0 (enable for a drill) |
| `deploy.bedrock_invocations_alarm_per_5min` | Alarm threshold on model invocations | prototype value | review |
| `deploy.model_regions` | Regions of the inference profile (IAM lists exactly these) | US profile regions | same |
| `deploy.simulator_task`, `greengrass_task` | Fargate CPU/memory (validated against Fargate task sizes) | sandbox-only values | same |
| `deploy.greengrass` | Nucleus version and SHA-256, official Docker repository commit | pinned | pinned |
| `deploy.budget` | Optional account-wide cost budget notifying the alarm topic; the limit is a sandbox-only deployable value | enabled | disabled |
| `deploy.alarm_email` | Optional e-mail subscription for alarms | null | set with the owner |
| `deploy.tags` | `Project`, `Purpose`, `Teardown` are required | `Teardown=after-test` | `Teardown=manual-review` |
| `pipeline.*` | Model id/mode, tokens, temperature, copy locale, guardrail switch, upload limits, rate limit | | |

Deploy-time context (never in the file): `operator_principal_arns` (added by `scripts/deploy.sh` from
your caller identity; the data bucket denies object access to anyone else), and optionally
`existing_vpc_id` / `existing_subnet_ids` (set by `USE_DEFAULT_VPC=1`).

## What `make deploy` does

1. `scripts/preflight.sh` (read-only): parameter validation, tools, account confirmation (last four
   digits, or `CONFIRM_ACCOUNT`), IoT Core and Greengrass availability, model/profile, CDK bootstrap,
   IoT Core quota values.
2. `scripts/build_bundle.sh`: aarch64 wheels for the Lambda bundle; staged image build context.
3. `cdk diff`; refuses deletions or replacements unless `ALLOW_DESTRUCTIVE=1`.
4. `cdk deploy`; the asset manifest is saved under `.deploy/` for cleanup.
5. `scripts/build_images.py`: starts the ARM CodeBuild project and waits for both images.

Then `make identities` (also run by `make verify`) creates the claim certificate and the Greengrass core
identities; private keys go directly into Secrets Manager.

## Profiles

- **Sandbox** (`config/sandbox.json`): everything destroyable, short retention, budget on, three
  Greengrass cores.
- **Handover** (`config/handover.json`): data retained on stack deletion, longer retention, no budget
  (set one with the account owner), no Greengrass cores until a drill is planned. See
  [handover.md](handover.md).
