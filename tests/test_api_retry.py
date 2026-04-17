"""Tests for ``models/_api_retry.call_with_retry``.

The retry helper sits between both model clients (reasoner.step and
executor.{semantic_validate,compress}) and the Anthropic SDK. It
must:

1. **Surface non-retryable failures immediately** (401/403/400/404/422)
   with a typed wrapper class and an actionable hint. No wasted
   retry attempts, no budget burned.
2. **Retry transient failures** (429/529/5xx/connection) with bounded
   exponential backoff + jitter.
3. **Honor server-supplied ``retry-after`` headers** on rate-limit
   responses.
4. **Raise a terminal ``ModelAPIError`` subclass** after the retry
   budget is exhausted, including the original SDK message so the
   user can still see the underlying detail.

All tests stub out ``asyncio.sleep`` to zero so the suite stays fast.
"""
from __future__ import annotations

import asyncio
from unittest.mock import patch

import pytest

from ghosthunter.models import _api_retry
from ghosthunter.models._api_retry import (
    MAX_RETRIES,
    ModelAPIError,
    ModelAuthError,
    ModelBadRequestError,
    ModelConnectionError,
    ModelOverloadedError,
    ModelPermissionError,
    ModelRateLimitError,
    ModelServerError,
    call_with_retry,
)


# ---------------------------------------------------------------------------
# Fakes mirroring the anthropic SDK's exception hierarchy
# ---------------------------------------------------------------------------
def _make_fake_anthropic_module():
    """Build a module-like object mimicking anthropic's public exception
    classes so we can feed different error shapes to call_with_retry
    without a live SDK."""

    class _APIStatusError(Exception):
        def __init__(self, msg="", status_code=None, response=None):
            super().__init__(msg)
            self.status_code = status_code
            self.response = response

    class _AuthenticationError(_APIStatusError): pass
    class _PermissionDeniedError(_APIStatusError): pass
    class _NotFoundError(_APIStatusError): pass
    class _BadRequestError(_APIStatusError): pass
    class _UnprocessableEntityError(_APIStatusError): pass
    class _RateLimitError(_APIStatusError): pass
    class _OverloadedError(_APIStatusError): pass
    class _InternalServerError(_APIStatusError): pass
    class _APIConnectionError(_APIStatusError): pass
    class _APITimeoutError(_APIStatusError): pass

    class _Module:
        APIStatusError = _APIStatusError
        AuthenticationError = _AuthenticationError
        PermissionDeniedError = _PermissionDeniedError
        NotFoundError = _NotFoundError
        BadRequestError = _BadRequestError
        UnprocessableEntityError = _UnprocessableEntityError
        RateLimitError = _RateLimitError
        OverloadedError = _OverloadedError
        InternalServerError = _InternalServerError
        APIConnectionError = _APIConnectionError
        APITimeoutError = _APITimeoutError

    return _Module()


@pytest.fixture
def fake_anthropic(monkeypatch):
    """Patch _classify_retryable's lazy ``import anthropic`` to our fake."""
    mod = _make_fake_anthropic_module()
    import sys
    monkeypatch.setitem(sys.modules, "anthropic", mod)
    return mod


@pytest.fixture(autouse=True)
def no_real_sleep(monkeypatch):
    """Zero out asyncio.sleep so retry backoffs don't slow the suite."""
    async def _fast_sleep(*_args, **_kwargs):
        return None
    monkeypatch.setattr(_api_retry.asyncio, "sleep", _fast_sleep)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------
def _call_n_times(seq: list):
    """Return an async callable that raises / returns from ``seq`` in order.

    Each element is either a value (returned as-is) or an Exception
    instance (raised). Pops from the front; the callable raises
    IndexError if called more times than seq has elements.
    """
    items = list(seq)

    async def _fn():
        if not items:
            raise IndexError("_call_n_times exhausted")
        item = items.pop(0)
        if isinstance(item, BaseException):
            raise item
        return item

    return _fn


# ---------------------------------------------------------------------------
# Happy path
# ---------------------------------------------------------------------------
class TestHappyPath:
    def test_returns_value_on_first_try(self, fake_anthropic):
        fn = _call_n_times(["ok"])
        result = asyncio.run(call_with_retry(fn, op_name="test"))
        assert result == "ok"


# ---------------------------------------------------------------------------
# Non-retryable errors — fail fast, no retries
# ---------------------------------------------------------------------------
class TestNonRetryable:
    def test_401_raises_model_auth_error(self, fake_anthropic):
        exc = fake_anthropic.AuthenticationError("bad key", status_code=401)
        fn = _call_n_times([exc])
        with pytest.raises(ModelAuthError) as excinfo:
            asyncio.run(call_with_retry(fn, op_name="Opus reasoning"))
        # The hint should tell the user where to look.
        assert "ANTHROPIC_API_KEY" in str(excinfo.value)
        assert "Opus reasoning" in str(excinfo.value)

    def test_403_raises_model_permission_error(self, fake_anthropic):
        exc = fake_anthropic.PermissionDeniedError("nope", status_code=403)
        fn = _call_n_times([exc])
        with pytest.raises(ModelPermissionError) as excinfo:
            asyncio.run(call_with_retry(fn, op_name="Sonnet"))
        assert "plan" in str(excinfo.value).lower() or "model" in str(excinfo.value).lower()

    def test_400_raises_model_bad_request_error(self, fake_anthropic):
        exc = fake_anthropic.BadRequestError("bad schema", status_code=400)
        fn = _call_n_times([exc])
        with pytest.raises(ModelBadRequestError) as excinfo:
            asyncio.run(call_with_retry(fn, op_name="test"))
        assert "bug" in str(excinfo.value).lower()

    def test_422_treated_as_bad_request(self, fake_anthropic):
        exc = fake_anthropic.UnprocessableEntityError("...", status_code=422)
        with pytest.raises(ModelBadRequestError):
            asyncio.run(call_with_retry(_call_n_times([exc]), op_name="t"))

    def test_non_retryable_makes_no_second_call(self, fake_anthropic):
        exc = fake_anthropic.AuthenticationError("x", status_code=401)
        calls = 0

        async def _fn():
            nonlocal calls
            calls += 1
            raise exc

        with pytest.raises(ModelAuthError):
            asyncio.run(call_with_retry(_fn, op_name="t"))
        assert calls == 1


# ---------------------------------------------------------------------------
# Retryable errors — retry then eventually succeed
# ---------------------------------------------------------------------------
class TestRetryableThenSuccess:
    def test_single_rate_limit_then_success(self, fake_anthropic):
        exc = fake_anthropic.RateLimitError("slow down", status_code=429)
        fn = _call_n_times([exc, "ok"])
        result = asyncio.run(call_with_retry(fn, op_name="t"))
        assert result == "ok"

    def test_overloaded_then_success(self, fake_anthropic):
        exc = fake_anthropic.OverloadedError("busy", status_code=529)
        fn = _call_n_times([exc, "ok"])
        result = asyncio.run(call_with_retry(fn, op_name="t"))
        assert result == "ok"

    def test_5xx_then_success(self, fake_anthropic):
        exc = fake_anthropic.InternalServerError("down", status_code=500)
        fn = _call_n_times([exc, "ok"])
        result = asyncio.run(call_with_retry(fn, op_name="t"))
        assert result == "ok"

    def test_connection_error_then_success(self, fake_anthropic):
        exc = fake_anthropic.APIConnectionError("dns")
        fn = _call_n_times([exc, "ok"])
        result = asyncio.run(call_with_retry(fn, op_name="t"))
        assert result == "ok"

    def test_timeout_then_success(self, fake_anthropic):
        exc = fake_anthropic.APITimeoutError("timeout")
        fn = _call_n_times([exc, "ok"])
        result = asyncio.run(call_with_retry(fn, op_name="t"))
        assert result == "ok"


# ---------------------------------------------------------------------------
# Retryable errors — budget exhausted
# ---------------------------------------------------------------------------
class TestRetryBudgetExhausted:
    def test_persistent_rate_limit_raises_terminal(self, fake_anthropic):
        exc = fake_anthropic.RateLimitError("slow down", status_code=429)
        # MAX_RETRIES + 1 total attempts (initial + retries). All fail.
        fn = _call_n_times([exc] * (MAX_RETRIES + 1))
        with pytest.raises(ModelRateLimitError) as excinfo:
            asyncio.run(call_with_retry(fn, op_name="Opus reasoning"))
        msg = str(excinfo.value)
        assert "retries" in msg.lower()
        assert "Opus reasoning" in msg
        # Original SDK message preserved.
        assert "slow down" in msg

    def test_persistent_overload_raises_terminal(self, fake_anthropic):
        exc = fake_anthropic.OverloadedError("busy", status_code=529)
        fn = _call_n_times([exc] * (MAX_RETRIES + 1))
        with pytest.raises(ModelOverloadedError):
            asyncio.run(call_with_retry(fn, op_name="t"))

    def test_persistent_5xx_raises_terminal(self, fake_anthropic):
        exc = fake_anthropic.InternalServerError("down", status_code=500)
        fn = _call_n_times([exc] * (MAX_RETRIES + 1))
        with pytest.raises(ModelServerError):
            asyncio.run(call_with_retry(fn, op_name="t"))

    def test_persistent_connection_raises_terminal(self, fake_anthropic):
        exc = fake_anthropic.APIConnectionError("offline")
        fn = _call_n_times([exc] * (MAX_RETRIES + 1))
        with pytest.raises(ModelConnectionError):
            asyncio.run(call_with_retry(fn, op_name="t"))

    def test_exact_retry_count(self, fake_anthropic):
        """Verify total attempts == MAX_RETRIES + 1."""
        exc = fake_anthropic.RateLimitError("slow", status_code=429)
        calls = 0

        async def _fn():
            nonlocal calls
            calls += 1
            raise exc

        with pytest.raises(ModelRateLimitError):
            asyncio.run(call_with_retry(_fn, op_name="t"))
        assert calls == MAX_RETRIES + 1


# ---------------------------------------------------------------------------
# retry-after header handling
# ---------------------------------------------------------------------------
class TestRetryAfterHeader:
    def test_retry_after_read_from_response_headers(self, fake_anthropic, monkeypatch):
        """If the SDK exposes ``response.headers['retry-after']``, the
        backoff uses that value (capped) instead of the exponential
        default."""

        class _FakeResponse:
            headers = {"retry-after": "7"}

        exc = fake_anthropic.RateLimitError(
            "x", status_code=429, response=_FakeResponse()
        )

        sleeps_requested: list[float] = []

        async def _record_sleep(seconds):
            sleeps_requested.append(seconds)

        monkeypatch.setattr(_api_retry.asyncio, "sleep", _record_sleep)

        fn = _call_n_times([exc, "ok"])
        asyncio.run(call_with_retry(fn, op_name="t"))
        # First retry should wait ~7s (+ jitter, up to 1s).
        assert sleeps_requested
        assert 7.0 <= sleeps_requested[0] <= 8.0 + 0.01

    def test_missing_retry_after_falls_back_to_exponential(self, fake_anthropic, monkeypatch):
        """When no retry-after header is present, backoff uses the
        INITIAL_BACKOFF_SECONDS * 2**attempt formula."""
        exc = fake_anthropic.RateLimitError("x", status_code=429)
        sleeps_requested: list[float] = []

        async def _record_sleep(seconds):
            sleeps_requested.append(seconds)

        monkeypatch.setattr(_api_retry.asyncio, "sleep", _record_sleep)

        fn = _call_n_times([exc, exc, "ok"])
        asyncio.run(call_with_retry(fn, op_name="t"))
        assert len(sleeps_requested) == 2
        # Attempt 0 → 2s base, attempt 1 → 4s base (+ jitter 0-1s each).
        assert 2.0 <= sleeps_requested[0] <= 3.0 + 0.01
        assert 4.0 <= sleeps_requested[1] <= 5.0 + 0.01


# ---------------------------------------------------------------------------
# status_code fallback for bare APIStatusError
# ---------------------------------------------------------------------------
class TestStatusCodeFallback:
    """Some SDK versions raise bare APIStatusError with only status_code
    set, not a specific subclass. Ensure _classify_retryable still
    picks the right behavior."""

    @pytest.mark.parametrize(
        "code,expected_cls,retryable",
        [
            (401, ModelAuthError, False),
            (403, ModelPermissionError, False),
            (400, ModelBadRequestError, False),
            (404, ModelBadRequestError, False),
            (429, ModelRateLimitError, True),
            (529, ModelOverloadedError, True),
            (500, ModelServerError, True),
            (503, ModelServerError, True),
        ],
    )
    def test_classify_by_status_code(
        self, fake_anthropic, code, expected_cls, retryable
    ):
        exc = fake_anthropic.APIStatusError("x", status_code=code)
        if retryable:
            fn = _call_n_times([exc] * (MAX_RETRIES + 1))
        else:
            fn = _call_n_times([exc])
        with pytest.raises(expected_cls):
            asyncio.run(call_with_retry(fn, op_name="t"))


# ---------------------------------------------------------------------------
# Terminal wrapping preserves detail
# ---------------------------------------------------------------------------
class TestErrorMessageQuality:
    def test_op_name_in_message(self, fake_anthropic):
        exc = fake_anthropic.AuthenticationError("revoked", status_code=401)
        with pytest.raises(ModelAuthError) as excinfo:
            asyncio.run(
                call_with_retry(
                    _call_n_times([exc]), op_name="Sonnet semantic validation"
                )
            )
        assert "Sonnet semantic validation" in str(excinfo.value)

    def test_original_message_preserved(self, fake_anthropic):
        exc = fake_anthropic.BadRequestError(
            "tool input did not match schema", status_code=400
        )
        with pytest.raises(ModelBadRequestError) as excinfo:
            asyncio.run(call_with_retry(_call_n_times([exc]), op_name="t"))
        assert "tool input did not match schema" in str(excinfo.value)

    def test_actionable_hint_present(self, fake_anthropic):
        for cls_name, attr, check in [
            ("AuthenticationError", "AuthenticationError", "ANTHROPIC_API_KEY"),
            ("RateLimitError", "RateLimitError", "Wait a few minutes"),
            ("InternalServerError", "InternalServerError", "status.anthropic.com"),
        ]:
            exc_cls = getattr(fake_anthropic, attr)
            exc = exc_cls("x", status_code=None)
            needed_retries = cls_name == "RateLimitError" or cls_name == "InternalServerError"
            if needed_retries:
                fn = _call_n_times([exc] * (MAX_RETRIES + 1))
            else:
                fn = _call_n_times([exc])
            with pytest.raises(ModelAPIError) as excinfo:
                asyncio.run(call_with_retry(fn, op_name="t"))
            assert check in str(excinfo.value), f"{cls_name}: hint missing ({check!r})"
