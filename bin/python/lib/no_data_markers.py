"""
Shared utility for writing no_data marker documents to Elasticsearch.

When an import runs for a date and finds zero results for a team,
a lightweight marker doc is written so the status page can distinguish
"never pulled" from "pulled but empty."

Markers use a deterministic ID — `{team_esid}_{kibana_date}_no_data` —
so re-running an import for the same date is idempotent: ES upserts
the existing marker instead of writing a duplicate.

A separate `cleanup_no_data_markers` tool handles the case
where a marker is left behind on a date that later got real data.

When a marker is the *first* doc written to a team's index, the writer
explicitly creates the index with the unified schema. Without that, ES
auto-detects string fields as `text + .keyword` — which is wrong for
keyword-aggregation queries and is
how 14 marker-only indices on staging ended up broken until they were
re-created.

Public entry points:
- write_no_data_markers — per-import hook, called once per (date, source)
  by the import scripts after the ad-loop finishes.
- backfill_missing_no_data_markers — one-shot for filling historical
  gaps without re-running an import. Queries each team's
  index for date-coverage, computes the [start, end] gap-set, and
  upserts one marker per gap-date. The companion CLI lives at
  bin/python/scripts/backfill_no_data_markers.py.
"""

import logging
from datetime import datetime, timedelta, timezone

from lib.es_helpers import (
    create_index_if_not_exists,
    get_elasticsearch_client,
    sanitize_team_title,  # noqa: F401 — re-exported for back-compat
)


# Map index suffix → schema_loader index_type. Suffixes not in this map
# (e.g. "_youtube_analytics", "_google_ads", "_chatwoot_insights") will be
# created with no explicit schema; ES will auto-detect — those platforms
# don't yet have a unified schema registered. Add an entry here when they do.
_SUFFIX_TO_SCHEMA_TYPE = {
    "_fb_insights": "fb_insights",
    "_fb_page_insights": "fb_page_insights",
    "_ig_insights": "ig_insights",
    "_ga_metrics": "ga_metrics",
}


def _ensure_index_with_schema(es, index_name, index_suffix):
    """Create the index with the unified schema if it doesn't already exist.

    Returns True on success (index exists or was created). Returns True even
    when no schema is registered for the suffix — callers fall back to
    es-auto-detected mappings the same way they did before this helper.
    """
    schema_type = _SUFFIX_TO_SCHEMA_TYPE.get(index_suffix)
    if not schema_type:
        return True  # caller will let ES auto-create on the index call
    try:
        from migration.schema_loader import get_schema_for_index_type

        schema = get_schema_for_index_type(schema_type)
        return create_index_if_not_exists(es, index_name, schema)
    except Exception as e:
        # Don't block marker writes if schema lookup fails for any reason —
        # ES will auto-create on the next es.index call.
        logging.warning(f"Schema-on-create for {index_name} failed ({e}); falling back to auto-detect")
        return True


def marker_doc_id(team_esid, kibana_date):
    """Deterministic ID for a no_data marker — one per (team, date)."""
    return f"{team_esid}_{kibana_date}_no_data"


def _upsert_marker(es, index_name, team_esid, team_title, date):
    """Index (or overwrite) a single marker doc with a deterministic ID."""
    doc = {
        "kibana_date": date,
        "team_title": team_title,
        "team_esid": team_esid,
        "no_data": True,
        "@timestamp": datetime.now(timezone.utc).isoformat(),
    }
    marker_id = marker_doc_id(team_esid, date)
    try:
        resp = es.index(
            index=index_name,
            id=marker_id,
            document=doc,
            refresh="wait_for",
        )
        # log per-call outcome so a prod log shows
        # whether each marker actually landed (created vs updated vs no-op)
        # — distinguishes "writer ran but ES rejected" from "writer fired
        # cleanly". The `result` field is "created" / "updated" / "noop".
        # `resp` is an ObjectApiResponse on real ES and a dict on tests;
        # both support .get(), so probe that directly.
        try:
            result = resp.get("result")
        except Exception:
            result = None
        logging.info(f"_upsert_marker[{index_name}/{marker_id}]: result={result!r}")
        return True
    except Exception as e:
        logging.warning(f"Failed to upsert no_data marker {marker_id} to {index_name}: " f"{type(e).__name__}: {e}")
        return False


def write_no_data_markers(date, teams_with_data, index_suffix, team_mapping=None, team_list=None):
    """No-op. The new dashboard design no longer reads
    no_data markers, so the subsystem is library-level neutralized to stop
    write floods across every importer in one shot. Existing markers in ES
    are left in place — they're inert. Function signature preserved so the
    36+ call sites across importers stay untouched.
    """
    logging.debug(f"write_no_data_markers[{date}{index_suffix}]: no-op")


def _iter_dates(start, end):
    """Yield ISO YYYY-MM-DD strings from ``start`` to ``end`` inclusive."""
    cur = datetime.strptime(start, "%Y-%m-%d").date()
    last = datetime.strptime(end, "%Y-%m-%d").date()
    while cur <= last:
        yield cur.isoformat()
        cur += timedelta(days=1)


def _team_dates_with_real_docs(es, index_name, start, end):
    """Return a set of ISO date strings for which ``index_name`` has at least
    one non-marker doc in ``[start, end]``. Returns an empty set if the index
    doesn't exist."""
    try:
        resp = es.search(
            index=index_name,
            body={
                "size": 0,
                "query": {
                    "bool": {
                        "must": [{"range": {"kibana_date": {"gte": start, "lte": end}}}],
                        "must_not": [{"term": {"no_data": True}}],
                    }
                },
                "aggs": {"by_date": {"terms": {"field": "kibana_date", "size": 10000}}},
            },
            ignore_unavailable=True,
            allow_no_indices=True,
        )
    except Exception as e:
        logging.warning(f"backfill: could not query {index_name} for date coverage: {e}")
        return set()
    buckets = resp.get("aggregations", {}).get("by_date", {}).get("buckets", [])
    return {b["key_as_string"][:10] if "key_as_string" in b else str(b["key"])[:10] for b in buckets}


def backfill_missing_no_data_markers(start, end, index_suffix, team_mapping=None, team_list=None):
    """No-op. See ``write_no_data_markers``. Returns 0 so
    callers that check the return value (e.g. the CLI at
    scripts/backfill_no_data_markers.py) see "wrote 0 markers" instead of
    raising.
    """
    logging.debug(f"backfill_missing_no_data_markers[{start}..{end}{index_suffix}]: no-op")
    return 0
