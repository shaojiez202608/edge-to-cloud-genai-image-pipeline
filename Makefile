# Single entry point. Targets used by CI (lint, test, synth, eval, scan-ci) only check: they change no
# tracked files and no AWS resources. Targets that call AWS say so; the paid ones need ALLOW_PAID=1.
SHELL := /bin/bash
PARAMS ?= config/sandbox.json
PY := uv run python
CDK := npx -y aws-cdk@2

.PHONY: help init preflight lint test synth bundle eval-data eval demo deploy identities verify verify-loadtest \
	verify-rollout destroy verify-cleanup scan-ci scan

help:
	@grep -E '^[a-z-]+:.*## ' $(MAKEFILE_LIST) | sed -E 's/:.*## /\t/'

init: ## Install dependencies; create config/*.json from the examples if missing
	uv sync
	@for f in config/*.example.json; do t="$${f%.example.json}.json"; \
	  [ -e "$$t" ] && echo "keep $$t" || { cp "$$f" "$$t"; echo "created $$t"; }; done

preflight: ## Read-only: account (last 4 digits) and region confirmation, tools, services, quotas
	./scripts/preflight.sh $(PARAMS)

lint: ## ruff + shell syntax (check only)
	uv run ruff check .
	@for f in scripts/*.sh containers/greengrass/entrypoint.sh; do bash -n "$$f" || exit 1; done

test: ## Unit, local-mode and CDK assertion tests (no AWS)
	uv run pytest

bundle: ## arm64 Lambda bundle and image build context (no Docker)
	./scripts/build_bundle.sh

synth: bundle ## cdk synth + cdk-nag for both profiles (no credentials needed)
	env -u CDK_DEFAULT_ACCOUNT $(CDK) synth -q -c params=config/sandbox.example.json -o cdk.out/sandbox
	env -u CDK_DEFAULT_ACCOUNT $(CDK) synth -q -c params=config/handover.example.json -o cdk.out/handover

eval-data: ## Generate the synthetic prompt-injection images (git-ignored)
	$(PY) scripts/gen_synthetic.py

eval: eval-data ## Injection evaluation with the mock models; the negative control must report failures
	$(PY) scripts/evaluate.py --model naive
	$(PY) scripts/evaluate.py --model robust
	$(PY) scripts/evaluate.py --model naive --negative-control

demo: ## Offline demo D1-D6 (local simulation mode)
	./scripts/demo.sh

deploy: init ## Preflight, refuse deletions/replacements unless ALLOW_DESTRUCTIVE=1, deploy, build images
	./scripts/deploy.sh $(PARAMS)

identities: ## Create the claim certificate and Greengrass core identities (AWS)
	$(PY) scripts/setup_identities.py --params $(PARAMS)

verify: identities ## Cloud end-to-end D1-D3 + D6 (paid: ALLOW_PAID=1)
	$(PY) scripts/e2e_aws.py --params $(PARAMS) --injection --report out/e2e_aws.json

verify-loadtest: ## Reconnect storm sized from Service Quotas (paid: ALLOW_PAID=1)
	$(PY) scripts/loadtest_aws.py --params $(PARAMS) --report out/loadtest_aws.json

verify-rollout: identities ## Rollout/rollback drills: IoT Jobs and Greengrass (paid: ALLOW_PAID=1)
	$(PY) scripts/rollout_drill_jobs.py --params $(PARAMS) --report out/rollout_jobs_aws.json
	$(PY) scripts/rollout_drill_gg.py --params $(PARAMS) --report out/rollout_gg_aws.json

destroy: ## Remove IoT/Greengrass resources, destroy the stack, clean leftovers (asks twice)
	./scripts/cleanup.sh $(PARAMS)

verify-cleanup: ## Read-only residual check; non-zero exit if anything is left
	./scripts/verify_cleanup.sh --params $(PARAMS)

scan-ci: ## Secret scan of the full history (gitleaks) with a canary that must be detected
	./scripts/secret_canary.sh
	gitleaks git . --config .gitleaks.toml --no-banner --redact

scan: scan-ci ## Local extra scans: workflow lint and dependency audit
	uvx zizmor .github/workflows
	osv-scanner scan --lockfile uv.lock
