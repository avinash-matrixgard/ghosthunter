"""Billing-file provider for Advisor Mode.

Parses one or more billing exports the user fetched themselves from GCP
(CSV or JSON) and turns them into `CostSpike` objects. No GCP credentials
required.

## Multiple files

You can pass several files at once — for example a "by service" CSV plus
a "by SKU" CSV plus a "by project" CSV from the Console. The parser
merges all rows, recognizes which extra dimension columns are present
in each file, and surfaces them as `top_contributors` on each spike so
Opus can see WHERE inside a service the cost moved.

## Single rich file (preferred)

If you query the BigQuery billing export directly, one query can
produce a single CSV with service, sku, project, location, date, and
cost columns. That's the easiest path — see `ghosthunter billing-template`.

## Supported columns

- **Service** (required): service / Service description / service.description
- **Cost** (required): cost / Cost ($) / Subtotal ($) / amount
- **Date** (optional): usage_start_time / Usage start date / date
- **SKU** (optional): sku / sku.description / SKU description
- **Project** (optional): project / project.id / Project ID
- **Location** (optional): location / location.region / region

Column matching is case-insensitive and tolerant of common aliases.
"""
from __future__ import annotations

import csv
import json
from dataclasses import dataclass
from datetime import date, datetime
from pathlib import Path
from typing import Any, Iterable

from ghosthunter.providers.gcp import CostSpike

# ---------------------------------------------------------------------------
# Column aliases
# ---------------------------------------------------------------------------
SERVICE_KEYS = (
    # GCP
    "service",
    "service.description",
    "service_description",
    "Service description",
    "Service",
    "service_name",
    # AWS
    "lineItem/ProductCode",          # CUR
    "product/ProductName",           # CUR
    "product_productname",           # CUR (some exports)
    "Service name",                  # CE CSV variant
    # FOCUS (FinOps Open Cost & Usage Specification — cross-cloud)
    "ServiceName",                   # FOCUS 1.0+
)

COST_KEYS = (
    # GCP
    "cost",
    "Cost",
    "Cost ($)",
    "Cost (USD)",
    "Subtotal ($)",
    "subtotal",
    "amount",
    "total_cost",
    # AWS
    "UnblendedCost",                 # CE
    "BlendedCost",                   # CE
    "NetUnblendedCost",              # CE
    "AmortizedCost",                 # CE
    "lineItem/UnblendedCost",        # CUR
    "lineItem/BlendedCost",          # CUR
    "lineItem/NetUnblendedCost",     # CUR
    "Amount",                        # CE JSON flattened rows
    # FOCUS. Order matters — we pick the FIRST match against a row's keys.
    # BilledCost is the primary "what you paid" FOCUS column; EffectiveCost
    # is after commitment / contract discounts. Prefer BilledCost for
    # user-facing cost reporting.
    "BilledCost",                    # FOCUS 1.0+
    "EffectiveCost",                 # FOCUS 1.0+ — used if BilledCost absent
    "ListCost",                      # FOCUS 1.0+ — pre-discount reference
    "ContractedCost",                # FOCUS 1.0+ — post-commitment price
)

DATE_KEYS = (
    # GCP
    "usage_start_time",
    "Usage start date",
    "Usage start time",
    "usage_start_date",
    "date",
    "Date",
    "day",
    "billing_date",
    # AWS
    "Start",                         # CE JSON flattened
    "TimePeriodStart",               # CE raw
    "lineItem/UsageStartDate",       # CUR
    "bill/BillingPeriodStartDate",   # CUR
    # FOCUS. ChargePeriodStart is per-row usage; BillingPeriodStart groups
    # the monthly invoice window. Prefer ChargePeriod for spike detection.
    "ChargePeriodStart",             # FOCUS 1.0+
    "BillingPeriodStart",            # FOCUS 1.0+ (coarser fallback)
)

SKU_KEYS = (
    # GCP
    "sku",
    "sku.description",
    "SKU description",
    "sku_description",
    "SKU",
    # AWS has no direct SKU equivalent; UsageType plays that role
    # and is its own dimension below.
    # FOCUS
    "SkuId",                         # FOCUS 1.0+ provider SKU id
    "SkuPriceId",                    # FOCUS 1.0+ specific price variant
)

# AWS UsageType — AWS's closest equivalent to a GCP SKU. Treated as a
# separate dimension so it shows up distinctly in top_contributors.
USAGE_TYPE_KEYS = (
    "UsageType",
    "lineItem/UsageType",
    "usage_type",
    "Usage Type",
)

PROJECT_KEYS = (
    "project",
    "project.id",
    "Project ID",
    "project_id",
    "Project name",
    "project_name",
)

# AWS account — analogous to a GCP project for our cross-file inference.
ACCOUNT_KEYS = (
    "Linked Account",                # CE CSV
    "Account name",                  # CE CSV variant
    "AccountId",                     # CE JSON flattened
    "UsageAccountId",                # CE / CUR
    "lineItem/UsageAccountId",       # CUR
    "bill/PayerAccountId",           # CUR
    "account",
    "account_id",
    # FOCUS — SubAccount is the per-tenant account inside a billing org.
    # BillingAccount is the invoice-level parent. Prefer SubAccount for
    # cross-file inference.
    "SubAccountId",                  # FOCUS 1.0+
    "SubAccountName",                # FOCUS 1.0+
    "BillingAccountId",              # FOCUS 1.0+ (coarser fallback)
    "BillingAccountName",            # FOCUS 1.0+
)

LOCATION_KEYS = (
    # GCP
    "location",
    "location.region",
    "location.location",
    "region",
    "Region",
    "Location",
    # AWS
    "product/region",                # CUR
    "product/location",              # CUR (human-readable region)
    "lineItem/AvailabilityZone",     # CUR
    "aws_region",
    # FOCUS
    "RegionName",                    # FOCUS 1.0+ (human-readable)
    "RegionId",                      # FOCUS 1.0+ (provider-canonical id)
    "AvailabilityZone",              # FOCUS 1.0+ (fallback)
)

PCT_CHANGE_KEYS = (
    "Percent change in subtotal compared to previous period",
    "percent_change",
    "Percent change",
    "Change %",
    "pct_change",
)

# Human-readable charge descriptions. When present, let the reasoner
# see what an opaque SKU-ID or UsageType actually represents (e.g. a
# g5.4xlarge GPU hour) without asking the user to decode it.
#   - FOCUS 1.0: ``ChargeDescription`` — always present.
#   - AWS CUR:   ``lineItem/LineItemDescription`` — always present.
#   - GCP:       exports rarely carry per-row descriptions; not critical.
DESCRIPTION_KEYS = (
    "ChargeDescription",
    "charge_description",
    "Charge Description",
    "lineItem/LineItemDescription",
    "line_item_description",
    "LineItemDescription",
    "description",
    "Description",
)

DIMENSION_KEY_GROUPS: dict[str, tuple[str, ...]] = {
    "service":    SERVICE_KEYS,
    "sku":        SKU_KEYS,
    "usage_type": USAGE_TYPE_KEYS,
    "project":    PROJECT_KEYS,
    "account":    ACCOUNT_KEYS,
    "location":   LOCATION_KEYS,
}

# Order in which we pick the "primary grouping" column for a file.
# Service > Project > Account > SKU > UsageType > Location.
# Account sits next to Project (AWS's equivalent grouping); UsageType
# next to SKU (AWS's equivalent granularity).
GROUPING_PRIORITY = ("service", "project", "account", "sku", "usage_type", "location")

# How many top contributors to keep per dimension per spike.
TOP_CONTRIBUTORS_LIMIT = 8


# ---------------------------------------------------------------------------
# Errors
# ---------------------------------------------------------------------------
class BillingFileError(Exception):
    """Raised when a billing file cannot be parsed or has no usable data."""


# ---------------------------------------------------------------------------
# Public entry points
# ---------------------------------------------------------------------------
def load_spikes_from_file(
    path: str | Path,
    min_change_percent: float = 20.0,
    min_absolute_change: float = 100.0,
) -> list[CostSpike]:
    """Single-file convenience wrapper around `load_spikes_from_files`."""
    return load_spikes_from_files(
        [path], min_change_percent, min_absolute_change
    )


def load_spikes_from_files(
    paths: Iterable[str | Path],
    min_change_percent: float = 20.0,
    min_absolute_change: float = 100.0,
) -> list[CostSpike]:
    """Parse one or more billing files and return ranked CostSpike objects.

    All files are merged before spike detection. If any file has SKU /
    project / location columns, those dimensions are surfaced as
    `top_contributors` on each spike.
    """
    all_normalized: list[NormalizedRow] = []
    paths_list = [Path(p).expanduser().resolve() for p in paths]
    file_diagnostics: list[str] = []

    for p in paths_list:
        if not p.exists():
            raise BillingFileError(f"Billing file not found: {p}")
        rows = _parse_file(p)
        if not rows:
            file_diagnostics.append(f"  • {p.name}: empty file")
            continue
        normalized, err = _normalize_rows(rows, source=str(p))
        if err:
            file_diagnostics.append(f"  • {p.name}: {err}")
            continue
        all_normalized.extend(normalized)

    if not all_normalized:
        details = "\n".join(file_diagnostics) if file_diagnostics else "(none)"
        raise BillingFileError(
            "No usable rows in any provided file.\n" + details
        )

    has_dates = any(r.day is not None for r in all_normalized)
    distinct_days = {r.day for r in all_normalized if r.day is not None}

    if has_dates and len(distinct_days) >= 4:
        spikes = _spikes_with_date_split(
            all_normalized, min_change_percent, min_absolute_change
        )
    else:
        spikes = _spikes_total_only(all_normalized)

    _attach_top_contributors(spikes, all_normalized)
    _attach_likely_homes(spikes)
    return spikes


# ---------------------------------------------------------------------------
# Normalized row
# ---------------------------------------------------------------------------
@dataclass
class NormalizedRow:
    grouping: str          # one of GROUPING_PRIORITY — what this row's primary key is
    grouping_value: str    # the value of that grouping column for this row
    cost: float
    day: date | None
    service: str | None
    sku: str | None
    usage_type: str | None   # AWS UsageType — the SKU-equivalent for AWS data
    project: str | None
    account: str | None      # AWS account id / name — the project-equivalent
    location: str | None
    source: str
    pct_change: float | None = None  # period-over-period % from the file, if present
    description: str | None = None   # ChargeDescription / LineItemDescription, if the file has it


# ---------------------------------------------------------------------------
# File parsing
# ---------------------------------------------------------------------------
def _parse_file(path: Path) -> list[dict[str, Any]]:
    suffix = path.suffix.lower()

    if suffix == ".parquet":
        raise BillingFileError(
            "Parquet CUR files are not supported in v1. "
            "Ask AWS to also export CSV, or convert the file with a "
            "local tool (e.g. `parquet-tools csv`)."
        )

    if suffix == ".json":
        with path.open() as f:
            data = json.load(f)

        # AWS Cost Explorer `get-cost-and-usage` response shape.
        if isinstance(data, dict) and "ResultsByTime" in data:
            return _flatten_ce_json(data)

        if isinstance(data, dict):
            for key in ("rows", "data", "billing", "results"):
                if key in data and isinstance(data[key], list):
                    data = data[key]
                    break
        if not isinstance(data, list):
            raise BillingFileError(
                "JSON billing file must be a list of rows, an `aws ce "
                "get-cost-and-usage` response, or contain a top-level "
                "'rows'/'data' array"
            )
        return [_flatten(r) for r in data if isinstance(r, dict)]

    delimiter = "\t" if suffix == ".tsv" else ","
    with path.open(newline="") as f:
        reader = csv.DictReader(f, delimiter=delimiter)
        return [dict(row) for row in reader]


# CE API dimension keys → the column names our alias tuples recognize.
# Used to project raw CE responses into our normalization pipeline.
_CE_DIMENSION_TO_COLUMN = {
    "SERVICE": "Service",
    "USAGE_TYPE": "UsageType",
    "USAGE_TYPE_GROUP": "UsageType",
    "LINKED_ACCOUNT": "Linked Account",
    "LINKED_ACCOUNT_NAME": "Linked Account",
    "REGION": "Region",
    "AZ": "Region",
    "INSTANCE_TYPE": "UsageType",
    "INSTANCE_TYPE_FAMILY": "UsageType",
    "OPERATION": "UsageType",
    "PURCHASE_TYPE": "UsageType",
    "RECORD_TYPE": "UsageType",
}


def _flatten_ce_json(data: dict[str, Any]) -> list[dict[str, Any]]:
    """Flatten `aws ce get-cost-and-usage` output into per-row dicts.

    A single CE response can contain multiple time buckets, each with
    many groups. We produce one row per (TimePeriod.Start × Group) pair,
    mapping ``Group.Keys[i]`` to the column implied by
    ``GroupDefinitions[i]`` and promoting each metric's ``Amount`` to a
    top-level numeric field using the metric's name.

    Shape of a flattened row::

        {"Start": "2026-03-01", "Service": "Amazon EC2",
         "UnblendedCost": 123.45}
    """
    group_defs = data.get("GroupDefinitions", []) or []
    columns_by_idx = [
        _CE_DIMENSION_TO_COLUMN.get(
            gd.get("Key", "").upper(), gd.get("Key", "") or f"Key{i}"
        )
        for i, gd in enumerate(group_defs)
    ]

    rows: list[dict[str, Any]] = []
    for period in data.get("ResultsByTime", []) or []:
        tp = period.get("TimePeriod", {}) or {}
        start = tp.get("Start")
        groups = period.get("Groups") or []
        if groups:
            for g in groups:
                row: dict[str, Any] = {"Start": start}
                keys = g.get("Keys", []) or []
                for i, k in enumerate(keys):
                    col = (
                        columns_by_idx[i]
                        if i < len(columns_by_idx)
                        else f"Key{i}"
                    )
                    row[col] = k
                _promote_ce_metrics(row, g.get("Metrics") or {})
                rows.append(row)
        else:
            # No group-by → use the period Total.
            total = period.get("Total") or {}
            row = {"Start": start, "Service": "Total"}
            _promote_ce_metrics(row, total)
            # Only emit the Total row if we managed to pull a cost field.
            if any(k in row for k in ("UnblendedCost", "BlendedCost", "Amount")):
                rows.append(row)
    return rows


def _promote_ce_metrics(row: dict[str, Any], metrics: dict[str, Any]) -> None:
    """Copy each CE metric's Amount into ``row`` under the metric's name."""
    for metric_name, metric_data in metrics.items():
        if not isinstance(metric_data, dict):
            continue
        amt = metric_data.get("Amount")
        if amt is None:
            continue
        try:
            row[metric_name] = float(amt)
        except (TypeError, ValueError):
            continue


def _flatten(row: dict[str, Any], prefix: str = "") -> dict[str, Any]:
    """Flatten nested dicts so 'service.description' is a top-level key."""
    flat: dict[str, Any] = {}
    for key, value in row.items():
        full_key = key if prefix == "" else f"{prefix}.{key}"
        if isinstance(value, dict):
            flat.update(_flatten(value, full_key))
        else:
            flat[full_key] = value
    return flat


# ---------------------------------------------------------------------------
# Row normalization
# ---------------------------------------------------------------------------
def _normalize_rows(
    rows: list[dict[str, Any]], source: str
) -> tuple[list[NormalizedRow], str | None]:
    """Normalize a parsed file into rows + a diagnostic if it can't be used.

    Returns (rows, error_message). error_message is None on success.
    """
    sample = rows[0]
    cost_key = _detect_optional_key(sample, COST_KEYS)
    if not cost_key:
        return [], (
            f"no cost column found. Looked for: {', '.join(COST_KEYS)}. "
            f"Available columns: {', '.join(sample.keys())}"
        )

    # Detect every dimension column the file has, then pick the highest-
    # priority one as this file's primary grouping.
    dim_keys: dict[str, str | None] = {
        dim: _detect_optional_key(sample, keys)
        for dim, keys in DIMENSION_KEY_GROUPS.items()
    }
    grouping = next(
        (dim for dim in GROUPING_PRIORITY if dim_keys.get(dim)), None
    )
    if grouping is None:
        return [], (
            "no usable grouping column found. Need at least one of: "
            f"service / project / sku / location. "
            f"Available columns: {', '.join(sample.keys())}"
        )

    grouping_key = dim_keys[grouping]
    date_key = _detect_optional_key(sample, DATE_KEYS)
    pct_key = _detect_optional_key(sample, PCT_CHANGE_KEYS)
    desc_key = _detect_optional_key(sample, DESCRIPTION_KEYS)

    out: list[NormalizedRow] = []
    for raw in rows:
        grouping_value = _clean_str(raw.get(grouping_key))
        if not grouping_value:
            continue
        cost = _parse_cost(raw.get(cost_key))
        if cost is None:
            continue
        out.append(
            NormalizedRow(
                grouping=grouping,
                grouping_value=grouping_value,
                cost=cost,
                day=_parse_date(raw.get(date_key)) if date_key else None,
                service=_clean_str(raw.get(dim_keys["service"])) if dim_keys["service"] else None,
                sku=_clean_str(raw.get(dim_keys["sku"])) if dim_keys["sku"] else None,
                usage_type=_clean_str(raw.get(dim_keys["usage_type"])) if dim_keys["usage_type"] else None,
                project=_clean_str(raw.get(dim_keys["project"])) if dim_keys["project"] else None,
                account=_clean_str(raw.get(dim_keys["account"])) if dim_keys["account"] else None,
                location=_clean_str(raw.get(dim_keys["location"])) if dim_keys["location"] else None,
                source=source,
                pct_change=_parse_pct(raw.get(pct_key)) if pct_key else None,
                description=_clean_str(raw.get(desc_key)) if desc_key else None,
            )
        )
    return out, None


def _parse_pct(value: Any) -> float | None:
    """Parse '825%', '+825.5%', '-12%', '0.825', etc. into a float percent."""
    if value is None or value == "":
        return None
    if isinstance(value, (int, float)):
        return float(value)
    s = str(value).strip().replace(",", "").replace("%", "").replace("+", "")
    if not s:
        return None
    try:
        return float(s)
    except ValueError:
        return None


def _detect_optional_key(
    sample_row: dict[str, Any], candidates: tuple[str, ...]
) -> str | None:
    lower_to_actual = {k.lower(): k for k in sample_row.keys()}
    for cand in candidates:
        if cand in sample_row:
            return cand
        if cand.lower() in lower_to_actual:
            return lower_to_actual[cand.lower()]
    return None


def _parse_cost(value: Any) -> float | None:
    if value is None or value == "":
        return None
    if isinstance(value, (int, float)):
        return float(value)
    s = str(value).strip().replace("$", "").replace(",", "")
    try:
        return float(s)
    except ValueError:
        return None


def _parse_date(value: Any) -> date | None:
    if value is None or value == "":
        return None
    s = str(value).strip()
    for fmt in (
        "%Y-%m-%d",
        "%Y-%m-%dT%H:%M:%S",
        "%Y-%m-%dT%H:%M:%S.%f",
        "%Y-%m-%dT%H:%M:%SZ",
        "%Y-%m-%d %H:%M:%S",
        "%Y-%m-%d %H:%M:%S UTC",
        "%m/%d/%Y",
        "%m/%d/%y",
    ):
        try:
            return datetime.strptime(s, fmt).date()
        except ValueError:
            continue
    try:
        return datetime.fromisoformat(s.replace("Z", "+00:00")).date()
    except ValueError:
        return None


def _clean_str(value: Any) -> str | None:
    if value is None:
        return None
    s = str(value).strip()
    return s or None


# ---------------------------------------------------------------------------
# Spike computation
# ---------------------------------------------------------------------------
def _spikes_with_date_split(
    rows: list[NormalizedRow],
    min_change_percent: float,
    min_absolute_change: float,
) -> list[CostSpike]:
    days = sorted({r.day for r in rows if r.day is not None})
    if len(days) < 2:
        return _spikes_total_only(rows)

    midpoint = days[len(days) // 2]
    # key: (grouping, value)
    grouped: dict[tuple[str, str], dict[str, Any]] = {}

    for r in rows:
        key = (r.grouping, r.grouping_value)
        entry = grouped.setdefault(
            key, {"current": 0.0, "previous": 0.0, "daily": []}
        )
        if r.day is None:
            entry["current"] += r.cost
        elif r.day < midpoint:
            entry["previous"] += r.cost
        else:
            entry["current"] += r.cost
            entry["daily"].append({"day": r.day.isoformat(), "cost": r.cost})

    spikes: list[CostSpike] = []
    for (grouping, value), data in grouped.items():
        previous = data["previous"]
        current = data["current"]
        if previous == 0 and current == 0:
            continue
        if previous > 0:
            pct = ((current - previous) / previous) * 100.0
        else:
            pct = float("inf") if current > 0 else 0.0
        absolute = current - previous
        material = (
            abs(pct) >= min_change_percent
            or abs(absolute) >= min_absolute_change
        )
        if not material:
            continue
        spikes.append(
            CostSpike(
                service=value,
                grouping=grouping,
                current_cost=current,
                previous_cost=previous,
                change_percent=pct,
                daily_breakdown=sorted(data["daily"], key=lambda d: d["day"]),
            )
        )

    spikes.sort(key=lambda s: abs(s.absolute_change), reverse=True)
    return spikes or _spikes_total_only(rows)


def _spikes_total_only(rows: list[NormalizedRow]) -> list[CostSpike]:
    """Aggregate by (grouping, value). When the file carries a per-row
    `pct_change` (Console "% change vs previous period"), back-compute the
    previous-period cost from it so we still get period-over-period numbers
    even without dates.
    """
    @dataclass
    class _Acc:
        current: float = 0.0
        previous: float = 0.0
        any_pct: bool = False

    grouped: dict[tuple[str, str], _Acc] = {}
    for r in rows:
        key = (r.grouping, r.grouping_value)
        acc = grouped.setdefault(key, _Acc())
        acc.current += r.cost
        if r.pct_change is not None:
            acc.any_pct = True
            # previous = current / (1 + pct/100)
            denom = 1.0 + (r.pct_change / 100.0)
            if denom > 0:
                acc.previous += r.cost / denom
            # If denom <= 0 (pct <= -100%), previous is undefined; ignore.

    spikes: list[CostSpike] = []
    for (grouping, value), acc in grouped.items():
        if acc.current <= 0 and acc.previous <= 0:
            continue
        if acc.any_pct and acc.previous > 0:
            pct = ((acc.current - acc.previous) / acc.previous) * 100.0
        elif acc.any_pct and acc.previous == 0 and acc.current > 0:
            pct = float("inf")
        else:
            pct = 0.0
        spikes.append(
            CostSpike(
                service=value,
                grouping=grouping,
                current_cost=acc.current,
                previous_cost=acc.previous,
                change_percent=pct,
                daily_breakdown=[],
            )
        )
    spikes.sort(key=lambda s: s.current_cost, reverse=True)
    return spikes


# ---------------------------------------------------------------------------
# Top contributors (the "where in the spike" detail)
# ---------------------------------------------------------------------------
# Common service-name fragments → keywords to look for in project names.
# Used by _attach_likely_homes for the name-match heuristic. All keywords
# are at least 3 chars to avoid false positives from generic substrings.
SERVICE_KEYWORDS_GCP = {
    "Cloud DNS": ["dns"],
    "Cloud Storage": ["storage", "gcs", "bucket"],
    "Cloud SQL": ["sql", "database", "rdbms"],
    "Cloud Run": ["run", "service"],
    "Cloud Functions": ["func", "lambda", "function"],
    "Cloud Logging": ["log", "logging"],
    "Cloud Monitoring": ["monitor", "metrics"],
    "Cloud Composer": ["composer", "airflow", "dag"],
    "Cloud Pub/Sub": ["pubsub"],
    "Cloud Memorystore": ["redis", "memorystore", "cache"],
    "Cloud Memorystore for Redis": ["redis", "memorystore", "cache"],
    "Compute Engine": ["compute", "gce"],
    "BigQuery": ["bigquery", "warehouse", "dwh", "analytics"],
    "Kubernetes Engine": ["gke", "k8s", "kube"],
    "VMware Engine": ["vmware", "vmw"],
    "Networking": ["net", "network", "vpc"],
    "Apigee": ["apigee"],
    "Vertex AI": ["vertex"],
    "Gemini API": ["gemini"],
    "Cloud Build": ["build", "cicd"],
    "Cloud Dataflow": ["dataflow"],
    "Datastream": ["datastream", "stream"],
    "Cloud Dialogflow API": ["dialogflow"],
    "Security Command Center": ["sec", "security", "scc"],
    "Certificate Authority Service": ["cert", "pki"],
    "Secret Manager": ["secret", "vault"],
    "Artifact Registry": ["artifact", "registry"],
    "Pub/Sub": ["pubsub"],
    "Identity Platform": ["iam", "identity"],
    "Backup for GKE": ["backup", "gke"],
    "Cloud Key Management Service (KMS)": ["kms"],
}

# AWS service-name fragments → keywords. Canonical names are the CE
# labels you see in the Cost Explorer UI / `ResultsByTime[].Groups[].Keys`.
SERVICE_KEYWORDS_AWS = {
    "Amazon Elastic Compute Cloud - Compute": ["ec2", "compute"],
    "EC2 - Other": ["ec2"],
    "Amazon Simple Storage Service": ["s3", "storage", "bucket"],
    "Amazon Relational Database Service": ["rds", "database"],
    "Amazon Aurora": ["aurora", "rds"],
    "Amazon DynamoDB": ["dynamo", "ddb"],
    "Amazon Redshift": ["redshift", "warehouse", "dwh"],
    "Amazon OpenSearch Service": ["opensearch", "search"],
    "Amazon Elasticsearch Service": ["elasticsearch", "search"],
    "AWS Lambda": ["lambda", "func", "function"],
    "Amazon CloudFront": ["cloudfront", "cdn"],
    "Amazon Route 53": ["route53", "dns"],
    "AWS CloudTrail": ["cloudtrail", "audit"],
    "Amazon CloudWatch": ["cloudwatch", "metrics", "monitor"],
    "AmazonCloudWatch": ["cloudwatch", "metrics", "monitor"],
    "Amazon Virtual Private Cloud": ["vpc", "net", "network"],
    "Amazon API Gateway": ["apigw", "apigateway", "gateway"],
    "Amazon Elastic Container Service": ["ecs", "container"],
    "Amazon Elastic Kubernetes Service": ["eks", "k8s", "kube"],
    "AWS Fargate": ["fargate", "container"],
    "Amazon Simple Queue Service": ["sqs", "queue"],
    "Amazon Simple Notification Service": ["sns", "notification"],
    "Amazon Kinesis": ["kinesis", "stream"],
    "Amazon Managed Streaming for Apache Kafka": ["msk", "kafka"],
    "AWS Glue": ["glue", "etl"],
    "Amazon Athena": ["athena", "query"],
    "Amazon EMR": ["emr", "bigdata", "hadoop"],
    "AWS Database Migration Service": ["dms", "migration"],
    "Amazon SageMaker": ["sagemaker", "ml"],
    "Amazon Bedrock": ["bedrock", "genai"],
    "AWS Step Functions": ["stepfunctions", "sfn", "workflow"],
    "Amazon EventBridge": ["eventbridge", "events"],
    "Amazon DocumentDB": ["documentdb", "mongo"],
    "Amazon Neptune": ["neptune", "graph"],
    "Amazon ElastiCache": ["elasticache", "redis", "cache"],
    "Amazon MemoryDB for Redis": ["memorydb", "redis"],
    "AWS AppSync": ["appsync", "graphql"],
    "Amazon Elastic File System": ["efs", "file", "storage"],
    "Amazon FSx": ["fsx", "file", "storage"],
    "AWS Backup": ["backup"],
    "AWS CodeBuild": ["codebuild", "build", "cicd"],
    "AWS Key Management Service": ["kms"],
    "AWS Secrets Manager": ["secret"],
    "AWS Certificate Manager": ["acm", "cert", "pki"],
    "AWS Private Certificate Authority": ["acm", "cert", "pki"],
    "Amazon GuardDuty": ["guardduty", "sec"],
    "AWS Security Hub": ["securityhub", "sec"],
    "AWS WAF": ["waf"],
    "AWS Shield": ["shield", "ddos"],
    "AWS Config": ["config", "compliance"],
    "Amazon VPC Lattice": ["lattice", "net"],
    "Amazon Elastic Load Balancing": ["elb", "lb", "loadbalancer"],
}

# Legacy single-dict alias — several tests and external code may still
# import SERVICE_KEYWORDS. Point it at the GCP dict for back-compat.
SERVICE_KEYWORDS = SERVICE_KEYWORDS_GCP

# Inference scoring constants — tuned for the kind of evidence we get from
# Console exports. Total score range ~0..120. Anything >= 30 surfaces.
SCORE_NAME_MATCH = 50
SCORE_PCT_MATCH_TIGHT = 50    # within 5% relative
SCORE_PCT_MATCH_LOOSE = 25    # within 25% relative
SCORE_MAGNITUDE_EXACT = 40    # project total within 10% of service total
SCORE_MAGNITUDE_CONTAINS = 20 # project >= service AND project is top-3
INFERENCE_THRESHOLD = 30
TOP_HOMES_PER_SPIKE = 3


def _attach_likely_homes(spikes: list[CostSpike]) -> None:
    """Cross-file inference: which projects most likely host each service spike,
    and vice versa.

    The 3 Console-export files cannot be joined row-by-row. But the totals
    and percent-changes give strong signals. We score every (service ↔ project)
    pair by name overlap, percent-change closeness, and magnitude fit.
    Pairs scoring above INFERENCE_THRESHOLD are attached to the spike's
    `likely_homes` field, sorted by score descending.
    """
    if not spikes:
        return

    services = [s for s in spikes if s.grouping == "service"]
    # A "home" is a project (GCP) or account (AWS) — both name the tenant
    # that owns the services. We treat them interchangeably here.
    projects = [s for s in spikes if s.grouping in ("project", "account")]
    if not services or not projects:
        return

    project_total_max = max(p.current_cost for p in projects)
    top3_project_ids = {
        p.service for p in sorted(projects, key=lambda x: -x.current_cost)[:3]
    }

    # ---- service spikes get a list of likely project homes ----
    for svc in services:
        scored: list[tuple[CostSpike, int, str]] = []
        for proj in projects:
            score, reasons = _score_match(svc, proj, "service")
            if proj.service in top3_project_ids and proj.current_cost >= svc.current_cost:
                score += SCORE_MAGNITUDE_CONTAINS
                reasons.append("top-3 project, large enough to contain service")
            if score >= INFERENCE_THRESHOLD:
                scored.append((proj, score, "; ".join(reasons)))
        scored.sort(key=lambda x: -x[1])
        svc.likely_homes = [
            (p.service, score, reason) for p, score, reason in scored[:TOP_HOMES_PER_SPIKE]
        ]

    # ---- project spikes get a list of likely service contents ----
    for proj in projects:
        scored: list[tuple[CostSpike, int, str]] = []
        for svc in services:
            score, reasons = _score_match(svc, proj, "project")
            if score >= INFERENCE_THRESHOLD:
                scored.append((svc, score, "; ".join(reasons)))
        scored.sort(key=lambda x: -x[1])
        proj.likely_homes = [
            (s.service, score, reason) for s, score, reason in scored[:TOP_HOMES_PER_SPIKE]
        ]


def _score_match(
    service_spike: CostSpike, project_spike: CostSpike, perspective: str
) -> tuple[int, list[str]]:
    """Score a (service, project) pair. Returns (score, list_of_reasons).

    perspective="service" → reasons phrased from the service's POV
    perspective="project" → reasons phrased from the project's POV
    """
    score = 0
    reasons: list[str] = []

    # ---- 1. Name match ----
    # Pick the right keyword dict by heuristic: AWS service names begin
    # with "Amazon " / "AWS " / "AmazonCloudWatch" (no-space variant).
    svc_name = service_spike.service
    is_aws = (
        svc_name.startswith("Amazon ")
        or svc_name.startswith("AWS ")
        or svc_name.startswith("AmazonCloudWatch")
        or svc_name == "EC2 - Other"
    )
    kw_dict = SERVICE_KEYWORDS_AWS if is_aws else SERVICE_KEYWORDS_GCP
    keywords = kw_dict.get(svc_name, [])
    if not keywords:
        # Fallback: use words from the service name itself, min 4 chars to
        # avoid noise (3-char curated keywords are OK; auto-extracted ones
        # need to be slightly longer to be reliable). Drop generic glue
        # words that show up across many services.
        stopwords = {
            "cloud", "service", "engine", "manager", "platform",
            "amazon", "aws", "elastic", "simple",
        }
        keywords = [
            w.lower() for w in svc_name.split()
            if len(w) >= 4 and w.lower() not in stopwords
        ]
    # Final guard: every keyword must be at least 3 chars
    keywords = [k for k in keywords if len(k) >= 3]
    project_lower = project_spike.service.lower()
    matched_keyword = next((kw for kw in keywords if kw in project_lower), None)
    if matched_keyword:
        score += SCORE_NAME_MATCH
        reasons.append(f"project name contains '{matched_keyword}'")

    # ---- 2. Percent-change match ----
    if (
        service_spike.previous_cost > 0
        and project_spike.previous_cost > 0
        and abs(service_spike.change_percent) >= 20
        and abs(project_spike.change_percent) >= 20
    ):
        svc_pct = service_spike.change_percent
        proj_pct = project_spike.change_percent
        # Same sign?
        if (svc_pct > 0) == (proj_pct > 0):
            relative_diff = abs(svc_pct - proj_pct) / max(abs(svc_pct), 1)
            if relative_diff < 0.05:
                score += SCORE_PCT_MATCH_TIGHT
                reasons.append(
                    f"both spiking ~{svc_pct:+.0f}% (project {proj_pct:+.0f}%)"
                )
            elif relative_diff < 0.25:
                score += SCORE_PCT_MATCH_LOOSE
                reasons.append(
                    f"both spiking same direction ({svc_pct:+.0f}% vs {proj_pct:+.0f}%)"
                )

    # ---- 3. Magnitude match ----
    if (
        service_spike.current_cost > 0
        and project_spike.current_cost > 0
    ):
        ratio = (
            min(service_spike.current_cost, project_spike.current_cost)
            / max(service_spike.current_cost, project_spike.current_cost)
        )
        if ratio >= 0.9:
            score += SCORE_MAGNITUDE_EXACT
            reasons.append(
                f"totals nearly equal "
                f"(svc ${service_spike.current_cost:,.0f} ≈ proj ${project_spike.current_cost:,.0f})"
            )

    return score, reasons


def _attach_top_contributors(
    spikes: list[CostSpike], rows: list[NormalizedRow]
) -> None:
    """For each spike, compute the top contributors across other dimensions.

    A "contributor" is another dimension column present on the same rows
    that match this spike. e.g. a service-level spike on Cloud DNS might
    have top SKUs and top projects if those columns are in the source file.
    A project-level spike on prod-edge might have top services and SKUs.

    Only the current period (after midpoint) is counted, and only rows
    matching this spike's (grouping, value) key are considered.
    """
    if not spikes:
        return

    days = sorted({r.day for r in rows if r.day is not None})
    midpoint = days[len(days) // 2] if len(days) >= 2 else None

    # Index rows by their (grouping, value) key, current period only
    rows_by_key: dict[tuple[str, str], list[NormalizedRow]] = {}
    for r in rows:
        if midpoint is not None and r.day is not None and r.day < midpoint:
            continue
        rows_by_key.setdefault((r.grouping, r.grouping_value), []).append(r)

    for spike in spikes:
        matching = rows_by_key.get((spike.grouping, spike.service), [])
        if not matching:
            continue

        for dim in DIMENSION_KEY_GROUPS:
            if dim == spike.grouping:
                continue  # don't show the spike's own dimension as a contributor
            totals: dict[str, float] = {}
            # For opaque identifier dimensions (sku, usage_type), also
            # collect a representative human-readable description so the
            # reasoner and the user see "$1.624 per g5.4xlarge Instance
            # Hour" next to a cryptic SKU ID. For each contributor value
            # we keep the description from its highest-cost row.
            descriptions_for_dim: dict[str, tuple[float, str]] = {}
            for r in matching:
                value = getattr(r, dim)
                if value is None:
                    continue
                totals[value] = totals.get(value, 0.0) + r.cost
                if dim in ("sku", "usage_type") and r.description:
                    best = descriptions_for_dim.get(value)
                    if best is None or r.cost > best[0]:
                        descriptions_for_dim[value] = (r.cost, r.description)
            if not totals:
                continue
            ranked = sorted(
                totals.items(), key=lambda kv: kv[1], reverse=True
            )[:TOP_CONTRIBUTORS_LIMIT]
            # Attach descriptions for the contributors we're actually showing.
            for (contributor_name, _cost) in ranked:
                desc_entry = descriptions_for_dim.get(contributor_name)
                if desc_entry:
                    spike.contributor_descriptions[
                        f"{dim}:{contributor_name}"
                    ] = desc_entry[1]
            spike.top_contributors[dim] = ranked
