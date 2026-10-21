# AGENTS.md

Instructions for AI coding assistants working in this repository. (CLAUDE.md only imports this file.)

## Role

You maintain a non-production prototype: simulated edge boxes connect to AWS IoT Core, upload images,
and a serverless workflow uses Amazon Bedrock to draft e-commerce assets. Device isolation, untrusted
input handling, content-free logging, least privilege and clean teardown matter more than features.
Read README.md, DECISIONS.md and THREAT_MODEL.md before changing behaviour.

## Common commands

| Task | Command |
|---|---|
| Install, create `config/*.json` from the examples | `make init` |
| Lint / tests (no AWS) | `make lint` / `make test` |
| cdk synth + cdk-nag, both profiles | `make synth` |
| Injection evaluation + negative control | `make eval` |
| Offline demo D1-D6 | `make demo` |
| Secret scans | `make scan-ci` / `make scan` |
| Sandbox only: preflight, deploy, verify, drills, load test, destroy, residual check | `make preflight`, `make deploy`, `ALLOW_PAID=1 make verify verify-rollout verify-loadtest`, `make destroy`, `make verify-cleanup` |

## Git workflow

- Branch from `main`; one topic per commit; Conventional Commits (`feat(scope): ...`, `fix`, `docs`,
  `test`, `chore`, `ci`, `refactor`). The body says what changed, why, and how it was verified, and
  ends with the co-author line your tool requires.
- Never push to a remote and never rewrite published history.

## Boundaries

**Always**
- Add or update tests with every change; run `make lint test synth eval` before committing.
- Treat device payloads, image bytes, text in images and model output as untrusted; keep hard limits in code.
- Take device identity only from the authenticated connection (topic segment == client id).
- Keep logs content-free (ids, counts, enum values, codes, timings).
- Mask 12-digit numbers (`111122223333`) in anything you write to files or output.
- Keep the device policy free of wildcard resources outside the device's own name space, and below the IoT policy size limit.

**Ask first**
- Any command that creates, changes or deletes AWS resources, or costs money (`ALLOW_PAID=1 ...`).
- Adding dependencies, changing IAM or IoT policies, resource policies, removal policies or retention.
- Changing the output schema or the content rules in `src/edge_pipeline/validation.py`.

**Never**
- Commit account ids, certificates, private keys, credentials, real images or customer names.
- Give the model tools with side effects, or let model output choose object keys or devices.
- Modify the CDK bootstrap stack (CDKToolkit), account-level settings (IoT logging, event
  configurations, registered CAs), or resources this project did not create.
- Disable a cdk-nag rule for the whole stack, or allowlist a secret-scanner finding without a rule id, a path and a reason.
