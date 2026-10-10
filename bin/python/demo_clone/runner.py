"""Elasticsearch orchestration for the anonymized demo clone.

``run()`` resolves the source and target teams by exact title, then for each
source copies ``{source_esid}__{source}`` → ``{target_esid}__{source}`` through
:class:`demo_clone.transform.Transformer`, upserting on a pseudonymous ``_id``
so the daily cron is idempotent.

Contract (shared with the migrations):

* Dry-run by default. A dry run reads, transforms and counts; it creates and
  writes nothing.
* ``--recreate`` deletes the destination index first and is gated by
  :func:`migration.snapshot_gate.ensure_restore_point` — no restore point,
  no delete.
* The destination index is created under the unified schema for its source
  via ``create_index_if_not_exists``; a failed create refuses (—
  a bulk into a missing index would dynamic-map it).
* Refuses when source and target resolve to the same team, or when either
  title matches zero or several teams. Never guesses (see CLAUDE.md, Meta
  data model: decline and report).
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any, Iterable

from elasticsearch.helpers import bulk, scan

from demo_clone import CloneRefused, TeamResolutionError
from demo_clone.transform import RETIRED_SALTS, TargetTeam, Transformer, pseudonymous_doc_id, salt
from lib.es_helpers import build_data_index_name, create_index_if_not_exists
from migration.schema_loader import get_schema_for_index_type
from migration.snapshot_gate import MigrationRefused, ensure_restore_point

logger = logging.getLogger(__name__)

# The sources the demo team takes from the source team. disciple_tools stays
# with the synthetic seeder (richer, and the real one is people); chatwoot_*
# stays with the generator (the source team has no Chatwoot).
CLONED_SOURCES: tuple[str, ...] = (
    "fb_insights",
    "fb_insights_window",
    "fb_page_insights",
    "fb_page_posts",
    "ig_insights",
    "ig_insights_window",
    "google_ads",
    "ga_metrics",
    "ga_metrics_window",
    "youtube_analytics",
    "bitly_clicks",
    "echo_conversations",
)

SNAPSHOT_PREFIX = "demo-clone"
_SCAN_SIZE = 1000
_BULK_CHUNK = 500
# how many source _ids per index to re-key under each retired salt
# and look for in the destination after an apply. Enough to catch a stale
# clone; cheap enough for the daily cron.
_RETIRED_SALT_SAMPLE = 50


@dataclass(frozen=True)
class TeamRef:
    """A resolved ``de_asset_teams`` document."""

    esid: str
    title: str
    doc: dict[str, Any] = field(default_factory=dict)


def resolve_team(es: Any, title: str) -> TeamRef:
    """Exactly one team with this exact ``team_title``, or raise.

    ``team_title`` is keyword-mapped in ``de_asset_teams``; a ``term`` query is
    exact. Workspaces allow duplicate titles across tenants, so
    two hits are a refusal, not a coin toss — writing one team's data under
    another team's esid is the cross-tenant error this whole package exists
    to avoid.
    """
    resp = es.search(index="de_asset_teams", body={"size": 5, "query": {"term": {"team_title": title}}})
    hits = [h for h in resp["hits"]["hits"] if h["_source"].get("team_title") == title]
    if len(hits) != 1:
        raise TeamResolutionError(f"team_title {title!r} matched {len(hits)} team(s) in de_asset_teams; need exactly 1")
    hit = hits[0]
    return TeamRef(esid=hit["_id"], title=title, doc=dict(hit["_source"]))


def target_from(team: TeamRef) -> TargetTeam:
    """Build the identity stamped onto every cloned document."""
    doc = team.doc
    return TargetTeam(
        esid=team.esid,
        title=team.title,
        organization_title=doc.get("organization_title"),
        site_url=doc.get("team_site_url") or None,
        country=doc.get("team_country") or None,
        city=doc.get("team_city") or None,
        country_iso3=doc.get("team_country_iso3_code") or None,
    )


def _query(since: str | None) -> dict[str, Any]:
    if since is None:
        return {"query": {"match_all": {}}}
    return {"query": {"range": {"@timestamp": {"gte": f"now-{since}"}}}}


def _actions(
    dest: str,
    transformer: Transformer,
    hits: Iterable[dict[str, Any]],
    counters: dict[str, int],
    sample_ids: list[str] | None = None,
) -> Iterable[dict[str, Any]]:
    for hit in hits:
        counters["scanned"] += 1
        if sample_ids is not None and len(sample_ids) < _RETIRED_SALT_SAMPLE:
            sample_ids.append(hit["_id"])
        yield {
            "_op_type": "index",
            "_index": dest,
            "_id": pseudonymous_doc_id(hit["_id"]),
            "_source": transformer.transform(hit["_source"]),
        }


def count_retired_salt_docs(es: Any, dest_index: str, source_ids: Iterable[str]) -> dict[str, int]:
    """How many destination docs are still keyed under each RETIRED salt.

    For every retired salt, re-derive the ``_id`` each sampled source doc would
    have had under it and count those ids in the destination. A non-zero count
    means the demo index still holds documents pseudonymized with a public
    salt — i.e. the rotation was not followed by a ``--full --recreate`` — and
    the run reports it as an error. Zero for every salt is the proof the
    ticket asks for: prior pseudonyms no longer match anything.
    """
    ids = list(source_ids)
    found: dict[str, int] = {}
    if not ids or not es.indices.exists(index=dest_index):
        return {retired: 0 for retired in RETIRED_SALTS}
    for retired in RETIRED_SALTS:
        stale_ids = [pseudonymous_doc_id(src_id, salt_value=retired) for src_id in ids]
        resp = es.count(index=dest_index, body={"query": {"ids": {"values": stale_ids}}})
        found[retired] = int((resp or {}).get("count", 0))
    return found


def clone_source(
    es: Any,
    *,
    source: str,
    source_esid: str,
    target: TargetTeam,
    since: str | None,
    dry_run: bool,
    recreate: bool,
    snapshot_repo: str | None,
) -> dict[str, Any]:
    """Clone one source index. Returns a per-source report dict."""
    src_index = build_data_index_name(source_esid, source)
    dest_index = build_data_index_name(target.esid, source)
    report: dict[str, Any] = {
        "source": source,
        "source_index": src_index,
        "dest_index": dest_index,
        "scanned": 0,
        "written": 0,
        "errors": [],
        "dropped_unclassified": {},
        "recreated": False,
        "skipped": None,
        "retired_salt_docs": {},
    }

    if not es.indices.exists(index=src_index):
        report["skipped"] = "source index missing"
        logger.warning("[%s] %s does not exist — skipped", source, src_index)
        return report

    if recreate and es.indices.exists(index=dest_index):
        if dry_run:
            logger.info("[%s] DRY RUN would delete and recreate %s", source, dest_index)
        else:
            try:
                restore = ensure_restore_point(es, [dest_index], snapshot_repo=snapshot_repo, name_prefix=SNAPSHOT_PREFIX)
            except MigrationRefused as exc:
                raise CloneRefused(str(exc)) from exc
            logger.info("[%s] restore point: %s — deleting %s", source, restore, dest_index)
            es.indices.delete(index=dest_index)
        report["recreated"] = True

    if not dry_run:
        schema = get_schema_for_index_type(source)
        if not create_index_if_not_exists(es, dest_index, schema):
            raise CloneRefused(f"could not create {dest_index} under its schema; refusing to bulk into it")

    transformer = Transformer(source, target)
    counters = {"scanned": 0}
    sample_ids: list[str] = []
    hits = scan(es, index=src_index, query=_query(since), size=_SCAN_SIZE, preserve_order=False, request_timeout=120)
    actions = _actions(dest_index, transformer, hits, counters, sample_ids)

    if dry_run:
        for _ in actions:
            pass
        report["scanned"] = counters["scanned"]
        report["written"] = 0
    else:
        ok, errors = bulk(es, actions, chunk_size=_BULK_CHUNK, raise_on_error=False, refresh="wait_for", request_timeout=120)
        report["scanned"] = counters["scanned"]
        report["written"] = ok
        report["errors"] = [str(e)[:300] for e in (errors or [])]
        # prove no document in the destination is still keyed under a
        # retired (public) salt. Runs on every apply, so the daily cron keeps
        # re-verifying the rotation.
        report["retired_salt_docs"] = count_retired_salt_docs(es, dest_index, sample_ids)
        stale = {k: v for k, v in report["retired_salt_docs"].items() if v}
        if stale:
            msg = f"{dest_index} still holds docs keyed by a RETIRED salt {stale} — run --full --recreate"
            logger.error("[%s] %s", source, msg)
            report["errors"].append(msg)

    report["dropped_unclassified"] = dict(transformer.unclassified)
    if transformer.unclassified:
        logger.warning(
            "[%s] %d field path(s) had no policy and were DROPPED (add a rule in demo_clone/policy.py): %s",
            source,
            len(transformer.unclassified),
            sorted(transformer.unclassified),
        )
    logger.info(
        "[%s] %s → %s: scanned %d, written %d, errors %d%s",
        source,
        src_index,
        dest_index,
        report["scanned"],
        report["written"],
        len(report["errors"]),
        " (dry run)" if dry_run else "",
    )
    return report


def run(
    es: Any,
    *,
    source_team_title: str,
    target_team_title: str,
    sources: Iterable[str] = CLONED_SOURCES,
    since: str | None = "3d",
    dry_run: bool = True,
    recreate: bool = False,
    snapshot_repo: str | None = None,
) -> dict[str, Any]:
    """Clone every requested source from one team to another. Returns the run report."""
    sources = tuple(sources)
    unknown = sorted(set(sources) - set(CLONED_SOURCES))
    if unknown:
        raise CloneRefused(f"not clonable: {unknown}. Clonable sources: {list(CLONED_SOURCES)}")
    # fail before touching Elasticsearch when the secret salt is
    # absent, short, or the retired public value (SaltMissing is a CloneRefused).
    salt()

    source_team = resolve_team(es, source_team_title)
    target_team = resolve_team(es, target_team_title)
    if source_team.esid.lower() == target_team.esid.lower():
        raise CloneRefused(f"source and target resolve to the same team ({source_team.esid}); nothing to clone")
    target = target_from(target_team)

    logger.info(
        "Cloning %d source(s) %s (%s) → %s (%s), since=%s%s",
        len(sources),
        source_team.title,
        source_team.esid,
        target_team.title,
        target_team.esid,
        since or "ALL",
        " [DRY RUN]" if dry_run else "",
    )

    report: dict[str, Any] = {
        "dry_run": dry_run,
        "source_team": {"title": source_team.title, "esid": source_team.esid},
        "target_team": {"title": target_team.title, "esid": target_team.esid},
        "since": since,
        "sources": [],
        "errors": [],
    }
    for source in sources:
        per = clone_source(
            es,
            source=source,
            source_esid=source_team.esid,
            target=target,
            since=since,
            dry_run=dry_run,
            recreate=recreate,
            snapshot_repo=snapshot_repo,
        )
        report["sources"].append(per)
        report["errors"].extend(f"{source}: {e}" for e in per["errors"])

    total_scanned = sum(p["scanned"] for p in report["sources"])
    total_written = sum(p["written"] for p in report["sources"])
    logger.info(
        "Done%s: %d doc(s) scanned, %d written, %d error(s) across %d source(s).",
        " (dry run)" if dry_run else "",
        total_scanned,
        total_written,
        len(report["errors"]),
        len(report["sources"]),
    )
    return report
