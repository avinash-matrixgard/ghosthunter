# Changelog

All notable changes to Ghosthunter are documented here.

The format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

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

[Unreleased]: https://github.com/avinash-matrixgard/ghosthunter/compare/v1.0.0...HEAD
[1.0.0]: https://github.com/avinash-matrixgard/ghosthunter/releases/tag/v1.0.0
