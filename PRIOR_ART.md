# Prior Art / Architecture Provenance

This document records the architectural decisions and original ideas
underlying Ghost-hunter, with timestamps. It serves as a public record
of provenance for any future trademark, patent, or attribution dispute.

## Project genesis

- **First commit**: 2026-03-27 (private repository, MatrixGard internal)
- **First public release**: 2026-04-27 (v1.0.6 to PyPI under MIT,
  relicensed to AGPL-3.0 on 2026-04-28)
- **Originator**: Avinash-MatrixGard, MatrixGard
- **Project home**: https://github.com/avinash-matrixgard/ghosthunter
- **Original use case**: Tool the originator built for his own fractional
  DevSecOps practice to investigate cloud cost spikes from billing CSVs
  without requiring admin credentials on client cloud accounts.

## Original architectural decisions

The following design choices are unique to Ghost-hunter and were
established through public commits during development:

### 1. Dual-model agent split (Opus reasons, Sonnet validates)

Most LLM-based developer tools in 2026 use a single model for both
reasoning and execution. Ghost-hunter deliberately splits these into
two distinct Anthropic Claude models:

- **Claude Opus** — handles hypothesis generation, confidence scoring,
  and root-cause reasoning. The "investigator" model.
- **Claude Sonnet** — handles command validation, output compression,
  and the security validator's semantic check. The "guardian" model.

The split is documented at `src/ghosthunter/models/reasoner.py` (Opus)
and `src/ghosthunter/models/executor.py` (Sonnet), each with its own
provider-aware system prompt.

**Established**: between v1.0.0 and v1.0.6 (March-April 2026, see git
log for exact commits).

### 2. 7-layer command security validator

Every command — whether proposed by Opus in advisor mode or executed by
Sonnet in active mode — passes through 7 distinct security layers
enforced in code, NOT in prompts:

1. Fast reject (regex against shell-injection patterns)
2. Provider-aware allowlist (gcloud/bq/gsutil for GCP, aws describe-*/
   list-*/get-* for AWS, with a `WRITE_DISGUISED_AS_READ` blocklist for
   verbs that look like reads but cause side effects)
3. Pipe target validation (only safe targets like head, wc, jq, grep, sort)
4. Safety checks (length cap, no encoding tricks, SELECT-only on
   `bq query`, blocklist for secret-revealing reads)
5. Per-investigation budget limits (15 commands / $1 / 10 minutes)
6. Sonnet semantic check (LLM-based final pass: "is this really safe?")
7. Sandboxed execution (provider-scoped env, no other credentials)

The architecture is documented at `src/ghosthunter/security/`.

**Established**: v1.0.0 onward.

### 3. Paranoid mode as the default

Most cloud-cost tools require admin credentials. Ghost-hunter's default
"paranoid" mode does the opposite:

- Reads only a billing CSV (or FOCUS 1.0 export) — no cloud access
- Prints commands to the user's terminal
- The user runs the command in their own shell
- The user pastes output back
- Ghost-hunter reasons over the pasted output

The implementation is documented at
`src/ghosthunter/providers/advisor.py`.

**Established**: v1.0.0 onward.

### 4. Demo mode with bundled scenarios

Ghost-hunter ships with 7 fully-bundled investigation scenarios (5 GCP,
2 AWS) that replay end-to-end without any API calls or cloud access.
This enables zero-setup evaluation:

- `dns_cache_bypass`, `nat_egress_runaway`, `bigquery_full_scan`,
  `orphaned_disks`, `gke_autoscaler_loop` (GCP)
- `aws_nat_gateway_runaway`, `aws_s3_lifecycle_miss` (AWS)

Defined in `sample_data/demo_script.json`.

**Established**: v1.0.0 onward.

## Originating context

The original problem statement, recorded in the project's first README:

> Investigate **why** your cloud costs spiked, not just what changed.
>
> Ghosthunter uses Claude Opus (hypothesis reasoning) and Claude Sonnet
> (command execution + output compression) to run a dual-model cost
> investigation over your cloud billing data.

This framing — investigation over optimization, dual-model architecture,
read-only-by-default — distinguishes Ghost-hunter from existing FinOps
tools (Vantage, CloudHealth, ProsperOps, CloudKeeper) which focus on
auto-optimization and require admin credentials.

## Public timestamps

For independent verification of the timeline:

- **PyPI v1.0.6 publication**: https://pypi.org/project/ghosthunter/
  (timestamped by PyPI)
- **GitHub release v1.0.6**:
  https://github.com/avinash-matrixgard/ghosthunter/releases/tag/v1.0.6
- **Git commit history**: `git log --all` on the public repository
- **LinkedIn announcement**: scheduled for 2026-04-29
- **PyPI Internet Archive snapshots**: indexable via web.archive.org
- **awesome-finops PR #25**: filed 2026-04-27, public timestamp
- **awesome-ai-agents-2026 PR #192**: filed 2026-04-27, public timestamp

## Contact

Questions about provenance, attribution, or licensing should be
directed to **avinash@matrixgard.com**.
