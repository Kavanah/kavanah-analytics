#!/usr/bin/env bash
# =============================================================================
# | 09_provision_slm.sh — Provision the ES snapshot repo + SLM policy
# -----------------------------------------------------------------------------
# Registers the `daily_repo` filesystem snapshot repository and the
# `daily-snapshots` SLM policy, idempotently, from the definition in THIS file.
#
# Until now the policy existed only as hand-created prod cluster state:
# nothing in git defined it, and staging never got one at all — the
# retention sweep's SLM check returned NotFoundError on staging
# from the day it shipped, and the restore
# check ENC-M01 could not run there. Same defect class as :
# state that only advances when someone remembers. Same fix: a deploy step.
#
# Desired state (the definition of record going forward):
#   repository `daily_repo`  — fs, /usr/share/elasticsearch/snapshots/daily
#                              (path.repo mount from $REPO_DIR/es-snapshots,
#                              wired by step3-app-stack.sh; ES creates the
#                              `daily` subdirectory itself on registration,
#                              the mount root is chown'd 1000:1000 by step3)
#   policy `daily-snapshots` — <daily-snap-{now/d}> (matches the observed
#                              daily-snap-YYYY.MM.DD-<uuid> names), 02:30 UTC,
#                              EVERYTHING minus an exclusion list, read from
#                              slm-indices.txt (see "Index list" below):
#                              `*`, then `-.*` (hidden dot-prefixed indices:
#                              the .internal.alerts-* Kibana-alerting backing
#                              indices), then the secret-bearing `de_*`
#                              indices. Kibana's own state (.kibana*) is a
#                              SYSTEM index: config.indices cannot include or
#                              exclude it (ES docs, create-snapshot API); it
#                              rides only in feature_states, which is empty
#                              because include_global_state is false — and
#                              data views are rebuilt by create-data-views.sh
#                              anyway (CLAUDE.md "Kibana State and Backups").
#                              expand_wildcards is left at the API default;
#                              the ES docs give no value for it, so the first
# snapshot's own record is the
#                              proof of what `*` reached; expire_after 3d +
#                              min_count 2 / max_count 5 (accepted on
# ; the retention sweep alarms above 30d)
#
# RECONCILED against live prod 2026-09-29: the bodies below are the
# live prod definitions read via GET /_slm/policy and GET /_snapshot — prod
# ran them 160-for-160 with 0 failures. Git is now the definition of record
# and --check keeps the cluster honest. The index list CHANGED on 2026-09-30
#: prod's hand-made allowlist of 17
# prefixes matched 29 of 363 live indices (7.6 of 405 MB) and none of the 44
# team-prefixed index families, and 12 of its patterns matched nothing at
# all. It is now `*` minus exclusions, so a NEW index is snapshotted by
# default and the only way to lose data is to add an exclusion. The first
# prod --check after this change reports DRIFT on config.indices once; the
# install that follows is the fix.
#
# Index list: kept beside this script in deployment/slm-indices.txt, which
# .gitattributes marks export-ignore (the old allowlist named
# mission teams, and inlined here it tripped the identifier scrub; the file
# stays private so a future exclusion may name one too). Every server's
# private checkout has it and the public snapshot never does. (The internal/
# submodule would not do: prod does not check it out.) Every mode, --dry-run
# included, refuses to run without a valid list rather than install a policy
# that snapshots nothing.
#
# THE ONE RULE for the list (THREAT-MODEL.md §9, per-PR question): a new
# index that stores a secret is either encrypted before write (Fernet, as
# de_asset_* connector tokens are) or added to the exclusions. Everything
# else is snapshotted without anyone remembering.
#
# Credentials: the elastic password is read env-first (ES_ADMIN_PASS, e.g.
# after `source deployment/lib/source-ssm-secrets.sh`), then from
# config/master-config.yaml (staging still carries the value), then — because
# stripped the values from prod's master-config — from SSM via
# lib/source-ssm-secrets.sh using the box's own reader profile. Never printed.
#
# Flags:
#   --env dev|prod — which SSM tree/profile the credential fallback uses
#                    (default prod, matching start-imports-container.sh)
#   --check        — compare the LIVE repo + policy against this file's
#                    definition; exit non-zero when either is missing or has
#                    drifted. Read-only. The release wrapper runs it after
# provisioning (same shape as).
#   --dry-run      — print the desired repo + policy bodies and exit. Needs
#                    no credentials and makes no network calls.
#   -h, --help     — show usage
#
# No root required: everything here is an ES REST call as `elastic`. There is
# deliberately no --uninstall — deleting a backup policy is an operator
# decision to make by hand, and no deploy path needs it.
#
# Idempotent: install PUTs the same bodies every run; ES verifies the
# repository on registration, so an unwritable snapshot dir fails loudly.
#
# Invoked by deployment/start-imports-container.sh on every deploy (,
# section 6 — the pattern).
# =============================================================================

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ES_URL="${ES_URL:-https://localhost:9200}"
MASTER_CONFIG="$SCRIPT_DIR/../config/master-config.yaml"

REPO_NAME="daily_repo"
POLICY_ID="daily-snapshots"
# Site data kept out of the public release — see "Index list" above.
INDICES_FILE="$SCRIPT_DIR/slm-indices.txt"

# The repository body. `location` is stored absolute; a live repo registered
# with the relative form ("daily", resolved against path.repo) names the same
# directory and is treated as equal by --check.
read -r -d '' REPO_BODY <<'JSON' || true
{
  "type": "fs",
  "settings": {
    "compress": "true",
    "location": "/usr/share/elasticsearch/snapshots/daily"
  }
}
JSON

# The policy body — verbatim from live prod, 2026-09-29 (see RECONCILED above),
# except config.indices, which is filled from $INDICES_FILE at run time.
#   name/schedule — observed prod snapshots: daily-snap-YYYY.MM.DD-<uuid>,
# written 02:30 UTC (elastic-stack-prod card, + runbooks)
# config — `*` minus the exclusions in slm-indices.txt (
#                   option A, 2026-09-30): `-.*` keeps hidden dot-prefixed
#                   indices out; include_global_state false keeps Kibana's
#                   system state out (no feature_states — see the header);
#                   the three `-de_*` exclusions are prod's own choices,
#                   kept as-is, as is ignore_unavailable (which also skips
#                   a closed index instead of failing the snapshot)
# retention — expire_after 3d + min_count 2 / max_count 5 (;
#                   the retention sweep reports anything over 30d as drift)
read -r -d '' POLICY_TEMPLATE <<'JSON' || true
{
  "name": "<daily-snap-{now/d}>",
  "schedule": "0 30 2 * * ?",
  "repository": "daily_repo",
  "config": {
    "indices": [],
    "ignore_unavailable": true,
    "include_global_state": false
  },
  "retention": {
    "expire_after": "3d",
    "min_count": 2,
    "max_count": 5
  }
}
JSON

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
MODE="install"
ENV="prod"
while [ $# -gt 0 ]; do
    case "$1" in
        --check)   MODE="check"; shift ;;
        --dry-run) MODE="dry-run"; shift ;;
        --env)     ENV="${2:-}"; shift 2 ;;
        --env=*)   ENV="${1#*=}"; shift ;;
        -h|--help) usage 0 ;;
        *)         error "Unknown argument: $1 (usage: $0 [--env dev|prod] [--check|--dry-run])" ;;
    esac
done
case "$ENV" in dev|prod) ;; *) error "--env must be dev or prod (got: $ENV)" ;; esac

# ── The policy body: the template plus the private index list ────────────────
# Built before any mode runs, so --dry-run, --check and install all refuse the
# same way when the list is missing or malformed. An install must never PUT a
# policy with an empty or partial list: that stops tenants' backups with
# every deploy step green.
command -v python3 >/dev/null 2>&1 || error "python3 is required."
[ -f "$INDICES_FILE" ] || error "The SLM index list is missing: ${INDICES_FILE}
          It names mission teams, so it is export-ignored and never ships in the
          public release. A server's private checkout carries it — restore it from git."
if ! POLICY_BODY=$(SLM_TEMPLATE="$POLICY_TEMPLATE" python3 - "$INDICES_FILE" <<'PY'
import json, os, re, sys

# One pattern per line. `#` starts a comment at the start of a line or after
# whitespace; a `#` glued to a pattern stays in it and fails the check below
# (it is illegal in an index name) rather than silently shortening it.
# Index names are lowercase with no \ / ? " < > | , or spaces; `*` is the
# wildcard and a leading `-` an exclusion. An INCLUDE may not start with `.`:
# it would reach hidden dot-prefixed indices (.internal.alerts-*), which this
# policy omits on purpose. An EXCLUSION may, and the dot must be followed by a
# name character or `*`: a bare `-.` excludes nothing (no index is named `.`)
# and would let `*` carry the hidden indices along while looking like `-.*`.
PATTERN = re.compile(r"(?:-\.|-?)[a-z0-9*][a-z0-9_.+*-]*")
COMMENT = re.compile(r"(?:^|\s)#.*")

# utf-8-sig: a byte-order mark from a Windows editor is not part of line 1.
try:
    with open(sys.argv[1], encoding="utf-8-sig") as fh:
        lines = fh.readlines()
except UnicodeDecodeError as exc:
    print(f"it is not UTF-8 text (bad byte at offset {exc.start})")
    sys.exit(1)

indices = []
first_exclusion = None
for lineno, raw in enumerate(lines, 1):
    entry = COMMENT.sub("", raw).strip()
    if not entry:
        continue
    if not PATTERN.fullmatch(entry):
        print(f"line {lineno}: {entry!r} is not an index pattern this policy accepts")
        sys.exit(1)
    # Elasticsearch applies the list in order: an exclusion removes what the
    # patterns before it matched, and a later include can add it back. So the
    # exclusions go last, where prod has them.
    if entry.startswith("-"):
        first_exclusion = first_exclusion or lineno
    elif first_exclusion:
        print(f"line {lineno}: {entry!r} follows the exclusion on line {first_exclusion}; keep every exclusion at the end")
        sys.exit(1)
    indices.append(entry)
if not any(not entry.startswith("-") for entry in indices):
    print("it lists no index pattern to snapshot (only comments or exclusions)")
    sys.exit(1)

policy = json.loads(os.environ["SLM_TEMPLATE"])
policy["config"]["indices"] = indices
print(json.dumps(policy, indent=2))
PY
); then
    error "Cannot build the SLM policy from ${INDICES_FILE}: ${POLICY_BODY}"
fi

if [[ "$MODE" == "dry-run" ]]; then
    info "Dry run — desired snapshot repository '$REPO_NAME':"
    echo "$REPO_BODY"
    info "Dry run — desired SLM policy '$POLICY_ID':"
    echo "$POLICY_BODY"
    exit 0
fi

# ── Credentials (never printed) ───────────────────────────────────────────────
ELASTIC_PASS="${ES_ADMIN_PASS:-}"
if [ -z "$ELASTIC_PASS" ] && [ -f "$MASTER_CONFIG" ] && command -v yq >/dev/null 2>&1; then
    ELASTIC_PASS=$(yq e '.elk-01.es01.admin_pass' "$MASTER_CONFIG" 2>/dev/null || true)
    if [ "$ELASTIC_PASS" = "null" ]; then ELASTIC_PASS=""; fi
fi
if [ -z "$ELASTIC_PASS" ]; then
    # Prod's master-config is stripped: fetch from SSM with
    # the box's own reader. `VAR=x source` prefix assignments are honored.
    case "$ENV" in
        prod) SSM_ENV=prod SSM_PROFILE="" SSM_SUDO=1 source "$SCRIPT_DIR/lib/source-ssm-secrets.sh" || true ;;
        dev)  SSM_ENV=staging SSM_PROFILE=bootstrap SSM_SUDO=1 source "$SCRIPT_DIR/lib/source-ssm-secrets.sh" || true ;;
    esac
    ELASTIC_PASS="${ES_ADMIN_PASS:-}"
fi
[ -n "$ELASTIC_PASS" ] || error "No elastic password: export ES_ADMIN_PASS (source deployment/lib/source-ssm-secrets.sh) or populate config/master-config.yaml"

# ── ES REST helpers ───────────────────────────────────────────────────────────
ES_BODY="$(mktemp)"
trap 'rm -f "$ES_BODY"' EXIT
ES_CODE=""

es_get() { # $1: path — body lands in $ES_BODY, HTTP status in $ES_CODE
    local out
    out=$(curl -sk -u "elastic:${ELASTIC_PASS}" -w $'\n%{http_code}' "${ES_URL}$1" 2>/dev/null) || out=$'\n000'
    ES_CODE="${out##*$'\n'}"
    printf '%s' "${out%$'\n'*}" > "$ES_BODY"
}

es_put() { # $1: path, $2: JSON body
    local out
    out=$(curl -sk -u "elastic:${ELASTIC_PASS}" -X PUT \
        -H 'Content-Type: application/json' -d "$2" \
        -w $'\n%{http_code}' "${ES_URL}$1" 2>/dev/null) || out=$'\n000'
    ES_CODE="${out##*$'\n'}"
    printf '%s' "${out%$'\n'*}" > "$ES_BODY"
}

es_extract() { # $1: JSON file, $2..: key path — prints the sub-object
    python3 -c '
import json, sys
d = json.load(open(sys.argv[1]))
for k in sys.argv[2:]:
    d = d[k]
print(json.dumps(d))
' "$@"
}

# Field-level semantic comparison. A relative fs `location` resolves against
# path.repo to the same directory as the absolute form, so it is normalized
# before comparing rather than reported as a cosmetic drift.
semantic_diff() { # $1: label, $2: desired JSON string, $3: live JSON file
    SLM_DESIRED="$2" python3 - "$1" "$3" <<'PY'
import json, os, sys

label, live_path = sys.argv[1], sys.argv[2]
desired = json.loads(os.environ["SLM_DESIRED"])
live = json.load(open(live_path))
PATH_REPO = "/usr/share/elasticsearch/snapshots"

# The cluster assigns a repository `uuid` on registration and returns it on
# every GET; it cannot be part of a PUT body, so it is never drift.
live.pop("uuid", None)

for obj in (desired, live):
    settings = obj.get("settings")
    if isinstance(settings, dict):
        loc = settings.get("location")
        if isinstance(loc, str) and loc and not loc.startswith("/"):
            settings["location"] = f"{PATH_REPO}/{loc}"

def diff(d, l, path=""):
    lines = []
    if isinstance(d, dict) and isinstance(l, dict):
        for key in sorted(set(d) | set(l)):
            p = f"{path}.{key}" if path else key
            if key not in d:
                lines.append(f"    {p}: live has {json.dumps(l[key])}, this script defines nothing")
            elif key not in l:
                lines.append(f"    {p}: this script defines {json.dumps(d[key])}, live has nothing")
            else:
                lines.extend(diff(d[key], l[key], p))
    elif d != l:
        lines.append(f"    {path}: this script defines {json.dumps(d)}, live has {json.dumps(l)}")
    return lines

drift = diff(desired, live)
if drift:
    print(f"  {label}:")
    print("\n".join(drift))
    sys.exit(1)
PY
}

fetch_live() { # $1: what (repo|policy) — live body lands in the named file
    case "$1" in
        repo)
            es_get "/_snapshot/${REPO_NAME}"
            LIVE_FILE="$LIVE_REPO_FILE"; EXTRACT=("$REPO_NAME") ;;
        policy)
            es_get "/_slm/policy/${POLICY_ID}"
            LIVE_FILE="$LIVE_POLICY_FILE"; EXTRACT=("$POLICY_ID" "policy") ;;
    esac
    if [ "$ES_CODE" = "000" ]; then error "Cannot reach Elasticsearch at ${ES_URL}."; fi
    if [ "$ES_CODE" = "404" ]; then
        return 1
    fi
    [ "$ES_CODE" = "200" ] || error "GET for the $1 returned HTTP ${ES_CODE}: $(head -c 300 "$ES_BODY")"
    es_extract "$ES_BODY" "${EXTRACT[@]}" > "$LIVE_FILE"
    return 0
}

LIVE_REPO_FILE="$(mktemp)"
LIVE_POLICY_FILE="$(mktemp)"
trap 'rm -f "$ES_BODY" "$LIVE_REPO_FILE" "$LIVE_POLICY_FILE"' EXIT

case "$MODE" in
    check)
        # Read-only drift check. Provisioning is idempotent, so
        # the only post-condition worth asserting is that the cluster holds
        # the repo and policy this script defines.
        MISSING=""
        fetch_live repo   || MISSING="repository ${REPO_NAME}"
        fetch_live policy || MISSING="${MISSING:+$MISSING and }policy ${POLICY_ID}"
        if [ -n "$MISSING" ]; then
            error "The ${MISSING} has never been provisioned on this cluster (staging's state ).
          Fix: bash ${BASH_SOURCE[0]} --env ${ENV}"
        fi
        DRIFT_REPORT=""
        if ! out=$(semantic_diff "repository ${REPO_NAME}" "$REPO_BODY" "$LIVE_REPO_FILE"); then
            DRIFT_REPORT+="$out"$'\n'
        fi
        if ! out=$(semantic_diff "policy ${POLICY_ID}" "$POLICY_BODY" "$LIVE_POLICY_FILE"); then
            DRIFT_REPORT+="$out"$'\n'
        fi
        if [ -n "$DRIFT_REPORT" ]; then
            warn "DRIFT — the live SLM state differs from this script's definition:"
            printf '%s' "$DRIFT_REPORT"
            error "Live SLM state has DRIFTED from deployment/09_provision_slm.sh.
          On the FIRST prod check: prod is the evidence — update this script (or slm-indices.txt) to match it.
          Anywhere else: bash ${BASH_SOURCE[0]} --env ${ENV}"
        fi
        success "Live SLM state matches this script (repository ${REPO_NAME} + policy ${POLICY_ID})."
        ;;
    install)
        info "Registering snapshot repository '${REPO_NAME}' (ES verifies the location is writable)..."
        es_put "/_snapshot/${REPO_NAME}" "$REPO_BODY"
        if [ "$ES_CODE" = "000" ]; then error "Cannot reach Elasticsearch at ${ES_URL}."; fi
        [ "$ES_CODE" = "200" ] || error "Repository registration returned HTTP ${ES_CODE}: $(head -c 300 "$ES_BODY")"
        success "Repository '${REPO_NAME}' registered."

        info "Installing SLM policy '${POLICY_ID}'..."
        es_put "/_slm/policy/${POLICY_ID}" "$POLICY_BODY"
        [ "$ES_CODE" = "200" ] || error "Policy install returned HTTP ${ES_CODE}: $(head -c 300 "$ES_BODY")"
        success "Policy '${POLICY_ID}' installed (<daily-snap-{now/d}> at 02:30 UTC, expire_after 3d)."
        ;;
esac
