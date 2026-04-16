# Contributing to Ghosthunter

Thanks for your interest in making Ghosthunter better. This guide
covers the quick path from clone to merged PR.

---

## Ground rules

1. **Security is not a feature — it's the core contract.** Ghosthunter
   executes shell commands with LLM-generated input against real cloud
   accounts. Any change that touches the 7-layer validator needs a
   test case that would have caught the old behavior AND a test case
   for the new one. See [SECURITY.md](SECURITY.md) for the threat
   model and disclosure policy.

2. **Be kind in issues and PRs.** See
   [CODE_OF_CONDUCT.md](CODE_OF_CONDUCT.md).

3. **Small, focused PRs.** One concern per PR. It's easier to
   review and revert if needed.

---

## Local setup

Requires Python 3.12+.

```bash
git clone https://github.com/avinash-matrixgard/ghosthunter
cd ghosthunter

python3.12 -m venv .venv
.venv/bin/pip install -U pip
.venv/bin/pip install -e .                 # core
.venv/bin/pip install -e '.[aws]'          # + boto3 for AWS active mode
.venv/bin/pip install -e '.[gcp]'          # + google-cloud for GCP active mode

.venv/bin/pip install pytest ruff pytest-asyncio
```

Activate the venv and you should have `ghosthunter` on PATH.

```bash
source .venv/bin/activate
ghosthunter --help
```

---

## Running tests

All changes must keep the full suite green.

```bash
pytest -q                              # runs all 895+ tests
pytest tests/test_security.py -v       # validator-only
pytest -k aws                          # AWS-related
```

Lint:

```bash
ruff check src/ tests/
```

---

## Branch & commit style

- Branch from `main`. Name branches after the work: `fix/bq-backtick`,
  `feat/azure-provider`, `docs/readme-aws`.
- Conventional-ish commit messages:
  `feat:`, `fix:`, `docs:`, `refactor:`, `test:`, `chore:`.
- Squash noisy WIP commits before merging.

---

## Writing tests

When you add a new security allowlist pattern, add both:
- A **positive** test: the intended read-only command is allowed.
- A **negative** test: a dangerous variant is rejected at the right layer.

When you touch the investigator or provider, mock the Anthropic
client (see `tests/test_aws_provider.py` for the pattern with
`MagicMock`) so tests don't hit the real API.

---

## Adding a cloud provider

The surface is defined in `providers/base.py:BaseProvider`. The
current GCP and AWS implementations are the reference. You'll also
need:

- `security/allowlist_<provider>.py` — command allowlist + any
  `WRITE_DISGUISED_AS_READ` entries for your provider's CLI.
- `providers/billing_file.py` — extend the column-alias tuples so
  users can feed your provider's cost export CSVs in advisor mode.
- A provider-specific block in `models/reasoner.py:_PROVIDER_RULES`
  telling Opus the CLI's read-verb vocabulary.
- At least one demo scenario in `sample_data/demo_script.json`.
- Tests for all of the above.

---

## Reporting bugs

Open an issue with:
1. What you expected to happen.
2. What actually happened.
3. The `ghosthunter --version` or commit SHA you're on.
4. Your Python version and OS.
5. The smallest reproduction you can find. **Do not paste real
   billing data, account IDs, or API keys** — sanitize first.

For **security issues**, follow [SECURITY.md](SECURITY.md) — **do not
file a public issue**.

---

## Proposing features

File an issue describing the use case before writing code. The
security-surface impact of any feature is the first thing we'll
discuss — even "nice UX tweaks" can change what an attacker can
smuggle through.
