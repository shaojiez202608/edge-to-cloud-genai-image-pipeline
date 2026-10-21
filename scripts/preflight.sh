#!/usr/bin/env bash
# Read-only preflight. Changes nothing. Usage: scripts/preflight.sh [params-file]
# Shows the target account (last 4 digits only) and region and asks for confirmation.
# Non-interactive runs confirm with CONFIRM_ACCOUNT=<last 4 digits>.
set -uo pipefail
cd "$(dirname "$0")/.."
PARAMS="${1:-config/sandbox.json}"
fail=0
ok() { echo "  ok    $*"; }
bad() { echo "  FAIL  $*"; fail=1; }
info() { echo "  info  $*"; }

echo "== parameter file"
[ -f "$PARAMS" ] || { bad "$PARAMS missing (run: make init)"; exit 1; }
read -r STACK REGION MODEL < <(python3 -c '
import json, sys
p = json.load(open(sys.argv[1]))
print(p["deploy"]["stack_name"], p["deploy"]["region"], p["pipeline"].get("model_id", "us.amazon.nova-2-lite-v1:0"))' "$PARAMS")
if .venv/bin/python -c 'import json,sys; sys.path[:0]=["infra","src"]; from edge_infra.params import validate; validate(json.load(open(sys.argv[1])))' "$PARAMS" 2>/tmp/preflight-params.err; then
  ok "$PARAMS valid (stack $STACK, region $REGION, model $MODEL)"
else
  bad "parameter validation:"; tail -15 /tmp/preflight-params.err | sed 's/^/        /'
fi

echo "== tools"
for t in uv node npx aws python3; do command -v "$t" >/dev/null 2>&1 && ok "$t" || bad "$t not found"; done
info "Docker is not used: images are built by CodeBuild (ARM), Lambda wheels are downloaded for aarch64"

echo "== credentials and target"
ACCOUNT=$(aws sts get-caller-identity --query Account --output text 2>/dev/null) || { bad "no AWS credentials"; exit 1; }
LAST4="${ACCOUNT: -4}"
KIND=$(aws sts get-caller-identity --query Arn --output text | sed -E 's#^arn:aws[a-z-]*:(iam|sts)::[0-9]{12}:([a-z-]+)/.*#\2#')
info "account ********${LAST4}, region ${REGION}, caller type ${KIND}"
if [ "${CONFIRM_ACCOUNT:-}" != "$LAST4" ]; then
  if [ -t 0 ]; then
    read -r -p "  Type the last 4 digits of the account to confirm it is the intended non-production account: " ans
    [ "$ans" = "$LAST4" ] || { bad "not confirmed; stop if this is not your intended account"; exit 1; }
  else
    bad "set CONFIRM_ACCOUNT=<last 4 digits> to confirm the target account"; exit 1
  fi
fi
ok "target account confirmed"

echo "== services in ${REGION}"
aws iot describe-endpoint --endpoint-type iot:Data-ATS --region "$REGION" >/dev/null 2>&1 && ok "AWS IoT Core data endpoint" || bad "AWS IoT Core not reachable"
aws greengrassv2 list-core-devices --max-results 1 --region "$REGION" >/dev/null 2>&1 && ok "AWS IoT Greengrass V2" || bad "Greengrass V2 not available"
case "$MODEL" in
  us.*|eu.*|apac.*|global.*|jp.*|au.*|ca.*)
    aws bedrock get-inference-profile --inference-profile-identifier "$MODEL" --region "$REGION" --query status --output text >/dev/null 2>&1 \
      && ok "inference profile $MODEL" || bad "inference profile $MODEL not available" ;;
  *) aws bedrock get-foundation-model --model-identifier "$MODEL" --region "$REGION" >/dev/null 2>&1 && ok "model $MODEL" || bad "model $MODEL not available" ;;
esac
.venv/bin/python - "$PARAMS" "$MODEL" "$REGION" <<'PY' || fail=1
import json, sys, boto3
p, model, region = json.load(open(sys.argv[1])), sys.argv[2], sys.argv[3]
want = sorted(p["deploy"]["model_regions"])
if model.split(".")[0] in ("us", "eu", "apac", "global", "jp", "au", "ca"):
    prof = boto3.client("bedrock", region_name=region).get_inference_profile(inferenceProfileIdentifier=model)
    have = sorted({m["modelArn"].split(":")[3] for m in prof["models"]})
    if have != want:
        print(f"  FAIL  deploy.model_regions {want} differs from the profile's regions {have}"); sys.exit(1)
    print(f"  ok    model_regions match the inference profile ({len(have)} regions)")
PY
aws cloudformation describe-stacks --stack-name CDKToolkit --region "$REGION" >/dev/null 2>&1 && ok "CDK bootstrap stack present (not modified)" || bad "CDKToolkit missing in $REGION"

echo "== quotas (Service Quotas, applied values; the load test sizes itself from these)"
for code in L-AEA1E176 L-033DE216 L-4B33E451 L-75CAB723 L-CAFFCA46 L-B2BBBD8C; do
  { aws service-quotas get-service-quota --service-code iotcore --quota-code "$code" --region "$REGION" \
      --query 'Quota.[QuotaName,Value]' --output text 2>/dev/null \
    || aws service-quotas get-aws-default-service-quota --service-code iotcore --quota-code "$code" --region "$REGION" \
      --query 'Quota.[QuotaName,Value]' --output text 2>/dev/null | sed 's/$/ (AWS default)/'; } | sed 's/^/  info  /' \
    || info "quota $code not readable"
done
exit $fail
