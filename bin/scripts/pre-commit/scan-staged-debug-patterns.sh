#!/usr/bin/env bash
# scan-staged-debug-patterns.sh
#
# Pre-commit hook that mirrors the manual grep recommended in
# the private docs
#
#   "Before every commit, scan staged changes for common mistakes:
#    git diff --staged | grep -E '(console\.log|fmt\.Print|debugger|print\(|TODO|FIXME|\.env)'"
#
# We deviate from the rule by NOT failing on TODO/FIXME — both legitimately
# appear in committed code (e.g. release-plan stubs reference TODO items).
# The point of the gate is to catch leftover debug calls and accidental
# .env exposure, not lint code-comment vocabulary.
#
# Exit 0 — clean.  Exit 1 — patterns found, commit blocked.

set -euo pipefail

# Patterns that almost certainly should not land in a commit. Each is
# matched as an extended-regex against added (`+`) lines in the staged
# diff.
PATTERNS=(
  'console\.log\('
  'debugger;'
  '(^|[^a-zA-Z_])print\('     # Python print() — excludes pprint, fprint
  # was '(ln|f|)?' whose empty alternative is an ERE error on BSD
  # grep ("empty (sub)expression", exit 2 silently eaten by || true), so the
  # Go pattern never actually matched on macOS. '(ln|f)?' is the same set.
  'fmt\.Print(ln|f)?\('        # Go fmt.Print*
)

# the old '\.env(...)' BODY pattern flagged every literal ".env"
# string in code/comments/tests (Phase 2 hit 25 false positives and
# had to SKIP the hook wholesale, disabling the real checks too). The thing
# actually worth blocking is a staged FILE whose path is a .env secrets file;
# gitleaks covers leaked VALUES. Scan the staged file list instead.
staged_env_files=$(
  git diff --staged --name-only --diff-filter=d \
  | grep -E '(^|/)\.env(\.[A-Za-z0-9_.-]+)?$' \
  | grep -Ev '\.(example|sample|template|dist)$' || true
)
if [ -n "$staged_env_files" ]; then
  echo "Debug-pattern scan: a .env secrets file is staged for commit:" >&2
  printf '%s\n' "$staged_env_files" >&2
  echo "Never commit .env files — unstage it (git restore --staged <file>) and add it to .gitignore." >&2
  echo "If it already contains real secrets, treat this as a leak: rotate them (the private docs §6)." >&2
  exit 1
fi

# Look at only ADDED lines in the staged diff. Removing a debug print is
# fine — adding one is not. The `^+` filter excludes the `+++` header line.
#
# Exclude files that legitimately reference these patterns as data
# (the hook script itself and its YAML wiring define the regexes
# they're scanning for).
staged_added=$(
  git diff --staged --no-color -- \
    ':!bin/scripts/pre-commit/scan-staged-debug-patterns.sh' \
    ':!.pre-commit-config.yaml' \
    ':!docs/local-ci-hooks.md' \
    ':!the release-plan tests' \
  | grep -E '^\+[^+]' || true
)

if [ -z "$staged_added" ]; then
  exit 0
fi

hit=0
for pat in "${PATTERNS[@]}"; do
  matches=$(printf '%s\n' "$staged_added" | grep -E -n "$pat" || true)
  if [ -n "$matches" ]; then
    if [ $hit -eq 0 ]; then
      echo "Debug-pattern scan: staged diff contains suspicious lines." >&2
      echo "(Pre-commit hook from the private docs §3.)" >&2
      hit=1
    fi
    echo "--- pattern: $pat" >&2
    printf '%s\n' "$matches" >&2
  fi
done

if [ $hit -eq 1 ]; then
  echo "" >&2
  echo "If any match is a false positive, remove the staged line, re-stage, and recommit." >&2
  echo "To bypass intentionally (e.g. committing a legitimate print() in a script):" >&2
  echo "    SKIP=debug-pattern-scan git commit ..." >&2
  exit 1
fi

exit 0
