#!/usr/bin/env bash
# =============================================================================
# | 04_seed_test_data.sh — Seed Elasticsearch with test data
# -----------------------------------------------------------------------------
# Wrapper around bin/python/seed_test_data.py. Meant to run on the host where
# imports_container is already up (run after 02_deploy_stack.sh).
#
# Indices seeded:
#   test_fb_insights, test_ga_metrics, test_echo_conversations,
#   test_ig_insights, test_fb_page_insights, test_youtube_analytics
#
# Idempotent:
#   - By default, skips any index that already contains documents.
#   - With --force, deletes every test_* index listed above and re-seeds.
#
# Flags:
#   --force        — delete and recreate all test indices before seeding
#   -h, --help     — show usage
# =============================================================================

set -euo pipefail

# ── Config ────────────────────────────────────────────────────────────────────
REPO_DIR="${REPO_DIR:-$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)}"
MASTER_CONFIG="${MASTER_CONFIG:-$REPO_DIR/config/master-config.yaml}"
FLASK_CONTAINER="${FLASK_CONTAINER:-imports_container}"
SEED_SCRIPT="/app/python/seed_test_data.py"
TEST_INDICES=(
    test_fb_insights
    test_ga_metrics
    test_echo_conversations
    test_ig_insights
    test_fb_page_insights
    test_youtube_analytics
)

# ── Colour helpers ────────────────────────────────────────────────────────────
RED='\033[0;31m'; GREEN='\033[0;32m'; YELLOW='\033[1;33m'; CYAN='\033[0;36m'; NC='\033[0m'
info()    { echo -e "${CYAN}[INFO]${NC}  $*"; }
success() { echo -e "${GREEN}[OK]${NC}    $*"; }
warn()    { echo -e "${YELLOW}[WARN]${NC}  $*"; }
error()   { echo -e "${RED}[ERROR]${NC} $*" >&2; exit 1; }

usage() {
    sed -n '2,/^# ===/p' "$0" | sed 's/^# \{0,1\}//'
    exit "${1:-0}"
}

# ── Arg parsing ───────────────────────────────────────────────────────────────
FORCE=false
for arg in "$@"; do
    case "$arg" in
        --force)     FORCE=true ;;
        -h|--help)   usage 0 ;;
        *)           error "Unknown argument: $arg" ;;
    esac
done

# ── Preflight ─────────────────────────────────────────────────────────────────
command -v docker >/dev/null 2>&1 || error "Docker is not installed."
command -v curl   >/dev/null 2>&1 || error "curl is not installed."
command -v yq     >/dev/null 2>&1 || error "yq is required (snap install yq)."
[[ -f "$MASTER_CONFIG" ]] || error "Master config missing: $MASTER_CONFIG"
docker ps --format '{{.Names}}' | grep -qw "$FLASK_CONTAINER" \
    || error "Container '$FLASK_CONTAINER' is not running. Run 02_deploy_stack.sh first."

ELASTIC_PASS=$(yq e '.elk-01.es01.admin_pass' "$MASTER_CONFIG")
[[ -n "$ELASTIC_PASS" && "$ELASTIC_PASS" != "null" ]] \
    || error "elk-01.es01.admin_pass is missing from $MASTER_CONFIG"

info "| 04_seed_test_data.sh"
info "Container: $FLASK_CONTAINER"
info "Mode:      $([[ "$FORCE" == true ]] && echo 'force (delete + reseed)' || echo 'idempotent (skip populated indices)')"

# ── Helpers ───────────────────────────────────────────────────────────────────
index_doc_count() {
    local idx="$1"
    curl -sk -u "elastic:${ELASTIC_PASS}" \
        "https://localhost:9200/${idx}/_count" 2>/dev/null \
        | grep -o '"count":[0-9]*' | head -1 | cut -d: -f2 || echo ""
}

index_exists() {
    local idx="$1"
    local code
    # curl -w prints "000" on connect failure; the `|| echo` fallback would
    # double-append so we rely on curl's own output.
    code=$(curl -sk -o /dev/null -w "%{http_code}" -u "elastic:${ELASTIC_PASS}" \
        "https://localhost:9200/${idx}" 2>/dev/null)
    [[ "$code" == "200" ]]
}

delete_index() {
    local idx="$1"
    curl -sk -o /dev/null -u "elastic:${ELASTIC_PASS}" -X DELETE \
        "https://localhost:9200/${idx}" 2>/dev/null || true
}

# ── Decide whether to seed ────────────────────────────────────────────────────
if [[ "$FORCE" == true ]]; then
    info "Force-deleting existing test indices..."
    for idx in "${TEST_INDICES[@]}"; do
        if index_exists "$idx"; then
            delete_index "$idx"
            info "  deleted $idx"
        fi
    done
else
    # If every listed index already has at least one document, skip seeding.
    all_populated=true
    for idx in "${TEST_INDICES[@]}"; do
        count=$(index_doc_count "$idx")
        if [[ -z "$count" || "$count" == "0" ]]; then
            all_populated=false
            break
        fi
    done
    if [[ "$all_populated" == true ]]; then
        success "All ${#TEST_INDICES[@]} test indices already populated. Nothing to do."
        info "Re-run with --force to wipe and reseed."
        exit 0
    fi
fi

# ── Run the seed script inside the container ──────────────────────────────────
info "Running $SEED_SCRIPT in $FLASK_CONTAINER..."
docker exec "$FLASK_CONTAINER" python3 "$SEED_SCRIPT"

# ── Summary ───────────────────────────────────────────────────────────────────
echo
info "Post-seed doc counts:"
for idx in "${TEST_INDICES[@]}"; do
    count=$(index_doc_count "$idx")
    printf '  %-35s docs: %s\n' "$idx" "${count:-?}"
done
echo
success "Test data seeding complete."
