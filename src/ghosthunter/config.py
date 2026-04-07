"""Config management for ~/.ghosthunter/config.toml.

The config file holds the GCP project, billing dataset, and budget knobs.
The Anthropic API key is read from $ANTHROPIC_API_KEY at runtime — we
deliberately do NOT persist it to disk.
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
class Config:
    project_id: str = ""
    billing_dataset: str = ""
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
        return cls(
            project_id=data.get("project_id", ""),
            billing_dataset=data.get("billing_dataset", ""),
            lookback_days=int(data.get("lookback_days", 30)),
            budget=BudgetConfig(**budget_data),
        )

    def save(self, path: Path = CONFIG_PATH) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        payload: dict[str, Any] = asdict(self)
        with path.open("wb") as f:
            tomli_w.dump(payload, f)
