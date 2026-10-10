"""One-shot recovery: Echo conversations archived without ever being indexed
.

Until, Echo's archive step moved every closed or expired conversation
out of ``/app/data/echo/conversations`` whether or not ``send_json_to_elastic``
had indexed it, and send reads only that directory. A conversation the indexer
dropped or failed once was therefore stranded in ``conversations_closed`` /
``conversations_expired`` for good. Prod 2026-09-29: 3,457 such conversations
for 12 live teams, nearly all from April-May 2026 around the index
cutover. The archive gate stops new stranding; this tool indexes the backlog.

For every file in the two archive roots whose document no index holds (exact
lookup, ``lib.es_helpers.existing_doc_ids``):

* **recoverable** — its team resolves and its date parses, i.e. exactly what
  the live indexer would accept. It is indexed through the live indexer's own
  ``generate_bulk_actions`` (same index, same upsert, same kibana_date and
  expiration handling), after the two per-conversation steps the live fetch
  applies before saving: ``minimise_entry`` (an older archived file
  can still carry the dropped person fields, and they must not reach ES) and
  ``compute_outcome_flags``. When the same id sits in both roots, the closed
  copy wins (closed is final).
* **skipped, counted** — unattributed (Echo matched no team; the retention
  sweep purges these at 90 days), team_unresolved (the title matches no DEAM
  team; fix the mapping first), no_date (the live indexer drops these too).

No file is moved, rewritten or deleted: once its document is indexed, a file is
an ordinary indexed archive file and the retention sweep purges it on its
normal 7-day rule. After the send every recovered id is re-checked; any that did
not land makes the run exit 1.

Dry-run by default: it prints per-root and per-team counts and writes nothing.
``--apply`` indexes. ``--team <esid>`` (repeatable) limits the run to those teams,
for a canary on the smallest team first. Idempotent: a second run finds nothing
missing. Never prints a document value, only counts, team esids and titles.

Exit codes: 0 = nothing failed; 1 = a send or verify failed; 2 = ES unavailable
or the index lookup could not be answered in full.

Usage (inside imports_container)::

    docker exec -w /app/python imports_container python3 \\
        -m tools.recover_echo_unindexed [--team <esid>...] [--apply]
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import sys
from collections import Counter, defaultdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Iterable

_HERE = Path(__file__).resolve().parent
for _candidate in (
    _HERE.parents[1],
    _HERE.parents[2] / "bin" / "python",
    Path("/app/python"),
):
    if (_candidate / "asset_management" / "__init__.py").exists():
        if str(_candidate) not in sys.path:
            sys.path.insert(0, str(_candidate))
        break

from lib.es_helpers import (  # noqa: E402
    build_team_lookup_cache,
    existing_doc_ids,
    get_elasticsearch_client,
    resolve_team_esid,
)

logger = logging.getLogger(__name__)

ECHO_INDEX_PATTERN = "*_echo_conversations"
UNATTRIBUTED_MARKER = "unknown"
# Closed first: when an id sits in both roots, the closed copy is final.
ARCHIVE_ROOTS: tuple[str, ...] = (
    "/app/data/echo/conversations_closed",
    "/app/data/echo/conversations_expired",
)
BULK_CHUNK_SIZE = 500


def _load_archive(roots: Iterable[str]) -> dict[str, tuple[str, str]]:
    """``{doc_id: (root, path)}`` for every archived file, first root wins."""
    out: dict[str, tuple[str, str]] = {}
    for root in roots:
        for dirpath, _dirs, files in os.walk(root):
            for name in files:
                if name.endswith(".json"):
                    out.setdefault(os.path.splitext(name)[0], (root, os.path.join(dirpath, name)))
    return out


def classify(data: dict[str, Any], team_cache: dict, to_iso_date: Callable[[Any], str | None]) -> tuple[str, str | None]:
    """``(kind, team_esid)`` for one missing conversation, by the live
    indexer's own acceptance rules."""
    if (
        str(data.get("team_esid") or "").strip().lower() == UNATTRIBUTED_MARKER
        and str(data.get("team_title") or "").strip().lower() == UNATTRIBUTED_MARKER
    ):
        return "unattributed", None
    iso = (
        (to_iso_date(data.get("kibana_date")) if data.get("kibana_date") else None)
        or to_iso_date(data.get("created_at"))
        or to_iso_date(data.get("createdAt"))
    )
    if not iso:
        return "no_date", None
    team_esid = resolve_team_esid(team_cache, team_title=data.get("team_title"))
    if not team_esid:
        return "team_unresolved", None
    return "recoverable", team_esid


def plan_recovery(
    archive: dict[str, tuple[str, str]],
    present: set[str],
    read: Callable[[str], dict[str, Any] | None],
    team_cache: dict,
    to_iso_date: Callable[[Any], str | None],
    teams: set[str] | frozenset[str] = frozenset(),
) -> dict[str, Any]:
    """Pure planner. ``read(path)`` returns the file's JSON or ``None`` when
    unreadable; ``teams`` (esids) narrows the recoverable set when given."""
    by_root: dict[str, Counter] = defaultdict(Counter)
    by_team: dict[str, dict[str, Any]] = {}
    recover: list[tuple[str, dict[str, Any]]] = []
    for doc_id, (root, path) in sorted(archive.items()):
        if doc_id in present:
            by_root[root]["indexed"] += 1
            continue
        data = read(path)
        if data is None:
            by_root[root]["unreadable"] += 1
            continue
        kind, team_esid = classify(data, team_cache, to_iso_date)
        if kind == "recoverable" and teams and team_esid not in teams:
            kind = "other_team"
        by_root[root][kind] += 1
        if kind == "recoverable":
            t = by_team.setdefault(team_esid, {"title": data.get("team_title") or team_esid, "count": 0})
            t["count"] += 1
            recover.append((doc_id, data))
    return {"by_root": {r: dict(c) for r, c in by_root.items()}, "by_team": by_team, "recover": recover}


def _prepare(data: dict[str, Any]) -> dict[str, Any]:
    """The per-conversation steps the live fetch applies before saving."""
    from import_scripts.echo.get_conversations import minimise_entry
    from import_scripts.echo.outcome_flags import compute_outcome_flags

    doc = dict(data)
    minimise_entry(doc)
    doc.update(compute_outcome_flags(doc.get("outcome", "")))
    return doc


def send(es: Any, docs: list[dict[str, Any]], team_cache: dict) -> tuple[int, list]:
    """Index ``docs`` through the live indexer's ``generate_bulk_actions``."""
    from elasticsearch.helpers import bulk
    from import_scripts.echo.send_json_to_elastic import generate_bulk_actions
    from migration.schema_loader import get_schema_for_index_type

    schema = get_schema_for_index_type("echo_conversations")
    now_formatted = datetime.now(timezone.utc).strftime("%B %d, %Y %H:%M:%S UTC")
    actions = generate_bulk_actions(docs, {}, es, schema, now_formatted, team_cache)
    return bulk(es, actions, chunk_size=BULK_CHUNK_SIZE, raise_on_error=False, refresh="wait_for")


def _read(path: str) -> dict[str, Any] | None:
    try:
        with open(path, "r", encoding="utf-8") as fh:
            data = json.load(fh)
        return data if isinstance(data, dict) else None
    except Exception:  # noqa: BLE001 — unreadable file: counted, never guessed at
        return None


def run(es: Any, *, apply: bool, teams: set[str], roots: tuple[str, ...] = ARCHIVE_ROOTS) -> int:
    from import_scripts.echo.send_json_to_elastic import _to_iso_date

    archive = _load_archive(roots)
    try:
        present = existing_doc_ids(es, ECHO_INDEX_PATTERN, sorted(archive))
        team_cache = build_team_lookup_cache(es)
    except Exception as e:  # noqa: BLE001 — CLI boundary; an incomplete answer must never become a plan
        logger.error("could not read the index or team state: %s: %s", type(e).__name__, e)
        return 2
    plan = plan_recovery(archive, present, _read, team_cache, _to_iso_date, teams=teams)

    for root, counts in sorted(plan["by_root"].items()):
        logger.info("ROOT %s: %s", root, ", ".join(f"{k}={v}" for k, v in sorted(counts.items())))
    for team_esid, t in sorted(plan["by_team"].items(), key=lambda kv: -kv[1]["count"]):
        logger.info("TEAM %s %r: %d conversation(s) to recover", team_esid, t["title"], t["count"])
    total = len(plan["recover"])
    if not apply:
        logger.info(
            "DRY-RUN: %d conversation(s) recoverable across %d team(s) — re-run with --apply to index",
            total,
            len(plan["by_team"]),
        )
        return 0
    if not total:
        logger.info("APPLY: nothing to recover")
        return 0

    ok, errors = send(es, [_prepare(d) for _id, d in plan["recover"]], team_cache)
    ids = [doc_id for doc_id, _d in plan["recover"]]
    try:
        landed = existing_doc_ids(es, ECHO_INDEX_PATTERN, ids)
    except Exception as e:  # noqa: BLE001 — verification must be reported, not assumed
        logger.error("sent %d, but could not verify: %s", ok, type(e).__name__)
        return 1
    missing = [i for i in ids if i not in landed]
    logger.info("APPLY: sent %d, bulk errors %d; VERIFY %d/%d landed", ok, len(errors), len(ids) - len(missing), len(ids))
    for err in errors[:10]:
        logger.warning("bulk error: %s", json.dumps(err, default=str)[:300])
    if missing:
        logger.warning("%d recovered id(s) not found after the send; first: %s", len(missing), ", ".join(missing[:10]))
    return 1 if (missing or errors) else 0


def main(argv: list[str] | None = None) -> int:
    logging.basicConfig(format="%(asctime)s %(levelname)s %(message)s", level=logging.INFO, stream=sys.stderr)
    ap = argparse.ArgumentParser(description="Index Echo conversations archived without being indexed.")
    ap.add_argument("--apply", action="store_true", help="index the recoverable conversations (default: dry-run)")
    ap.add_argument("--team", action="append", default=[], help="team esid to limit the run to (repeatable; canary)")
    args = ap.parse_args(argv)
    es = get_elasticsearch_client()
    if es is None:
        logger.error("Could not connect to ES; aborting.")
        return 2
    return run(es, apply=args.apply, teams=set(args.team))


if __name__ == "__main__":
    raise SystemExit(main())
