## What and why

<!-- One change per PR. Use a Conventional Commits title, e.g. "fix(pipeline): ..." -->

## Checks run

- [ ] `make lint`
- [ ] `make test`
- [ ] `make synth` (cdk-nag clean for both profiles; new acknowledgements have a reason in SECURITY-NOTES.md)
- [ ] `make eval` (negative control still reported as failing)
- [ ] `make scan-ci` (no secrets, no account ids, no real images)
- [ ] Deployed check (`make deploy verify verify-rollout verify-loadtest destroy verify-cleanup`, sandbox only), if infrastructure or device code changed

## Not tested

<!-- List what you did not run or could not verify, and why. -->

## Docs

- [ ] README / docs / DECISIONS / THREAT_MODEL / VERIFICATION updated where behaviour changed
