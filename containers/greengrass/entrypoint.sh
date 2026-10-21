#!/bin/sh
# Manual provisioning for a Greengrass core in a container (PROVISION=false, INIT_CONFIG).
# Inputs (task definition / run-task overrides): THING_NAME, GG_SECRET_ID, IOT_DATA_ENDPOINT,
# IOT_CRED_ENDPOINT, ROLE_ALIAS, AWS_REGION. The secret holds {"certificatePem", "privateKey"}.
set -eu
CFG_DIR=/opt/edge-config  # created 0700 in the image; not a shared temporary directory
mkdir -p "$CFG_DIR"
umask 077
SECRET=$(aws secretsmanager get-secret-value --secret-id "$GG_SECRET_ID" --query SecretString --output text)
printf '%s' "$SECRET" | python3 -c 'import json,sys; print(json.load(sys.stdin)["certificatePem"], end="")' > "$CFG_DIR/device.pem.crt"
printf '%s' "$SECRET" | python3 -c 'import json,sys; print(json.load(sys.stdin)["privateKey"], end="")' > "$CFG_DIR/private.pem.key"
unset SECRET
curl -sSf -o "$CFG_DIR/AmazonRootCA1.pem" https://www.amazontrust.com/repository/AmazonRootCA1.pem
NUCLEUS_VERSION=$(cat /opt/greengrassv2/nucleus-version 2>/dev/null || echo "")
cat > "$CFG_DIR/config.yaml" <<YAML
---
system:
  certificateFilePath: "$CFG_DIR/device.pem.crt"
  privateKeyPath: "$CFG_DIR/private.pem.key"
  rootCaPath: "$CFG_DIR/AmazonRootCA1.pem"
  rootpath: "/greengrass/v2"
  thingName: "$THING_NAME"
services:
  aws.greengrass.Nucleus:
    componentType: "NUCLEUS"
    version: "$NUCLEUS_VERSION"
    configuration:
      awsRegion: "$AWS_REGION"
      iotRoleAlias: "$ROLE_ALIAS"
      iotDataEndpoint: "$IOT_DATA_ENDPOINT"
      iotCredEndpoint: "$IOT_CRED_ENDPOINT"
YAML
umask 022
# Components must not see the task role: drop the ECS credential pointer before Greengrass starts.
unset AWS_CONTAINER_CREDENTIALS_RELATIVE_URI AWS_CONTAINER_CREDENTIALS_FULL_URI AWS_CONTAINER_AUTHORIZATION_TOKEN
export INIT_CONFIG="$CFG_DIR/config.yaml"
# The nucleus logs to files under its root; mirror them to stdout so they reach CloudWatch Logs.
(
  while [ ! -f /greengrass/v2/logs/greengrass.log ]; do sleep 2; done
  exec tail -n +1 -F /greengrass/v2/logs/greengrass.log /greengrass/v2/logs/com.example.EdgeModel.log 2>/dev/null
) &
exec /greengrass-entrypoint.sh
