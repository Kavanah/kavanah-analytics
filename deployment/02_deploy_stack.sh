#!/usr/bin/env bash
# =============================================================================
# | 02_deploy_stack.sh — Bring up / refresh the full application stack
# -----------------------------------------------------------------------------
# Runs `docker compose up -d` on the generated compose file and then waits for
# each service to become healthy:
#   - Elasticsearch  — cluster health green/yellow
#   - Kibana         — /api/status level=available
#   - Flask apps     — ports 8083, 8084, 8085 return 2xx/3xx
#
# Prerequisites:
#   - step3-app-stack.sh must have been run once to generate
#     $CONFIG_DIR/docker-compose.yml and $MASTER_CONFIG with elastic creds.
#   - The `elastic-network` Docker network must exist (created by step3).
#
# Idempotent:
#   - `docker compose up -d` is a no-op if containers are already running
#     with the declared spec; it reconciles partial-up state automatically.
#
# Flags:
#   --no-wait      — start containers but skip health polling (fast dev loop)
#   --timeout N    — total health-poll timeout in seconds (default 300)
#   -h, --help     — show usage
# =============================================================================

set -euo pipefail

# ── Config ────────────────────────────────────────────────────────────────────
REPO_DIR="${REPO_DIR:-$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)}"
CONFIG_DIR="${CONFIG_DIR:-$REPO_DIR/config}"
MASTER_CONFIG="${MASTER_CONFIG:-$CONFIG_DIR/master-config.yaml}"
# Compose file lives at deployment/docker-compose.yml in git as of
# (2026-05-09). Override with COMPOSE_FILE=... if needed for migration scripts.
# Pre-cutover hosts may still have the legacy step3-generated file at
# $CONFIG_DIR/docker-compose.yml; we fall back to that path if the canonical
# one is absent so this change is safe to land before step3 is rewritten.
DEFAULT_COMPOSE_FILE="$REPO_DIR/deployment/docker-compose.yml"
LEGACY_COMPOSE_FILE="$CONFIG_DIR/docker-compose.yml"
if [[ -z "${COMPOSE_FILE:-}" ]]; then
    if [[ -f "$DEFAULT_COMPOSE_FILE" ]]; then
        COMPOSE_FILE="$DEFAULT_COMPOSE_FILE"
    else
        COMPOSE_FILE="$LEGACY_COMPOSE_FILE"
    fi
fi

ES_CONTAINER="es01"
KIB_CONTAINER="kib01"
FLASK_CONTAINER="imports_container"
NGINX_CONTAINER="nginx-reverse-proxy"
FLASK_PORTS=(8083 8084 8085)

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
WAIT_FOR_HEALTH=true
TOTAL_TIMEOUT=300
while [[ $# -gt 0 ]]; do
    case "$1" in
        --no-wait)    WAIT_FOR_HEALTH=false; shift ;;
        --timeout)    TOTAL_TIMEOUT="$2"; shift 2 ;;
        -h|--help)    usage 0 ;;
        *)            error "Unknown argument: $1" ;;
    esac
done

# ── Preflight ─────────────────────────────────────────────────────────────────
command -v docker >/dev/null 2>&1 || error "Docker is not installed."
docker compose version >/dev/null 2>&1 || error "Docker Compose v2 plugin not available."
[[ -f "$COMPOSE_FILE" ]] || error "Compose file missing: $COMPOSE_FILE. Run deployment/step3-app-stack.sh first."
[[ -f "$MASTER_CONFIG" ]] || error "Master config missing: $MASTER_CONFIG. Run deployment/step3-app-stack.sh first."
command -v yq >/dev/null 2>&1 || error "yq is required (snap install yq)."

ELASTIC_PASS=$(yq e '.elk-01.es01.admin_pass' "$MASTER_CONFIG")
[[ -n "$ELASTIC_PASS" && "$ELASTIC_PASS" != "null" ]] \
    || error "elk-01.es01.admin_pass is not set in $MASTER_CONFIG"

info "| 02_deploy_stack.sh"
info "Compose: $COMPOSE_FILE"
info "Timeout: ${TOTAL_TIMEOUT}s (per-service poll)"

# ── Migrate legacy / mismatched-project containers ───────────────────────────
# step3-app-stack.sh creates containers via raw `docker run` for first-run
# bootstrap. Compose refuses to manage a container it did not create (no
# `com.docker.compose.project` label).
#
# A second case bites the containers from a PREVIOUS
# compose project (e.g. config/docker-compose.yml — project=config) carry a
# label, but a different one than the active compose file (deployment/docker-compose.yml — project=deployment).
# Compose then errors with `Conflict. The container name "/es01" is already in use`.
# We remove both kinds — unlabeled and mismatched-project — preserving the
# bind-mounted data dirs (host-owned, container-recreate-safe).
ACTIVE_PROJECT=$(docker compose -f "$COMPOSE_FILE" config --format json 2>/dev/null \
    | python3 -c "import json,sys; print(json.load(sys.stdin).get('name',''))" 2>/dev/null || echo "")
if [[ -z "$ACTIVE_PROJECT" ]]; then
    # Fallback: compose's default project name is the basename of the dir
    # containing the compose file (with hyphens etc.).
    ACTIVE_PROJECT=$(basename "$(dirname "$COMPOSE_FILE")")
fi
info "Active compose project: $ACTIVE_PROJECT"

for c in "$ES_CONTAINER" "$KIB_CONTAINER" "$FLASK_CONTAINER" "$NGINX_CONTAINER"; do
    if docker ps -a --format '{{.Names}}' | grep -qw "$c"; then
        label=$(docker inspect --format '{{index .Config.Labels "com.docker.compose.project"}}' "$c" 2>/dev/null || echo "")
        if [[ -z "$label" ]]; then
            warn "Removing legacy non-compose container: $c (data volumes preserved)"
            docker rm -f "$c" >/dev/null
        elif [[ "$label" != "$ACTIVE_PROJECT" ]]; then
            warn "Removing $c (compose project '$label' != active '$ACTIVE_PROJECT'; data volumes preserved)"
            docker rm -f "$c" >/dev/null
        fi
    fi
done

# ── Bring up the stack ────────────────────────────────────────────────────────
info "Running 'docker compose up -d'..."
docker compose -f "$COMPOSE_FILE" up -d
success "Containers reconciled (up -d completed)."

if [[ "$WAIT_FOR_HEALTH" == false ]]; then
    warn "--no-wait was set. Skipping health checks."
    exit 0
fi

# ── Health waiters ────────────────────────────────────────────────────────────
# Each waiter polls once per 5s. TOTAL_TIMEOUT caps each phase independently so
# a slow ES start does not starve Kibana / Flask waiters.
wait_until() {
    local label="$1" probe="$2"
    local deadline=$(( $(date +%s) + TOTAL_TIMEOUT ))
    info "Waiting for $label..."
    while (( $(date +%s) < deadline )); do
        if eval "$probe" >/dev/null 2>&1; then
            success "$label is healthy."
            return 0
        fi
        sleep 5
    done
    error "$label did not become healthy within ${TOTAL_TIMEOUT}s. Check: docker logs <container>"
}

es_probe() {
    local status
    status=$(curl -sk -u "elastic:${ELASTIC_PASS}" \
        "https://localhost:9200/_cluster/health" 2>/dev/null \
        | grep -o '"status":"[^"]*"' | cut -d'"' -f4 || true)
    [[ "$status" == "green" || "$status" == "yellow" ]]
}

kibana_probe() {
    local level
    # /kibana/api/status not /api/status: kibana.yml sets server.basePath:
    # /kibana with rewriteBasePath: true, so the bare path 404s.
    level=$(curl -sk "http://localhost:5601/kibana/api/status" 2>/dev/null \
        | grep -o '"level":"[^"]*"' | head -1 | cut -d'"' -f4 || true)
    [[ "$level" == "available" ]]
}

flask_probe_port() {
    local code
    # curl prints "000" on connect failure via -w; no `|| echo` fallback needed
    code=$(curl -sk -o /dev/null -w "%{http_code}" \
        "http://localhost:$1/" 2>/dev/null)
    [[ "$code" =~ ^[23] ]]
}

# Sanity: the containers must actually exist and be running before we poll APIs.
# nginx is optional — step6 is a separate, optional bootstrap step — so we only
# require it when the compose file declares it as a service.
REQUIRED=("$ES_CONTAINER" "$KIB_CONTAINER" "$FLASK_CONTAINER")
if yq e '.services | has("'"$NGINX_CONTAINER"'")' "$COMPOSE_FILE" 2>/dev/null | grep -qx true; then
    REQUIRED+=("$NGINX_CONTAINER")
fi
for c in "${REQUIRED[@]}"; do
    docker ps --format '{{.Names}}' | grep -qw "$c" \
        || error "Container '$c' is not running. Check: docker compose logs $c"
done

wait_until "Elasticsearch cluster"     "es_probe"
wait_until "Kibana status API"         "kibana_probe"
for p in "${FLASK_PORTS[@]}"; do
    wait_until "Flask port $p" "flask_probe_port $p"
done

echo
success "Stack is fully deployed and healthy."
info    "  Elasticsearch: https://localhost:9200 (user: elastic)"
info    "  Kibana:        http://localhost:5601"
info    "  Flask apps:    http://localhost:{8083,8084,8085}"

# Run idempotent post-deploy data migrations. Safe to re-run on every deploy.
MIGRATIONS_SCRIPT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/07_post_deploy_migrations.sh"
if [[ -x "$MIGRATIONS_SCRIPT" ]]; then
    info "Running post-deploy migrations..."
    "$MIGRATIONS_SCRIPT"
elif [[ -f "$MIGRATIONS_SCRIPT" ]]; then
    info "Running post-deploy migrations..."
    bash "$MIGRATIONS_SCRIPT"
else
    warn "Post-deploy migrations script not found: $MIGRATIONS_SCRIPT"
fi
