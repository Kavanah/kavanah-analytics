#!/bin/bash
# Rotate INTERNAL_REPO_TOKEN into BOTH GitHub secret stores for this repo.
#
# The pytest and release-plan workflows check out the private `internal`
# submodule with `token: ${{ secrets.INTERNAL_REPO_TOKEN }}` — the default
# GITHUB_TOKEN can't reach another private repo. Actions and Dependabot keep
# SEPARATE encrypted secret stores that do not share values, so the token has
# to be written twice. Updating only the Actions copy leaves Dependabot PRs
# failing at checkout while normal dev pushes stay green — a confusing split
# that this script exists to prevent.
#
# This script does NOT mint the token. Creating a PAT requires an interactive
# browser session by design. Generate it first at:
#   github.com -> Settings -> Developer settings -> Personal access tokens
# Fine-grained is preferred: grant `Contents: read` on BOTH
# Kavanah/digital-engagement-internal and Kavanah/digital-engagement-dashboard,
# with Resource owner = the Kavanah organization. A fine-grained PAT is bound
# to the org that owns the repos when it is minted — the 2026-08-24 transfer
# from Team-Expansion to Kavanah killed the previous token for exactly that
# reason (km-infra gotcha #46a), so re-mint rather than re-scope after any
# future org move. If the org hasn't opted into fine-grained PATs the token
# will generate but 403 on use — fall back to a classic PAT with `repo` scope.
#
# Usage: bash bin/scripts/rotate-internal-repo-token.sh
#        bash bin/scripts/rotate-internal-repo-token.sh --verify-only

set -e

repo="Kavanah/digital-engagement-dashboard"
secret_name="INTERNAL_REPO_TOKEN"

echo "=== Rotate $secret_name ==="
echo "Repo: $repo"
echo ""

# gh must be present and authenticated as someone who can write repo secrets.
if ! command -v gh >/dev/null 2>&1; then
    echo "ERROR: gh CLI not found. Install from https://cli.github.com/"
    exit 1
fi

if ! gh auth status >/dev/null 2>&1; then
    echo "ERROR: gh is not authenticated. Run: gh auth login"
    exit 1
fi

# Show current state before touching anything. Values are never retrievable —
# only the last-updated timestamp — so this is the only "before" signal there is.
echo "Current secret timestamps:"
echo "  Actions:"
gh secret list -R "$repo" 2>/dev/null | grep "^$secret_name" | sed 's/^/    /' \
    || echo "    <not set>"
echo "  Dependabot:"
gh secret list -R "$repo" --app dependabot 2>/dev/null | grep "^$secret_name" | sed 's/^/    /' \
    || echo "    <not set>"
echo ""

if [ "$1" = "--verify-only" ]; then
    echo "--verify-only given; not writing. Done."
    exit 0
fi

# Read the token without echoing it and without it landing in shell history.
# -r so backslashes survive; -s so the paste stays off-screen.
printf 'Paste the new token (input hidden), then press Enter: '
read -rs new_token
echo ""

if [ -z "$new_token" ]; then
    echo "ERROR: empty token; nothing written."
    exit 1
fi

# Sanity-check the shape before spending two API calls on an obvious typo.
# ghp_ = classic, github_pat_ = fine-grained. Warn rather than block: GitHub
# has changed these prefixes before and a hard fail would age badly.
case "$new_token" in
    ghp_*)        echo "Token looks like a classic PAT." ;;
    github_pat_*) echo "Token looks like a fine-grained PAT." ;;
    *)            echo "WARNING: token doesn't match a known GitHub PAT prefix." ;;
esac
echo ""

# Write both stores. `set -e` aborts on the first failure — but a failure on
# the Dependabot write after the Actions write succeeded leaves the two stores
# split, which is exactly the state this script prevents, so say so loudly.
echo "Writing Actions secret..."
printf '%s' "$new_token" | gh secret set "$secret_name" -R "$repo"

echo "Writing Dependabot secret..."
if ! printf '%s' "$new_token" | gh secret set "$secret_name" -R "$repo" --app dependabot; then
    echo ""
    echo "ERROR: Dependabot write FAILED after the Actions write SUCCEEDED."
    echo "       The two stores are now out of sync. Dependabot PRs will fail"
    echo "       checkout until you re-run this script successfully."
    unset new_token
    exit 1
fi

unset new_token
echo ""

# Verify: re-list and confirm both timestamps moved to today.
echo "=== Verification ==="
today=$(date -u +%Y-%m-%d)
actions_ts=$(gh secret list -R "$repo" 2>/dev/null | grep "^$secret_name" | awk '{print $2}')
dependabot_ts=$(gh secret list -R "$repo" --app dependabot 2>/dev/null | grep "^$secret_name" | awk '{print $2}')

echo "  Actions:    $actions_ts"
echo "  Dependabot: $dependabot_ts"
echo ""

ok=true
case "$actions_ts" in
    "$today"*) ;;
    *) echo "WARNING: Actions timestamp is not today ($today)."; ok=false ;;
esac
case "$dependabot_ts" in
    "$today"*) ;;
    *) echo "WARNING: Dependabot timestamp is not today ($today)."; ok=false ;;
esac

if [ "$ok" != true ]; then
    echo "Rotation may not have applied. Re-check with:"
    echo "  bash bin/scripts/rotate-internal-repo-token.sh --verify-only"
    exit 1
fi

echo "Both stores updated."
echo ""
echo "Next: confirm CI actually accepts the new token — a bad scope fails at"
echo "the submodule checkout, not at the write above."
echo "  gh workflow run pytest.yml -R $repo --ref dev"
echo "  gh run list -R $repo --workflow pytest.yml --limit 1"
echo ""
echo "Then revoke the OLD token at:"
echo "  github.com -> Settings -> Developer settings -> Personal access tokens"
echo "Revoke only the PAT that previously backed $secret_name (since 2026-09-15"
echo "that is the fine-grained token named 'internal-repo-token-ci'). Leave every"
echo "other PAT alone — e.g. 'deam-ci-scan-status' backs a different workflow and"
echo "nothing here references it."
echo ""
echo "Done!"
