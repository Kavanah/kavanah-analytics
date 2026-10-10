#!/usr/bin/env bash
# =============================================================================
# | 03_provision_cron_jobs.sh — Install DEAM cron entries
# -----------------------------------------------------------------------------
# Installs crontab entries for every scheduled job DEAM owns, delimited by
# marker comments so re-running atomically replaces the block (no duplicates).
#
# Covered jobs (every job appends to a named log under /var/log/,
# which replaced the old /dev/null redirects that left silent failures
# untraceable):
# • refresh_connection_tokens — daily (02:30)
#   • generate_demo_data.py for Kavanah Team — daily (non-DT sources); wrapped
# in tools/run_tracked_job.py so its exit code lands in de_job_health
# • seed_kavanah_demo_dt.py --reseed — daily; same wrapper
# • notify_trial_ending.py — daily (06:00)
# • run_report_schedules.py — every 5 minutes
# • youtube_duplicate_check — daily (05:05), tracked in de_job_health
# • tiktok_daily_snapshot — daily (06:20), ONLY while deployment/.env
# has TIKTOK_ENABLED=true; absent/false = no line at all
#
# NOTE: the daily ad-platform imports (facebook, google_ads, google_analytics,
# echo, youtube, chatwoot) and the disciple_tools pull importer — which used to
# run `import.py <connector> run_daily_imports` here — have been migrated to the
# report scheduler and are no longer provisioned as cron lines.
#
# Schedule: jobs run on their own cadences across the night/morning window so
# they don't all contend for API quota and log disk at once (see SCHEDULE).
#
# Logs: /var/log/<slug>.log (2>&1 merged). The script pre-creates each log
# file with sudo + chown $USER so cron can append. A logrotate config
# (deployment/logrotate-deam-imports.conf) is also installed under
# /etc/logrotate.d/deam-imports to cap log growth.
#
# Flags:
#   --uninstall    — remove the DEAM-managed block from crontab (logrotate
#                    config is left in place; remove manually if no longer
#                    needed)
#   --dry-run      — print the generated block instead of installing
#   --check        — compare the INSTALLED block against the one this script
#                    would generate; exit non-zero when it is missing or has
#                    drifted. Read-only. This is what the release wrapper runs
# after provisioning.
#   --skip-logrotate — install crontab only, do not touch /etc/logrotate.d
#   -h, --help     — show usage
#
# RUN IT WITH sudo. The managed block lives
# in ROOT's crontab. `crontab` here is bare, so running as another user does not
# fail — it quietly installs a SECOND managed block in that user's spool, and
# both copies then fire on every schedule. --install and --uninstall therefore
# refuse to run as anyone but root; --dry-run and --check do not (they write
# nothing), but --check reads the invoking user's crontab, so a non-root --check
# is not a statement about root's block and says so.
#
# Idempotent: re-running produces identical crontab output.
#
# Invoked by deployment/start-imports-container.sh on every deploy.
# It used to be a hand-run step named only in the README and the
# runbooks; staging's block was consequently three weeks and three tickets
# behind the script, missing the credential-health probe and both retention
# jobs outright and running the two demo jobs in their earlier untracked
# form.
# =============================================================================

set -euo pipefail

# ── Config ────────────────────────────────────────────────────────────────────
FLASK_CONTAINER="${FLASK_CONTAINER:-imports_container}"
LOG_DIR="${LOG_DIR:-/var/log}"
BEGIN_MARKER="# DEAM-CRON-BEGIN (managed by deployment/03_provision_cron_jobs.sh — do not edit)"
END_MARKER="# DEAM-CRON-END"
LOGROTATE_SRC="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/logrotate-deam-imports.conf"
LOGROTATE_DEST="/etc/logrotate.d/deam-imports"
# gunicorn app logs moved off the container's /tmp onto the
# host-mounted import-data volume, so they now need rotation on the host.
# Separate file from deam-imports because gunicorn holds its logs open and
# requires copytruncate — see the header of the .conf for why that matters.
LOGROTATE_APP_SRC="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/logrotate-deam-app-logs.conf"
LOGROTATE_APP_DEST="/etc/logrotate.d/deam-app-logs"

# Each row is a 5-field tab-separated record:
#   <minute>  <hour>  <dom_dow>  <log_slug>  <command-args-after-docker-exec-CONTAINER>
#
# - <minute>/<hour>/<dom_dow>: cron timing fields. <dom_dow> covers the
#   "day-of-month month day-of-week" triple, so use "* * *" for daily or e.g.
#   "* * 1-5" for weekdays. "*/6" goes in <hour>.
# - <log_slug>: filename under $LOG_DIR (no extension). The full log path is
#   "$LOG_DIR/<log_slug>.log". Slugs preserve the existing prod naming for
# the imports — log filenames did not change.
# - <command>: everything after `docker exec $FLASK_CONTAINER `. Quote args
#   with spaces using \" so they survive bash word-splitting in build_block.
SCHEDULE=(
    # NOTE: the daily ad-platform imports (echo, facebook, google_ads, youtube,
    # google_analytics, chatwoot) and the disciple_tools pull importer — all of
    # which ran `import.py <connector> run_daily_imports` here — have been
    # migrated to the report scheduler and
    # are no longer provisioned as standalone cron lines.

    # wizard-cohort de_connection token refresh. 02:30 daily,
    # well away from the 07:00 import window so a refresh failure has
    # overnight runway to surface before the morning cron tries to use
    # the token.
    $'30\t02\t* * *\trefresh_connection_tokens\tpython3 /app/python/import.py facebook refresh_connection_tokens'

    # Daily demo data for Kavanah Team — 05:30 server-local. --dt-days
    # dropped (deprecated no-op); DT demo data moved to the 05:40 seeder job.
    # both demo jobs run through tools/run_tracked_job.py, which
    # streams the job's output into the same log and then records the exit
    # code + last output line in de_job_health (Status Reports page and the
    # superuser banner read it). The 05:40 reseed exited 1 for five days
    # (2026-09-02..07) with nothing but this log file knowing. --job MUST equal
    # the log slug; the job's own command follows `--` verbatim.
    $'30\t05\t* * *\tkavanah_demo_data\tpython3 /app/python/tools/run_tracked_job.py --job kavanah_demo_data -- python3 /app/python/import_scripts/migration/generate_demo_data.py --team \"Kavanah Team\" --org \"Kavanah\" --days 1'

    # daily demo Disciple Tools rebuild. Replaces the orphaned
    # generate_demo_data → disciple-tools-staging → promote path (promote cron
    # retired at the cutover). --reseed rebuilds the curated
    # kavanah_team_disciple_tools index with its trailing-90-day window anchored
    # at today, so the exact demo totals never age out of the report's range.
    $'40\t05\t* * *\tkavanah_demo_dt_reseed\tpython3 /app/python/tools/run_tracked_job.py --job kavanah_demo_dt_reseed -- python3 /app/python/scripts/seed_kavanah_demo_dt.py --reseed'

    # "trial ending soon" billing email. 06:00 daily (after the
    # demo-data job, before the 07:00 import window). Scans trialing users
    # and emails each once when their trial ends within 3 days; the
    # trial_ending_email_sent_at stamp prevents re-sends on later runs.
    $'00\t06\t* * *\ttrial_ending_email\tpython3 /app/python/asset_management/jobs/notify_trial_ending.py --days 3'

    # the nightly TikTok snapshot is NOT listed here: it is appended
    # below only while the TikTok switch is on.

    # report-scheduler poll-runner. Every 5 minutes: execute active
    # schedules whose next_run has passed (Option A). State lives in ES so the
    # runner is stateless; per-schedule isolation means one bad schedule never
    # sinks the batch. P1 writes stub snapshots; email delivery is a later phase.
    $'*/5\t*\t* * *\treport_scheduler\tpython3 /app/python/asset_management/jobs/run_report_schedules.py'

    # deferred-build sweep, every 1 minute. run_now defers the snapshot
    # build (phase=importing) and relied on either the builder tab polling
    # import-status (which it doesn't during the importing phase — the bug)
    # or run_due's 5-min sweep. This dedicated high-cadence sweep finalizes a
    # finished run within ~1 min regardless of the browser. Build-only (no due
    # dispatch), so it's safe to run far more often than run_due — no import re-fire.
    $'*/1\t*\t* * *\tdeferred_build_sweep\tpython3 /app/python/asset_management/jobs/sweep_deferred_builds.py'

    # read-only credential validity sweep, every 6 hours at :15 (so it
    # never collides with the 02:30 token refresh or the 07:00 import window).
    #
    # Detects credential death WITHOUT waiting for an import to fail. The signal
    # that matters is data_access_expires_at: Meta's stored tokens report no
    # expires_at at all, so the 02:30 refresh job is a no-op on every row, and the
    # 90-day data-access window is the only live clock — previously unwatched by
    # anything. Walks BOTH credential families (de_asset_fb_app_users, which every
    # ds_facebook team imports through, and the de_connection wizard cohort).
    #
    # Writes only to de_credential_health. Stamps a heartbeat each run; check it
    # with `--check-heartbeat` (CLAUDE.md #24 — a worker with no canary can stop
    # silently, and "no errors" then reads as "healthy").
    $'15\t*/6\t* * *\tcredential_health_probe\tpython3 /app/python/asset_management/jobs/probe_credential_health.py'

    # conversation retention sweep, 04:30 daily (after the 03:00
    # ELK backups, before the 07:00 import window). Rolls up every month of
    # Echo / Chatwoot conversations older than 24 months into
    # {team_esid}__conversation_rollups, deletes the raw docs, purges raw JSON
    # already indexed for 7+ days and reports the SLM snapshot retention.
    #
    # GATE: provisioned WITHOUT --apply on purpose — it reports what it WOULD
    # roll up and delete and writes nothing to data indices or disk. Add
    # --apply only after the Summary Report has been re-seeded to read the
    # rollup source and the first dry-run report has been
    # reviewed. Heartbeat: --check-heartbeat.
    $'30\t04\t* * *\tretention_sweep\tpython3 /app/python/asset_management/jobs/retention_sweep.py'

    # Disciple.Tools reconciliation, Sundays 04:45. Lists every
    # post id per site and removes ES docs the upstream no longer has (the
    # importer never tombstones). Same dry-run gate as the sweep above.
    $'45\t04\t* * 0\tretention_dt_reconcile\tpython3 /app/python/asset_management/jobs/retention_sweep.py --only-dt'

    # YouTube duplicate-channel check, 05:05 daily. Read-only and
    # ES-only (no Google calls, no quota). Fails when a youtube_channel_id is held
    # by more than one de_asset_youtube_channels record, the reconnect debris the
    # wizard no longer creates. Tracked, so a duplicate reaches the
    # superuser banner and Status Reports, nobody else. Fix with
    # tools.dedupe_youtube_channels (dry-run first).
    $'05\t05\t* * *\tyoutube_duplicate_check\tpython3 /app/python/tools/run_tracked_job.py --job youtube_duplicate_check -- python3 /app/python/tools/audit_youtube_channel_links.py --duplicates-only'
)

# ── Colour helpers ────────────────────────────────────────────────────────────
RED='\033[0;31m'; GREEN='\033[0;32m'; YELLOW='\033[1;33m'; CYAN='\033[0;36m'; NC='\033[0m'
info()    { echo -e "${CYAN}[INFO]${NC}  $*"; }
success() { echo -e "${GREEN}[OK]${NC}    $*"; }
warn()    { echo -e "${YELLOW}[WARN]${NC}  $*"; }
error()   { echo -e "${RED}[ERROR]${NC} $*" >&2; exit 1; }

# ── Switched jobs ──────────────────────────────────────────────────
# nightly TikTok organic snapshot, 06:20 (before the 07:00 import
# window). The one importer still on cron, on purpose: TikTok's
# Display API has lifetime totals only, so a missed day can never be
# backfilled and must not depend on someone creating a schedule. Covers every
# team with ds_tiktok on and an account linked; tracked so a failed
# or missing night reaches Status Reports and the superuser banner.
TIKTOK_ROW=$'20\t06\t* * *\ttiktok_daily_snapshot\tpython3 /app/python/tools/run_tracked_job.py --job tiktok_daily_snapshot -- python3 /app/python/import.py tiktok run_all_teams'

# The switch is read from deployment/.env — the same line compose passes into
# imports_container as TIKTOK_ENABLED — so the crontab and the app (job-health
# expectations, tile, routes) can never disagree about it. Not from this
# process's environment: under sudo that is reset, and a second source is how
# the two would drift. Off when the file or the line is missing. ENV_FILE is
# overridable for tests only.
ENV_FILE="${ENV_FILE:-$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/.env}"
tiktok_switch_on() {
    [[ -r "$ENV_FILE" ]] || return 1
    local value
    value="$(grep -E '^TIKTOK_ENABLED=' "$ENV_FILE" | tail -n 1 | cut -d= -f2- | tr -d "\"' \r" | tr '[:upper:]' '[:lower:]')" || true
    [[ "$value" == "1" || "$value" == "true" || "$value" == "yes" || "$value" == "on" ]]
}
if tiktok_switch_on; then
    SCHEDULE+=("$TIKTOK_ROW")
fi

usage() {
    sed -n '2,/^# ===/p' "$0" | sed 's/^# \{0,1\}//'
    exit "${1:-0}"
}

# ── Arg parsing ───────────────────────────────────────────────────────────────
MODE="install"
SKIP_LOGROTATE=0
for arg in "$@"; do
    case "$arg" in
        --uninstall)       MODE="uninstall" ;;
        --dry-run)         MODE="dry-run" ;;
        --check)           MODE="check" ;;
        --skip-logrotate)  SKIP_LOGROTATE=1 ;;
        -h|--help)         usage 0 ;;
        *)                 error "Unknown argument: $arg" ;;
    esac
done

# ── Preflight ─────────────────────────────────────────────────────────────────
command -v crontab >/dev/null 2>&1 || error "crontab is not installed."

# the block belongs to root and `crontab` is bare, so a
# non-root run installs a parallel block in that user's spool instead of failing.
# Both then fire. Only the modes that WRITE are refused; --dry-run and --check
# are read-only and stay available to anyone.
if [[ "$MODE" == "install" || "$MODE" == "uninstall" ]] && (( EUID != 0 )); then
    error "$MODE must run as root: sudo bash ${BASH_SOURCE[0]} $*
          The DEAM block lives in root's crontab. Running as $(id -un) would install a
          SECOND managed block in that user's spool and every job would fire twice."
fi

# ── Build the managed block ───────────────────────────────────────────────────
# Each SCHEDULE row is tab-separated (5 fields):
#   <minute>\t<hour>\t<dom_dow>\t<log_slug>\t<command-args>
# IFS=$'\t' makes `read` split on tabs only, so spaces inside <command-args>
# survive intact (e.g. quoted "Kavanah Team").
build_block() {
    printf '%s\n' "$BEGIN_MARKER"
    local minute hour dom_dow log_slug cmd
    for row in "${SCHEDULE[@]}"; do
        IFS=$'\t' read -r minute hour dom_dow log_slug cmd <<<"$row"
        local log="$LOG_DIR/${log_slug}.log"
        printf '%s %s %s docker exec %s %s >> %s 2>&1\n' \
            "$minute" "$hour" "$dom_dow" "$FLASK_CONTAINER" "$cmd" "$log"
    done
    printf '%s\n' "$END_MARKER"
}

# Return the unique set of log slugs referenced by SCHEDULE — used to pre-touch
# log files with the right ownership before cron first runs.
schedule_log_slugs() {
    local minute hour dom_dow log_slug cmd
    for row in "${SCHEDULE[@]}"; do
        IFS=$'\t' read -r minute hour dom_dow log_slug cmd <<<"$row"
        printf '%s\n' "$log_slug"
    done | sort -u
}

install_logrotate() {
    if (( SKIP_LOGROTATE )); then
        warn "Skipping logrotate install (--skip-logrotate)."
        return 0
    fi
    if [[ ! -f "$LOGROTATE_SRC" ]]; then
        warn "Logrotate source not found at $LOGROTATE_SRC — skipping."
        return 0
    fi
    if ! command -v sudo >/dev/null 2>&1; then
        warn "sudo unavailable — cannot install $LOGROTATE_DEST. Skipping."
        return 0
    fi
    info "Installing logrotate config → $LOGROTATE_DEST"
    sudo install -o root -g root -m 0644 "$LOGROTATE_SRC" "$LOGROTATE_DEST"
    success "Logrotate config installed."

    # app-log rotation. Same guards as above; missing source is a
    # warning, not a failure, so an older checkout still provisions cleanly.
    if [[ -f "$LOGROTATE_APP_SRC" ]]; then
        info "Installing app-log logrotate config → $LOGROTATE_APP_DEST"
        sudo install -o root -g root -m 0644 "$LOGROTATE_APP_SRC" "$LOGROTATE_APP_DEST"
        success "App-log logrotate config installed."
    else
        warn "App-log logrotate source not found at $LOGROTATE_APP_SRC — skipping."
    fi
}

# ── Read current crontab and strip any existing managed block ─────────────────
current_crontab() {
    crontab -l 2>/dev/null || true
}

strip_managed_block() {
    # Delete the managed block (inclusive of both markers). `awk` is POSIX-safe
    # and does not rely on GNU sed. `gsub` escapes nothing here — markers are
    # fixed literal strings.
    awk -v begin="$BEGIN_MARKER" -v end="$END_MARKER" '
        $0 == begin { inside=1; next }
        $0 == end   { inside=0; next }
        !inside     { print }
    '
}

extract_managed_block() {
    # The inverse of strip_managed_block: print the managed block only, markers
    # included, so it can be compared verbatim with build_block's output.
    awk -v begin="$BEGIN_MARKER" -v end="$END_MARKER" '
        $0 == begin { inside=1 }
        inside      { print }
        $0 == end   { inside=0 }
    '
}

NEW_BLOCK=$(build_block)

case "$MODE" in
    check)
        # Read-only drift check. Provisioning is idempotent, so the
        # only thing worth asserting after an install is that the block the
        # script generates is the block the machine actually holds.
        if (( EUID != 0 )); then
            warn "Not root — reading $(id -un)'s crontab, not root's. The managed block lives in"
            warn "root's crontab, so this result says nothing about it. Re-run with sudo."
        fi
        INSTALLED=$(current_crontab | extract_managed_block)
        if [[ -z "$INSTALLED" ]]; then
            error "No DEAM-managed cron block in $(id -un)'s crontab — it has never been provisioned here.
          Fix: sudo bash ${BASH_SOURCE[0]}"
        fi
        if [[ "$INSTALLED" == "$NEW_BLOCK" ]]; then
            success "Installed cron block matches this script ($(grep -c 'docker exec' <<<"$NEW_BLOCK") jobs)."
            exit 0
        fi
        warn "DRIFT — the installed block differs from the one this script generates:"
        diff <(printf '%s\n' "$INSTALLED") <(printf '%s\n' "$NEW_BLOCK") || true
        error "Installed cron block has DRIFTED from deployment/03_provision_cron_jobs.sh.
          Fix: sudo bash ${BASH_SOURCE[0]}"
        ;;
    dry-run)
        info "Dry run — would install the following block:"
        echo
        echo "$NEW_BLOCK"
        exit 0
        ;;
    uninstall)
        info "Removing DEAM cron block from crontab..."
        STRIPPED=$(current_crontab | strip_managed_block)
        if [[ -z "$STRIPPED" ]]; then
            crontab -r 2>/dev/null || true
        else
            printf '%s\n' "$STRIPPED" | crontab -
        fi
        success "DEAM cron entries removed."
        exit 0
        ;;
    install)
        info "Installing DEAM cron block (container: $FLASK_CONTAINER, logs: $LOG_DIR)..."
        # Pre-create log files so first cron run does not hit a permission error.
        # Use sudo if available; fall back gracefully if it isn't.
        while read -r slug; do
            log_path="$LOG_DIR/${slug}.log"
            if [[ ! -f "$log_path" ]]; then
                if command -v sudo >/dev/null 2>&1; then
                    sudo touch "$log_path" && sudo chmod 664 "$log_path" \
                        && sudo chown "$USER":"$USER" "$log_path" 2>/dev/null || true
                else
                    touch "$log_path" 2>/dev/null || warn "Could not create $log_path"
                fi
            fi
        done < <(schedule_log_slugs)

        install_logrotate

        STRIPPED=$(current_crontab | strip_managed_block)
        # Assemble: existing entries (minus old block) + blank line + new block
        {
            [[ -n "$STRIPPED" ]] && printf '%s\n\n' "$STRIPPED"
            printf '%s\n' "$NEW_BLOCK"
        } | crontab -

        success "Installed. Current DEAM entries:"
        crontab -l | awk -v begin="$BEGIN_MARKER" -v end="$END_MARKER" '
            $0 == begin { inside=1 }
            inside
            $0 == end   { inside=0 }
        '
        ;;
esac
