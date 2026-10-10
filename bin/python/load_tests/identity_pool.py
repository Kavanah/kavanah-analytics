"""Identity pool for multi-identity load tests.

Loads the CSV emitted by ``tools/seed_load_test_identities.py`` and hands out
identities round-robin so each simulated locust user logs in as a different
seeded account. The CSV lives on the ``/app/data`` volume (never in the repo)
because it contains known passwords.

Deliberately free of locust imports so the loader and round-robin logic are
unit-testable without locust installed.
"""

from __future__ import annotations

import csv
import logging
import os
import threading
from dataclasses import dataclass

logger = logging.getLogger(__name__)

DEFAULT_IDENTITY_FILE = "/app/data/load_tests/identities.csv"
_IDENTITY_FILE_ENV = "LOAD_TEST_IDENTITY_FILE"
# The seeder's probe account row. Excluded from the pool: the probe is
# injected as LOAD_TEST_USERNAME by run-locust and must not double as
# background load (its stats ARE the baseline).
PROBE_ROLE_LABEL = "probe"
# Unscoped identities (no workspace/org/team grant) are excluded by DEFAULT:
# they can't see any dashboards, so they contribute only 403 chatter on the
# list gate — at 50 users they were 28% of the pool and silently diluted the
# capacity numbers (operator finding, 2026-07-05). Opt back in per run with
# LOAD_TEST_INCLUDE_UNSCOPED=1 (the "Include unscoped users" checkbox).
UNSCOPED_ROLE_LABEL = "unscoped"
_INCLUDE_UNSCOPED_ENV = "LOAD_TEST_INCLUDE_UNSCOPED"


@dataclass(frozen=True)
class Identity:
    """One seeded load-test login. role/workspace are informational labels."""

    username: str
    password: str
    role: str = ""
    workspace: str = ""


def load_pool(path: str, include_unscoped: bool = False) -> list[Identity]:
    """Load identities from CSV. Failure-soft: a missing/unreadable/empty file
    returns [] (the locustfile then falls back to single-identity mode so the
    run still measures something rather than aborting)."""
    if not os.path.isfile(path):
        logger.warning("identity pool file not found: %s", path)
        return []
    identities: list[Identity] = []
    try:
        with open(path, newline="", encoding="utf-8") as fh:
            for row in csv.DictReader(fh):
                username = (row.get("username") or "").strip()
                password = (row.get("password") or "").strip()
                role = (row.get("role") or "").strip()
                if not username or not password or role == PROBE_ROLE_LABEL:
                    continue
                if role == UNSCOPED_ROLE_LABEL and not include_unscoped:
                    continue
                identities.append(
                    Identity(
                        username=username,
                        password=password,
                        role=role,
                        workspace=(row.get("workspace") or "").strip(),
                    )
                )
    except OSError as exc:
        logger.warning("failed to read identity pool %s: %s", path, exc)
        return []
    if not identities:
        logger.warning("identity pool %s contained no usable rows", path)
    return identities


class IdentityPool:
    """Round-robin dispenser. Wraps around when there are more locust users
    than identities (index modulo pool size)."""

    def __init__(self, identities: list[Identity]) -> None:
        self._identities = list(identities)
        self._index = 0
        self._lock = threading.Lock()

    def __len__(self) -> int:
        return len(self._identities)

    def next(self) -> Identity | None:
        """Return the next identity, or None when the pool is empty."""
        with self._lock:
            if not self._identities:
                return None
            identity = self._identities[self._index % len(self._identities)]
            self._index += 1
            return identity


_default_pool: IdentityPool | None = None


def default_pool() -> IdentityPool:
    """Process-wide pool loaded lazily from $LOAD_TEST_IDENTITY_FILE."""
    global _default_pool
    if _default_pool is None:
        path = os.environ.get(_IDENTITY_FILE_ENV, DEFAULT_IDENTITY_FILE)
        include_unscoped = os.environ.get(_INCLUDE_UNSCOPED_ENV, "") == "1"
        _default_pool = IdentityPool(load_pool(path, include_unscoped=include_unscoped))
    return _default_pool
