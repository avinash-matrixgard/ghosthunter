#!/usr/bin/env bash
#
# One-time setup: point this clone's git at the tracked `.githooks/`
# directory so the CLAUDE.md-size pre-commit check runs automatically.
#
# Rerun this script after cloning. It only touches this repo's git
# config — no global changes.

set -euo pipefail

cd "$(git rev-parse --show-toplevel)"

# Ensure the hook scripts are executable (git doesn't track the bit
# reliably across Windows / filesystems).
chmod +x .githooks/*

git config --local core.hooksPath .githooks

echo "✓ hooks path set to .githooks (local to this clone)"
echo "  enabled: $(ls .githooks | tr '\n' ' ')"
echo ""
echo "  To disable a single commit's checks: git commit --no-verify"
echo "  To uninstall completely:             git config --unset core.hooksPath"
