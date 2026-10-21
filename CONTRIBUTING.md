# Contributing

Thank you for your interest in improving this prototype. Bug reports, fixes and documentation
changes are welcome.

## Reporting bugs and requesting features

Use the issue forms. Describe what you did, what you expected and what happened. Do not attach real
images, device certificates, personal data, account ids or credentials; reproduce problems with the
synthetic generator (`make eval-data`) or local mode (`make demo`). Security issues go to [SECURITY.md](SECURITY.md), not to
public issues.

## Pull requests

1. Create a branch from `main`. One change per pull request; it is squash-merged.
2. `make init`, then make your change.
3. **Every change needs a test** (unit tests under `tests/`; infrastructure changes also need an
   assertion in `tests/test_infra.py` and a clean `make synth`).
4. Run `make lint test synth eval scan-ci`. Fill in the pull request template, including what
   you did not test.
5. Commit messages follow [Conventional Commits](https://www.conventionalcommits.org/)
   (`feat(scope): ...`, `fix`, `docs`, `test`, `chore`, `ci`, `refactor`). The body says what changed,
   why, and how it was verified.

## Rules for content

- No account ids (use `111122223333`), customer, company or personal names, internal host names
  (use `example.com`), credentials, or values that look like real keys.
- Placeholders are `<UPPER_SNAKE>`.
- Only synthetic data, or public data with a compatible licence, may be added. No real photos, no
  images of real people, no real device identifiers or certificates.
- New cdk-nag acknowledgements are attached to the construct that triggers them and have a written
  reason in SECURITY-NOTES.md.

## AI-assisted contributions

You may use AI coding assistants. You are responsible for every line you submit: you must be able
to explain what each line does and why it is there, and you must have run the checks above
yourself. Assistant instructions for this repository are in [AGENTS.md](AGENTS.md).

## Licensing

See the [LICENSE](LICENSE) file for this project's licensing. We will ask you to confirm the
licensing of your contribution. By submitting a pull request you confirm that you have the right to
submit it and that it is licensed under the MIT-0 license of this repository. Third-party code or
assets need an entry in [THIRD-PARTY-LICENSES](THIRD-PARTY-LICENSES) and a compatible licence.
