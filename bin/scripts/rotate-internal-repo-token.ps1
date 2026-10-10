# Rotate INTERNAL_REPO_TOKEN into BOTH GitHub secret stores for this repo.
#
# PowerShell twin of rotate-internal-repo-token.sh. Same behavior; exists
# because this is a developer-workstation tool and the developers are on
# Windows. The .sh version shells out to `read -rs` for hidden input, which
# is unreliable when bash is invoked from a PowerShell host (no guaranteed
# TTY — the token can echo to the screen). Read-Host -AsSecureString has no
# such ambiguity. Prefer this file on Windows; the .sh on Linux/macOS.
#
# The pytest and release-plan workflows check out the private `internal`
# submodule with `token: ${{ secrets.INTERNAL_REPO_TOKEN }}` — the default
# GITHUB_TOKEN can't reach another private repo. Actions and Dependabot keep
# SEPARATE encrypted secret stores that do not share values, so the token has
# to be written twice. Updating only the Actions copy leaves Dependabot PRs
# failing at checkout while normal dev pushes stay green.
#
# This script does NOT mint the token. Generate it first at:
#   github.com -> Settings -> Developer settings -> Personal access tokens
# Classic needs `repo`. Fine-grained needs `Contents: read` on BOTH
# Kavanah/digital-engagement-internal and Kavanah/digital-engagement-dashboard,
# with Resource owner = the Kavanah organization (a fine-grained PAT is bound
# to the org that owned the repos when it was minted; the 2026-08-24 org
# transfer killed the previous token for that reason — re-mint after any move).
#
# Usage: .\bin\scripts\rotate-internal-repo-token.ps1
#        .\bin\scripts\rotate-internal-repo-token.ps1 -VerifyOnly

param(
    [switch]$VerifyOnly
)

$ErrorActionPreference = 'Stop'

$repo = 'Kavanah/digital-engagement-dashboard'
$secretName = 'INTERNAL_REPO_TOKEN'

Write-Host "=== Rotate $secretName ==="
Write-Host "Repo: $repo"
Write-Host ""

# gh must be present and authenticated as someone who can write repo secrets.
if (-not (Get-Command gh -ErrorAction SilentlyContinue)) {
    Write-Host "ERROR: gh CLI not found. Install from https://cli.github.com/"
    exit 1
}

gh auth status *> $null
if ($LASTEXITCODE -ne 0) {
    Write-Host "ERROR: gh is not authenticated. Run: gh auth login"
    exit 1
}

# Show current state before touching anything. Secret values are never
# retrievable — only the last-updated timestamp — so this is the only
# "before" signal available.
function Get-SecretTimestamp {
    param([string]$App)

    $listArgs = @('secret', 'list', '-R', $repo)
    if ($App) { $listArgs += @('--app', $App) }

    $rows = & gh @listArgs 2>$null
    if ($LASTEXITCODE -ne 0) { return $null }

    foreach ($row in $rows) {
        $cols = $row -split '\s+'
        if ($cols[0] -eq $secretName) { return $cols[1] }
    }
    return $null
}

$beforeActions = Get-SecretTimestamp
$beforeDependabot = Get-SecretTimestamp -App 'dependabot'

Write-Host "Current secret timestamps:"
Write-Host ("  Actions:    " + $(if ($beforeActions) { $beforeActions } else { '<not set>' }))
Write-Host ("  Dependabot: " + $(if ($beforeDependabot) { $beforeDependabot } else { '<not set>' }))
Write-Host ""

if ($VerifyOnly) {
    Write-Host "-VerifyOnly given; not writing. Done."
    exit 0
}

# Read the token without echoing it. -AsSecureString keeps it off the screen
# and out of PSReadLine history; the plaintext is unwrapped only in-memory
# and cleared in the finally block below.
$secure = Read-Host -Prompt 'Paste the new token (input hidden)' -AsSecureString

$bstr = [IntPtr]::Zero
$newToken = $null
try {
    $bstr = [Runtime.InteropServices.Marshal]::SecureStringToBSTR($secure)
    $newToken = [Runtime.InteropServices.Marshal]::PtrToStringBSTR($bstr)

    if ([string]::IsNullOrWhiteSpace($newToken)) {
        Write-Host "ERROR: empty token; nothing written."
        exit 1
    }

    # Sanity-check the shape before spending two API calls on an obvious typo.
    # Warn rather than block: GitHub has changed these prefixes before and a
    # hard fail would age badly.
    if ($newToken.StartsWith('ghp_')) {
        Write-Host "Token looks like a classic PAT."
    } elseif ($newToken.StartsWith('github_pat_')) {
        Write-Host "Token looks like a fine-grained PAT."
    } else {
        Write-Host "WARNING: token doesn't match a known GitHub PAT prefix."
    }
    Write-Host ""

    # Write both stores. `gh secret set` reads the value from stdin when
    # --body is omitted, which keeps the token off the process command line
    # (otherwise visible to other users via the process table). Do NOT pass
    # `--body -`: --body takes a literal string, so that would store "-".
    Write-Host "Writing Actions secret..."
    $newToken | & gh secret set $secretName -R $repo
    if ($LASTEXITCODE -ne 0) {
        Write-Host "ERROR: Actions write failed; Dependabot not attempted."
        exit 1
    }

    Write-Host "Writing Dependabot secret..."
    $newToken | & gh secret set $secretName -R $repo --app dependabot
    if ($LASTEXITCODE -ne 0) {
        Write-Host ""
        Write-Host "ERROR: Dependabot write FAILED after the Actions write SUCCEEDED."
        Write-Host "       The two stores are now out of sync. Dependabot PRs will fail"
        Write-Host "       checkout until you re-run this script successfully."
        exit 1
    }
}
finally {
    # Zero the unmanaged copy and drop the managed one regardless of outcome.
    if ($bstr -ne [IntPtr]::Zero) {
        [Runtime.InteropServices.Marshal]::ZeroFreeBSTR($bstr)
    }
    Remove-Variable -Name newToken -ErrorAction SilentlyContinue
}

Write-Host ""

# Verify: re-list and confirm both timestamps moved to today.
Write-Host "=== Verification ==="
$today = (Get-Date).ToUniversalTime().ToString('yyyy-MM-dd')
$afterActions = Get-SecretTimestamp
$afterDependabot = Get-SecretTimestamp -App 'dependabot'

Write-Host "  Actions:    $afterActions"
Write-Host "  Dependabot: $afterDependabot"
Write-Host ""

$ok = $true
if (-not $afterActions -or -not $afterActions.StartsWith($today)) {
    Write-Host "WARNING: Actions timestamp is not today ($today)."
    $ok = $false
}
if (-not $afterDependabot -or -not $afterDependabot.StartsWith($today)) {
    Write-Host "WARNING: Dependabot timestamp is not today ($today)."
    $ok = $false
}

if (-not $ok) {
    Write-Host "Rotation may not have applied. Re-check with:"
    Write-Host "  .\bin\scripts\rotate-internal-repo-token.ps1 -VerifyOnly"
    exit 1
}

Write-Host "Both stores updated."
Write-Host ""
Write-Host "Next: confirm CI actually accepts the new token — a bad scope fails at"
Write-Host "the submodule checkout, not at the write above."
Write-Host "  gh workflow run pytest.yml -R $repo --ref dev"
Write-Host "  gh run list -R $repo --workflow pytest.yml --limit 1"
Write-Host ""
Write-Host "Done!"
