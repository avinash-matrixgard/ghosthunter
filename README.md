# Ghost-hunter™

[![Python](https://img.shields.io/badge/python-3.12%2B-blue?logo=python&logoColor=white)](https://www.python.org/)
[![License](https://img.shields.io/badge/license-AGPL--3.0--or--later-blue.svg)](LICENSE)
[![Tests](https://img.shields.io/badge/tests-1000%2B-brightgreen.svg)](tests/)
[![Providers](https://img.shields.io/badge/providers-GCP%20%7C%20AWS-orange.svg)](README.md#-providers)
[![FOCUS 1.0](https://img.shields.io/badge/FOCUS-1.0-blueviolet.svg)](https://focus.finops.org/)
[![Status: looking for adopters](https://img.shields.io/badge/status-looking_for_adopters-ff9500.svg)](#-early-adopter-mode--be-the-first-10)

Investigate **why** your cloud costs spiked, not just what changed.

Ghosthunter uses Claude Opus (hypothesis reasoning) and Claude Sonnet
(command execution + output compression) to run a dual-model cost
investigation over your cloud billing data. **Supports GCP and AWS.**
Security is enforced in code through a 7-layer validator — the LLM
cannot run anything the allowlist does not permit.

![Ghosthunter investigating a 875% GCP cost spike in paranoid mode — no cloud credentials, just a billing CSV](https://raw.githubusercontent.com/avinash-matrixgard/ghosthunter/main/docs/demo-paranoid-gcp.gif)

> *Paranoid mode — no cloud access, just a billing CSV. Hypotheses with confidence bars, proposed read-only commands, you stay in control.*

---

## 🧪 Early adopter mode — be the first 10

> [!IMPORTANT]
> Ghosthunter shipped **v1.0.6 to PyPI on April 27, 2026.**
>
> It's been hammered against bundled demo scenarios, synthetic billing data, and a 1,000+ test suite. **It has not yet been run against production cloud accounts at scale** — and we're not going to pretend otherwise.
>
> **Paranoid mode is risk-free** by construction (it never touches your cloud — just reads a billing CSV and prints commands you run yourself). Run it on a real billing export and tell us what worked, what broke, what surprised you.
>
> **What we'll do for the first 10 reporters:**
> - 🚀 Reply within 24 hours
> - 🤝 Walk through your investigation alongside you (free, NDA on request)
> - 🐛 Fix any reproducible bug you hit, fast
> - 🏆 Credit you in CHANGELOG and on [matrixgard.com](https://matrixgard.com)
>
> **How to reach us:**
> - 📬 [Open an issue](https://github.com/avinash-matrixgard/ghosthunter/issues/new)
> - 💌 Email Nash directly — `avinash@matrixgard.com`
>
> *Built in the open. Imperfect on purpose. Looking for the first 10.*

---

## Why Ghosthunter? Comparison vs FinOps tools

Most FinOps tools want admin access and auto-optimize.
Ghosthunter does neither. It's an **investigator**, not an optimizer.

| | Ghosthunter | Vantage / CloudHealth / ProsperOps |
|---|---|---|
| **Access required** | None (paranoid mode reads a CSV) | Cross-account IAM role with broad read |
| **Acts on your cloud** | Never (read-only by default) | Auto-applies "savings recommendations" |
| **Source code** | Open (MIT) — you can audit every command | Closed SaaS |
| **AI model** | Claude Opus (reasoning) + Sonnet (execution) | Rules + heuristics, mostly |
| **What it answers** | *"Why did the bill spike?"* (root cause) | *"How can you cut 5%?"* (optimization) |
| **Pricing** | Free CLI; first manual audit free; paid retainer | $X/mo SaaS, often % of cloud spend |
| **Self-hostable** | Yes — runs locally, your billing data never leaves your machine in advisor mode | No |
| **Multi-cloud** | GCP + AWS (Azure planned) | Multi-cloud, varies by tool |

If you want auto-optimization and trust your vendor with admin keys,
Vantage and CloudHealth are mature options. If you want to **understand
your bill** without giving anyone admin access, Ghosthunter is the tool.

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
# Core (paranoid/advisor mode — no cloud SDK needed)
pip install ghosthunter

# Optional active-mode extras
pip install 'ghosthunter[gcp]'   # GCP active mode (BigQuery + gcloud)
pip install 'ghosthunter[aws]'   # AWS active mode (Cost Explorer via boto3)
pip install 'ghosthunter[all]'   # both providers
```

After install, the `ghosthunter` command is on your PATH.

<details>
<summary>Build from source (contributors)</summary>

```bash
git clone https://github.com/avinash-matrixgard/ghosthunter
cd ghosthunter
python3.12 -m venv .venv
.venv/bin/pip install -e '.[all]'
```

</details>

Set your API key:

```bash
export ANTHROPIC_API_KEY=sk-ant-...
```

## 2. Try the demo (no setup)

```bash
# Random scenario across all providers
ghosthunter demo

# Specific scenario
ghosthunter demo --scenario=aws_nat_gateway_runaway
ghosthunter demo --scenario=dns_cache_bypass

# Filter by provider
ghosthunter demo --provider=aws
ghosthunter demo --provider=gcp
```

Replays a bundled investigation end-to-end with no API calls and no
cloud access. Takes ~30 seconds.

![Ghosthunter investigating an AWS NAT gateway runaway from a Cost Explorer CSV — same paranoid mode](https://raw.githubusercontent.com/avinash-matrixgard/ghosthunter/main/docs/demo-paranoid-aws.gif)

> *Same paranoid mode, AWS Cost Explorer CSV.*

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
ghosthunter investigate \
    -f by-service.csv -f by-usage-type.csv

# Or pick the provider explicitly
ghosthunter investigate \
    --provider=aws -f ce-export.csv

# Or start the chat REPL (mode picker appears unless files are passed)
ghosthunter chat billing*.csv
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
| `/remember <fact>` | Save a fact to the memory palace *(requires MemPalace — `pip install mempalace mcp`; silently no-ops otherwise)* |
| `/recall <query>` | Search memory palace for prior knowledge *(same requirement)* |
| `/quit` | End this investigation, keep chatting |
| `/exit` | Exit Ghosthunter |

---

## 4. Active mode (sandbox only)

Only safe on a personal/scoped account/project. Requires read-only
cloud credentials.

```bash
ghosthunter init
# → prompts for provider (gcp/aws), then provider-specific fields

ghosthunter investigate --active
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
ghosthunter investigate \
    --active --provider=aws
```

**Do not use active mode against an organization where your credentials
have write permission.** Use paranoid mode instead.

---

## 5. Other commands

```bash
ghosthunter audit                       # past investigations (~/.ghosthunter/audit.log, default 20)
ghosthunter audit --limit 50            # show the last 50 entries
ghosthunter palace status               # check MemPalace memory integration
ghosthunter billing-template            # GCP export recipe
ghosthunter billing-template --provider=aws   # AWS export recipe (4 paths: CE CSV, CE JSON, CUR, FOCUS)
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

**Test suite: 1,000+ tests** covering the validator, both providers
(GCP + AWS), billing-file parsing (GCP/AWS/FOCUS), investigator loop,
CLI, advisor mode, memory palace, and demo replay.
Notable files: `tests/test_security.py`, `tests/test_security_aws.py`,
`tests/test_security_aws_full.py`, `tests/test_investigator.py`,
`tests/test_advisor.py`, `tests/test_gcp_provider.py`,
`tests/test_aws_provider.py`, `tests/test_api_retry.py`.

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

- **`ghosthunter: command not found`** — `pip install ghosthunter` hasn't run, or your shell PATH doesn't include the install location (`pip show ghosthunter` to find it). For contributor builds, run `pip install -e '.[all]'` from the repo root.
- **`ANTHROPIC_API_KEY not set`** — export the env var (see install step).
- **Opus re-proposes a blocked command** — type `/skip` or `/note try a different angle` to force a pivot.
- **Long JSON output is painful to paste** — save to `/tmp/out.json` and paste the path instead; Ghosthunter reads it directly.
- **AWS `boto3 is not installed` in active mode** — `pip install boto3` (or `pip install 'ghosthunter[aws]'` if using an editable install with extras).
- **AWS `ExpiredToken` mid-investigation** — your SSO session timed out. `aws sso login --profile <x>` and retry the command Opus proposed.
- **Parquet CUR files** — not supported in v1. Ask AWS to also export CSV, or convert locally with `parquet-tools csv`.

---

## Known Limitations

These are documented caveats, not bugs. See [SECURITY.md](SECURITY.md)
for the full threat model.

- **Prompt injection via pasted output.** Content you paste back from
  your own terminal is compressed by Sonnet before Opus sees it.
  Ghosthunter wraps every paste in an `<UNTRUSTED_COMMAND_OUTPUT>`
  envelope and instructs Sonnet to treat the contents as factual data
  only, but the mitigation is trust-based, not rule-based. **Don't
  paste output from untrusted sources** (logs from a compromised host,
  blobs of unknown origin, attacker-supplied data). Every command Opus
  subsequently proposes still has to pass the 7-layer security
  validator, so this can waste budget but cannot escalate to
  arbitrary command execution.
- **No secret redaction on disk.** If the output you paste contains
  secrets (env dump, session tokens in log lines, a config file with
  credentials), those secrets persist to `~/.ghosthunter/chat_history`
  (every prompt-toolkit line you typed) and may end up in
  `~/.ghosthunter/audit.log` / `~/.ghosthunter/palace/` if memory
  palace is enabled. Redact pastes before handing them to Ghosthunter;
  delete the relevant files if something slips through.
- **Opus can loop on a blocked command.** If Opus re-proposes the same
  rejected command twice, use `/skip` or `/note <hint>` to force a
  pivot. Budget caps keep the blast radius small.
- **Per-investigation budget caps.** 15 commands / $1 / 10 minutes by
  default. Hitting any one aborts the investigation. Tune via
  `~/.ghosthunter/config.toml`. AWS active mode additionally tracks
  Cost Explorer API calls (~$0.01 each) in the audit log.
- **Streaming is not implemented.** Each Opus turn blocks 5–15 seconds
  while the API call completes. A live spinner shows the current phase
  + elapsed time so the UI doesn't look frozen.
- **CUR Parquet files not supported.** Advisor mode reads CSV only
  (GCP Console exports, AWS CUR CSV, FOCUS 1.0 CSV, Cost Explorer CSV
  / JSON). Convert Parquet to CSV externally.
- **Multi-account AWS Organizations aggregation** — one account per
  run. Point Ghosthunter at each account's billing export separately.
- **Azure / OCI / other providers** — not shipped. The provider
  abstraction supports them; implementations are welcome as PRs.
- **AWS active mode requires `boto3`.** Install via
  `pip install 'ghosthunter[aws]'`. Advisor mode doesn't need it —
  advisor mode works with a billing file and never calls the AWS API.
- **macOS / Linux only in v1.** Windows support is untested. The
  advisor-mode flow should work via WSL; active mode may hit
  subprocess-environment edge cases.
- **Layer 6 is judgment, not rules.** The Sonnet-based semantic
  validator caps damage beyond the regex allowlist but isn't
  infallible. Layer 2's static allowlist is the primary gate.

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

## FAQ

**Will Ghosthunter touch my cloud?**
Not in paranoid mode (the default). It prints proposed read-only commands,
you run them in your own terminal, you paste the output back. Active mode
is opt-in, sandbox-only, and still passes every command through the
7-layer validator before execution.

**How is this different from a FinOps SaaS like Vantage or CloudHealth?**
Those tools auto-optimize. Ghosthunter investigates. They want admin
keys; Ghosthunter wants a CSV. They're closed-source SaaS; Ghosthunter
is MIT-licensed CLI you can audit line-by-line. Use both if you have
budget. Use Ghosthunter if you don't, or if you can't give your vendor
admin access.

**Why open source?**
We're a security practice (MatrixGard). Black-box "AI" tools that touch
production cloud aren't a fit for that worldview. Verifiability matters.

**Do you store my billing data?**
Advisor mode keeps everything on your machine. No telemetry, no upload,
no analytics. Active mode also runs locally — it queries your cloud
directly with credentials you control. Audit logs land in
`~/.ghosthunter/audit.log` for your own review.

**What clouds are supported?**
GCP and AWS today. Azure provider is planned for v1.2. The provider
abstraction (`src/ghosthunter/providers/base.py`) accepts community PRs.

**Can I use my own Anthropic API key?**
Yes — Ghosthunter reads `ANTHROPIC_API_KEY` from your env. Per-investigation
budget caps (15 commands / $1 / 10 min) keep blast radius small.

**How fast can I run my first investigation?**
With `pip install ghosthunter` + `ghosthunter demo` — about 30 seconds.
With your own GCP / AWS billing export — about 5 minutes including
the export download.

**Is there a paid version?**
The CLI is free forever, MIT-licensed. If you want a manual audit
walked-through by a human (the team that built Ghosthunter), see
[matrixgard.com](https://matrixgard.com) — first 60-minute audit is
free under NDA.

**How do I report a security issue?**
See [SECURITY.md](SECURITY.md). Private vuln reporting via GitHub
Security Advisories.

**What's the roadmap?**
See the [Roadmap](#roadmap) section. AWS provider, Azure provider,
streaming Opus responses, multi-account AWS Organizations, autonomous
mode with strict guardrails.

---

## License

See [LICENSE](LICENSE).

---

## Built by MatrixGard

Ghosthunter is built and maintained by [MatrixGard](https://matrixgard.com)
— a fractional DevSecOps practice for pre-seed and seed startups.

If you'd rather hire a human to investigate your cloud bill alongside
the tool, the first 60-minute audit is free under NDA. Get in touch at
[matrixgard.com](https://matrixgard.com).
