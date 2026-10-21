#!/usr/bin/env bash
# Canary for the secret scanner: plant a freshly generated fake AWS access key in a throw-away
# directory and confirm that gitleaks (same version and rules as `gitleaks git` on the history)
# reports it. The fake key exists only in a temp directory, is never committed anywhere and never
# written into this repository. Exits 1 if the scanner misses it.
set -uo pipefail
TMP=$(mktemp -d)
trap 'rm -rf "$TMP"' EXIT
KEY="AKIA$(LC_ALL=C tr -dc 'A-Z2-7' < /dev/urandom | head -c 16)"
SECRET="$(LC_ALL=C tr -dc 'A-Za-z0-9/+' < /dev/urandom | head -c 40)"
printf 'aws_access_key_id = %s\naws_secret_access_key = %s\n' "$KEY" "$SECRET" > "$TMP/creds.ini"
CONFIG=()
[ -f .gitleaks.toml ] && CONFIG=(--config .gitleaks.toml)   # same allowlist as the history scan
if gitleaks dir "$TMP" ${CONFIG[@]+"${CONFIG[@]}"} --no-banner --redact >/dev/null 2>&1; then
  echo "secret canary NOT detected: the scanner configuration is broken" >&2
  exit 1
fi
echo "secret canary detected by gitleaks (expected)"
