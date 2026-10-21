# Security notes

Least-privilege summary, cdk-nag acknowledgements, trust-policy conditions and known risks. The
threat analysis is in [THREAT_MODEL.md](THREAT_MODEL.md).

## Permissions by role

| Role | Allowed | Scope |
|---|---|---|
| `<prefix>-fn-upload-url` | `s3:PutObject` (signs presigned POSTs), `dynamodb:PutItem/UpdateItem`, `iot:Publish`, `iot:DescribeEndpoint` | `uploads/*`; items table; `topic/edge/*/upload/url`; endpoint call has no resource type |
| `<prefix>-fn-dispatch` | `dynamodb:UpdateItem`, `states:StartExecution`, SQS consume (event source) | items table; this state machine; ingest queue |
| `<prefix>-fn-validate-input` | `s3:GetObject`, `s3:PutObject` | `uploads/*` read; `work/*` write |
| `<prefix>-fn-analyze` | `s3:GetObject`, `bedrock:InvokeModel`, `bedrock:ApplyGuardrail` | `work/*`; the inference profile and its three regional foundation-model ARNs; this guardrail |
| `<prefix>-fn-finalize` / `-fallback` | `s3:GetObject`, `s3:PutObject`, `s3:ListBucket` (prefix condition), `dynamodb:UpdateItem`, `iot:Publish`, `iot:DescribeEndpoint` | `work/*` and the manifest; `results/*`, `shelf/*`; `topic/edge/*/results` |
| `<prefix>-fn-provisioning-hook` | logs only | own log group |
| `<prefix>-iot-rules` | `sqs:SendMessage`, `dynamodb:PutItem` | ingest, telemetry, rule-error queues; presence table |
| `<prefix>-provisioning` | AWS managed `AWSIoTThingsRegistration` | used only by this provisioning template (trust condition) |
| `<prefix>-gg-tes` | `s3:GetObject` | `gg-artifacts/*` |
| `<prefix>-task-simulator` | `secretsmanager:GetSecretValue`, `s3:PutObject` | claim secret; `loadtest/*` |
| `<prefix>-task-greengrass` | `secretsmanager:GetSecretValue` | the core secrets |
| CodeBuild role | ECR push/pull, asset read, logs, reports | the two repositories; the source asset; this project |

Device-side permissions are AWS IoT policies, not IAM: `<prefix>-device` (thing policy variable, own
topics), `<prefix>-claim` (provisioning topics only), `<core>-policy` per Greengrass core (literal
names, documented minimal set narrowed to this component and thing).

## Resource policies

- Data bucket: deny `s3:GetObject`/`s3:PutObject` to every principal except the stack's function
  roles, the token-exchange role, the simulator task role and the operator ARNs passed at deploy time
  (`operator_principal_arns`); TLS-only; Block Public Access; SSE-S3; access logs to a separate bucket.
- SQS queues: TLS-only (`enforceSSL`), SSE-SQS.
- SNS alarm topic: TLS-only; `budgets.amazonaws.com` may publish with `aws:SourceAccount` and
  `aws:SourceArn` conditions.
- Lambda permissions for IoT: the hook only for this provisioning template ARN, the upload function only
  for its rule ARN, both with `SourceAccount`.

## Trust-policy conditions

| Role principal | Conditions | Documentation |
|---|---|---|
| `iot.amazonaws.com` (rules role) | `aws:SourceAccount` + `aws:SourceArn` = `arn:aws:iot:<REGION>:<ACCOUNT_ID>:rule/<prefix_>*` | [IoT confused deputy](https://docs.aws.amazon.com/iot/latest/developerguide/cross-service-confused-deputy-prevention.html) |
| `iot.amazonaws.com` (provisioning role) | `SourceAccount` + `SourceArn` = this provisioning template | same page, fleet provisioning example |
| `credentials.iot.amazonaws.com` (TES role) | `SourceAccount` + `SourceArn` = this role alias | same page, credential provider example |
| `codebuild.amazonaws.com` | `SourceAccount` + `SourceArn` = `project/<prefix>-*` | [CodeBuild](https://docs.aws.amazon.com/codebuild/latest/userguide/cross-service-confused-deputy-prevention.html) |
| `ecs-tasks.amazonaws.com` (task roles) | `SourceAccount` + `SourceArn` = `arn:aws:ecs:<REGION>:<ACCOUNT_ID>:*` | [ECS task roles](https://docs.aws.amazon.com/AmazonECS/latest/developerguide/task-iam-roles.html) |
| `vpc-flow-logs.amazonaws.com` | `SourceAccount` + `SourceArn` = `vpc-flow-log/*` | [Flow logs role](https://docs.aws.amazon.com/vpc/latest/userguide/flow-logs-iam-role.html) |
| `lambda.amazonaws.com`, `states.amazonaws.com`, ECS task execution roles, CDK custom-resource roles | none | Not added: no confused-deputy guidance for these trust policies was verified; left unchanged rather than risk a role that cannot be assumed |

## cdk-nag acknowledgements

All are attached to the construct that triggers them (`infra/edge_infra/nag.py`), with finding ids
built from stack values; an entry that matches no construct raises at synth. Both profiles synthesise
with zero unacknowledged findings (`tests/test_infra.py`).

| Rule | Construct | Reason |
|---|---|---|
| IAM5 `<LogGroup>.Arn:*` | each function role | Log streams in the function's own log group (generated stream names) |
| IAM5 `<Bucket>.Arn/<prefix>/*` | function roles, TES role, simulator task role | Object ARNs under one fixed prefix of this stack's bucket |
| IAM5 `Resource::*` | upload-url, finalize, fallback roles | `iot:DescribeEndpoint` has no resource-level permissions |
| IAM5 `topic/edge/*/upload/url`, `topic/edge/*/results` | upload-url, finalize, fallback roles | Replies to the requesting device; thing names are generated; device side is isolated by the policy variable |
| IAM5 `<Fn>.Arn:*`, `Resource::*` | workflow role | CDK LambdaInvoke grant (function versions); X-Ray actions have no resource scoping |
| IAM5 `Resource::*` | task execution roles, CodeBuild role | `ecr:GetAuthorizationToken` has no resource-level permissions |
| IAM5 `s3:GetBucket*`, `GetObject*`, `List*`; report group; log group | CodeBuild role | CDK asset-read grant for the source asset; CodeBuild defaults scoped to this project |
| IAM4 `AWSIoTThingsRegistration` | provisioning role | Documented managed policy for provisioning templates; trust limited to this template |
| ECS2 | both task definitions | Environment holds names only; certificate material comes from Secrets Manager at start-up |
| SMG4 | claim and core secrets | Device certificate material; rotation is a new IoT certificate, not a Secrets Manager rotation |
| SQS3 | telemetry queue, rule-error queue | Measurement sink with short retention; the error queue is itself the destination |
| CB4 | CodeBuild project | Outputs are ECR images (scan on push); customer managed KMS key is in the hardening list |

Not acknowledged but noted: SNS topic without SSE (cdk-nag does not flag it here; DECISIONS D8).

## Known risks

- **R1 Prompt injection** is reduced, not eliminated (THREAT_MODEL T1, known gap `i12-subtle-claim`).
- **R2 Shared claim certificate** allows registration of serials accepted by the hook (T2).
- **R3 Pillow parses untrusted images** (T5).
- **R4 Public subnets**: tasks have public IPs (no inbound rules); use private subnets and VPC endpoints in production.
- **R5 Greengrass container runs as root** with the official entrypoint and sudoers change; components run as `ggc_user`.
- **R6 Fleet provisioning orphans**: if a device obtains a certificate but registration is refused or
  times out, that certificate has no thing or policy and cannot be matched to this project by the
  cleanup script; the drills record provisioning failures so this can be checked (VERIFICATION).
- **R7 Account-wide signals**: AWS/IoT metrics and the cost budget are account-wide.
- **R8 Artifacts are not signed**; Greengrass verifies the recipe digest it computed at component creation.
