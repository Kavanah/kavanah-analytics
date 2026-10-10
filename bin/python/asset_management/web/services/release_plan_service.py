"""Release-plan catalog.

Discovers `@pytest.mark.release_plan("...")` and Playwright
`markReleasePlan(testInfo, ..., "<plan>")` annotations across the test
tree at app startup, caches the result, and exposes a couple of helpers
for the Testing-page endpoints.

This is a *text scan*, not an AST parse — it's intentionally tolerant of
minor source-format drift. The canonical execution of every release plan
still happens via pytest / Playwright themselves; this scanner only
populates the UI dropdown.
"""

from __future__ import annotations

import os
import re
import threading
from datetime import datetime, timezone
from typing import Iterable

# Match `release_plan("plan-25")` or `release_plan('plan-25')` (pytest decorator,
# Playwright `markReleasePlan` argument, etc). Captures the plan name.
_RELEASE_PLAN_RE = re.compile(r"""release_plan\s*\(\s*['"]([^'"]+)['"]""")

# Match `test_id("SEC-CSP-005")` similarly — used by the catalog summary.
_TEST_ID_RE = re.compile(r"""test_id\s*\(\s*['"]([^'"]+)['"]""")

# Playwright helper signature: markReleasePlan(testInfo, '<test_id>', '<plan>', ...).
# Captures the test_id (group 1) and the comma-separated plan list (group 2)
# so the scanner can attribute multiple plans to one test_id from a single line.
_MARK_RELEASE_PLAN_RE = re.compile(
    r"""markReleasePlan\s*\(\s*[A-Za-z_$][\w$]*\s*,\s*['"]([^'"]+)['"]\s*,\s*((?:['"][^'"]+['"]\s*(?:,\s*)?)+)\)"""
)
_QUOTED_RE = re.compile(r"""['"]([^'"]+)['"]""")

# Skip these noisy directories when scanning. Add new ones here if they
# generate false positives.
_SKIP_DIRS = {".git", "__pycache__", "node_modules", ".venv", "venv", ".pytest_cache"}

# Skip files that contain marker strings inside Python source-as-data
# (test fixtures, generators, etc.). The scanner is text-based and would
# otherwise index those literal strings as real markers.
_SKIP_FILE_NAMES = {
    "test_release_plan_report.py",
    "test_release_plan_service.py",
    "test_release_plan_to_stubs.py",
    "release_plan_to_stubs.py",
}

_TEST_FILE_SUFFIXES = (".py", ".ts", ".tsx", ".js")


def _walk_test_files(roots: Iterable[str]) -> Iterable[str]:
    for root in roots:
        if not os.path.isdir(root):
            continue
        for dirpath, dirnames, filenames in os.walk(root):
            # Mutate in-place to prune
            dirnames[:] = [d for d in dirnames if d not in _SKIP_DIRS]
            for name in filenames:
                if name in _SKIP_FILE_NAMES:
                    continue
                if name.endswith(_TEST_FILE_SUFFIXES):
                    yield os.path.join(dirpath, name)


def scan_release_plans(roots: Iterable[str]) -> dict[str, list[str]]:
    """Return `{plan_name: [test_id, ...]}` for every release_plan marker found.

    `test_id` lookup is best-effort: the test file's nearest preceding
    `test_id("...")` marker is associated with the plan markers that follow
    on the same test. When no `test_id` is found near a `release_plan`, the
    plan still appears in the result with an empty list.
    """
    plans: dict[str, set[str]] = {}
    for path in _walk_test_files(roots):
        try:
            with open(path, encoding="utf-8", errors="replace") as f:
                source = f.read()
        except OSError:
            continue

        # Walk by line; track the most recently seen test_id within a
        # ~6-line window so multiple release_plan markers on the same test
        # all get associated correctly.
        recent_test_id: str | None = None
        recent_test_id_line = -100
        for lineno, line in enumerate(source.splitlines()):
            # Single-line markReleasePlan(testInfo, "<test_id>", "<plan>", ...)
            for m in _MARK_RELEASE_PLAN_RE.finditer(line):
                tid = m.group(1)
                for plan in _QUOTED_RE.findall(m.group(2)):
                    plans.setdefault(plan, set()).add(tid)
                # markReleasePlan also satisfies the per-line test_id tracker
                recent_test_id = tid
                recent_test_id_line = lineno

            tid_match = _TEST_ID_RE.search(line)
            if tid_match:
                recent_test_id = tid_match.group(1)
                recent_test_id_line = lineno
            for plan in _RELEASE_PLAN_RE.findall(line):
                bucket = plans.setdefault(plan, set())
                if recent_test_id and lineno - recent_test_id_line <= 6:
                    bucket.add(recent_test_id)

    return {plan: sorted(ids) for plan, ids in sorted(plans.items())}


class ReleasePlanCatalog:
    """In-memory cache populated lazily on first access. Refresh by
    process restart (SIGHUP gunicorn) or by calling `invalidate()`."""

    def __init__(self, roots: Iterable[str]):
        self._roots = list(roots)
        self._lock = threading.Lock()
        self._plans: dict[str, list[str]] | None = None
        self._scanned_at: datetime | None = None

    def get(self) -> dict[str, list[str]]:
        with self._lock:
            if self._plans is None:
                self._plans = scan_release_plans(self._roots)
                self._scanned_at = datetime.now(timezone.utc)
            return dict(self._plans)

    def scanned_at(self) -> datetime | None:
        return self._scanned_at

    def invalidate(self) -> None:
        with self._lock:
            self._plans = None
            self._scanned_at = None
