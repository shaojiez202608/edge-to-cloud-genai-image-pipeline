#!/usr/bin/env bash
# Destroy everything this project created. Asks twice: once for the stack (and the IoT identities,
# jobs and Greengrass resources made by the scripts), once more before deleting data (retained
# buckets/tables) and this project's objects in the CDK bootstrap assets bucket. A failing step prints a
# warning and the script continues; run scripts/verify_cleanup.sh afterwards.
# Non-interactive: CONFIRM_STACK=<stack name> CONFIRM_DATA=yes. Usage: scripts/cleanup.sh [params-file]
set -uo pipefail
cd "$(dirname "$0")/.."
PARAMS="${1:-config/sandbox.json}"
mask() { sed -E 's/[0-9]{12}/111122223333/g'; }
warn() { echo "WARNING: $* (continuing)" >&2; }
read -r STACK PREFIX REGION REMOVAL < <(python3 -c '
import json, sys
d = json.load(open(sys.argv[1]))["deploy"]
print(d["stack_name"], d["resource_prefix"], d["region"], d["removal_policy"])' "$PARAMS")
ask() {
  [ "${3:-}" = "$2" ] && return 0
  [ -t 0 ] || { echo "Not confirmed (set the matching CONFIRM_* variable)."; return 1; }
  read -r -p "$1 " a; [ "$a" = "$2" ]
}
ask "Type the stack name ($STACK) to destroy it and this project's IoT/Greengrass resources in $REGION:" "$STACK" "${CONFIRM_STACK:-}" || exit 1

echo "== IoT things, certificates, jobs, Greengrass deployments/components, running tasks"
uv run python scripts/cleanup_iot.py --params "$PARAMS" 2>&1 | mask || warn "IoT cleanup failed"

echo "== cdk destroy"
CTX=(-c "params=$PARAMS")
[ -d build/lambda ] || ./scripts/build_bundle.sh >/dev/null
npx -y aws-cdk@2 destroy "${CTX[@]}" --force 2>&1 | mask | grep -vE '^\s*$' | tail -15 || warn "cdk destroy failed"
STATUS=$(aws cloudformation describe-stacks --stack-name "$STACK" --region "$REGION" --query 'Stacks[0].StackStatus' --output text 2>/dev/null || echo GONE)
echo "stack status: $STATUS"
if [ "$STATUS" = "DELETE_FAILED" ]; then
  warn "stack DELETE_FAILED; listing failed resources"
  aws cloudformation describe-stack-events --stack-name "$STACK" --region "$REGION" \
    --query "StackEvents[?ResourceStatus=='DELETE_FAILED'].[LogicalResourceId,ResourceStatusReason]" --output text | mask | head -20
fi

echo "== log groups created by services (custom-resource handlers, Container Insights)"
for p in "/aws/lambda/$STACK" "/aws/lambda/$PREFIX-" "/aws/ecs/containerinsights/$PREFIX" "/$PREFIX/" "/aws/codebuild/$PREFIX" "/aws/vendedlogs/states/$PREFIX"; do
  for g in $(aws logs describe-log-groups --region "$REGION" --log-group-name-prefix "$p" --query 'logGroups[].logGroupName' --output text); do
    aws logs delete-log-group --region "$REGION" --log-group-name "$g" && echo "deleted $g" || warn "could not delete $g"
  done
done

echo "== secrets (CloudFormation schedules deletion; force it so the names are free and nothing lingers)"
for s in $(aws secretsmanager list-secrets --region "$REGION" --include-planned-deletion --filters "Key=name,Values=$PREFIX/" --query 'SecretList[].Name' --output text); do
  aws secretsmanager delete-secret --region "$REGION" --secret-id "$s" --force-delete-without-recovery >/dev/null && echo "deleted secret $s" || warn "could not delete $s"
done

BUCKETS=$(aws s3api list-buckets --query "Buckets[?starts_with(Name, '$(echo "$STACK" | tr 'A-Z' 'a-z')')].Name" --output text)
TABLES=$(aws dynamodb list-tables --region "$REGION" --query "TableNames[?starts_with(@, '$PREFIX-')]" --output text)
KEYS=$(uv run python -c 'import sys; sys.path.insert(0, "scripts"); from verify_cleanup import project_asset_keys; print(" ".join(project_asset_keys(sys.argv[1])))' "$STACK")
if [ -n "$BUCKETS$TABLES$KEYS" ]; then
  echo "== data and bootstrap objects"
  [ -n "$BUCKETS$TABLES" ] && echo "Retained data (removal_policy=$REMOVAL): buckets [$BUCKETS] tables [$TABLES]" | mask
  [ -n "$KEYS" ] && echo "This project's objects in the CDK bootstrap assets bucket: $(echo $KEYS | wc -w) (CDK generic provider packages are kept)"
  if ask "Delete the retained data and these asset objects? Type yes:" "yes" "${CONFIRM_DATA:-}"; then
    for b in $BUCKETS; do
      uv run python - "$b" <<'PY' || warn "could not empty a bucket"
import sys, boto3
boto3.resource("s3").Bucket(sys.argv[1]).object_versions.delete()
PY
      aws s3api delete-bucket --bucket "$b" && echo "deleted bucket" || warn "could not delete a bucket"
    done
    for t in $TABLES; do aws dynamodb delete-table --region "$REGION" --table-name "$t" >/dev/null && echo "deleted table $t" || warn "could not delete $t"; done
    ACCOUNT=$(aws sts get-caller-identity --query Account --output text)
    ASSETS="cdk-hnb659fds-assets-${ACCOUNT}-${REGION}"
    for k in $KEYS; do
      for v in $(aws s3api list-object-versions --bucket "$ASSETS" --prefix "$k" \
                  --query "[Versions[?Key=='$k'].VersionId, DeleteMarkers[?Key=='$k'].VersionId][]" --output text); do
        [ "$v" = "None" ] && continue
        aws s3api delete-object --bucket "$ASSETS" --key "$k" --version-id "$v" >/dev/null && echo "deleted asset object ($v)" || warn "could not delete an asset object"
      done
    done
  else
    echo "Kept retained data and asset objects."
  fi
fi
echo "Now run: make verify-cleanup"
