"""Verify sanitized demo captures and publish them into the docs tree.

Second stage of the docs-screenshot pipeline. The first stage
(`npx playwright test --project=docs-capture`, see
tests/e2e/docs-capture/) logs into prod as the demo account and writes,
for every entry in tests/e2e/docs-capture/capture-manifest.json:

    .docs-capture/<slug>.png    the screenshot
    .docs-capture/<slug>.json   sidecar: visible page text + capture metadata

This tool is the ONLY thing that moves images into docs/. For each
sidecar it checks, fail-closed:

  1. the capture came from the expected demo username (kavanah-demo),
  2. every required marker from the manifest (e.g. "Kavanah Demo")
     appears in the page's visible text OR in the hidden
     ``data-docs-workspace`` anchor the sidecar captured — proof the demo
     workspace was the active context (some pages, e.g. /dashboard, carry
     no visible workspace-name text, so the anchor is their marker source).
     ``required_markers`` is mandatory on every manifest entry — a missing
     key or an empty list is a hard manifest-load error, not a silent pass
     ,
  3. no term from the local denylist of real tenant/org/team names
     appears in the VISIBLE page text (the leak check is deliberately
     scoped to what is actually shown in the screenshot). The denylist lives at
     .docs-capture-denylist.txt (gitignored — it IS customer data; see
     the runbook for how to regenerate it from prod ES).

Any violation on ANY capture publishes NOTHING (a cross-tenant leak on
one page means the account scoping is wrong for all of them). A missing
capture (entry in the manifest but no artifacts on disk) only skips that
entry, publishes the clean ones, and exits 2 so the run is visibly
partial.

Exit codes: 0 = all published, 1 = sanitization violation (nothing
published), 2 = partial (some manifest entries had no capture).

Usage (from the repo root):
    python -m bin.python.tools.publish_doc_captures
    python -m bin.python.tools.publish_doc_captures --dry-run
"""

from __future__ import annotations

import argparse
import json
import re
import shutil
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[3]
MANIFEST_PATH = REPO_ROOT / "tests" / "e2e" / "docs-capture" / "capture-manifest.json"
CAPTURE_DIR = REPO_ROOT / ".docs-capture"
DENYLIST_PATH = REPO_ROOT / ".docs-capture-denylist.txt"

# Names that legitimately appear in demo captures and must never be treated
# as leaks even if an operator pastes them into the denylist by mistake.
ALLOWED_NAMES = frozenset({"kavanah", "kavanah team", "kavanah-demo", "kavanah demo", "kavanah media"})

# Denylist terms shorter than this are too likely to be substrings of
# ordinary UI copy to be meaningful leak signals.
_MIN_DENY_TERM_LEN = 3

EXIT_OK = 0
EXIT_VIOLATION = 1
EXIT_PARTIAL = 2


def load_denylist(path: Path) -> list[str]:
    """Read one denylist term per line; drop comments, blanks, allowed names."""
    terms: list[str] = []
    for raw_line in path.read_text(encoding="utf-8").splitlines():
        term = raw_line.strip()
        if not term or term.startswith("#"):
            continue
        if term.lower() in ALLOWED_NAMES:
            print(f"[publish] WARNING: denylist term {term!r} is an allowed demo name — ignored")
            continue
        if len(term) < _MIN_DENY_TERM_LEN:
            print(f"[publish] WARNING: denylist term {term!r} shorter than {_MIN_DENY_TERM_LEN} chars — ignored")
            continue
        terms.append(term)
    return terms


def validate_manifest(manifest: dict) -> list[str]:
    """Return human-readable manifest problems, one per offending entry.

    ``required_markers`` is the positive demo-tenant proof (see module
    docstring point 2) and must be a non-empty list of non-blank strings on
    EVERY capture entry — an absent key, an empty list, a non-list value, or
    a list containing a non-string/blank string all leave the marker check
    vacuously satisfied for that entry, silently defeating the one check that
    catches a capture taken against the wrong account. Called before any
    capture is inspected so a bad manifest is reported before anything else
    runs.
    """
    problems: list[str] = []
    for entry in manifest.get("captures", []):
        slug = entry.get("slug", "<no slug>")
        if "required_markers" not in entry:
            problems.append(f"manifest entry {slug!r} has no required_markers")
            continue
        markers = entry["required_markers"]
        if not isinstance(markers, list) or not markers:
            problems.append(
                f"manifest entry {slug!r} has an empty required_markers list"
                if isinstance(markers, list)
                else f"manifest entry {slug!r} has a non-list required_markers"
            )
            continue
        if any(not isinstance(m, str) or not m.strip() for m in markers):
            problems.append(
                f"manifest entry {slug!r} has a non-string or blank required_markers entry "
                ""
            )
    return problems


def _term_pattern(term: str) -> re.Pattern[str]:
    """Word-boundary pattern for a denylist term.

    Acronym-style names (any uppercase in the term) match case-sensitively so
    e.g. the org 'SEND' fires on a standalone 'SEND' but not on UI copy like
    'Send us feedback'. All-lowercase terms stay case-insensitive. Lookarounds
    instead of \\b so terms with punctuation ('ORG-ABC', 'TEAM.X') anchor on
    their own first/last characters.
    """
    flags = 0 if any(c.isupper() for c in term) else re.IGNORECASE
    return re.compile(r"(?<!\w)" + re.escape(term) + r"(?!\w)", flags)


def verify_capture(
    page_text: str,
    username: str | None,
    required_markers: list[str],
    denylist: list[str],
    expected_username: str,
    anchor_text: str = "",
) -> list[str]:
    """Return human-readable violations for one capture (empty list = clean).

    ``page_text`` is the visible page text (``document.body.innerText``);
    ``anchor_text`` is the optional hidden ``data-docs-workspace`` attribute
    some pages expose. Required markers are the POSITIVE
    demo-workspace proof and may come from either source, so they are matched
    against both. The denylist tenant-name scan is the NEGATIVE leak check and
    runs against the VISIBLE ``page_text`` ONLY — a real tenant name must never
    be allowed to appear in the published screenshot.
    """
    violations: list[str] = []
    if username != expected_username:
        violations.append(f"captured as username {username!r}, expected {expected_username!r}")
    marker_haystack = f"{page_text}\n{anchor_text}".lower()
    for marker in required_markers:
        if marker.lower() not in marker_haystack:
            violations.append(f"required marker {marker!r} not found in page text or workspace anchor")
    for term in denylist:
        if _term_pattern(term).search(page_text):
            violations.append(f"denylisted tenant name {term!r} found in page text")
    return violations


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="verify only; do not copy any image into docs/",
    )
    parser.add_argument(
        "--allow-missing-denylist",
        action="store_true",
        help="proceed without .docs-capture-denylist.txt (marker + username checks only)",
    )
    parser.add_argument(
        "--expected-username",
        default=None,
        help="override the manifest's expected_username",
    )
    args = parser.parse_args(argv)

    manifest = json.loads(MANIFEST_PATH.read_text(encoding="utf-8"))

    manifest_problems = validate_manifest(manifest)
    if manifest_problems:
        for problem in manifest_problems:
            print(f"[publish] ERROR: {problem}", file=sys.stderr)
        print(
            f"[publish] {len(manifest_problems)} manifest problem(s) — NOTHING published. "
            "Every capture entry must carry required_markers.",
            file=sys.stderr,
        )
        return EXIT_VIOLATION

    expected_username: str = args.expected_username or manifest["expected_username"]

    if DENYLIST_PATH.exists():
        denylist = load_denylist(DENYLIST_PATH)
        print(f"[publish] denylist: {len(denylist)} terms from {DENYLIST_PATH.name}")
    elif args.allow_missing_denylist:
        denylist = []
        print("[publish] WARNING: no denylist — only marker + username checks will run")
    else:
        print(
            f"[publish] ERROR: {DENYLIST_PATH} not found. Generate it per the "
            "runbook, or pass --allow-missing-denylist to skip the "
            "tenant-name scan.",
            file=sys.stderr,
        )
        return EXIT_VIOLATION

    publishable: list[tuple[Path, Path]] = []  # (staged png, docs destination)
    all_violations: list[str] = []
    missing: list[str] = []

    for entry in manifest["captures"]:
        slug: str = entry["slug"]
        png = CAPTURE_DIR / f"{slug}.png"
        sidecar = CAPTURE_DIR / f"{slug}.json"
        if not png.exists() or not sidecar.exists():
            missing.append(slug)
            print(f"[publish] MISSING: {slug} has no capture in {CAPTURE_DIR.name}/ — skipped")
            continue

        meta = json.loads(sidecar.read_text(encoding="utf-8"))
        violations = verify_capture(
            page_text=meta.get("page_text", ""),
            username=meta.get("username"),
            required_markers=entry["required_markers"],
            denylist=denylist,
            expected_username=expected_username,
            anchor_text=meta.get("anchor_text", ""),
        )
        if violations:
            for v in violations:
                all_violations.append(f"{slug}: {v}")
                print(f"[publish] VIOLATION: {slug}: {v}", file=sys.stderr)
            continue

        publishable.append((png, REPO_ROOT / entry["docs_image"]))
        print(f"[publish] clean: {slug}")

    if all_violations:
        print(
            f"[publish] {len(all_violations)} violation(s) — NOTHING published. "
            "A leak on one page means the demo account scoping is wrong; fix "
            "that before publishing anything.",
            file=sys.stderr,
        )
        return EXIT_VIOLATION

    for src, dest in publishable:
        if args.dry_run:
            print(f"[publish] dry-run: would copy {src.name} -> {dest.relative_to(REPO_ROOT)}")
        else:
            dest.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(src, dest)
            print(f"[publish] published {src.name} -> {dest.relative_to(REPO_ROOT)}")

    if missing:
        print(f"[publish] PARTIAL: {len(missing)} manifest entr(ies) had no capture: {', '.join(missing)}")
        return EXIT_PARTIAL
    return EXIT_OK


if __name__ == "__main__":
    sys.exit(main())
