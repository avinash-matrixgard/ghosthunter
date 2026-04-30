"""Tests for the secrets redactor (ghosthunter#3, v1.0.7).

Covers all 8 credential pattern classes with positive (must redact)
and negative (must NOT redact) cases. False negatives are unacceptable —
a leaked credential persists on disk forever and can land in backups.
False positives are tolerable; over-redaction in audit logs is fine.

Also covers the recursive ``redact_dict`` helper used by the audit-log
writer to walk nested entry structures.
"""

from __future__ import annotations

from ghosthunter.security.secrets_redactor import (
    SECRET_PATTERNS,
    redact_dict,
    redact_secrets,
)


# ---------------------------------------------------------------------------
# Positive cases — each pattern must fire
# ---------------------------------------------------------------------------
class TestAWSKeys:
    def test_redacts_aws_access_key(self):
        out = redact_secrets("found AKIAIOSFODNN7EXAMPLE in the env dump")
        assert "AKIA" not in out.redacted
        assert "[REDACTED:aws_access_key]" in out.redacted
        assert out.redactions_by_pattern.get("aws_access_key") == 1

    def test_redacts_aws_temp_access_key(self):
        out = redact_secrets("session token: ASIAY34FZKBOKMUTVV7A")
        assert "ASIA" not in out.redacted
        assert "[REDACTED:aws_temp_access_key]" in out.redacted

    def test_does_not_redact_short_akia_substring(self):
        # Less than the 16-char body — not a real key.
        out = redact_secrets("AKIASHORT")
        assert out.redacted == "AKIASHORT"
        assert not out.had_redactions


class TestGitHubTokens:
    def test_redacts_github_personal_access_token(self):
        token = "ghp_" + "A" * 36
        out = redact_secrets(f"GITHUB_TOKEN={token}")
        assert "ghp_" not in out.redacted or "[REDACTED:github_token]" in out.redacted
        assert out.redactions_by_pattern.get("github_token") == 1

    def test_redacts_github_server_token(self):
        token = "ghs_" + "B" * 36
        out = redact_secrets(f"server token {token}")
        assert "[REDACTED:github_token]" in out.redacted


class TestAnthropicKeys:
    def test_redacts_anthropic_api_key(self):
        # Realistic anthropic key shape: sk-ant-<long alphanumeric>
        key = "sk-ant-api03-abc123_def-456GHI789jkl"
        out = redact_secrets(f"ANTHROPIC_API_KEY={key}")
        assert "sk-ant-" not in out.redacted
        assert "[REDACTED:anthropic_key]" in out.redacted


class TestOpenAIKeys:
    def test_redacts_openai_key(self):
        key = "sk-" + "X" * 48
        out = redact_secrets(f"export OPENAI_API_KEY={key}")
        assert "[REDACTED:openai_key]" in out.redacted

    def test_does_not_redact_short_sk_prefix(self):
        # `sk-` followed by < 48 chars — not a real OpenAI key.
        out = redact_secrets("sk-short")
        assert out.redacted == "sk-short"


class TestJWTs:
    def test_redacts_jwt(self):
        jwt = "eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiIxMjM0NTY3ODkwIn0.SflKxwRJSMeKKF2QT4fwpMeJf36POk6yJV_adQssw5c"
        out = redact_secrets(f"Token: {jwt}")
        assert "[REDACTED:jwt]" in out.redacted

    def test_jwt_in_bearer_header_redacts_as_jwt_first(self):
        # JWT pattern intentionally fires before bearer pattern.
        jwt = "eyJhbGc.eyJzdWI.SflKxw"
        out = redact_secrets(f"Authorization: Bearer {jwt}")
        # Either jwt or bearer_token captures it — must not leak.
        assert jwt not in out.redacted


class TestBearerTokens:
    def test_redacts_bearer_token(self):
        out = redact_secrets("Authorization: Bearer abc123def456ghi789xyz")
        assert "abc123def456ghi789xyz" not in out.redacted
        assert "[REDACTED:bearer_token]" in out.redacted

    def test_redacts_lowercase_bearer(self):
        out = redact_secrets("authorization: bearer somelongtoken1234567890")
        assert "[REDACTED:bearer_token]" in out.redacted


class TestAuthHeaders:
    def test_redacts_api_key_header(self):
        out = redact_secrets('headers = {"x-api-key": "abcdef0123456789xyz"}')
        assert "abcdef0123456789xyz" not in out.redacted
        assert "[REDACTED:auth_header]" in out.redacted

    def test_redacts_authorization_assignment(self):
        out = redact_secrets("authorization=longsecrettoken1234567890")
        assert "longsecrettoken" not in out.redacted


class TestPEMPrivateKeys:
    def test_redacts_pem_private_key(self):
        pem = (
            "-----BEGIN RSA PRIVATE KEY-----\n"
            "MIIEpAIBAAKCAQEAfake_key_data_here_for_test_only\n"
            "abcdef0123456789==\n"
            "-----END RSA PRIVATE KEY-----"
        )
        out = redact_secrets(f"private_key was: {pem}")
        assert "MIIEpAIBAAKCAQEA" not in out.redacted
        assert (
            "[REDACTED:pem_private_key]" in out.redacted
            or "[REDACTED:gcp_private_key]" in out.redacted
        )

    def test_redacts_gcp_service_account_private_key(self):
        # A typical GCP service account JSON shape with the private_key field.
        text = (
            '{"type":"service_account","project_id":"prj-x","'
            'private_key":"-----BEGIN PRIVATE KEY-----\\nMIIEvQIBADANBgk\\n'
            '-----END PRIVATE KEY-----\\n","client_email":"sa@prj.iam.gserviceaccount.com"}'
        )
        out = redact_secrets(text)
        assert "MIIEvQIBADAN" not in out.redacted
        # client_email is NOT a secret, should remain.
        assert "sa@prj.iam.gserviceaccount.com" in out.redacted


# ---------------------------------------------------------------------------
# Negative cases — benign content must NOT trigger redaction
# ---------------------------------------------------------------------------
class TestNoFalsePositives:
    def test_empty_string(self):
        out = redact_secrets("")
        assert out.redacted == ""
        assert not out.had_redactions

    def test_normal_billing_csv_row(self):
        text = "2026-04-29,compute.googleapis.com,us-central1,$1234.56,$1100.00\n"
        out = redact_secrets(text)
        assert out.redacted == text
        assert not out.had_redactions

    def test_resource_id_with_alphanumerics(self):
        # GCP resource IDs are long alphanumerics but don't have credential prefixes.
        text = "instance: projects/my-prj/zones/us-central1-a/instances/web-server-001"
        out = redact_secrets(text)
        assert out.redacted == text
        assert not out.had_redactions

    def test_iam_member_email(self):
        text = "iam_member: user:avinash@matrixgard.com"
        out = redact_secrets(text)
        assert out.redacted == text
        assert not out.had_redactions

    def test_uuid_does_not_match(self):
        text = "request_id: 12345678-1234-1234-1234-123456789abc"
        out = redact_secrets(text)
        assert out.redacted == text
        assert not out.had_redactions


# ---------------------------------------------------------------------------
# Multi-redaction
# ---------------------------------------------------------------------------
class TestMultipleSecrets:
    def test_redacts_multiple_distinct_credentials(self):
        text = (
            "AWS_ACCESS_KEY_ID=AKIAIOSFODNN7EXAMPLE\n"
            "GH_TOKEN=ghp_" + "Z" * 36 + "\n"
            "Authorization: Bearer abcdefghijklmn1234567890\n"
        )
        out = redact_secrets(text)
        assert out.total_redactions >= 3
        assert "AKIA" not in out.redacted
        assert "ghp_" not in out.redacted
        assert "abcdefghijklmn1234567890" not in out.redacted


# ---------------------------------------------------------------------------
# redact_dict — the audit-log writer's helper
# ---------------------------------------------------------------------------
class TestRedactDict:
    def test_redacts_string_values(self):
        entry = {
            "timestamp": "2026-04-30T05:00:00",
            "conclusion": "Found AKIAIOSFODNN7EXAMPLE in env dump",
            "succeeded": True,
        }
        out, counts = redact_dict(entry)
        assert "AKIA" not in out["conclusion"]
        assert out["timestamp"] == entry["timestamp"]
        assert out["succeeded"] is True
        assert counts.get("aws_access_key") == 1

    def test_recursive_into_nested_dict(self):
        entry = {
            "extra": {
                "raw_stdout": "Bearer abc123def456ghi789jkl0",
            },
        }
        out, counts = redact_dict(entry)
        assert "abc123def456ghi789jkl0" not in out["extra"]["raw_stdout"]
        assert counts.get("bearer_token") == 1

    def test_recursive_into_list(self):
        entry = {"hypotheses": ["safe one", "AKIAIOSFODNN7EXAMPLE in here"]}
        out, counts = redact_dict(entry)
        assert "AKIA" not in out["hypotheses"][1]
        assert out["hypotheses"][0] == "safe one"

    def test_does_not_mutate_input(self):
        entry = {"conclusion": "AKIAIOSFODNN7EXAMPLE leaked"}
        original_value = entry["conclusion"]
        out, _ = redact_dict(entry)
        assert entry["conclusion"] == original_value  # unchanged
        assert "[REDACTED:" in out["conclusion"]


# ---------------------------------------------------------------------------
# Pattern registry invariants
# ---------------------------------------------------------------------------
class TestPatternRegistry:
    def test_at_least_8_patterns(self):
        # Issue #3 specified 8 pattern classes minimum.
        assert len(SECRET_PATTERNS) >= 8

    def test_all_patterns_named_uniquely(self):
        names = [name for name, _, _ in SECRET_PATTERNS]
        assert len(names) == len(set(names)), "duplicate pattern names"

    def test_all_replacements_have_redacted_marker(self):
        for name, _pattern, replacement in SECRET_PATTERNS:
            assert "REDACTED" in replacement, f"{name} replacement must mention REDACTED"
