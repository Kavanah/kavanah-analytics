"""Move public-link scope snapshots to a renamed team's or org's new name.

A publish entry stores the publisher's chosen scope as a title snapshot
(``published_team_title`` / ``published_org_title``). The snapshot is
both the public viewer's filter and the key the publish dialog, unpublish and
the dashboard list find the entry by (from the toolbar's current team name). A
rename left it on the old name, so the link stopped serving its team (since
 it serves nothing rather than the dashboard's whole scope) and the
publisher could no longer see or revoke it under the new name.

:func:`rename_publish_snapshots` rewrites matching snapshots on every dashboard
whose own allowed scope contains the renamed team or org. Titles are not
unique across tenants, so a dashboard outside that scope is never
touched, even when it holds a snapshot with the same name.
"""

from __future__ import annotations

import logging
from typing import Any

from lib.es_helpers import get_all_documents_paginated

DASHBOARDS_INDEX = "de_asset_echarts_dashboards"

# kind -> (snapshot field, the dashboard's allowed-scope esid field)
_KINDS = {
    "team": ("published_team_title", "allowed_team_esids"),
    "org": ("published_org_title", "allowed_org_esids"),
}


def _norm(value: Any) -> str:
    return value.strip() if isinstance(value, str) else ""


def _as_list(value: Any) -> list[str]:
    if isinstance(value, str):
        value = [value]
    return [_norm(v) for v in (value or []) if _norm(v)]


def _scope_key(entry: dict) -> tuple[str, str]:
    """Same normalisation as the dashboard builder's ``_publish_scope_key``."""
    return (_norm(entry.get("published_team_title")), _norm(entry.get("published_org_title")))


def _renamed_publishes(source: dict, field: str, old: str, new: str) -> list[dict] | None:
    """The doc's ``publishes`` with ``old`` moved to ``new``; None when nothing changes.

    An entry whose rewritten key would collide with another entry's key is
    left as it is (the publisher already has a link for the new name).
    """
    entries = [dict(e) for e in (source.get("publishes") or []) if isinstance(e, dict)]
    taken = {_scope_key(e) for e in entries}
    changed = False
    for entry in entries:
        if _norm(entry.get(field)) != old:
            continue
        renamed = {**entry, field: new}
        if _scope_key(renamed) in taken:
            logging.warning("Publish entry for %r not renamed to %r: an entry for that scope already exists", old, new)
            continue
        taken.discard(_scope_key(entry))
        taken.add(_scope_key(renamed))
        entry[field] = new
        changed = True
    return entries if changed else None


def rename_publish_snapshots(es: Any, *, kind: str, esid: str, old_title: str, new_title: str) -> int:
    """Rewrite ``old_title`` to ``new_title`` in publish snapshots; return dashboards updated.

    ``kind`` is ``"team"`` or ``"org"``. Covers the ``publishes`` list and a
    legacy (earlier) flat publish on the dashboard doc. Writes use
    optimistic concurrency so a publish made at the same moment is not lost;
    a dashboard that changed underneath is logged and skipped.
    """
    field, scope_field = _KINDS[kind]
    esid, old, new = _norm(esid), _norm(old_title), _norm(new_title)
    if not esid or not old or not new:
        raise ValueError("rename_publish_snapshots: esid, old_title and new_title are required")
    if old == new:
        return 0
    query = {"bool": {"should": [{"exists": {"field": "publishes"}}, {"exists": {"field": field}}], "minimum_should_match": 1}}
    updated = 0
    for hit in get_all_documents_paginated(DASHBOARDS_INDEX, query=query, es=es):
        source = hit.get("_source") or {}
        if esid not in _as_list(source.get(scope_field)):
            continue
        doc: dict[str, Any] = {}
        publishes = _renamed_publishes(source, field, old, new)
        if publishes is not None:
            doc["publishes"] = publishes
        if _norm(source.get(field)) == old:  # legacy flat publish (earlier)
            doc[field] = new
        if not doc:
            continue
        current = es.get(index=DASHBOARDS_INDEX, id=hit["_id"])
        if (current.get("_source") or {}) != source:
            logging.warning("Dashboard %s changed while renaming its publish scope; left for the next rename", hit["_id"])
            continue
        try:
            es.update(
                index=DASHBOARDS_INDEX,
                id=hit["_id"],
                doc=doc,
                if_seq_no=current["_seq_no"],
                if_primary_term=current["_primary_term"],
                refresh="wait_for",
            )
        except Exception as e:  # noqa: BLE001 — a version conflict means a publish write won; log and move on
            logging.warning("Dashboard %s: publish scope rename lost a write race: %s", hit["_id"], type(e).__name__)
            continue
        updated += 1
    return updated
