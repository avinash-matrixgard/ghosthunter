# Changelog

All notable changes to Ghosthunter are documented here.

The format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

## [1.0.1] - 2026-04-17

### Added
- Interactive preflight checks for active mode (`--active`): missing `boto3` / `google-cloud-bigquery`, absent CLI tools (`aws`, `gcloud`), unconfigured or expired credentials, and Cost Explorer permission gaps now surface as Rich panels with guided prompts instead of stack traces.
- Auto-fix hooks for pip-installable dependencies: the preflight can install the right extra (`ghosthunter[aws]` / `ghosthunter[gcp]`) after a confirm, then re-check.
- `sts:GetCallerIdentity` verification on AWS preflight; the verified account and principal are echoed back before any Cost Explorer call so users can abort if they're pointing at the wrong account.

### Changed
- Test suite discovery is now pinned to `tests/` via `[tool.pytest.ini_options]` so a second Ghosthunter checkout placed next to the repo no longer causes `import file mismatch` collection errors.

### Fixed
- `investigate --active` no longer aborts with a raw `ProviderError` / traceback when an optional dependency is missing — the user is walked through installing it.

[Unreleased]: https://github.com/avinash-matrixgard/ghosthunter/compare/v1.0.1...HEAD
[1.0.1]: https://github.com/avinash-matrixgard/ghosthunter/releases/tag/v1.0.1

## [1.0.0] - 2026-04-17

Initial public release.

### Added
- Dual-model investigator: Opus reasons about hypotheses, Sonnet proposes and validates commands.
- GCP cost-spike root-cause analysis via `gcloud` and Cloud Logging.
- AWS cost-spike support via `aws` CLI and Cost Explorer (advisor mode and active mode).
- FOCUS 1.0 billing parser for cloud-agnostic cost input.
- Provider-aware command allowlist with per-provider validator prompts.
- Interactive CLI with Claude Code-style progress spinner.
- Memory palace: per-investigation hypothesis and evidence state.
- Demo mode with scripted scenarios for GCP and AWS.
- No-cloud sandbox mode for trying Ghosthunter without credentials.
- Audit log of past investigations (`ghosthunter audit`).

### Security
- Read-only shell command enforcement: shell commands must match an explicit allowlist (SDK calls for billing fetch are hardcoded, not user-variable).
- Two-layer validation (static allowlist + model-side validator) before any command execution.

[1.0.0]: https://github.com/avinash-matrixgard/ghosthunter/releases/tag/v1.0.0
