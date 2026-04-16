"""Config management for ~/.ghosthunter/config.toml.

The config file holds the cloud provider, billing source, and budget
knobs. The Anthropic API key is read from $ANTHROPIC_API_KEY at runtime
— we deliberately do NOT persist it to disk.

Backward compat: existing configs predating the `provider` field load
cleanly and default to ``provider = "gcp"``. Writing a loaded config
back out adds the field in-place.
"""
from __future__ import annotations

from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

import tomli
import tomli_w

CONFIG_DIR = Path.home() / ".ghosthunter"
CONFIG_PATH = CONFIG_DIR / "config.toml"
AUDIT_LOG_PATH = CONFIG_DIR / "audit.log"


@dataclass
class BudgetConfig:
    max_commands: int = 15
    max_cost_usd: float = 1.0
    max_seconds: float = 600.0


@dataclass
class AWSConfig:
    """AWS-specific config. Populated when provider == "aws".

    Phase 1 defines the shape; Phase 2 wires the `init` CLI to populate it
    and Phase 4 uses `ce_api_cost_ack` to suppress the CE-cost banner
    after first acknowledgment.
    """
    profile: str = ""
    region: str = "us-east-1"
    account_id: str = ""
    ce_api_cost_ack: bool = False


@dataclass
class Config:
    # Cloud provider — "gcp" (default) or "aws".
    provider: str = "gcp"

    # GCP-specific (used when provider == "gcp")
    project_id: str = ""
    billing_dataset: str = ""

    # AWS-specific (used when provider == "aws")
    aws: AWSConfig | None = None

    # Shared
    lookback_days: int = 30
    budget: BudgetConfig = field(default_factory=BudgetConfig)

    @classmethod
    def load(cls, path: Path = CONFIG_PATH) -> "Config":
        if not path.exists():
            raise FileNotFoundError(
                f"No config at {path}. Run `ghosthunter init` first."
            )
        with path.open("rb") as f:
            data = tomli.load(f)
        budget_data = data.pop("budget", {})
        aws_data = data.pop("aws", None)
        aws_cfg = AWSConfig(**aws_data) if aws_data else None
        return cls(
            provider=data.get("provider", "gcp"),
            project_id=data.get("project_id", ""),
            billing_dataset=data.get("billing_dataset", ""),
            aws=aws_cfg,
            lookback_days=int(data.get("lookback_days", 30)),
            budget=BudgetConfig(**budget_data),
        )

    def save(self, path: Path = CONFIG_PATH) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        payload: dict[str, Any] = asdict(self)
        # tomli-w doesn't serialize None — drop aws if not configured.
        if payload.get("aws") is None:
            payload.pop("aws", None)
        with path.open("wb") as f:
            tomli_w.dump(payload, f)
