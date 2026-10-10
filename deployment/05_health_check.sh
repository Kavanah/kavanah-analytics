#!/usr/bin/env bash
# =============================================================================
# | 05_health_check.sh — Read-only stack health probe
# -----------------------------------------------------------------------------
# Single-pass health probe for the full DEAM stack. Exits 0 when all required
# services are healthy; exits 1 with a breakdown of failures otherwise.
#
# Checks (in order):
#   [es]     Elasticsearch cluster health  (green/yellow)
#   [kib]    Kibana /api/status            (level=available)
#   [flask]  Flask apps                    (ports 8083, 8084, 8085 → 2xx/3xx)
#   [nginx]  nginx-reverse-proxy container (running)
#   [impc]   imports_container             (running)
#
# Use at deploy time (post 02_deploy_stack.sh) and as a monitor (cron, CI).
#
# Flags:
#   --skip-nginx   — don't require the nginx-reverse-proxy container
#                    (test VMs often skip step6-nginx-setup)
#   --quiet        — only print failing checks (exit code still meaningful)
#   -h, --help     — show usage
# =============================================================================

set -uo pipefail
# Note: no `set -e` — we collect failures and report them at the end rather
# than aborting on the first one.

# ── Config ────────────────────────────────────────────────────────────────────
REPO_DIR="${REPO_DIR:-$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)}"
MASTER_CONFIG="${MASTER_CONFIG:-$REPO_DIR/config/master-config.yaml}"
FLASK_CONTAINER="${FLASK_CONTAINER:-imports_container}"
NGINX_CONTAINER="${NGINX_CONTAINER:-nginx-reverse-proxy}"
FLASK_PORTS=(8083 8084 8085)

# ── Colour helpers ────────────────────────────────────────────────────────────
RED='\033[0;31m'; GREEN='\033[0;32m'; YELLOW='\033[1;33m'; CYAN='\033[0;36m'; NC='\033[0m'

usage() {
    sed -n '2,/^# ===/p' "$0" | sed 's/^# \{0,1\}//'
    exit "${1:-0}"
}

# ── Arg parsing ───────────────────────────────────────────────────────────────
SKIP_NGINX=false
QUIET=false
for arg in "$@"; do
    case "$arg" in
        --skip-nginx) SKIP_NGINX=true ;;
        --quiet)      QUIET=true ;;
        -h|--help)    usage 0 ;;
        *)            echo "Unknown argument: $arg" >&2; exit 2 ;;
    esac
done

# ── Result tracking ───────────────────────────────────────────────────────────
FAILURES=()

ok()   { [[ "$QUIET" == true ]] || echo -e "  ${GREEN}[OK]${NC}   $*"; }
fail() { echo -e "  ${RED}[FAIL]${NC} $*"; FAILURES+=("$*"); }
note() { [[ "$QUIET" == true ]] || echo -e "  ${YELLOW}[NOTE]${NC} $*"; }

[[ "$QUIET" == true ]] || echo -e "${CYAN}DEAM health check — $(date -u +%FT%TZ)${NC}"

# ── Elasticsearch ─────────────────────────────────────────────────────────────
check_elasticsearch() {
    if [[ ! -f "$MASTER_CONFIG" ]] || ! command -v yq >/dev/null 2>&1; then
        fail "[es] master-config.yaml or yq missing — cannot read admin password"
        return
    fi
    local pass status body
    pass=$(yq e '.elk-01.es01.admin_pass' "$MASTER_CONFIG" 2>/dev/null || echo "")
    if [[ -z "$pass" || "$pass" == "null" ]]; then
        fail "[es] admin_pass not set in master-config.yaml"
        return
    fi
    body=$(curl -sk -u "elastic:${pass}" --max-time 5 \
        "https://localhost:9200/_cluster/health" 2>/dev/null || true)
    status=$(echo "$body" | grep -o '"status":"[^"]*"' | cut -d'"' -f4)
    case "$status" in
        green|yellow) ok "[es] cluster health: $status" ;;
        red)          fail "[es] cluster health: red — check docker logs es01" ;;
        *)            fail "[es] unreachable (https://localhost:9200)" ;;
    esac
}

# ── Kibana ────────────────────────────────────────────────────────────────────
# curl's -w "%{http_code}" prints "000" when the connection fails, so we use
# that directly as a reachability signal — no `|| echo` fallback (which would
# double-append on failure).
check_kibana() {
    local code body level
    code=$(curl -sk -o /tmp/.deam_kib_body --max-time 5 -w "%{http_code}" \
        "http://localhost:5601/api/status" 2>/dev/null)
    if [[ "$code" == "000" ]]; then
        fail "[kib] unreachable (http://localhost:5601) — is port 5601 bound to the host?"
        return
    fi
    body=$(cat /tmp/.deam_kib_body 2>/dev/null || true); rm -f /tmp/.deam_kib_body
    level=$(echo "$body" | grep -o '"level":"[^"]*"' | head -1 | cut -d'"' -f4)
    case "$level" in
        available) ok "[kib] status: available" ;;
        degraded)  fail "[kib] status: degraded" ;;
        critical)  fail "[kib] status: critical" ;;
        "")        fail "[kib] reachable (HTTP $code) but /api/status has no 'level' field (basePath or proxy rewrite?)" ;;
        *)         fail "[kib] unexpected status: $level" ;;
    esac
}

# ── Flask apps ────────────────────────────────────────────────────────────────
check_flask_apps() {
    for port in "${FLASK_PORTS[@]}"; do
        local code
        code=$(curl -sk -o /dev/null --max-time 5 -w "%{http_code}" \
            "http://localhost:${port}/" 2>/dev/null)
        if [[ "$code" =~ ^[23] ]]; then
            ok "[flask] port $port: HTTP $code"
        elif [[ "$code" == "000" ]]; then
            fail "[flask] port $port: unreachable — port not bound to host?"
        else
            fail "[flask] port $port: HTTP $code"
        fi
    done
}

# ── Docker container liveness ─────────────────────────────────────────────────
check_container() {
    local label="$1" name="$2" required="$3"
    if ! command -v docker >/dev/null 2>&1; then
        fail "$label docker not installed"
        return
    fi
    if docker ps --format '{{.Names}}' 2>/dev/null | grep -qw "$name"; then
        ok "$label $name: running"
    elif docker ps -a --format '{{.Names}}' 2>/dev/null | grep -qw "$name"; then
        if [[ "$required" == "required" ]]; then
            fail "$label $name: container exists but is not running"
        else
            note "$label $name: container exists but is not running (optional)"
        fi
    else
        if [[ "$required" == "required" ]]; then
            fail "$label $name: container does not exist"
        else
            note "$label $name: container does not exist (optional)"
        fi
    fi
}

# ── Run checks ────────────────────────────────────────────────────────────────
check_elasticsearch
check_kibana
check_flask_apps
check_container "[impc]"  "$FLASK_CONTAINER"  "required"
if [[ "$SKIP_NGINX" == true ]]; then
    note "[nginx] skipped (--skip-nginx)"
else
    check_container "[nginx]" "$NGINX_CONTAINER" "required"
fi

# ── Verdict ───────────────────────────────────────────────────────────────────
echo
if (( ${#FAILURES[@]} == 0 )); then
    [[ "$QUIET" == true ]] || echo -e "${GREEN}All services healthy.${NC}"
    exit 0
fi

echo -e "${RED}Health check failed (${#FAILURES[@]} issue(s)):${NC}"
for f in "${FAILURES[@]}"; do echo "  - $f"; done
exit 1
