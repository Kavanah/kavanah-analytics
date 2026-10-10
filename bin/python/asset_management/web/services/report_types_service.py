"""
Built-in report types for the Report Scheduler.

The "New email schedule" dialog's "Dashboard to email" dropdown offers, under
its Reports group, two SYSTEM report types alongside the user's dashboards:

* ``report:backfill`` — the Data Inventory backfill report:
  per-team × per-data-view "# days of imports" + "last date imported", over
  ALL data views, over FULL HISTORY (see :func:`_build_backfill_report` — the
  schedule's date window is deliberately NOT applied to this report type).
* ``report:import_errors`` — a list of teams and the classified import errors
   recorded by each team's import schedules' most
  recent runs.

A report type is carried on the schedule's existing ``dashboard_id`` field as a
synthetic ``report:*`` id — no mapping change, and every downstream consumer
(snapshot persistence, email formats, CSV/xlsx export, the snapshot View page)
already renders the ``data.tables`` shape this module produces, so the
email pipeline works unchanged. ``_build_snapshot_doc`` branches to
:func:`build_report_data` when :func:`is_report_type` matches.

Headless by design: the builders run under the cron runner (no Flask session),
so scoping comes from the schedule itself — the caller passes the team titles
``ReportScheduleService._resolve_scope_titles`` resolved (explicit
``team_esids``, else the creator clamp). An empty title list computes
EMPTY (deny-all), never unscoped — the same contract as ``_precompute_charts``.
This is why the backfill builder does NOT reuse ``app._backfill_report``
verbatim: that route scopes by ``RoleFilterService.workspace_scoped_index_
pattern``, which reads the Flask session. The aggregation itself (terms on the
mixed keyword/text ``team_title`` mapping, ``by_day`` date_histogram + ``max``
date) is kept identical so the two reports can't drift in meaning.
"""

from __future__ import annotations

import importlib
import logging
from datetime import date, timedelta

from lib.fb_app_attribution import finding_detail, legacy_app_findings
from lib.import_error_classifier import SEVERITY_INFO, category_presentation

# ── Report-type registry ─────────────────────────────────────────────────────

REPORT_TYPE_PREFIX = "report:"
REPORT_TYPE_BACKFILL = "report:backfill"
REPORT_TYPE_IMPORT_ERRORS = "report:import_errors"
# Phase 2 — per-link Bitly click counts over the schedule's window
# (see _build_bitly_clicks_report; unlike backfill, the window IS applied).
REPORT_TYPE_BITLY_CLICKS = "report:bitly_clicks"

# id → dropdown/report title. The esm dialog mirrors these entries client-side
# (report_scheduler.html SYSTEM_REPORTS) — keep the two lists in sync.
REPORT_TYPES = {
    REPORT_TYPE_BACKFILL: "Backfill report",
    REPORT_TYPE_IMPORT_ERRORS: "Import errors",
    REPORT_TYPE_BITLY_CLICKS: "Bitly Clicks per Link",
}


def is_report_type(dashboard_id: str) -> bool:
    """True iff ``dashboard_id`` is a known synthetic ``report:*`` id."""
    return (dashboard_id or "") in REPORT_TYPES


# ── Data-view registry (backfill report) ─────────────────────────────────────
#
# Mirrors app._DATA_INVENTORY_VIEWS, which itself mirrors
# deployment/create-data-views.sh — a data-view rename must update all three.
# Not imported from app.py: the cron runner must not import the Flask app
# module. Alphabetical by label (the report renders columns in this order).
_DATA_VIEWS = [
    {"pattern": "*_chatwoot_conversations", "label": "Chatwoot Conversations"},
    {"pattern": "*_chatwoot_insights", "label": "Chatwoot Insights"},
    {"pattern": "*_disciple_tools", "label": "Disciple Tools (by team)"},
    {"pattern": "*_echo_conversations", "label": "Echo Conversations"},
    {"pattern": "*_fb_insights", "label": "Facebook Ad Insights"},
    {"pattern": "*_fb_page_insights", "label": "Facebook Page Insights"},
    {"pattern": "*_fb_page_posts", "label": "Facebook Page Posts"},
    {"pattern": "*_google_ads", "label": "Google Ads"},
    {"pattern": "*_ga_metrics", "label": "Google Analytics"},
    {"pattern": "*_ig_insights", "label": "Instagram Insights"},
    {"pattern": "*_youtube_analytics", "label": "YouTube Analytics"},
]

_INDEX_TEAMS = "de_asset_teams"
_INDEX_REPORT_SCHEDULES = "de_report_schedules"

# Row/label strings (one place, matching the email/export table shape).
_BACKFILL_TABLE_TITLE = "Data Inventory Backfill Report"
# Phase 2 — Bitly report tables. Indices are the Phase 1 importer's
# {team_esid}__bitly_clicks; the view pattern matches both naming
# schemes exactly like _DATA_VIEWS' patterns do.
_BITLY_PATTERN = "*_bitly_clicks"
_BITLY_LINKS_TABLE_TITLE = "Bitly clicks per link"
_BITLY_DAYS_TABLE_TITLE = "Bitly clicks per day"
_BITLY_LINKS_COLUMNS_PREFIX = ("Link", "Title", "Tags")
_BITLY_LINKS_COLUMNS_SUFFIX = ("Total clicks",)
# Per-day link columns up to a month of days; a wider window pivots to ISO-week
# columns so a long range can't produce an unreadable 90+-column table/sheet.
_BITLY_MAX_DAY_COLUMNS = 31
_BITLY_DAYS_COLUMNS = ("Date", "Clicks")
# Bounded per-link terms agg — the config-scoped Bitly account is small (tens
# of links); 200 keeps the table complete without an unbounded agg.
_BITLY_MAX_LINKS = 200
# Default trailing window when the schedule resolves no {from,to} (matches the
# ticket's "trailing 30 days" default; the importer's own MAX_DAYS is also 30).
_BITLY_DEFAULT_WINDOW_DAYS = 30
_ERRORS_TABLE_TITLE = "Import errors by team"
_NO_ERRORS_ROW_TEXT = "No import errors recorded"
_ERRORS_COLUMNS = ("Team", "Connector", "Error", "Detail", "Last run")
_SCHEDULE_LEVEL_TEAM_LABEL = "(Multiple teams)"

# Error categories with no per-team meaning (the run itself failed / was
# skipped / couldn't store) — attributing these to a team would be false
# precision, so they always land in the schedule-level block, even when the
# entry happens to carry a detail string.
_SCHEDULE_LEVEL_CATEGORIES = frozenset({"import_incomplete", "lock_contention", "storage_error"})

# Team-doc fields the attribution resolver walks: the asset-link pointers per
# connector family (mirrors data_connectors' summary walkers) + echo's inline
# tenant title. Fetched once per report run alongside team_title. ds_facebook
# gates the legacy-Meta-app sweep (lib.fb_app_attribution).
_TEAM_ASSET_SOURCE_FIELDS = [
    "team_title",
    "ds_facebook",
    "echo_tenant_title",
    "team_ga4_property_esids",
    "team_google_ads_customer_esids",
    "google_ads_customer_esid",
    "team_youtube_channel_handle_esids",
    "team_facebook_page_esids",
    "fb_page_esid",
    "fb_ad_account_links",
]

# asset index → (esid-collector team fields..., name/id fields on the asset doc
# that importers put in an error's ``detail``). Titles AND raw ids/handles are
# keyed — GA4 records the property name, YouTube the channel title / id /
# handle, google_ads the customer, Meta the page or ad account.
_ASSET_LOOKUPS = [
    ("de_asset_ga_properties", ("team_ga4_property_esids",), ("ga_property_title", "ga_property_id")),
    (
        "de_asset_google_ads_customers",
        ("team_google_ads_customer_esids", "google_ads_customer_esid"),
        ("google_ads_customer_title", "google_ads_customer_id"),
    ),
    (
        "de_asset_youtube_channels",
        ("team_youtube_channel_handle_esids",),
        ("youtube_channel_title", "youtube_channel_id", "youtube_channel_handle"),
    ),
    ("de_asset_fb_pages", ("team_facebook_page_esids", "fb_page_esid"), ("fb_page_title", "fb_page_slug")),
]


def _table_entry(chart_id: str, title: str, columns: list, rows: list) -> dict:
    """One snapshot ``tables[]`` entry. ``chart_type: "table"`` on BOTH the
    entry and the result mirrors the precompute shape — the snapshot View page
    dispatches its renderer on ``result.chart_type`` and classes the slot on
    the entry's, and the email/export adapters read ``result.columns/rows``."""
    return {
        "chart_id": chart_id,
        "chart_type": "table",
        "title": title,
        "result": {"chart_type": "table", "columns": columns, "rows": rows},
    }


def _inventory_date_field(pattern: str) -> str:
    """Per-view date field (mirrors app._inventory_date_field exactly): the DT
    contact shape carries first_contact_date, the team-keyed DT shape
    date_created_ms, every other view kibana_date."""
    dv = pattern.lower()
    if "disciple-tools" in dv:
        return "first_contact_date"
    if "disciple_tools" in dv:
        return "date_created_ms"
    return "kibana_date"


def _schedule_service():
    """Lazy three-root import of ReportScheduleService (container / app /
    pytest). Lazy because report_schedule_service imports THIS module for the
    snapshot branch — a top-level import back would be circular."""
    last_err: Exception | None = None
    for name in (
        "asset_management.web.services.report_schedule_service",
        "web.services.report_schedule_service",
        "services.report_schedule_service",
    ):
        try:
            return importlib.import_module(name)
        except ImportError as e:
            last_err = e
    raise last_err  # type: ignore[misc]


# ── Entry point ──────────────────────────────────────────────────────────────


def build_report_data(es, schedule: dict, scope_titles: list[str], date_range: dict) -> dict:
    """Build the snapshot ``{tables, summary_metrics}`` for one report-type run.

    ``scope_titles`` is the schedule's resolved team-title scope (explicit
    teams, else the creator clamp) — empty computes empty, never
    unscoped. ``date_range`` is the schedule's resolved {from,to} window; the
    import-errors report ignores it (it reports last-run STATE, not a window).
    Raises on nothing: any per-view/per-schedule failure degrades to missing
    rows (logged), matching ``_precompute_charts``' per-chart containment.
    """
    report_type = (schedule or {}).get("dashboard_id") or ""
    if report_type == REPORT_TYPE_BACKFILL:
        return _build_backfill_report(es, scope_titles, date_range)
    if report_type == REPORT_TYPE_IMPORT_ERRORS:
        return _build_import_errors_report(es, schedule, scope_titles)
    if report_type == REPORT_TYPE_BITLY_CLICKS:
        return _build_bitly_clicks_report(es, scope_titles, date_range)
    return {
        "tables": [],
        "summary_metrics": {},
        "precompute_skipped": f"unknown report type {report_type}",
    }


# ── Backfill report ──────────────────────────────────────────────────────────


def _team_title_clause(team_titles: list[str]) -> dict:
    """Scope filter over the mixed team_title mapping (keyword on some indices,
    text+keyword on others) — exact-terms on the keyword subfield OR a per-team
    match_phrase. Mirrors app._backfill_report's team clause."""
    should: list[dict] = [{"terms": {"team_title.keyword": sorted(team_titles)}}]
    for t in sorted(team_titles):
        should.append({"match_phrase": {"team_title": t}})
    return {"bool": {"should": should, "minimum_should_match": 1}}


def _scoped_indices(pattern: str, titles: list[str]) -> str:
    """Concrete, team-scoped index list for ``pattern`` — the headless
    equivalent of app._backfill_report's ``RoleFilterService.workspace_scoped_
    index_pattern``.

    Why not the bare glob: ``*_ga_metrics`` matches EVERY tenant's ga_metrics
    index, so a team title that exists in two workspaces would silently pull a
    foreign tenant's documents into this report's cells — the same class of
    cross-tenant leak closed on the email team picker. Scoping by the
    report's own resolved team titles keeps the query inside the schedule's
    scope.

    ``_build_allowed_indices`` is module-level and takes only team titles (no
    Flask session), so it is safe under the cron runner. It emits both naming
    schemes (``{esid}__{source}`` and legacy ``{slug}_{source}``);
    intersecting with the view's suffix keeps only this view's indices.
    Returns "" when nothing matches — the caller skips the view rather than
    falling back to an unscoped glob.
    """
    try:
        from asset_management.web.services.role_filter_service import _build_allowed_indices
    except ImportError:  # container / pytest root differences
        try:
            from web.services.role_filter_service import _build_allowed_indices
        except ImportError:
            from services.role_filter_service import _build_allowed_indices  # type: ignore[no-redef]

    suffix = pattern[1:] if pattern.startswith("*") else pattern
    allowed = _build_allowed_indices(list(titles))
    return ",".join(sorted(idx for idx in allowed if idx.endswith(suffix)))


def _build_backfill_report(es, scope_titles: list[str], date_range: dict) -> dict:
    """Per-team × per-view days-with-data + last-date-imported, over ALL data
    views (the scheduled report always includes every view), scoped
    to the schedule's teams by both index set and team title.

    FULL HISTORY by design — ``date_range`` is accepted (uniform builder
    signature) but deliberately NOT applied. A backfill report answers "how
    much history do we hold", so bounding it by the schedule's window is
    meaningless: email schedules default to rolling_7d, which made every cell
    read 0 / blank whenever imports hadn't run in the last week (the
    follow-up bug). app._backfill_report's UI default is likewise no bounds.

    Aggregation mirrors app._backfill_report: per view, a terms agg
    on team_title run against BOTH mappings (keyword + text; disjoint team
    sets, unioned, first write wins) with a min_doc_count:1 day histogram
    (bucket count == days with data) and a max on the view's date field.
    """
    titles = sorted({t for t in (scope_titles or []) if t})
    cells: dict[str, dict[str, dict]] = {}
    if titles:  # deny-by-default: no resolvable scope → an empty report
        for view in _DATA_VIEWS:
            pattern = view["pattern"]
            date_field = _inventory_date_field(pattern)
            scoped = _scoped_indices(pattern, titles)
            if not scoped:
                continue  # no indices for this view within the schedule's scope
            must: list[dict] = [
                {"bool": {"must_not": [{"term": {"no_data": True}}]}},
                _team_title_clause(titles),
            ]
            sub_aggs = {
                "by_day": {"date_histogram": {"field": date_field, "calendar_interval": "day", "min_doc_count": 1}},
                "last": {"max": {"field": date_field}},
            }
            for team_field in ("team_title.keyword", "team_title"):
                try:
                    resp = es.search(
                        index=scoped,
                        body={
                            "size": 0,
                            "query": {"bool": {"filter": must}},
                            "aggs": {"by_team": {"terms": {"field": team_field, "size": 1000}, "aggs": sub_aggs}},
                        },
                        ignore_unavailable=True,
                        allow_no_indices=True,
                    )
                except Exception as e:  # noqa: BLE001 — a pure-text mapping 400s; the other field covers it
                    logging.debug("report_types_service backfill %s/%s: %s", pattern, team_field, e)
                    continue
                for b in resp.get("aggregations", {}).get("by_team", {}).get("buckets", []):
                    team = b["key"]
                    view_cell = cells.setdefault(team, {})
                    if pattern in view_cell:
                        continue  # already counted from the other mapping
                    last_raw = b.get("last", {}).get("value_as_string")
                    view_cell[pattern] = {
                        "days": len(b.get("by_day", {}).get("buckets", [])),
                        "last_date": last_raw[:10] if last_raw else None,
                    }

    # Every scoped team gets a row, data or not (an explicitly-selected team
    # with nothing imported is exactly what a backfill report must surface).
    headers = ["Team"]
    for v in _DATA_VIEWS:
        headers.append(f"{v['label']} — # days of imports")
        headers.append(f"{v['label']} — Last date imported")
    rows = []
    for team in titles:
        row: list = [team]
        team_cells = cells.get(team, {})
        for v in _DATA_VIEWS:
            cell = team_cells.get(v["pattern"]) or {}
            row.append(cell.get("days", 0))
            row.append(cell.get("last_date") or "")
        rows.append(row)

    return {
        "tables": [_table_entry(REPORT_TYPE_BACKFILL, _BACKFILL_TABLE_TITLE, headers, rows)],
        "summary_metrics": {
            "Teams": len(titles),
            "Teams with data": len([t for t in titles if cells.get(t)]),
        },
    }


# ── Bitly clicks report ───────────────────────────────────


def _bitly_window(date_range: dict) -> tuple[str, str]:
    """The report's applied {from,to} window as yyyy-MM-dd strings. UNLIKE the
    backfill report, the schedule's resolved window IS applied here (a click
    report answers "what happened in this period"); a schedule that resolves no
    window falls back to the trailing _BITLY_DEFAULT_WINDOW_DAYS days."""
    frm = ((date_range or {}).get("from") or "").strip()
    to = ((date_range or {}).get("to") or "").strip()
    if frm and to:
        return frm, to
    today = date.today()
    return (today - timedelta(days=_BITLY_DEFAULT_WINDOW_DAYS - 1)).isoformat(), today.isoformat()


def _bitly_period_key(day_iso: str, weekly: bool) -> str:
    """The pivot-column key a yyyy-MM-dd day belongs to: the day itself, or
    (weekly windows) the Monday of its ISO week, prefixed for readability."""
    try:
        d = date.fromisoformat(day_iso)
    except (TypeError, ValueError):
        return ""
    if not weekly:
        return d.isoformat()
    return f"wk {(d - timedelta(days=d.weekday())).isoformat()}"


def _bitly_period_keys(frm: str, to: str) -> tuple[list[str], bool]:
    """Every pivot column for the window, in order, plus the weekly flag.

    Day columns for windows up to ``_BITLY_MAX_DAY_COLUMNS`` days; wider
    windows pivot to ISO-week columns (labeled ``wk <monday>``) so a long
    range cannot produce an unreadable table. Every period in the range gets
    a column even with no data — a 7-day selection always shows 7 columns.
    An unparseable window degrades to no pivot columns (totals still render).
    """
    try:
        d0, d1 = date.fromisoformat(frm), date.fromisoformat(to)
    except (TypeError, ValueError):
        return [], False
    if d1 < d0:
        return [], False
    weekly = (d1 - d0).days + 1 > _BITLY_MAX_DAY_COLUMNS
    keys: list[str] = []
    d = d0
    while d <= d1:
        key = _bitly_period_key(d.isoformat(), weekly)
        if key not in keys:
            keys.append(key)
        d += timedelta(days=1)
    return keys, weekly


def _build_bitly_clicks_report(es, scope_titles: list[str], date_range: dict) -> dict:
    """Per-link Bitly click totals + a per-day click series over the schedule's
    resolved window, scoped to the schedule's teams by both index set
    (:func:`_scoped_indices` — never the bare glob) and ``team_title``
    (a plain keyword in the bitly_clicks schema, so a single terms filter
    suffices — no dual-mapping pass like the backfill report needs).

    One aggregation query, two tables:

    * "Bitly clicks per link" — one row per ``link_id`` (terms, size
      ``_BITLY_MAX_LINKS``, ordered by total clicks desc): link id, latest
      ``link_title``, tags, then ONE COLUMN PER PERIOD in the window (day
      columns up to ``_BITLY_MAX_DAY_COLUMNS`` days, ISO-week columns beyond),
      then the window's summed clicks. A 7-day selection shows 7 day columns
      even where a period has no data.
    * "Bitly clicks per day"  — one row per day with data (date_histogram on
      ``date``, min_doc_count 1): date, summed clicks. Zero-click days that the
      importer wrote still count as data days (clicks sum 0).

    Deny-by-default: an empty scope computes EMPTY tables and issues no ES
    query — the same contract as the other builders. Any ES failure degrades
    to empty tables (logged), matching ``_precompute_charts``' containment.
    """
    titles = sorted({t for t in (scope_titles or []) if t})
    frm, to = _bitly_window(date_range)
    period_keys, weekly = _bitly_period_keys(frm, to)
    link_rows: list[list] = []
    day_rows: list[list] = []
    total_clicks = 0

    scoped = _scoped_indices(_BITLY_PATTERN, titles) if titles else ""
    if scoped:
        body = {
            "size": 0,
            "query": {
                "bool": {
                    "filter": [
                        {"term": {"record_type": "link_clicks"}},
                        {"terms": {"team_title": titles}},
                        {"range": {"date": {"gte": frm, "lte": to}}},
                    ]
                }
            },
            "aggs": {
                "total_clicks": {"sum": {"field": "clicks"}},
                "by_link": {
                    "terms": {"field": "link_id", "size": _BITLY_MAX_LINKS, "order": {"total_clicks": "desc"}},
                    "aggs": {
                        "total_clicks": {"sum": {"field": "clicks"}},
                        # Latest doc carries the link's current title/tags (Bitly
                        # metadata can be edited; the newest snapshot wins).
                        "latest": {
                            "top_hits": {
                                "size": 1,
                                "sort": [{"date": {"order": "desc"}}],
                                "_source": ["link_title", "tags"],
                            }
                        },
                        # Per-link daily series — pivoted into one column per
                        # period (day, or ISO week for wide windows) so the
                        # email/xlsx table reads across the selected range.
                        "by_day": {
                            "date_histogram": {"field": "date", "calendar_interval": "day", "min_doc_count": 0},
                            "aggs": {"clicks": {"sum": {"field": "clicks"}}},
                        },
                    },
                },
                "by_day": {
                    "date_histogram": {"field": "date", "calendar_interval": "day", "min_doc_count": 1},
                    "aggs": {"clicks": {"sum": {"field": "clicks"}}},
                },
            },
        }
        try:
            resp = es.search(index=scoped, body=body, ignore_unavailable=True, allow_no_indices=True)
        except Exception as e:  # noqa: BLE001 — degrade to empty tables, never a failed run
            logging.error("report_types_service._build_bitly_clicks_report: %s", e)
            resp = {}
        aggs = (resp or {}).get("aggregations") or {}
        total_clicks = int((aggs.get("total_clicks") or {}).get("value") or 0)
        for b in (aggs.get("by_link") or {}).get("buckets", []):
            hits = ((b.get("latest") or {}).get("hits") or {}).get("hits") or []
            src = (hits[0].get("_source") or {}) if hits else {}
            tags = src.get("tags")
            tags_cell = ", ".join(tags) if isinstance(tags, list) else (tags or "")
            # Pivot the link's daily series onto the window's period columns.
            per_period: dict[str, int] = {}
            for d in (b.get("by_day") or {}).get("buckets", []):
                day_key = (d.get("key_as_string") or "")[:10]
                pkey = _bitly_period_key(day_key, weekly)
                if pkey:
                    per_period[pkey] = per_period.get(pkey, 0) + int((d.get("clicks") or {}).get("value") or 0)
            link_rows.append(
                [
                    b.get("key", ""),
                    src.get("link_title") or "",
                    tags_cell,
                    *[per_period.get(p, 0) for p in period_keys],
                    int((b.get("total_clicks") or {}).get("value") or 0),
                ]
            )
        for b in (aggs.get("by_day") or {}).get("buckets", []):
            day_key = b.get("key_as_string") or ""
            day_rows.append([day_key[:10], int((b.get("clicks") or {}).get("value") or 0)])

    return {
        "tables": [
            _table_entry(
                REPORT_TYPE_BITLY_CLICKS,
                _BITLY_LINKS_TABLE_TITLE,
                [*_BITLY_LINKS_COLUMNS_PREFIX, *period_keys, *_BITLY_LINKS_COLUMNS_SUFFIX],
                link_rows,
            ),
            _table_entry(f"{REPORT_TYPE_BITLY_CLICKS}:by_day", _BITLY_DAYS_TABLE_TITLE, list(_BITLY_DAYS_COLUMNS), day_rows),
        ],
        "summary_metrics": {
            "Total clicks": total_clicks,
            "Links": len(link_rows),
            "Date range": f"{frm} to {to}",
        },
    }


# ── Import-errors report ─────────────────────────────────────────────────────


def _resolve_report_teams(es, schedule: dict, scope_titles: list[str]) -> list[dict]:
    """The report's team rows as ``[{esid, title}]``, sorted by title.

    Explicit ``team_esids`` resolve by id; otherwise the creator-clamp
    titles are mapped back to team docs (the errors join below needs esids —
    import schedules scope by esid, not title). Empty on no resolvable scope.
    """
    svc = _schedule_service()
    team_esids = svc.ReportScheduleService._schedule_team_esids(schedule)
    try:
        if team_esids:
            res = es.search(
                index=_INDEX_TEAMS,
                body={
                    "query": {"ids": {"values": team_esids}},
                    "_source": _TEAM_ASSET_SOURCE_FIELDS,
                    "size": len(team_esids),
                },
            )
        else:
            titles = {t for t in (scope_titles or []) if t}
            if not titles:
                return []
            res = es.search(
                index=_INDEX_TEAMS,
                body={"query": {"match_all": {}}, "_source": _TEAM_ASSET_SOURCE_FIELDS, "size": 10000},
            )
    except Exception as e:  # noqa: BLE001 — no team rows beats a failed run
        logging.error("report_types_service._resolve_report_teams: %s", e)
        return []
    teams = []
    for h in res.get("hits", {}).get("hits", []):
        src = h.get("_source") or {}
        title = (src.get("team_title") or "").strip()
        if not title:
            continue
        if not team_esids and title not in scope_titles:
            continue  # clamp path: keep only the creator's authorized titles
        teams.append({"esid": h["_id"], "title": title, "doc": src})
    teams.sort(key=lambda t: t["title"].lower())
    return teams


def _norm_key(value) -> str:
    """Normalize a detail/asset string for matching: trimmed + casefolded."""
    return str(value or "").strip().casefold()


def _strip_id_suffix(detail: str) -> str:
    """Drop a trailing parenthetical — several importers format detail as
    "Title (12345)"; the map keys titles and ids separately."""
    d = detail.strip()
    if d.endswith(")") and "(" in d:
        return d[: d.rfind("(")].strip()
    return d


def _team_asset_keys(es, teams: list[dict]) -> dict[str, set[str]]:
    """Map every asset name/id owned by a report team → the owning team
    title(s), for the attribution pass (rule 2).

    Keys are the strings importers put in an error's ``detail`` — asset titles,
    raw platform ids, YouTube handles, echo tenant titles — normalized via
    ``_norm_key``. A key owned by two teams maps to both (genuine shared
    ownership → the error rows on each). One bounded ES read per asset index
    (ids query over the union of the teams' pointers). Fail-soft per index: a
    lookup error just loses that family's attributions (those errors fall to
    the schedule-level block, never to a wrong team).
    """
    keys: dict[str, set[str]] = {}

    def _add(value, title: str) -> None:
        k = _norm_key(value)
        if k:
            keys.setdefault(k, set()).add(title)

    # esid → owning team titles, per asset index, from the team docs' pointers.
    wanted: dict[str, dict[str, set[str]]] = {index: {} for index, _f, _n in _ASSET_LOOKUPS}
    for team in teams:
        doc = team.get("doc") or {}
        title = team["title"]
        _add(doc.get("echo_tenant_title"), title)
        for link in doc.get("fb_ad_account_links") or []:
            if isinstance(link, dict):
                _add(link.get("fb_ad_account_id"), title)
        for index, esid_fields, _name_fields in _ASSET_LOOKUPS:
            for field in esid_fields:
                raw = doc.get(field)
                esids = raw if isinstance(raw, list) else ([raw] if raw else [])
                for esid in [str(e).strip() for e in esids if str(e).strip()]:
                    wanted[index].setdefault(esid, set()).add(title)

    for index, _esid_fields, name_fields in _ASSET_LOOKUPS:
        esid_owners = wanted[index]
        if not esid_owners:
            continue
        try:
            res = es.search(
                index=index,
                body={
                    "query": {"ids": {"values": list(esid_owners)}},
                    "_source": list(name_fields),
                    "size": len(esid_owners),
                },
                ignore_unavailable=True,
                allow_no_indices=True,
            )
        except Exception as e:  # noqa: BLE001 — lose this family's attributions, never the report
            logging.warning("report_types_service._team_asset_keys %s: %s", index, e)
            continue
        for h in res.get("hits", {}).get("hits", []):
            src = h.get("_source") or {}
            for owner_title in esid_owners.get(h["_id"], ()):
                for field in name_fields:
                    _add(src.get(field), owner_title)
    return keys


def _build_import_errors_report(es, schedule: dict, scope_titles: list[str]) -> dict:
    """Teams and the classified import errors their import schedules' most
    recent runs recorded (``last_run_state.import_errors``, lazily
    back-filled from ``de_import_progress`` via ``backfill_import_outcome``).

    Attribution (follow-up — an error is NEVER repeated per team):
    an import schedule's errors are per RUN, not per team, so each entry is
    attributed in this order:

      1. ``detail`` equals a report team's title → that team's row.
      2. ``detail`` names an asset a report team owns (GA4 property, YouTube
         channel title/id/handle, Google Ads customer, FB page/ad account,
         echo tenant — via :func:`_team_asset_keys`) → the owning team(s).
      3. The schedule scopes exactly ONE report team → that team (no ambiguity).
      4. Otherwise ONE row in a trailing schedule-level block ("(Multiple
         teams)" + "affects N teams: …"), instead of the pre-fix behavior of
         repeating the row under every scoped team.

    Categories with no per-team meaning (``_SCHEDULE_LEVEL_CATEGORIES``) go
    straight to rule 4. ``info``-severity entries (no-data / import-step-
    skipped) are omitted — they aren't errors. Teams with nothing recorded get
    an explicit "no errors" row so the report is a full team list.

    legacy-Meta-app teams additionally get a SYNTHETIC warning row
    (category ``legacy_meta_app``, computed from asset state via
    ``lib.fb_app_attribution``, ONE bounded sweep per build). Synthetic because
    the failure mode is silent attrition: the importer skips permission-denied
    assets and completes "successfully", so an affected team may record no
    ``import_errors[]`` entry at all — and for the same reason such a team must
    NOT collapse to the "no errors" row, which would be actively false. The
    row is injected AFTER the ``info`` drop above (warning severity survives
    by construction) and its ``Last run`` cell is legitimately blank — this is
    config state, not a run outcome.
    """
    svc = _schedule_service()
    teams = _resolve_report_teams(es, schedule, scope_titles)
    if not teams:
        return {
            "tables": [_table_entry(REPORT_TYPE_IMPORT_ERRORS, _ERRORS_TABLE_TITLE, list(_ERRORS_COLUMNS), [])],
            "summary_metrics": {"Teams checked": 0, "Teams with import errors": 0, "Teams on legacy Meta app": 0},
        }
    team_esid_set = {t["esid"] for t in teams}
    title_by_esid = {t["esid"]: t["title"] for t in teams}
    title_by_norm = {_norm_key(t["title"]): t["title"] for t in teams}
    asset_keys = _team_asset_keys(es, teams)

    try:
        res = es.search(
            index=_INDEX_REPORT_SCHEDULES,
            body={"query": {"match_all": {}}, "size": 500},
            ignore_unavailable=True,
            allow_no_indices=True,
        )
        hits = res.get("hits", {}).get("hits", [])
    except Exception as e:  # noqa: BLE001 — degrade to "no errors recorded"
        logging.error("report_types_service._build_import_errors_report schedules: %s", e)
        hits = []

    def _attribute(detail: str, category: str, sched_titles: list[str], entry_team_esid: str = "") -> list[str]:
        """The team title(s) an entry lands on, or [] → schedule-level row."""
        if category in _SCHEDULE_LEVEL_CATEGORIES:
            return []
        # rule 0: the importer stamped the team at record time
        # (lib.import_run_context.set_current_team). Durable and exact, so it
        # beats every title/asset heuristic; heuristics remain for historical
        # entries without the stamp. A stamp outside the report's team set
        # falls through (same as an unmatched detail).
        if entry_team_esid and entry_team_esid in title_by_esid:
            return [title_by_esid[entry_team_esid]]
        for candidate in (detail, _strip_id_suffix(detail)):
            k = _norm_key(candidate)
            if not k:
                continue
            if k in title_by_norm:  # rule 1 — detail IS a report team title
                return [title_by_norm[k]]
            owners = asset_keys.get(k)
            if owners:  # rule 2 — detail names an asset a report team owns
                return sorted(owners)
        if len(sched_titles) == 1:  # rule 3 — one scoped team, no ambiguity
            return list(sched_titles)
        return []

    # team title → [[Connector, Error, Detail, Last run]]
    errors_by_team: dict[str, list[list]] = {}
    schedule_level_rows: list[list] = []
    for hit in hits:
        src = hit.get("_source") or {}
        src["schedule_id"] = hit["_id"]
        if (src.get("schedule_type") or svc.SCHEDULE_TYPE_IMPORT) == svc.SCHEDULE_TYPE_EMAIL:
            continue  # email schedules import nothing
        connector_id = (src.get("connector_id") or "").strip()
        if not connector_id:
            continue
        sched_teams = [t for t in svc.ReportScheduleService._schedule_team_esids(src) if t in team_esid_set]
        if not sched_teams:
            continue
        sched_titles = sorted({title_by_esid[t] for t in sched_teams}, key=str.lower)
        # Reconcile the run's fire-and-forget import outcome (idempotent, best-effort).
        src = svc.ReportScheduleService.backfill_import_outcome(es, src)
        run_state = src.get("last_run_state") or {}
        last_run = (run_state.get("finished_at") or run_state.get("started_at") or "")[:19].replace("T", " ")
        for entry in run_state.get("import_errors") or []:
            if not isinstance(entry, dict) or (entry.get("severity") or "") == SEVERITY_INFO:
                continue
            detail = (entry.get("detail") or "").strip()
            err_title = entry.get("title") or "Import error"
            message = entry.get("message") or ""
            targets = _attribute(
                detail, entry.get("category") or "", sched_titles, entry_team_esid=(entry.get("team_esid") or "")
            )
            if targets:
                for title in targets:
                    errors_by_team.setdefault(title, []).append([connector_id, err_title, message, last_run])
                continue
            # Rule 4 — ONE schedule-level row, never repeated per team. Name
            # the in-scope teams the run covered (first 3 + a count).
            shown = ", ".join(sched_titles[:3]) + (f", +{len(sched_titles) - 3} more" if len(sched_titles) > 3 else "")
            n = len(sched_titles)
            affects = f"{message} — affects {n} team{'' if n == 1 else 's'}: {shown}".lstrip(" —")
            schedule_level_rows.append([_SCHEDULE_LEVEL_TEAM_LABEL, connector_id, err_title, affects, last_run])

    # legacy-Meta-app sweep, ONCE per report build (bounded ES
    # reads; never per row — the schedules scan above already walks up to 500
    # docs). Keyed on which app the team is on, not on observed scope gaps.
    legacy_findings = legacy_app_findings(es, {t["esid"]: t.get("doc") or {} for t in teams})
    legacy_title = category_presentation("legacy_meta_app")["title"]

    rows: list[list] = []
    for team in teams:
        team_rows = [[team["title"], *tail] for tail in errors_by_team.get(team["title"]) or []]
        finding = legacy_findings.get(team["esid"])
        if finding:
            # Connector ds_facebook (the team flag that gates the sweep);
            # Last run blank — a config state has no run timestamp.
            team_rows.append([team["title"], "ds_facebook", legacy_title, finding_detail(finding), ""])
        if not team_rows:
            team_rows = [[team["title"], "", "", _NO_ERRORS_ROW_TEXT, ""]]
        rows.extend(team_rows)
    rows.extend(schedule_level_rows)

    return {
        "tables": [_table_entry(REPORT_TYPE_IMPORT_ERRORS, _ERRORS_TABLE_TITLE, list(_ERRORS_COLUMNS), rows)],
        "summary_metrics": {
            "Teams checked": len(teams),
            "Teams with import errors": len(errors_by_team),
            # migration progress as one readable number over time.
            "Teams on legacy Meta app": len(legacy_findings),
            "Schedule-level errors": len(schedule_level_rows),
        },
    }
