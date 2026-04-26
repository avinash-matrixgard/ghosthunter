"""Retry + friendly-error wrapping for Anthropic API calls.

The two model clients (``reasoner.step`` and ``executor.semantic_validate``
/ ``executor.compress``) both call ``client.messages.create(...)``. A
transient failure there — a 429 rate limit, a 529 overloaded response,
a network blip — should not crash mid-investigation. Conversely, a 401
(bad API key) or a 400 (malformed tool schema) should surface loudly
with an actionable message, not a stack trace.

This module centralizes the retry/backoff policy so both call sites
behave identically. It intentionally does **not** bury errors — a
persistent outage still raises after the retry budget is spent, with
a message that tells the user what to check.

Design choices:

- **Bounded retries.** ``MAX_RETRIES = 3``. A rate-limit storm during
  a 15-command investigation should not silently consume the whole
  per-investigation budget on retries.
- **Exponential backoff with jitter.** 2s → 4s → 8s, plus 0-1s jitter.
- **Honor ``retry-after`` headers** when the SDK surfaces them.
- **Do not retry auth / validation errors.** Those are configuration
  problems; the caller needs to fix them, not wait.
"""

from __future__ import annotations

import asyncio
import random
from typing import TYPE_CHECKING, Awaitable, Callable, TypeVar

if TYPE_CHECKING:
    pass


T = TypeVar("T")

MAX_RETRIES = 3
INITIAL_BACKOFF_SECONDS = 2.0
MAX_BACKOFF_SECONDS = 30.0
JITTER_SECONDS = 1.0


# ---------------------------------------------------------------------------
# Error taxonomy — module-level so callers can catch specific cases.
# ---------------------------------------------------------------------------
class ModelAPIError(Exception):
    """Base class for any failure calling the Anthropic API after retries."""


class ModelAuthError(ModelAPIError):
    """401 — ANTHROPIC_API_KEY is missing, invalid, or revoked."""


class ModelPermissionError(ModelAPIError):
    """403 — key lacks permission for the requested model."""


class ModelBadRequestError(ModelAPIError):
    """400 — malformed request (wrong tool schema, too many tokens, etc).

    These are bugs in Ghosthunter, not user-recoverable. Retry would be
    useless; re-raise with clear context so reports are actionable.
    """


class ModelRateLimitError(ModelAPIError):
    """429 — sustained rate limit even after retry budget exhausted."""


class ModelOverloadedError(ModelAPIError):
    """529 — Anthropic reports the model as overloaded after retries."""


class ModelServerError(ModelAPIError):
    """5xx — Anthropic-side outage after retry budget exhausted."""


class ModelConnectionError(ModelAPIError):
    """Network / DNS / TLS failure after retry budget exhausted."""


# ---------------------------------------------------------------------------
# Retry driver
# ---------------------------------------------------------------------------
def _classify_retryable(exc: Exception) -> tuple[bool, type[ModelAPIError]]:
    """Inspect an exception raised by the anthropic SDK and decide whether
    to retry. Returns ``(retryable, terminal_wrapper)``.

    The anthropic SDK defines subclasses like ``RateLimitError``,
    ``AuthenticationError``, etc. We import them lazily so the ghosthunter
    package still imports cleanly in environments where ``anthropic`` is
    somehow unavailable (the CLI hard-requires it, but tests may mock it).
    """
    try:
        import anthropic  # type: ignore
    except ImportError:  # pragma: no cover — tests always import it
        return False, ModelAPIError

    # Auth / permission: do not retry, surface immediately.
    if isinstance(exc, getattr(anthropic, "AuthenticationError", ())):
        return False, ModelAuthError
    if isinstance(exc, getattr(anthropic, "PermissionDeniedError", ())):
        return False, ModelPermissionError
    if isinstance(exc, getattr(anthropic, "NotFoundError", ())):
        # Model name typo, for example. Configuration problem.
        return False, ModelBadRequestError
    if isinstance(exc, getattr(anthropic, "BadRequestError", ())):
        return False, ModelBadRequestError
    if isinstance(exc, getattr(anthropic, "UnprocessableEntityError", ())):
        return False, ModelBadRequestError

    # Transient: retry.
    if isinstance(exc, getattr(anthropic, "RateLimitError", ())):
        return True, ModelRateLimitError
    # Some SDK versions expose OverloadedError (529); fall back to
    # inspecting status_code if the class isn't present.
    overloaded = getattr(anthropic, "OverloadedError", None)
    if overloaded is not None and isinstance(exc, overloaded):
        return True, ModelOverloadedError
    if isinstance(exc, getattr(anthropic, "InternalServerError", ())):
        return True, ModelServerError
    if isinstance(exc, getattr(anthropic, "APIConnectionError", ())):
        return True, ModelConnectionError
    if isinstance(exc, getattr(anthropic, "APITimeoutError", ())):
        return True, ModelConnectionError

    # Generic APIStatusError: classify by status_code if available.
    status = getattr(exc, "status_code", None)
    if status is not None:
        if status == 401:
            return False, ModelAuthError
        if status == 403:
            return False, ModelPermissionError
        if status in (400, 404, 422):
            return False, ModelBadRequestError
        if status == 429:
            return True, ModelRateLimitError
        if status == 529:
            return True, ModelOverloadedError
        if 500 <= status < 600:
            return True, ModelServerError

    # Unknown SDK-raised exception — treat as non-retryable to avoid
    # accidentally burning the retry budget on a bug.
    return False, ModelAPIError


def _retry_after_seconds(exc: Exception) -> float | None:
    """Extract the ``retry-after`` header value from a rate-limit error,
    if the SDK exposes it. Returns None when unavailable."""
    response = getattr(exc, "response", None)
    if response is None:
        return None
    headers = getattr(response, "headers", None) or {}
    # httpx Headers vs dict: tolerate both.
    try:
        raw = headers.get("retry-after") or headers.get("Retry-After")
    except AttributeError:
        return None
    if raw is None:
        return None
    try:
        return float(raw)
    except (TypeError, ValueError):
        return None


async def call_with_retry(
    fn: Callable[[], Awaitable[T]],
    *,
    op_name: str,
    max_retries: int = MAX_RETRIES,
) -> T:
    """Run an async Anthropic SDK call with bounded retry and clear errors.

    Parameters
    ----------
    fn:
        Zero-arg async callable that performs the actual API call (usually
        a ``lambda: client.messages.create(...)``).
    op_name:
        Short description used in error messages (e.g. ``"Opus reasoning"``
        or ``"Sonnet compression"``).
    max_retries:
        Maximum number of retry attempts on transient failures. The total
        number of attempts is ``max_retries + 1``.

    Returns
    -------
    The value returned by ``fn`` on success.

    Raises
    ------
    ModelAPIError (or a subclass)
        On terminal failure — either a non-retryable error, or the retry
        budget was exhausted. The message explains what the user should do
        (check API key / wait / file a bug).
    """
    last_exc: Exception | None = None

    for attempt in range(max_retries + 1):
        try:
            return await fn()
        except Exception as exc:
            retryable, wrapper = _classify_retryable(exc)
            last_exc = exc

            if not retryable:
                raise _wrap(wrapper, op_name, exc) from exc

            if attempt >= max_retries:
                # Retries exhausted.
                raise _wrap(wrapper, op_name, exc, exhausted=True) from exc

            # Compute backoff: server-suggested > exponential.
            suggested = _retry_after_seconds(exc)
            if suggested is not None and suggested > 0:
                delay = min(suggested, MAX_BACKOFF_SECONDS)
            else:
                base = INITIAL_BACKOFF_SECONDS * (2**attempt)
                delay = min(base, MAX_BACKOFF_SECONDS)
            delay += random.uniform(0, JITTER_SECONDS)
            await asyncio.sleep(delay)

    # Unreachable — the loop either returns or raises.
    raise ModelAPIError(  # pragma: no cover
        f"{op_name}: exhausted retries (last error: {last_exc!r})"
    )


def _wrap(
    wrapper_cls: type[ModelAPIError],
    op_name: str,
    original: Exception,
    *,
    exhausted: bool = False,
) -> ModelAPIError:
    """Build a terminal error with an actionable message."""
    hint = _hint_for(wrapper_cls)
    prefix = f"{op_name} failed"
    if exhausted:
        prefix += f" after {MAX_RETRIES} retries"

    # Include the original exception's message so the user still sees the
    # raw SDK detail, but in a labelled way.
    detail = str(original).strip() or type(original).__name__
    message = f"{prefix}: {detail}"
    if hint:
        message += f"\n→ {hint}"
    return wrapper_cls(message)


def _hint_for(cls: type[ModelAPIError]) -> str:
    """Actionable next step for each error class."""
    if cls is ModelAuthError:
        return "Check that ANTHROPIC_API_KEY is set and still valid."
    if cls is ModelPermissionError:
        return (
            "Your API key doesn't have access to the required model. "
            "Check your plan or pick a different model."
        )
    if cls is ModelBadRequestError:
        return (
            "This looks like a Ghosthunter bug (malformed request to "
            "Anthropic). Please file an issue with the command that "
            "triggered it."
        )
    if cls is ModelRateLimitError:
        return (
            "Rate limit persisted across retries. Wait a few minutes or "
            "upgrade your Anthropic plan."
        )
    if cls is ModelOverloadedError:
        return "Anthropic is overloaded. Try again in a few minutes."
    if cls is ModelServerError:
        return "Anthropic-side outage. Check https://status.anthropic.com/."
    if cls is ModelConnectionError:
        return "Network / DNS / TLS failure. Check your internet connection."
    return ""


__all__ = [
    "ModelAPIError",
    "ModelAuthError",
    "ModelBadRequestError",
    "ModelConnectionError",
    "ModelOverloadedError",
    "ModelPermissionError",
    "ModelRateLimitError",
    "ModelServerError",
    "call_with_retry",
]
