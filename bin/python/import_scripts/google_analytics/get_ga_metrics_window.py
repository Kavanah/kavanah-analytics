"""
Custom-range (windowed) GA4 importer — PROPERTY grain, deduplicated.

Sister to ``get_ga_metrics.py`` (per-day, 8 breakdown dimensions). This one asks
GA4 for ONE arbitrary multi-day ``DateRange`` with **no dimensions** and writes a
single property-level row to ``{team_esid}__ga_metrics_window``.

WHY no dimensions (the whole point):
  GA4 ``activeUsers`` / ``totalUsers`` are counts of *unique* users. The per-day
  importer pulls them broken down by date + 8 dimensions, so the dashboard's
  unique-users tile (``aggregation="sum"``) over-counts massively over a range:
  a user active on N days across M dimension slices is summed N×M times.
  Reproduced on staging: the tile showed
  11,582 vs a single-day max of 1,158 — a ~3-10× inflation.

  GA4 only returns a deduplicated user count when asked for the metric over the
  whole window WITHOUT a breakdown that splits the audience. A zero-dimension
  RunReport for [start, end] returns ONE row whose activeUsers/totalUsers are
  GA4's true deduplicated counts for the range — exactly analogous to FB's
  combined cross-platform "all" reach doc.

  Filtering deduped unique-users by country/platform is intentionally NOT
  supported here (same limitation as FB reach): GA4 can't return a deduped count
  for a filtered audience subset in one call. The per-day index keeps the
  dimensioned data for every other (additive) chart.

The window doc carries the FB window-doc shape (``insight_window_start/end/label``)
so the dashboard's ``get_latest_window`` date-picker auto-pin works unchanged.

 (event grain): a SECOND RunReport per property, grouped by ``eventName``
alone, writes one ``grain="event"`` doc per event into the same index — GA4's
deduplicated per-event ``totalUsers`` / ``activeUsers`` for the window (a teammate's
Firebase events table). The property doc is stamped ``grain="property"``.

Usage::

    python import.py google_analytics get_ga_metrics_window \\
        --since 2026-05-01 --until 2026-05-31 [--property_ids id1,id2]

``--since`` / ``--until`` are required. A single-day window is allowed but the
caller (run_daily_imports) skips it — the per-day doc already holds that day's
(trivially deduplicated) figure.
"""

import hashlib
import json
import logging
import os
import sys
from datetime import datetime, timezone

sys.path.insert(0, "/app/python")
import lib.logging as custom_logging
from google.analytics.data_v1beta.types import DateRange, Dimension, Metric, RunReportRequest
from google.auth.exceptions import RefreshError

from import_scripts.google_analytics.lib.ga_config import GAConfig
from import_scripts.google_analytics.get_ga_metrics import (
    _ga4_data_client_for_property,
    _revoke_ga4_connection_for_property,
    load_properties_from_assets,
)
from import_scripts.facebook.get_page_insights import _resolve_team_country

# Property-level deduplicated metrics. The non-additive ones (activeUsers,
# totalUsers) are the reason this index exists; the additive ones ride along so
# a single window row can show them without a second index hit.
#
# the property-level ratios (averageSessionDuration, bounceRate,
# engagementRate) are added here so the KPI tiles ("Average engagement time")
# read a single correct property-level figure. GA4 computes these for the whole
# window in one zero-dimension call, so they are NOT summed/averaged across
# rows — there is exactly one window row per property. (Per-page bounce/
# engagement live on the daily `pages` report instead; see get_ga_metrics.py.)
_WINDOW_METRICS = [
    "activeUsers",
    "totalUsers",
    "sessions",
    "screenPageViews",
    "engagedSessions",
    "eventCount",
    "userEngagementDuration",
    "averageSessionDuration",
    "bounceRate",
    "engagementRate",
]

# Window metrics that are floats (seconds or 0..1 ratios), not integer counts.
_FLOAT_WINDOW_METRICS = frozenset({"userEngagementDuration", "averageSessionDuration", "bounceRate", "engagementRate"})

# the EVENT grain. GA4's Events table (event name x event count x total
# users x event count per active user) needs unique-user counts PER EVENT over the
# whole window. Those only come back deduplicated from a RunReport grouped by
# eventName alone (no date / linkUrl / platform split — the daily ``events``
# report has all three, so summing its rows over-counts exactly like).
# One ``grain="event"`` doc per (property, eventName, window) lands in the SAME
# window index beside the property doc (the FB multi-grain pattern),
# discriminated by ``grain``. The property doc is stamped ``grain="property"``.
#
# eventCount is additive and ALSO on the daily index; it rides here so the doc
# is self-describing. eventCountPerUser is a per-row ratio: correct at row grain,
# never summed (the read path registers it as non-additive).
_EVENT_DIMENSION = "eventName"
_EVENT_METRICS = [
    "eventCount",
    "totalUsers",
    "activeUsers",
    "eventCountPerUser",
]
_FLOAT_EVENT_METRICS = frozenset({"eventCountPerUser"})
_GRAIN_PROPERTY = "property"
_GRAIN_EVENT = "event"

_OUTPUT_DIR = "/app/data/google_analytics/ga_metrics_window"


def _fetch_property_window(data_client, property_id, since, until):
    """One zero-dimension RunReport over [since, until] → a single deduped row.

    Returns a dict of metric_name -> value, or None when GA4 returns no row
    (a property with zero traffic in the window).
    """
    request = RunReportRequest(
        property=f"properties/{property_id}",
        dimensions=[],  # NO breakdown — property-level deduplicated total
        metrics=[Metric(name=m) for m in _WINDOW_METRICS],
        date_ranges=[DateRange(start_date=since, end_date=until)],
    )
    response = data_client.run_report(request)
    rows = list(response.rows)
    if not rows:
        return None
    # Zero-dimension report → exactly one row of metric values, ordered as
    # requested.
    values = rows[0].metric_values
    out = {}
    for i, name in enumerate(_WINDOW_METRICS):
        raw = values[i].value if i < len(values) else "0"
        # users / sessions / views / events are integers; the duration + ratio
        # metrics (averageSessionDuration, bounceRate, engagementRate)
        # are floats.
        out[name] = float(raw or 0) if name in _FLOAT_WINDOW_METRICS else int(raw or 0)
    return out


def _fetch_event_window(data_client, property_id, since, until):
    """ONE eventName-dimensioned RunReport over [since, until].

    Returns a list of ``{"eventName": str, <metric>: value, ...}`` rows — one per
    event, each carrying GA4's deduplicated per-event unique-user counts for the
    whole window. Empty list when the property had no events in the window.
    The single dimension is the load-bearing detail: any further breakdown
    (date, platform, linkUrl) would re-split the audience and over-count.
    """
    request = RunReportRequest(
        property=f"properties/{property_id}",
        dimensions=[Dimension(name=_EVENT_DIMENSION)],  # eventName ONLY
        metrics=[Metric(name=m) for m in _EVENT_METRICS],
        date_ranges=[DateRange(start_date=since, end_date=until)],
        limit=10000,
    )
    response = data_client.run_report(request)
    rows = []
    for row in response.rows:
        dims = list(row.dimension_values)
        event_name = dims[0].value if dims else ""
        if not event_name:
            continue
        values = row.metric_values
        out = {_EVENT_DIMENSION: event_name}
        for i, name in enumerate(_EVENT_METRICS):
            raw = values[i].value if i < len(values) else "0"
            out[name] = float(raw or 0) if name in _FLOAT_EVENT_METRICS else int(raw or 0)
        rows.append(out)
    return rows


def _window_doc_id(property_id, team_esid, since, until, event_name=None):
    """Stable ES doc id so a re-pull overwrites the same doc. The property doc
    keeps its earlier id (no event suffix); event docs add the event name so
    two events — and an event vs. the property doc — never collide."""
    key = f"{property_id}-{team_esid}-{since}-{until}"
    if event_name is not None:
        key = f"{key}-event-{event_name}"
    return hashlib.md5(key.encode()).hexdigest()  # nosemgrep: insecure-hash-algorithm-md5 - non-cryptographic doc id


def build_event_window_docs(event_rows, property_id, property_name, team, since, until):
    """pure: map the per-event rows of one property window to ES docs.

    One ``grain="event"`` doc per row, with the same team/window envelope as the
    property doc so the dashboard's window snap (``insight_window_*``) and
    team scoping read both grains identically. Rows with no eventName are dropped
    (they cannot be attributed to an event and would collide on the doc id).
    """
    window_label = f"{since}..{until}"
    docs = []
    for row in event_rows:
        event_name = row.get(_EVENT_DIMENSION)
        if not event_name:
            continue
        doc = {k: v for k, v in row.items()}
        doc["grain"] = _GRAIN_EVENT
        doc["property_id"] = property_id
        doc["property_name"] = property_name
        doc["team_esid"] = team["team_esid"]
        doc["team_title"] = team["team_title"]
        doc["organization_title"] = team.get("organization_title", "Unknown")
        doc["team_country"] = _resolve_team_country(team)
        doc["insight_window_start"] = since
        doc["insight_window_end"] = until
        doc["insight_window_label"] = window_label
        doc["kibana_date"] = since
        doc["@timestamp"] = datetime.now(timezone.utc).isoformat()
        doc["_doc_id"] = _window_doc_id(property_id, team["team_esid"], since, until, event_name=event_name)
        docs.append(doc)
    return docs


def _save_window_doc(doc, property_id, team_esid, since, until):
    """Write one property-level window doc. Keyed by (property, team, window) so
    a re-pull of the same window overwrites rather than accumulating dups."""
    property_dir = os.path.join(_OUTPUT_DIR, str(property_id), str(team_esid))
    os.makedirs(property_dir, exist_ok=True)
    filename = f"{since}_{until}.json"
    filepath = os.path.join(property_dir, filename)
    with open(filepath, "w") as f:
        f.write(json.dumps(doc) + "\n")
    logging.info("ga-window: wrote %s", filepath)


def _save_event_window_docs(docs, property_id, team_esid, since, until):
    """write all event-grain docs of one property window as ONE
    newline-delimited file (the sender reads NDJSON), keyed like the property
    file plus an ``__events`` suffix so a re-pull overwrites in place."""
    if not docs:
        return
    property_dir = os.path.join(_OUTPUT_DIR, str(property_id), str(team_esid))
    os.makedirs(property_dir, exist_ok=True)
    filepath = os.path.join(property_dir, f"{since}_{until}__events.json")
    with open(filepath, "w") as f:
        for doc in docs:
            f.write(json.dumps(doc) + "\n")
    logging.info("ga-window: wrote %d event-grain doc(s) to %s", len(docs), filepath)


def _is_cancel_requested(progress_run_id):
    """Fail-soft cancellation probe (mirrors get_ga_metrics)."""
    if not progress_run_id:
        return False
    try:
        from lib.import_progress_service import ImportProgressService

        return ImportProgressService.is_cancelling(progress_run_id)
    except Exception as exc:  # noqa: BLE001 — boundary
        logging.warning("ga-window is_cancelling raised: %s — proceeding", exc)
        return False


def main(params):
    custom_logging.define("ga-get-metrics-window")

    since = params.get("since")
    until = params.get("until")
    if not since or not until:
        raise ValueError("get_ga_metrics_window requires both --since and --until (the window bounds).")
    for label, value in (("since", since), ("until", until)):
        try:
            datetime.strptime(value, "%Y-%m-%d")
        except ValueError:
            raise ValueError(f"Invalid --{label} format: {value!r}. Expected YYYY-MM-DD.")
    if since > until:
        raise ValueError(f"--since ({since}) must be <= --until ({until}).")

    window_label = f"{since}..{until}"
    progress_run_id = params.get("progress_run_id")

    config = GAConfig()
    # OAuth-only (service account retired); client resolved per property.
    oauth_client_cache = {}

    # require an explicit property scope BEFORE the ES fetch, same as
    # the daily importer (get_ga_metrics.py). No legitimate caller omits
    # -property_ids: cron is retired, the manual Import Data Page sends
    # the selected team, and backfill reuses the scheduler dialog. An absent flag
    # must fail loudly rather than fetch fleet-wide.
    # the flag scopes the ES FETCH itself so out-of-scope properties
    # never get a team lookup; the late filter below stays as a safety net.
    from lib.import_scope import require_scope, scope_resolution_error

    property_ids_filter = require_scope(params, scope_param="property_ids", connector_id="ga4")
    scoped_property_ids = [p.strip() for p in str(property_ids_filter).split(",") if p.strip()]

    properties = load_properties_from_assets(property_ids=scoped_property_ids)
    if not properties:
        # scope provided but the scoped fetch matched nothing. Pre-fix
        # this warned and returned rc 0, so the scheduler recorded a success for a
        # run that imported nothing (the failure mode).
        raise scope_resolution_error(
            scope_param="property_ids",
            scope_value=property_ids_filter,
            connector_id="ga4",
        )

    # property_ids_filter is guaranteed truthy by require_scope; conditional kept
    # so this reads the same as the daily importer's safety net.
    if property_ids_filter:
        wanted = {p.strip().split("/")[-1] for p in str(property_ids_filter).split(",") if p.strip()}
        before = len(properties)
        properties = [p for p in properties if str(p["property_id"]).split("/")[-1] in wanted]
        logging.info("ga-window: --property_ids filter %d -> %d properties", before, len(properties))
        if not properties:
            # see the daily importer: the exact-match net emptied the
            # list, so no requested property survived id normalisation.
            raise scope_resolution_error(
                scope_param="property_ids",
                scope_value=property_ids_filter,
                connector_id="ga4",
            )

    written = 0
    events_written = 0
    for prop in properties:
        if _is_cancel_requested(progress_run_id):
            logging.info("ga-window: cancellation observed; stopping (windows written so far retained).")
            break

        property_id = prop["property_id"]
        property_name = prop["property_name"]
        team = prop["team_metadata"]

        # OAuth-only. Skip the property if its team has no GA4 OAuth
        # connection (no service-account fallback any more).
        data_client = _ga4_data_client_for_property(property_id, oauth_client_cache)
        if data_client is None:
            logging.warning(
                "ga-window: skipping property %s (%s) — no GA4 OAuth connection for its team.",
                property_name,
                property_id,
            )
            continue
        try:
            metrics = _fetch_property_window(data_client, property_id, since, until)
        except RefreshError as e:
            # Dead OAuth token: flip the connection to revoked and skip; no SA to
            # retry on.
            logging.warning("ga-window: OAuth refresh failed for property %s: %s — skipping.", property_id, e)
            _revoke_ga4_connection_for_property(property_id)
            continue
        except Exception as e:  # noqa: BLE001 — per-property boundary; one bad property must not abort the run
            logging.warning("ga-window: fetch failed for property %s: %s — skipping.", property_id, e)
            continue

        if not metrics:
            logging.info("ga-window: property %s had no data in %s; skipping.", property_id, window_label)
            continue

        doc = dict(metrics)
        # grain discriminator (the FB pattern). earlier
        # property docs are unstamped; the read path excludes grain=event rather
        # than requiring grain=property so those legacy docs stay readable.
        doc["grain"] = _GRAIN_PROPERTY
        doc["property_id"] = property_id
        doc["property_name"] = property_name
        doc["team_esid"] = team["team_esid"]
        doc["team_title"] = team["team_title"]
        doc["organization_title"] = team.get("organization_title", "Unknown")
        doc["team_country"] = _resolve_team_country(team)
        doc["insight_window_start"] = since
        doc["insight_window_end"] = until
        doc["insight_window_label"] = window_label
        doc["kibana_date"] = since
        doc["@timestamp"] = datetime.now(timezone.utc).isoformat()
        # Stable doc id so a re-pull overwrites the same ES doc.
        doc["_doc_id"] = _window_doc_id(property_id, team["team_esid"], since, until)

        _save_window_doc(doc, property_id, team["team_esid"], since, until)
        written += 1

        # event grain: a second, eventName-only pull for the same
        # window. Best-effort per property: a failure here must not lose the
        # property doc already written above.
        try:
            event_rows = _fetch_event_window(data_client, property_id, since, until)
        except Exception as e:  # noqa: BLE001 — per-property boundary; the property doc is already saved
            logging.warning("ga-window: event pull failed for property %s: %s — skipping events.", property_id, e)
            event_rows = []
        event_docs = build_event_window_docs(event_rows, property_id, property_name, team, since, until)
        _save_event_window_docs(event_docs, property_id, team["team_esid"], since, until)
        events_written += len(event_docs)

    logging.info(
        "ga-window: complete — wrote %d property window doc(s) + %d event-grain doc(s) for %s",
        written,
        events_written,
        window_label,
    )


if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser(description="Fetch GA4 metrics — one arbitrary window, property grain")
    parser.add_argument("--since", required=True, help="Window start (YYYY-MM-DD)")
    parser.add_argument("--until", required=True, help="Window end (YYYY-MM-DD)")
    parser.add_argument("--property_ids", default=None, help="Comma-separated GA4 property ids to scope to")
    parser.add_argument("--progress_run_id", default=None)
    args = parser.parse_args()
    main(vars(args))
