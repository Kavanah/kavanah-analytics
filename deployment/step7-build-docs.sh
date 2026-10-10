#!/bin/bash
# Step 7: Build Sphinx docs into docs/documentation/_build/html
# nginx (step6) mounts that directory at /var/www/deam-docs so the site
# serves it at /docs/.
#
# Idempotent — safe to re-run after each `git pull` to pick up doc updates.
#
# no longer a remembered first-install step. Both release wrappers
# call this after recreating imports_container, because `git pull` updates the
# docs SOURCE and changes nothing about what /docs/ serves — the served copy is
# a gitignored build directory. Prod drifted two weeks and four doc commits
# that way, and the drift is not symmetric: a DELETED page or image keeps being
# served, which is how redacted screenshots stayed publicly readable
# after they were removed from the repo.
#
# Usage:
#   bash deployment/step7-build-docs.sh            # build (and stamp)
#   bash deployment/step7-build-docs.sh --check    # verify the served build
#                                                  # matches the current tree
set -euo pipefail

MODE="build"
case "${1:-}" in
    --check) MODE="check" ;;
    "")      ;;
    *) echo "[FAIL] Unknown argument: $1 (usage: $0 [--check])" >&2; exit 2 ;;
esac

# honour REPO_DIR like the deploy wrappers do. Hardcoding
# $HOME/kavanah-analytics meant this could not run on staging (~/DE-Reporting)
# and resolved to /root under sudo — both silent failures for a script whose
# whole job is keeping a directory fresh.
DERS_HOME="${REPO_DIR:-$HOME/kavanah-analytics}"
DOCS_SRC="$DERS_HOME/docs/documentation"
DOCS_REQS="$DOCS_SRC/requirements.txt"
DOCS_OUT="$DOCS_SRC/_build/html"
# Kept OUTSIDE $DOCS_OUT: the build wipes that directory, and anything inside it
# is served publicly at /docs/. The stamp records a commit SHA, so it stays on
# the host where only a post-deploy check reads it.
DOCS_STAMP="$DOCS_SRC/_build/.docs-build-stamp"
# Beside the repo rather than under $HOME, so a sudo invocation does not build a
# second venv at /root/docs-venv and leave the real one stale.
VENV_DIR="${DOCS_VENV_DIR:-$(dirname "$DERS_HOME")/docs-venv}"

GREEN='\033[0;32m'
RED='\033[0;31m'
YELLOW='\033[1;33m'
NC='\033[0m'

ok()   { echo -e "${GREEN}[OK]${NC} $*"; }
warn() { echo -e "${YELLOW}[WARN]${NC} $*"; }
die()  { echo -e "${RED}[FAIL]${NC} $*"; exit 1; }

# ── Sanity checks ────────────────────────────────────────────────────────────
[[ -d "$DOCS_SRC" ]] || die "Docs source not found at $DOCS_SRC — is this a full clone of the repo?"
[[ -f "$DOCS_REQS" ]] || die "Docs requirements.txt not found at $DOCS_REQS"

# ── Build stamp ───────────────────────────────────────────────────
# The served tree is gitignored, so nothing about it is visible in a git diff
# and staleness is silent. The stamp records which commit produced the build so
# a post-deploy check can compare it against the tree it claims to document.
tree_commit() {
    git -C "$DERS_HOME" rev-parse HEAD 2>/dev/null || echo "unknown"
}

if [[ "$MODE" == "check" ]]; then
    # Standalone verification: run after a deploy, or any time you want to know
    # whether /docs/ reflects the checkout. Exits non-zero when it does not, so
    # a runbook step or CI check fails loudly instead of drifting quietly.
    [[ -f "$DOCS_OUT/index.html" ]] || die "No built docs at $DOCS_OUT — /docs/ is serving nothing or a stale mount. Run: bash deployment/step7-build-docs.sh"
    [[ -f "$DOCS_STAMP" ]] || die "No build stamp at $DOCS_STAMP — the served docs predate  and their provenance is unknown. Rebuild to establish it."

    stamped=$(sed -n 's/^commit=//p' "$DOCS_STAMP" | head -1)
    built_at=$(sed -n 's/^built_at=//p' "$DOCS_STAMP" | head -1)
    current=$(tree_commit)

    if [[ "$current" == "unknown" ]]; then
        warn "Not a git checkout ($DERS_HOME) — cannot compare. Stamp says commit=$stamped built_at=$built_at"
        exit 0
    fi
    if [[ "$stamped" != "$current" ]]; then
        die "Served docs are STALE — built from ${stamped:-<empty>} at ${built_at:-<unknown>}, tree is at ${current}.
       /docs/ still serves the older build, including any page or image deleted since.
       Fix: bash deployment/step7-build-docs.sh"
    fi
    ok "Served docs match the tree (commit ${current}, built ${built_at})"
    exit 0
fi

# ── Venv setup ───────────────────────────────────────────────────────────────
# Ubuntu 24 enforces PEP 668, so we install into a dedicated venv rather
# than fighting `--break-system-packages`.
if [[ ! -d "$VENV_DIR" ]]; then
    echo "Creating docs venv at $VENV_DIR..."
    python3 -m venv "$VENV_DIR"
    ok "Venv created"
fi

echo "Installing Sphinx + theme deps into $VENV_DIR..."
"$VENV_DIR/bin/pip" install --quiet --upgrade pip
"$VENV_DIR/bin/pip" install --quiet -r "$DOCS_REQS"
ok "Deps installed ($($VENV_DIR/bin/sphinx-build --version))"

# ── Build ────────────────────────────────────────────────────────────────────
# Clean prior output before building. Incremental builds can leave stale
# files behind — sub-pages don't get regenerated when only index.rst
# changes, and modified _static/ assets aren't always re-copied, so the
# site can drift from the source.
#
# NB: We wipe the *contents* of $DOCS_OUT rather than the directory
# itself. The nginx container bind-mounts $DOCS_OUT; removing the
# directory would orphan that mount to a stale inode (fixable only by
# restarting nginx). Clearing contents in place keeps the inode stable.
#
# the wipe fails confusingly if a previous run left files this user
# cannot delete — which happens the moment someone runs this under sudo and then
# without it (staging needs sudo today because deployment/.env is root-owned).
# Now that the rebuild is part of every deploy rather than a rare manual step,
# that mismatch would surface as a mid-deploy `find: cannot delete` with no
# explanation. Name it instead.
if [[ -d "$DOCS_OUT" ]]; then
    foreign=$(find "$DOCS_OUT" -mindepth 1 ! -user "$(id -un)" -print -quit 2>/dev/null || true)
    if [[ -n "$foreign" ]]; then
        die "$DOCS_OUT holds files owned by another user (e.g. $foreign) — this run is $(id -un).
       A previous build ran as a different user, so the tree cannot be cleaned.
       Fix: re-run as that user, or chown -R $(id -un) \"$DOCS_OUT\" first."
    fi
    find "$DOCS_OUT" -mindepth 1 -delete
fi

# -b dirhtml: generate clean URLs (page/index.html) so links read as
#   /docs/guide-dashboards/ rather than /docs/guide-dashboards/index.html.
# -E: discard the saved environment so every source file is re-read.
# -a: write all output files, not just those whose source changed.
# Together these force a full rebuild, including _static/ asset copies.
echo "Building HTML docs..."
"$VENV_DIR/bin/sphinx-build" -b dirhtml -q -E -a "$DOCS_SRC" "$DOCS_OUT"

# Verify output exists and has the expected files
[[ -f "$DOCS_OUT/index.html" ]] || die "Build finished but $DOCS_OUT/index.html is missing"
ok "Docs built at $DOCS_OUT"

# Stamp only after the build is verified, so a failed build never leaves a
# stamp claiming freshness it does not have.
{
    echo "# Written by deployment/step7-build-docs.sh. Do not edit."
    echo "commit=$(tree_commit)"
    echo "built_at=$(date -u +'%Y-%m-%dT%H:%M:%SZ')"
} > "$DOCS_STAMP"
ok "Build stamped: commit=$(tree_commit)"

# ── nginx pickup ─────────────────────────────────────────────────────────────
# If nginx is already running and was started by step6 without the docs mount,
# the new HTML won't be visible inside the container. A reload doesn't help —
# the mount has to be added by re-running step6 (or the admin recreates the
# container by hand). Flag that explicitly.
if docker ps --filter name=nginx-reverse-proxy --filter status=running --format '{{.Names}}' | grep -q nginx-reverse-proxy; then
    if docker inspect nginx-reverse-proxy --format '{{range .Mounts}}{{.Destination}}{{println}}{{end}}' | grep -q '^/var/www/deam-docs$'; then
        echo "nginx already has /var/www/deam-docs mounted — docs are live now."
    else
        warn "nginx is running but doesn't have /var/www/deam-docs mounted."
        warn "Re-run step6-nginx-setup.sh to pick up the new mount, or recreate the container by hand."
    fi
fi

echo ""
ok "Step 7 complete."
echo "    Source:  $DOCS_SRC"
echo "    Output:  $DOCS_OUT"
echo "    Serve:   https://<your-host>/docs/"
echo "    Rebuild: bash deployment/step7-build-docs.sh (re-run anytime)"
echo "    Verify:  bash deployment/step7-build-docs.sh --check"
