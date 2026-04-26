"""Benchmark scenarios — the single source of ground truth.

Each scenario is a dict that (a) drives fixture generation AND (b) is the
"correct answer" the harness checks against. Keeping both in one place is
intentional: if you change a scenario's expected behaviour, the generator
and the scorer see the change together.

Every scenario produces a 60-day FOCUS 1.0 CSV (30 "previous" days at a
flat baseline + 30 "current" days where the target service is amplified
by the configured factor). Random per-day variation is seeded so
regeneration is deterministic.

Ground-truth fields:
  id                 — kebab-case scenario id, also the fixture filename
  provider           — "gcp" | "aws"
  description        — one-line plain-English description of what happened
  spike              — dict describing the expected top spike the parser
                       must find in the generated CSV
    service              — service name the parser should rank #1
    direction            — "up" | "down"
    min_change_percent   — absolute % change must be >= this
    current_cost_range   — [min, max] the spike's current_cost must fall in
  root_cause         — the "correct diagnosis" an investigator should
                       eventually reach. Used by the Layer-2 harness
                       (gated on ANTHROPIC_API_KEY) to score diagnostic
                       output via keyword overlap.
    summary          — short description
    evidence_keywords — set of lowercase keywords an Opus-generated
                       conclusion should contain to be counted correct
    remediation      — short description of the fix
  difficulty         — "easy" | "medium" | "hard"
  tags               — free-form list for filtering / reporting

Baseline design notes:
  - 8 services per scenario, with the target being one of them. The
    remaining 7 stay flat ±10% gaussian noise across both periods.
  - Target service: flat baseline in days 0-29, then amplified by
    `spike_factor` in days 30-59 with ±5% noise.
  - "down" spikes use 1/spike_factor — the target was expensive and
    dropped. The parser treats negative-direction spikes the same way.
  - All dollar figures are plausible but fictional.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Literal


@dataclass(frozen=True)
class ExpectedSpike:
    service: str
    direction: Literal["up", "down"]
    min_change_percent: float
    current_cost_range: tuple[float, float]


@dataclass(frozen=True)
class RootCause:
    summary: str
    evidence_keywords: tuple[str, ...]
    remediation: str


@dataclass(frozen=True)
class Scenario:
    id: str
    provider: Literal["gcp", "aws"]
    description: str
    difficulty: Literal["easy", "medium", "hard"]
    tags: tuple[str, ...]
    spike: ExpectedSpike
    root_cause: RootCause
    # Generator-only knobs (not part of ground truth, but drive the CSV):
    baseline_daily_cost: float  # target service's typical per-day cost
    spike_factor: float  # multiplier applied in the current period
    other_services: tuple[tuple[str, float], ...] = field(default_factory=tuple)
    sku: str | None = None
    region: str | None = None
    sub_account: str | None = None


# ---------------------------------------------------------------------------
# Common background services used across scenarios so the CSV looks realistic.
# The target service OVERRIDES any entry with the same name in these lists.
# ---------------------------------------------------------------------------
_GCP_BACKGROUND = (
    ("Compute Engine", 800.0),
    ("Cloud Storage", 220.0),
    ("Cloud SQL", 310.0),
    ("Kubernetes Engine", 540.0),
    ("Cloud Logging", 180.0),
    ("Cloud Monitoring", 90.0),
    ("Networking", 420.0),
)

_AWS_BACKGROUND = (
    ("Amazon Elastic Compute Cloud - Compute", 920.0),
    ("Amazon Simple Storage Service", 260.0),
    ("Amazon Relational Database Service", 380.0),
    ("Amazon Elastic Kubernetes Service", 470.0),
    ("AWS Lambda", 140.0),
    ("Amazon CloudWatch", 110.0),
    ("Amazon Virtual Private Cloud", 260.0),
)


SCENARIOS: list[Scenario] = [
    # -----------------------------------------------------------------
    # GCP scenarios
    # -----------------------------------------------------------------
    Scenario(
        id="gcp_01_bigquery_slot_runaway",
        provider="gcp",
        description=(
            "BigQuery on-demand costs blow up after a new experimental "
            "analytics project submits a Cartesian-join query on a 40 TB "
            "table every 5 minutes from a Composer DAG."
        ),
        difficulty="medium",
        tags=("bigquery", "runaway-query", "composer"),
        spike=ExpectedSpike(
            service="BigQuery",
            direction="up",
            min_change_percent=400.0,
            # 900 * 12 * 30 = $324K (matches the anonymized $325K narrative
            # in the description). ±8% to absorb gaussian noise.
            current_cost_range=(298_000, 350_000),
        ),
        root_cause=RootCause(
            summary=(
                "Composer DAG scheduled to run every 5 minutes executes an "
                "unbounded Cartesian join. On-demand BigQuery billed per "
                "TB scanned — the same query scans the full 40 TB table "
                "288 times/day."
            ),
            evidence_keywords=(
                "bigquery",
                "on-demand",
                "scan",
                "tb",
                "cartesian",
                "join",
                "composer",
                "schedule",
                "unbounded",
            ),
            remediation=(
                "Kill the DAG, add partition / clustering filters, and "
                "switch project to slot reservations with a per-query "
                "byte-scan limit."
            ),
        ),
        baseline_daily_cost=900.0,
        spike_factor=12.0,
        other_services=_GCP_BACKGROUND,
        sku="Analysis",
        region="us",
        sub_account="analytics-exp-2026",
    ),
    Scenario(
        id="gcp_02_nat_egress_compromised",
        provider="gcp",
        description=(
            "Cloud NAT egress explodes after a leaked service-account key "
            "is used from a training GCE instance to exfiltrate ~8 TB/day "
            "to an external S3 bucket."
        ),
        difficulty="hard",
        tags=("nat", "egress", "security", "exfiltration"),
        spike=ExpectedSpike(
            service="Cloud NAT",
            direction="up",
            min_change_percent=500.0,
            # 400 * 7 * 30 = $84K. ±10% for noise.
            current_cost_range=(75_000, 95_000),
        ),
        root_cause=RootCause(
            summary=(
                "Leaked service-account key exfiltrating data via Cloud "
                "NAT to an external endpoint. Egress to internet from a "
                "single instance in training-prod."
            ),
            evidence_keywords=(
                "nat",
                "egress",
                "external",
                "exfiltration",
                "service account",
                "key",
                "compromise",
                "training",
                "internet",
            ),
            remediation=(
                "Rotate the compromised key immediately, add egress VPC-SC "
                "perimeter, cap NAT gateway egress per-instance with a "
                "quota, enable Flow Logs if not already on."
            ),
        ),
        baseline_daily_cost=400.0,
        spike_factor=7.0,
        other_services=_GCP_BACKGROUND,
        sku="NAT Gateway Data Processing",
        region="us-central1",
        sub_account="training-prod",
    ),
    Scenario(
        id="gcp_03_cloudrun_min_instances",
        provider="gcp",
        description=(
            "Cloud Run cost jumps because a staging rollout set "
            "min-instances=100 on 8 services and shipped to production."
        ),
        difficulty="easy",
        tags=("cloud-run", "min-instances", "config-drift"),
        spike=ExpectedSpike(
            service="Cloud Run",
            direction="up",
            min_change_percent=250.0,
            current_cost_range=(40_000, 70_000),
        ),
        root_cause=RootCause(
            summary=(
                "min-instances=100 accidentally promoted from staging to "
                "prod across 8 Cloud Run services — each now keeps 100 "
                "warm containers 24/7 regardless of traffic."
            ),
            evidence_keywords=(
                "cloud run",
                "min-instances",
                "minimum",
                "warm",
                "idle",
                "staging",
                "rollout",
                "config",
            ),
            remediation=(
                "Roll back to min-instances=0 (or 1 for latency-sensitive "
                "services). Add an admission controller / Terraform review "
                "rule that flags min-instances > 5."
            ),
        ),
        baseline_daily_cost=320.0,
        spike_factor=5.5,
        other_services=_GCP_BACKGROUND,
        sku="CPU Allocation Time (Always-On)",
        region="us-central1",
        sub_account="platform-prod",
    ),
    Scenario(
        id="gcp_04_logging_debug_leak",
        provider="gcp",
        description=(
            "Cloud Logging spikes after a debug log level was left on in "
            "a high-traffic microservice across all regions."
        ),
        difficulty="easy",
        tags=("logging", "debug", "verbosity"),
        spike=ExpectedSpike(
            service="Cloud Logging",
            direction="up",
            min_change_percent=300.0,
            current_cost_range=(35_000, 60_000),
        ),
        root_cause=RootCause(
            summary=(
                "A feature-flag rollout left LOG_LEVEL=DEBUG on in the "
                "checkout service. DEBUG emits request/response payloads; "
                "log volume grew ~8x."
            ),
            evidence_keywords=(
                "logging",
                "debug",
                "verbose",
                "log level",
                "volume",
                "ingestion",
                "checkout",
            ),
            remediation=(
                "Set LOG_LEVEL=INFO, add a log-volume alert at 2x week-"
                "over-week, sample DEBUG rather than emitting all of it."
            ),
        ),
        baseline_daily_cost=180.0,
        spike_factor=8.0,
        other_services=_GCP_BACKGROUND,
        sku="Log Volume",
        region="global",
        sub_account="checkout-prod",
    ),
    Scenario(
        id="gcp_05_gke_autoscaler_flapping",
        provider="gcp",
        description=(
            "GKE node-pool cost jumps as cluster autoscaler flaps between "
            "0 and 50 nodes every ~3 minutes; node warm-up is billed each "
            "cycle. PodDisruptionBudget was misconfigured."
        ),
        difficulty="hard",
        tags=("gke", "autoscaler", "flapping", "kubernetes"),
        spike=ExpectedSpike(
            service="Kubernetes Engine",
            direction="up",
            min_change_percent=200.0,
            current_cost_range=(40_000, 70_000),
        ),
        root_cause=RootCause(
            summary=(
                "Cluster autoscaler flapping between min=0 and max=50 "
                "every few minutes because a PodDisruptionBudget blocks "
                "scale-down; each warm-up boots N nodes fully."
            ),
            evidence_keywords=(
                "gke",
                "kubernetes",
                "autoscaler",
                "flap",
                "poddisruption",
                "pdb",
                "scale",
                "node pool",
            ),
            remediation=(
                "Fix PDB minAvailable on the offending deployment. Add "
                "scale-down-delay-after-add=10m. Alert on cluster "
                "autoscaler scaleup_count > 10/hour."
            ),
        ),
        baseline_daily_cost=540.0,
        spike_factor=3.5,
        other_services=_GCP_BACKGROUND,
        sku="E2 Instance Core",
        region="us-central1",
        sub_account="platform-prod",
    ),
    # -----------------------------------------------------------------
    # AWS scenarios
    # -----------------------------------------------------------------
    Scenario(
        id="aws_01_nat_gateway_runaway",
        provider="aws",
        description=(
            "NAT Gateway data-processing costs explode because a new ETL "
            "job in us-east-1 pulls from a public S3 bucket via the NAT "
            "Gateway instead of a VPC endpoint."
        ),
        difficulty="medium",
        tags=("nat-gateway", "vpc-endpoint", "etl"),
        spike=ExpectedSpike(
            service="Amazon Virtual Private Cloud",
            direction="up",
            min_change_percent=500.0,
            current_cost_range=(45_000, 90_000),
        ),
        root_cause=RootCause(
            summary=(
                "ETL job pulls ~4 TB/day from a public S3 bucket through "
                "the NAT Gateway instead of a Gateway VPC Endpoint for "
                "S3. NAT Gateway charges $0.045/GB processed; VPC "
                "endpoints are free for same-region S3."
            ),
            evidence_keywords=(
                "nat gateway",
                "vpc endpoint",
                "s3",
                "etl",
                "egress",
                "data processing",
                "gateway endpoint",
            ),
            remediation=(
                "Add an S3 Gateway VPC Endpoint in the VPC and update "
                "the ETL job's route table. One-line fix; savings ≈ 100%."
            ),
        ),
        baseline_daily_cost=280.0,
        spike_factor=7.5,
        other_services=_AWS_BACKGROUND,
        sku="NatGateway-Bytes",
        region="us-east-1",
        sub_account="data-platform",
    ),
    Scenario(
        id="aws_02_s3_lifecycle_miss",
        provider="aws",
        description=(
            "S3 Standard-tier storage bill doubles after a security log "
            "pipeline starts writing 12 TB/month of compliance logs "
            "without lifecycle policies moving them to Glacier."
        ),
        difficulty="easy",
        tags=("s3", "lifecycle", "storage-tier", "compliance"),
        spike=ExpectedSpike(
            service="Amazon Simple Storage Service",
            direction="up",
            min_change_percent=120.0,
            current_cost_range=(18_000, 35_000),
        ),
        root_cause=RootCause(
            summary=(
                "Security compliance log pipeline writes to S3 Standard "
                "with no lifecycle rule. Logs are read once then never "
                "again but accrue Standard pricing indefinitely."
            ),
            evidence_keywords=(
                "s3",
                "lifecycle",
                "standard",
                "glacier",
                "tier",
                "compliance",
                "log",
                "cold",
            ),
            remediation=(
                "Add a lifecycle rule: transition to Standard-IA at 30 "
                "days, Glacier Instant Retrieval at 90 days, Deep "
                "Archive at 365 days. ~70% cost reduction on old logs."
            ),
        ),
        baseline_daily_cost=360.0,
        spike_factor=2.5,
        other_services=_AWS_BACKGROUND,
        sku="TimedStorage-ByteHrs",
        region="us-east-1",
        sub_account="security-logs-prod",
    ),
    Scenario(
        id="aws_03_cloudwatch_logs_verbose",
        provider="aws",
        description=(
            "CloudWatch Logs ingestion charges spike when a staging "
            "release accidentally enables DEBUG-level JSON structured "
            "logging across 40 Lambda functions in production."
        ),
        difficulty="easy",
        tags=("cloudwatch", "logs", "lambda", "verbosity"),
        spike=ExpectedSpike(
            service="Amazon CloudWatch",
            direction="up",
            min_change_percent=400.0,
            current_cost_range=(22_000, 45_000),
        ),
        root_cause=RootCause(
            summary=(
                "40 Lambda functions deployed with LOG_LEVEL=DEBUG via a "
                "shared layer update. Each invocation now writes ~8x the "
                "previous log volume to CloudWatch Logs at $0.50/GB "
                "ingestion."
            ),
            evidence_keywords=(
                "cloudwatch",
                "logs",
                "ingestion",
                "debug",
                "verbose",
                "lambda",
                "log level",
                "layer",
            ),
            remediation=(
                "Pin LOG_LEVEL=INFO in the shared Lambda layer. Set "
                "CloudWatch Log Group retention to 7 days for Lambda "
                "functions. Add a metric filter + alarm on log volume."
            ),
        ),
        baseline_daily_cost=110.0,
        spike_factor=10.0,
        other_services=_AWS_BACKGROUND,
        sku="DataProcessing-Bytes",
        region="us-east-1",
        sub_account="platform-prod",
    ),
    Scenario(
        id="aws_04_data_transfer_cross_az",
        provider="aws",
        description=(
            "Cross-AZ data transfer cost jumps because a Kafka consumer "
            "group was moved to a different subnet but producers kept "
            "publishing to the original AZ's brokers."
        ),
        difficulty="hard",
        tags=("data-transfer", "cross-az", "kafka", "msk"),
        spike=ExpectedSpike(
            service="Amazon Elastic Compute Cloud - Compute",
            direction="up",
            min_change_percent=80.0,
            # 920 * 1.9 * 30 = $52,440. ±10% for noise.
            current_cost_range=(47_000, 58_000),
        ),
        root_cause=RootCause(
            summary=(
                "Kafka consumers relocated to us-east-1c but MSK brokers "
                "are in us-east-1a. Every consumed message crosses an "
                "AZ boundary; cross-AZ data transfer is $0.01/GB each "
                "way."
            ),
            evidence_keywords=(
                "cross-az",
                "data transfer",
                "kafka",
                "msk",
                "subnet",
                "availability zone",
                "consumer",
            ),
            remediation=(
                "Move consumers back to the broker's AZ, or use MSK "
                "rack-awareness + client-side fetch.from.closest."
            ),
        ),
        baseline_daily_cost=920.0,
        spike_factor=1.9,
        other_services=_AWS_BACKGROUND,
        sku="DataTransfer-Regional-Bytes",
        region="us-east-1",
        sub_account="streaming-prod",
    ),
    Scenario(
        id="aws_05_bedrock_runaway",
        provider="aws",
        description=(
            "Amazon Bedrock spend triples after an experimental agent "
            "framework is left running over a weekend with no rate limit "
            "and a prompt that chains 200 LLM calls per user message."
        ),
        difficulty="medium",
        tags=("bedrock", "llm", "rate-limit", "agent"),
        spike=ExpectedSpike(
            service="Amazon Bedrock",
            direction="up",
            min_change_percent=600.0,
            current_cost_range=(25_000, 60_000),
        ),
        root_cause=RootCause(
            summary=(
                "An internal agent demo chains ~200 Bedrock model calls "
                "per user turn, and a synthetic load test was left "
                "looping over a weekend. No per-key rate limit or cost "
                "quota."
            ),
            evidence_keywords=(
                "bedrock",
                "agent",
                "chain",
                "loop",
                "rate limit",
                "quota",
                "weekend",
                "synthetic",
            ),
            remediation=(
                "Kill the runaway agent job. Add Bedrock provisioned "
                "throughput quotas per service role, add CloudWatch "
                "alarm on InvokeModel call-count anomalies."
            ),
        ),
        baseline_daily_cost=140.0,
        spike_factor=9.0,
        other_services=_AWS_BACKGROUND,
        sku="model-invocations",
        region="us-east-1",
        sub_account="ml-experimental",
    ),
]


def by_id(scenario_id: str) -> Scenario:
    """Look up a scenario by id. Raises KeyError if not found."""
    for s in SCENARIOS:
        if s.id == scenario_id:
            return s
    raise KeyError(scenario_id)


def all_ids() -> list[str]:
    return [s.id for s in SCENARIOS]
