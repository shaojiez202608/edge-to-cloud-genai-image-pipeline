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
| DESTROY_TIME | `make destroy` | DESTROY_RESULT |
| VERIFY_TIME | `make verify-cleanup` | VERIFY_RESULT |

## Resources

Created by CloudFormation: S3 data and access-log buckets; DynamoDB `proto-edge-items`, `proto-edge-presence`;
SQS `proto-edge-ingest`, `-ingest-dlq`, `-telemetry`, `-rule-errors`; seven Lambda functions
`proto-edge-*` with their roles and log groups; Step Functions `proto-edge-workflow`; Bedrock guardrail
`proto-edge-guardrail` and a version; IoT policies `proto-edge-device`, `proto-edge-claim`,
`proto-edge-gg-0{1,2,3}-policy`; thing groups `proto-edge-sim-fleet`, `proto-edge-gg-fleet`; provisioning
template `proto-edge-sim-fleet` and its role; four topic rules `proto_edge_*` and their role; role alias
`proto-edge-gg-tes` and its role; secrets `proto-edge/claim`, `proto-edge/gg/*`; security group
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

DESTROY_SECTION
