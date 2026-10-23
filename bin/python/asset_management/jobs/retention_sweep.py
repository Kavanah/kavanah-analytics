"""Conversation retention sweep.

Decisions (a teammate, 2026-09-08, the private docs):

* Echo and Chatwoot conversations are kept for 24 months, then deleted.
* Before a month of raw conversations is deleted it is rolled up, per team,
  into ``{team_esid}__conversation_rollups`` (one doc per team × source ×
  month: conversation count, the outcome-flag sums, message sums, distinct
  clients), so the Summary Report's all-time tiles keep their history.
* Raw JSON on disk is purged once its document has been indexed for 7 days.
  this includes the Disciple.Tools staging dir and its legacy
  ``raw_imported`` archive (a file is indexed when every id in it is).
* Raw Echo JSON that Echo could not attribute to any team (``team_esid`` and
  ``team_title`` both ``unknown``) is purged once its file is 90 days old
  : no team can ever index it, and a conversation
  Echo attributes later is re-fetched and saved again under its team. Files
  whose team title merely fails to resolve (a rename, say) are kept and
  counted, because fixing the mapping would recover them.
* Disciple.Tools has no time-based deletion; posts the upstream site no
  longer has are removed within 30 days (the importer never tombstones, so
  this sweep reconciles the id sets — ``--dt-reconcile`` / ``--only-dt``).
* Aggregates are kept indefinitely. Snapshot retention (SLM
  ``daily-snapshots``; ``expire_after`` must stay within 30 days, 3d in
  practice) is provisioned from git by
  ``deployment/09_provision_slm.sh``; the sweep only reports it.

Ordering is the safety property: for every index the rollup is written and
refreshed FIRST, and the delete runs only for indices whose rollup wrote
cleanly. A rollup document id is ``{team_esid}:{source}:{yyyy-MM}``, so a
re-run overwrites the same month with the same sums (idempotent) and a
month whose raw docs are already gone simply produces no bucket.

The cutoff is the first day of the month 24 months ago (UTC) — the
month-rounded form of the agreed ``created_at < now-24M`` — so a month is
either entirely rolled up and deleted or entirely kept; a day-granular
cutoff would delete part of a month that was never rolled up.

Dry-run is the default and writes nothing to conversation indices, rollup
indices, DT indices or disk; it prints the counts it WOULD act on. The run
record and heartbeat in ``de_retention_runs`` are written in both modes so
``--check-heartbeat`` can tell "the cron stopped" from "the cron is in
dry-run" (CLAUDE.md #24 — a worker with no canary stops silently).

Never prints a document VALUE — only counts, index names, ids of rollup
docs (team esid + month) and file counts.

Usage::

    python3 /app/python/asset_management/jobs/retention_sweep.py              # dry-run
    python3 /app/python/asset_management/jobs/retention_sweep.py --apply
    python3 /app/python/asset_management/jobs/retention_sweep.py --only-dt [--apply] [--force]
    python3 /app/python/asset_management/jobs/retention_sweep.py --check-heartbeat --max-age-hours 30
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import sys
import uuid
from datetime import datetime, timedelta, timezone
from typing import Any, Callable, Iterable

sys.path.insert(0, "/app/python")

import lib.logging  # noqa: E402
from lib.es_helpers import (  # noqa: E402
    build_data_index_name,
    build_team_lookup_cache,
    create_index_if_not_exists,
    ensure_data_index,
    existing_doc_ids,
    get_document_by_id,
    get_elasticsearch_client,
    resolve_team_esid,
)
from migration.schema_loader import get_schema_for_index_type  # noqa: E402

LOG_PREFIX = "retention-sweep"

RETENTION_MONTHS = 24
ROLLUP_SOURCE = "conversation_rollups"
RUNS_INDEX = "de_retention_runs"
HEARTBEAT_ID = "__heartbeat__"
# The sweep runs nightly; 30h is one missed run plus slack.
DEFAULT_MAX_AGE_HOURS = 30

RAW_JSON_MIN_AGE_DAYS = 7
ID_BATCH = 500
# Echo's marker for a conversation it could not match to a team
# (get_conversations writes team_esid/team_title "unknown" under unknownteams/).
UNATTRIBUTED_MARKER = "unknown"
UNATTRIBUTED_MAX_AGE_DAYS = 90

# DT: refuse to delete more than this share of a site's docs without --force —
# a half-empty API answer (pagination bug, permissions change) must not wipe
# a team's history.
DT_DELETE_RATIO_GUARD = 0.5

# Rollup teams per index; an index with more distinct team_esids than this
# is refused (sum_other_doc_count > 0) rather than partially rolled up.
TEAM_TERMS_SIZE = 500

# Conversation families under retention. ``sum_fields`` are the additive
# per-conversation counters the Summary Report sums; ``client_field`` feeds
# the per-month distinct-client count (informational — cardinality is not
# additive across months, see DECISIONS).
FAMILIES: dict[str, dict[str, Any]] = {
    "echo_conversations": {
        "pattern": "*_echo_conversations",
        "date_field": "created_at",
        "sum_fields": (
            "spiritual_conversation",
            "multiplier",
            "no_response",
            "language_barrier",
            "Other",
            "incomingMessagesCount",
            "outboundMessagesCount",
        ),
        "client_field": "client_id",
    },
    "chatwoot_conversations": {
        "pattern": "*_chatwoot_conversations",
        "date_field": "created_at",
        "sum_fields": (),
        "client_field": "sender_id",
    },
}

# Raw JSON roots: (path, family, id_style). ``echo`` ids are the filename
# stem (send_json_to_elastic uses ``data["id"]``); ``chatwoot`` ids are
# ``{chatwoot_account_esid}_{conversation_id}`` (make_doc_id), with the
# account esid read from the file and the parent directory as fallback.
# ``dt``: one file per (team, post_type) holding many documents —
# ``docs`` (current) or raw REST ``posts`` (earlier files and the
# ``raw_imported`` archives); a file counts as indexed only when EVERY id in it
# is in that team's own ``{team_esid}__disciple_tools`` index (DT ids repeat
# across sites, so a cross-index lookup would be wrong). Its family is
# ``DT_SOURCE``, which is not a rollup family.
RAW_JSON_ROOTS: tuple[tuple[str, str, str], ...] = (
    ("/app/data/echo/conversations", "echo_conversations", "echo"),
    ("/app/data/echo/conversations_closed", "echo_conversations", "echo"),
    ("/app/data/echo/conversations_expired", "echo_conversations", "echo"),
    ("/app/data/chatwoot/conversations", "chatwoot_conversations", "chatwoot"),
    ("/app/data/disciple_tools/raw", "disciple_tools", "dt"),
    ("/app/data/disciple_tools/raw_imported", "disciple_tools", "dt"),
)

DT_SOURCE = "disciple_tools"
DT_POST_TYPES = ("contacts", "groups")

SLM_POLICY = "daily-snapshots"
SLM_MAX_RETENTION_DAYS = 30

RUNS_MAPPING = {
    "properties": {
        "run_id": {"type": "keyword"},
        "kind": {"type": "keyword"},
        "apply": {"type": "boolean"},
        "ok": {"type": "boolean"},
        "started_at": {"type": "date"},
        "finished_at": {"type": "date"},
        "last_run_at": {"type": "date"},
        "cutoff": {"type": "date"},
        "summary": {"type": "object", "enabled": False},
    }
}


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _body(resp: Any) -> Any:
    """Unwrap an elasticsearch-py ObjectApiResponse (or pass a dict through)."""
    return resp.body if hasattr(resp, "body") else resp


def retention_cutoff(now: datetime | None = None, months: int = RETENTION_MONTHS) -> datetime:
    """First day (00:00 UTC) of the month ``months`` months before ``now``.

    Everything with ``created_at`` strictly before this instant is out of
    retention; the month that contains ``now - months`` is kept whole.
    """
    now = now or _now()
    y, m = now.year, now.month - months
    while m <= 0:
        y -= 1
        m += 12
    return datetime(y, m, 1, tzinfo=timezone.utc)


def _iso(dt: datetime) -> str:
    return dt.strftime("%Y-%m-%dT%H:%M:%SZ")


def retention_query(date_field: str, cutoff: datetime) -> dict:
    """The one range query both the rollup and the delete use.

    An explicit ISO instant (not ``now-24M``) so the rollup and the delete
    that follows it cannot disagree about the boundary; ``format`` overrides
    the field's own format (Echo stores ``MMMM dd, yyyy …``, Chatwoot epoch
    seconds) for parsing the query value only.
    """
    return {"range": {date_field: {"lt": _iso(cutoff), "format": "strict_date_optional_time"}}}


# ── ES helpers (thin, so tests can fake them) ─────────────────────────────────


def _list_indices(es, pattern: str) -> list[str]:
    body = _body(es.indices.get(index=pattern, expand_wildcards="open"))
    names = list(body.keys()) if hasattr(body, "keys") else []
    return sorted(n for n in names if not n.startswith("."))


def _bulk(es, actions: Iterable[dict]) -> tuple[int, list]:
    """helpers.bulk with errors collected, never raised."""
    from elasticsearch.helpers import bulk

    ok, errors = bulk(es, list(actions), raise_on_error=False, refresh="wait_for")
    return int(ok), list(errors or [])


def _scan_ids(es, index: str, query: dict) -> set[str]:
    from elasticsearch.helpers import scan

    return {str(hit["_id"]) for hit in scan(es, index=index, query={"query": query}, _source=False)}


# ── Rollup ────────────────────────────────────────────────────────────────────


def rollup_query(family: dict, cutoff: datetime) -> dict:
    per_month: dict[str, Any] = {f: {"sum": {"field": f}} for f in family["sum_fields"]}
    per_month["clients"] = {"cardinality": {"field": family["client_field"]}}
    return {
        "size": 0,
        "query": retention_query(family["date_field"], cutoff),
        "aggs": {
            "by_team": {
                "terms": {"field": "team_esid", "size": TEAM_TERMS_SIZE, "missing": ""},
                "aggs": {
                    "team_title": {"terms": {"field": "team_title", "size": 1}},
                    "by_month": {
                        "date_histogram": {
                            "field": family["date_field"],
                            "calendar_interval": "month",
                            "format": "yyyy-MM",
                            "min_doc_count": 1,
                            "time_zone": "UTC",
                        },
                        "aggs": per_month,
                    },
                },
            }
        },
    }


def rollup_doc_id(team_esid: str, source: str, month: str) -> str:
    return f"{team_esid}:{source}:{month}"


def build_rollup_docs(
    aggregations: dict, *, source: str, family: dict, cutoff: datetime, now: datetime
) -> tuple[list[dict], int, bool]:
    """Turn one index's aggregation response into rollup docs.

    Returns ``(docs, unattributed_docs, complete)``. ``unattributed`` counts
    raw docs with no team_esid (they cannot be rolled up per team and are
    reported, not silently dropped). ``complete`` is False when the terms
    agg overflowed (``sum_other_doc_count`` > 0) — the caller must then
    refuse to delete, because some team's history would be lost.
    """
    by_team = (aggregations or {}).get("by_team") or {}
    complete = int(by_team.get("sum_other_doc_count") or 0) == 0
    docs: list[dict] = []
    unattributed = 0
    for tb in by_team.get("buckets") or []:
        team_esid = str(tb.get("key") or "").strip()
        if not team_esid or team_esid.lower() == "unknown":
            unattributed += int(tb.get("doc_count") or 0)
            continue
        title_buckets = ((tb.get("team_title") or {}).get("buckets")) or []
        team_title = str(title_buckets[0].get("key")) if title_buckets else ""
        for mb in ((tb.get("by_month") or {}).get("buckets")) or []:
            month = str(mb.get("key_as_string") or "")
            if len(month) != 7:
                continue
            doc: dict[str, Any] = {
                "team_esid": team_esid,
                "team_title": team_title,
                "source": source,
                "month": month,
                "kibana_date": f"{month}-01",
                "@timestamp": f"{month}-01T00:00:00Z",
                "conversations": int(mb.get("doc_count") or 0),
                "clients": int(((mb.get("clients") or {}).get("value")) or 0),
                "retention_months": RETENTION_MONTHS,
                "cutoff": _iso(cutoff),
                "rolled_up_at": _iso(now),
            }
            for f in family["sum_fields"]:
                doc[f] = int(round(float(((mb.get(f) or {}).get("value")) or 0)))
            docs.append(doc)
    return docs, unattributed, complete


def rollup_and_delete(es, *, cutoff: datetime, now: datetime, apply: bool) -> dict:
    """Per family, per index: roll up months older than ``cutoff``, then delete.

    The delete for an index runs only when that index's rollup completed
    without bulk errors (and its team terms did not overflow). Returns the
    per-index report.
    """
    report: dict[str, Any] = {"cutoff": _iso(cutoff), "apply": apply, "indices": [], "ok": True}
    rollup_schema = None
    for source, family in FAMILIES.items():
        for index in _list_indices(es, family["pattern"]):
            row: dict[str, Any] = {"index": index, "source": source}
            try:
                resp = _body(es.search(index=index, body=rollup_query(family, cutoff)))
                aggs = resp.get("aggregations") or {}
                matched = int(((resp.get("hits") or {}).get("total") or {}).get("value") or 0)
            except Exception as exc:  # noqa: BLE001 — one index must not sink the sweep
                row.update(status="search_failed", error=type(exc).__name__)
                report["indices"].append(row)
                report["ok"] = False
                continue
            docs, unattributed, complete = build_rollup_docs(aggs, source=source, family=family, cutoff=cutoff, now=now)
            row.update(expired_docs=matched, rollup_docs=len(docs), unattributed_docs=unattributed)
            if matched == 0:
                row["status"] = "nothing_to_do"
                report["indices"].append(row)
                continue
            if not complete:
                row["status"] = "refused_team_overflow"
                report["indices"].append(row)
                report["ok"] = False
                continue
            if not apply:
                row["status"] = "dry_run"
                report["indices"].append(row)
                continue

            # 1. rollup first
            if rollup_schema is None:
                rollup_schema = get_schema_for_index_type(ROLLUP_SOURCE)
            actions = []
            try:
                for d in docs:
                    target = ensure_data_index(es, d["team_esid"], ROLLUP_SOURCE, rollup_schema)
                    actions.append(
                        {
                            "_op_type": "index",
                            "_index": target,
                            "_id": rollup_doc_id(d["team_esid"], source, d["month"]),
                            "_source": d,
                        }
                    )
                written, errors = _bulk(es, actions) if actions else (0, [])
            except Exception as exc:  # noqa: BLE001
                row.update(status="rollup_failed", error=type(exc).__name__)
                report["indices"].append(row)
                report["ok"] = False
                continue
            row["rollup_written"] = written
            if errors or written != len(actions):
                row.update(status="rollup_failed", rollup_errors=len(errors))
                report["indices"].append(row)
                report["ok"] = False
                continue

            # 2. delete only after the rollup landed
            try:
                dq = _body(
                    es.delete_by_query(
                        index=index,
                        body={"query": retention_query(family["date_field"], cutoff)},
                        conflicts="proceed",
                        refresh=True,
                    )
                )
                row.update(
                    status="deleted", deleted=int(dq.get("deleted") or 0), delete_failures=len(dq.get("failures") or [])
                )
                if row["delete_failures"]:
                    report["ok"] = False
            except Exception as exc:  # noqa: BLE001
                row.update(status="delete_failed", error=type(exc).__name__)
                report["ok"] = False
            report["indices"].append(row)
    return report


# ── Raw JSON purge ────────────────────────────────────────────────────────────


def _chatwoot_doc_id(path: str) -> str:
    account = os.path.basename(os.path.dirname(path))
    try:
        with open(path, "r", encoding="utf-8") as fh:
            data = json.load(fh)
        account = str(data.get("chatwoot_account_esid") or account)
        conv = data.get("conversation_id")
        if conv not in (None, ""):
            return f"{account}_{conv}"
    except Exception:  # noqa: BLE001 — unreadable file: fall back to path-derived id
        pass
    return f"{account}_{os.path.splitext(os.path.basename(path))[0]}"


def _candidate_files(root: str, *, min_mtime_age: timedelta, now: datetime) -> list[str]:
    cutoff_ts = (now - min_mtime_age).timestamp()
    out: list[str] = []
    if not os.path.isdir(root):
        return out
    for dirpath, _dirs, files in os.walk(root):
        for name in files:
            if not name.endswith(".json"):
                continue
            path = os.path.join(dirpath, name)
            try:
                if os.path.getmtime(path) <= cutoff_ts:
                    out.append(path)
            except OSError:
                continue
    return sorted(out)


def _indexed_ids(es, pattern: str, ids: list[str]) -> set[str]:
    # exact; raises IncompleteLookupError rather than under-count.
    return existing_doc_ids(es, pattern, ids, batch=ID_BATCH)


def _classify_unindexed_echo(path: str, team_cache: dict | None) -> str:
    """Why an Echo file has no document, for the sweep report:

    * ``unattributed`` — Echo matched it to no team (team_esid and team_title
      both ``unknown``); nothing can ever index it.
    * ``team_unresolved`` — its team_title matches no DEAM team (a rename, a
      deleted team); fixing the mapping would recover it.
    * ``team_known`` — its team resolves, so it was simply never sent; a
      re-send recovers it.
    * ``unclassified`` — unreadable, or the team cache is unavailable.
    """
    try:
        with open(path, "r", encoding="utf-8") as fh:
            data = json.load(fh)
    except Exception:  # noqa: BLE001 — unreadable: not provably anything, so keep it
        return "unclassified"
    if (
        str(data.get("team_esid") or "").strip().lower() == UNATTRIBUTED_MARKER
        and str(data.get("team_title") or "").strip().lower() == UNATTRIBUTED_MARKER
    ):
        return "unattributed"
    if team_cache is None:
        return "unclassified"
    return "team_known" if resolve_team_esid(team_cache, team_title=data.get("team_title")) else "team_unresolved"


def purge_raw_json(es, *, now: datetime, apply: bool, roots: tuple[tuple[str, str, str], ...] = RAW_JSON_ROOTS) -> dict:
    """Delete raw conversation JSON that is ≥7 days old AND indexed in ES.

    A file is kept when it is younger than 7 days (the importer may still
    be re-saving it) or when its document is not in ES (nothing indexed it
    yet — deleting would lose the only copy). Directories are left in place.

    an Echo file Echo marked unattributed (see
    :func:`_is_unattributed`) that is not indexed is deleted once it is
    ``UNATTRIBUTED_MAX_AGE_DAYS`` old — no team can ever index it. It is
    counted under ``unattributed`` (``unattributed_due`` once old enough,
    ``unattributed_deleted`` when applied), not ``not_indexed``. Every other
    unindexed Echo file is explained by ``not_indexed_by_reason`` (see
    :func:`_classify_unindexed_echo`).
    """
    report: dict[str, Any] = {"apply": apply, "roots": [], "ok": True}
    unattributed_cutoff = (now - timedelta(days=UNATTRIBUTED_MAX_AGE_DAYS)).timestamp()
    team_cache: dict | None = None
    team_cache_tried = False
    for root, family_key, style in roots:
        if style == "dt":
            row = _purge_dt_root(es, root, now=now, apply=apply)
            if row.get("lookup_failed"):
                report["ok"] = False
            report["roots"].append(row)
            continue
        family = FAMILIES[family_key]
        row: dict[str, Any] = {
            "root": root,
            "candidates": 0,
            "indexed": 0,
            "not_indexed": 0,
            "deleted": 0,
            "unattributed": 0,
            "unattributed_due": 0,
            "unattributed_deleted": 0,
            "not_indexed_by_reason": {},
        }
        files = _candidate_files(root, min_mtime_age=timedelta(days=RAW_JSON_MIN_AGE_DAYS), now=now)
        row["candidates"] = len(files)
        if files:
            ids_by_path = {
                p: (os.path.splitext(os.path.basename(p))[0] if style == "echo" else _chatwoot_doc_id(p)) for p in files
            }
            try:
                present = _indexed_ids(es, family["pattern"], sorted(set(ids_by_path.values())))
            except Exception as exc:  # noqa: BLE001
                row.update(status="lookup_failed", error=type(exc).__name__)
                report["roots"].append(row)
                report["ok"] = False
                continue
            for path, doc_id in ids_by_path.items():
                if doc_id not in present:
                    reason = None
                    if style == "echo":
                        if not team_cache_tried:
                            team_cache_tried = True
                            try:
                                team_cache = build_team_lookup_cache(es)
                            except Exception as exc:  # noqa: BLE001 — report-only; files are kept either way
                                logging.warning("%s: team lookup unavailable: %s", LOG_PREFIX, type(exc).__name__)
                        reason = _classify_unindexed_echo(path, team_cache)
                    if reason == "unattributed":
                        row["unattributed"] += 1
                        if os.path.getmtime(path) <= unattributed_cutoff:
                            row["unattributed_due"] += 1
                            if apply:
                                try:
                                    os.remove(path)
                                    row["unattributed_deleted"] += 1
                                except OSError as exc:
                                    row["remove_errors"] = row.get("remove_errors", 0) + 1
                                    logging.warning("%s: could not remove %s: %s", LOG_PREFIX, path, type(exc).__name__)
                        continue
                    row["not_indexed"] += 1
                    if reason:
                        by = row["not_indexed_by_reason"]
                        by[reason] = by.get(reason, 0) + 1
                    continue
                row["indexed"] += 1
                if apply:
                    try:
                        os.remove(path)
                        row["deleted"] += 1
                    except OSError as exc:
                        row["remove_errors"] = row.get("remove_errors", 0) + 1
                        logging.warning("%s: could not remove %s: %s", LOG_PREFIX, path, type(exc).__name__)
        row["status"] = "purged" if apply else "dry_run"
        report["roots"].append(row)
    return report


def _dt_file_ids(path: str) -> tuple[str, list[str]] | None:
    """``(team_esid, ids)`` of a DT staging / archive file, or None if unreadable.

    Reads ``docs`` or ``posts`` (older files). Only the ids
    and the team are taken; nothing else in the file is looked at or logged.
    """
    try:
        with open(path, "r", encoding="utf-8") as fh:
            data = json.load(fh)
    except Exception:  # noqa: BLE001 — unreadable: not provably indexed, so keep it
        return None
    team_esid = str((data or {}).get("team_esid") or "").strip() if isinstance(data, dict) else ""
    if not team_esid:
        return None
    rows = data.get("docs") if "docs" in data else data.get("posts")
    ids = sorted({str(r.get("ID")) for r in rows or [] if isinstance(r, dict) and r.get("ID") not in (None, "")})
    return team_esid, ids


def _purge_dt_root(es, root: str, *, now: datetime, apply: bool) -> dict:
    """purge Disciple.Tools raw / archived files under ``root``.

    Same rule as the conversation roots — ≥7 days old AND indexed — with the DT
    meaning of indexed: every id in the file is present in the file's own team
    index. A file with no ids holds nothing to lose and is purged. A file whose
    team index no longer exists is kept and counted (``team_index_missing``);
    an ES lookup error keeps the file and marks the run not-ok.
    """
    row: dict[str, Any] = {
        "root": root,
        "candidates": 0,
        "indexed": 0,
        "not_indexed": 0,
        "deleted": 0,
        "not_indexed_by_reason": {},
    }
    files = _candidate_files(root, min_mtime_age=timedelta(days=RAW_JSON_MIN_AGE_DAYS), now=now)
    row["candidates"] = len(files)
    by = row["not_indexed_by_reason"]
    for path in files:
        parsed = _dt_file_ids(path)
        if parsed is None:
            row["not_indexed"] += 1
            by["unclassified"] = by.get("unclassified", 0) + 1
            continue
        team_esid, ids = parsed
        if ids:
            index = build_data_index_name(team_esid, DT_SOURCE)
            try:
                if not es.indices.exists(index=index):
                    row["not_indexed"] += 1
                    by["team_index_missing"] = by.get("team_index_missing", 0) + 1
                    continue
                present = _indexed_ids(es, index, ids)
            except Exception as exc:  # noqa: BLE001 — unknown is not "indexed": keep the file
                row["lookup_failed"] = row.get("lookup_failed", 0) + 1
                row["error"] = type(exc).__name__
                continue
            if set(ids) - present:
                row["not_indexed"] += 1
                by["ids_missing"] = by.get("ids_missing", 0) + 1
                continue
        row["indexed"] += 1
        if apply:
            try:
                os.remove(path)
                row["deleted"] += 1
            except OSError as exc:
                row["remove_errors"] = row.get("remove_errors", 0) + 1
                logging.warning("%s: could not remove %s: %s", LOG_PREFIX, path, type(exc).__name__)
    row["status"] = "purged" if apply else "dry_run"
    return row


# ── Disciple.Tools reconciliation ─────────────────────────────────────────────


def _remote_dt_ids(client, post_type: str) -> set[str]:
    return {str(p.get("ID")) for p in client.fetch_posts(post_type) if p.get("ID") not in (None, "")}


def reconcile_disciple_tools(
    es,
    *,
    apply: bool,
    force: bool = False,
    site_configs: Callable[[], list] | None = None,
    client_factory: Callable[..., Any] | None = None,
) -> dict:
    """Remove DT docs whose post the upstream site no longer has.

    Guards (each one skips the site/post_type and marks the run not-ok):
      * the DT API call fails → nothing is compared, nothing deleted;
      * the API returns zero posts while ES has docs → treated as a bad
        answer, not as "everything was deleted upstream";
      * more than ``DT_DELETE_RATIO_GUARD`` of ES docs would go → refused
        unless ``--force``.
    """
    if site_configs is None:
        from import_scripts.disciple_tools.connections import list_dt_site_configs

        site_configs = list_dt_site_configs
    if client_factory is None:
        from import_scripts.disciple_tools.client import DTSiteClient

        client_factory = DTSiteClient

    report: dict[str, Any] = {"apply": apply, "force": force, "sites": [], "ok": True}
    try:
        configs = list(site_configs())
    except Exception as exc:  # noqa: BLE001
        report.update(ok=False, error=type(exc).__name__)
        return report
    for cfg in configs:
        index = build_data_index_name(cfg.team_esid, DT_SOURCE)
        try:
            exists = bool(es.indices.exists(index=index))
        except Exception as exc:  # noqa: BLE001
            report["sites"].append(
                {"team_esid": cfg.team_esid, "index": index, "status": "exists_failed", "error": type(exc).__name__}
            )
            report["ok"] = False
            continue
        if not exists:
            report["sites"].append({"team_esid": cfg.team_esid, "index": index, "status": "no_index"})
            continue
        client = client_factory(cfg.dt_domain, cfg.our_domain, cfg.token)
        for post_type in DT_POST_TYPES:
            row: dict[str, Any] = {"team_esid": cfg.team_esid, "index": index, "post_type": post_type}
            try:
                es_ids = _scan_ids(es, index, {"term": {"post_type": post_type}})
            except Exception as exc:  # noqa: BLE001
                row.update(status="es_scan_failed", error=type(exc).__name__)
                report["sites"].append(row)
                report["ok"] = False
                continue
            try:
                remote_ids = _remote_dt_ids(client, post_type)
            except Exception as exc:  # noqa: BLE001 — auth errors, HTTP errors: skip, never delete
                row.update(status="api_failed", error=type(exc).__name__, es_docs=len(es_ids))
                report["sites"].append(row)
                report["ok"] = False
                continue
            missing = sorted(es_ids - remote_ids)
            row.update(es_docs=len(es_ids), remote_posts=len(remote_ids), missing=len(missing))
            if not missing:
                row["status"] = "in_sync"
                report["sites"].append(row)
                continue
            if not remote_ids:
                row["status"] = "refused_empty_api"
                report["sites"].append(row)
                report["ok"] = False
                continue
            ratio = len(missing) / max(1, len(es_ids))
            row["ratio"] = round(ratio, 3)
            if ratio > DT_DELETE_RATIO_GUARD and not force:
                row["status"] = "refused_ratio_guard"
                report["sites"].append(row)
                report["ok"] = False
                continue
            if not apply:
                row["status"] = "dry_run"
                report["sites"].append(row)
                continue
            try:
                deleted, errors = _bulk(es, ({"_op_type": "delete", "_index": index, "_id": i} for i in missing))
                row.update(status="deleted", deleted=deleted, delete_errors=len(errors))
                if errors:
                    report["ok"] = False
            except Exception as exc:  # noqa: BLE001
                row.update(status="delete_failed", error=type(exc).__name__)
                report["ok"] = False
            report["sites"].append(row)
    return report


# ── SLM check ─────────────────────────────────────────────────────────────────


def _duration_days(value: str) -> float | None:
    v = str(value or "").strip().lower()
    units = {"d": 1.0, "h": 1 / 24, "m": 1 / 1440, "s": 1 / 86400, "w": 7.0}
    for suffix, factor in units.items():
        if v.endswith(suffix):
            try:
                return float(v[: -len(suffix)]) * factor
            except ValueError:
                return None
    return None


def check_slm_retention(es, policy_id: str = SLM_POLICY) -> dict:
    """Report the SLM policy's ``expire_after`` against the agreed 30 days.

    Read-only: the policy is provisioned by ``deployment/09_provision_slm.sh``
     and converged by the deploy wrapper's ``--check``; a drift seen
    here is reported for an operator, never corrected mid-sweep.
    """
    try:
        body = _body(es.slm.get_lifecycle(policy_id=policy_id))
        policy = ((body.get(policy_id) or {}).get("policy")) or {}
        expire_after = str(((policy.get("retention") or {}).get("expire_after")) or "")
    except Exception as exc:  # noqa: BLE001
        return {"policy": policy_id, "status": "unavailable", "error": type(exc).__name__, "ok": False}
    days = _duration_days(expire_after)
    if days is None:
        return {"policy": policy_id, "status": "no_retention", "expire_after": expire_after, "ok": False}
    ok = days <= SLM_MAX_RETENTION_DAYS
    return {"policy": policy_id, "status": "ok" if ok else "exceeds_30d", "expire_after": expire_after, "days": days, "ok": ok}


# ── Run record + heartbeat ────────────────────────────────────────────────────


def record_run(
    es,
    *,
    run_id: str,
    kind: str,
    apply: bool,
    ok: bool,
    started: datetime,
    finished: datetime,
    cutoff: datetime,
    summary: dict,
) -> None:
    try:
        if not create_index_if_not_exists(es, RUNS_INDEX, RUNS_MAPPING):
            logging.error("%s: could not ensure %s — run record skipped", LOG_PREFIX, RUNS_INDEX)
            return
        es.index(
            index=RUNS_INDEX,
            id=run_id,
            body={
                "run_id": run_id,
                "kind": kind,
                "apply": apply,
                "ok": ok,
                "started_at": _iso(started),
                "finished_at": _iso(finished),
                "cutoff": _iso(cutoff),
                "summary": summary,
            },
        )
        es.index(
            index=RUNS_INDEX,
            id=HEARTBEAT_ID,
            body={
                "run_id": run_id,
                "kind": "heartbeat",
                "apply": apply,
                "ok": ok,
                "last_run_at": _iso(finished),
                "cutoff": _iso(cutoff),
            },
            refresh="wait_for",
        )
    except Exception as exc:  # noqa: BLE001
        logging.error("%s: run record / heartbeat failed: %s", LOG_PREFIX, type(exc).__name__)


def check_heartbeat(max_age_hours: int) -> int:
    """Exit 0 when the sweep ran recently, 1 when it did not (or never)."""
    doc = get_document_by_id(RUNS_INDEX, HEARTBEAT_ID)
    if not doc:
        print("CRITICAL: retention sweep has NO heartbeat row — the job has never completed a run on this cluster.")
        return 1
    raw = str(doc.get("last_run_at") or "")
    try:
        last = datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError:
        print(f"CRITICAL: retention sweep heartbeat carries an unparseable last_run_at ({raw!r}).")
        return 1
    age = _now() - last
    mode = "apply" if doc.get("apply") else "dry-run"
    if age > timedelta(hours=max_age_hours):
        print(
            f"CRITICAL: retention sweep last ran {age.total_seconds() / 3600:.1f}h ago ({raw}, {mode}) — older than {max_age_hours}h."
        )
        return 1
    print(f"OK: retention sweep ran {age.total_seconds() / 3600:.1f}h ago ({raw}, {mode}, ok={doc.get('ok')}).")
    return 0


# ── Entry point ───────────────────────────────────────────────────────────────


def _log_report(title: str, rows: list[dict]) -> None:
    logging.info("%s: %s — %d row(s)", LOG_PREFIX, title, len(rows))
    for row in rows:
        logging.info("%s:   %s", LOG_PREFIX, json.dumps(row, sort_keys=True, default=str))


def run(*, apply: bool, conversations: bool, dt: bool, force: bool, now: datetime | None = None) -> int:
    started = now or _now()
    es = get_elasticsearch_client()
    if es is None:
        logging.error("%s: no Elasticsearch connection", LOG_PREFIX)
        return 1
    cutoff = retention_cutoff(started)
    summary: dict[str, Any] = {}
    ok = True
    logging.info(
        "%s: mode=%s cutoff=%s (retention %d months)",
        LOG_PREFIX,
        "APPLY" if apply else "DRY-RUN",
        _iso(cutoff),
        RETENTION_MONTHS,
    )

    if conversations:
        rd = rollup_and_delete(es, cutoff=cutoff, now=started, apply=apply)
        _log_report("rollup + delete", rd["indices"])
        summary["conversations"] = rd
        ok &= bool(rd["ok"])

        pj = purge_raw_json(es, now=started, apply=apply)
        _log_report("raw JSON purge", pj["roots"])
        summary["raw_json"] = pj
        ok &= bool(pj["ok"])

        slm = check_slm_retention(es)
        logging.info("%s: SLM %s", LOG_PREFIX, json.dumps(slm, sort_keys=True))
        summary["slm"] = slm
        # informational: drift is reported, not a sweep failure

    if dt:
        dr = reconcile_disciple_tools(es, apply=apply, force=force)
        _log_report("Disciple.Tools reconcile", dr["sites"])
        summary["disciple_tools"] = dr
        ok &= bool(dr["ok"])

    finished = _now()
    kind = "dt_only" if (dt and not conversations) else ("full" if dt else "conversations")
    record_run(
        es,
        run_id=str(uuid.uuid4()),
        kind=kind,
        apply=apply,
        ok=ok,
        started=started,
        finished=finished,
        cutoff=cutoff,
        summary=summary,
    )
    logging.info("%s: done ok=%s in %.1fs", LOG_PREFIX, ok, (finished - started).total_seconds())
    return 0 if ok else 1


def main(argv: list[str] | None = None) -> int:
    lib.logging.define(LOG_PREFIX)
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--apply", action="store_true", help="perform the rollup, deletes and purge (default: dry-run)")
    ap.add_argument("--dt-reconcile", action="store_true", help="also reconcile Disciple.Tools ids against each site")
    ap.add_argument("--only-dt", action="store_true", help="reconcile Disciple.Tools only (skip conversations)")
    ap.add_argument("--force", action="store_true", help="DT: allow deleting more than 50%% of a site's docs")
    ap.add_argument("--check-heartbeat", action="store_true", help="do not sweep; exit non-zero if the last run is stale")
    ap.add_argument("--max-age-hours", type=int, default=DEFAULT_MAX_AGE_HOURS)
    args = ap.parse_args(argv)
    if args.check_heartbeat:
        return check_heartbeat(args.max_age_hours)
    return run(
        apply=args.apply,
        conversations=not args.only_dt,
        dt=args.dt_reconcile or args.only_dt,
        force=args.force,
    )


if __name__ == "__main__":
    sys.exit(main())
