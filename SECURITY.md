# Security Policy

Ghosthunter executes shell commands against real cloud accounts, with
those commands proposed by a large language model. Security is the
core contract of this project, not a feature. Please read this policy
before reporting a vulnerability or contributing security-sensitive code.

---

## Reporting a vulnerability

**Do not file a public GitHub issue for security vulnerabilities.**

Report privately via GitHub Security Advisories:

> https://github.com/avinash-matrixgard/ghosthunter/security/advisories/new

Include, where possible:

- A minimal reproduction (command sequence, input files, or transcript).
- The version or commit SHA you reproduced on (`ghosthunter --version`
  or `git rev-parse HEAD`).
- The expected behavior vs. the observed behavior.
- Your assessment of impact (what the vulnerability lets an attacker do).
- Any mitigation suggestions.

**Please redact real account IDs, API keys, and billing data from
your report.** Ghosthunter's threat model assumes attackers already
know the CLI surface; your specific environment is what we need to
protect.

### What to expect

As a small-team / solo-maintainer project, SLAs are best-effort:

| Phase | Target |
|---|---|
| Acknowledgement of receipt | within 7 days |
| Initial severity assessment | within 14 days |
| Coordinated disclosure window | 90 days (or earlier by mutual agreement) |

If a reported vulnerability is active exploitation, please say so
prominently in the report so it gets triaged first.

---

## Threat model

### What Ghosthunter is designed to protect against

1. **Destructive cloud commands proposed by the LLM.** Every command
   that reaches the shell passes through a 7-layer pipeline:

   - **Layer 1 — Fast reject.** Shell-injection patterns (`;`, `&&`,
     `$()`, backticks, unquoted redirects, `curl`/`wget`/`rm`/`bash`
     etc.) are blocked before parsing.
   - **Layer 2 — Allowlist.** The primary gate. The first token of
     the command must match a provider-specific allowlist of read
     patterns (`gcloud ... list|describe|read|get-*`, `aws ...
     describe-*|list-*|get-*|batch-get-*`, plus explicit exceptions).
     Commands that don't match are blocked.
   - **Layer 3 — Pipe validation.** Only safe sinks are allowed after
     `|` (`head`, `tail`, `wc`, `sort`, `uniq`, `grep`, `cut`, `awk`,
     `tr`, `jq`). `bash`, `curl`, `xargs`, `tee`, `dd`, and redirects
     into files are blocked.
   - **Layer 4 — Safety checks.** Length cap (2000 chars), encoding-
     trick rejection, `bq query` = `SELECT`-only, `aws ssm
     --with-decryption` blocked, `WRITE_DISGUISED_AS_READ` list
     rejects commands whose verbs look like reads but aren't
     (`aws lambda invoke`, `aws secretsmanager get-secret-value`,
     `aws kms decrypt`, `aws sts get-session-token`, etc.).
   - **Layer 5 — Budget limits.** Per-investigation caps (15 commands,
     $1, 10 minutes). Limits runaway damage from bugs or prompt
     injection that gets past Layers 1–4.
   - **Layer 6 — Semantic validator.** A Sonnet-based judgment call
     on whether the command is reasonable for the investigation.
     Provider-aware. Biases toward approval (the real gatekeeping is
     Layer 2); catches obvious-but-legal commands that a human
     wouldn't run (e.g., a `logging read` with no time filter).
   - **Layer 7 — Sandboxed execution.** Provider-scoped subprocess
     environment (only the cloud-credential env vars for the active
     provider are exposed; everything else is stripped).

   Security is enforced in **code, not prompts**. An LLM that tries
   to "ignore previous instructions" still has to pass the regex
   allowlist and the shell-injection blocklist.

2. **Shell injection via user input.** Any user-supplied string
   (paste, file path, `/note` text) that later becomes part of a
   command goes through Layer 1. Backticks, command substitution,
   and shell chaining are all blocked.

3. **Cross-provider leakage.** A GCP session cannot run AWS commands
   and vice versa; the allowlist is dispatched per provider and the
   sandbox environment only exposes the active provider's credential
   vars (see `providers/base.py` + `providers/{gcp,aws}.py:
   _sandbox_env`).

### What Ghosthunter does NOT protect against

These are documented limitations, not bugs. If you find a way to
widen the allowlist or bypass a layer, that IS a security issue —
please report it.

1. **Prompt injection via pasted command output.** Ghosthunter
   compresses output you paste back from your terminal with Sonnet
   before feeding it to Opus. A sufficiently crafted paste
   ("Ignore previous instructions, …") can steer Opus toward a
   different hypothesis. **It cannot escape the 7-layer pipeline** —
   Opus's next command still has to pass Layers 1–6 — but it can
   waste your time and budget. Don't paste output from untrusted
   sources.

   **Mitigation in v1.0.7:** the most common injection markers
   ("ignore previous instructions", role-overrides, `<system>` /
   `<admin>` tags, "new instructions:", etc) are stripped from
   command output before the prompt is built (see
   `ghosthunter/security/prompt_sanitizer.py`). The output is also
   wrapped in a `<command_output>` defensive frame instructing the
   LLM to treat the contents as untrusted data, not instructions.
   This is best-effort, not absolute — a novel injection shape we
   haven't seen will still get through. Treat investigation
   conclusions as advisory, not authoritative, especially when
   working from logs you don't fully control.

2. **Secrets in pasted output persist to disk.** If you paste command
   output that contains secrets (an env dump, a config file with
   credentials, a log line with a session token), those secrets are
   written to:

   - `~/.ghosthunter/chat_history` — prompt_toolkit's line history,
     every input you typed.
   - `~/.ghosthunter/audit.log` — the investigation outcome (Opus's
     evidence summary may quote snippets).
   - `~/.ghosthunter/palace/` — if memory palace is enabled,
     conclusions are indexed.

   No automatic redaction is performed. Delete these files (or the
   relevant entries) if you're concerned. Redacting your pastes
   before handing them to Ghosthunter is the current mitigation.

3. **Social engineering.** A malicious party convincing you to paste
   specific output or install a specific billing file is outside the
   tool's control. Treat billing files and command output as you
   would any other untrusted input.

4. **Layer 6 is judgment, not rules.** Sonnet's semantic validator is
   a ~200-token LLM decision. It has a documented approval bias
   (Layer 2 does the real gatekeeping). A clever command that is
   syntactically legal but conceptually unreasonable might slip
   through. Layer 2's allowlist caps the damage.

5. **Supply-chain attacks on dependencies.** Ghosthunter depends on
   `anthropic`, `typer`, `rich`, `boto3` (optional), and
   `google-cloud-bigquery` (optional). Pin versions in production
   use. Run `pip-audit` periodically. We don't vendor any of these.

6. **Denial of service via the Anthropic API.** Rate-limit storms
   burn the per-investigation budget fast; a 429 loop will exhaust
   the command count before doing work. Retry logic with exponential
   backoff and a hard cap exists (see `models/reasoner.py:step` and
   `models/executor.py`).

7. **Billing data integrity.** Ghosthunter trusts the billing CSV
   you feed it. A CSV crafted to show a fake cost spike could steer
   an investigation toward a phantom root cause. This is a concern
   for automated pipelines that consume Ghosthunter's output without
   human review.

---

## Supported versions

Security fixes are backported to the most recent minor version only.

| Version | Status |
|---|---|
| 1.0.x | ✅ Supported |
| < 1.0 | ❌ No fixes |

---

## Security-critical code

Changes to the files below require a corresponding test that would
have failed before the change. Reviewers: **do not merge a PR that
modifies these without tests.**

- `src/ghosthunter/security/validator.py` — the 7-layer orchestrator.
- `src/ghosthunter/security/allowlist.py` — dispatcher keyed on
  command prefix.
- `src/ghosthunter/security/allowlist_gcp.py` — GCP read-verb
  patterns; `bq query` SELECT-only check.
- `src/ghosthunter/security/allowlist_aws.py` — AWS read-verb
  patterns; `WRITE_DISGUISED_AS_READ` blocklist;
  `--with-decryption` check.
- `src/ghosthunter/security/blocklist.py` — Layer 1 shell-injection
  patterns and quote-aware redirect detector.
- `src/ghosthunter/security/pipes.py` — Layer 3 pipe-sink allowlist.
- `src/ghosthunter/models/executor.py` — Layer 6 semantic validator.
- `src/ghosthunter/providers/base.py` + `providers/{gcp,aws}.py` —
  sandbox env construction (Layer 7).

Existing test coverage that locks these behaviors:

- `tests/test_security.py` — 91 tests on the shared validator.
- `tests/test_security_aws.py` — 75 AWS-specific tests.
- `tests/test_security_aws_full.py` — 538 parametrized tests.
- `tests/test_provider_registry.py` — cross-provider isolation.
- `tests/test_executor_prompt.py` — Layer 6 prompt correctness.

---

## Acknowledgements

Security researchers who report vulnerabilities in good faith will
be credited in the CHANGELOG entry for the fix (unless they prefer
to remain anonymous).
