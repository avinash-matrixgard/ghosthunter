# Changelog

All notable changes to Ghosthunter are documented here.

The format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

## [1.0.8] - 2026-04-30

### Security

This release closes all four findings from an external security audit
on 2026-04-29 (the day after v1.0.6 went public). Three landed as
deterministic fixes; one was confirmed as by-design and documented.
The full audit and gap ledger live at
<https://matrixgard.com/docs/ghosthunter/security-review>.

- **Secrets redacted before any disk write** ([#3]). Pasted command
  output may contain credentials. Without redaction those secrets
  persisted in `~/.ghosthunter/audit.log` and the memory palace
  forever (and replicated via Time Machine, Dropbox, iCloud sync).
  New module `ghosthunter.security.secrets_redactor` runs a 9-pattern
  redaction over all on-disk writes — AWS access keys (AKIA / ASIA),
  GitHub tokens (gh[psru]_), Anthropic / OpenAI API keys, JWTs (with
  intentional ordering before bearer tokens), bearer tokens, generic
  auth headers in shell / JSON / YAML forms, GCP service-account
  `private_key` JSON fields, and standalone PEM-armored private keys.
  Each match becomes `[REDACTED:<type>]`, preserving log structure.
- **`ghosthunter purge-history` CLI command** ([#3]). Wipes
  `~/.ghosthunter/chat_history`, `audit.log`, and `palace/` after a
  y/N confirmation. `--yes` / `-y` skips the prompt. Migration path
  for anyone who pasted sensitive output during v1.0.6 (no redaction
  available then). Configuration files are preserved.
- **Pre-prompt injection sanitizer + defensive prompt frame** ([#5]).
  New module `ghosthunter.security.prompt_sanitizer` strips seven
  known prompt-injection shapes ("ignore previous instructions",
  role-redefinition phrases, `<system>` / `<admin>` / `<override>`
  tags, "new instructions:" markers) from command output before the
  prompt is built. Output is also wrapped in a `<command_output>`
  defensive frame instructing Claude to treat the contents as
  untrusted data, not instructions. Best-effort, not absolute — the
  deterministic Layer 1–4 validator still holds regardless. This
  defense exists to prevent misdirected investigations, not
  security-boundary breaches.
- **`awk` blocked in pipes** ([#4]). The previous safe-pipe regex
  `^awk(\s+.+)?$` accepted any awk arguments, leaving awk's
  `system()`, `getline`, and `exec()` builtins reachable in theory.
  Removed awk from `SAFE_PIPE_TARGETS` and added it to
  `BLOCKED_PIPE_TARGETS` (belt-and-braces — the blocklist check runs
  before the safe-list check in `validate_pipes()`). GCP and AWS
  reasoner prompts updated to drop `awk` from the listed safe pipes
  so Claude doesn't propose awk pipes the validator then rejects.
  No legitimate Ghost-hunter use case requires awk; `grep` / `cut` /
  `jq` cover the same ground.
- **Layer 6 approval-bias documented** (audit gap 4, by design).
  Sonnet's semantic check defaults toward approval. The audit
  confirmed this is intentional — Layer 2's static allowlist is the
  deterministic gate; Layer 6 explains and double-checks.
  `SECURITY.md` item 4 already covered this; no code change required.

### Tests

- **+158 security tests** (1097 → 1255 total). Five new awk-blocking
  tests exercise the audit-flagged shapes (`system()`, `getline`,
  `exec()` plus plain and bare awk). Twenty-six new tests for the
  prompt sanitizer cover pattern hits, false-positive guards,
  multi-pattern counting, the defensive wrapper, registry invariants,
  and end-to-end integration with `_format_for_compression`.
  Twenty-nine new tests for the secrets redactor cover all nine
  credential classes, false-positive guards (resource IDs, IAM
  emails, UUIDs, normal CSV rows), `redact_dict()` recursion, and
  pattern-registry invariants.
- **AWS test fixture strings replaced.** `AKIAIOSFODNN7EXAMPLE` and
  `ASIAY34FZKBOKMUTVV7A` were AWS's published example keys — they
  appeared in dozens of public repos' leak-fingerprint databases and
  triggered GitHub Secret Scanning alerts on every fixture commit.
  Replaced with synthetic strings (`AKIATESTFIXTUREXYZ12`,
  `ASIATESTFIXTUREABC56`) that match the redactor regex shape but
  aren't in any leak database.

### Documentation

- `SECURITY.md` items 1 ("Prompt injection via pasted output") and 2
  ("Secrets in pasted output persist to disk") updated with v1.0.8
  mitigation notes — honest about scope. The `chat_history` file is
  prompt_toolkit-owned and remains outside our redaction path; this
  is now explicit.
- New website page <https://matrixgard.com/docs/ghosthunter/security-review>
  publishes the audit verdict, the four documented gaps, and per-gap
  fix tracking. Standing rule: every external review of Ghost-hunter
  is published there in full, with auditor permission.

[#3]: https://github.com/avinash-matrixgard/ghosthunter/issues/3
[#4]: https://github.com/avinash-matrixgard/ghosthunter/issues/4
[#5]: https://github.com/avinash-matrixgard/ghosthunter/issues/5

## [1.0.7] - 2026-04-28

### Changed
- **License: MIT → AGPL-3.0-or-later.** Going forward, Ghost-hunter is licensed under the GNU Affero General Public License v3.0 or later. This protects against the open-source SaaS arbitrage pattern (a third party hosting a fork as a paid service without contributing back) while keeping the project fully OSI-approved open source. Internal use, individual use, and modifications that are not hosted publicly are unaffected. Versions at or before v1.0.6 remain available under MIT terms. See `LICENSE_HISTORY.md` for the full rationale.
- **Trademark declaration.** "Ghost-hunter" is now declared as a trademark of MatrixGard via common-law usage. See `TRADEMARK.md` and `NOTICE`. Forks must rebrand. The AGPL license grants no trademark rights — this is consistent with how Linux®, Mozilla®, Kubernetes®, MongoDB®, Sentry®, Plausible®, Grafana® handle their respective marks.

### Added
- `NOTICE` file at repository root declaring copyright + trademark + license.
- `TRADEMARK.md` documenting the trademark policy, what's permitted, and what requires permission.
- `LICENSE_HISTORY.md` documenting the MIT → AGPL-3.0 transition and rationale.
- `PRIOR_ART.md` documenting the original architectural decisions (dual-model split, 7-layer validator, paranoid mode, demo scenarios) with public timestamps.
- `LICENSE.MIT.original` preserves the original MIT license text for reference.

### PyPI metadata
- License classifier updated from `MIT License` to `GNU Affero General Public License v3 or later (AGPLv3+)`.
- Project license field updated to `AGPL-3.0-or-later` (SPDX identifier).

## [1.0.6] - 2026-04-17

### Added
- **One-gesture command copy.** When Ghosthunter proposes a command in advisor mode it now pushes the command onto the user's system clipboard via the OSC 52 terminal escape sequence — no triple-click, no click-drag, no wrestling with soft-wrapped long commands. Works in iTerm2 (default-on), Kitty, WezTerm, Ghostty, Alacritty (opt-in), and tmux with `set-clipboard on`. Silently no-ops in terminals that don't honour the sequence.
- **`/copy` slash command** as the explicit path when OSC 52 isn't available. At the advisor-mode prompt, type `/copy` to re-put the most recent proposed command on the clipboard. Uses OS-native tools first (pbcopy on macOS, wl-copy / xclip / xsel on Linux, clip.exe on Windows), then OSC 52 as a fallback. Works under SSH, inside tmux, and over other remote sessions.
- **`GHOSTHUNTER_NO_CLIPBOARD` env var** — set to `1` / `true` / `yes` / `on` to disable both paths for users who don't want their clipboard mutated by a tool they didn't explicitly invoke.
- **Clipboard auto-copy also applies to remediation commands** in the conclusion, but only when there's exactly one command across all recommendations — multiple commands would mean guessing which the user wants to run first, and we don't guess.
- New `src/ghosthunter/clipboard.py` module with `write_osc52` and `copy_to_clipboard` helpers. 26 new tests covering OSC 52 emission, non-tty safety, env opt-out, OS-native tool selection, fallback to OSC 52 when native tools are missing, `/copy` slash behaviour, command tracking across turns, and the blocked-command renderer upgrade.

### Changed
- **Blocked-command display now shows WHAT was blocked and WHY, not just the layer code.** Previously `✗ blocked (L2): command not in allowlist` gave users no way to know which command Opus had tried. Now we print the command (dim, indented) and a one-line layer explanation below the error. Applies consistently across `ghosthunter investigate`, the `chat` REPL, and the shared UI renderer via a new `ui.render_command_blocked` helper.
- **Prompt hint line now mentions `/copy`** so the feature is discoverable without reading `/help`. When OSC 52 emission succeeded, the hint also says so — otherwise it's just the `/copy` pointer.

## [1.0.5] - 2026-04-17

### Changed
- **Fix-first conclusion layout.** When Ghosthunter converges on a root cause, the `What to do now` block now renders before the root-cause paragraph, evidence list, and unverified gaps. Users asked for "suggest fixes, not just find the cause and leave" — the recommendations were always there but buried below a long analysis. They now lead.
- **Structured remediations with copy-paste-safe commands.** Each recommendation is tagged with an urgency bucket (`NOW` / `THIS WEEK` / `THIS MONTH` / `MONITORING`), its description, and — where an exact command can make the fix — a command block that renders in the same plain-ASCII format as mid-investigation commands (no Unicode borders, `soft_wrap=True`, triple-click-safe). A separate `Verify with` block shows how to confirm the fix worked.

### Added
- Opus's `investigation_step` tool schema now accepts either a plain string (legacy v1.0.4 shape, still honored) OR a structured object `{urgency, description, command?, verification?}` for each recommendation. The reasoner prompt guides Opus to prefer the object form heavily and to OMIT `command` rather than invent one when the fix is a console click or vendor decision — preserving the authenticity rule.
- 11 new tests covering fix-first layout ordering, structured rendering, urgency sort (canonical order regardless of Opus's emission order), legacy string back-compat, mixed-shape lists, and a schema-level guard against scenario-specific content sneaking into the tool definition.

## [1.0.4] - 2026-04-17

### Fixed
- **`/spike N` no longer crashes mid-investigation** in the direct `ghosthunter investigate` path. The typed `AdvisorSpikeSwitch` exception was only caught by the chat REPL — the CLI path leaked it as a traceback. The advisor CLI now catches it, rebinds the target spike, and restarts the investigation cleanly. Also guards against pathological switch loops with a per-session cap.
- **Commands proposed in advisor mode can now be copy-pasted without mangling.** The "Run this command in your own terminal" panel previously used a Rich `Panel` with Unicode box-drawing borders (`│ ─ ╭`). When a long command soft-wrapped inside the panel and the user triple-clicked or click-dragged to select it, the border characters came with — pasting into a shell produced `unrecognized arguments: │` and pasting into `bq query` produced `Illegal input character "\342"` (`\342` is the first byte of `│` in UTF-8). The command now renders as plain text between ASCII header/footer lines, with `soft_wrap=True` so long commands stay logically contiguous.
- **`bq query` SQL with backticks around fully-qualified table references is no longer blocked.** BigQuery Standard SQL requires `FROM \`project.dataset.table\`` — but Layer 1's backtick check wasn't quote-aware, so every such query was rejected and Opus was producing un-runnable backtick-less SQL as a workaround. The new `has_unquoted_command_substitution` helper mirrors the existing `has_unquoted_redirect` pattern: backticks, `$(`, and `${` are allowed inside single-quoted strings (where bash treats them as literal) and still blocked everywhere bash would actually expand them.

### Added
- Regression test suite `tests/test_paste_safety_v1_0_4.py` — 24 tests covering the new quote-aware substitution logic, end-to-end validator behavior on the exact customer query that was failing, and assertions that the command panel output contains no clipboard-hostile characters (box-drawing glyphs or smart quotes).

## [1.0.3] - 2026-04-17

### Fixed
- **Advisor mode no longer asks the user to decode SKU codes the CSV already explains.** The billing parser now reads `ChargeDescription` (FOCUS 1.0) / `lineItem/LineItemDescription` (AWS CUR) and surfaces a representative description for each top SKU and UsageType contributor — so Opus sees `SKU 4GQWNPC9K2PZAY97 ($209.67) — "$1.624 per On Demand Linux g5.4xlarge Instance Hour"` instead of an opaque SKU ID. Shows up in both the initial prompt to Opus and the CLI's top-contributors display.
- **Advisor mode no longer loops asking "can you look it up?" after the user says they can't.** Added an explicit rule to Opus's system prompt: when the user says they have no CLI access / no console access / "work with what you have", Opus must consolidate into a conclusion based only on the billing data rather than keep asking. Includes a specific template (root cause + honest confidence + `not_verified` list + actionable recommendations).

### Added
- `CostSpike.contributor_descriptions` — a new optional dict carrying `{dim}:{value} → description` mappings. Populated when the billing file has a description column; empty otherwise. Rendered inline in the investigator prompt and in both the CLI + chat renderers.
- 9 new tests covering description column detection, threading through the parser, rendering in the prompt, and the full FOCUS 100K round-trip that motivated the fix.

## [1.0.2] - 2026-04-17

### Fixed
- `investigate` no longer crashes with an opaque `string indices must be integers, not 'str'` when Opus returns hypotheses as strings (or drops `next_action.type`, or similar tool-use shape slips). The reasoner now coerces minor slips, raises a typed `ReasonerSchemaError` on un-coerceable shapes, and the investigator retries once with a corrective nudge before aborting. Caught in the wild running advisor mode on the FinOps Foundation's FOCUS 100K sample.
- Advisor mode banner now correctly says `(aws)` / `(gcp)` for FOCUS 1.0 billing exports. Previously defaulted to `(gcp)` for any FOCUS file because the provider sniffer only recognized AWS CUR / GCP Console column shapes — it now reads the per-row `ProviderName` column (or falls back to `ServiceName` prefix matching).

### Added
- 45 new tests covering the defensive reasoner parsing and FOCUS provider sniffing, including edge cases for mixed-cloud data and unsupported providers (Azure / Oracle → returns None rather than mis-routing).

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
[1.0.6]: https://github.com/avinash-matrixgard/ghosthunter/releases/tag/v1.0.6
[1.0.5]: https://github.com/avinash-matrixgard/ghosthunter/releases/tag/v1.0.5
[1.0.4]: https://github.com/avinash-matrixgard/ghosthunter/releases/tag/v1.0.4
[1.0.3]: https://github.com/avinash-matrixgard/ghosthunter/releases/tag/v1.0.3
[1.0.2]: https://github.com/avinash-matrixgard/ghosthunter/releases/tag/v1.0.2
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
