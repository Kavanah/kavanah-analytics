#!/usr/bin/env bash
# =============================================================================
# 08_post_deploy.sh — One-shot manual migrations for the
# ship.
# -----------------------------------------------------------------------------
# Runs the three operator-invoked migrations in the correct order and reports
# what each one finds / changes. Default mode is dry-run; --apply makes the
# destructive operations actually happen, each one gated behind a snapshot.
#
# What this runs
# --------------
# 1. audit_legacy_text_mappings
#       Surveys *_<suffix> indices for keyword-vs-text drift. With --apply,
#       takes a snapshot to --repo, then deletes broken indices. The next
#       import recreates them via _ensure_index_with_schema with the right
#       keyword/date mappings from the start.
#
# 2. cleanup_leaked_fb_insights
#       Surveys *_fb_insights and *_fb_page_insights for docs whose page-id
#       field is not in the index-owning team's current
#       team_facebook_page_id list. With --apply, takes a snapshot, then
#       deletes the leak docs.
#
# 3. Reminder for prewarm cron removal
#       The standalone prewarm_city_geocache cron at 45 7 * * * is folded
# into run_daily_imports and should be removed from sudo
#       crontab. This script does NOT touch the host crontab — it just
#       prints the line to remove.
#
# What this does NOT run
# ----------------------
# - 02_deploy_stack.sh — the standard deploy script that runs marker
#   cleanup automatically. Run that first via your normal deploy flow.
# - Re-imports for indices deleted in step 1 — run manually after this
# script finishes (--team_ids takes the team ESID, not its title;
#   prefix FB_IMPORT_ALLOW_TITLE_MATCH=1 for the legacy exact-title escape hatch):
#       docker exec imports_container python3 /app/python/import.py \
#           facebook run_daily_imports --start_date <D> --end_date <D> \
#           --team_ids <TEAM_ESID>
#
# Flags
# -----
#   --apply        — actually delete docs / indices (each gated behind a
#                    snapshot). Default is dry-run.
#   --repo NAME    — snapshot repository to use. Default: daily_repo
#                    (prod). Use migration_repo on staging/dev.
#   -h, --help     — show usage
# =============================================================================

set -euo pipefail

FLASK_CONTAINER="imports_container"

RED='\033[0;31m'; GREEN='\033[0;32m'; YELLOW='\033[1;33m'; CYAN='\033[0;36m'; NC='\033[0m'
info()    { echo -e "${CYAN}[INFO]${NC}  $*"; }
success() { echo -e "${GREEN}[OK]${NC}    $*"; }
warn()    { echo -e "${YELLOW}[WARN]${NC}  $*"; }
error()   { echo -e "${RED}[ERROR]${NC} $*" >&2; exit 1; }
heading() { echo; echo -e "${CYAN}═══ $* ═══${NC}"; }

usage() {
    sed -n '2,/^# ===/p' "$0" | sed 's/^# \{0,1\}//'
    exit "${1:-0}"
}

APPLY=false
REPO="daily_repo"
while [[ $# -gt 0 ]]; do
    case "$1" in
        --apply)      APPLY=true; shift ;;
        --repo)       REPO="$2"; shift 2 ;;
        -h|--help)    usage 0 ;;
        *)            error "Unknown argument: $1" ;;
    esac
done

docker ps --format '{{.Names}}' | grep -qw "$FLASK_CONTAINER" \
    || error "Container '$FLASK_CONTAINER' is not running. Run 02_deploy_stack.sh first."

if [[ "$APPLY" == true ]]; then
    info "Mode: APPLY (destructive operations enabled, snapshots to '$REPO')"
else
    info "Mode: DRY-RUN (no writes — pass --apply to actually run destructive ops)"
fi

# ── Step 1: legacy text-mapping audit ────────────────────────────────────────
heading "Step 1/3: audit_legacy_text_mappings"
if [[ "$APPLY" == true ]]; then
    docker exec -w /app/python "$FLASK_CONTAINER" \
        python3 import.py migration audit_legacy_text_mappings \
        --apply --repo "$REPO" \
        || warn "audit_legacy_text_mappings exited non-zero — review log and decide before continuing"
else
    docker exec -w /app/python "$FLASK_CONTAINER" \
        python3 import.py migration audit_legacy_text_mappings \
        || warn "audit_legacy_text_mappings exited non-zero"
fi

# ── Step 2: FB insight leak cleanup ──────────────────────────────────────────
heading "Step 2/3: cleanup_leaked_fb_insights"
if [[ "$APPLY" == true ]]; then
    docker exec -w /app/python "$FLASK_CONTAINER" \
        python3 import.py migration cleanup_leaked_fb_insights \
        --apply --repo "$REPO" \
        || warn "cleanup_leaked_fb_insights exited non-zero — review log and decide before continuing"
else
    docker exec -w /app/python "$FLASK_CONTAINER" \
        python3 import.py migration cleanup_leaked_fb_insights \
        || warn "cleanup_leaked_fb_insights exited non-zero"
fi

# ── Step 3: prewarm cron reminder ────────────────────────────────────────────
heading "Step 3/3: prewarm_city_geocache cron removal — manual"
echo
echo "folds prewarm_city_geocache into run_daily_imports. The"
echo "standalone cron at '45 7 * * *' is now redundant. To remove it:"
echo
echo "    sudo crontab -l            # confirm the line exists"
echo "    sudo crontab -e            # remove the prewarm_city_geocache line"
echo
warn "This script does NOT modify the host crontab. Remove the line manually."

# ── Wrap up ──────────────────────────────────────────────────────────────────
heading "Done"
if [[ "$APPLY" == true ]]; then
    success "Migrations applied. Snapshots are in '$REPO'."
    echo
    echo "Next steps:"
    echo "  - Re-run imports for any indices step 1 deleted, e.g.:"
    echo "      docker exec $FLASK_CONTAINER python3 /app/python/import.py \\"
    echo "          facebook run_daily_imports --start_date <D> --end_date <D> --team_ids <TEAM_ESID>"
    echo "    (--team_ids takes the team ESID, not its title. To scope by"
    echo "     title instead, prefix FB_IMPORT_ALLOW_TITLE_MATCH=1 — it does an EXACT,"
    echo "     case-insensitive title match and can still collide on same-title teams.)"
    echo "  - Remove the prewarm cron (step 3 above)."
    echo "  - Verify dashboards show clean data for affected teams."
else
    success "Dry-run complete. Re-run with --apply to actually make changes."
    echo "      bash $0 --apply --repo $REPO"
fi
