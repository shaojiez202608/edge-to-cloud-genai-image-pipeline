# Cleanup

Commands: README, "Clean up". `make destroy` runs `scripts/cleanup.sh`, which asks twice (stack, then
data). Non-interactive: `CONFIRM_STACK=<stack name> CONFIRM_DATA=yes make destroy`. A failing step
prints a warning and the script continues.

## Order

1. `scripts/cleanup_iot.py`: stop running Fargate tasks; cancel and delete Greengrass deployments;
   delete core devices and component versions; force-cancel and delete IoT jobs (named with the prefix
   or targeting the project's thing groups); for every certificate found through the project's things
   or policies — detach policies, detach things, set `INACTIVE`, then delete (retried while detach
   operations settle); delete the things. All list calls are paginated.
2. `cdk destroy` of the stack (policies, groups, rules, role alias, template, functions, workflow,
   queues, tables, buckets in the sandbox profile, repositories with their images, task definitions,
   CodeBuild project, alarms, dashboard, budget).
3. Log groups that services create on their own (custom-resource handlers, Container Insights) and any
   left-over project log groups.
4. Secrets: CloudFormation schedules deletion; the script forces it so nothing lingers.
5. After a second confirmation: retained buckets/tables (handover profile) and this project's own
   objects in the CDK bootstrap assets bucket (keys from the saved asset manifests; CDK's generic
   provider packages and the CDKToolkit stack are not touched).

## Residual check

`make verify-cleanup` (`scripts/verify_cleanup.py`, read-only) checks the stack, buckets (with
versions), log groups, Lambda, SQS, DynamoDB, Step Functions, SNS, alarms, dashboards, ECR, ECS
clusters and task definitions, CodeBuild, secrets (including scheduled deletions), guardrails, VPCs and
security groups, IAM roles, budgets, every IoT and Greengrass resource type listed above, resources
found by tag (each confirmed with its service, because the tag index lags behind deletions), and
bootstrap asset objects. It exits non-zero if anything is left. The conclusion is either "Cleaned up:
nothing of this project remains" or a list of what remains.

## Not removed automatically by CloudFormation

IoT things, certificates and jobs; Greengrass deployments, core devices and component versions;
service-created log groups; scheduled secret deletions; retained data; bootstrap asset objects. A
certificate created by fleet provisioning whose registration was refused has no thing or policy and
cannot be attributed to this project (SECURITY-NOTES R6); the drills report provisioning failures so
that this case is visible.
