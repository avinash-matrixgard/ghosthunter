# Ghosthunter

Investigate **why** your cloud costs spiked, not just what changed.

Ghosthunter uses Claude Opus (hypothesis reasoning) and Claude Sonnet
(command execution + output compression) to run a dual-model cost
investigation over your GCP billing data. Security is enforced in code
through a 7-layer validator — the LLM cannot run anything the allowlist
does not permit.

---

## Modes at a glance

| Mode | When to use | Credentials |
|---|---|---|
| **Paranoid (advisor) — default** | Real work data. Ghosthunter never touches your cloud. | None — zero blast radius |
| **Active** | Personal / sandbox projects only. Ghosthunter runs `gcloud` directly. | Read-only GCP creds + `~/.ghosthunter/config.toml` |
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
.venv/bin/pip install rich typer tomli tomli_w anthropic \
    google-cloud-bigquery prompt_toolkit pytest
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
PYTHONPATH=src .venv/bin/python -m ghosthunter.cli demo
```

Replays a bundled DNS cache-bypass attack investigation end to end. No
API calls, no GCP, no billing data. Takes ~30 seconds.

---

## 3. Paranoid (advisor) mode — the normal path

### 3a. Export your billing data

Pick **one** of these paths. Ghosthunter will tell you the exact commands
if you run `ghosthunter billing-template`.

**Option A — BigQuery (recommended, one rich file):**

```bash
bq query --nouse_legacy_sql --format=csv \
  'SELECT
     service.description           AS service,
     sku.description               AS sku,
     project.id                    AS project,
     IFNULL(location.region, location.location) AS location,
     DATE(usage_start_time)        AS usage_start_date,
     SUM(cost)                     AS cost
   FROM `YOUR_PROJECT.billing_export.gcp_billing_export_v1_*`
   WHERE DATE(usage_start_time) >= DATE_SUB(CURRENT_DATE(), INTERVAL 60 DAY)
   GROUP BY service, sku, project, location, usage_start_date
   ORDER BY usage_start_date' > billing.csv
```

**Option B — Console CSVs (no BQ needed):**

1. https://console.cloud.google.com/billing → your account → Reports
2. Pick the date range covering the spike
3. Group by **Service** → Download CSV
4. Change grouping → **SKU** → Download CSV
5. (optional) Group by **Project** → Download CSV

Ghosthunter merges multiple files and cross-infers project↔service
mappings from totals + percent changes.

### 3b. Start the chat REPL

```bash
PYTHONPATH=src .venv/bin/python -m ghosthunter.cli chat billing*.csv
```

Passing files skips the mode picker and goes straight to paranoid mode.
No files → mode picker appears and you choose 1.

### 3c. Drive the investigation

At the `>` prompt:

```
/list             # show detected cost spikes
/spike 0          # investigate the largest spike
```

Ghosthunter (via Opus) will:

1. Form 2–4 competing hypotheses with confidence scores
2. Propose a read-only `gcloud`/`bq` command to test the top hypothesis
3. **Pause** and ask you to run it in your own terminal and paste output back
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

Only safe on a personal/scoped project. Requires read-only GCP
credentials.

```bash
PYTHONPATH=src .venv/bin/python -m ghosthunter.cli init   # creates ~/.ghosthunter/config.toml
PYTHONPATH=src .venv/bin/python -m ghosthunter.cli investigate --active
```

Ghosthunter will query BigQuery billing export directly, detect spikes,
and execute allowlisted `gcloud` commands itself. Every command still
passes through the 7-layer security validator.

**Do not use active mode against an organization where your credentials
have write permission.** Use paranoid mode instead.

---

## 5. Other commands

```bash
ghosthunter audit            # review past investigations (~/.ghosthunter/audit.log)
ghosthunter palace status    # check MemPalace memory integration
ghosthunter billing-template # show billing export recipes
```

---

## Security model — what the validator enforces

Every command — whether Opus proposes it in advisor mode or Sonnet
executes it in active mode — passes through 7 layers:

1. **Fast reject** — no `; && || curl wget bash rm` or unquoted redirects
2. **Allowlist** — must match a specific read-only `gcloud`/`bq`/`gsutil` pattern
3. **Pipe validation** — only safe targets (`head`, `wc`, `jq`, `grep`, `sort`…)
4. **Safety checks** — length cap, no encoding tricks, `bq query` is SELECT-only
5. **Budget limits** — 15 commands / $1 / 10 min per investigation
6. **Sonnet semantic check** — "is this really safe?" final pass
7. **Sandboxed execution**

Security is in code, not prompts. Allowlist is the primary gate — if a
command doesn't match an allowed pattern, it's blocked regardless of
what the LLM claims.

Test suite: 91 validator tests in `tests/test_security.py`.

---

## Project layout

```
src/ghosthunter/
  cli.py              Typer CLI entrypoint
  chat.py             REPL orchestrator + mode picker
  chat_io.py          prompt_toolkit shared session
  investigator.py     Main investigation loop
  hypothesis.py       Hypothesis dataclass + confidence logic
  evidence.py         Evidence chain
  demo.py             Replay bundled scenarios
  models/
    reasoner.py       Claude Opus client + system prompt
    executor.py       Claude Sonnet client (validate + compress)
  security/
    validator.py      7-layer orchestrator
    allowlist.py      GCP read-only command patterns
    blocklist.py      Fast-reject patterns
    pipes.py          Safe pipe validation
  providers/
    gcp.py            Active mode — BigQuery billing + gcloud exec
    billing_file.py   Advisor mode — parse user-exported CSVs
    advisor.py        Print-command / wait-for-paste pseudo-execution
  memory/
    palace.py         Optional MemPalace MCP client (cross-session memory)
```

---

## Troubleshooting

- **`ghosthunter: command not found`** — you haven't installed editable mode. Use the full `PYTHONPATH=src .venv/bin/python -m ghosthunter.cli ...` prefix, or run `.venv/bin/pip install -e .`.
- **`ANTHROPIC_API_KEY not set`** — export the env var (see install step).
- **Opus re-proposes a blocked command** — type `/skip` or `/note try a different angle` to force a pivot.
- **Long JSON output is painful to paste** — save to `/tmp/out.json` and paste the path instead; Ghosthunter reads it directly.

---

## Roadmap

- [ ] Streaming Opus responses (currently blocks ~5–10s per turn)
- [ ] AWS provider (v1.1)
- [ ] Azure provider (v1.2)
- [ ] Autonomous mode with strict guardrails (v1.1)
- [ ] Editable install by default

---

## License

See [LICENSE](LICENSE).
