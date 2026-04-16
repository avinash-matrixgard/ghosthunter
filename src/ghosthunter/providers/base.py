"""Provider abstraction: the common surface GCP/AWS/future providers share.

Every cloud provider Ghosthunter supports implements `BaseProvider`:

- `fetch_billing_spikes()` — active mode: query the cloud's billing API.
- `execute_command()`      — sandboxed shell execution for commands the
                             reasoner proposes. Already-validated commands
                             only; providers re-validate as defense in depth.
- Metadata methods          (`cli_tools`, `env_keep_list`, ...) let the
                             validator, sandbox, and reasoner prompt pick
                             the right rules per provider.

Provider-neutral data types (`CostSpike`, `CommandResult`) and the shared
error hierarchy live here. Concrete provider modules import from `.base`
and re-export these for back-compat with existing import sites.
"""
from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Any, ClassVar


# ---------------------------------------------------------------------------
# Data structures (provider-neutral)
# ---------------------------------------------------------------------------
@dataclass
class CostSpike:
    """A cost dimension whose value changed materially over a window.

    `top_contributors` is an optional map of dimension -> list of
    (name, cost) tuples capturing where the spike came from. e.g.
        {"sku":     [("Internet Egress NA", 8120.40), ...],
         "project": [("prod-web", 6200.00), ...]}
    Populated by the billing-file provider when extra columns are present;
    empty in pure single-file mode.
    """
    service: str
    current_cost: float
    previous_cost: float
    change_percent: float
    daily_breakdown: list[dict[str, Any]] = field(default_factory=list)
    top_contributors: dict[str, list[tuple[str, float]]] = field(default_factory=dict)
    # How the spike is keyed. Values used so far:
    #   "service", "project", "account", "sku", "usage_type", "location"
    grouping: str = "service"
    # Cross-file inference: for a service-level spike, which projects/accounts
    # most likely host it (and vice versa). Each entry is (name, score, reason).
    likely_homes: list[tuple[str, int, str]] = field(default_factory=list)

    @property
    def absolute_change(self) -> float:
        return self.current_cost - self.previous_cost


@dataclass
class CommandResult:
    """Result of executing a shell command in the sandbox."""
    command: str
    stdout: str
    stderr: str
    exit_code: int
    duration_seconds: float
    truncated: bool = False

    @property
    def succeeded(self) -> bool:
        return self.exit_code == 0


# ---------------------------------------------------------------------------
# Errors
# ---------------------------------------------------------------------------
class ProviderError(Exception):
    """Base error for any provider. Concrete providers may subclass."""


class CommandRejectedError(ProviderError):
    """Raised when a command fails security validation at execution time."""


class CommandTimeoutError(ProviderError):
    """Raised when a command exceeds its timeout."""


# ---------------------------------------------------------------------------
# Provider ABC
# ---------------------------------------------------------------------------
class BaseProvider(ABC):
    """Abstract surface every cloud provider implements.

    Implementations:
      - `providers.gcp.GCPProvider`
      - `providers.aws.AWSProvider`  (Phase 2+)

    The advisor mode's `AdvisorProvider` is NOT a BaseProvider — it's a
    shim around a chosen provider's validator + allowlist. The provider
    key it carries (`provider_key`) tells the allowlist dispatcher which
    ruleset to enforce.
    """

    provider_key: ClassVar[str] = ""  # "gcp" | "aws"

    @abstractmethod
    def fetch_billing_spikes(
        self,
        lookback_days: int = 30,
        min_change_percent: float = 20.0,
        min_absolute_change: float = 100.0,
    ) -> list[CostSpike]:
        """Return cost spikes from the provider's billing API (active mode)."""

    @abstractmethod
    async def execute_command(self, command: str) -> CommandResult:
        """Execute a single shell command in the sandbox."""

    # ------------------------------------------------------------------
    # Metadata — concrete providers override. Defaults keep the ABC
    # usable without every subclass overriding everything.
    # ------------------------------------------------------------------
    def env_keep_list(self) -> set[str]:
        """Env vars the sandbox should preserve (credentials, region, etc)."""
        return {"PATH", "HOME", "USER", "LANG", "LC_ALL"}

    def cli_tools(self) -> tuple[str, ...]:
        """CLI binaries this provider's allowlist permits."""
        return ()

    def billing_template_help(self) -> str:
        """Human-readable export recipe shown by `ghosthunter billing-template`."""
        return ""

    def provider_hint_for_reasoner(self) -> str:
        """Provider-specific block appended to the Opus system prompt.

        Covers allowed verbs, format conventions, and common pitfalls
        for this provider's CLI. See `models.reasoner` for where this
        gets composed with the shared core prompt.
        """
        return ""


__all__ = [
    "BaseProvider",
    "CommandRejectedError",
    "CommandResult",
    "CommandTimeoutError",
    "CostSpike",
    "ProviderError",
]
