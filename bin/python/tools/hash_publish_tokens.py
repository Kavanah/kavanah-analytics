"""Hash the public-report publish tokens still stored in plaintext.

Since a publish entry stores ``publish_token_hash`` (SHA-256 hex, the
lookup key) and ``publish_token_enc`` (the token Fernet-encrypted, only so the
publish dialog and scheduled emails can show the link again), and the
top-level ``publish_tokens_lookup`` mirror holds hashes. Docs written before
that still carry plaintext in three places:

* ``publishes[].publish_token``,
* ``publish_tokens_lookup`` (the mirror array of those tokens),
* the pre-664 flat slot (top-level ``publish_token`` and its siblings).

This tool rewrites each such doc to the hashed form. The published URL does not
change — the hash is of the same token — so every live link keeps working.
The legacy flat slot is folded into ``publishes[]`` (what any publish write on
the doc already does) unless a ``publishes[]`` entry already holds its scope,
in which case the slot was hidden and unreachable and is simply cleared.

The transform reuses the request path's own helpers (``_hash_entry_token``,
``_tokens_lookup_array``, ``_legacy_entry_from_flat_fields``) so the two can
never disagree.

Report-only by default; ``--apply`` writes. Each write re-reads the doc and is
guarded by ``if_seq_no`` / ``if_primary_term``: a doc changed in between is
counted as a ``conflict`` and left for the next run. Idempotent: a second run
finds nothing to do. No snapshot is written on purpose — a snapshot of these
docs would be a file of plaintext tokens. Output is dashboard ids and counts
only, never a token, hash or ciphertext.

ORDER: run ``--apply`` only once the release is confirmed good on the
host. Code from compares plaintext, so rolling the code back
after this tool has run would 404 every published link until re-published.

Once it reports ``remaining=0`` on prod, the "legacy" branches in
dashboard_builder/routes.py (plaintext acceptance) can be removed.

Usage (inside imports_container)::

    docker exec -w /app/python imports_container python3 -m tools.hash_publish_tokens # report
    docker exec -w /app/python imports_container python3 -m tools.hash_publish_tokens --apply

Exit status: 0 when nothing is left to do (or a report completed), 2 when
``--apply`` left docs behind (conflicts), 1 on a hard error (no ES).
"""

from __future__ import annotations

import argparse
import logging
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

_HERE = Path(__file__).resolve().parent
for _candidate in (
    _HERE.parents[1],
    _HERE.parents[2] / "bin" / "python",
    Path("/app/python"),
):
    if (_candidate / "asset_management").exists():
        sys.path.insert(0, str(_candidate))
        _APP_ROOT = _candidate
        break
else:
    _APP_ROOT = None

from lib.es_helpers import get_all_documents_paginated, get_elasticsearch_client  # noqa: E402

logger = logging.getLogger(__name__)

DASHBOARDS_INDEX = "de_asset_echarts_dashboards"


def _add_cli_paths() -> None:
    """Put ``asset_management`` on sys.path for a standalone run.

    dashboard_builder/routes.py imports ``web.services.*`` first, the layout the
    web app runs with — without this entry the tool died on prod with
    ModuleNotFoundError (its fallback ``services.*`` needs asset_management/web
    instead). Only ``main()`` calls this: done at import time it made the test
    suite load ``web.services.*`` and ``asset_management.web.services.*`` as two
    separate modules, so patches on one missed the other (230 failures in CI).
    """
    if _APP_ROOT is None:
        return
    path = str(_APP_ROOT / "asset_management")
    if path not in sys.path:
        sys.path.insert(1, path)


def _routes():
    from asset_management.web.blueprints.dashboard_builder import routes

    return routes


def plan_update(source: dict[str, Any]) -> tuple[dict[str, Any] | None, int]:
    """Return ``(partial_doc, plaintext_count)`` for one dashboard ``_source``.

    ``partial_doc`` is ``None`` when the doc holds no plaintext token. The
    count is the number of plaintext tokens the update removes (list entries
    plus the legacy flat slot), for the report.
    """
    routes = _routes()
    entries = source.get("publishes")
    entries = [e for e in entries if isinstance(e, dict)] if isinstance(entries, list) else []
    plaintext = sum(1 for e in entries if e.get("publish_token"))
    new_entries = [routes._hash_entry_token(e) for e in entries]

    update: dict[str, Any] = {}
    legacy = routes._legacy_entry_from_flat_fields(source)
    if legacy is not None:
        plaintext += 1
        legacy_key = routes._publish_scope_key(legacy.get("published_team_title"), legacy.get("published_org_title"))
        shadowed = any(
            routes._publish_scope_key(e.get("published_team_title"), e.get("published_org_title")) == legacy_key
            for e in new_entries
        )
        if not shadowed:
            new_entries.append(routes._hash_entry_token(legacy))
        for key in routes._PUBLISH_ENTRY_FIELDS:
            update[key] = None

    new_lookup = routes._tokens_lookup_array(new_entries)
    lookup_stale = list(source.get("publish_tokens_lookup") or []) != new_lookup
    if not plaintext and not lookup_stale:
        return None, 0

    update["publishes"] = new_entries
    update["publish_tokens_lookup"] = new_lookup
    return update, plaintext


@dataclass
class Report:
    scanned: int = 0
    docs_to_fix: int = 0
    tokens_hashed: int = 0
    written: int = 0
    conflicts: int = 0
    conflict_ids: list[str] = field(default_factory=list)

    @property
    def remaining(self) -> int:
        return self.docs_to_fix - self.written


def run(es: Any, *, apply: bool, dashboard_ids: list[str] | None = None) -> Report:
    from elasticsearch import ConflictError

    report = Report()
    if dashboard_ids:
        hits = [{"_id": did} for did in dashboard_ids]
    else:
        hits = get_all_documents_paginated(DASHBOARDS_INDEX, es=es)
    for hit in hits:
        doc_id = hit["_id"]
        # Re-read for the concurrency guard (and for the --dashboard-id path).
        try:
            current = es.get(index=DASHBOARDS_INDEX, id=doc_id)
        except Exception as e:  # noqa: BLE001 — a doc deleted mid-run just isn't migrated
            logger.warning("skip %s: not readable (%s)", doc_id, type(e).__name__)
            continue
        report.scanned += 1
        update, plaintext = plan_update(current.get("_source") or {})
        if update is None:
            continue
        report.docs_to_fix += 1
        report.tokens_hashed += plaintext
        if not apply:
            logger.info("WOULD hash %s: %d plaintext token(s)", doc_id, plaintext)
            continue
        try:
            es.update(
                index=DASHBOARDS_INDEX,
                id=doc_id,
                doc=update,
                if_seq_no=current["_seq_no"],
                if_primary_term=current["_primary_term"],
                refresh="wait_for",
            )
        except ConflictError:
            report.conflicts += 1
            report.conflict_ids.append(doc_id)
            logger.warning("conflict %s: changed since read — re-run to pick it up", doc_id)
            continue
        report.written += 1
        logger.info("hashed %s: %d plaintext token(s)", doc_id, plaintext)
    return report


def main(argv: list[str] | None = None) -> int:
    logging.basicConfig(level=logging.INFO, format="%(message)s")
    ap = argparse.ArgumentParser(description="Hash plaintext public-report publish tokens.")
    ap.add_argument("--apply", action="store_true", help="write the hashed form (default: report only)")
    ap.add_argument("--dashboard-id", action="append", dest="dashboard_ids", help="restrict to dashboard id(s)")
    args = ap.parse_args(argv)
    _add_cli_paths()

    es = get_elasticsearch_client()
    if es is None:
        logger.error("Elasticsearch unavailable")
        return 1

    report = run(es, apply=args.apply, dashboard_ids=args.dashboard_ids)
    logger.info(
        "%s scanned=%d docs_to_fix=%d tokens=%d written=%d conflicts=%d remaining=%d",
        "APPLY" if args.apply else "REPORT",
        report.scanned,
        report.docs_to_fix,
        report.tokens_hashed,
        report.written,
        report.conflicts,
        report.remaining if args.apply else report.docs_to_fix,
    )
    if not args.apply:
        logger.info("Report only. Re-run with --apply to write.")
        return 0
    return 2 if report.remaining else 0


if __name__ == "__main__":
    raise SystemExit(main())
