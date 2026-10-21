#!/usr/bin/env bash
# Residual-resource check (read-only); exits non-zero if anything of this project is left.
set -uo pipefail
cd "$(dirname "$0")/.."
exec uv run python scripts/verify_cleanup.py "$@"
