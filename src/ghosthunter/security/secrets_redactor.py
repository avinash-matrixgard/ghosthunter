"""Layer 5: secrets redaction before any disk write.

Pasted command output may contain credentials. Without redaction, those
secrets land unredacted in:

  - ``~/.ghosthunter/audit.log`` — investigation outcomes (Opus's evidence
    summary may quote stdout snippets containing tokens).
  - The memory palace — conclusions stored for cross-session recall may
    include sensitive context.
  - Any future on-disk artifact a Ghost-hunter component writes.

Backups, Time Machine, cloud sync (Dropbox / iCloud) propagate the leak.
The user's mental model in paranoid mode is "Ghost-hunter is read-only and
safe — I can paste freely" — so we have to make that assumption true at
the disk-write layer.

Added in v1.0.8 per ghosthunter#3 (Apr 29 2026 audit, the only release-
blocking finding among four documented gaps).

Patterns target high-confidence credential shapes only. False positives
on normal billing data and command output are unacceptable in this
module — we redact what we're sure is a secret, not what *might be* one.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any

# Each pattern: (label, regex, replacement_fn).
# replacement_fn receives the match object so we can preserve the
# secret type in the redaction (helpful for debugging without leaking
# the value itself).
SECRET_PATTERNS: list[tuple[str, re.Pattern[str], str]] = [
    # AWS access keys — public format, deterministic prefix.
    ("aws_access_key", re.compile(r"\bAKIA[0-9A-Z]{16}\b"), "[REDACTED:aws_access_key]"),
    ("aws_temp_access_key", re.compile(r"\bASIA[0-9A-Z]{16}\b"), "[REDACTED:aws_temp_access_key]"),
    # GitHub PAT / app tokens — well-defined prefixes, fixed length.
    ("github_token", re.compile(r"\bgh[psru]_[A-Za-z0-9]{36,}\b"), "[REDACTED:github_token]"),
    # Anthropic API keys — `sk-ant-` prefix.
    ("anthropic_key", re.compile(r"\bsk-ant-[A-Za-z0-9_\-]{20,}\b"), "[REDACTED:anthropic_key]"),
    # OpenAI API keys — `sk-` followed by 48 chars of base62.
    ("openai_key", re.compile(r"\bsk-[A-Za-z0-9]{48}\b"), "[REDACTED:openai_key]"),
    # JWT-shape tokens (3 base64url segments, dot-separated).
    # Note: must come BEFORE bearer_token to win the race on `Bearer eyJ...`.
    (
        "jwt",
        re.compile(r"\beyJ[A-Za-z0-9_\-]+\.[A-Za-z0-9_\-]+\.[A-Za-z0-9_\-]+\b"),
        "[REDACTED:jwt]",
    ),
    # Bearer tokens in Authorization headers — capture everything until
    # whitespace, comma, or quote.
    (
        "bearer_token",
        re.compile(r"(?i)\bBearer\s+[A-Za-z0-9._~+/=\-]{16,}"),
        "[REDACTED:bearer_token]",
    ),
    # Generic Authorization / api-key headers. Handles three common forms:
    #   - shell:    api_key=longvalue
    #   - http:     Authorization: longvalue
    #   - json:     "x-api-key": "longvalue"
    # The closing-quote-on-key is what JSON requires; the optional quote on
    # the value covers shell, JSON, and YAML.
    (
        "auth_header",
        re.compile(
            r"""(?ix)
            \b(authorization|api[_-]?key|x-api-key)['\"]?
            \s*[:=]\s*
            ['\"]?[A-Za-z0-9._~+/=\-]{12,}['\"]?
            """
        ),
        "[REDACTED:auth_header]",
    ),
    # GCP service account JSON — match a private key block specifically
    # (the most sensitive part). The full SA JSON contains email + project
    # ID which are not secret on their own; the private_key field is what
    # matters.
    #
    # `[A-Z ]*` (zero-or-more) handles BOTH shapes:
    #   "-----BEGIN PRIVATE KEY-----"      (PKCS#8 — GCP default)
    #   "-----BEGIN RSA PRIVATE KEY-----"  (PKCS#1 — older AWS / generated)
    (
        "gcp_private_key",
        re.compile(
            r'"private_key"\s*:\s*"-----BEGIN [A-Z ]*PRIVATE KEY-----[\s\S]*?-----END [A-Z ]*PRIVATE KEY-----\\n?"',
        ),
        '"private_key":"[REDACTED:gcp_private_key]"',
    ),
    # PEM-armored private keys anywhere in text — same zero-or-more allowance
    # for the algorithm tag.
    (
        "pem_private_key",
        re.compile(
            r"-----BEGIN [A-Z ]*PRIVATE KEY-----[\s\S]*?-----END [A-Z ]*PRIVATE KEY-----",
        ),
        "[REDACTED:pem_private_key]",
    ),
]


@dataclass(frozen=True)
class RedactionResult:
    """Output of redact_secrets: cleaned text + per-pattern hit counts."""

    redacted: str
    redactions_by_pattern: dict[str, int]

    @property
    def total_redactions(self) -> int:
        return sum(self.redactions_by_pattern.values())

    @property
    def had_redactions(self) -> bool:
        return self.total_redactions > 0


def redact_secrets(text: str) -> RedactionResult:
    """Strip credential-shaped substrings from text.

    Idempotent and safe to call on text that contains no secrets — returns
    the original text with zero redactions. The function applies patterns
    in registration order; the JWT pattern intentionally fires before the
    bearer pattern so a `Bearer eyJ...` header gets the more specific
    redaction label first.
    """
    if not text:
        return RedactionResult(redacted=text, redactions_by_pattern={})

    redactions: dict[str, int] = {}
    redacted = text
    for name, pattern, replacement in SECRET_PATTERNS:
        redacted, count = pattern.subn(replacement, redacted)
        if count > 0:
            redactions[name] = count
    return RedactionResult(redacted=redacted, redactions_by_pattern=redactions)


def redact_dict(data: dict[str, Any]) -> tuple[dict[str, Any], dict[str, int]]:
    """Recursively redact secrets in a dict's string values.

    Used by the audit-log writer: an audit entry's `conclusion` field can
    contain stdout snippets quoted by Opus. We walk every string value
    and apply redact_secrets.

    Non-string scalars (ints, bools, None) and nested structures are
    walked but not modified except where they contain strings.

    Returns a fresh dict (does NOT mutate the input) plus aggregate
    redaction counts across all values.
    """
    counts: dict[str, int] = {}

    def _walk(value: Any) -> Any:
        if isinstance(value, str):
            r = redact_secrets(value)
            for k, v in r.redactions_by_pattern.items():
                counts[k] = counts.get(k, 0) + v
            return r.redacted
        if isinstance(value, dict):
            return {k: _walk(v) for k, v in value.items()}
        if isinstance(value, list):
            return [_walk(v) for v in value]
        if isinstance(value, tuple):
            return tuple(_walk(v) for v in value)
        return value

    return _walk(data), counts
