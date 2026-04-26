<!--
Thanks for the contribution. A few quick checks below.
Anything you can't fill in yet, leave as-is and we'll work on it together.
-->

## What

<!-- One-paragraph summary of what this PR changes -->

## Why

<!-- Link to the issue / discussion / external context that motivated the change -->

Fixes #

## Type of change

- [ ] New provider / mode / control surface
- [ ] Bug fix (non-breaking)
- [ ] Bug fix (breaking — CLI flags or config schema changed)
- [ ] Refactor (no behavior change)
- [ ] Documentation only
- [ ] Tests / fixtures
- [ ] CI / tooling

## Security review (mandatory for command-validation changes)

If this PR touches the command validator, allowlist, fast-reject layer, or anything that decides whether a proposed command runs:

- [ ] I added test cases covering the new behavior
- [ ] I added at least one negative test case (something that MUST be rejected)
- [ ] I did NOT relax any existing fast-reject pattern (or if I did, the rationale is explained below)
- [ ] I did NOT add a "soft override" / "ignore validator if user confirms" path

## Checklist

- [ ] `ruff check` is clean
- [ ] `ruff format` is clean
- [ ] `pytest` passes locally on Python 3.12
- [ ] If a new dep was added, it's a deliberate choice and the README's stack section is updated
- [ ] If a new CLI flag was added, README usage section is updated
- [ ] CHANGELOG.md "Unreleased" section updated
- [ ] No secrets, no real account IDs, no real billing data committed
- [ ] If GitHub Actions were added/updated: pinned to a commit SHA, not a version tag

## Tested with

- Python:
- OS:
- Provider (gcp / aws / billing-file):
- Mode (paranoid / active / demo / audit):

## Sample output / screenshots

<!-- Paste a redacted CLI session or screenshot showing the new behavior -->
