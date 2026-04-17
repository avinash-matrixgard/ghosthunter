# Ghosthunter Benchmark

A reproducible detection benchmark for Ghosthunter, inspired by Aider's
[code-editing leaderboard](https://github.com/Aider-AI/aider/tree/main/benchmark):
instead of waiting for real users on real cloud bills to tell us whether
the tool works, we ship a suite of synthetic-but-plausible cost-spike
scenarios with written ground truth and score every release against
them.

## What this validates

**Layer 1 (this directory, today).** The non-AI part of the pipeline:
billing-file parsing, column auto-detection, spike detection, ranking,
and cost-magnitude arithmetic. Zero Anthropic API calls. Fully
deterministic.

For each scenario we ask: **given only the CSV, does Ghosthunter's
parser rank the correct service at #1 with the correct direction,
magnitude, and cost range?** That's what the CLI's `investigate -f`
command sees before Opus even enters the picture, so getting it right
here is the floor of any real investigation.

**Layer 2 (not shipped in v1.0.1).** The AI part: given a detected
spike, does Opus reach a root-cause diagnosis that overlaps with the
scenario's `root_cause.evidence_keywords`? That harness will read
`ANTHROPIC_API_KEY` from the env, run advisor mode per scenario, score
the final conclusion by keyword overlap, and emit a separate report. It
will land in a follow-up release.

## Scenarios

All 10 scenarios are synthetic but structurally realistic. Each produces
a 60-day [FOCUS 1.0](https://focus.finops.org/)-format CSV (30 baseline
days + 30 amplified days) with 7–8 services. The target service is
amplified by the configured `spike_factor` in the second half.

| ID | Provider | Difficulty | What happened |
|----|----------|------------|---------------|
| `gcp_01_bigquery_slot_runaway` | gcp | medium | On-demand BigQuery Cartesian-join query scheduled every 5 min |
| `gcp_02_nat_egress_compromised` | gcp | hard | Leaked service-account key exfiltrating via Cloud NAT |
| `gcp_03_cloudrun_min_instances` | gcp | easy | min-instances=100 promoted staging → prod |
| `gcp_04_logging_debug_leak` | gcp | easy | LOG_LEVEL=DEBUG left on in checkout service |
| `gcp_05_gke_autoscaler_flapping` | gcp | hard | Cluster autoscaler flapping due to bad PDB |
| `aws_01_nat_gateway_runaway` | aws | medium | ETL job missing S3 Gateway VPC Endpoint |
| `aws_02_s3_lifecycle_miss` | aws | easy | S3 Standard tier for cold compliance logs |
| `aws_03_cloudwatch_logs_verbose` | aws | easy | DEBUG logging in 40 Lambda functions |
| `aws_04_data_transfer_cross_az` | aws | hard | Kafka consumers in wrong AZ vs MSK brokers |
| `aws_05_bedrock_runaway` | aws | medium | Agent chaining 200 LLM calls/turn in a loop |

The scenario configs in [`scenarios.py`](./scenarios.py) are the single
source of ground truth. They drive both the CSV generator and the
scorer.

## Scoring rubric (Layer 1)

Each scenario scores 0–100:

| Points | Check |
|--------|-------|
| +50 | Top-ranked spike's service name matches expected (exact) |
| +25 | Service name matches as substring (either direction) |
| +20 | Spike `current_cost` falls inside expected range |
| +15 | Direction (up/down) matches expected |
| +15 | `|change_percent|` ≥ `min_change_percent` |

A scenario passes if its score ≥ 80. The harness reports pass rate,
mean score, and per-provider breakdown.

## Running it

```bash
# Regenerate fixtures from scenarios.py (only needed if you changed a scenario)
python benchmarks/generate_fixtures.py

# Run the benchmark
python benchmarks/run_benchmark.py

# Filter to a subset
python benchmarks/run_benchmark.py --filter gcp
python benchmarks/run_benchmark.py --filter bigquery

# Machine-readable output
python benchmarks/run_benchmark.py --json > results.json
```

The runner writes a Markdown summary to
[`benchmarks/results/latest.md`](./results/latest.md) on every run. Keep
that file committed — it's a pre-release checkpoint and a useful diff
signal when the parser changes.

## Current result

10/10 passing, mean 100/100. See
[`benchmarks/results/latest.md`](./results/latest.md) for the latest
per-scenario breakdown.

## How the CSVs are built

Determinism is enforced via a per-scenario integer seed derived from the
scenario id (not Python's hash, which is salted per-process). A fresh
checkout regenerates bit-identical CSVs. CSVs **are committed to git**
because (a) the benchmark must run without installing `scenarios.py` as
a package, (b) diffs on fixture files are a useful review signal when
the generator changes, and (c) it matches the Aider pattern.

## Why synthetic?

We do not have a way to ship real customer billing data in an OSS
repository. We do not have read-only access to 10 strangers' real
billing exports to privately beta-test against. We do have the
[FOCUS Sandbox](https://www.finops.org/insights/focus-sandbox/)
datasets and
[aws-samples/sample-finops-agent](https://github.com/aws-samples/sample-finops-agent)
fixtures — both are good starting points and we may layer them in as
additional scenarios over time. For now, synthetic + fully documented
ground truth is the honest validation story:

- every "correct answer" is written down and committed
- the generator is inspectable
- anyone can add a scenario in ~15 lines

## How to add a scenario

1. Append a `Scenario(...)` entry to `SCENARIOS` in
   [`scenarios.py`](./scenarios.py). Follow the docstring at the top of
   that file — `baseline_daily_cost × spike_factor × 30` should land
   inside your declared `current_cost_range`.
2. `python benchmarks/generate_fixtures.py` to produce the CSV + JSON.
3. `python benchmarks/run_benchmark.py --filter <your-id>` to confirm
   it scores ≥ 80.
4. Commit the scenario, the generated CSV, the generated JSON, and the
   updated `results/latest.md`.

## How this plugs into CI

`tests/test_benchmark_smoke.py` runs the benchmark on every `pytest`
invocation and fails the build if pass-rate drops below 100% or any
scenario errors. The benchmark runs in <200 ms — it's parser-only, no
network.
