#!/bin/bash
#
# run-attack-surface-tests.sh — execute the multi-persona attack-surface
# regression suite against staging.
#
# WHY a separate wrapper:
#   The suite (tests/security/test_attack_surface.py) is a live HTTP
# regression that logs in as 6 personas and walks the attack
#   surface. It needs ~30 env vars (one credential bundle per persona)
#   forwarded into imports_container. Typing those by hand each run is
#   error-prone, and most of them are real staging passwords that must
#   never appear in shell history or commits.
#
# WHAT it does:
#   - Sources ~/.env.attack-surface (or REPO_DIR/.env.attack-surface) to
#     load TEST_BASE_URL and PERSONA_* credentials.
#   - Forwards every TEST_* and PERSONA_* variable into imports_container
#     via `docker exec -e`.
#   - Runs pytest against tests/security/test_attack_surface.py with the
#     `live` marker. End-of-session output is a persona x surface matrix
#     printed by the conftest plugin.
#
# WHEN to run:
# After a release with tenant-isolation changes (any -adjacent
#     work — workspace scoping, role filter, asset visibility).
#   - As an ad-hoc sanity check before flipping a tenant-isolation flag.
#   - After provisioning a new persona account (smoke that login works).
#
# NOT for prod. The suite's conftest hard-fails if TEST_BASE_URL resolves
# to kavanahanalytics.com (or a PROD_HOSTNAMES entry). The persona
# accounts should only exist on staging.
#
# Setup (one-time): see tests/security/PERSONAS.md for how to create the
# 6 staging accounts with bypass_mfa, and tests/security/.env.attack-
# surface.example for the credential template.

set -euo pipefail

# ── Argument parsing ──────────────────────────────────────────────────────────
# -prod: opt in to running against a production hostname. stood the
#   suite up on prod with dedicated throwaway personas; without this flag the
#   prod-hostname guard below (and the suite's own conftest guard) hard-refuse
#   prod. The flag is deliberately verbose to type — there is no short form.
#   It also exports ATTACK_SURFACE_ALLOW_PROD=1 so the in-container conftest
#   guard honors the same opt-in (belt and suspenders: both must agree).
# Remaining args are forwarded to pytest unchanged.
ALLOW_PROD=0
PYTEST_EXTRA=()
while [[ $# -gt 0 ]]; do
    case "$1" in
        --prod) ALLOW_PROD=1; shift ;;
        *) PYTEST_EXTRA+=("$1"); shift ;;
    esac
done

# ── Configuration ────────────────────────────────────────────────────────────
REPO_DIR="${REPO_DIR:-$HOME/DE-Reporting}"
[[ -d "$REPO_DIR" ]] || REPO_DIR="$HOME/kavanah-analytics"

# Env file lookup order:
#   1. $ATTACK_SURFACE_ENV (explicit override)
#   2. $REPO_DIR/.env.attack-surface (alongside the repo)
#   3. $HOME/.env.attack-surface (operator home)
ENV_FILE="${ATTACK_SURFACE_ENV:-}"
if [[ -z "$ENV_FILE" ]]; then
    for candidate in "$REPO_DIR/.env.attack-surface" "$HOME/.env.attack-surface"; do
        if [[ -f "$candidate" ]]; then
            ENV_FILE="$candidate"
            break
        fi
    done
fi

FLASK_CONTAINER="${FLASK_CONTAINER:-imports_container}"

# ── Helpers ──────────────────────────────────────────────────────────────────
info()    { echo "[INFO]    $*"; }
success() { echo "[SUCCESS] $*"; }
error()   { echo "[ERROR]   $*" >&2; exit 1; }

# ── Pre-flight ───────────────────────────────────────────────────────────────
command -v docker >/dev/null 2>&1 || error "docker not found"
[[ -d "$REPO_DIR" ]] || error "Repo directory not found: $REPO_DIR"
[[ -n "$ENV_FILE" ]] || error \
    "No .env.attack-surface found. Looked in \$ATTACK_SURFACE_ENV, $REPO_DIR/.env.attack-surface, $HOME/.env.attack-surface. See tests/security/PERSONAS.md."
[[ -f "$ENV_FILE" ]] || error "Env file does not exist: $ENV_FILE"

# Refuse to read a world-readable env file — it contains real passwords.
ENV_MODE=$(stat -c '%a' "$ENV_FILE" 2>/dev/null || stat -f '%A' "$ENV_FILE" 2>/dev/null || echo "")
if [[ -n "$ENV_MODE" && "$ENV_MODE" != "600" && "$ENV_MODE" != "400" ]]; then
    error "Env file $ENV_FILE has permissions $ENV_MODE — must be 600 or 400. Run: chmod 600 $ENV_FILE"
fi

docker ps --format '{{.Names}}' | grep -qw "$FLASK_CONTAINER" \
    || error "$FLASK_CONTAINER is not running. Start the stack first."

# ── Load env ─────────────────────────────────────────────────────────────────
info "Loading credentials from $ENV_FILE"
set -a
# shellcheck disable=SC1090
source "$ENV_FILE"
set +a

[[ -n "${TEST_BASE_URL:-}" ]] || error "TEST_BASE_URL not set in $ENV_FILE"

# Refuse-to-run-on-prod guard — the suite has its own conftest guard too, but
# catching it here avoids spinning up pytest at all. --prod opts in to
# a deliberate prod run (dedicated throwaway personas live there now). When
# opted in we also export ATTACK_SURFACE_ALLOW_PROD=1 so the conftest guard
# agrees — both must say yes, so a stray env var alone can't reach prod and a
# stray flag alone can't either.
# production hostnames = the built-in product domain plus PROD_HOSTNAMES
# (comma-separated), the same list lib/prod_hosts.py gives the Python guards.
# Case-insensitive substring match, as before. bash-3.2 compatible (no ${var,,}).
_is_prod_host() {
    local url h extra
    url=$(printf '%s' "$1" | tr '[:upper:]' '[:lower:]')
    # ${VAR:-} first: under `set -u` a pattern substitution on an unset var aborts
    # the script, and PROD_HOSTNAMES is unset on every box that has not opted in.
    extra="${PROD_HOSTNAMES:-}"
    for h in kavanahanalytics.com ${extra//,/ }; do
        h=$(printf '%s' "$h" | tr '[:upper:]' '[:lower:]')
        [[ -n "$h" && "$url" == *"$h"* ]] && return 0
    done
    return 1
}

if _is_prod_host "$TEST_BASE_URL"; then
    if [[ "$ALLOW_PROD" -eq 1 ]]; then
        info "PROD opt-in (--prod) set — running against production ($TEST_BASE_URL)."
        export ATTACK_SURFACE_ALLOW_PROD=1
    else
        error "Refusing to run against production ($TEST_BASE_URL). Pass --prod to opt in."
    fi
fi

info "Target: $TEST_BASE_URL"

# ── Build docker exec env args ───────────────────────────────────────────────
# Forward every TEST_* and PERSONA_* var into the container as -e NAME.
# `docker exec -e NAME` (without =VALUE) inherits the value from the
# current shell, which means passwords never appear in `ps` output or
# shell history.
ENV_ARGS=()
while IFS='=' read -r name _; do
    case "$name" in
        TEST_*|PERSONA_*)
            ENV_ARGS+=("-e" "$name")
            ;;
    esac
done < <(env | grep -E '^(TEST_|PERSONA_)' | sort)

ENV_ARGS+=("-e" "PYTHONPATH=/app/bin/python:/app/bin/python/asset_management/web")

# forward the prod opt-in into the container so the suite's conftest
# guard honors the same signal. Only set when --prod was passed.
if [[ "$ALLOW_PROD" -eq 1 ]]; then
    ENV_ARGS+=("-e" "ATTACK_SURFACE_ALLOW_PROD=1")
fi

[[ ${#ENV_ARGS[@]} -gt 2 ]] || error "No TEST_*/PERSONA_* vars in environment — env file may be empty."

info "Forwarding ${#ENV_ARGS[@]} env entries (TEST_*/PERSONA_*) into $FLASK_CONTAINER"

# ── Run pytest ───────────────────────────────────────────────────────────────
# -m live: only run tests marked @pytest.mark.live (the attack-surface
#   suite is the only thing under tests/security/ marked live).
# -v: per-test status, needed for the matrix output to render.
# Trailing pytest exit code is preserved by set -e so the script
# returns non-zero on any LEAK or unexpected ERROR.
info "Running pytest..."
# Any non---prod CLI args are forwarded to pytest (e.g. -k DataConnectorsPicker).
# Guarded expansion keeps set -u happy when PYTEST_EXTRA is empty.
docker exec -i "${ENV_ARGS[@]}" "$FLASK_CONTAINER" \
    python3 -m pytest /app/tests/security/test_attack_surface.py \
        -v -m live \
        -c /app/pytest.ini \
        --rootdir /app \
        ${PYTEST_EXTRA[@]+"${PYTEST_EXTRA[@]}"}

success "Attack-surface regression complete."
