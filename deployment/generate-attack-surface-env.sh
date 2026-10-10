#!/bin/bash
#
# generate-attack-surface-env.sh — write a fresh .env.attack-surface
# with strong random passwords and placeholders for everything else.
#
# WHY a separate generator:
#   The attack-surface suite (tests/security/test_attack_surface.py)
#   needs ~30 env vars per run, 6 of them passwords. Hand-typing the
#   file is error-prone, and copying the example file leaves the
#   FORBIDDEN/ALLOWED keys empty so the suite's no-leak checks become
#   no-ops without warning. This script writes the file in one shot
#   with random passwords already baked in, so the only thing left to
#   edit by hand is the workspace/org/team identifier placeholders
#   (which differ per environment).
#
# WHAT it does:
#   - Picks a sensible TEST_BASE_URL (overridable via flag).
#   - Generates 6 long random passwords (32 chars, alnum + ! @ # %).
#   - Writes ~/DE-Reporting/.env.attack-surface (overridable via flag)
#     with the full PERSONA_* block, 0600 perms.
#   - Refuses to clobber an existing file unless --force is passed.
#   - Prints the 6 generated passwords once to stdout so you can copy
#     them into the staging User Management UI when creating the
#     persona accounts.
#
# WHAT IT DOES NOT DO (yet):
#   - Create the persona accounts in ES — you still do that via the
#     staging User Management UI (see tests/security/PERSONAS.md).
#   - Wire up memberships / grants / bypass_mfa — also UI for now.
#   - These will come in a Phase 2 wrapper once the service-layer
#     plumbing is traced.
#
# Usage:
#   deployment/generate-attack-surface-env.sh                   # default path, prompts before overwrite
#   deployment/generate-attack-surface-env.sh --force           # overwrite if exists
#   deployment/generate-attack-surface-env.sh --out /tmp/x.env  # write elsewhere
#   deployment/generate-attack-surface-env.sh --base-url https://...

set -euo pipefail

# ── Defaults ────────────────────────────────────────────────────────────────
# Base URL of the staging deployment. No org default in the tree:
# pass --base-url or export ATTACK_SURFACE_BASE_URL.
DEFAULT_BASE_URL="${ATTACK_SURFACE_BASE_URL:-}"
DEFAULT_OUT="${ATTACK_SURFACE_ENV:-}"
if [[ -z "$DEFAULT_OUT" ]]; then
    if [[ -d "$HOME/DE-Reporting" ]]; then
        DEFAULT_OUT="$HOME/DE-Reporting/.env.attack-surface"
    elif [[ -d "$HOME/kavanah-analytics" ]]; then
        DEFAULT_OUT="$HOME/kavanah-analytics/.env.attack-surface"
    else
        DEFAULT_OUT="$HOME/.env.attack-surface"
    fi
fi

# Default persona usernames. Override the defaults block below before
# generating if your staging convention is different.
DEFAULT_USERNAME_PREFIX="test"
# Must match TEST_PERSONA_DOMAIN on the target (lib/test_personas.py).
DEFAULT_USERNAME_DOMAIN="${TEST_PERSONA_DOMAIN:-example.com}"

OUT="$DEFAULT_OUT"
BASE_URL="$DEFAULT_BASE_URL"
FORCE=0

# ── Arg parsing ─────────────────────────────────────────────────────────────
while [[ $# -gt 0 ]]; do
    case "$1" in
        --out)
            OUT="$2"; shift 2
            ;;
        --base-url)
            BASE_URL="$2"; shift 2
            ;;
        --force)
            FORCE=1; shift
            ;;
        -h|--help)
            sed -n '2,30p' "$0"
            exit 0
            ;;
        *)
            echo "[ERROR] Unknown flag: $1" >&2
            exit 2
            ;;
    esac
done

# ── Pre-flight ──────────────────────────────────────────────────────────────
info()    { echo "[INFO]    $*"; }
success() { echo "[SUCCESS] $*"; }
error()   { echo "[ERROR]   $*" >&2; exit 1; }

if [[ -e "$OUT" && $FORCE -eq 0 ]]; then
    error "File exists: $OUT. Pass --force to overwrite, or --out to write elsewhere."
fi

command -v openssl >/dev/null 2>&1 || error "openssl not found (needed for password generation)"
[[ -n "$BASE_URL" ]] || error "No base URL: pass --base-url https://<staging-host> or export ATTACK_SURFACE_BASE_URL."

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

# Refuse to write a prod-pointing file by accident.
if _is_prod_host "$BASE_URL"; then
    error "Refusing to generate an attack-surface env file targeting production ($BASE_URL)."
fi

# ── Helpers ─────────────────────────────────────────────────────────────────
# Generate a 32-char password from a friendly alphabet (alnum plus a
# few symbols that don't need shell-quoting). Avoids characters that
# would confuse `set -a; source` of the env file.
gen_password() {
    # 24 random bytes → base64 (32 chars) → strip /+= which are noisy
    # in env files. Append a digit + symbol so password policies that
    # require both are satisfied.
    local raw
    raw=$(openssl rand -base64 24 | tr -d '/+=' | head -c 28)
    echo "${raw}9!"
}

# ── Generate passwords ──────────────────────────────────────────────────────
info "Generating 6 random passwords..."
PASS_BARE=$(gen_password)
PASS_WSADMIN=$(gen_password)
PASS_ORG=$(gen_password)
PASS_TEAM=$(gen_password)
PASS_GRANT=$(gen_password)
PASS_SUPER=$(gen_password)

# ── Write env file ──────────────────────────────────────────────────────────
info "Writing $OUT"

# Set umask + touch + chmod BEFORE writing the content so the file
# never exists on disk with looser permissions, even momentarily.
# We then re-apply chmod 600 AFTER the heredoc in case the redirect
# resets the perms (some filesystems / shells do).
umask 077
: > "$OUT"
chmod 600 "$OUT"

cat > "$OUT" <<EOF
# Attack-surface regression suite credential bundle.
# Generated by deployment/generate-attack-surface-env.sh on $(date -Iseconds).
#
# Edit the FORBIDDEN_/ALLOWED_ placeholders below to match your staging
# workspace/org/team naming. Then create the 6 persona accounts in the
# staging User Management UI using these usernames + passwords, plus
# set bypass_mfa=true on each. See tests/security/PERSONAS.md.
#
# Permissions: 0600 (owner read/write only). Do not commit.

# --- Target environment ---
TEST_BASE_URL=$BASE_URL

# --- Chart-data statistic keys (optional overrides) ---
# Defaults in test_attack_surface.py are real _MAP_STATISTICS keys; uncomment
# only if your staging has renamed one.
#TEST_CHART_STAT_FACEBOOK=reach
#TEST_CHART_STAT_ECHO=messages
#TEST_CHART_STAT_GOOGLE_ADS=google_ads_app_installs
#TEST_CHART_STAT_DT=baptisms

# ===========================================================================
# bare — no roles, no memberships, no grants, no workspace
# ===========================================================================
PERSONA_BARE_USERNAME=${DEFAULT_USERNAME_PREFIX}-bare@${DEFAULT_USERNAME_DOMAIN}
PERSONA_BARE_PASSWORD=$PASS_BARE
# Identifiers the bare persona must NEVER see in any response.
# Use the control workspace + control org + control team you've seeded
# on staging, plus a couple of real tenants for belt-and-suspenders.
PERSONA_BARE_FORBIDDEN_ORGS=ControlOrg
PERSONA_BARE_FORBIDDEN_TEAMS=ControlTeam
PERSONA_BARE_FORBIDDEN_WORKSPACES=control-workspace
PERSONA_BARE_EXPECTS_EMPTY_SCOPE=true

# ===========================================================================
# workspace_admin — workspace_admin role in one workspace
# ===========================================================================
PERSONA_WORKSPACE_ADMIN_USERNAME=${DEFAULT_USERNAME_PREFIX}-wsadmin@${DEFAULT_USERNAME_DOMAIN}
PERSONA_WORKSPACE_ADMIN_PASSWORD=$PASS_WSADMIN
# Replace these placeholders with the workspace slug this persona admins,
# and a second (forbidden) workspace they should never see.
PERSONA_WORKSPACE_ADMIN_ALLOWED_WORKSPACES=workspace-a
PERSONA_WORKSPACE_ADMIN_FORBIDDEN_WORKSPACES=control-workspace,workspace-b
PERSONA_WORKSPACE_ADMIN_FORBIDDEN_ORGS=ControlOrg,OrgFromWorkspaceB
PERSONA_WORKSPACE_ADMIN_FORBIDDEN_TEAMS=ControlTeam,TeamFromWorkspaceB
PERSONA_WORKSPACE_ADMIN_CAN_SEE_USER_MANAGEMENT=true

# ===========================================================================
# org_user — dashboard_user with one org membership
# ===========================================================================
PERSONA_ORG_USER_USERNAME=${DEFAULT_USERNAME_PREFIX}-org@${DEFAULT_USERNAME_DOMAIN}
PERSONA_ORG_USER_PASSWORD=$PASS_ORG
PERSONA_ORG_USER_ALLOWED_ORGS=OrgA
PERSONA_ORG_USER_FORBIDDEN_ORGS=ControlOrg,OrgB
PERSONA_ORG_USER_FORBIDDEN_TEAMS=ControlTeam,TeamFromOrgB

# ===========================================================================
# team_user — dashboard_user with one team membership
# ===========================================================================
PERSONA_TEAM_USER_USERNAME=${DEFAULT_USERNAME_PREFIX}-team@${DEFAULT_USERNAME_DOMAIN}
PERSONA_TEAM_USER_PASSWORD=$PASS_TEAM
PERSONA_TEAM_USER_ALLOWED_TEAMS=TeamA1
PERSONA_TEAM_USER_FORBIDDEN_TEAMS=ControlTeam,TeamA2,TeamFromOrgB
PERSONA_TEAM_USER_FORBIDDEN_ORGS=ControlOrg,OrgB

# ===========================================================================
# grant_user — workspace-scoped data grant
# ===========================================================================
PERSONA_GRANT_USER_USERNAME=${DEFAULT_USERNAME_PREFIX}-grant@${DEFAULT_USERNAME_DOMAIN}
PERSONA_GRANT_USER_PASSWORD=$PASS_GRANT
# Team(s) the grant resolves to. Distinct from a membership — the grant
# path is exercised by check_grant_resolution.
PERSONA_GRANT_USER_GRANTED_TEAMS=TeamB1
PERSONA_GRANT_USER_ALLOWED_TEAMS=TeamB1
PERSONA_GRANT_USER_FORBIDDEN_TEAMS=ControlTeam,TeamB2
PERSONA_GRANT_USER_FORBIDDEN_ORGS=ControlOrg

# ===========================================================================
# superuser — sees everything (cross-tenant by design)
# ===========================================================================
PERSONA_SUPERUSER_USERNAME=${DEFAULT_USERNAME_PREFIX}-super@${DEFAULT_USERNAME_DOMAIN}
PERSONA_SUPERUSER_PASSWORD=$PASS_SUPER
PERSONA_SUPERUSER_IS_SUPERUSER=true
PERSONA_SUPERUSER_CAN_SEE_USER_MANAGEMENT=true
EOF

# Re-apply tight perms (in case the heredoc redirect reset them).
chmod 600 "$OUT"

success "Wrote $OUT (0600)."

# ── Print credentials for the operator ──────────────────────────────────────
cat <<EOF

============================================================================
Persona credentials (copy these into the staging User Management UI when
creating the accounts — they are also in $OUT):
============================================================================

  bare              ${DEFAULT_USERNAME_PREFIX}-bare@${DEFAULT_USERNAME_DOMAIN}
                    $PASS_BARE

  workspace_admin   ${DEFAULT_USERNAME_PREFIX}-wsadmin@${DEFAULT_USERNAME_DOMAIN}
                    $PASS_WSADMIN

  org_user          ${DEFAULT_USERNAME_PREFIX}-org@${DEFAULT_USERNAME_DOMAIN}
                    $PASS_ORG

  team_user         ${DEFAULT_USERNAME_PREFIX}-team@${DEFAULT_USERNAME_DOMAIN}
                    $PASS_TEAM

  grant_user        ${DEFAULT_USERNAME_PREFIX}-grant@${DEFAULT_USERNAME_DOMAIN}
                    $PASS_GRANT

  superuser         ${DEFAULT_USERNAME_PREFIX}-super@${DEFAULT_USERNAME_DOMAIN}
                    $PASS_SUPER

============================================================================

Next steps:
  1. Edit $OUT — replace the placeholder workspace/org/team identifiers
     (OrgA, TeamA1, workspace-a, ControlOrg, etc.) with real values from
     your staging environment.
  2. Create the 6 persona accounts in the staging User Management UI
     using the usernames + passwords printed above.
  3. For each persona, set bypass_mfa=true (User Management → MFA → Set
     bypass; reason: "attack-surface regression test account").
  4. Assign memberships / roles per tests/security/PERSONAS.md.
  5. Run: deployment/run-attack-surface-tests.sh

This prompt block does NOT appear in $OUT — these are the only copies
of the passwords you'll see in plain text. Save them to your password
manager now if you want to log in as a persona later for debugging.
EOF
