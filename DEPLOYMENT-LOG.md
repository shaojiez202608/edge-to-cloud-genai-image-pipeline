# Deployment log

Region **us-east-1** only (no other region used). Account shown as `111122223333`; all command output was
masked before being written. Credentials: environment variables of an IAM user in a disposable sandbox
account. Stack: **proto-edge-pipeline**. Parameter file: copy of `config/sandbox.example.json`. Date:
**2026-10-21** (times UTC).

## Timeline

| Time | Step | Result |
|---|---|---|
| 02:21–02:22 | `make deploy` #1 | Stack creation rolled back: IoT device policy above the 2048-character limit (VERIFICATION T1). CloudFormation removed everything it had created. |
| 02:30 | `make deploy` #2 | Refused by the destructive-change guard (the rolled-back stack would be replaced). Re-run with `ALLOW_DESTRUCTIVE=1` for this project's own failed stack. |
| 02:31–02:37 | `make deploy` #3 | Rolled back: the account had reached its VPC and internet-gateway limits (T2). |
| 02:37–02:41 | `USE_DEFAULT_VPC=1 make deploy` #4 | CREATE_COMPLETE; CodeBuild (ARM) built both images |
| 02:41 | `make identities` | Claim certificate and three Greengrass core identities created; keys stored in Secrets Manager |
| 02:45–05:30 | Development runs and fixes | e2e, Jobs drill, Greengrass drill, load test; fixes T3–T9 deployed as stack updates (every replacement was an ECS task-definition revision or the CodeBuild source, reviewed in the diff, then `ALLOW_DESTRUCTIVE=1`) |
| 05:34 | Deploy of commit **59a9927** | UPDATE_COMPLETE, images rebuilt with tag = source-asset hash |
| 05:37–05:56 | Recorded runs | e2e with the injection set, Greengrass drill, IoT Jobs drill (VERIFICATION.md) |
| 05:57– | Recorded load test | see VERIFICATION.md, D5 |
| 06:46–08:28 | `make destroy` (`CONFIRM_STACK`, `CONFIRM_DATA=yes`) | IoT cleanup removed 1029 things and 2973 certificates (detach, INACTIVE, delete), 3 core devices, 3 component versions; 2 jobs and 4 Greengrass deployments failed on deletion limits (warned, continued); `cdk destroy` succeeded; 2 service-created log groups and 17 bootstrap asset object versions of this project deleted; no secrets left |
| 08:29 | `make verify-cleanup` | 8 residuals: 2 IoT jobs, 4 Greengrass deployments (+2 tag-index entries for the jobs) |
| 08:35 | `scripts/cleanup_iot.py` (second pass, retrying the deletion limits) | 2 jobs and 4 deployments deleted |
| 08:37 | `make verify-cleanup` | **Cleaned up: nothing of this project remains in the checked regions** |

## Resources

Created by CloudFormation: S3 data and access-log buckets; DynamoDB `proto-edge-items`, `proto-edge-presence`;
SQS `proto-edge-ingest`, `-ingest-dlq`, `-telemetry`, `-rule-errors`; seven Lambda functions
`proto-edge-*` with their roles and log groups; Step Functions `proto-edge-workflow`; Bedrock guardrail
`proto-edge-guardrail` and a version; IoT policies `proto-edge-device`, `proto-edge-claim`,
`proto-edge-gg-0{1,2,3}-policy`; thing groups `proto-edge-sim-fleet`, `proto-edge-gg-fleet`; provisioning
template `proto-edge-sim-fleet` and its role; four topic rules `proto_edge_*` and their role; role alias
`proto-edge-gg-tes` and its role; Secrets Manager entries for the claim and core certificates (names under
the `proto-edge/` prefix); security group
`proto-edge-simulators` (in the default VPC, which was not modified); ECS cluster `proto-edge-sim`; task
definitions `proto-edge-simulator`, `proto-edge-greengrass`; ECR `proto-edge-simulator`,
`proto-edge-greengrass`; CodeBuild `proto-edge-images`; SNS `proto-edge-alarms`; five alarms; dashboard;
budget `proto-edge-monthly`.

Created by the scripts: things `proto-edge-sim-*` (fleet provisioning) and `proto-edge-gg-0*`, their
certificates, the claim certificate, IoT jobs `proto-edge-edge-model-*`, Greengrass component versions
`com.example.EdgeModel` 1.0.0/1.1.0/2.0.0 and deployments `proto-edge-*`, Greengrass core devices, Fargate
task runs.

Created by services: Container Insights log group, CDK custom-resource handler log groups.

## Destroy and residual check

Destroy command: `CONFIRM_STACK=proto-edge-pipeline CONFIRM_DATA=yes make destroy`, then a second
`uv run python scripts/cleanup_iot.py --params config/sandbox.json` pass after the residual check listed
IoT jobs and Greengrass deployments that had hit service deletion limits (fixed in the script, commit
"fix(cleanup): ...").

Residual check (`scripts/verify_cleanup.py`, us-east-1, the only region used): stack, S3 buckets and
versions, log groups (including service-created ones), Lambda, SQS, DynamoDB, Step Functions, SNS,
alarms, dashboards, ECR, ECS clusters and task definitions, CodeBuild, Secrets Manager (including
scheduled deletions), Bedrock guardrails, VPCs and security groups, IAM roles, budgets, IoT things,
certificates, policies, thing groups, rules, role aliases, provisioning templates, jobs, Greengrass
deployments, core devices and component versions, tagged resources (confirmed one by one) and this
project's bootstrap asset objects.

Conclusion: **已清理，无残留 (cleaned up, nothing left).** The CDKToolkit stack and the default VPC were
not modified. Fleet-provisioning orphans: the only provisioning failure in the recorded runs was a claim
connection refused before any certificate was created, so no unattributable certificate was produced.
