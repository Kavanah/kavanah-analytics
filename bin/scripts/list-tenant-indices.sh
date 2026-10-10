#!/bin/bash
# Reverse-lookup table mapping each tenant DATA index back to its
# human-readable team / org / workspace.
#
# After, tenant data indices are named {team_esid}__{source} — opaque
# in `_cat/indices`. This is the day-to-day debugging view that turns those
# ESID names back into team/org/workspace titles. It also understands the OLD
# {slug}_{source} names (best-effort reverse-sanitize) so it stays useful
# during the migration window when both name styles coexist.
#
# LEFT JOIN semantics: every data index prints. An index whose team doc is
# missing/deleted, or a team with no org/workspace, still prints with `—` in
# the unresolved columns — orphan indices are exactly what you debug here.
#
# Columns: WORKSPACE | ORG | TEAM | SOURCE | INDEX | DOCS | SIZE
#
# Usage:
#   bash list-tenant-indices.sh [--workspace W] [--org O] [--team T]
#   (filters match on the resolved human-readable title, case-insensitive substring)
#
# Runs the join inside imports_container (no curl there; reuses es_helpers).

set -euo pipefail

CONTAINER="${IMPORTS_CONTAINER:-imports_container}"

FILTER_WORKSPACE=""
FILTER_ORG=""
FILTER_TEAM=""
while [ $# -gt 0 ]; do
    case "$1" in
        --workspace) FILTER_WORKSPACE="${2:-}"; shift 2 ;;
        --org)       FILTER_ORG="${2:-}"; shift 2 ;;
        --team)      FILTER_TEAM="${2:-}"; shift 2 ;;
        -h|--help)
            grep '^#' "$0" | sed 's/^# \{0,1\}//'
            exit 0 ;;
        *) echo "Unknown argument: $1" >&2; exit 1 ;;
    esac
done

# Run the reverse-lookup join inside imports_container (no curl there; reuses
# es_helpers). invoke the module entry point rather than piping a
# Python heredoc on stdin — some terminals mangle heredoc bodies, leaving the
# interpreter with empty stdin (and thus no output). `python3 -m` has no such
# fragility. The join + render live in lib/tenant_index_view.py (unit-tested).
ARGS=()
[ -n "$FILTER_WORKSPACE" ] && ARGS+=(--workspace "$FILTER_WORKSPACE")
[ -n "$FILTER_ORG" ] && ARGS+=(--org "$FILTER_ORG")
[ -n "$FILTER_TEAM" ] && ARGS+=(--team "$FILTER_TEAM")

# -w /app/python so `-m lib.tenant_index_view` resolves (lib/ is under
# /app/python; matches how import.py and the deployment scripts run).
docker exec -w /app/python "$CONTAINER" python3 -m lib.tenant_index_view "${ARGS[@]}"
