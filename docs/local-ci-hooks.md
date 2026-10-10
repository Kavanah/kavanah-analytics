# Local CI hooks

This repo runs four pre-commit hooks on every `git commit`: black, flake8,
ESLint, and (added 2026-05-13) **gitleaks** + a **debug-pattern scan**.

The last two mirror the cloud workflows in `.github/workflows/gitleaks.yml`
and the manual grep that `the private docs` §3 prescribes —
they exist so we still get a secrets gate and a debug-statement gate even
when the self-hosted `ubuntu-1` runner is offline (which is what
hosts those two cloud workflows).

> Rotating the CI submodule token (`INTERNAL_REPO_TOKEN`, listed on GitHub as
> "pytest and Dependabot token")? See the private rotating-internal-repo-token
> notes — it has to be written to **two** separate secret stores, and there's a
> self-hosted runner failure mode that mimics a bad token.

## First-time setup

You need three things on PATH: `pre-commit`, `gitleaks`, and a bash
interpreter (Git Bash on Windows is fine).

### Windows

```powershell
# 1) pre-commit (if you don't already have it)
pip install pre-commit

# 2) gitleaks
winget install Gitleaks.Gitleaks

# 3) Install the git hook into this repo's .git/hooks/
pre-commit install
```

**One more thing on Windows:** the `debug-pattern-scan` hook runs via
`bash`, which ships with Git but isn't on PATH by default — only
`C:\Program Files\Git\cmd` is. Add `C:\Program Files\Git\bin` to your
**user** PATH (Settings → Environment Variables → User → Path → New),
then open a new shell. Without this, the hook fails on first commit
with `Executable bash not found` before it scans anything. See the
Troubleshooting section below.

### macOS / Linux

```bash
pip install pre-commit
brew install gitleaks      # or: apt install gitleaks
pre-commit install
```

That's it. Every subsequent `git commit` will run black + flake8 + ESLint +
gitleaks + debug-pattern-scan on the staged diff.

## What each hook does

| Hook | Stage | What it catches |
|------|-------|-----------------|
| `black` | pre-commit | Python formatting drift in `bin/python/` |
| `flake8` | pre-commit | Python lint errors in `bin/python/` |
| `eslint` | pre-commit | JS lint errors in `bin/python/asset_management/web/static/` |
| `gitleaks` | pre-commit | Real-looking secrets (AWS keys, Stripe tokens, generic high-entropy strings) |
| `debug-pattern-scan` | pre-commit | `console.log(`, `debugger;`, `print(`, `fmt.Print*(`, `.env` references in added lines |

Gitleaks honors `.gitleaks.toml` at the repo root — that's where the
documentation-placeholder allowlists live. If a real value is being
flagged that you can't change, add it to the `regexes` allowlist there
(not to `.gitleaksignore` — that's per-finding-fingerprint and brittle).

## Bypass (use sparingly)

If you absolutely need to commit through a hook (e.g. you're committing a
legitimate `print()` in a CLI script), skip just that one hook:

```bash
SKIP=debug-pattern-scan git commit -m "feat(cli): add --verbose output"
```

Never `--no-verify`. That bypasses every hook and is a documented red
flag in the project rules — if a hook is wrong, fix the hook, don't
silence the whole bar.

## Why local, not cloud

Today this is also enforced cloud-side via
`.github/workflows/gitleaks.yml` and `.github/workflows/pre-commit.yml`,
but both run on `[self-hosted, ubuntu-1]`. When that runner is offline
those workflows sit in "Waiting for a runner" forever and PR checks never
go green. Local hooks keep the gate working — and they catch secrets
*before* the secret enters the git history, which is the only place
remediation is cheap. Once the secret is pushed, it has to be rotated
even if the commit is reverted.

## Troubleshooting

**`gitleaks: command not found`** — gitleaks isn't on PATH. Reinstall
(see "First-time setup") and restart your terminal so the new PATH is
picked up.

**`pre-commit: command not found`** — same fix with pre-commit.

**`Executable bash not found`** (Windows) — the debug-pattern-scan hook
shells out to `bash`, which ships with Git but isn't on PATH by default.
Add `C:\Program Files\Git\bin` to your user PATH and open a new shell.

**A hook is wrongly blocking** — first, double-check it's actually wrong.
If it is, the right fix depends on which hook:
- gitleaks false positive → add the literal value to `.gitleaks.toml`
  `[[allowlists]].regexes` and commit that change first.
- debug-pattern-scan false positive → either rephrase the offending line,
  or if it's a legitimate `print()` etc., `SKIP=debug-pattern-scan` for
  the one commit.

**Hook hangs / takes forever** — gitleaks shouldn't take more than ~2s
on a normal-sized diff. If it's stuck, you may be on an old version
(`gitleaks version` should be 8.18+); update via your package manager.
