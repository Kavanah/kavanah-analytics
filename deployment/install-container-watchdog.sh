#!/usr/bin/env bash
# =============================================================================
# Install the DEAM container watchdog as a systemd timer
# =============================================================================
# Usage:
#   bash deployment/install-container-watchdog.sh         # install + enable
#   bash deployment/install-container-watchdog.sh --uninstall
#   bash deployment/install-container-watchdog.sh --dry-run
#
# Idempotent: re-running upgrades the units in place. Logs go to journald
# under unit name container-watchdog.service.
#
# Re-run after any change to deployment/systemd/container-watchdog.{service,timer}
# — the installer always re-renders and re-installs the units, so any unit-file
# changes (like the Documentation= path templating) require a re-run
# to propagate to a host that already has the watchdog installed.
#
# Verify after install:
#   systemctl status container-watchdog.timer
#   systemctl list-timers container-watchdog.timer
#   journalctl -u container-watchdog.service -n 50
#   sudo systemctl start container-watchdog.service   # force-run once
# =============================================================================
set -euo pipefail

REPO_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
WATCHDOG_SCRIPT="$REPO_DIR/deployment/container-watchdog.sh"
SYSTEMD_SRC="$REPO_DIR/deployment/systemd"
SYSTEMD_DST="/etc/systemd/system"
SERVICE_NAME="container-watchdog"

GREEN='\033[0;32m'; RED='\033[0;31m'; YELLOW='\033[1;33m'; CYAN='\033[0;36m'; NC='\033[0m'
info()  { echo -e "${CYAN}[INFO]${NC}  $*"; }
ok()    { echo -e "${GREEN}[OK]${NC}    $*"; }
warn()  { echo -e "${YELLOW}[WARN]${NC}  $*"; }
die()   { echo -e "${RED}[FAIL]${NC} $*" >&2; exit 1; }

DRY_RUN=0
UNINSTALL=0
for arg in "$@"; do
    case "$arg" in
        --dry-run)   DRY_RUN=1 ;;
        --uninstall) UNINSTALL=1 ;;
        -h|--help)
            sed -n '2,12p' "$0"; exit 0 ;;
        *) die "Unknown arg: $arg" ;;
    esac
done

run() {
    if (( DRY_RUN )); then
        echo "  + $*"
    else
        eval "$@"
    fi
}

# ── Uninstall path ───────────────────────────────────────────────────────────
if (( UNINSTALL )); then
    info "Uninstalling $SERVICE_NAME..."
    run "sudo systemctl disable --now ${SERVICE_NAME}.timer 2>/dev/null || true"
    run "sudo systemctl stop ${SERVICE_NAME}.service 2>/dev/null || true"
    run "sudo rm -f ${SYSTEMD_DST}/${SERVICE_NAME}.service ${SYSTEMD_DST}/${SERVICE_NAME}.timer"
    run "sudo systemctl daemon-reload"
    ok "Uninstalled."
    exit 0
fi

# ── Install path ─────────────────────────────────────────────────────────────
[[ -f "$WATCHDOG_SCRIPT" ]] || die "Watchdog script not found at $WATCHDOG_SCRIPT"
[[ -d "$SYSTEMD_SRC" ]]     || die "Systemd unit dir not found at $SYSTEMD_SRC"
command -v systemctl >/dev/null || die "systemctl not found (this script requires a systemd host)"
command -v docker    >/dev/null || die "docker not found"

info "Installing $SERVICE_NAME from $REPO_DIR"
info "Watchdog path: $WATCHDOG_SCRIPT"

# Sanity: shellcheck the watchdog if shellcheck is available. Don't block
# install if it isn't — but if it is, fail noisily on errors.
if command -v shellcheck >/dev/null 2>&1; then
    info "Running shellcheck on watchdog script..."
    shellcheck "$WATCHDOG_SCRIPT" || die "shellcheck failed on $WATCHDOG_SCRIPT"
    ok "shellcheck clean."
fi

# chmod must come before the smoke test — otherwise the test fails with
# permission denied on a fresh checkout (git doesn't preserve the +x bit
# from Windows clones).
run "chmod +x $WATCHDOG_SCRIPT"

# Make sure the watchdog itself runs end-to-end before we install the timer.
# A buggy watchdog that exits 1 every 2 minutes spams journald and may mask
# real failures.
info "Smoke-testing watchdog (expect either exit 0 or exit 1 with a clear reason)..."
if (( DRY_RUN == 0 )); then
    if "$WATCHDOG_SCRIPT"; then
        ok "Smoke test passed (exit 0)."
    else
        rc=$?
        warn "Smoke test exited $rc — install will continue, but check the output above."
        warn "If the issue is missing containers, that's expected on a fresh box —"
        warn "the watchdog will never auto-create them. Run step3/step6 to provision."
    fi
fi

# Render the service AND timer units with the absolute path to the watchdog
# script. We do the substitution into tempfiles so the source units in git keep
# the placeholder; this lets the same files work on staging (~/DE-Reporting)
# and prod (~/kavanah-analytics) without per-host edits in version control.
#
# Both units' Documentation= field carries the path so `systemctl status` shows
# the correct location regardless of which unit the operator inspects.
TMP_SERVICE=$(mktemp)
TMP_TIMER=$(mktemp)
trap 'rm -f "$TMP_SERVICE" "$TMP_TIMER"' EXIT
sed "s|__WATCHDOG_PATH__|$WATCHDOG_SCRIPT|g" "$SYSTEMD_SRC/${SERVICE_NAME}.service" > "$TMP_SERVICE"
sed "s|__WATCHDOG_PATH__|$WATCHDOG_SCRIPT|g" "$SYSTEMD_SRC/${SERVICE_NAME}.timer" > "$TMP_TIMER"

# Verify the substitutions actually happened — `sed` silently no-ops if the
# placeholder is absent, which would leave a broken unit file.
if grep -q "__WATCHDOG_PATH__" "$TMP_SERVICE"; then
    die "Failed to substitute __WATCHDOG_PATH__ in service unit"
fi
if grep -q "__WATCHDOG_PATH__" "$TMP_TIMER"; then
    die "Failed to substitute __WATCHDOG_PATH__ in timer unit"
fi

info "Installing systemd units to $SYSTEMD_DST..."
run "sudo install -m 0644 $TMP_SERVICE $SYSTEMD_DST/${SERVICE_NAME}.service"
run "sudo install -m 0644 $TMP_TIMER $SYSTEMD_DST/${SERVICE_NAME}.timer"
run "sudo systemctl daemon-reload"
run "sudo systemctl enable --now ${SERVICE_NAME}.timer"

ok "Installed and enabled ${SERVICE_NAME}.timer"

if (( DRY_RUN == 0 )); then
    echo ""
    info "Verify:"
    echo "  systemctl status ${SERVICE_NAME}.timer"
    echo "  systemctl list-timers ${SERVICE_NAME}.timer"
    echo "  journalctl -u ${SERVICE_NAME}.service -n 30 --no-pager"
    echo "  sudo systemctl start ${SERVICE_NAME}.service   # force-run once"
fi
