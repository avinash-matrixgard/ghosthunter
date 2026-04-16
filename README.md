# Ghosthunter

Investigate **why** your cloud costs spiked, not just what changed.

Ghosthunter uses Claude Opus (hypothesis reasoning) and Claude Sonnet
(command execution + output compression) to run a dual-model cost
investigation over your cloud billing data. **Supports GCP and AWS.**
Security is enforced in code through a 7-layer validator — the LLM
cannot run anything the allowlist does not permit.

---

## Modes at a glance

| Mode | When to use | Credentials |
|---|---|---|
| **Paranoid (advisor) — default** | Real work data. Ghosthunter never touches your cloud. | None — zero blast radius |
| **Active** | Personal / sandbox projects only. Ghosthunter runs `gcloud` or `aws` directly. | Read-only GCP or AWS creds + `~/.ghosthunter/config.toml` |
| **Demo** | First look, screenshots, offline walkthrough. | None — pre-recorded, no API calls |
| **Audit** | Review past investigations. | None — reads `~/.ghosthunter/audit.log` |

Paranoid mode is the default and the one you should use for anything
touching production. It prints the proposed command, you paste the
output back — Ghosthunter reasons, you keep control.

---

## 1. Install

Requires Python 3.12+.

```bash
git clone https://github.com/avinash-matrixgard/ghosthunter
cd ghosthunter

python3.12 -m venv .venv
.venv/bin/pip install -U pip

# Core install (paranoid/advisor mode works without any cloud SDK)
.venv/bin/pip install rich typer tomli tomli_w anthropic \
    prompt_toolkit pytest

# OPTIONAL: active-mode extras
.venv/bin/pip install 'google-cloud-bigquery>=3.25'   # GCP active mode
.venv/bin/pip install 'boto3>=1.34'                    # AWS active mode
```

Set your API key:

```bash
export ANTHROPIC_API_KEY=sk-ant-...
```

All commands below assume you use the venv Python. If you prefer a real
`ghosthunter` command on your PATH, run `.venv/bin/pip install -e .`
once and drop the `PYTHONPATH=src .venv/bin/python -m ghosthunter.cli`
prefix everywhere.

---

## 2. Try the demo (no setup)

```bash
# Random scenario across all providers
PYTHONPATH=src .venv/bin/python -m ghosthunter.cli demo

# Specific scenario
PYTHONPATH=src .venv/bin/python -m ghosthunter.cli demo --scenario=aws_nat_gateway_runaway
PYTHONPATH=src .venv/bin/python -m ghosthunter.cli demo --scenario=dns_cache_bypass

# Filter by provider
PYTHONPATH=src .venv/bin/python -m ghosthunter.cli demo --provider=aws
PYTHONPATH=src .venv/bin/python -m ghosthunter.cli demo --provider=gcp
```

Replays a bundled investigation end-to-end with no API calls and no
cloud access. Takes ~30 seconds.

**Bundled scenarios:**
- **GCP**: `dns_cache_bypass`, `nat_egress_runaway`, `bigquery_full_scan`,
  `orphaned_disks`, `gke_autoscaler_loop`
- **AWS**: `aws_nat_gateway_runaway` (missing S3 VPC endpoint),
  `aws_s3_lifecycle_miss` (bucket with no lifecycle policy)

---

## 3. Paranoid (advisor) mode — the normal path

Ghosthunter sniffs the provider from your billing file's column headers,
so `--provider` is usually unnecessary. Pass it explicitly if you want to
override the sniff.

### 3a. Export your billing data

**GCP:** run `ghosthunter billing-template` for the exact commands.
Pick one of:
- **Option A (recommended)**: a single rich BigQuery export with
  service, sku, project, location, date, cost.
- **Option B**: Console Reports CSV downloads (one per grouping) —
  merge with multiple `-f` flags.

**AWS:** run `ghosthunter billing-template --provider=aws`. Four paths:
- **Option A** — Cost Explorer UI CSV downloads grouped by Service,
  UsageType, Linked Account. Merge with multiple `-f` flags.
- **Option B** — `aws ce get-cost-and-usage` JSON piped to a file.
- **Option C** — CUR (Cost and Usage Report) CSV from S3 (richest; CUR
  Parquet is not supported in v1).
- **Option D** — FOCUS 1.0 CSV (cross-cloud FinOps Foundation spec).
  Public samples at
  [FinOps-Open-Cost-and-Usage-Spec/FOCUS-Sample-Data](https://github.com/FinOps-Open-Cost-and-Usage-Spec/FOCUS-Sample-Data)
  let you try Ghosthunter with no cloud account at all.

### 3b. Run an investigation

```bash
# Auto-detects AWS from CUR/CE column headers
PYTHONPATH=src .venv/bin/python -m ghosthunter.cli investigate \
    -f by-service.csv -f by-usage-type.csv

# Or pick the provider explicitly
PYTHONPATH=src .venv/bin/python -m ghosthunter.cli investigate \
    --provider=aws -f ce-export.csv

# Or start the chat REPL (mode picker appears unless files are passed)
PYTHONPATH=src .venv/bin/python -m ghosthunter.cli chat billing*.csv
```

### 3c. Drive the investigation

At the `>` prompt inside the chat REPL:

```
/list             # show detected cost spikes
/spike 0          # investigate the largest spike
```

Ghosthunter (via Opus) will:

1. Form 2–4 competing hypotheses with confidence scores
2. Propose a read-only command (`gcloud`/`bq`/`gsutil` for GCP, `aws`
   for AWS) to test the top hypothesis
3. **Pause** and ask you to run it in your own terminal and paste
   output back
4. Update confidences as evidence comes in
5. Conclude when one hypothesis hits 85% confidence

Controls during an investigation:

| Command | Effect |
|---|---|
| (paste command output) | Feed evidence back to Opus |
| `<free text question>` | Ask Opus anything; it answers in the next turn |
| `/note <text>` | Inject a note into Opus's context |
| `/hypotheses` | Show current confidence bars |
| `/skip` | Skip this command, ask Opus to try something else |
| `/spike N` | Switch mid-flight to a different spike |
| `/remember <fact>` | Save a fact to the memory palace (if installed) |
| `/recall <query>` | Search memory palace for prior knowledge |
| `/quit` | End this investigation, keep chatting |
| `/exit` | Exit Ghosthunter |

---

## 4. Active mode (sandbox only)

Only safe on a personal/scoped account/project. Requires read-only
cloud credentials.

```bash
PYTHONPATH=src .venv/bin/python -m ghosthunter.cli init
# → prompts for provider (gcp/aws), then provider-specific fields

PYTHONPATH=src .venv/bin/python -m ghosthunter.cli investigate --active
```

**GCP:** Ghosthunter queries BigQuery billing export directly, detects
spikes, and executes allowlisted `gcloud`/`bq`/`gsutil` commands itself.
Requires `google-cloud-bigquery` installed and
`GOOGLE_APPLICATION_CREDENTIALS` / `gcloud auth application-default`
credentials.

**AWS:** Ghosthunter queries Cost Explorer via `boto3` (one `get_cost_and_usage`
call per window + optional follow-up by USAGE_TYPE), detects spikes, and
executes allowlisted `aws` commands itself. Uses your default credential
chain — `AWS_PROFILE`, env-var keys, SSO, or IAM role. Cost Explorer API
is metered at ~$0.01 per request — Ghosthunter shows a one-time banner
and persists your acknowledgment in `~/.ghosthunter/config.toml`. Each
investigation's CE call count lands in the audit log.

```bash
# AWS active-mode example
export AWS_PROFILE=dev-sandbox
PYTHONPATH=src .venv/bin/python -m ghosthunter.cli investigate \
    --active --provider=aws
```

**Do not use active mode against an organization where your credentials
have write permission.** Use paranoid mode instead.

---

## 5. Other commands

```bash
ghosthunter audit                       # past investigations (~/.ghosthunter/audit.log)
ghosthunter palace status               # check MemPalace memory integration
ghosthunter billing-template            # GCP export recipe
ghosthunter billing-template --provider=aws   # AWS export recipe (3 paths)
```

The audit table shows provider, service, result, command count (with CE
API call count for AWS active-mode runs), and root cause.

---

## Security model — what the validator enforces

Every command — whether Opus proposes it in advisor mode or Sonnet
executes it in active mode — passes through 7 layers:

1. **Fast reject** — no `; && || curl wget bash rm` or unquoted redirects
2. **Allowlist** (provider-aware) — must match a specific read-only pattern:
   - GCP: `gcloud` / `bq` / `gsutil`
   - AWS: `aws <service> describe-*|list-*|get-*|batch-get-*` plus
     explicit patterns for non-read-shaped reads (`aws s3 ls`,
     `aws dynamodb scan`, `aws ce get-*`, `aws cloudtrail lookup-events`,
     …)
3. **Pipe validation** — only safe targets (`head`, `wc`, `jq`, `grep`, `sort`…)
4. **Safety checks** — length cap, no encoding tricks. SELECT-only for
   `bq query` on GCP. `--with-decryption` blocked on AWS SSM Parameter
   Store reads. `WRITE_DISGUISED_AS_READ` list blocks verbs that look
   like reads but cause side effects or leak secrets:
   `aws lambda invoke`, `secretsmanager get-secret-value`, `ec2 get-password-data`,
   `sts assume-role`, `kms decrypt`, Bedrock/SageMaker `invoke-*`,
   Athena `start-query-execution`, etc.
5. **Budget limits** — 15 commands / $1 / 10 min per investigation
6. **Sonnet semantic check** — "is this really safe?" final pass
7. **Sandboxed execution** with provider-scoped env (GCP creds for GCP
   mode, AWS profile/region/session token for AWS mode; nothing else)

Security is in code, not prompts. Allowlist is the primary gate — if a
command doesn't match an allowed pattern, it's blocked regardless of
what the LLM claims.

**Test suite: 860+ validator + provider + demo tests.**
See `tests/test_security.py`, `tests/test_security_aws.py`,
`tests/test_security_aws_full.py`.

---

## Project layout

```
src/ghosthunter/
  cli.py              Typer CLI entrypoint + provider sniffing
  chat.py             REPL orchestrator + mode picker
  chat_io.py          prompt_toolkit shared session
  investigator.py     Main investigation loop
  hypothesis.py       Hypothesis dataclass + confidence logic
  evidence.py         Evidence chain
  demo.py             Replay bundled scenarios (GCP + AWS)
  models/
    reasoner.py       Claude Opus client + provider-aware system prompt
    executor.py       Claude Sonnet client (validate + compress)
  security/
    validator.py      7-layer orchestrator, provider-parametrized
    allowlist.py      Dispatcher keyed on command prefix
    allowlist_gcp.py  gcloud/bq/gsutil patterns + bq SELECT-only
    allowlist_aws.py  aws patterns + BASE_READ_RULE +
                      WRITE_DISGUISED_AS_READ blocklist
    blocklist.py      Fast-reject patterns (shell injection)
    pipes.py          Safe pipe validation
  providers/
    base.py           BaseProvider ABC + CostSpike / CommandResult
    gcp.py            GCPProvider — BigQuery billing + gcloud exec
    aws.py            AWSProvider — CE via boto3 + aws CLI exec
    billing_file.py   Advisor mode — parse CE/CUR/Console CSVs
    advisor.py        Print-command / wait-for-paste pseudo-execution
  memory/
    palace.py         Optional MemPalace MCP client (cross-session memory)
sample_data/
  demo_script.json    GCP + AWS bundled scenarios (replay without API)
```

---

## Troubleshooting

- **`ghosthunter: command not found`** — you haven't installed editable mode. Use the full `PYTHONPATH=src .venv/bin/python -m ghosthunter.cli ...` prefix, or run `.venv/bin/pip install -e .`.
- **`ANTHROPIC_API_KEY not set`** — export the env var (see install step).
- **Opus re-proposes a blocked command** — type `/skip` or `/note try a different angle` to force a pivot.
- **Long JSON output is painful to paste** — save to `/tmp/out.json` and paste the path instead; Ghosthunter reads it directly.
- **AWS `boto3 is not installed` in active mode** — `pip install boto3` (or `pip install 'ghosthunter[aws]'` if using an editable install with extras).
- **AWS `ExpiredToken` mid-investigation** — your SSO session timed out. `aws sso login --profile <x>` and retry the command Opus proposed.
- **Parquet CUR files** — not supported in v1. Ask AWS to also export CSV, or convert locally with `parquet-tools csv`.

---

## Roadmap

- [x] **GCP provider** (v1.0)
- [x] **AWS provider** — advisor + active modes, full allowlist catalog (v1.0)
- [ ] Azure provider (v1.2)
- [ ] Streaming Opus responses (currently blocks ~5–10s per turn)
- [ ] Autonomous mode with strict guardrails (v1.1)
- [ ] Multi-account AWS Organizations aggregation
- [ ] CUR Parquet support (requires `pyarrow`)
- [ ] Editable install by default

---

## License

See [LICENSE](LICENSE).
