# Standards review

Review of this repository against the review checklist (appendix A–F) of the prototype standards
used for this project. Status: **Met**, **Changed** (was missing or wrong during development and has
been fixed, with the fix), or **N/A** (with the reason). Evidence is `file:line` at tag v0.1.0.

## A. Positioning and redaction

| Item | Status | Evidence |
|---|---|---|
| README first screen: disclaimer (non-production, user responsible, security/legal review, shared responsibility) linking the hardening checklist | Met | `README.md:5-10`, `docs/handover.md:34`; same in `README.zh-CN.md:5-9` |
| grep for customer/company/person names, domains, 12-digit numbers, profile names, template leftovers = 0 | Met | Final self-check in VERIFICATION.md ("Repository self-check"); scripts mask 12-digit numbers (`scripts/guards.py`) |
| Placeholders `<UPPER_SNAKE>`, `example.com`, `111122223333`; no screenshots; synthetic data only | Met | `.github/workflows/deploy-verify.yml` (role ARN placeholder), `src/edge_pipeline/synthetic.py`; no images committed (`.gitignore`) |

## B. Standard repository files

| Item | Status | Evidence |
|---|---|---|
| LICENSE (MIT-0, Amazon copyright line), SECURITY (reporting + full content), CODE_OF_CONDUCT, CONTRIBUTING (tests, Conventional Commits, no account ids, licensing, AI rule) | Met | `LICENSE:3`, `SECURITY.md`, `CODE_OF_CONDUCT.md`, `CONTRIBUTING.md` |
| THIRD-PARTY-LICENSES with distributed / downloaded tables; CHANGELOG (Keep a Changelog) + VERSION | Met | `THIRD-PARTY-LICENSES`, `CHANGELOG.md`, `VERSION` |
| Issue forms (blank issues off, security contact), PR template (checks run, not tested), AGENTS.md + CLAUDE.md, Makefile, .gitignore, .dockerignore | Met | `.github/ISSUE_TEMPLATE/`, `.github/pull_request_template.md`, `AGENTS.md`, `CLAUDE.md`, `Makefile`, `.gitignore`, `.dockerignore` |
| DECISIONS, OPEN-QUESTIONS, SECURITY-NOTES, THREAT_MODEL (DRAFT), VERIFICATION, DEPLOYMENT-LOG | Met | root files; `THREAT_MODEL.md:3` |

## C. Configuration and deployment

| Item | Status | Evidence |
|---|---|---|
| Parameter templates, validation (patterns, cross-field rules), removal policy as parameter, uniform tags | Met | `config/*.example.json`, `infra/edge_infra/params.py:40`, `infra/edge_infra/common.py` (`removal`) |
| Preflight (account last 4 digits, region, tools, services, quotas), destructive-change guard, paid-command guard, end-to-end checks incl. deny/abuse/async paths | Met | `scripts/preflight.sh:34`, `scripts/deploy.sh:34`, `scripts/guards.py` (`require_paid`), `scripts/e2e_aws.py` |
| Verification records: date, region, configuration, commit, failures, limits | Met | `VERIFICATION.md`, `DEPLOYMENT-LOG.md` |
| Account id never in config/cdk.json | Met | `infra/edge_infra/params.py` (12-digit check), `infra/app.py` (account from the environment) |

## D. Security and CI

| Item | Status | Evidence |
|---|---|---|
| cdk-nag zero unhandled findings, reasons per acknowledgement, attached per construct, built from config | Met | `infra/edge_infra/nag.py:26`, `tests/test_infra.py` (both profiles + other prefix) |
| Trust policies with `aws:SourceAccount` and `aws:SourceArn` where documented; others listed with reason | Met | `infra/edge_infra/iot.py:30`, `infra/edge_infra/stack.py:92`, `SECURITY-NOTES.md` (trust table) |
| Resource policy on the data bucket (only expected callers) | Met | `infra/edge_infra/stack.py:40` |
| CI: lint, tests, synth + nag, secret scan (history + canary), ASH; Actions pinned by SHA; minimal permissions; Dependabot (weekly, grouped, cooldown); gitleaks allowlist none | Met | `.github/workflows/ci.yml:9`, `.github/dependabot.yml:1`, `.gitleaks.toml`; CI not run on GitHub (no push) |
| No account-level setting changed | Met | No IoT logging/event-configuration/CA registration/quota changes; budget is a resource; default VPC used read-only (DECISIONS D16) |
| Dependencies: lock file, cooldown, osv-scanner without High+ | Changed | Pillow 12.3.0 had High/Critical advisories → upgraded to 13.0.0 with a documented cooldown exception (`pyproject.toml`, DECISIONS D15) |

## E. Cleanup

| Item | Status | Evidence |
|---|---|---|
| Cleanup script with confirmations (stack, then data), warn-and-continue; residual check fails on leftovers; README lists resources not removed automatically | Met | `scripts/cleanup.sh:21,60`, `scripts/cleanup_iot.py`, `scripts/verify_cleanup.py:224`, `README.md:111` |
| IoT-specific cleanup: certificates (detach, INACTIVE, delete), policies, things, groups, rules, Greengrass deployments/components/core devices, jobs, Fargate tasks, ECR images | Met | `scripts/cleanup_iot.py`, stack (`empty_on_delete` ECR) |
| All used regions checked, explicit conclusion | Met | `DEPLOYMENT-LOG.md` (us-east-1 only) |
| Bootstrap assets: only this project's objects (asset hashes) | Met | `scripts/verify_cleanup.py` (`project_asset_keys`), `scripts/cleanup.sh` |

## F. Documentation and commits

| Item | Status | Evidence |
|---|---|---|
| Fact check with official links and dates; "found in testing" marked; costs as dimensions only; README section order | Met | `docs/architecture.md` (Fact check, T1–T8), `README.md:42`, README headings `27-155` |
| Conventional Commits with what/why/verified; not pushed | Met | `git log` (self-check in VERIFICATION.md); no remote configured |

## Summary

Met: all items. Changed during development: 1 (dependency advisories, fixed). N/A: 0.
