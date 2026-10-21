#!/usr/bin/env bash
# Guarded deploy: preflight -> bundle -> cdk diff (refuses deletions/replacements unless
# ALLOW_DESTRUCTIVE=1) -> cdk deploy -> image build in CodeBuild. Output is masked.
# Usage: scripts/deploy.sh [params-file]
set -euo pipefail
cd "$(dirname "$0")/.."
PARAMS="${1:-config/sandbox.json}"
mask() { sed -E 's/[0-9]{12}/111122223333/g'; }
CDK=(npx -y aws-cdk@2)

./scripts/preflight.sh "$PARAMS"
./scripts/build_bundle.sh
CALLER=$(aws sts get-caller-identity --query Arn --output text)
# Resource policies match IAM user/role ARNs, not STS sessions.
OPERATOR=$(echo "$CALLER" | sed -E 's#^arn:(aws[a-z-]*):sts::([0-9]{12}):assumed-role/([^/]+)/.*#arn:\1:iam::\2:role/\3#')
CTX=(-c "params=$PARAMS" -c "operator_principal_arns=$OPERATOR")
export CDK_DEFAULT_ACCOUNT=$(aws sts get-caller-identity --query Account --output text)

echo "== cdk diff"
DIFF=$("${CDK[@]}" diff "${CTX[@]}" 2>&1 || true)
echo "$DIFF" | mask | grep -vE '^\s*$' | tail -40
if echo "$DIFF" | grep -qE '^\[-\]|requires replacement|may be replaced|\(replace\)'; then
  if [ "${ALLOW_DESTRUCTIVE:-}" != "1" ]; then
    echo "The change set deletes or replaces resources. Review the diff and re-run with ALLOW_DESTRUCTIVE=1." >&2
    exit 2
  fi
  echo "ALLOW_DESTRUCTIVE=1: continuing with deletions/replacements."
fi

echo "== cdk deploy"
"${CDK[@]}" deploy "${CTX[@]}" --require-approval never 2>&1 | mask | grep -vE '^\s*$' | tail -40
STACK=$(python3 -c 'import json,sys; print(json.load(open(sys.argv[1]))["deploy"]["stack_name"])' "$PARAMS")
# Keep the uploaded asset manifest: later synths overwrite cdk.out, cleanup needs these keys.
mkdir -p .deploy && cp "cdk.out/${STACK}.assets.json" ".deploy/${STACK}.$(date -u +%Y%m%dT%H%M%SZ).assets.json"

echo "== image build (CodeBuild, ARM)"
uv run python scripts/build_images.py --params "$PARAMS"
