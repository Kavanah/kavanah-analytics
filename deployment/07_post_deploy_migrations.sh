#!/usr/bin/env bash
# =============================================================================
# 07_post_deploy_migrations.sh — One-shot data migrations after a deploy
# -----------------------------------------------------------------------------
# Runs idempotent ES data migrations inside the imports_container. Safe to
# re-run; each migration is responsible for its own no-op behaviour when
# already applied.
#
# Currently runs:

#   - cleanup_no_data_markers for every platform that writes markers
#       (facebook ads, facebook pages, instagram, google_ads, google_analytics,
#        youtube, chatwoot — Echo doesn't write markers).
#     Dedupes legacy auto-ID markers down to canonical-ID docs, then removes
#     zombie markers (markers that coexist with real data for the same
#     team/date pair).
#

#   - migration/backfill_echo_kibana_date — converts Echo's kibana_date from
#     the free-text English-month form ("April 27, 2026 05:23:46 UTC") to
#     ISO yyyy-MM-dd in every *_echo_conversations index. Idempotent.
#
# NOT run here (operator-invoked, destructive):
#   - migration/audit_legacy_text_mappings — surveys text-mapped indices that
#     should be keyword-mapped per the unified schema. Run manually before
# promoting to a fresh environment. See
# bin/python/import_scripts/migration/MIGRATION_GUIDE.md → "
#     follow-up: legacy text-mapping audit".
#
# Flags:
#   --dry-run      — pass --dry_run to the cleanup; no writes.
#   -h, --help     — show usage
# =============================================================================

set -euo pipefail

FLASK_CONTAINER="imports_container"

RED='\033[0;31m'; GREEN='\033[0;32m'; YELLOW='\033[1;33m'; CYAN='\033[0;36m'; NC='\033[0m'
info()    { echo -e "${CYAN}[INFO]${NC}  $*"; }
success() { echo -e "${GREEN}[OK]${NC}    $*"; }
warn()    { echo -e "${YELLOW}[WARN]${NC}  $*"; }
error()   { echo -e "${RED}[ERROR]${NC} $*" >&2; exit 1; }

usage() {
    sed -n '2,/^# ===/p' "$0" | sed 's/^# \{0,1\}//'
    exit "${1:-0}"
}

DRY_RUN_FLAG=""
DRY_RUN_KV_FLAG=""
while [[ $# -gt 0 ]]; do
    case "$1" in
        --dry-run)    DRY_RUN_FLAG="--dry_run"; DRY_RUN_KV_FLAG="--dry-run=true"; shift ;;
        -h|--help)    usage 0 ;;
        *)            error "Unknown argument: $1" ;;
    esac
done

docker ps --format '{{.Names}}' | grep -qw "$FLASK_CONTAINER" \
    || error "Container '$FLASK_CONTAINER' is not running. Run 02_deploy_stack.sh first."

run_cleanup() {
    local source="$1" action="$2"
    info "Cleanup: $source $action"
    if ! docker exec -w /app/python "$FLASK_CONTAINER" \
        python3 import.py "$source" "$action" $DRY_RUN_FLAG; then
        warn "$source $action exited non-zero — continuing"
    fi
}

run_migration() {
    local source="$1" action="$2"
    info "Migration: $source $action"
    if ! docker exec -w /app/python "$FLASK_CONTAINER" \
        python3 import.py "$source" "$action" $DRY_RUN_KV_FLAG; then
        warn "$source $action exited non-zero — continuing"
    fi
}

info "Running  no_data marker migrations${DRY_RUN_FLAG:+ (dry run)}..."
run_cleanup facebook         cleanup_no_data_markers
run_cleanup facebook         cleanup_no_data_markers_pages
run_cleanup instagram        cleanup_no_data_markers
run_cleanup google_ads       cleanup_no_data_markers
run_cleanup google_analytics cleanup_no_data_markers
run_cleanup youtube          cleanup_no_data_markers
run_cleanup chatwoot         cleanup_no_data_markers

info "Running  echo kibana_date backfill${DRY_RUN_FLAG:+ (dry run)}..."
run_migration migration backfill_echo_kibana_date

success "Post-deploy migrations complete."
