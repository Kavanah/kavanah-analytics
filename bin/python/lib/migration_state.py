"""applied-vs-pending state for ES-data migrations (update_vN).

Migrations under ``asset_management/updates/update_vN.py`` mutate ES runtime
state that git diffs cannot see, and they historically ran by hand with no
record of which ran where. The 2026-08-26 prod audit found three silently
unapplied (v16 one country short, v22's YouTube First Look template never
seeded, v27 partially pinned) — the same class as the incident this
ticket was filed for.

The durable fix: a tiny ``de_meta_migrations`` marker index IN THE SAME
CLUSTER the migrations mutate — one doc per applied version. The environment
is implicit (each env has its own cluster), so a marker can never be read
against the wrong env, unlike the old per-container JSON file (which only
ever existed where someone ran the interactive CLI, i.e. not on prod).

Report-only posture (the ticket's own weighing): nothing here auto-applies a
migration. ``asset-management.py`` gains explicit ``--migrations-status`` /
``--apply-pending`` / ``--mark-applied-through`` modes, and the Status
Reports Data Quality panel surfaces pending versions. Fail-CLOSED on ES
errors: an unreachable cluster reports "unknown", never "nothing pending".
"""

from __future__ import annotations

import logging
import os
import re
from datetime import datetime, timezone
from typing import Any

MIGRATIONS_INDEX = "de_meta_migrations"

_VERSION_RE = re.compile(r"^update_v(\d+)\.py$")


def updates_dir() -> str:
    """The updates directory, derived from THIS file's location so it resolves
    both in the container (/app/python/lib → /app/python/asset_management/updates)
    and in a repo checkout (bin/python/lib → bin/python/asset_management/updates)."""
    return os.path.normpath(os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "asset_management", "updates"))


def available_versions() -> list[int]:
    """Every update_vN version shipped in the code, sorted ascending."""
    versions = []
    try:
        for filename in os.listdir(updates_dir()):
            m = _VERSION_RE.match(filename)
            if m:
                versions.append(int(m.group(1)))
    except OSError as e:
        logging.warning("migration_state: cannot list updates dir: %s", e)
    return sorted(versions)


def _ensure_index(es) -> None:
    """Create the marker index with an explicit mapping on first write."""
    try:
        if es.indices.exists(index=MIGRATIONS_INDEX):
            return
        es.indices.create(
            index=MIGRATIONS_INDEX,
            body={
                "mappings": {
                    "properties": {
                        "version": {"type": "integer"},
                        "applied_at": {"type": "date"},
                        "applied_by": {"type": "keyword"},
                        "method": {"type": "keyword"},
                        "note": {"type": "text"},
                    }
                }
            },
        )
    except Exception as e:  # noqa: BLE001 — a racing create is fine; writes fail loudly below
        logging.info("migration_state: ensure_index: %s", e)


def applied_versions(es) -> dict[int, dict[str, Any]] | None:
    """``{version: marker_doc}`` for every recorded migration, or **None when
    the state is UNKNOWN** (ES error). None is deliberately distinct from {}:
    an empty dict means "nothing recorded", None means "could not read" — a
    caller must never treat unreadable as unapplied (or worse, applied)."""
    try:
        if not es.indices.exists(index=MIGRATIONS_INDEX):
            return {}
        resp = es.search(index=MIGRATIONS_INDEX, body={"size": 1000, "query": {"match_all": {}}})
        out: dict[int, dict[str, Any]] = {}
        for hit in resp.get("hits", {}).get("hits", []):
            src = hit.get("_source") or {}
            try:
                out[int(src.get("version"))] = src
            except (TypeError, ValueError):
                continue
        return out
    except Exception as e:  # noqa: BLE001 — boundary: unknown, not empty
        logging.warning("migration_state: applied_versions failed: %s", e)
        return None


def mark_applied(es, version: int, *, method: str, applied_by: str = "", note: str = "") -> bool:
    """Record ``update_v<version>`` as applied. Idempotent (deterministic _id:
    re-marking overwrites the same doc). Returns False on write failure."""
    _ensure_index(es)
    try:
        es.index(
            index=MIGRATIONS_INDEX,
            id=f"update_v{int(version)}",
            document={
                "version": int(version),
                "applied_at": datetime.now(timezone.utc).isoformat(),
                "applied_by": applied_by or os.environ.get("USER", ""),
                "method": method,
                "note": note,
            },
            refresh="wait_for",
        )
        return True
    except Exception as e:  # noqa: BLE001 — the caller decides how loud to be
        logging.error("migration_state: mark_applied v%s failed: %s", version, e)
        return False


def migration_status(es) -> dict[str, Any]:
    """The applied-vs-pending picture for THIS cluster.

    Returns ``{"ok", "available", "applied", "pending", "unknown"}`` where
    ``pending`` is available-but-unrecorded (ascending). ``unknown=True``
    (with ok=False) means the marker index could not be read — callers must
    surface that, not render an empty pending list as health."""
    available = available_versions()
    applied = applied_versions(es)
    if applied is None:
        return {"ok": False, "unknown": True, "available": available, "applied": {}, "pending": []}
    pending = [v for v in available if v not in applied]
    return {"ok": True, "unknown": False, "available": available, "applied": applied, "pending": pending}
