"""
Report Schedule Service (Report scheduler, P1 backend foundation)

Backs two new ES indices for the "scheduled email / PDF report delivery"
feature:

* ``de_report_schedules`` — one row per named, recurring report schedule.
  Carries the data-source query envelope, the schedule cadence (resolved to a
  ``next_run`` timestamp the poll-runner queries on), the delivery preferences,
  and org/team scope. Doc id is ES-generated; ``schedule_id`` mirrors the _id
  in the body for callers that don't carry the _id separately.
* ``de_report_snapshots`` — one row per executed run. P1 stores a *stub*
  payload (the resolved query envelope + the resolved date window + an empty
  ``summary_metrics`` placeholder). The real chart/table precompute lands in P2
  once the dashboard-state hydration path is wired (it reads the renderer's
  data contract there rather than guessing it here).

Execution model is Option A (user decision 2026-06-09): a host-cron poll-runner
(``asset_management/jobs/run_report_schedules.py``) calls ``run_due`` every 5
minutes. Schedule state lives in ES, so the AC "schedules survive container
restarts" holds for free — there is no in-memory scheduler to reload.

Org/team visibility scoping reuses ``role_filter_service.resolve_user_scope``:
``create_schedule`` rejects an org/team the caller can't see, and
``list_schedules`` returns only schedules whose scope intersects the caller's
``UserScope``.

Cron parsing is hand-rolled (user decision 2026-06-09): the creation form only
emits a handful of cadence shapes (daily / weekly+DOW / biweekly / monthly /
quarterly), so
``compute_next_run`` works off structured frequency fields rather than parsing a
general cron string. The ``schedule.cron`` field is still stored for display +
future generality.
"""

from __future__ import annotations

import json
import logging
import os
import signal
import subprocess
import time
import urllib.parse
from datetime import date, datetime, timedelta, timezone
from zoneinfo import ZoneInfo

from lib.es_helpers import (
    get_elasticsearch_client,
    put_regular_index_template,
    create_index_if_not_exists,
    scoped_index_pattern as _scoped_index_pattern,
    team_data_index_names as _team_indices,
)
from lib.import_error_classifier import SEVERITY_ERROR, SEVERITY_INFO
from lib.errors import UserFacingError

# ─────────────────────────────────────────────────────────────────────────────
# Index names
# ─────────────────────────────────────────────────────────────────────────────

INDEX_REPORT_SCHEDULES = "de_report_schedules"
INDEX_REPORT_SNAPSHOTS = "de_report_snapshots"

# ─────────────────────────────────────────────────────────────────────────────
# Status enum
# ─────────────────────────────────────────────────────────────────────────────

STATUS_ACTIVE = "active"
STATUS_PAUSED = "paused"
_VALID_STATUSES = (STATUS_ACTIVE, STATUS_PAUSED)

# ─────────────────────────────────────────────────────────────────────────────
# Schedule type — a schedule is either an IMPORT schedule (pulls
# connector data on a cadence + snapshots it; the original behavior) or an EMAIL
# schedule (emails an existing dashboard report on a cadence; pulls no data, no
# snapshot). Default "import" so every earlier row keeps its behavior with
# no migration. run_due branches on this: email rows skip the import dispatch +
# snapshot build and send via report_email_service instead.
# ─────────────────────────────────────────────────────────────────────────────
SCHEDULE_TYPE_IMPORT = "import"
SCHEDULE_TYPE_EMAIL = "email"
_VALID_SCHEDULE_TYPES = (SCHEDULE_TYPE_IMPORT, SCHEDULE_TYPE_EMAIL)

# ─────────────────────────────────────────────────────────────────────────────
# Run-state phases — the lifecycle a run moves through, persisted on
# the schedule's ``last_run_state`` so a progress bar survives navigation/reload.
#   IMPORTING   — a fresh connector data-import was dispatched (fire-and-forget);
#                 the snapshot build waits on nothing, but the import runs on.
#   GENERATING  — building the snapshot (precompute + email) synchronously.
#   DONE        — snapshot built + scheduling advanced (the connector import may
#                 still be finishing; the client polls the lock for that tail).
#   FAILED      — the synchronous build/advance errored (error carries the reason).
# ─────────────────────────────────────────────────────────────────────────────
RUN_PHASE_IMPORTING = "importing"
RUN_PHASE_GENERATING = "generating"
RUN_PHASE_DONE = "done"
RUN_PHASE_FAILED = "failed"
# the user cancelled an in-flight run. Terminal, like done/failed: the
# card shows a clear "Cancelled" state and the import tail stops (the persisted
# cancel flag + a process-group kill reap the connector import subprocess).
RUN_PHASE_CANCELLED = "cancelled"

# the snapshot must be built only AFTER its dispatched import lands data
# in ES (a snapshot is frozen at build time; building before the import finishes
# captured empty/stale data permanently). run_due (headless cron) WAITS inline up to
# WAIT_TIMEOUT_SECONDS, polling the import's de_import_progress doc every
# WAIT_POLL_INTERVAL_SECONDS. run_now (in a Flask request, must not block) DEFERS the
# build: it returns immediately leaving phase=IMPORTING, and a later run_due poll
# (_sweep_deferred_builds) builds the snapshot once the import is terminal. A run that
# never reaches terminal is built on current data once it ages past
# DEFERRED_BUILD_MAX_AGE_SECONDS (just past the 30-min reaper) so a card never
# sticks forever.
WAIT_TIMEOUT_SECONDS = 900  # 15 min — a 90d FB pull is <10 min; reaper is 30 min
WAIT_POLL_INTERVAL_SECONDS = 10
DEFERRED_BUILD_MAX_AGE_SECONDS = 2100  # 35 min — past the 30-min reaper's first crack

# precompute-skip reasons that are CONFIGURATION states, not failures:
#   * no dashboard attached (import-only schedule), and
#   * the attached dashboard has no charts yet.
# Both mean "the run did its job but there was nothing to render" — the History
# modal shows them as a grey info note, not a red error badge. Genuine problems
# (ES unavailable, a per-chart query/transform failure) still badge red. Kept as
# named constants so _build_snapshot_doc and _is_benign_skip can't drift on the
# wording. The "no charts" reason embeds the dashboard id, so it's matched by its
# stable suffix rather than an exact string.
SKIP_NO_DASHBOARD = "no dashboard_id on schedule"
SKIP_NO_CHARTS_SUFFIX = "has no charts"


def _is_benign_skip(reason: str) -> bool:
    """True iff this precompute-skip reason is a benign configuration
    state (no dashboard / empty dashboard), which the History modal surfaces as an
    info note instead of a red error."""
    r = (reason or "").strip()
    return r == SKIP_NO_DASHBOARD or r.endswith(SKIP_NO_CHARTS_SUFFIX)


def _normalize_data_view_for_search(data_view: str) -> str:
    """Promote a bare per-source slug (e.g. ``chatwoot_insights``) to the
    wildcard index pattern that actually matches the per-team data indices
    (``*_chatwoot_insights``), mirroring the Kibana data views in
    deployment/create-data-views.sh and the live request path.

     writes every source's data to per-team indices named
    ``{team_esid}__{slug}`` — there is NO index or alias named by the bare slug.
    The live dashboard path passes the data_view through
    ``RoleFilterService.scoped_index_pattern`` (which only expands ``*``-prefixed
    patterns), so a chart that correctly stores ``*_chatwoot_insights`` resolves
    to the team indices. The headless snapshot runner searched ``index=data_view``
    verbatim, so a chart that stored the BARE slug (the hand-seeded chatwoot
    dashboard did) matched zero indices and produced an all-zero snapshot for
    every team. Normalizing here is defense-in-depth: even a bare-slug chart now
    reads the real data. A pattern that already carries a wildcard, an explicit
    ``{esid}__slug`` index, or a comma/exclusion list passes through unchanged.

    Team isolation is unaffected — ``_precompute_charts`` appends the same
    allow-only ``team_title`` filter the request path uses, so widening the index
    to the wildcard still restricts rows to the schedule's teams.
    """
    from lib.es_helpers import KNOWN_SOURCES

    dv = (data_view or "").strip()
    if not dv:
        return dv
    # Only touch a plain, single, wildcard-free token that IS a known source
    # slug. Anything with a wildcard, separator, comma, or exclusion is already
    # an explicit pattern the caller meant — leave it.
    if any(ch in dv for ch in ("*", ",", "-")) or "__" in dv:
        return dv
    if dv in KNOWN_SOURCES:
        return f"*_{dv}"
    return dv


def _relabel_table_columns(result: dict, config: dict) -> None:
    """rewrite a table result's raw ES field-name headers to the
    dashboard's human column labels, in place.

    ``echarts_transformer.transform`` returns a table's ``columns`` as raw field
    names (e.g. ``estimated_minutes_watched``); the live dashboard makes them
    friendly via a client-side label map in charts.js (``renderTable``) built
    from ``table_columns[i].label`` and ``x_label``/``x_label_2``. A snapshot
    stores the raw transform output and the snapshot viewer/export never had
    that map, so View/CSV/Excel showed raw field names. We apply the same map
    server-side at snapshot-build time (and only here — the live render is
    untouched, so its sort/format maps that key off the raw column values are
    unaffected).

    Positional and non-destructive: columns are relabelled by index only, never
    reordered or dropped, so the rows (which align to column positions) stay
    valid. Mirrors charts.js ``renderTable`` exactly:
      * leading label columns (x_field, optional x_field_2) → x_label/x_label_2;
      * value column i → table_columns[i].label, falling back to y_labels[i] for
        converted charts;
      * any column without a label keeps its raw field name.
    """
    if not isinstance(result, dict) or result.get("chart_type") != "table":
        return
    columns = result.get("columns")
    if not isinstance(columns, list) or not columns:
        return

    # numLabelCols: x_field present → 1, plus x_field_2 → 2; else 0. Matches
    # charts.js _numLabelCols / the transform's label_cols ordering.
    x_field = config.get("x_field")
    num_label_cols = (2 if config.get("x_field_2") else 1) if x_field else 0

    new_columns = list(columns)
    x_label = config.get("x_label")
    if x_label and len(new_columns) >= 1:
        new_columns[0] = x_label
    x_label_2 = config.get("x_label_2")
    if x_label_2 and len(new_columns) >= 2:
        new_columns[1] = x_label_2

    table_columns = config.get("table_columns")
    if isinstance(table_columns, list) and table_columns:
        for i, col in enumerate(table_columns):
            idx = i + num_label_cols
            label = col.get("label") if isinstance(col, dict) else None
            if label and 0 <= idx < len(new_columns):
                new_columns[idx] = label
    else:
        y_labels = config.get("y_labels")
        if isinstance(y_labels, list):
            for i, label in enumerate(y_labels):
                idx = i + num_label_cols
                if label and 0 <= idx < len(new_columns):
                    new_columns[idx] = label

    result["columns"] = new_columns


def _append_totals_row(result: dict, config: dict) -> None:
    """append a grand-totals row to a `table` result's rows, in place.

    Mirrors charts.js ``renderTable`` (the live builder) so a saved snapshot shows
    the same totals row the builder does. Gated on ``config.include_totals_row``;
    a no-op when the box is off, so a table renders exactly as before.

    The snapshot stores **raw numeric** row values (formatting is applied at
    view/CSV/Excel render time), so the appended totals are raw numbers too — the
    same per-column format then applies to them downstream, matching the rest of
    the column. The first label cell reads ``Totals``; a second label cell
    (x_field_2) is blank. Each numeric, not-opted-out column is summed or averaged
    per ``totals_aggregation``; non-numeric and opted-out columns get a blank cell.

    Positional and consistent with ``_relabel_table_columns``: value column i maps
    to ``table_columns[i]`` at output index ``i + num_label_cols``.
    """
    if not isinstance(result, dict) or result.get("chart_type") != "table":
        return
    # the transform attaches server-computed totals (aligned with
    # columns, None = blank) when the totals box is on: ratio columns as
    # ratio-of-sums over the whole scope, non-additive columns blank. Pop it
    # unconditionally so the stored snapshot never carries the raw key; bake
    # it below in preference to the legacy row-sum, which mis-totaled ratio
    # and deduplicated columns.
    server_totals = result.pop("totals", None)
    if not isinstance(server_totals, list):
        server_totals = None
    if config.get("include_totals_row") is not True:
        return
    columns = result.get("columns")
    rows = result.get("rows")
    if not isinstance(columns, list) or not columns:
        return
    if not isinstance(rows, list) or not rows:
        # No data rows → no totals row (matches the live builder's
        # displayRows.length > 0 guard).
        return

    x_field = config.get("x_field")
    num_label_cols = (2 if config.get("x_field_2") else 1) if x_field else 0

    table_columns = config.get("table_columns")
    table_columns = table_columns if isinstance(table_columns, list) else []

    agg = "avg" if config.get("totals_aggregation") == "avg" else "sum"

    totals_row: list = []
    for col_idx in range(len(columns)):
        # Leading label columns: "Totals" in the first, blank in the rest.
        if col_idx < num_label_cols:
            totals_row.append("Totals" if col_idx == 0 else "")
            continue
        tcol_idx = col_idx - num_label_cols
        tcol = table_columns[tcol_idx] if 0 <= tcol_idx < len(table_columns) else None
        # Per-column opt-out (default-on). bool is an int subclass; numeric cells
        # are real ints/floats, so guard bool out explicitly.
        included = not isinstance(tcol, dict) or tcol.get("include_in_totals") is not False
        # bake the server total when the payload carries one; a
        # non-numeric slot (None: non-additive/text column) bakes as blank.
        if server_totals is not None:
            v = server_totals[col_idx] if col_idx < len(server_totals) else None
            if included and isinstance(v, (int, float)) and not isinstance(v, bool):
                totals_row.append(v)
            else:
                totals_row.append("")
            continue
        total = 0.0
        count = 0
        if included:
            for row in rows:
                if not isinstance(row, list) or col_idx >= len(row):
                    continue
                v = row[col_idx]
                if isinstance(v, bool):
                    continue
                if isinstance(v, (int, float)):
                    total += v
                    count += 1
        # count == 0 → non-numeric (or excluded) column → blank cell.
        if count == 0:
            totals_row.append("")
        else:
            totals_row.append(total / count if agg == "avg" else total)

    # The snapshot viewer reuses charts.js renderTable with a MINIMAL config
    # (no include_totals_row flag), so it renders this baked row as an ordinary
    # body row and can't style it distinctly. Stamp its index so renderTable can
    # bold/shade it to match the live builder. The live builder never sets this
    # key (it appends + styles its own row), so its render path is unaffected.
    result["totals_row_index"] = len(rows)
    rows.append(totals_row)


# ─────────────────────────────────────────────────────────────────────────────
# Frequency enum + rolling date-range modes
# ─────────────────────────────────────────────────────────────────────────────

FREQ_DAILY = "daily"
FREQ_WEEKLY = "weekly"
FREQ_BIWEEKLY = "biweekly"
FREQ_MONTHLY = "monthly"
FREQ_QUARTERLY = "quarterly"
_VALID_FREQUENCIES = (FREQ_DAILY, FREQ_WEEKLY, FREQ_BIWEEKLY, FREQ_MONTHLY, FREQ_QUARTERLY)

# quarterly is an EMAIL cadence only. An import schedule's pull window
# is derived from its frequency (_FREQUENCY_WINDOW_MODE), and a
# quarter-long pull every run is not something the importers are sized for, so
# create/update refuse it for non-email schedules.
_EMAIL_ONLY_FREQUENCIES = (FREQ_QUARTERLY,)

# Rolling windows. Each resolves at *run time* relative to "now" — see
# resolve_date_range. AC: "previous 7 days" always = the 7 days ending
# yesterday, never the 7 days ending today, so a Monday run reports a full
# Sun-back-to-prev-Mon window, not a partial today.
RANGE_PREV_DAY = "rolling_1d"
RANGE_PREV_7D = "rolling_7d"
RANGE_PREV_14D = "rolling_14d"
RANGE_PREV_30D = "rolling_30d"
RANGE_PREV_CAL_WEEK = "prev_calendar_week"
RANGE_PREV_CAL_MONTH = "prev_calendar_month"
RANGE_PREV_CAL_QUARTER = "prev_calendar_quarter"
RANGE_MONTH_TO_DATE = "month_to_date"
_VALID_DATE_RANGE_MODES = (
    RANGE_PREV_DAY,
    RANGE_PREV_7D,
    RANGE_PREV_14D,
    RANGE_PREV_30D,
    RANGE_PREV_CAL_WEEK,
    RANGE_PREV_CAL_MONTH,
    RANGE_PREV_CAL_QUARTER,
    RANGE_MONTH_TO_DATE,
)

# an IMPORT schedule's pull window is now derived from its FREQUENCY,
# not a standalone Date Range selector (which is removed from the import dialog).
# When the user picks a cadence they pick the window with it, so each run pulls
# only the new period instead of re-pulling a fixed 7-day window every time:
#   daily    → the previous 1 day      (yesterday)
#   weekly   → the previous calendar week (Mon..Sun, complete)
#   biweekly → the previous 14 days    (complete, ending yesterday)
#   monthly  → the previous calendar month
# All windows END YESTERDAY (the existing resolve_date_range contract — a run
# never reports a partial today). EMAIL schedules keep an independent
# Date Range and are unaffected by this mapping.
_FREQUENCY_WINDOW_MODE = {
    FREQ_DAILY: RANGE_PREV_DAY,
    FREQ_WEEKLY: RANGE_PREV_CAL_WEEK,
    FREQ_BIWEEKLY: RANGE_PREV_14D,
    FREQ_MONTHLY: RANGE_PREV_CAL_MONTH,
}

# a DAILY import schedule can pull the previous N complete days
# (ending yesterday) instead of always exactly 1. Stored on
# ``schedule.days_to_pull``; absent on earlier rows, which must resolve
# identically to RANGE_PREV_DAY (1 day) — no migration. Bounds match the form
# input's min/max. Other frequencies keep their fixed frequency-derived windows.
DAILY_DAYS_DEFAULT = 1
DAILY_DAYS_MIN = 1
DAILY_DAYS_MAX = 7


def clamp_daily_days(value, *, default: int = DAILY_DAYS_DEFAULT) -> int:
    """coerce a caller-supplied daily "days to pull" to a valid int
    in [DAILY_DAYS_MIN, DAILY_DAYS_MAX].

    A missing/None/non-numeric value falls back to ``default`` (the field is
    optional and absent on earlier schedules, where 1 = the previous-day
    behavior). Out-of-range numbers are clamped rather than rejected so a
    hand-crafted API body can't widen a daily pull past 7 days. A PURE helper
    so the route assembly and the window resolution agree on the bounds."""
    try:
        n = int(value)
    except (TypeError, ValueError):
        return default
    if n < DAILY_DAYS_MIN:
        return DAILY_DAYS_MIN
    if n > DAILY_DAYS_MAX:
        return DAILY_DAYS_MAX
    return n


# Snapshot payload schema version. Bump when the stored ``data`` shape changes
# so a future renderer can branch on old vs new snapshots.
#   v1 (P1) — stub: query envelope + resolved window + empty charts/tables.
#   v2 (P2) — real precompute: charts[]/tables[] carry per-chart
#             {chart_id, chart_type, title, result} from build_query+transform.
SNAPSHOT_FORMAT_VERSION = 2

# how many snapshots a schedule retains. The Edit dialog exposes a
# "Number to keep" field (visible only when save_snapshot is on); its value is
# stored on delivery.snapshot_keep. Every run prunes the oldest snapshots beyond
# this count so the retained set stays synced to the chosen value. Bounds match
# the form input's min/max; the default applies to schedules created before this
# field existed (no delivery.snapshot_keep).
SNAPSHOT_KEEP_DEFAULT = 7
SNAPSHOT_KEEP_MIN = 1
SNAPSHOT_KEEP_MAX = 100


def clamp_snapshot_keep(value, *, default: int = SNAPSHOT_KEEP_DEFAULT) -> int:
    """coerce a caller-supplied ``snapshot_keep`` to a valid int in
    [SNAPSHOT_KEEP_MIN, SNAPSHOT_KEEP_MAX].

    A missing/None/non-numeric value falls back to ``default`` (the field is
    optional and absent on earlier schedules). Out-of-range numbers are
    clamped to the bounds rather than rejected, so a hand-crafted API body can't
    push the retained set to 0 (delete everything) or unbounded. A PURE helper so
    the route, the form-assembly, and the prune path all agree on the bounds."""
    try:
        n = int(value)
    except (TypeError, ValueError):
        return default
    if n < SNAPSHOT_KEEP_MIN:
        return SNAPSHOT_KEEP_MIN
    if n > SNAPSHOT_KEEP_MAX:
        return SNAPSHOT_KEEP_MAX
    return n


# Chart types whose transform() output is a tabular result (goes in data.tables)
# rather than an ECharts series option (data.charts). Mirrors the run_query
# branch in dashboard_builder/routes.py.
_TABLE_CHART_TYPES = frozenset({"table", "posts_table"})
# Chart type whose transform() output is a single {label, value} KPI — collected
# into data.summary_metrics in addition to data.charts.
_METRIC_CHART_TYPE = "metric"

# The ECharts dashboards index a schedule's dashboard_id points at. The doc _id
# IS the dashboard_id, so _load_dashboard_charts' es.get(id=dashboard_id) works.
# v2 (2026-06-11): corrected from de_asset_dashboards (a near-empty
# legacy index — 1 stale doc on staging) to de_asset_echarts_dashboards (the
# real 44-doc store the dashboard builder writes). The P2 bug was masked because
# the P2 live smoke created a throwaway de_asset_dashboards doc; a REAL dashboard
# would have loaded zero charts → empty precompute.
INDEX_DASHBOARDS = "de_asset_echarts_dashboards"
INDEX_TEAMS = "de_asset_teams"

# Impact Report chart types (report / google_report / combined_report)
# are rendered entirely client-side by charts.js and carry none of the fields
# es_query_builder.build_query expects (no data_view / x_field / filters). Feeding
# one to build_query produces a malformed terms clause (VALUE_NULL) that ES rejects
# 400 and the precompute freezes into the snapshot as an error. Instead of trying to
# compute them headlessly, the snapshot/email links out to the live report — the
# public /public/report/<token> viewer when the dashboard is published for the
# schedule's scope, else the login-gated in-app dashboard URL.
# canonical definition moved to export_capability_service (this
# module and dashboard_builder.routes carried drifting copies before).
try:
    from asset_management.web.services.export_capability_service import IMPACT_REPORT_CHART_TYPES
except ImportError:
    try:
        from web.services.export_capability_service import IMPACT_REPORT_CHART_TYPES
    except ImportError:
        from services.export_capability_service import IMPACT_REPORT_CHART_TYPES
# In-app dashboard viewer route (login-gated). Served under nginx's /assets/ mount
# (PrefixMiddleware strips it) — same prefix caveat as the snapshot link.
_DASHBOARD_VIEW_PATH = "/assets/dashboard-builder/view/{dashboard_id}"
# Public report viewer route (no login). Token-scoped.
_PUBLIC_REPORT_PATH = "/assets/public/report/{token}"

# In-container path to the import dispatcher (the runner runs inside
# imports_container; this is the same entry the cron uses).
_IMPORT_PY = "/app/python/import.py"

# v2 Phase D — connector_id → how the scoped import is dispatched.
#   source     : import_scripts.<source>.run_daily_imports
#   scope_flag : the per-connector team/asset scope CLI flag
#   resolver   : None  → the flag value is the team_esid(s) directly
#                "google_ads" / "ga4" → resolve team_esid → customer_ids / property_ids
#   multi      : True  → one dispatch with comma-joined values
#                False → one dispatch PER value
# chatwoot was previously absent (its importer had no team-scope
# flag); it now accepts --team_ids and resolves teams → accounts, so it's wired.
# echo is multi:True. Echo's GraphQL API is tenant-scoped (no team
# concept): EVERY echo run pulls the full tenant stream and filters, so one
# dispatch per team meant N full-tenant pulls — and, worse, the N concurrent
# Popens raced the per-connector importer lock, which ABORTS (not queues), so
# all but one team's import was silently dropped. --limit_teams accepts a
# comma-separated list end-to-end and the importer contacts
# only the tenants owning the listed teams, each once. run_due additionally
# batches ALL due echo schedules into a single dispatch (see _batch_echo_import).
_CONNECTOR_IMPORT_DISPATCH = {
    "meta_ads": {"source": "facebook", "scope_flag": "team_ids", "resolver": None, "multi": True},
    "youtube": {"source": "youtube", "scope_flag": "team_ids", "resolver": None, "multi": True},
    "echo": {"source": "echo", "scope_flag": "limit_teams", "resolver": None, "multi": True},
    # DT's importer already supports team scoping: run_daily_imports
    # accepts --team_ids=a,b (native team_esids, no resolver) and writes per-team
    # to {team_esid}__disciple_tools. multi:True comma-joins into one dispatch.
    "disciple_tools": {"source": "disciple_tools", "scope_flag": "team_ids", "resolver": None, "multi": True},
    # chatwoot's importer now accepts --team_ids=a,b: each team maps
    # to one chatwoot account (several teams may share one, like echo), and
    # run_daily_imports resolves the teams to their accounts and pulls only
    # those. Native team_esids (no resolver); multi:True comma-joins into one
    # dispatch so the tenant is contacted once.
    "chatwoot": {"source": "chatwoot", "scope_flag": "team_ids", "resolver": None, "multi": True},
    # bitly is CONNECTION-scoped (Phase 3): its team attribution
    # lives on the active bitly de_connection rows (per-team tag filters), not
    # a per-team CLI flag. scope_flag None means ONE dispatch with no scope
    # flag — the importer covers every active connection, and its own scope
    # guard refuses to run when none exist. Its window is a trailing --days
    # count, not start/end dates (window_flag), derived from the schedule's
    # resolved from-date; the importer clamps to its 30-day Bitly API cap.
    "bitly": {"source": "bitly", "scope_flag": None, "resolver": None, "multi": True, "window_flag": "days"},
    # TikTok organic: team-scoped like youtube (native team_esids,
    # multi:True → one dispatch). The importer takes today's snapshot and ignores
    # --start_date/--end_date (the Display API has lifetime counts only, no
    # history), so a backfill window cannot reach back in time.
    "tiktok": {"source": "tiktok", "scope_flag": "team_ids", "resolver": None, "multi": True},
    "google_ads": {"source": "google_ads", "scope_flag": "customer_ids", "resolver": "google_ads", "multi": True},
    "google_analytics_4": {
        "source": "google_analytics",
        "scope_flag": "property_ids",
        "resolver": "ga4",
        "multi": True,
    },
}

# Sources whose run_daily_imports acquires the cluster import lock
# (lib.import_lock_service). The Run-now import-status poll can track those via
# ImportLockService.is_held; youtube takes no lock, so its background import
# has no live signal (the poll endpoint reports trackable: False).
# Phase 3 — bitly acquires the connector-global lock (scope_id "").
# youtube now takes a per-team-set lock (so it sits inside the global
# import cap); listing it here is what lets Cancel force_release that lock.
_LOCKED_IMPORT_SOURCES = frozenset({"facebook", "google_ads", "echo", "google_analytics", "bitly", "youtube"})

# sources whose run_daily_imports honors --progress_run_id and writes
# live per-stage / per-day progress to de_import_progress (ImportProgressService).
# The Run-now bar reads that doc for a real percentage + stage message instead of
# the coarse per-connector lock. Add a source here once its importer adopts an
# explicit --progress_run_id and writes update_progress/complete_run against it.
# facebook — per-day ad loop + page/instagram phases
# google_ads — milestone day bumps on the external_run_id path
# google_analytics — milestone day bumps on the external_run_id path
# echo — milestone day bumps on the external_run_id path
# youtube — 3-step phase progress (fetch/index/archive)
# bitly — 3-step phase progress (fetch/index/archive)
# tiktok — 3-step phase progress (fetch/index/archive)
_PROGRESS_IMPORT_SOURCES = frozenset({"facebook", "google_ads", "google_analytics", "echo", "youtube", "bitly", "tiktok"})

# importer sources whose run_daily_imports takes a PER-SCOPE import lock
# (keyed on the dispatch's resolved id set) rather than the connector-global lock,
# so independent teams/customers/properties run concurrently. These are the
# per-connection-OAuth connectors with no shared mutable credential (google_ads
# , ga4). echo joins the per-scope set, but its scope is
# PER-TENANT not per-team: its rate-limit lock is per-tenant, so the dispatch resolves
# its team set → tenant set before keying the lock (see the echo branch in
# _trigger_scoped_import). facebook joins too: its lock is PER-CONNECTION
# (facebook::<connection_esid>) when the team cohort resolves to one wizard
# connection, via meta_connection_scope_key; legacy/mixed/unresolvable cohorts fall
# back to "" (connector-global), same as before. youtube joins: its
# lock is keyed on the dispatch's TEAM-ESID set (scope_key_for_ids, the generic
# branch below — the importer is team-scoped and has no connection key). Keep in
# sync with the scope_id passed at each importer's ImportLockService.acquire call.
_PER_SCOPE_LOCK_SOURCES = frozenset({"google_ads", "google_analytics", "echo", "facebook", "youtube"})

# YouTube reports progress by step (fetch -> index -> archive), not by
# date, so its progress denominator is this fixed step count. Keep in sync with
# the importer's _YT_STEPS_TOTAL.
_YOUTUBE_PROGRESS_STEPS = 3

# ─────────────────────────────────────────────────────────────────────────────
# Mappings
# ─────────────────────────────────────────────────────────────────────────────

REPORT_SCHEDULE_MAPPING = {
    "properties": {
        # Mirrors the ES _id (ES-generated). Stored in the body so a list
        # response carries the id without the caller threading _id separately.
        "schedule_id": {"type": "keyword"},
        # Org/team scope. team_id == "__all__" means "all teams under the org".
        # Both keyed for the scope-intersection terms filter in list_schedules.
        # v2 (2026-06-11): a schedule can now scope MULTIPLE orgs/teams.
        # team_esids/org_esids are the v2 multi-select lists; org_id/team_id are
        # kept as the v1 single fields (back-filled with the first selection) so
        # v1 readers + existing rows keep working. A v1 row (no *_esids) reads as
        # a single-team v2 row via _schedule_team_esids/_schedule_org_esids.
        "org_id": {"type": "keyword"},
        "team_id": {"type": "keyword"},
        "org_esids": {"type": "keyword"},
        "team_esids": {"type": "keyword"},
        # The connector this schedule imports + reports on (meta_ads / google_ads
        # / google_analytics_4 / youtube / echo / chatwoot). v2: the schedule's
        # identity is (connector + teams); the dashboard is for the email render.
        "connector_id": {"type": "keyword"},
        "name": {"type": "text"},
        "description": {"type": "text"},
        # the de_asset_dashboards doc this schedule precomputes.
        # Source of truth for WHAT to compute (the ticket's standalone ``query``
        # envelope is kept as optional metadata; dashboard charts are what the
        # renderer can actually execute + the View UI can render). A schedule
        # with no dashboard_id still snapshots (stub, precompute skipped).
        "dashboard_id": {"type": "keyword"},
        # follow-up — v2 MULTI-dashboard: an email schedule may carry
        # several dashboards, one link each in the same email. ``dashboard_id``
        # is retained and back-filled with the first entry for v1 readers (same
        # dual-write shape as team_id/team_esids). keyword is already
        # multi-valued in ES, so this needs no reindex — ensure_indices_exist's
        # in-place put_mapping adds it. Read via _schedule_dashboard_ids, never
        # directly, so a v1 row (scalar only) normalizes to a one-item list.
        "dashboard_ids": {"type": "keyword"},
        # Free-form query envelope (index_patterns / metrics / filters). We read
        # it back whole and never query into it → enabled:false. P2 keeps it for
        # traceability; dashboard_id drives precompute.
        "query": {"type": "object", "enabled": False},
        # Delivery preferences (save_snapshot + email block). Same — opaque blob.
        "delivery": {"type": "object", "enabled": False},
        # Schedule cadence. The structured fields drive compute_next_run; cron is
        # stored for display + future generality. timezone is an IANA name.
        "schedule": {"type": "object", "enabled": False},
        # last_run / next_run are the poll-runner's load-bearing fields: it
        # queries next_run<=now AND status=active. Kept top-level + keyed dates
        # (NOT inside the disabled ``schedule`` blob) so the range query works.
        "last_run": {"type": "date"},
        "next_run": {"type": "date"},
        # #5 — persisted state of the MOST RECENT run, so the page can
        # rehydrate a progress bar after navigation/reload (the old feedback was
        # an in-memory setInterval lost on reload). Opaque blob: {phase, started_at,
        # finished_at, snapshot_id, import_dispatched, connector_id, error}. Phase
        # is importing → generating → done | failed. Only the latest run is kept.
        "last_run_state": {"type": "object", "enabled": False},
        "status": {"type": "keyword"},
        "created_by": {"type": "keyword"},
        "created_at": {"type": "date"},
        "updated_at": {"type": "date"},
    }
}

REPORT_SNAPSHOT_MAPPING = {
    "properties": {
        "snapshot_id": {"type": "keyword"},
        "schedule_id": {"type": "keyword"},
        # Denormalised scope so a snapshot can be access-filtered without a
        # join back to its schedule (the schedule may be deleted; the snapshot
        # history outlives it).
        "org_id": {"type": "keyword"},
        "team_id": {"type": "keyword"},
        "generated_at": {"type": "date"},
        # Resolved window the run covered. Keyed dates so history can filter.
        "date_range": {
            "properties": {
                "from": {"type": "date", "format": "yyyy-MM-dd"},
                "to": {"type": "date", "format": "yyyy-MM-dd"},
            }
        },
        # Pre-computed payload. P1 = stub (query envelope + resolved window +
        # empty summary_metrics). P2 fills charts/tables. Opaque blob.
        "data": {"type": "object", "enabled": False},
        "format_version": {"type": "integer"},
        "size_bytes": {"type": "integer"},
        # record kind. Absent / "snapshot" = a normal report snapshot
        # (the only kind before this ticket). "backfill" = a one-time historical
        # backfill RUN record (no viewable report content): it lists in the History
        # modal noting what was backfilled, but has no snapshot data to open.
        "record_type": {"type": "keyword"},
        # backfill metadata, present only when record_type == "backfill".
        "backfill": {
            "properties": {
                "days": {"type": "integer"},
                "connector_id": {"type": "keyword"},
                "team_esids": {"type": "keyword"},
                "requested_by": {"type": "keyword"},
                # overwrite choice + settled outcome (written at finalize).
                "overwrite": {"type": "boolean"},
                "bailed_out": {"type": "boolean"},
                "error_count": {"type": "integer"},
                "covered_from": {"type": "keyword"},
                "covered_to": {"type": "keyword"},
                "errors": {
                    "properties": {
                        "category": {"type": "keyword"},
                        "title": {"type": "keyword"},
                        "message": {"type": "text"},
                        "severity": {"type": "keyword"},
                    }
                },
            }
        },
    }
}


def _import_first(*candidates):
    """Import the first resolvable module from a list of candidate dotted names.

    The renderer + role-filter modules sit at different import roots depending on
    how the process was started:
      * container runtime (``/app/python``) → ``asset_management.web.services.*``
      * some app contexts                   → ``web.services.*``
      * pytest (conftest puts .../web on path) → ``services.*``
    This service is imported under all three, so a single hard-coded path breaks
    one of them (caught by the P2 live smoke 2026-06-09). Mirrors the
    tools-container three-candidate resolver pattern.
    """
    import importlib

    last_err = None
    for name in candidates:
        try:
            return importlib.import_module(name)
        except ImportError as e:
            last_err = e
    raise last_err  # all candidates failed → surface the last ImportError


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _now_iso() -> str:
    return _now().isoformat()


class ReportScheduleError(UserFacingError):
    """Base exception for the report scheduler service."""


class ReportScheduleService:
    """CRUD + scheduling math for report schedules and their snapshots.

    Methods return the ``{"success": bool, ...}`` envelope used across the
    web/services layer (mirrors ConnectionService) rather than raising, except
    the pure scheduling helpers (compute_next_run / resolve_date_range) which
    raise ReportScheduleError on bad input so callers fail loudly at create.
    """

    # ─────────────────────────────────────────────────────────────────────────
    # Index bootstrap
    # ─────────────────────────────────────────────────────────────────────────

    @staticmethod
    def ensure_indices_exist() -> None:
        """Create both report-scheduler indices if absent; update mappings in
        place if extended. Mirrors ConnectionService.ensure_indices_exist."""
        es = get_elasticsearch_client()
        if es is None:
            logging.warning("ReportScheduleService: could not connect to ES")
            return
        for index_name, mapping in (
            (INDEX_REPORT_SCHEDULES, REPORT_SCHEDULE_MAPPING),
            (INDEX_REPORT_SNAPSHOTS, REPORT_SNAPSHOT_MAPPING),
        ):
            put_regular_index_template(es, index_name, mapping, priority=500)
            create_index_if_not_exists(es, index_name, mapping)
            try:
                es.indices.put_mapping(index=index_name, body=mapping)
            except Exception as e:
                logging.warning(
                    "ReportScheduleService: put_mapping update on %s: %s",
                    index_name,
                    e,
                )

    # ─────────────────────────────────────────────────────────────────────────
    # Scheduling math (pure — no ES)
    # ─────────────────────────────────────────────────────────────────────────

    @staticmethod
    def compute_next_run(
        schedule: dict,
        *,
        after: datetime | None = None,
    ) -> datetime:
        """Return the next UTC fire time strictly after ``after`` (default now).

        Reads the structured cadence from ``schedule``:
          * ``frequency``       — daily / weekly / biweekly / monthly / quarterly
          * ``time``            — "HH:MM" local wall-clock in ``timezone``
          * ``timezone``        — IANA tz name (e.g. "America/New_York")
          * ``day_of_week``     — 0=Mon … 6=Sun, required for weekly/biweekly
          * ``day_of_month``    — 1..28, required for monthly and quarterly
                                  (capped at 28 so every month has the day);
                                  quarterly fires on it in Jan / Apr / Jul / Oct

        Hand-rolled (no croniter): the form constrains input to these shapes.
        Raises ReportScheduleError on a malformed schedule so a bad create
        fails loudly rather than silently never firing.
        """
        freq = (schedule or {}).get("frequency")
        if freq not in _VALID_FREQUENCIES:
            raise ReportScheduleError(f"frequency must be one of {_VALID_FREQUENCIES}; got {freq!r}")

        tz_name = (schedule or {}).get("timezone") or "UTC"
        try:
            tz = ZoneInfo(tz_name)
        except Exception as e:  # noqa: BLE001 — bad tz string is caller error
            raise ReportScheduleError(f"invalid timezone {tz_name!r}") from e

        hh, mm = ReportScheduleService._parse_hhmm(schedule.get("time"))

        # Anchor "now" in the schedule's local zone so DOW/DOM math matches the
        # user's intent (a "Monday 06:00" schedule fires at 06:00 *their* time).
        after = after or _now()
        if after.tzinfo is None:
            after = after.replace(tzinfo=timezone.utc)
        local_after = after.astimezone(tz)

        if freq == FREQ_DAILY:
            nxt = ReportScheduleService._next_daily(local_after, hh, mm)
        elif freq in (FREQ_WEEKLY, FREQ_BIWEEKLY):
            dow = schedule.get("day_of_week")
            if not isinstance(dow, int) or not (0 <= dow <= 6):
                raise ReportScheduleError(f"day_of_week (0=Mon..6=Sun) required for {freq}; got {dow!r}")
            # For biweekly this is the FIRST fire only (create / edit / resume).
            # After a run, _next_run_after_fire steps 14 days from the fire that
            # just ran.
            nxt = ReportScheduleService._next_weekly(local_after, hh, mm, dow)
        else:  # FREQ_MONTHLY / FREQ_QUARTERLY
            dom = schedule.get("day_of_month")
            if not isinstance(dom, int) or not (1 <= dom <= 28):
                raise ReportScheduleError(f"day_of_month (1..28) required for {freq}; got {dom!r}")
            if freq == FREQ_QUARTERLY:
                nxt = ReportScheduleService._next_quarterly(local_after, hh, mm, dom)
            else:
                nxt = ReportScheduleService._next_monthly(local_after, hh, mm, dom)

        return nxt.astimezone(timezone.utc)

    @staticmethod
    def _next_run_after_fire(
        cadence: dict,
        previous_next_run: str | None,
        *,
        now: datetime,
    ) -> datetime:
        """The next fire time after a run that started at ``now``.

        Every cadence except biweekly is ``compute_next_run(after=now)``.

        biweekly steps 14 days from ``previous_next_run`` (the fire
        that just ran), not to the next weekly hit, so the schedule keeps its
        two-week phase:
          * a Run-now before the due time leaves the pending fire where it is;
          * a scheduler that was down past one or more fires catches up on the
            same phase (the first fire strictly after ``now``);
          * no stored next_run falls back to the next weekly hit (first fire).
        Each step asks for the named weekday strictly after +10 days, which is
        always +14 days in local wall-clock time: a DST change shifts the UTC
        gap by an hour, never by the 3+ days that would land on another week.
        """
        if (cadence or {}).get("frequency") != FREQ_BIWEEKLY or not previous_next_run:
            return ReportScheduleService.compute_next_run(cadence, after=now)
        previous = datetime.fromisoformat(previous_next_run)
        if previous.tzinfo is None:
            previous = previous.replace(tzinfo=timezone.utc)
        if now.tzinfo is None:
            now = now.replace(tzinfo=timezone.utc)
        if previous > now:
            return previous
        nxt = previous
        while nxt <= now:
            nxt = ReportScheduleService.compute_next_run(cadence, after=nxt + timedelta(days=10))
        return nxt

    @staticmethod
    def _parse_hhmm(value: str | None) -> tuple[int, int]:
        if not value:
            return (0, 0)
        try:
            hh_s, mm_s = str(value).split(":", 1)
            hh, mm = int(hh_s), int(mm_s)
        except Exception as e:  # noqa: BLE001 — caller-supplied string
            raise ReportScheduleError(f"invalid time {value!r}, expected HH:MM") from e
        if not (0 <= hh <= 23 and 0 <= mm <= 59):
            raise ReportScheduleError(f"time {value!r} out of range")
        return (hh, mm)

    @staticmethod
    def _next_daily(local_after: datetime, hh: int, mm: int) -> datetime:
        candidate = local_after.replace(hour=hh, minute=mm, second=0, microsecond=0)
        if candidate <= local_after:
            candidate += timedelta(days=1)
        return candidate

    @staticmethod
    def _next_weekly(local_after: datetime, hh: int, mm: int, dow: int) -> datetime:
        candidate = local_after.replace(hour=hh, minute=mm, second=0, microsecond=0)
        days_ahead = (dow - candidate.weekday()) % 7
        candidate += timedelta(days=days_ahead)
        if candidate <= local_after:
            candidate += timedelta(days=7)
        return candidate

    @staticmethod
    def _next_monthly(local_after: datetime, hh: int, mm: int, dom: int) -> datetime:
        candidate = local_after.replace(day=dom, hour=hh, minute=mm, second=0, microsecond=0)
        if candidate <= local_after:
            # Roll to the same day next month (dom<=28 so always valid).
            year = candidate.year + (1 if candidate.month == 12 else 0)
            month = 1 if candidate.month == 12 else candidate.month + 1
            candidate = candidate.replace(year=year, month=month)
        return candidate

    @staticmethod
    def _next_quarterly(local_after: datetime, hh: int, mm: int, dom: int) -> datetime:
        """day ``dom`` of the first month of a calendar quarter
        (Jan / Apr / Jul / Oct), strictly after ``local_after``."""
        quarter_start_month = ((local_after.month - 1) // 3) * 3 + 1
        candidate = local_after.replace(month=quarter_start_month, day=dom, hour=hh, minute=mm, second=0, microsecond=0)
        if candidate <= local_after:
            # Roll to the next quarter's first month (dom<=28 so always valid).
            year = candidate.year + (1 if quarter_start_month == 10 else 0)
            month = 1 if quarter_start_month == 10 else quarter_start_month + 3
            candidate = candidate.replace(year=year, month=month)
        return candidate

    @staticmethod
    def resolve_date_range(mode: str, *, now: datetime | None = None) -> dict:
        """Resolve a rolling-window mode to a concrete {from, to} date pair
        (inclusive, ``YYYY-MM-DD`` strings) at run time.

        ``to`` is yesterday for the rolling/calendar windows (a report run today
        covers complete days, never a partial today) and today for MTD.
        """
        if mode not in _VALID_DATE_RANGE_MODES:
            raise ReportScheduleError(f"date_range_mode must be one of {_VALID_DATE_RANGE_MODES}; got {mode!r}")
        now = now or _now()
        today = now.date()
        yesterday = today - timedelta(days=1)

        if mode == RANGE_PREV_DAY:
            # daily import schedules pull just the previous day.
            return ReportScheduleService._fmt_range(yesterday, yesterday)
        if mode == RANGE_PREV_7D:
            return ReportScheduleService._fmt_range(yesterday - timedelta(days=6), yesterday)
        if mode == RANGE_PREV_14D:
            return ReportScheduleService._fmt_range(yesterday - timedelta(days=13), yesterday)
        if mode == RANGE_PREV_30D:
            return ReportScheduleService._fmt_range(yesterday - timedelta(days=29), yesterday)
        if mode == RANGE_PREV_CAL_WEEK:
            # ISO week: Monday-start. Previous calendar week = the Mon..Sun that
            # ended before this week's Monday.
            this_monday = today - timedelta(days=today.weekday())
            prev_monday = this_monday - timedelta(days=7)
            prev_sunday = this_monday - timedelta(days=1)
            return ReportScheduleService._fmt_range(prev_monday, prev_sunday)
        if mode == RANGE_PREV_CAL_MONTH:
            first_of_this_month = today.replace(day=1)
            last_of_prev = first_of_this_month - timedelta(days=1)
            first_of_prev = last_of_prev.replace(day=1)
            return ReportScheduleService._fmt_range(first_of_prev, last_of_prev)
        if mode == RANGE_PREV_CAL_QUARTER:
            # the complete calendar quarter before the current one
            # (Q1 = Jan..Mar, ...). A run on Jan 5 reports Oct 1..Dec 31.
            first_of_this_quarter = today.replace(month=((today.month - 1) // 3) * 3 + 1, day=1)
            last_of_prev = first_of_this_quarter - timedelta(days=1)
            first_of_prev = last_of_prev.replace(month=last_of_prev.month - 2, day=1)
            return ReportScheduleService._fmt_range(first_of_prev, last_of_prev)
        # RANGE_MONTH_TO_DATE — 1st of this month through today (inclusive).
        return ReportScheduleService._fmt_range(today.replace(day=1), today)

    @staticmethod
    def _fmt_range(frm: date, to: date) -> dict:
        return {"from": frm.isoformat(), "to": to.isoformat()}

    @staticmethod
    def window_mode_for_schedule(schedule: dict) -> str:
        """the date-range mode an IMPORT schedule pulls, derived from
        its frequency (the import dialog no longer has a standalone Date Range).

        EMAIL schedules keep an
        independent stored ``date_range_mode``; this returns that instead so an
        email run honours the user's explicit window choice. An import schedule's
        stored ``date_range_mode`` is left dormant (ignored) per the
        migration note. Unknown/legacy frequencies fall back to the previous
        7-day window so an old row never resolves to an empty range.
        """
        cfg = (schedule or {}).get("schedule") or {}
        if (schedule or {}).get("schedule_type") == SCHEDULE_TYPE_EMAIL:
            return cfg.get("date_range_mode", RANGE_PREV_7D)
        return _FREQUENCY_WINDOW_MODE.get(cfg.get("frequency"), RANGE_PREV_7D)

    @staticmethod
    def resolve_window_for_schedule(schedule: dict, *, now: datetime | None = None) -> dict:
        """Concrete {from,to} window for one schedule — the import-vs-email-aware
        wrapper over ``resolve_date_range``. Import schedules derive the mode from
        frequency; email schedules use their stored date_range_mode.

        a DAILY import schedule may carry ``schedule.days_to_pull``
        (1..7): its window is then the previous N complete days ending yesterday
        (same "a run never reports a partial today" contract). The fixed mode
        enum can't express a parameterized span, hence this branch rather than a
        new mode. days_to_pull absent / 1 / unparseable resolves identically to
        RANGE_PREV_DAY so earlier rows are unchanged.
        """
        cfg = (schedule or {}).get("schedule") or {}
        if (schedule or {}).get("schedule_type") != SCHEDULE_TYPE_EMAIL and cfg.get("frequency") == FREQ_DAILY:
            days = clamp_daily_days(cfg.get("days_to_pull"))
            if days > 1:
                now = now or _now()
                yesterday = now.date() - timedelta(days=1)
                return ReportScheduleService._fmt_range(yesterday - timedelta(days=days - 1), yesterday)
        return ReportScheduleService.resolve_date_range(ReportScheduleService.window_mode_for_schedule(schedule), now=now)

    @staticmethod
    def _date_range_days(date_range: dict) -> int:
        """inclusive day count for a {from,to} range (matches the FB
        importer's per-day fetch loop, which iterates start..end inclusive).

        Returns 0 on a missing/unparseable range so the caller skips minting a
        progress doc rather than minting one with a bogus denominator."""
        frm = (date_range or {}).get("from") or ""
        to = (date_range or {}).get("to") or ""
        try:
            d0 = datetime.strptime(frm, "%Y-%m-%d").date()
            d1 = datetime.strptime(to, "%Y-%m-%d").date()
        except (TypeError, ValueError):
            return 0
        return max(0, (d1 - d0).days + 1)

    @staticmethod
    def _trailing_days_from(date_range: dict, *, now: datetime | None = None) -> int:
        """Phase 2 — the trailing-window day count a ``--days``-style
        importer (bitly) needs to cover the resolved window's FROM date.

        Trailing windows anchor at NOW, not at the window's ``to`` — a schedule
        window ending yesterday still needs (today - from + 1) trailing days to
        include its first day. Returns 0 on a missing/unparseable from-date so
        the caller omits the flag (the importer then applies its own default).
        """
        frm = (date_range or {}).get("from") or ""
        try:
            d0 = datetime.strptime(frm, "%Y-%m-%d").date()
        except (TypeError, ValueError):
            return 0
        return max(1, ((now or _now()).date() - d0).days + 1)

    # ─────────────────────────────────────────────────────────────────────────
    # Scope helpers
    # ─────────────────────────────────────────────────────────────────────────

    @staticmethod
    def _scope_allows(scope, org_id: str, team_id: str) -> bool:
        """True iff a caller with ``scope`` (a UserScope) may act on a schedule
        scoped to (org_id, team_id). A privileged/empty-restriction scope is the
        caller's concern — this checks intersection against the resolved esids.

        team_id == "__all__" means the schedule is org-wide; org membership is
        then sufficient.
        """
        org_esids = getattr(scope, "org_esids", frozenset()) or frozenset()
        team_esids = getattr(scope, "team_esids", frozenset()) or frozenset()
        if org_id and org_id in org_esids:
            return True
        if team_id and team_id != "__all__" and team_id in team_esids:
            return True
        return False

    # ── v2 multi-team helpers ──────────────────────────────────

    @staticmethod
    def _schedule_team_esids(doc: dict) -> list[str]:
        """The teams a schedule scopes. v2 ``team_esids`` if present, else the v1
        single ``team_id`` (dropping the ``__all__`` org-wide sentinel)."""
        lst = doc.get("team_esids")
        if lst:
            return [t for t in lst if t]
        tid = doc.get("team_id")
        return [tid] if (tid and tid != "__all__") else []

    @staticmethod
    def _schedule_dashboard_ids(doc: dict) -> list[str]:
        """The dashboards a schedule emails, in check order. v2
        ``dashboard_ids`` if present, else the v1 single ``dashboard_id``
        . Every reader goes through this so a pre-v2 row —
        which has only the scalar — behaves as a one-dashboard schedule."""
        lst = doc.get("dashboard_ids")
        if lst:
            return [d for d in lst if d]
        did = doc.get("dashboard_id")
        return [did] if did else []

    @staticmethod
    def _schedule_org_esids(doc: dict) -> list[str]:
        """The orgs a schedule scopes. v2 ``org_esids`` if present, else the v1
        single ``org_id``."""
        lst = doc.get("org_esids")
        if lst:
            return [o for o in lst if o]
        oid = doc.get("org_id")
        return [oid] if oid else []

    @staticmethod
    def _scope_allows_any(scope, doc: dict) -> bool:
        """True iff the caller can see ANY of a schedule's selected orgs/teams.

        Covers both the v1 single (org_id/team_id) and v2 list (org_esids/
        team_esids) shapes via the helpers. A privileged/empty-restriction scope
        is the caller's concern (same contract as _scope_allows)."""
        scope_orgs = getattr(scope, "org_esids", frozenset()) or frozenset()
        scope_teams = getattr(scope, "team_esids", frozenset()) or frozenset()
        if any(o in scope_orgs for o in ReportScheduleService._schedule_org_esids(doc)):
            return True
        if any(t in scope_teams for t in ReportScheduleService._schedule_team_esids(doc)):
            return True
        return False

    @staticmethod
    def _scope_allows_all_teams(scope, team_esids: list[str]) -> bool:
        """True iff EVERY selected team is within ``scope``.

        The picker gate for explicit team selections. ``_scope_allows_any`` is
        the VISIBILITY predicate (seeing a schedule that touches any of your
        teams is correct); it is NOT a save gate — any-match would let a
        selection smuggle foreign teams in alongside one in-scope team,
        bypassing the creator clamp. An empty selection passes (it
        means "no explicit narrowing"; the clamp handles it downstream).
        """
        scope_teams = getattr(scope, "team_esids", frozenset()) or frozenset()
        return all(t in scope_teams for t in team_esids or [])

    @staticmethod
    def _scope_or_creator_allows(scope, doc: dict, *, created_by: str | None = None) -> bool:
        """single-schedule visibility predicate for the route gates
        (api_get / _mutate_gate / snapshot / import-status): visible iff the
        caller's ``scope`` intersects the schedule (``_scope_allows_any``) OR the
        caller created it. The creator leg mirrors list_schedules' created_by OR,
        so what a user can SEE in the list is exactly what they can fetch/gate on.
        A ``scope`` of None means see-all (server-internal); creator is ignored
        there."""
        if scope is None:
            return True
        if created_by and (doc or {}).get("created_by") == created_by:
            return True
        return ReportScheduleService._scope_allows_any(scope, doc)

    # ── Ownership / read-only ──────────────────────────────────

    @staticmethod
    def can_mutate(doc: dict, *, username: str, is_privileged: bool) -> bool:
        """True iff the caller may EDIT / Pause / Delete / Run-now this schedule.

         #1: an org owner sees every schedule touching their teams —
        including ones a workspace owner created for those teams (the wide tree,
         #4) — but may only mutate the ones they OWN. Ownership rule
        (decision 2026-06-15: creator + privilege):

          * privileged callers (superuser / asset_admin / workspace_admin /
            workspace_owner) may mutate anything they can see — they ARE the
            workspace authority;
          * everyone else may mutate ONLY a schedule they created
            (``created_by`` == their username).

        Anything else is read-only ("workspace owned" — History only). This is a
        PURE predicate so the route layer can both stamp ``can_edit`` on the list
        response (for the card UI) and re-check it as the server-side gate on the
        mutator routes (a crafted request must not bypass the hidden buttons).
        Scope/visibility (whether the caller may SEE the schedule at all) is a
        separate, earlier check (_scope_allows / _scope_allows_any); this only
        governs mutate-vs-read-only AMONG visible schedules.
        """
        if is_privileged:
            return True
        created_by = (doc or {}).get("created_by") or ""
        return bool(username) and created_by == username

    # ─────────────────────────────────────────────────────────────────────────
    # CRUD
    # ─────────────────────────────────────────────────────────────────────────

    @staticmethod
    def create_schedule(
        *,
        principal: str,
        org_id: str,
        team_id: str,
        name: str,
        query: dict,
        delivery: dict,
        schedule: dict,
        description: str = "",
        dashboard_id: str = "",
        dashboard_ids: list | None = None,
        connector_id: str = "",
        team_esids: list | None = None,
        org_esids: list | None = None,
        schedule_type: str = SCHEDULE_TYPE_IMPORT,
        scope=None,
    ) -> dict:
        """Create a schedule. Computes the first ``next_run`` from the cadence.

        v2: a schedule may scope MULTIPLE teams/orgs via
        ``team_esids``/``org_esids`` + carry a ``connector_id``. The v1 single
        ``org_id``/``team_id`` are back-filled from the first selection so v1
        readers + existing rows keep working.

        ``scope`` (a UserScope from resolve_user_scope) is enforced when
        provided: the caller may not create a schedule for any org/team they
        can't see. Passing ``scope=None`` skips the check (server-internal
        callers / the runner) — route callers MUST pass it.

        Returns ``{"success": True, "esid", "doc"}`` or ``{"success": False, "error"}``.
        """
        team_esids = [t for t in (team_esids or []) if t]
        org_esids = [o for o in (org_esids or []) if o]
        # Back-fill the v1 single fields from the first selection (v2 lists win).
        org_id = org_id or (org_esids[0] if org_esids else "")
        team_id = team_id or (team_esids[0] if team_esids else "")

        # an email schedule is creator-owned and carries no org/team
        # scope (it just emails a dashboard the creator can read). Skip the
        # org/team-required guard and the scope intersection for it.
        is_email = schedule_type == SCHEDULE_TYPE_EMAIL

        if not principal:
            return {"success": False, "error": "principal is required"}
        if not name:
            return {"success": False, "error": "name is required"}
        if not is_email and not org_id and not team_id and not org_esids and not team_esids:
            return {"success": False, "error": "an org or team selection is required"}
        if not is_email and (schedule or {}).get("frequency") in _EMAIL_ONLY_FREQUENCIES:
            return {"success": False, "error": "Quarterly is available for email reports only."}

        # scope check against the FULL selection, ALL-match. The old
        # any-match (_scope_allows_any, a VISIBILITY predicate) let one in-scope
        # team or org carry foreign teams in with it. A team selection is what
        # scopes the data (_resolve_scope_titles), so every selected team must be
        # in scope; with no team selected (v1 org-wide), every org must be.
        if not is_email and scope is not None:
            sel_teams = team_esids or ([team_id] if team_id and team_id != "__all__" else [])
            sel_orgs = org_esids or ([org_id] if org_id else [])
            scope_orgs = getattr(scope, "org_esids", frozenset()) or frozenset()
            if sel_teams:
                in_scope = ReportScheduleService._scope_allows_all_teams(scope, sel_teams)
            else:
                in_scope = bool(sel_orgs) and all(o in scope_orgs for o in sel_orgs)
            if not in_scope:
                return {
                    "success": False,
                    "error": "not authorized for the requested org/team scope",
                }
        elif is_email and team_esids:
            # an email schedule MAY carry an explicit team selection
            # (the dialog's optional narrowing). Every selected team must be
            # within the creator's scope — an any-match here would let one
            # in-scope team smuggle foreign teams past the clamp.
            if scope is not None and not ReportScheduleService._scope_allows_all_teams(scope, team_esids):
                return {
                    "success": False,
                    "error": "not authorized for the requested team selection",
                }

        try:
            next_run = ReportScheduleService.compute_next_run(schedule)
        except ReportScheduleError as e:
            return {"success": False, "error": e.public_message}

        es = get_elasticsearch_client()
        if es is None:
            return {"success": False, "error": "Could not connect to Elasticsearch"}

        now = _now_iso()
        doc = {
            "org_id": org_id,
            # email schedules default to no team scope (creator-owned);
            # keep the import default of "__all__" only for non-email schedules.
            # an email schedule MAY carry an explicit team_esids
            # selection (validated all-in-creator-scope above); the v1 team_id
            # back-fill from the first selected team then applies as usual.
            "team_id": team_id or ("" if is_email else "__all__"),
            "org_esids": org_esids,
            "team_esids": team_esids,
            # "import" (default) or "email". Coerced to a known value so
            # a malformed body can't store a type run_due wouldn't recognise.
            "schedule_type": schedule_type if schedule_type in _VALID_SCHEDULE_TYPES else SCHEDULE_TYPE_IMPORT,
            "connector_id": connector_id or "",
            "name": name,
            "description": description or "",
            # follow-up — dual-write: the v2 list is canonical, the v1
            # scalar carries its first entry. A caller passing only the scalar
            # (server-internal / v1) still gets a coherent one-item list.
            "dashboard_id": dashboard_id or (dashboard_ids[0] if dashboard_ids else ""),
            "dashboard_ids": [d for d in (dashboard_ids or []) if d] or ([dashboard_id] if dashboard_id else []),
            "query": query or {},
            "delivery": delivery or {},
            "schedule": schedule or {},
            "last_run": None,
            "next_run": next_run.isoformat(),
            "status": STATUS_ACTIVE,
            "created_by": principal,
            "created_at": now,
            "updated_at": now,
        }
        try:
            result = es.index(index=INDEX_REPORT_SCHEDULES, body=doc, refresh="wait_for")
        except Exception as e:
            logging.error("ReportScheduleService.create_schedule error: %s", e)
            return {"success": False, "error": "Failed to create the schedule."}

        esid = result["_id"]
        # Stamp schedule_id = _id so the body is self-describing.
        try:
            es.update(
                index=INDEX_REPORT_SCHEDULES,
                id=esid,
                body={"doc": {"schedule_id": esid}},
                refresh="wait_for",
            )
        except Exception as e:
            logging.warning("ReportScheduleService: schedule_id stamp on %s: %s", esid, e)
        doc["schedule_id"] = esid
        return {"success": True, "esid": esid, "doc": doc}

    @staticmethod
    def get_schedule(schedule_id: str) -> dict | None:
        """Return the schedule doc (with ``schedule_id`` set) or None."""
        es = get_elasticsearch_client()
        if es is None:
            return None
        try:
            res = es.get(index=INDEX_REPORT_SCHEDULES, id=schedule_id)
        except Exception:
            return None
        src = res.get("_source") or {}
        src["schedule_id"] = res["_id"]
        return src

    @staticmethod
    def list_schedules(*, scope=None, status: str | None = None, created_by: str | None = None, size: int = 200) -> list[dict]:
        """List schedules visible to ``scope``. With ``scope=None`` returns all
        (server-internal use). With a UserScope, filters to schedules whose
        org_id ∈ scope.org_esids OR team_id ∈ scope.team_esids.

        ``created_by`` adds a creator leg: a schedule is visible if its
        org/team intersects ``scope`` OR it was created by ``created_by``. So a
        user always sees schedules they made, even ones scoped to teams not in
        their resolved scope (e.g. a workspace-tree team they aren't a direct
        member of). A caller with no scope intersection but a ``created_by`` still
        sees their own — so the empty-scope short-circuit only fires when there's
        ALSO no creator leg.
        """
        es = get_elasticsearch_client()
        if es is None:
            return []

        must: list[dict] = []
        if status:
            must.append({"term": {"status": status}})

        if scope is not None:
            org_esids = list(getattr(scope, "org_esids", frozenset()) or [])
            team_esids = list(getattr(scope, "team_esids", frozenset()) or [])
            # v2: match the v1 single fields OR the v2 lists, so a multi-team
            # schedule surfaces for a member of ANY of its teams. the
            # creator leg is OR'd in so a user always sees their own schedules.
            should = []
            if org_esids:
                should.append({"terms": {"org_id": org_esids}})
                should.append({"terms": {"org_esids": org_esids}})
            if team_esids:
                should.append({"terms": {"team_id": team_esids}})
                should.append({"terms": {"team_esids": team_esids}})
            if created_by:
                should.append({"term": {"created_by": created_by}})
            if not should:
                return []  # no resolvable scope AND no creator → see nothing
            must.append({"bool": {"should": should, "minimum_should_match": 1}})

        query = {"bool": {"must": must}} if must else {"match_all": {}}
        try:
            res = es.search(
                index=INDEX_REPORT_SCHEDULES,
                body={"query": query, "size": size, "sort": [{"created_at": "desc"}]},
            )
        except Exception as e:
            logging.error("ReportScheduleService.list_schedules error: %s", e)
            return []
        out = []
        for hit in res.get("hits", {}).get("hits", []):
            src = hit.get("_source") or {}
            src["schedule_id"] = hit["_id"]
            # lazily capture each schedule's fire-and-forget import
            # outcome onto last_run_state once the import has settled (best-effort,
            # idempotent, terminal-only). No-op for runs without a progress doc or
            # already back-filled, so a steady-state poll does no extra ES writes.
            src = ReportScheduleService.backfill_import_outcome(es, src)
            out.append(src)
        return out

    # ─────────────────────────────────────────────────────────────────────────
    # Snapshots (history / view / export) — P4c
    # ─────────────────────────────────────────────────────────────────────────

    @staticmethod
    def list_snapshots(
        *,
        scope=None,
        schedule_id: str | None = None,
        schedule_ids: list | None = None,
        date_from: str | None = None,
        date_to: str | None = None,
        size: int = 100,
    ) -> list[dict]:
        """List snapshots (newest first), scope-filtered like list_schedules.

        Optional filters: a specific ``schedule_id`` (or a ``schedule_ids``
        list, for "all runs of any schedule on this dashboard") and a
        generated-at window (``date_from``/``date_to``, ISO). Snapshots carry
        denormalised org_id/team_id so they remain access-filterable after
        their schedule is deleted (history outlives the schedule).
        """
        es = get_elasticsearch_client()
        if es is None:
            return []

        must: list[dict] = []
        if schedule_id:
            must.append({"term": {"schedule_id": schedule_id}})
        if schedule_ids:
            must.append({"terms": {"schedule_id": [s for s in schedule_ids if s]}})
        if date_from or date_to:
            rng: dict = {}
            if date_from:
                rng["gte"] = date_from
            if date_to:
                rng["lte"] = date_to
            must.append({"range": {"generated_at": rng}})

        if scope is not None:
            org_esids = list(getattr(scope, "org_esids", frozenset()) or [])
            team_esids = list(getattr(scope, "team_esids", frozenset()) or [])
            if not org_esids and not team_esids:
                return []  # no resolvable scope → see nothing
            # v2: snapshots denormalise the single org_id/team_id from their
            # schedule (a snapshot belongs to one run of one team), so the v1
            # single-field match is sufficient here; we also match the *_esids
            # lists in case a future run stamps them.
            should = []
            if org_esids:
                should.append({"terms": {"org_id": org_esids}})
                should.append({"terms": {"org_esids": org_esids}})
            if team_esids:
                should.append({"terms": {"team_id": team_esids}})
                should.append({"terms": {"team_esids": team_esids}})
            must.append({"bool": {"should": should, "minimum_should_match": 1}})

        query = {"bool": {"must": must}} if must else {"match_all": {}}
        try:
            res = es.search(
                index=INDEX_REPORT_SNAPSHOTS,
                body={"query": query, "size": size, "sort": [{"generated_at": "desc"}]},
            )
        except Exception as e:
            logging.error("ReportScheduleService.list_snapshots error: %s", e)
            return []
        out = []
        for hit in res.get("hits", {}).get("hits", []):
            src = hit.get("_source") or {}
            src["snapshot_id"] = hit["_id"]
            out.append(src)
        return out

    @staticmethod
    def get_snapshot(snapshot_id: str) -> dict | None:
        """Return one snapshot doc (with snapshot_id set) or None. Caller applies
        the scope gate via _scope_allows on the doc's org_id/team_id."""
        es = get_elasticsearch_client()
        if es is None:
            return None
        try:
            res = es.get(index=INDEX_REPORT_SNAPSHOTS, id=snapshot_id)
        except Exception:
            return None
        src = res.get("_source") or {}
        src["snapshot_id"] = res["_id"]
        return src

    @staticmethod
    def delete_snapshot(snapshot_id: str) -> dict:
        """Hard-delete a single snapshot doc.

        Removes the ``de_report_snapshots`` doc, which is the sole store of the
        run's precomputed charts/tables AND the source the on-the-fly CSV/Excel
        exports read from — so once it's gone the run no longer appears in
        ``list_snapshots`` and is neither viewable nor exportable, with no
        orphaned export/precompute content left behind (there is no separate
        export doc; exports are generated from this doc at request time).

        Authorization (ownership / scope) is the ROUTE's responsibility — the
        route gate (mirroring _mutate_gate against the parent schedule) runs
        before this. Mirrors delete_schedule's hard-delete + refresh="wait_for"
        so the History list re-read after delete reflects the removal.

        Returns ``{"success": True, "snapshot_id": ...}`` or
        ``{"success": False, "error": ...}`` (snapshot not found / ES error).
        """
        es = get_elasticsearch_client()
        if es is None:
            return {"success": False, "error": "Could not connect to Elasticsearch"}
        try:
            es.delete(index=INDEX_REPORT_SNAPSHOTS, id=snapshot_id, refresh="wait_for")
        except Exception as e:
            # A missing doc surfaces here too (es.delete 404s) — report it as
            # not-found rather than a generic failure so a double-click on the
            # row's Delete reads cleanly.
            msg = (
                "snapshot not found" if "not_found" in str(e).lower() or "404" in str(e) else "Failed to delete the snapshot."
            )
            logging.error("ReportScheduleService.delete_snapshot error: %s", e)
            return {"success": False, "error": msg}
        return {"success": True, "snapshot_id": snapshot_id}

    @staticmethod
    def prune_snapshots(es, schedule_id: str, *, keep: int, record_type: str = "snapshot") -> int:
        """delete the oldest records of ``schedule_id`` and
        ``record_type`` beyond ``keep``.

        Keeps the ``keep`` most-recent records of that kind (by ``generated_at``,
        newest first) and hard-deletes the rest. Report snapshots
        (``record_type="snapshot"``, the default — matches absent/"snapshot" for
        earlier rows) and backfill RUN records (``record_type="backfill"``)
        prune INDEPENDENTLY: each kind honors the schedule's
        ``delivery.snapshot_keep`` on its own count, so a backfill can never evict
        a real report snapshot (or vice versa). Called after each report-snapshot
        write (``_finalize_build``) and after each backfill record write
        (``backfill_now``), so lowering the keep value also drops the now-excess
        older records on the next run of that kind. ``keep`` is assumed already
        clamped by the caller (clamp_snapshot_keep).

        Best-effort: returns the number deleted (0 on any error / nothing to do).
        A prune failure must never fail the run, so the single caller wraps this
        and only logs — but we also swallow ES errors here so a partial delete
        loop still reports its progress.
        """
        if es is None or not schedule_id:
            return 0
        keep = max(SNAPSHOT_KEEP_MIN, int(keep))
        filters = [{"term": {"schedule_id": schedule_id}}]
        if record_type == "backfill":
            filters.append({"term": {"record_type": "backfill"}})
        else:
            # A report snapshot: record_type is "snapshot" OR absent (earlier
            # rows written before the field existed). Exclude only "backfill".
            filters.append({"bool": {"must_not": [{"term": {"record_type": "backfill"}}]}})
        try:
            # Fetch only the ids, newest first, past the keep window. size is
            # bounded so a runaway backlog still drains a chunk per run rather
            # than scanning all at once.
            res = es.search(
                index=INDEX_REPORT_SNAPSHOTS,
                body={
                    "query": {"bool": {"filter": filters}},
                    "sort": [{"generated_at": "desc"}],
                    "_source": False,
                    "from": keep,
                    "size": 1000,
                },
            )
        except Exception as e:  # noqa: BLE001 — prune is best-effort; never fail the run
            logging.warning("ReportScheduleService.prune_snapshots search failed for %s (%s): %s", schedule_id, record_type, e)
            return 0

        stale_ids = [hit["_id"] for hit in res.get("hits", {}).get("hits", [])]
        if not stale_ids:
            return 0

        deleted = 0
        for sid in stale_ids:
            try:
                es.delete(index=INDEX_REPORT_SNAPSHOTS, id=sid, refresh="wait_for")
                deleted += 1
            except Exception as e:  # noqa: BLE001 — keep deleting the rest on a single failure
                logging.warning(
                    "ReportScheduleService.prune_snapshots delete %s failed (schedule %s): %s",
                    sid,
                    schedule_id,
                    e,
                )
        if deleted:
            logging.info(
                "ReportScheduleService.prune_snapshots: schedule %s (%s) kept %d, deleted %d old record(s)",
                schedule_id,
                record_type,
                keep,
                deleted,
            )
        return deleted

    @staticmethod
    def summarize_snapshot_errors(data: dict) -> dict:
        """#7 — collect the per-run errors a snapshot's ``data`` recorded,
        for the History modal's error badge + expandable list.

        Two sources, both already written at run time (no new capture here):
          * per-chart / per-table failures — ``entry["error"]`` on each item in
            ``data.charts`` / ``data.tables`` (set by _precompute_charts when a
            single chart's build/transform/ES call raised);
          * a whole-run ``data.precompute_skipped`` reason (no dashboard_id, the
            dashboard had no charts, ES unavailable) — recorded by
            _build_snapshot_doc.

        not every precompute-skip is an error. Two are CONFIGURATION
        states, not failures (see _is_benign_skip): an import-only schedule with no
        dashboard, and an attached dashboard that has no charts yet. Both mean "the
        run did its job but there was nothing to render" — returned as ``info`` (a
        grey note) and NOT counted toward the red error badge. Genuine problems
        (ES unavailable, a per-chart query/transform failure) still come back as
        ``skipped`` / ``errors`` and count.

        Returns ``{"count": int, "errors": [{"title", "message"}], "skipped": str,
        "info": str}`` — ``count`` is real errors only, ``errors`` carries each
        failing chart/table labeled by its title (falling back to chart_id),
        ``skipped`` is a genuine-problem skip reason or "", and ``info`` is a
        benign note or "". A PURE function so the route can attach it to the
        trimmed history rows and tests can exercise it directly.
        """
        data = data or {}
        errors: list[dict] = []
        for entry in (data.get("charts") or []) + (data.get("tables") or []):
            if not isinstance(entry, dict):
                continue
            msg = entry.get("error")
            if msg:
                errors.append(
                    {
                        "title": entry.get("title") or entry.get("chart_id") or "(untitled chart)",
                        "message": str(msg),
                    }
                )
        raw_skipped = str(data.get("precompute_skipped") or "")
        # A configuration state (no dashboard / empty dashboard) is not a failure —
        # surface it as an info note, not an error. Everything else (ES down, a
        # per-chart failure) is a real problem.
        if raw_skipped and _is_benign_skip(raw_skipped):
            skipped, info = "", raw_skipped
        else:
            skipped, info = raw_skipped, ""
        # count reflects only what the RED badge stands for: per-chart errors plus a
        # genuine-problem skip. A benign import-only skip badges nothing.
        count = len(errors) + (1 if skipped else 0)
        return {"count": count, "errors": errors, "skipped": skipped, "info": info}

    @staticmethod
    def list_snapshots_for_dashboard(dashboard_id: str, *, scope=None, size: int = 50) -> list[dict]:
        """Snapshots whose schedule references ``dashboard_id``, newest first,
        each labeled with its schedule's name.

        The dashboard link lives on the SCHEDULE (snapshots only denormalise
        org/team), so this is a two-step lookup: schedules with the
        dashboard_id → their snapshots. Snapshots of a since-deleted schedule
        are intentionally unreachable here — with the schedule gone there is no
        dashboard link left to follow (they remain in the scheduler's history).

        Returns trimmed rows ({snapshot_id, schedule_id, schedule_name,
        generated_at, date_range}) — the picker only needs a label + the
        window; the heavy ``data`` blob never leaves ES.
        """
        if not dashboard_id:
            return []
        es = get_elasticsearch_client()
        if es is None:
            return []
        try:
            res = es.search(
                index=INDEX_REPORT_SCHEDULES,
                body={
                    # follow-up — match either the v1 scalar or the v2
                    # list, so a multi-dashboard schedule surfaces under EACH of
                    # its dashboards, not just the first.
                    "query": {
                        "bool": {
                            "should": [
                                {"term": {"dashboard_id": dashboard_id}},
                                {"term": {"dashboard_ids": dashboard_id}},
                            ],
                            "minimum_should_match": 1,
                        }
                    },
                    "_source": ["name"],
                    "size": 500,
                },
            )
        except Exception as e:
            logging.error("ReportScheduleService.list_snapshots_for_dashboard error: %s", e)
            return []
        names = {hit["_id"]: (hit.get("_source") or {}).get("name") or "" for hit in res.get("hits", {}).get("hits", [])}
        if not names:
            return []
        snapshots = ReportScheduleService.list_snapshots(scope=scope, schedule_ids=list(names), size=size)
        return [
            {
                "snapshot_id": s.get("snapshot_id"),
                "schedule_id": s.get("schedule_id"),
                "schedule_name": names.get(s.get("schedule_id"), ""),
                "generated_at": s.get("generated_at"),
                "date_range": s.get("date_range") or {},
            }
            for s in snapshots
        ]

    @staticmethod
    def update_schedule(schedule_id: str, *, fields: dict, scope=None) -> dict:
        """Patch mutable fields (name/description/query/delivery/schedule). If
        the cadence (``schedule``) changes, next_run is recomputed.

        Enforces ``scope`` against the *existing* doc's org/team when provided.
        """
        es = get_elasticsearch_client()
        if es is None:
            return {"success": False, "error": "Could not connect to Elasticsearch"}
        existing = ReportScheduleService.get_schedule(schedule_id)
        if not existing:
            return {"success": False, "error": "schedule not found"}
        # a scopeless doc (email schedules pre-picker: org_id/team_id/
        # esids all empty) has nothing to intersect, and _scope_allows("", "")
        # is always False — which blocked a non-privileged CREATOR from editing
        # their own email schedule (ownership is already enforced upstream by the
        # route's _mutate_gate). Skip the existing-doc intersection only when the
        # doc carries no scope at all.
        existing_has_scope = bool(
            existing.get("org_id")
            or ReportScheduleService._schedule_team_esids(existing)
            or ReportScheduleService._schedule_org_esids(existing)
        )
        if (
            scope is not None
            and existing_has_scope
            and not ReportScheduleService._scope_allows(scope, existing.get("org_id", ""), existing.get("team_id", ""))
        ):
            return {"success": False, "error": "not authorized for this schedule"}
        # the check above authorizes against the EXISTING doc only;
        # it never validated the NEW selection, so a crafted PUT could swap
        # team_esids to foreign teams. Gate the incoming selection: every team
        # in the patch must be within the caller's scope (all-match — see
        # _scope_allows_all_teams for why any-match is not a save gate).
        new_teams = [t for t in ((fields or {}).get("team_esids") or []) if t]
        if new_teams and scope is not None and not ReportScheduleService._scope_allows_all_teams(scope, new_teams):
            return {"success": False, "error": "not authorized for the requested team selection"}

        allowed = {
            "name",
            "description",
            "dashboard_id",
            "dashboard_ids",  # follow-up — patched in lockstep with the scalar
            "connector_id",
            "org_esids",
            "team_esids",
            # email edits patch the v1 single team_id in lockstep with
            # team_esids (it back-fills from the first selection at create, and
            # _schedule_team_esids falls back to it when team_esids is empty).
            "team_id",
            "query",
            "delivery",
            "schedule",
        }
        patch = {k: v for k, v in (fields or {}).items() if k in allowed}
        if not patch:
            return {"success": False, "error": "no updatable fields provided"}

        if "schedule" in patch:
            if (
                existing.get("schedule_type") != SCHEDULE_TYPE_EMAIL
                and (patch["schedule"] or {}).get("frequency") in _EMAIL_ONLY_FREQUENCIES
            ):
                return {"success": False, "error": "Quarterly is available for email reports only."}
            try:
                patch["next_run"] = ReportScheduleService.compute_next_run(patch["schedule"]).isoformat()
            except ReportScheduleError as e:
                return {"success": False, "error": e.public_message}
        patch["updated_at"] = _now_iso()
        try:
            es.update(
                index=INDEX_REPORT_SCHEDULES,
                id=schedule_id,
                body={"doc": patch},
                refresh="wait_for",
            )
        except Exception as e:
            logging.error("ReportScheduleService.update_schedule error: %s", e)
            return {"success": False, "error": "Failed to update the schedule."}
        return {"success": True, "schedule_id": schedule_id}

    @staticmethod
    def set_status(schedule_id: str, status: str, *, scope=None) -> dict:
        """Pause/resume. Resuming recomputes next_run so a long-paused schedule
        doesn't immediately fire for every missed window (no backfill storm)."""
        if status not in _VALID_STATUSES:
            return {"success": False, "error": f"status must be one of {_VALID_STATUSES}"}
        es = get_elasticsearch_client()
        if es is None:
            return {"success": False, "error": "Could not connect to Elasticsearch"}
        existing = ReportScheduleService.get_schedule(schedule_id)
        if not existing:
            return {"success": False, "error": "schedule not found"}
        if scope is not None and not ReportScheduleService._scope_allows(
            scope, existing.get("org_id", ""), existing.get("team_id", "")
        ):
            return {"success": False, "error": "not authorized for this schedule"}

        patch = {"status": status, "updated_at": _now_iso()}
        if status == STATUS_ACTIVE:
            try:
                patch["next_run"] = ReportScheduleService.compute_next_run(existing.get("schedule") or {}).isoformat()
            except ReportScheduleError as e:
                return {"success": False, "error": e.public_message}
        try:
            es.update(
                index=INDEX_REPORT_SCHEDULES,
                id=schedule_id,
                body={"doc": patch},
                refresh="wait_for",
            )
        except Exception as e:
            logging.error("ReportScheduleService.set_status error: %s", e)
            return {"success": False, "error": "Failed to update the schedule status."}
        return {"success": True, "schedule_id": schedule_id, "status": status}

    @staticmethod
    def delete_schedule(schedule_id: str, *, scope=None) -> dict:
        """Delete a schedule. Snapshots are intentionally retained (history
        outlives the schedule). Enforces ``scope`` when provided."""
        es = get_elasticsearch_client()
        if es is None:
            return {"success": False, "error": "Could not connect to Elasticsearch"}
        existing = ReportScheduleService.get_schedule(schedule_id)
        if not existing:
            return {"success": False, "error": "schedule not found"}
        if scope is not None and not ReportScheduleService._scope_allows(
            scope, existing.get("org_id", ""), existing.get("team_id", "")
        ):
            return {"success": False, "error": "not authorized for this schedule"}
        try:
            es.delete(index=INDEX_REPORT_SCHEDULES, id=schedule_id, refresh="wait_for")
        except Exception as e:
            logging.error("ReportScheduleService.delete_schedule error: %s", e)
            return {"success": False, "error": "Failed to delete the schedule."}
        return {"success": True, "schedule_id": schedule_id}

    # ─────────────────────────────────────────────────────────────────────────
    # Execution
    # ─────────────────────────────────────────────────────────────────────────

    @staticmethod
    def _resolve_scope_titles(es, schedule: dict) -> list[str]:
        """The team titles of ``_resolve_scope`` (kept for title-only callers)."""
        return ReportScheduleService._resolve_scope(es, schedule)[0]

    @staticmethod
    def _resolve_scope(es, schedule: dict) -> tuple[list[str], list[str]]:
        """Resolve a schedule's stored team/org scope to ``(team_titles,
        team_esids)`` — the titles label and filter the snapshot, the ESIDs pin
        every query to the teams' own ``{team_esid}__{source}`` indices
        (titles are not unique across tenants, so a title filter over
        a wildcard data view also matched a same-titled foreign team).

        Headless equivalent of the session-based scoping in run_query: the cron
        runner has no Flask session, so scope comes from the schedule itself.
          * explicit team(s) → those teams' titles (de_asset_teams);
          * no explicit teams but an org_id → all team titles under org_id
            (RoleFilterService.get_team_titles_by_organization_esids — a
            classmethod that queries de_asset_teams directly, no session).

        resolve ALL of the schedule's teams via _schedule_team_esids
        (v2 ``team_esids`` list, falling back to the v1 single ``team_id``), not
        just the single v1 ``team_id``. A multi-team schedule previously had its
        snapshot scoped to ONE team (the back-filled team_id), so an N-team report
        rendered only one team's data — empty if that team had none in the window
        (staging: a 19-team Echo report resolved to 1 title → empty table).

        Returns the allowed titles and ESIDs (possibly empty → the snapshot
        computes empty rather than unscoped).
        """
        org_id = schedule.get("org_id") or ""
        team_esids = ReportScheduleService._schedule_team_esids(schedule)

        if team_esids:
            try:
                res = es.search(
                    index=INDEX_TEAMS,
                    body={
                        "query": {"ids": {"values": team_esids}},
                        "_source": ["team_title"],
                        "size": len(team_esids),
                    },
                )
                hits = res.get("hits", {}).get("hits", [])
                titles = [h["_source"]["team_title"] for h in hits if (h.get("_source") or {}).get("team_title")]
                return titles, [h["_id"] for h in hits if h.get("_id")]
            except Exception as e:
                logging.error("ReportScheduleService._resolve_scope team lookup: %s", e)
                return [], []

        if org_id:
            rf = _import_first(
                "asset_management.web.services.role_filter_service",
                "web.services.role_filter_service",
                "services.role_filter_service",
            )
            RoleFilterService = rf.RoleFilterService
            try:
                teams = list(RoleFilterService.get_teams_by_organization_esids([org_id]) or [])
            except Exception as e:
                logging.error("ReportScheduleService._resolve_scope org lookup: %s", e)
                return [], []
            return [title for _e, title in teams if title], [esid for esid, _t in teams if esid]
        # no explicit scope on the schedule. Email schedules store no
        # org/team at all (the dialog has no scope fields), which used to fall
        # through to [] here and then to an EMPTY match_phrase_any clause in
        # _precompute_charts — an ES no-op, so the wildcard data view (
        # {team_esid}__{slug}, no workspace prefix) matched EVERY team in the
        # cluster: cross-workspace exposure. Clamp to the creator's team
        # universe instead; a creator whose scope can't be resolved gets []
        # which _precompute_charts now treats as deny-all (empty results),
        # never unfiltered. "the creator's teams" means the set the
        # dialog's org/team tree offered them, NOT what their privilege can
        # reach — see _creator_scope.
        return ReportScheduleService._creator_scope(es, schedule)

    @staticmethod
    def _creator_scope_titles(es, schedule: dict) -> list[str]:
        """The team titles of ``_creator_scope`` (kept for title-only callers)."""
        return ReportScheduleService._creator_scope(es, schedule)[0]

    @staticmethod
    def _creator_scope(es, schedule: dict) -> tuple[list[str], list[str]]:
        """resolve the schedule creator's team titles as
        the set the email dialog's org/team tree offered them.

        Used when the schedule itself stores no org/team scope (email
        schedules). The dialog's copy promises "All your teams are included by
        default", where "your teams" is the ``/api/org-team-tree`` universe:
        ``connector_status_service.list_teams`` over the creator's allowed
        workspace(s), primary-org narrowed for org owners.

        the previous clamp re-derived scope from creator PRIVILEGE
        instead — a system-admin creator expanded via ``match_all`` to every
        team title in the cluster, so the emailed report leaked teams from
        every workspace. The cron path now reproduces the dialog's own
        universe, headlessly (all resolution keyed by ``created_by``; the
        route helpers read the Flask session and must not be called here):

          * privileged creator (system-admin doc / privileged workspace role /
            privileged role name via ES-Security roles) → teams in
            ``WorkspaceMembershipService.primary_and_grant_workspaces`` — the
             strict set ``_effective_allowed_workspaces`` feeds the
            tree for privileged callers;
          * non-privileged creator → teams in the workspaces derived from
            their resolved scope (direct memberships + the inferred org/team →
            workspace walk — ``get_allowed_workspaces``' union, headless),
            narrowed earlier for org-tier creators: keep a team only
            when its PRIMARY org is one of the creator's own ``org_esids``;
          * empty/unresolvable workspace set → [] (deny-all downstream) —
            NEVER match-all, regardless of privilege. A see-all/``None``
            resolution has no equivalent here by construction: both branches
            produce a concrete workspace list, and an empty one denies
            .
        """
        created_by = (schedule.get("created_by") or "").strip()
        if not created_by:
            return [], []
        try:
            rf = _import_first(
                "asset_management.web.services.role_filter_service",
                "web.services.role_filter_service",
                "services.role_filter_service",
            )
            css = _import_first(
                "asset_management.web.services.connector_status_service",
                "web.services.connector_status_service",
                "services.connector_status_service",
            )
            wms = _import_first(
                "asset_management.web.services.workspace_membership_service",
                "web.services.workspace_membership_service",
                "services.workspace_membership_service",
            ).WorkspaceMembershipService
            try:
                cfg = rf.RoleFilterService.load_config() or {}
            except Exception:  # noqa: BLE001 — config only gates role-name privilege checks
                cfg = {}
            roles = ReportScheduleService._creator_roles(es, created_by)
            if rf._user_is_privileged(roles, created_by, cfg):
                # strict set: primary workspace + explicit workspace
                # grants. Empty → deny; a privileged creator with no home
                # workspace emails nothing rather than everything.
                workspaces = list(wms.primary_and_grant_workspaces(created_by) or [])
                own_org_esids: frozenset = frozenset()
            else:
                scope = rf.resolve_user_scope(created_by, roles, cfg)
                direct = set(scope.workspace_esids or ())
                inferred = set(rf._workspace_esids_from_scope_esids(scope.org_esids, scope.team_esids) or ())
                if not scope.team_esids and not scope.org_esids and (scope.team_titles or scope.org_titles):
                    # Legacy pre-ESID cohort — same title fallback
                    # get_allowed_workspaces applies.
                    inferred |= set(rf._workspace_esids_from_scope_titles(scope.org_titles, scope.team_titles) or ())
                workspaces = list(direct | inferred)
                own_org_esids = frozenset(scope.org_esids or ())
            if not workspaces:
                return [], []
            teams = css.list_teams(workspaces) or []
            if own_org_esids:
                # an org owner's tree only offers teams whose
                # PRIMARY org is theirs; a team listing their org secondarily
                # (or none) was never offered, so it must not email either.
                teams = [t for t in teams if t.get("org_esid") in own_org_esids]
            return sorted({t["title"] for t in teams if t.get("title")}), sorted({t["esid"] for t in teams if t.get("esid")})
        except Exception as e:  # noqa: BLE001 — deny-all on any resolution failure, never unfiltered
            logging.error("ReportScheduleService._creator_scope %s: %s", created_by, e)
            return [], []

    @staticmethod
    def _creator_roles(es, username: str) -> list[str]:
        """headless ES-Security role-name lookup for ``username``.

        The session-based privilege check reads role names (``superuser``,
        ``asset_admin``) from ``session["roles"]``, which the login paths seed
        from the ES native realm. The cron runner has no session, so read the
        same realm directly. Fail-soft ``[]``: the username-keyed privilege
        legs (system-admin doc, workspace role assignment) still apply, and a
        missed role name can only NARROW the resolved universe, never widen it.
        """
        try:
            doc = dict(es.security.get_user(username=username)).get(username) or {}
            return [r for r in (doc.get("roles") or []) if r]
        except Exception:  # noqa: BLE001 — roles are an input to a fail-closed resolution
            return []

    @staticmethod
    def _load_dashboard_source(es, dashboard_id: str) -> dict:
        """Return the full de_asset_echarts_dashboards ``_source`` doc, or {}.

        The precompute path needs more than the charts list: the ``publishes[]``
        entries live at the top level of the
        source, not inside ``echarts_config``.
        """
        if not dashboard_id:
            return {}
        try:
            res = es.get(index=INDEX_DASHBOARDS, id=dashboard_id)
        except Exception as e:
            logging.warning(
                "ReportScheduleService._load_dashboard_source: %s not found: %s",
                dashboard_id,
                e,
            )
            return {}
        return res.get("_source") or {}

    @staticmethod
    def _load_dashboard_charts(es, dashboard_id: str) -> list[dict]:
        """Return the chart configs from a de_asset_dashboards doc, or []."""
        src = ReportScheduleService._load_dashboard_source(es, dashboard_id)
        charts = (src.get("echarts_config") or {}).get("charts") or []
        return charts if isinstance(charts, list) else []

    @staticmethod
    def _resolve_report_link(
        schedule: dict,
        dashboard_source: dict,
        scope_titles: list[str],
        date_range: dict | None = None,
    ) -> tuple[str, str]:
        """resolve the out-link for an Impact Report chart.

        Impact Report charts can't be precomputed headlessly (see
        IMPACT_REPORT_CHART_TYPES), so the snapshot/email links to the live
        report instead. Priority:

          1. A live (enabled, unexpired) publish entry whose scope matches the
             schedule's resolved scope → the public ``/public/report/<token>``
             viewer (no login). Only a SINGLE-scope schedule (exactly one team,
             or one org with no team) can match a publish entry, which is keyed
             by a single (team_title, org_title) tuple.
          2. Otherwise → the login-gated in-app dashboard viewer, which always
             works for an authorized recipient (the guaranteed fallback — no
             auto-publish).

        Returns ``(relative_path, kind)`` where ``kind`` is ``"public"`` or
        ``"dashboard"``. The path is app-relative (carries the /assets prefix);
        the email service prepends the absolute base via _get_app_base_url.
        Empty path only if there is no dashboard_id.

        ``date_range`` (the schedule's resolved window) is appended
        to the DASHBOARD link as ``?time_from&time_to``: view.html reads those
        params into ``window.DASHBOARD_TIME_RANGE``, which the report renderer
        uses for both its queries and its date label. Opened bare (no params)
        the report fell back to All Time, ignoring the schedule's window. The
        PUBLIC link is deliberately left alone — a published report's window is
        the publisher's choice, and the public viewer doesn't read
        URL time params.
        """
        dashboard_id = schedule.get("dashboard_id") or ""

        # Try a scope-matching public token first (single-scope only).
        token = ""
        if dashboard_source and len(scope_titles) == 1:
            try:
                db = _import_first(
                    "asset_management.web.blueprints.dashboard_builder.routes",
                    "web.blueprints.dashboard_builder.routes",
                    "blueprints.dashboard_builder.routes",
                )
                # A schedule scope title is a team title (the common case) — try
                # it as team first, then as org, mirroring _publish_scope_key's
                # team-tighter-than-org precedence.
                title = scope_titles[0]
                for scope_key in ((title, ""), ("", title)):
                    entry = db._find_publish_entry(dashboard_source, scope_key)
                    if entry and db._entry_is_active(entry):
                        # stored hashed + encrypted; decrypt for the link.
                        token = db._entry_token(entry) or ""
                        break
            except Exception as e:  # noqa: BLE001 — link is non-critical; fall back to dashboard URL
                logging.warning("ReportScheduleService._resolve_report_link publish lookup: %s", e)

        if token:
            return _PUBLIC_REPORT_PATH.format(token=token), "public"
        if dashboard_id:
            path = _DASHBOARD_VIEW_PATH.format(dashboard_id=dashboard_id)
            params: list[tuple[str, str]] = []
            # carry the schedule's resolved window on the link.
            dr_from = (date_range or {}).get("from") or ""
            dr_to = (date_range or {}).get("to") or ""
            if dr_from and dr_to:
                params.append(("time_from", dr_from))
                params.append(("time_to", dr_to))
            # carry the schedule's EXPLICIT team scope on the link.
            # view.html reads repeated team_filter params into
            # DASHBOARD_EXTRA_FILTERS (match_phrase_any on team_title), same as
            # the parent dashboards page appends. Narrow-only: the dashboard
            # APIs still enforce the VIEWER's session scope server-side, so a
            # tampered URL can't widen access. Only when the schedule carries an
            # explicit selection — a creator-clamp ("all my teams") schedule
            # appends nothing and the viewer's own scope governs, rather than
            # bloating the URL with every title.
            explicit_scope = bool(ReportScheduleService._schedule_team_esids(schedule) or (schedule.get("org_id") or ""))
            if explicit_scope:
                for title in scope_titles or []:
                    params.append(("team_filter", title))
            if params:
                path += "?" + urllib.parse.urlencode(params)
            return path, "dashboard"
        return "", "dashboard"

    @staticmethod
    def _field_types_for(es, data_view: str, cache: dict) -> dict:
        """{field_name: es_type} for a data view, from the live
        mapping, cached in ``cache`` keyed by data_view. Mirrors the request
        path's ``_get_field_types`` (dashboard_builder.routes) but kept
        self-contained so the headless runner doesn't import the Flask
        blueprint. Includes ``.keyword`` sub-fields so build_query can tell a
        present sub-field from a redundant suffix. Fail-soft: {} on any error
        (build_query then leaves field names unchanged — the prior behavior)."""
        if not data_view:
            return {}
        if data_view in cache:
            return cache[data_view]
        out: dict = {}
        try:
            mapping = es.indices.get_mapping(index=data_view)
            for idx_data in (mapping.body if hasattr(mapping, "body") else mapping).values():
                props = (idx_data.get("mappings") or {}).get("properties") or {}
                for name, spec in props.items():
                    out.setdefault(name, spec.get("type"))
                    for sub, sub_spec in (spec.get("fields") or {}).items():
                        out.setdefault(f"{name}.{sub}", sub_spec.get("type"))
        except Exception as e:
            logging.warning("ReportScheduleService._field_types_for %s: %s", data_view, e)
            out = {}
        cache[data_view] = out
        return out

    @staticmethod
    def _precompute_charts(
        es,
        charts: list[dict],
        date_range: dict,
        scope_titles: list[str],
        *,
        scope_team_esids: list[str],
        schedule: dict | None = None,
        dashboard_source: dict | None = None,
    ) -> dict:
        """Execute each chart against ES with the schedule's date range + team
        scope and bucket the results.

        Reuses the dashboard renderer's own ``build_query`` + ``transform`` so a
        stored snapshot is byte-identical to a live render. Scope is enforced by
        appending the SAME allow-only team_title clause append_scope_filter adds
        in the request path (RoleFilterService.append_scope_filter) — here
        sourced from the schedule rather than the session.

        Impact Report charts (report / google_report / combined_report)
        can't be computed by build_query (they're client-side-rendered and carry
        none of the fields build_query needs; feeding one in produces a malformed
        terms/VALUE_NULL query ES rejects 400). Such charts are SKIPPED here and
        stamped with ``report_link`` + ``report_link_kind`` so the snapshot viewer
        and email link out to the live report instead of freezing an ES error.

        Returns ``{"charts": [...], "tables": [...], "summary_metrics": {...}}``.
        Per-chart try/except: a single bad chart is recorded with an error and
        does not abort the batch.
        """
        schedule = schedule or {}
        dashboard_source = dashboard_source or {}
        qb = _import_first(
            "asset_management.web.services.es_query_builder",
            "web.services.es_query_builder",
            "services.es_query_builder",
        )
        et = _import_first(
            "asset_management.web.services.echarts_transformer",
            "web.services.echarts_transformer",
            "services.echarts_transformer",
        )
        build_query = qb.build_query
        transform = et.transform

        import copy

        out_charts: list[dict] = []
        out_tables: list[dict] = []
        summary: dict = {}
        # per-data-view {field: es_type} cache. The request path
        # (_execute_chart_query) enriches each chart with the live mapping types
        # so build_query can resolve a stored ``x_field`` that carries a
        # ``.keyword`` suffix against a field that's actually bare ``keyword``
        # (e.g. YouTube's youtube_channel_handle) — without it a table/chart
        # aggregates on a non-existent sub-field and renders ZERO rows. The
        # headless snapshot runner had no equivalent, so its snapshots came out
        # empty even when the live dashboard rendered fine. Mirror it here.
        field_types_cache: dict = {}

        for chart in charts or []:
            chart_id = chart.get("chart_id")
            chart_type = chart.get("chart_type", "")
            entry = {
                "chart_id": chart_id,
                "chart_type": chart_type,
                "title": chart.get("title", ""),
            }
            # Impact Report charts can't be precomputed headlessly.
            # Skip build_query entirely and stamp an out-link to the live report
            # so the snapshot/email links out instead of freezing an ES 400.
            if chart_type in IMPACT_REPORT_CHART_TYPES:
                link, kind = ReportScheduleService._resolve_report_link(
                    schedule, dashboard_source, scope_titles, date_range=date_range
                )
                entry["report_link"] = link
                entry["report_link_kind"] = kind
                out_charts.append(entry)
                continue
            # deny-by-default. An empty scope must compute EMPTY, never
            # unfiltered: an empty match_phrase_any value list is an ES no-op, and
            # the wildcard data views span every team in the cluster, so querying
            # with no scope clause leaked cross-workspace data. The empty result
            # renders as the snapshot viewer's neutral "No data in scope" note.
            if not scope_titles or not scope_team_esids:
                entry["result"] = {}
                (out_tables if chart_type in _TABLE_CHART_TYPES else out_charts).append(entry)
                continue
            try:
                config = copy.deepcopy(chart)
                # Override the chart's window with the schedule's resolved range,
                # preserving its date field. Charts with no date_range/field stay
                # un-time-filtered (some metric tiles are all-time).
                dr = config.get("date_range")
                if isinstance(dr, dict) and dr.get("field"):
                    dr["from"] = date_range["from"]
                    dr["to"] = date_range["to"]
                # Append the allow-only team scope clause (identical shape to
                # RoleFilterService.append_scope_filter). Sources isolation from
                # the schedule's team scope since there is no session.
                filters = config.get("filters")
                if not isinstance(filters, list):
                    filters = []
                    config["filters"] = filters
                filters.append({"field": "team_title", "op": "match_phrase_any", "value": list(scope_titles)})

                # Promote a bare per-source slug (e.g. "chatwoot_insights") to the
                # wildcard pattern that matches the per-team data indices
                # ("*_chatwoot_insights"). A bare slug names no real index/alias, so
                # the search below matched nothing and the snapshot came out all-zero
                # for every team (the hand-seeded chatwoot dashboard hit this). See
                # _normalize_data_view_for_search.
                data_view = _normalize_data_view_for_search(config.get("data_view", ""))
                # pin the search to the scope teams' own indices. The
                # title clause above is not enough on its own: titles repeat
                # across tenants, and the data view is a cluster-wide wildcard.
                search_index = _scoped_index_pattern(data_view, _team_indices(scope_team_esids))
                # enrich the chart with live mapping field-types so
                # build_query resolves a redundant ``.keyword`` suffix to the
                # field that exists (parity with _execute_chart_query). Cached
                # per data_view so N charts over one view cost one get_mapping.
                ftypes = ReportScheduleService._field_types_for(es, data_view, field_types_cache)
                if ftypes:
                    config["field_types"] = ftypes
                query_body = build_query(config)
                es_response = es.search(index=search_index, body=query_body, ignore_unavailable=True)
                es_dict = es_response.body if hasattr(es_response, "body") else dict(es_response)
                result = transform(es_dict, config)
                # relabel raw field-name headers to the dashboard's
                # human column labels for table results (the live render does
                # this client-side; the snapshot must do it server-side).
                if chart_type in _TABLE_CHART_TYPES:
                    _relabel_table_columns(result, config)
                    # bake the optional grand-totals row into the
                    # stored rows so the snapshot viewer (minimal-config
                    # renderTable, no client recompute) and CSV/Excel exports
                    # all show it. table-only: _append_totals_row no-ops on
                    # posts_table (chart_type guard) and when the box is off.
                    _append_totals_row(result, config)
                entry["result"] = result

                if chart_type in _TABLE_CHART_TYPES:
                    out_tables.append(entry)
                else:
                    out_charts.append(entry)
                    if chart_type == _METRIC_CHART_TYPE and isinstance(result, dict):
                        # Collect KPI tiles into a flat summary for email/header use.
                        label = result.get("label") or chart.get("title") or chart_id
                        summary[label] = result.get("value")
            except Exception as e:
                logging.error("ReportScheduleService._precompute_charts: chart %s failed: %s", chart_id, e)
                # stored, then served by the History modal; detail stays in the log.
                entry["error"] = "This chart could not be computed."
                (out_tables if chart_type in _TABLE_CHART_TYPES else out_charts).append(entry)

        return {"charts": out_charts, "tables": out_tables, "summary_metrics": summary}

    @staticmethod
    def _build_snapshot_doc(schedule: dict, *, now: datetime | None = None, es=None) -> dict:
        """Build the snapshot payload for one schedule.

        P2: when the schedule carries a ``dashboard_id``, execute that
        dashboard's charts (scoped to the schedule's team(s) + resolved window)
        and store the real per-chart results. With no dashboard_id, fall back to
        the P1 stub (empty results + ``precompute_skipped`` reason) — backward
        safe with P1-era schedules. ``es`` is passed by the caller (the runner
        already holds a client); fetched lazily if absent.
        """
        now = now or _now()
        # the snapshot covers the same window the run pulled, derived
        # from frequency for import schedules (email schedules use their stored
        # date_range_mode). resolve_window_for_schedule encapsulates that choice.
        date_range = ReportScheduleService.resolve_window_for_schedule(schedule, now=now)

        # follow-up — a schedule may carry SEVERAL dashboards; each is
        # precomputed independently and their charts/tables concatenated into
        # one snapshot (one email, a link per dashboard). ``data.dashboard_id``
        # stays the first for v1 readers; ``data.dashboard_ids`` lists them all.
        dashboard_ids = ReportScheduleService._schedule_dashboard_ids(schedule)
        data = {
            "query": schedule.get("query") or {},
            "dashboard_id": dashboard_ids[0] if dashboard_ids else "",
            "dashboard_ids": list(dashboard_ids),
            "date_range": date_range,
            "summary_metrics": {},
            "charts": [],
            "tables": [],
        }

        rt = _import_first(
            "asset_management.web.services.report_types_service",
            "web.services.report_types_service",
            "services.report_types_service",
        )
        if not dashboard_ids:
            data["precompute_skipped"] = SKIP_NO_DASHBOARD
        else:
            es = es or get_elasticsearch_client()
            if es is None:
                data["precompute_skipped"] = "elasticsearch unavailable"
            else:
                scope_titles, scope_team_esids = ReportScheduleService._resolve_scope(es, schedule)
                skipped: list[str] = []
                # follow-up — one link per dashboard, in check order.
                # The email renders these directly (see report_email_service.
                # _dashboard_links_html); ``report:*`` types have no dashboard
                # page to link to, so they contribute tables only.
                dashboard_links: list[dict] = []
                for dashboard_id in dashboard_ids:
                    if rt.is_report_type(dashboard_id):
                        # a synthetic ``report:*`` id is a BUILT-IN
                        # report type (Backfill / Import errors), not an ECharts
                        # dashboard. Computed headlessly, scoped by the same
                        # resolved team titles the chart precompute uses, and
                        # stored in the standard tables shape so the
                        # email/export/View pipeline is unchanged.
                        computed = rt.build_report_data(
                            es,
                            {**schedule, "dashboard_id": dashboard_id},
                            scope_titles,
                            date_range,
                            scope_team_esids=scope_team_esids,
                        )
                        data["tables"].extend(computed.get("tables") or [])
                        data["summary_metrics"].update(computed.get("summary_metrics") or {})
                        data.setdefault("report_types", []).append(dashboard_id)
                        if len(dashboard_ids) == 1:
                            data["report_type"] = dashboard_id  # v1 shape
                        if computed.get("precompute_skipped"):
                            skipped.append(computed["precompute_skipped"])
                        continue
                    dashboard_source = ReportScheduleService._load_dashboard_source(es, dashboard_id)
                    link, kind = ReportScheduleService._resolve_report_link(
                        {**schedule, "dashboard_id": dashboard_id},
                        dashboard_source,
                        scope_titles,
                        date_range=date_range,
                    )
                    if link:
                        dashboard_links.append(
                            {
                                "dashboard_id": dashboard_id,
                                "title": dashboard_source.get("title") or "Dashboard",
                                "link": link,
                                "kind": kind,
                            }
                        )
                    charts = (dashboard_source.get("echarts_config") or {}).get("charts") or []
                    if not isinstance(charts, list) or not charts:
                        skipped.append(f"dashboard {dashboard_id} {SKIP_NO_CHARTS_SUFFIX}")
                        continue
                    computed = ReportScheduleService._precompute_charts(
                        es,
                        charts,
                        date_range,
                        scope_titles,
                        scope_team_esids=scope_team_esids,
                        schedule=schedule,
                        dashboard_source=dashboard_source,
                    )
                    data["charts"].extend(computed["charts"])
                    data["tables"].extend(computed["tables"])
                    data["summary_metrics"].update(computed["summary_metrics"])
                # Only a WHOLLY empty precompute is a skip — if any dashboard
                # produced content the snapshot is real, and a per-dashboard
                # failure must not blank the run.
                if skipped and not data["charts"] and not data["tables"]:
                    data["precompute_skipped"] = "; ".join(skipped)
                # Only stamped for a genuinely MULTI-dashboard schedule: with one
                # dashboard the existing single "View the full report" link is
                # already correct, and emitting both would double the link.
                if len(dashboard_links) > 1:
                    data["dashboard_links"] = dashboard_links

        size_bytes = len(json.dumps(data).encode("utf-8"))
        return {
            "schedule_id": schedule.get("schedule_id"),
            "org_id": schedule.get("org_id"),
            "team_id": schedule.get("team_id"),
            "generated_at": now.isoformat(),
            "date_range": date_range,
            "data": data,
            "format_version": SNAPSHOT_FORMAT_VERSION,
            "size_bytes": size_bytes,
        }

    # ─────────────────────────────────────────────────────────────────────────
    # Scoped import
    # ─────────────────────────────────────────────────────────────────────────

    @staticmethod
    def _resolve_team_to_google_ads_customer_id(es, team_esid: str) -> str | None:
        """team_esid → its google_ads_customer_id (de_asset_teams
        .google_ads_customer_esid → de_asset_google_ads_customers
        .google_ads_customer_id). Mirrors analytics.routes. None on miss."""
        if not es or not team_esid:
            return None
        try:
            team = es.get(index=INDEX_TEAMS, id=team_esid, ignore=[404])
        except Exception:
            return None
        if not team or not team.get("found"):
            return None
        cust_esid = str((team.get("_source") or {}).get("google_ads_customer_esid") or "").strip()
        if not cust_esid:
            return None
        try:
            cust = es.get(index="de_asset_google_ads_customers", id=cust_esid, ignore=[404])
        except Exception:
            return None
        if not cust or not cust.get("found"):
            return None
        return str((cust.get("_source") or {}).get("google_ads_customer_id") or "").strip() or None

    @staticmethod
    def _resolve_team_to_google_ads_customer_ids(es, team_esid: str) -> list[str]:
        """team_esid → ALL its google_ads_customer_ids (multi-account aware).

         follow-up — a multi-account team stores its
        customer(s) in the PLURAL ``team_google_ads_customer_esids`` array and
        may leave the legacy scalar ``google_ads_customer_esid`` empty (observed
        on "Kavanah Test"). The singular
        :meth:`_resolve_team_to_google_ads_customer_id` reads only the scalar, so
        it resolved such a team to nothing — the Import Data page reported "no
        Google Ads account linked" and the Report Scheduler imported zero/one
        customer for a multi-account team.

        Union the plural array with the legacy scalar (older single-account teams
        set both to the same value), resolve each customer-esid to its
        ``google_ads_customer_id`` via de_asset_google_ads_customers, dedup
        preserving order. Mirrors GA4's already-plural
        :meth:`_resolve_team_to_ga_property_ids`. Empty list on miss/error.
        """
        if not es or not team_esid:
            return []
        try:
            team = es.get(index=INDEX_TEAMS, id=team_esid, ignore=[404])
        except Exception:
            return []
        if not team or not team.get("found"):
            return []
        src = team.get("_source") or {}
        cust_esids = list(src.get("team_google_ads_customer_esids") or [])
        legacy = str(src.get("google_ads_customer_esid") or "").strip()
        if legacy:
            cust_esids.append(legacy)

        out: list[str] = []
        seen: set[str] = set()
        for cust_esid in [str(e).strip() for e in cust_esids if str(e).strip()]:
            if cust_esid in seen:
                continue
            seen.add(cust_esid)
            try:
                cust = es.get(index="de_asset_google_ads_customers", id=cust_esid, ignore=[404])
            except Exception:
                continue
            if cust and cust.get("found"):
                cid = str((cust.get("_source") or {}).get("google_ads_customer_id") or "").strip()
                if cid and cid not in out:
                    out.append(cid)
        return out

    @staticmethod
    def _resolve_team_to_ga_property_ids(es, team_esid: str) -> list[str]:
        """team_esid → its GA4 property_ids (team_ga4_property_esids[] →
        de_asset_ga_properties.ga_property_id). Mirrors analytics.routes."""
        if not es or not team_esid:
            return []
        try:
            team = es.get(index=INDEX_TEAMS, id=team_esid, ignore=[404])
        except Exception:
            return []
        if not team or not team.get("found"):
            return []
        esids = (team.get("_source") or {}).get("team_ga4_property_esids") or []
        if not isinstance(esids, list):
            return []
        out = []
        for esid in [str(e).strip() for e in esids if str(e).strip()]:
            try:
                p = es.get(index="de_asset_ga_properties", id=esid, ignore=[404])
            except Exception:
                continue
            if p and p.get("found"):
                pid = str((p.get("_source") or {}).get("ga_property_id") or "").strip()
                if pid:
                    out.append(pid)
        return out

    # ── Echo per-tenant uniqueness ───────────────────────────────

    @staticmethod
    def _resolve_team_to_echo_tenant(es, team_esid: str) -> tuple[str, str, str]:
        """team_esid → (workspace_esid, echo_tenant_esid, echo_tenant_title) from
        de_asset_teams. Any missing field comes back "" so the caller can decide
        how to treat a team with no Echo tenant (a team with "" tenant never keys
        a conflict — see _echo_tenant_pairs). Empty triple on a miss/ES error.

        Echo's tenant lives directly on the team (echo_tenant_esid /
        echo_tenant_title, set in handlers/teams.py), unlike google_ads/ga4 whose
        resolvers hop through an asset doc — so this is a single team read."""
        empty = ("", "", "")
        if not es or not team_esid:
            return empty
        try:
            team = es.get(index=INDEX_TEAMS, id=team_esid, ignore=[404])
        except Exception:
            return empty
        if not team or not team.get("found"):
            return empty
        src = team.get("_source") or {}
        return (
            str(src.get("workspace_esid") or "").strip(),
            str(src.get("echo_tenant_esid") or "").strip(),
            str(src.get("echo_tenant_title") or "").strip(),
        )

    @staticmethod
    def _echo_tenant_pairs(es, team_esids: list) -> dict:
        """Resolve a schedule's teams to the set of (workspace_esid,
        echo_tenant_esid) pairs that key the uniqueness constraint,
        mapped to a representative tenant title for the conflict message.

        A team with no echo_tenant_esid (or no workspace_esid) contributes
        nothing — it can't key a tenant conflict. Returns {(ws, tenant): title}."""
        pairs: dict = {}
        for t in [t for t in (team_esids or []) if t]:
            ws, tenant, title = ReportScheduleService._resolve_team_to_echo_tenant(es, t)
            if ws and tenant:
                pairs.setdefault((ws, tenant), title or tenant)
        return pairs

    @staticmethod
    def echo_tenant_conflict(team_esids: list, *, exclude_schedule_id: str = "") -> dict | None:
        """at most one Echo report per (workspace, echo_tenant) pair.

        Resolve ``team_esids`` to their (workspace_esid, echo_tenant_esid) pairs
        and check whether any OTHER active/paused Echo schedule already covers the
        same pair. ``exclude_schedule_id`` skips the schedule being edited so a
        no-op save of an existing Echo schedule doesn't self-conflict.

        Returns ``None`` when there's no conflict (or the teams resolve to no Echo
        tenant at all — nothing to de-conflict), else a dict
        ``{"tenant_esid", "tenant_title", "workspace_esid", "schedule_id"}`` for
        the route to build a tenant-named 409 message. Fail-soft: an ES error
        resolving the comparison set returns ``None`` (don't block a save on an
        infra blip — the batch-import dedup in run_due remains as defense in depth
        per the ticket)."""
        es = get_elasticsearch_client()
        if es is None:
            return None
        candidate = ReportScheduleService._echo_tenant_pairs(es, team_esids)
        if not candidate:
            return None  # no resolvable Echo tenant → nothing to de-conflict

        # Only active/paused Echo schedules contend for a tenant slot; a deleted
        # schedule is gone and there's no other terminal status in this store.
        must = [
            {"term": {"connector_id": "echo"}},
            {
                "bool": {
                    "should": [{"term": {"status": STATUS_ACTIVE}}, {"term": {"status": STATUS_PAUSED}}],
                    "minimum_should_match": 1,
                }
            },
        ]
        try:
            res = es.search(
                index=INDEX_REPORT_SCHEDULES,
                body={"query": {"bool": {"must": must}}, "size": 500},
            )
        except Exception as e:
            logging.warning("ReportScheduleService.echo_tenant_conflict: %s", e)
            return None

        for hit in res.get("hits", {}).get("hits", []):
            sid = hit["_id"]
            if exclude_schedule_id and sid == exclude_schedule_id:
                continue
            src = hit.get("_source") or {}
            existing_teams = ReportScheduleService._schedule_team_esids(src)
            existing_pairs = ReportScheduleService._echo_tenant_pairs(es, existing_teams)
            for pair in candidate:
                if pair in existing_pairs:
                    ws, tenant = pair
                    return {
                        "tenant_esid": tenant,
                        "tenant_title": candidate[pair],
                        "workspace_esid": ws,
                        "schedule_id": sid,
                    }
        return None

    @staticmethod
    def _resolve_scope_values(es, connector_id: str, team_esids: list) -> list[str]:
        """Resolve the schedule's team_esids to the values the connector's import
        scope flag expects: team_esids directly (meta/youtube/echo), customer_ids
        (google_ads), or property_ids (GA4). Returns the flag values (may be empty
        → caller skips dispatch)."""
        spec = _CONNECTOR_IMPORT_DISPATCH.get(connector_id) or {}
        resolver = spec.get("resolver")
        if resolver is None:
            return [t for t in team_esids if t]
        if resolver == "google_ads":
            # follow-up — multi-account aware: a team may carry several
            # google_ads customers (plural team_google_ads_customer_esids). The
            # old singular resolver imported only one/zero for such teams.
            vals = []
            for t in team_esids:
                vals.extend(ReportScheduleService._resolve_team_to_google_ads_customer_ids(es, t))
            return vals
        if resolver == "ga4":
            vals = []
            for t in team_esids:
                vals.extend(ReportScheduleService._resolve_team_to_ga_property_ids(es, t))
            return vals
        return []

    @staticmethod
    def _trigger_scoped_import(
        es,
        connector_id: str,
        team_esids: list,
        date_range: dict,
        *,
        progress_run_id: str | None = None,
        overwrite: bool = False,
        backfill_bailout: bool = False,
        is_backfill: bool = False,
    ) -> dict:
        """Fire-and-forget a team-scoped import for the connector over the resolved
        window, via import.py (session-free, lock-guarded, idempotent).

        Returns {dispatched: bool, source, skipped_reason, pids}. Best-effort: any
        error is caught + returned as skipped, never raised (the run proceeds).
        Unknown connectors (absent from _CONNECTOR_IMPORT_DISPATCH) are skipped.

        each dispatch is launched with ``start_new_session=True`` so the
        spawned ``python3 import.py`` is the leader of a NEW process group whose
        descendants (run_daily_imports → get_campaign_data / get_conversations,
        which use plain subprocess.run and stay in the group) all share its pgid.
        That lets a later Cancel reach the whole subtree with one ``os.killpg`` —
        mirroring ImportService.cancel_import's model. The child PID (==
        pgid for a session leader) is captured into ``pids`` so cancel_run can find
        it. Windows dev has no setsid; ``start_new_session`` is silently ignored
        there (os.killpg also absent → cancel falls back to the flag).

        when ``progress_run_id`` is set, pass it to the importer via
        ``--progress_run_id`` so the importer writes live per-stage / per-day
        progress to ``de_import_progress`` (the doc was minted by
        :meth:`_start_progress_run`). The importer adopts the run_id and skips its
        own start_run. Only FB wires this today; the flag is harmless to others
        (they ignore an unknown kwarg), but we only pass it for sources that
        actually honor it (see _execute_one).
        """
        spec = _CONNECTOR_IMPORT_DISPATCH.get(connector_id)
        if not spec:
            logging.info(
                "ReportScheduleService._trigger_scoped_import: connector %s has no scoped import (skipped)",
                connector_id,
            )
            return {"dispatched": False, "source": "", "skipped_reason": "connector not team-scopable", "pids": []}

        if spec.get("scope_flag") is None:
            # Phase 2 — config-scoped connector (bitly): scope comes
            # from the importer's own config file, so there are no scope values
            # to resolve and no scope flag to pass. One dispatch.
            values: list[str] = []
            value_batches: list[list[str]] = [[]]
        else:
            values = ReportScheduleService._resolve_scope_values(es, connector_id, team_esids)
            if not values:
                return {
                    "dispatched": False,
                    "source": spec["source"],
                    "skipped_reason": "no scope values resolved",
                    "pids": [],
                }
            # multi connectors comma-join into ONE dispatch (echo included);
            # a non-multi connector would dispatch once per value.
            value_batches = [[v] for v in values] if not spec.get("multi") else [values]

        if spec.get("window_flag") == "days":
            # Phase 2 — trailing-window importer (bitly takes --days,
            # not start/end dates). Trailing means "from now", so derive the
            # count from the resolved window's FROM date to today (inclusive);
            # an unresolvable window omits the flag → the importer's default.
            days = ReportScheduleService._trailing_days_from(date_range)
            date_args = [f"--days={days}"] if days else []
        else:
            date_args = [f"--start_date={date_range.get('from', '')}", f"--end_date={date_range.get('to', '')}"]
        # only thread the progress run_id when there's a SINGLE dispatch
        # (multi connectors like FB). A per-value fan-out would have N importers
        # racing one progress doc; we don't wire progress for those yet.
        pass_run_id = progress_run_id if (progress_run_id and len(value_batches) == 1) else None
        dispatched = 0
        pids: list[dict] = []
        for batch in value_batches:
            scope_args = [f"--{spec['scope_flag']}={','.join(batch)}"] if spec.get("scope_flag") else []
            argv = [
                "python3",
                _IMPORT_PY,
                spec["source"],
                "run_daily_imports",
                *scope_args,
                *date_args,
            ]
            # the scheduler is the automated recurring pull that owns
            # "keep recent data current", so it opts the FB ad importer into the
            # attribution-maturation tail (re-pull the still-settling last ~8 days
            # so conversion counts mature). The flag is FB-only: other connectors
            # have no post-impression attribution window. Manual paths (Import Data
            # page, wizard) never set it, so they pull their exact requested range.
            if spec["source"] == "facebook":
                # the maturation tail force-re-writes the last ~8 days so
                # still-settling conversions mature. That's the RECURRING pull's job;
                # a backfill honors ONLY the user's overwrite choice (its
                # window is historical + user-controlled), so we suppress the tail on
                # the backfill path. Scheduled runs (is_backfill False) keep it.
                if not is_backfill:
                    argv.append("--maturation_tail=true")
                # backfill overwrite + rate-limit bail-out are FB-only.
                # ``overwrite`` maps to the existing --force_refetch re-write path
                # (skip-existing stays the default when off); ``backfill_bailout``
                # makes a Meta rate limit stop gracefully and keep partial data.
                # Other connectors have no force/bailout path (verified: only the FB
                # importer honors these flags), so we append them for facebook only.
                if overwrite:
                    argv.append("--force_refetch=true")
                if backfill_bailout:
                    argv.append("--backfill_bailout=true")
            # a scheduled GA4 run resolves to a single day (rolling_1d)
            # and the importer's window step skips single-day ranges, so a plain
            # daily schedule never wrote a *_ga_metrics_window doc (4 of 11 GA
            # teams had one on prod; the GA4 first-look charts read that index).
            # Opt the scheduled run into the earlier rolling last-7 /
            # last-30 dedup-window upkeep. Scheduled runs only: a backfill's window
            # is historical and user-controlled, and the manual paths (Import Data
            # page / wizard) never come through here.
            if spec["source"] == "google_analytics" and not is_backfill:
                argv.append("--rolling_windows=true")
            if pass_run_id:
                argv.append(f"--progress_run_id={pass_run_id}")
            try:
                proc = subprocess.Popen(
                    argv,
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                    close_fds=True,
                    # own process group so Cancel can killpg the whole
                    # subtree. No-op on Windows dev (prod path is the Linux container).
                    start_new_session=True,
                )
                dispatched += 1
                # For a session leader the pid IS the pgid; record both so cancel_run
                # can os.killpg(pgid) even if the leader exec'd into a child.
                pids.append({"pid": proc.pid, "pgid": proc.pid})
            except Exception as e:  # noqa: BLE001 — dispatch is best-effort
                logging.error("ReportScheduleService._trigger_scoped_import Popen failed (%s): %s", argv, e)
        # the lock scope key the importer will compute on its
        # side, for the connectors that take a PER-SCOPE lock. It must match the
        # importer's ImportLockService.acquire scope_id EXACTLY so a later Cancel can
        # force_release the right per-scope doc. Connectors on the connector-GLOBAL
        # lock report "" here and cancel force_release falls back to the
        # connector-global key, unchanged.
        #   * google_ads / ga4: ``values`` ARE the resource ids → scope_key_for_ids.
        # * echo: the lock is PER-TENANT, and ``values`` are TEAM esids,
        #     so resolve teams → tenant set first (echo_tenant_scope_key), exactly as
        #     the importer's lock site does — the same function both sides, no drift.
        # * facebook: the lock is PER-CONNECTION and ``values`` are TEAM
        #     esids → meta_connection_scope_key, the importer's exact lock-site
        #     function (legacy/mixed cohorts resolve to "" = connector-global). Meta
        #     is multi:True so ``values`` equals the one batch's --team_ids verbatim.
        # * youtube: ``values`` are TEAM esids and the importer keys its
        #     lock on scope_key_for_ids(--team_ids) — the generic branch, multi:True
        #     so the one batch's --team_ids is ``values`` verbatim.
        lock_scope = ""
        if spec["source"] in _PER_SCOPE_LOCK_SOURCES:
            from lib.import_lock_service import ImportLockService

            if spec["source"] == "echo":
                lock_scope = ImportLockService.echo_tenant_scope_key(es, values)
            elif spec["source"] == "facebook":
                lock_scope = ImportLockService.meta_connection_scope_key(es, values)
            else:
                lock_scope = ImportLockService.scope_key_for_ids(values)
        return {
            "dispatched": dispatched > 0,
            "source": spec["source"],
            "skipped_reason": "",
            "pids": pids,
            "lock_scope": lock_scope,
        }

    @staticmethod
    def _batch_echo_import(es, schedules: list, *, now: datetime | None = None) -> dict:
        """ONE echo import covering every due echo schedule in a
        run_due batch.

        Echo's API is tenant-scoped, so per-schedule dispatch made N overlapping
        full-tenant pulls whose lock contention silently dropped all but the
        first (ImportLockService.acquire aborts instead of queuing). Instead:
        union the schedules' team_esids and widen to the earliest from / latest
        to of their date ranges, then dispatch once — the importer contacts each
        owning tenant exactly once and the wider window is safe (conversations
        upsert idempotently by Echo conversation id).

        Returns the _trigger_scoped_import result dict (dispatched=False with a
        skipped_reason when no echo schedule resolves any team).

        mints ONE shared ``de_import_progress`` doc for the batch and threads
        its run_id through the dispatch, and returns it as ``progress_run_id``. run_due
        stamps that shared run_id onto EVERY due echo schedule's run_state, so each
        schedule's snapshot build can wait on (and be finalized by) the same pull. Without
        this the echo batch held no run_id, so echo — the connector that surfaced
        — would have kept building snapshots before its data landed.
        """
        now = now or _now()
        teams: set = set()
        froms: list = []
        tos: list = []
        for s in schedules:
            team_esids = [t for t in ReportScheduleService._schedule_team_esids(s) if t]
            if not team_esids:
                continue
            teams.update(team_esids)
            # derive each schedule's window from its frequency.
            dr = ReportScheduleService.resolve_window_for_schedule(s, now=now)
            froms.append(dr.get("from", ""))
            tos.append(dr.get("to", ""))
        if not teams:
            return {"dispatched": False, "source": "echo", "skipped_reason": "no scope values resolved", "pids": []}
        # ISO dates compare lexicographically; min/max give the widest window.
        date_range = {"from": min(froms), "to": max(tos)}

        # mint ONE progress doc for the shared batch pull. Keyed on a synthetic
        # batch connection so it doesn't collide with a per-schedule run_now doc. echo's
        # importer adopts --progress_run_id.
        batch_run_id = None
        try:
            from lib.import_progress_service import ImportProgressService

            range_days = max(1, ReportScheduleService._date_range_days(date_range))
            batch_run_id = ImportProgressService.start_run(
                username="scheduler:echo-batch",
                connection_esid=f"schedule-batch:echo:{date_range['from']}:{date_range['to']}",
                connector_id="echo",
                days_total=range_days,
            )
        except Exception as exc:  # noqa: BLE001 — boundary; progress is a UX/wait nicety
            logging.warning("_batch_echo_import start_run failed: %s", exc)

        imp = ReportScheduleService._trigger_scoped_import(es, "echo", sorted(teams), date_range, progress_run_id=batch_run_id)
        # Surface the shared run_id so run_due can stamp it onto each echo schedule.
        # If dispatch didn't happen, fail the dangling doc so nothing waits on it.
        if batch_run_id:
            if imp.get("dispatched"):
                imp["progress_run_id"] = batch_run_id
            else:
                ReportScheduleService._fail_progress_run(
                    batch_run_id, error=imp.get("skipped_reason") or "echo batch import not dispatched"
                )
        return imp

    @staticmethod
    @staticmethod
    def _set_run_state(es, schedule_id: str, state: dict) -> None:
        """#5 — persist the schedule's ``last_run_state`` (best-effort).

        Writing run-state must NEVER fail or slow a run, so every error is caught
        and logged. ``refresh="wait_for"`` so the next /api/list the page polls
        reflects the new phase immediately (the bar rehydrates on what it reads).
        """
        if not schedule_id:
            return
        try:
            es.update(
                index=INDEX_REPORT_SCHEDULES,
                id=schedule_id,
                body={"doc": {"last_run_state": state}},
                refresh="wait_for",
            )
        except Exception as e:  # noqa: BLE001 — run-state is advisory, never fatal
            logging.warning("ReportScheduleService._set_run_state %s: %s", schedule_id, e)

    # ─────────────────────────────────────────────────────────────────────────
    # Import-error capture
    # ─────────────────────────────────────────────────────────────────────────

    @staticmethod
    def summarize_import_errors(run_state: dict) -> dict:
        """collect the classified import errors a run captured, for the
        scheduler dialog's "Errors" button + expandable list.

        Reads ``run_state.import_errors`` — a list of classifier results
        (``{category, title, message, severity}``) stamped by
        :meth:`backfill_import_outcome` once the run's fire-and-forget import
        reaches a terminal status. ``count`` reflects only ``error``-severity
        entries (the red badge): ``transient`` (rate-limit, lock-skip) and
        ``info`` (no-data, chatwoot-skipped) are surfaced in ``errors``/``info``
        but never counted, mirroring ``summarize_snapshot_errors``' benign-skip
        handling.

        Returns ``{"count": int, "errors": [{"title", "message", "severity"}],
        "info": [{"title", "message"}]}``. PURE — the route attaches it to list
        rows and tests exercise it directly.
        """
        run_state = run_state or {}
        raw_entries = run_state.get("import_errors") or []
        errors: list[dict] = []
        info: list[dict] = []
        count = 0
        for entry in raw_entries:
            if not isinstance(entry, dict):
                continue
            severity = entry.get("severity") or SEVERITY_ERROR
            row = {
                "title": entry.get("title") or "Import error",
                "message": entry.get("message") or "",
                "severity": severity,
            }
            if severity == SEVERITY_INFO:
                info.append({"title": row["title"], "message": row["message"]})
            else:
                errors.append(row)
                if severity == SEVERITY_ERROR:
                    count += 1
        return {"count": count, "errors": errors, "info": info}

    @staticmethod
    def _import_is_terminal(progress_run_id: str) -> str | None:
        """the terminal status of a dispatched import's de_import_progress
        doc, or None if it's still in flight / unknown / ES is down.

        Terminal = ``succeeded`` | ``failed`` | ``cancelled`` (the same set
        ``backfill_import_outcome`` and ``ImportProgressService.complete_run`` use). The
        wait loop (run_due) and the deferred-build sweep both gate the snapshot build on
        this: a snapshot is built only once this returns non-None (or no import was
        dispatched, or the deferred run aged out). Fail-soft — any error returns None so
        a transient ES blip just defers the build to the next poll rather than building
        on stale data.

        Note: ``backfill_import_outcome`` keeps its OWN get_run rather than calling this,
        because it needs the whole doc (per-asset import_errors + the fatal error), and
        routing it through here would double the ES read for no behavior change.
        """
        if not progress_run_id:
            return None
        try:
            from lib.import_progress_service import ImportProgressService

            doc = ImportProgressService.get_run(progress_run_id)
        except Exception as exc:  # noqa: BLE001 — boundary; defer rather than guess
            logging.warning("_import_is_terminal get_run(%r): %s", progress_run_id, exc)
            return None
        if not doc:
            return None
        status = (doc.get("status") or "").strip()
        return status if status in ("succeeded", "failed", "cancelled") else None

    @staticmethod
    def backfill_import_outcome(es, schedule: dict) -> dict:
        """when a schedule's fire-and-forget import has finished, read
        its terminal outcome from ``de_import_progress`` and stamp a classified
        ``import_errors`` list onto ``last_run_state`` (persisted once, not every
        poll).

        The import is dispatched fire-and-forget (``_trigger_scoped_import``), so
        its outcome doesn't exist when ``_execute_one`` finalizes the run. This
        lazy back-fill — invoked from the read path (``list_schedules``) — closes
        that gap WITHOUT blocking the run or re-running the import:

          * No ``progress_run_id`` on the run-state → nothing to read (a connector
            with no progress doc, or an older run). No-op.
          * Already reconciled THIS run_id to a NON-EMPTY list (tracked via
            ``import_errors_run_id``) → no-op; a terminal doc's errors don't change.
          * Otherwise re-read the doc and reconcile. Two cases deliberately fall
            through: (a) an empty/absent list — an early poll can stamp ``[]``
            microseconds before the importer's ``record_import_error`` append lands
            (its append and the parent's ``complete_run`` are separate ES writes),
            so ``[]`` is never trusted as final; and (b) a stamp whose
            ``import_errors_run_id`` differs from the current run — a fresh run
            (Run-now / next cadence) supersedes the prior run's errors, which must
            not linger. Both were real staging bugs (2026-06-19): the
            empty-race lost an error permanently; the run_id mismatch showed a prior
            run's stale message. The write-back is skipped when both the list and
            the run_id already match, so a settled run costs no extra ES writes.
          * Progress doc not terminal yet (pending/running/cancelling) → no-op; the
            existing phase/progress bar covers the in-flight state. We only
            summarize a SETTLED run, so "no errors" never masks "still importing".
          * Any terminal status → collect the per-asset errors the importer
            appended live (``doc.import_errors``, already classified by
            ``record_import_error``) AND, on ``failed``, the fatal ``doc.error``
            classified here, deduped by category. A ``succeeded`` run can still
            carry per-asset errors (one page/property failed, the loop continued)
            — those surface too.
          * A clean terminal run (no per-asset errors, and ``succeeded`` /
            ``cancelled`` so no fatal error) → stamp ``import_errors=[]`` so the run
            is marked back-filled (idempotent) and the button stays absent.

        Best-effort: any ES/classify error is caught and the schedule is returned
        unchanged — capturing errors must never fail or slow a list. Returns the
        (possibly mutated) ``schedule`` dict so the caller can use it inline.
        """
        try:
            run_state = schedule.get("last_run_state") or {}
            if not isinstance(run_state, dict):
                return schedule
            run_id = run_state.get("progress_run_id")
            if not run_id:
                return schedule
            # Idempotency WITHOUT going stale: short-circuit only when we've
            # already reconciled THIS run_id to a non-empty list. We record the
            # reconciled run_id in `import_errors_run_id`; if it matches the
            # current run_id and the list is non-empty, the doc is terminal and
            # won't change → no re-read. Two cases deliberately fall through to a
            # re-read:
            #   * empty/absent list — an early poll can stamp [] microseconds
            #     before the importer's record_import_error append lands (the
            # race), so [] is never trusted as final; and
            #   * a DIFFERENT run_id than we reconciled — a fresh run (Run-now /
            #     next cadence) supersedes the prior run's errors, which must not
            #     linger (the stale-wording bug, staging 2026-06-19).
            existing_errors = run_state.get("import_errors")
            reconciled_run_id = run_state.get("import_errors_run_id")
            if isinstance(existing_errors, list) and existing_errors and reconciled_run_id == run_id:
                return schedule

            from lib.import_progress_service import ImportProgressService

            doc = ImportProgressService.get_run(run_id)
            if not doc:
                return schedule
            status = (doc.get("status") or "").strip()
            # Only a settled run is summarizable. pending/running/cancelling →
            # leave un-stamped so a later poll picks up the terminal outcome.
            if status not in ("succeeded", "failed", "cancelled"):
                return schedule

            import_errors: list[dict] = []

            # (1) Per-asset errors the importer appended live via
            # ImportProgressService.record_import_error. These
            # are ALREADY classified ({category,title,message,severity}) and deduped
            # by category — a run can carry these even when it "succeeded" overall,
            # because they are non-fatal per-asset failures the loop continued past.
            granular = doc.get("import_errors")
            if isinstance(granular, list):
                import_errors.extend(e for e in granular if isinstance(e, dict) and e.get("category"))

            # (2) The run's fatal error (the one that aborted it), classified here.
            # Only present on a "failed" terminal status. Dedup against the granular
            # entries by category so a fatal error that was ALSO recorded per-asset
            # isn't shown twice.
            if status == "failed":
                from lib.import_error_classifier import classify

                raw = (doc.get("error") or "").strip()
                if raw:
                    fatal = classify(raw, connector_id=run_state.get("connector_id"))
                    seen = {e.get("category") for e in import_errors}
                    # Suppress a GENERIC fatal when a specific per-asset error was
                    # already recorded. The dispatcher stamps a subprocess
                    # CalledProcessError ("...returned non-zero exit status 1") as
                    # the fatal error, which classifies as "unknown" — but the
                    # importer already recorded the REAL cause (zero teams, revoked,
                    # …) via record_import_error. Showing the unknown alongside it
                    # is noise. A *specific* fatal (one that classified to a real
                    # category) is still added if new.
                    if fatal.get("category") == "unknown" and import_errors:
                        pass
                    elif fatal.get("category") not in seen:
                        import_errors.append(fatal)

            # (3) a Meta backfill that stopped at a rate limit stamped a
            # ``backfill_bailout`` note on the progress doc. Surface it as an
            # INFO-severity entry (grey note, not a red error): the run succeeded and
            # the data written before the limit is committed — this just tells the
            # user MORE remains and to run the backfill again to continue.
            bailout = doc.get("backfill_bailout")
            if isinstance(bailout, dict) and bailout.get("stopped_at_rate_limit"):
                frm = str(bailout.get("date_from") or "").strip()
                to = str(bailout.get("date_to") or "").strip()
                span = (" for " + frm + " to " + to) if (frm and to) else ""
                import_errors.append(
                    {
                        "category": "backfill_rate_limit",
                        "title": "Backfill paused at Meta rate limit",
                        "message": (
                            "The backfill" + span + " stopped when Meta's rate limit was reached. "
                            "The data imported before the limit was kept — run the backfill again to "
                            "continue from where it left off."
                        ),
                        "severity": SEVERITY_INFO,
                    }
                )

            # Reconcile: persist only when the computed list OR the run_id it was
            # computed from differs from what's stamped. `import_errors_run_id`
            # records which run the stamped list belongs to, so a NEW run (Run-now
            # / next cadence) always rewrites — its predecessor's errors don't
            # linger — while a re-poll of the SAME settled run writes nothing.
            if existing_errors == import_errors and reconciled_run_id == run_id:
                schedule["last_run_state"] = {**run_state, "import_errors": import_errors, "import_errors_run_id": run_id}
                return schedule
            run_state = {**run_state, "import_errors": import_errors, "import_errors_run_id": run_id}
            schedule["last_run_state"] = run_state
            ReportScheduleService._set_run_state(es, schedule.get("schedule_id"), run_state)
        except Exception as e:  # noqa: BLE001 — capture is best-effort, never fatal
            logging.warning(
                "ReportScheduleService.backfill_import_outcome %s: %s",
                schedule.get("schedule_id"),
                e,
            )
        return schedule

    @staticmethod
    def _execute_email_one(es, schedule: dict, *, now: datetime, result: dict) -> dict:
        """execute one EMAIL schedule: render the dashboard report for
        the resolved window and email it. NO import is dispatched.

        Snapshot persistence is format-dependent (, user decision
        2026-07-16): a ``summary_link`` email is useless without a persisted
        snapshot to link to, so that one format indexes its snapshot exactly
        like an import schedule (stamped snapshot_id + snapshot_keep pruning).
        Every other format keeps the delivery-only contract — the
        snapshot is built in-memory purely as the email payload. Persistence
        is best-effort: on failure the email still goes out and the mailer
        suppresses the link (empty snapshot_id).

        Scheduling state is advanced like any other run. Email failure is
        recorded on ``result["error"]`` (the run is "done" otherwise);
        rendering/advance errors fail the run. A minimal terminal run_state is
        written so the card reflects the last outcome (no progress bar — there
        is no import to track).
        """
        schedule_id = schedule.get("schedule_id")
        delivery = schedule.get("delivery") or {}
        email_cfg = delivery.get("email") or {}
        run_state = {
            "phase": RUN_PHASE_GENERATING,
            "started_at": now.isoformat(),
            "finished_at": None,
            "snapshot_id": None,
            "import_dispatched": False,
            "connector_id": "",
            "progress_run_id": None,
            "import_pids": [],
            "import_lock_scope": "",
            "error": None,
            "import_errors": [],
            "import_errors_run_id": None,
            # follow-up — clear a stale backfill marker (ES merges nested
            # objects on partial update; see the import-run initializer).
            "is_backfill": False,
        }
        ReportScheduleService._set_run_state(es, schedule_id, run_state)

        try:
            snapshot_for_email = ReportScheduleService._build_snapshot_doc(schedule, now=now, es=es)
        except Exception as e:  # noqa: BLE001 — a render failure fails this run
            logging.error("ReportScheduleService._execute_email_one render failed for %s: %s", schedule_id, e)
            result["error"] = "Could not render the report."
            run_state["phase"] = RUN_PHASE_FAILED
            run_state["finished_at"] = _now().isoformat()
            run_state["error"] = result["error"]
            ReportScheduleService._set_run_state(es, schedule_id, run_state)
            return result

        # persist the snapshot when the resolved email format is
        # summary_link (the mailer's default, also chosen for unknown values —
        # mirror that resolution here) so the emailed link has something to
        # open. Other formats stay in-memory-only. Best-effort:
        # a persist failure still sends the email, just without the link.
        fmt = (email_cfg.get("format") or "") or "summary_link"
        if fmt not in ("inline_html", "excel", "pdf"):
            try:
                res = es.index(index=INDEX_REPORT_SNAPSHOTS, body=snapshot_for_email, refresh="wait_for")
                snap_id = res["_id"]
                es.update(
                    index=INDEX_REPORT_SNAPSHOTS,
                    id=snap_id,
                    body={"doc": {"snapshot_id": snap_id}},
                    refresh="wait_for",
                )
                snapshot_for_email["snapshot_id"] = snap_id  # the email link references it
                result["snapshot_id"] = snap_id
                run_state["snapshot_id"] = snap_id
                # Same retention contract as import schedules.
                try:
                    keep = clamp_snapshot_keep(delivery.get("snapshot_keep"))
                    ReportScheduleService.prune_snapshots(es, schedule_id, keep=keep)
                except Exception as e:  # noqa: BLE001 — pruning never fails the run
                    logging.warning(
                        "ReportScheduleService._execute_email_one snapshot prune failed for %s: %s",
                        schedule_id,
                        e,
                    )
            except Exception as e:  # noqa: BLE001 — email still goes out, link suppressed
                logging.error(
                    "ReportScheduleService._execute_email_one snapshot persist failed for %s "
                    "(email will send without a link): %s",
                    schedule_id,
                    e,
                )

        # Deliver. Recipients are required at create/edit, but guard anyway so a
        # hand-crafted row with no recipients is a no-op rather than a crash.
        if email_cfg.get("enabled") and (email_cfg.get("recipients") or []):
            try:
                res_mod = _import_first(
                    "asset_management.web.services.report_email_service",
                    "web.services.report_email_service",
                    "services.report_email_service",
                )
                deliver_res = res_mod.ReportEmailService.deliver(schedule, snapshot_for_email)
                result["emailed"] = deliver_res.get("emailed", 0)
            except Exception as e:  # noqa: BLE001 — delivery failure is recorded, not raised
                logging.error("ReportScheduleService._execute_email_one delivery failed for %s: %s", schedule_id, e)
                result["error"] = "Could not send the report email."
        else:
            logging.info(
                "ReportScheduleService._execute_email_one: %s has email disabled / no recipients; nothing to send",
                schedule_id,
            )

        # Advance scheduling state (independent of delivery outcome — the cadence
        # marches on; a failed send is surfaced on the card, not retried off-cadence).
        try:
            ReportScheduleService._advance_schedule(es, schedule, now=now)
        except Exception:  # noqa: BLE001 — already logged in _advance_schedule
            result["error"] = result["error"] or "Could not schedule the next run."

        run_state["finished_at"] = _now().isoformat()
        run_state["phase"] = RUN_PHASE_FAILED if result["error"] else RUN_PHASE_DONE
        run_state["error"] = result["error"]
        ReportScheduleService._set_run_state(es, schedule_id, run_state)
        return result

    @staticmethod
    def _execute_one(
        es,
        schedule: dict,
        *,
        now: datetime | None = None,
        predispatched_import: dict | None = None,
        wait_for_import: bool = False,
    ) -> dict:
        """Execute a single schedule: dispatch the import, then build a snapshot (if
        save_snapshot) AFTER the import lands, and advance last_run/next_run. Returns a
        per-schedule result dict.

        Email delivery (P3): after a successful snapshot, if
        ``delivery.email.enabled`` and recipients are present, dispatch the
        report to each. Best-effort — an email failure never fails the run.

        ``predispatched_import``: when run_due already covered this
        schedule's connector with a batch-level dispatch, its result dict is
        passed here and the per-schedule dispatch is suppressed — even if the
        batch dispatch failed (falling back per-schedule would reintroduce the
        overlapping-pulls problem the batch exists to prevent).

        ``wait_for_import``: True only from run_due (headless cron). When the
        run dispatched an import (has a progress_run_id), it WAITS inline up to
        WAIT_TIMEOUT_SECONDS for the import to reach terminal, then builds. run_now passes
        False — it must not block the HTTP request — so it DEFERS the build: it returns
        immediately leaving phase=IMPORTING, and a later run_due poll
        (_sweep_deferred_builds) builds the snapshot once the import settles. Either way
        the snapshot is built only AFTER the import's data lands (never on pre-import
        data). A run with no import to wait on builds inline regardless of this flag.
        """
        now = now or _now()
        schedule_id = schedule.get("schedule_id")
        delivery = schedule.get("delivery") or {}
        result = {
            "schedule_id": schedule_id,
            "snapshot_id": None,
            "emailed": 0,
            "import_dispatched": False,
            "error": None,
        }

        # an EMAIL schedule emails an existing dashboard report on its
        # cadence and imports NO data + saves NO snapshot. It shares none of the
        # import/run-state/progress-bar machinery below, so branch out here.
        if schedule.get("schedule_type") == SCHEDULE_TYPE_EMAIL:
            return ReportScheduleService._execute_email_one(es, schedule, now=now, result=result)

        # #5 — start persisted run-state so a progress bar can rehydrate.
        # progress_run_id links the bar to the live de_import_progress
        # doc (real percentage + stage message) for progress-capable connectors.
        connector_id = schedule.get("connector_id") or ""
        run_state = {
            "phase": RUN_PHASE_IMPORTING,
            "started_at": now.isoformat(),
            "finished_at": None,
            "snapshot_id": None,
            "import_dispatched": False,
            "connector_id": connector_id,
            "progress_run_id": None,
            # [{pid, pgid}] of the dispatched import subprocess(es), so a
            # later Cancel can os.killpg the process group. Cleared on terminal phase.
            "import_pids": [],
            # the per-scope import-lock key this dispatch acquired (""
            # for connector-global). Cancel's force_release needs it to clear the
            # right per-scope lock doc (the connector_id alone no longer identifies
            # the lock for per-scope connectors like google_ads / ga4).
            "import_lock_scope": "",
            "error": None,
            # explicitly CLEAR the prior run's captured import errors at
            # the start of a new run so the "Errors" button disappears immediately
            # on Run-now, then reappears only if THIS run records errors (don't rely
            # on _set_run_state's replace semantics for a nested object field).
            "import_errors": [],
            "import_errors_run_id": None,
            # follow-up — explicitly clear the backfill marker: ES partial
            # updates MERGE nested objects, so after one backfill stamped
            # is_backfill=true, every later normal run that omitted the key kept it
            # and the card labeled fresh-data runs "Backfilling historical data…".
            "is_backfill": False,
        }
        ReportScheduleService._set_run_state(es, schedule_id, run_state)

        # v2 (Phase D): import fresh data for the connector + selected teams over
        # the resolved window BEFORE rendering. Fire-and-forget + best-effort —
        # an import failure never fails the run (the snapshot/email proceed on
        # existing data; the next run picks up the fresh import). chatwoot has no
        # team-scope flag → skipped.
        team_esids = ReportScheduleService._schedule_team_esids(schedule)
        if predispatched_import is not None:
            result["import_dispatched"] = bool(predispatched_import.get("dispatched"))
            # the echo batch dispatch (run_due) carries the shared pull's
            # pids; stamp them so cancelling ANY echo schedule can killpg the shared
            # process group (the tenant-wide pull all due echo schedules share).
            run_state["import_pids"] = list(predispatched_import.get("pids") or [])
            # echo (the only predispatched/batched connector) now takes a
            # PER-TENANT lock; the batch dispatch resolved its team set → tenant scope
            # key in _trigger_scoped_import, so this carries that key (Cancel's
            # force_release uses it). "" only for a cron-style unscoped batch.
            run_state["import_lock_scope"] = predispatched_import.get("lock_scope") or ""
            # adopt the batch's SHARED progress run_id so this echo schedule's
            # snapshot build waits on (and is finalized by) the one shared pull. Persist
            # immediately so a crash before the wait/defer decision still leaves the
            # schedule resolvable by the sweep.
            batch_run_id = predispatched_import.get("progress_run_id")
            if batch_run_id:
                run_state["progress_run_id"] = batch_run_id
                ReportScheduleService._set_run_state(es, schedule_id, run_state)
        elif connector_id and team_esids:
            try:
                # pull window derived from frequency, not a stored mode.
                dr = ReportScheduleService.resolve_window_for_schedule(schedule, now=now)
                # mint a progress doc (FB-capable connectors only) and
                # pass its run_id to the importer so it streams stage/day progress.
                range_days = ReportScheduleService._date_range_days(dr)
                progress_run_id = ReportScheduleService._start_progress_run(connector_id, schedule_id, range_days=range_days)
                if progress_run_id:
                    run_state["progress_run_id"] = progress_run_id
                    ReportScheduleService._set_run_state(es, schedule_id, run_state)
                imp = ReportScheduleService._trigger_scoped_import(
                    es, connector_id, team_esids, dr, progress_run_id=progress_run_id
                )
                result["import_dispatched"] = bool(imp.get("dispatched"))
                # record the dispatched group's pids so Cancel can killpg it.
                run_state["import_pids"] = list(imp.get("pids") or [])
                # record the per-scope lock key so Cancel's force_release
                # can clear the right per-scope lock doc (google_ads / ga4).
                run_state["import_lock_scope"] = imp.get("lock_scope") or ""
                # a connector with no team-scope import step (chatwoot)
                # is skipped with reason "connector not team-scopable" and no
                # progress doc. Surface that as an INFORMATIONAL note now (no async
                # outcome to wait for), so the user understands the report rendered
                # on existing data rather than silently. backfill_import_outcome
                # leaves this run alone (the import_errors key is already present).
                if not result["import_dispatched"] and (imp.get("skipped_reason") == "connector not team-scopable"):
                    from lib.import_error_classifier import classify

                    run_state["import_errors"] = [classify("import step skipped", connector_id=connector_id)]
                # a connector with a resolver (google_ads / ga4) can
                # mint a progress doc and then skip dispatch when no scope values
                # resolve. An un-dispatched doc would sit pending and hang the bar
                # until the 30-min reaper. Stamp it failed and drop the run_id so
                # the bar resolves immediately instead of polling a dead doc.
                if progress_run_id and not result["import_dispatched"]:
                    ReportScheduleService._fail_progress_run(
                        progress_run_id, error=imp.get("skipped_reason") or "import not dispatched"
                    )
                    run_state["progress_run_id"] = None
            except Exception as e:  # noqa: BLE001 — import dispatch never fails the run
                logging.error("ReportScheduleService._execute_one import dispatch failed for %s: %s", schedule_id, e)

        # #5 — import dispatched (if any). the snapshot must be
        # built only AFTER the dispatched import lands data in ES, so decide HOW to
        # reach the build from here:
        run_state["import_dispatched"] = bool(result["import_dispatched"])
        progress_run_id = run_state.get("progress_run_id")

        # (A) No import to wait on — chatwoot / no-teams / a resolver-skip that
        # cleared progress_run_id, OR a connector with no progress doc. There is no
        # async data to wait for, so build INLINE on existing data (today's behavior).
        # Run-now still returns a real snapshot_id for these.
        if not progress_run_id:
            return ReportScheduleService._finalize_build(es, schedule, run_state, now=now, result=result)

        # (B) run_due (headless) — WAIT inline for the import to land, then build.
        # Bounded poll on the progress doc; a transient/long import that hasn't
        # settled by the cap falls through to DEFER (the sweep finishes it).
        if wait_for_import:
            deadline = time.monotonic() + WAIT_TIMEOUT_SECONDS
            while time.monotonic() < deadline:
                if ReportScheduleService._import_is_terminal(progress_run_id) is not None:
                    return ReportScheduleService._finalize_build(es, schedule, run_state, now=now, result=result)
                time.sleep(WAIT_POLL_INTERVAL_SECONDS)
            logging.info(
                "_execute_one: import %s for %s not terminal after %ds; deferring build to sweep",
                progress_run_id,
                schedule_id,
                WAIT_TIMEOUT_SECONDS,
            )

        # (C) run_now (in-request, must not block) OR run_due timed out — DEFER the
        # build. Leave phase=IMPORTING (the UI keeps showing "Importing…" + polling),
        # advance next_run NOW so a slow import isn't re-selected as due next poll, and
        # let _sweep_deferred_builds build the snapshot once the import is terminal.
        try:
            ReportScheduleService._advance_schedule(es, schedule, now=now)
            run_state["next_run_advanced"] = True
        except Exception:  # noqa: BLE001 — advance is best-effort; never 500 a Run-now.
            # next_run_advanced stays falsy → _finalize_build will advance at build time.
            # Worst case the schedule re-fires before the deferred build; the sweep dedups
            # by the single latest run_state (latest run wins), so no double-build.
            pass
        ReportScheduleService._set_run_state(es, schedule_id, run_state)
        return result

    @staticmethod
    def _advance_schedule(es, schedule: dict, *, now: datetime) -> None:
        """Advance ``last_run``/``next_run`` strictly after ``now`` so a run never
        re-fires the same window on the next poll. Best-effort: a failure is
        logged, never raised — extracted from _execute_one so both the inline-build and
        the deferred path advance identically."""
        schedule_id = schedule.get("schedule_id")
        try:
            next_run = ReportScheduleService._next_run_after_fire(
                schedule.get("schedule") or {}, schedule.get("next_run"), now=now
            )
            es.update(
                index=INDEX_REPORT_SCHEDULES,
                id=schedule_id,
                body={"doc": {"last_run": now.isoformat(), "next_run": next_run.isoformat()}},
                refresh="wait_for",
            )
        except Exception as e:
            logging.error("ReportScheduleService._advance_schedule failed for %s: %s", schedule_id, e)
            raise

    @staticmethod
    def _finalize_build(es, schedule: dict, run_state: dict, *, now: datetime, result: dict) -> dict:
        """build the snapshot + deliver + advance + finalize run-state.

        Extracted from _execute_one's tail so it can run EITHER inline (run_due wait
        completed, or no import to wait on) OR deferred (from _sweep_deferred_builds once
        a run_now's import reached terminal). A snapshot is written ONLY here, so it is
        never built on pre-import data while an import is in flight.

        ``now`` is the ORIGINAL run's timestamp (the deferred caller reconstructs it from
        ``run_state["started_at"]``) so the report's date window + ``generated_at`` match
        the run, not the (later) sweep time. When ``run_state["next_run_advanced"]`` is
        set, the defer path already advanced the schedule, so the advance here is skipped.
        """
        es_client = es
        schedule_id = schedule.get("schedule_id")
        delivery = schedule.get("delivery") or {}

        # Now building the snapshot.
        run_state["phase"] = RUN_PHASE_GENERATING
        ReportScheduleService._set_run_state(es_client, schedule_id, run_state)

        snap = None
        if delivery.get("save_snapshot", True):
            snap = ReportScheduleService._build_snapshot_doc(schedule, now=now, es=es_client)
            try:
                res = es_client.index(index=INDEX_REPORT_SNAPSHOTS, body=snap, refresh="wait_for")
                snap_id = res["_id"]
                es_client.update(
                    index=INDEX_REPORT_SNAPSHOTS,
                    id=snap_id,
                    body={"doc": {"snapshot_id": snap_id}},
                    refresh="wait_for",
                )
                snap["snapshot_id"] = snap_id  # so the email link can reference it
                result["snapshot_id"] = snap_id
                run_state["snapshot_id"] = snap_id
                # keep only the N most-recent snapshots for this
                # schedule (delivery.snapshot_keep, default 7). Pruning every run
                # syncs the retained set to the chosen value, so lowering it also
                # deletes the now-excess older snapshots on the next run. The just-
                # written snapshot is the newest, so it's never pruned. Best-effort:
                # a prune failure must never fail the run.
                try:
                    keep = clamp_snapshot_keep(delivery.get("snapshot_keep"))
                    ReportScheduleService.prune_snapshots(es_client, schedule_id, keep=keep)
                except Exception as e:  # noqa: BLE001 — pruning never fails the run
                    logging.warning(
                        "ReportScheduleService._finalize_build snapshot prune failed for %s: %s",
                        schedule_id,
                        e,
                    )
            except Exception as e:
                logging.error(
                    "ReportScheduleService._finalize_build snapshot write failed for %s: %s",
                    schedule_id,
                    e,
                )
                result["error"] = "Could not save the report snapshot."
                # #5 — mark the run failed so the card shows it, not a
                # bar stuck mid-"generating".
                run_state["phase"] = RUN_PHASE_FAILED
                run_state["finished_at"] = _now().isoformat()
                run_state["error"] = result["error"]
                run_state["import_pids"] = []  # terminal; drop stale pids
                ReportScheduleService._set_run_state(es_client, schedule_id, run_state)
                return result

        # Email delivery (best-effort; isolated from the snapshot + advance).
        # Import-only schedules (no dashboard) are excluded: there is no report
        # content to render, so an enabled email flag — reachable via the raw
        # API even though the form disables the checkbox — would ship an EMPTY
        # report email every cadence.
        if (delivery.get("email") or {}).get("enabled"):
            if not ReportScheduleService._schedule_dashboard_ids(schedule):
                logging.info(
                    "ReportScheduleService._finalize_build: %s is import-only (no dashboard); skipping email delivery",
                    schedule_id,
                )
            else:
                try:
                    snapshot_for_email = snap or ReportScheduleService._build_snapshot_doc(schedule, now=now, es=es_client)
                    res_mod = _import_first(
                        "asset_management.web.services.report_email_service",
                        "web.services.report_email_service",
                        "services.report_email_service",
                    )
                    deliver_res = res_mod.ReportEmailService.deliver(schedule, snapshot_for_email)
                    result["emailed"] = deliver_res.get("emailed", 0)
                except Exception as e:  # noqa: BLE001 — delivery never fails the run
                    logging.error(
                        "ReportScheduleService._finalize_build email delivery failed for %s: %s",
                        schedule_id,
                        e,
                    )

        # Advance scheduling state — UNLESS the defer path already advanced it at
        # dispatch, which would otherwise skip a cadence.
        if not run_state.get("next_run_advanced"):
            try:
                ReportScheduleService._advance_schedule(es_client, schedule, now=now)
            except Exception:  # noqa: BLE001 — already logged in _advance_schedule
                result["error"] = "Could not schedule the next run."

        # #5 — finalize the run-state: done on success, failed if the
        # advance errored. The connector import may still be running; the client
        # polls the per-connector lock for that tail (import_dispatched tells it
        # whether to bother).
        run_state["finished_at"] = _now().isoformat()
        if result["error"]:
            run_state["phase"] = RUN_PHASE_FAILED
            run_state["error"] = result["error"]
            # the synchronous build/advance failed; no live import tail to
            # cancel, so drop the pids (a stale pgid could collide with a recycled pid).
            run_state["import_pids"] = []
        else:
            run_state["phase"] = RUN_PHASE_DONE
            # keep import_pids on DONE: the snapshot is built but the
            # connector import is still running its tail, and that's exactly what a
            # Cancel during the tail needs to killpg.
        ReportScheduleService._set_run_state(es_client, schedule_id, run_state)
        return result

    # Connectors whose backfill covered-range we can compute from a daily index +
    # day field. FB is the only backfill that bails out mid-range; others complete
    # their whole range or fail, so covered==requested and we skip the query.
    _BACKFILL_COVERAGE = {
        "meta_ads": {"index_type": "fb_insights", "day_field": "insight_date_start"},
    }

    @staticmethod
    def _backfill_covered_range(es, connector_id, team_esids, requested_range):
        """the ACTUAL day coverage now in ES for a backfill's teams, so the
        History row can say "requested X, imported Y" (how much of the range landed
        before a rate-limit bail-out).

        Returns ``{"from": iso, "to": iso}`` (union min/max of the day field across the
        in-scope team indices, clamped to the requested window) or ``None`` when the
        connector has no coverage mapping, no data exists, or the query fails. Best-
        effort — never raises (a coverage failure must not block the run settling)."""
        spec = ReportScheduleService._BACKFILL_COVERAGE.get(connector_id)
        if not spec or es is None:
            return None
        req = requested_range or {}
        frm = str(req.get("from") or "").strip()
        to = str(req.get("to") or "").strip()
        if not frm or not to or not team_esids:
            return None
        try:
            from lib.es_helpers import build_data_index_name

            indices = [build_data_index_name(t, spec["index_type"]) for t in team_esids]
            day = spec["day_field"]
            res = es.search(
                index=",".join(indices),
                body={
                    "size": 0,
                    "query": {"range": {day: {"gte": frm, "lte": to}}},
                    "aggs": {"min_day": {"min": {"field": day}}, "max_day": {"max": {"field": day}}},
                },
                ignore=[404],
            )
            aggs = (res or {}).get("aggregations") or {}
            lo = (aggs.get("min_day") or {}).get("value_as_string") or (aggs.get("min_day") or {}).get("value")
            hi = (aggs.get("max_day") or {}).get("value_as_string") or (aggs.get("max_day") or {}).get("value")
            if not lo or not hi:
                return None
            # value_as_string may carry a time component on a date field — keep YYYY-MM-DD.
            return {"from": str(lo)[:10], "to": str(hi)[:10]}
        except Exception as exc:  # noqa: BLE001 — coverage is advisory, never fatal
            logging.warning("_backfill_covered_range(%s) failed: %s", connector_id, exc)
            return None

    @staticmethod
    def _finalize_backfill(es, schedule: dict, run_state: dict, *, progress_run_id: str, now: datetime) -> None:
        """settle a BACKFILL run once its import reaches terminal.

        A backfill has no report, so it must NOT go through ``_finalize_build`` (which
        builds a snapshot + advances the schedule's cadence). This just moves the
        run-state to a terminal phase — fixing the "Backfill button stuck disabled"
        bug (the run sat in ``phase=importing`` forever, keeping ``runStateActive``
        true) — and enriches the history record so the row can show what actually
        happened: the real covered date range (how much of the requested window
        landed), whether it paused at a Meta rate limit, and any per-asset errors.

        Idempotent + race-safe: the caller only invokes this for a still-importing
        backfill whose progress doc is terminal; setting phase terminal makes a repeat
        call a no-op (the caller's ``phase==importing`` guard fails).
        """
        schedule_id = schedule.get("schedule_id")
        connector_id = (run_state.get("connector_id") or schedule.get("connector_id") or "").strip()

        # Read the terminal progress doc for status + per-asset errors + bail-out note.
        status = "succeeded"
        import_errors: list = []
        bailed_out = False
        try:
            from lib.import_progress_service import ImportProgressService

            doc = ImportProgressService.get_run(progress_run_id) or {}
            status = (doc.get("status") or "succeeded").strip()
            granular = doc.get("import_errors")
            if isinstance(granular, list):
                import_errors = [e for e in granular if isinstance(e, dict) and e.get("category")]
            bailout = doc.get("backfill_bailout")
            bailed_out = bool(isinstance(bailout, dict) and bailout.get("stopped_at_rate_limit"))
        except Exception as exc:  # noqa: BLE001 — boundary; settle even if the read fails
            logging.warning("_finalize_backfill get_run(%r): %s", progress_run_id, exc)

        # Actual covered range (best-effort) — the "how much of my 3 months landed" signal.
        covered = ReportScheduleService._backfill_covered_range(
            es, connector_id, run_state.get("backfill_team_esids") or [], run_state.get("backfill_requested_range")
        )

        # Enrich the history record written at dispatch (found by its stored id).
        history_id = run_state.get("backfill_history_id")
        if history_id:
            patch = {
                "bailed_out": bailed_out,
                "error_count": len(import_errors),
                "errors": import_errors[:20],
            }
            if covered:
                patch["covered_from"] = covered["from"]
                patch["covered_to"] = covered["to"]
            try:
                es.update(
                    index=INDEX_REPORT_SNAPSHOTS,
                    id=history_id,
                    body={"doc": {"backfill": patch}},
                    refresh="wait_for",
                )
            except Exception as exc:  # noqa: BLE001 — history enrichment is advisory
                logging.warning("_finalize_backfill history update %s: %s", history_id, exc)

        # Settle the run-state — NO snapshot, NO cadence advance.
        run_state["phase"] = RUN_PHASE_FAILED if status == "failed" else RUN_PHASE_DONE
        run_state["finished_at"] = _now().isoformat()
        run_state["import_pids"] = []  # terminal — drop stale pids
        ReportScheduleService._set_run_state(es, schedule_id, run_state)
        logging.info(
            "_finalize_backfill: settled %s as %s (bailed_out=%s, errors=%d, covered=%s)",
            schedule_id,
            run_state["phase"],
            bailed_out,
            len(import_errors),
            covered,
        )

    @staticmethod
    def sweep_deferred_builds(*, now: datetime | None = None, limit: int = 500) -> dict:
        """public, self-contained entry point for the deferred-build sweep.

        ``run_now`` returns immediately leaving ``phase=importing`` and relies on
        something else to build the snapshot once the import lands. Two finalizers
        exist: the inline poll (``finalize_deferred_build_if_ready``) and
        the cron sweep inside ``run_due``. But the inline poll only fires
        while the builder tab is foregrounded and polling ``import-status`` during
        the importing phase — which it does not do today — and ``run_due``
        only sweeps every 5 min. So a closed/backgrounded tab leaves a finished
        run_now stuck on "Importing fresh data…" for up to 5 min (and indefinitely
        where the run_due cron isn't installed).

        This wrapper lets a dedicated, high-cadence cron (``*/1``) run JUST the
        deferred-build sweep — no due-schedule dispatch, so it can run far more
        often than ``run_due`` without re-triggering imports. It connects to ES
        itself (so the thin job has no ES wiring), reuses the same idempotent,
        per-schedule-isolated :meth:`_sweep_deferred_builds`, and returns a summary.
        """
        es = get_elasticsearch_client()
        if es is None:
            logging.warning("ReportScheduleService.sweep_deferred_builds: could not connect to ES")
            return {"swept": 0}
        now = now or _now()
        swept = ReportScheduleService._sweep_deferred_builds(es, now=now, limit=limit)
        return {"swept": swept}

    @staticmethod
    def _sweep_deferred_builds(es, *, now: datetime, limit: int = 500) -> int:
        """finalize runs whose snapshot build was DEFERRED.

        A run_now (in-request) dispatches the import and returns immediately, leaving
        ``last_run_state.phase == importing`` with a ``progress_run_id`` and NO snapshot
        yet (sub-choice (a): no placeholder). A run_due that timed out waiting leaves the
        same state. This sweep — called first thing in ``run_due`` — picks those up and
        builds the snapshot once the import is terminal, so the build always happens AFTER
        the data lands.

        For each such schedule (per-schedule try/except isolated):

          * import terminal (``succeeded`` / ``failed`` / ``cancelled``) → build the
            snapshot via :meth:`_finalize_build`, with ``now`` reconstructed from the
            run's ``started_at`` so the report window + ``generated_at`` match the
            original run, not this sweep. (A failed/orphaned import is terminal too → the
            report still renders on whatever landed.)
          * not terminal, run younger than ``DEFERRED_BUILD_MAX_AGE_SECONDS`` → skip; a
            later poll retries.
          * not terminal but older than the cap → the importer likely died without
            stamping a terminal status (past even the 30-min reaper). Build on current
            data anyway so the card never sticks forever (mirrors the original "an import
            failure never fails the run; the snapshot proceeds on existing data"
            philosophy), and best-effort stamp the dangling progress doc failed.

        Returns the number of deferred runs finalized this sweep. Fail-soft: never raises
        (run_due must not wedge cron); each schedule is isolated.
        """
        # NOTE: last_run_state is mapped {type: object, enabled: false} — its nested
        # fields are NOT indexed, so a server-side term query on last_run_state.phase
        # returns ZERO hits SILENTLY (no error), not a match. So we scan schedules and
        # filter the phase client-side — the same approach backfill_import_outcome and
        # the heal use for this index. (An earlier attempt at the indexed query
        # with an error-fallback never triggered the fallback, because the query
        # succeeds-with-zero-hits rather than raising — found on staging 2026-06-23.)
        try:
            res = es.search(
                index=INDEX_REPORT_SCHEDULES,
                body={"query": {"match_all": {}}, "size": limit, "sort": [{"updated_at": "desc"}]},
            )
        except Exception as e:  # noqa: BLE001 — boundary; a sweep failure must not wedge cron
            logging.error("_sweep_deferred_builds scan failed: %s", e)
            return 0

        finalized = 0
        for hit in res.get("hits", {}).get("hits", []):
            src = hit.get("_source") or {}
            src["schedule_id"] = hit["_id"]
            run_state = src.get("last_run_state") or {}
            if not isinstance(run_state, dict):
                continue
            # Only DEFERRED runs: still in the importing phase AND carrying a run_id to
            # wait on. (Inline-built runs are already DONE/FAILED; a no-import run never
            # has a progress_run_id.)
            if run_state.get("phase") != RUN_PHASE_IMPORTING:
                continue
            progress_run_id = run_state.get("progress_run_id")
            if not progress_run_id:
                continue
            try:
                terminal = ReportScheduleService._import_is_terminal(progress_run_id)
                aged_out = False
                if terminal is None:
                    started_at = run_state.get("started_at")
                    try:
                        age = (now - datetime.fromisoformat(started_at)).total_seconds() if started_at else 0
                    except (TypeError, ValueError):
                        age = 0
                    if age <= DEFERRED_BUILD_MAX_AGE_SECONDS:
                        continue  # still importing within the window — retry next poll
                    aged_out = True
                    logging.warning(
                        "_sweep_deferred_builds: import %s for %s never reached terminal "
                        "(age %.0fs > %ds); building on current data",
                        progress_run_id,
                        src["schedule_id"],
                        age,
                        DEFERRED_BUILD_MAX_AGE_SECONDS,
                    )
                    # Best-effort: stamp the dangling doc so the bar/backfill resolve.
                    ReportScheduleService._fail_progress_run(
                        progress_run_id, error="import did not complete; report built on available data"
                    )
                # a backfill settles WITHOUT a snapshot build (no report) +
                # without advancing the cadence. Covers the terminal AND aged-out cases
                # (a dangling backfill still needs its run-state resolved so the button
                # re-enables).
                if run_state.get("is_backfill"):
                    ReportScheduleService._finalize_backfill(es, src, run_state, progress_run_id=progress_run_id, now=now)
                    finalized += 1
                    continue
                # Reconstruct the ORIGINAL run's timestamp so the report window +
                # generated_at match the run, not this (later) sweep.
                started_at = run_state.get("started_at")
                try:
                    run_now = datetime.fromisoformat(started_at) if started_at else now
                except (TypeError, ValueError):
                    run_now = now
                result = {
                    "schedule_id": src["schedule_id"],
                    "snapshot_id": None,
                    "emailed": 0,
                    "import_dispatched": bool(run_state.get("import_dispatched")),
                    "error": None,
                }
                ReportScheduleService._finalize_build(es, src, run_state, now=run_now, result=result)
                finalized += 1
            except Exception as e:  # noqa: BLE001 — per-schedule isolation
                logging.error("_sweep_deferred_builds: %s blew up: %s", src["schedule_id"], e)
        if finalized:
            logging.info("_sweep_deferred_builds: finalized %d deferred run(s)", finalized)
        return finalized

    @staticmethod
    def finalize_deferred_build_if_ready(es, schedule_id: str) -> bool:
        """finalize ONE schedule's deferred snapshot build inline, if ready.

         left a gap: a ``run_now`` defers the build (returns with
        ``phase=importing`` + a ``progress_run_id`` and no snapshot), relying on
        ``_sweep_deferred_builds`` to finalize once the import is terminal. But the sweep
        only runs at the start of ``run_due`` (the cron). Between a Run-now and the next
        scheduled cron, a schedule whose import has already finished sticks on "Importing
        fresh data…" — potentially for hours.

        The building screen polls ``api_import_status`` every ~3s while it shows that
        state. This method is the inline finalize that poll triggers: if the polled
        schedule is genuinely deferred (``phase=importing`` with a terminal
        ``progress_run_id``), build the snapshot right there — reusing the SAME
        :meth:`_import_is_terminal` gate and :meth:`_finalize_build` path the sweep uses,
        reconstructing ``now`` from ``started_at`` so the report window + ``generated_at``
        match the original run, not the (later) poll. Bounded latency: one poll interval.

        Idempotent and race-safe with the cron sweep. The schedule is RE-READ fresh here
        (not trusted from the caller's copy) so a concurrent sweep / fresh Run-now that
        already moved the run on is seen: this fires ONLY when the current run-state is
        still ``phase=importing`` with the same terminal ``progress_run_id``. A second
        poll after the build (phase now ``done``) is a no-op; a fresh Run-now re-inits the
        run-state with a new run_id, and latest-wins (the same supersession the sweep
        relies on) prevents a double-build.

        Unlike the sweep, this does NOT age-out a never-terminal import onto current data
        — that fallback stays the cron's job (the user is watching a live import; building
        on partial data behind their back would be worse than the bar holding). A poll for
        an in-flight import simply returns False and the bar keeps polling.

        Returns True iff a snapshot build was finalized this call. Fail-soft: any error is
        logged and returns False — a poll-path build must never break the status response.
        """
        try:
            schedule = ReportScheduleService.get_schedule(schedule_id)
            if not schedule:
                return False
            run_state = schedule.get("last_run_state") or {}
            if not isinstance(run_state, dict):
                return False
            # Only a genuinely DEFERRED run: still importing AND carrying a run_id.
            if run_state.get("phase") != RUN_PHASE_IMPORTING:
                return False
            progress_run_id = run_state.get("progress_run_id")
            if not progress_run_id:
                return False
            # Only build once the import has actually settled — no age-out here.
            if ReportScheduleService._import_is_terminal(progress_run_id) is None:
                return False
            # a backfill has no report: settle its run-state (fixing the
            # stuck Backfill button) + enrich history, without _finalize_build's
            # snapshot/advance.
            if run_state.get("is_backfill"):
                ReportScheduleService._finalize_backfill(es, schedule, run_state, progress_run_id=progress_run_id, now=_now())
                return True
            # Reconstruct the ORIGINAL run's timestamp so the report window +
            # generated_at match the run, not this (later) poll.
            started_at = run_state.get("started_at")
            try:
                run_now = datetime.fromisoformat(started_at) if started_at else _now()
            except (TypeError, ValueError):
                run_now = _now()
            result = {
                "schedule_id": schedule_id,
                "snapshot_id": None,
                "emailed": 0,
                "import_dispatched": bool(run_state.get("import_dispatched")),
                "error": None,
            }
            ReportScheduleService._finalize_build(es, schedule, run_state, now=run_now, result=result)
            logging.info("finalize_deferred_build_if_ready: finalized %s inline on poll", schedule_id)
            return True
        except Exception as e:  # noqa: BLE001 — boundary; a poll-path build must never break the status response
            logging.error("finalize_deferred_build_if_ready: %s blew up: %s", schedule_id, e)
            return False

    @staticmethod
    def run_due(*, now: datetime | None = None, limit: int = 500) -> dict:
        """Poll-runner entry point. Find active schedules with next_run<=now and
        execute each sequentially. One schedule's failure does not abort the
        batch (per-schedule try/except in _execute_one + the loop).

        first finalizes any DEFERRED snapshot builds (a prior run_now whose
        import has since landed) via :meth:`_sweep_deferred_builds`, then runs due
        schedules (each WAITS inline for its import before building).

        Returns a batch summary
        ``{"due": N, "ran": N, "errors": N, "swept": N, "results": [...]}``.
        """
        es = get_elasticsearch_client()
        if es is None:
            logging.warning("ReportScheduleService.run_due: could not connect to ES")
            return {"due": 0, "ran": 0, "errors": 0, "results": []}
        now = now or _now()

        # FIRST, finalize any runs whose snapshot build was DEFERRED (a
        # prior run_now whose import has since landed, or a prior run_due that timed
        # out waiting). Done before the due-query so a deferred run resolves before we
        # re-evaluate cadence, and so it shares this poll cycle (no new cron entry).
        swept = ReportScheduleService._sweep_deferred_builds(es, now=now)

        query = {
            "bool": {
                "must": [
                    {"term": {"status": STATUS_ACTIVE}},
                    {"range": {"next_run": {"lte": now.isoformat()}}},
                ]
            }
        }
        try:
            res = es.search(
                index=INDEX_REPORT_SCHEDULES,
                body={"query": query, "size": limit, "sort": [{"next_run": "asc"}]},
            )
        except Exception as e:
            logging.error("ReportScheduleService.run_due search error: %s", e)
            return {"due": 0, "ran": 0, "errors": 0, "results": []}

        hits = res.get("hits", {}).get("hits", [])
        sources = []
        for hit in hits:
            src = hit.get("_source") or {}
            src["schedule_id"] = hit["_id"]
            sources.append(src)

        # echo schedules share ONE batch-level import (union of teams,
        # widest window) instead of dispatching per schedule: echo pulls are
        # tenant-wide, and overlapping pulls lose the importer lock silently.
        # email schedules never dispatch an import, so they're excluded
        # from the echo batch even if they somehow carry an echo connector_id.
        batch_echo = None
        echo_due = [
            s for s in sources if (s.get("connector_id") or "") == "echo" and s.get("schedule_type") != SCHEDULE_TYPE_EMAIL
        ]
        if echo_due:
            try:
                batch_echo = ReportScheduleService._batch_echo_import(es, echo_due, now=now)
            except Exception as e:  # noqa: BLE001 — import dispatch never fails the batch
                logging.error("ReportScheduleService.run_due batch echo dispatch failed: %s", e)
                batch_echo = {"dispatched": False, "source": "echo", "skipped_reason": str(e), "pids": []}

        results = []
        errors = 0
        for src in sources:
            is_echo = (src.get("connector_id") or "") == "echo" and src.get("schedule_type") != SCHEDULE_TYPE_EMAIL
            # overlapping poll ticks re-dispatched a schedule whose
            # previous run was still in flight: run_due WAITS inline for the
            # import, so a schedule that takes >5 min is still due (next_run
            # unadvanced) when the next cron tick fires, and the duplicate
            # dispatch died on the per-connection import lock — a red "failed"
            # run every day (prod: the Weekly Meta schedule, 06:30 legit /
            # 06:35 duplicate, 4/4 days verified). Skip a due schedule whose
            # progress doc (connection_esid="schedule:<id>", stamped by
            # _start_progress_run) is still pending/running: no error, no
            # next_run advance — the first tick advances cadence when it
            # completes, and reap_stale_runs unsticks a truly orphaned doc.
            # Echo is exempt: its batch dispatch has its own dedup
            # and this guard must not interfere with the shared batch run.
            # Fail-open: an ES blip in the probe must not stop schedules.
            if not is_echo:
                try:
                    from lib.import_progress_service import ImportProgressService

                    inflight = ImportProgressService.find_active_run_id(f"schedule:{src['schedule_id']}")
                except Exception:  # noqa: BLE001 — probe is best-effort
                    inflight = None
                if inflight:
                    logging.info(
                        "ReportScheduleService.run_due: schedule %s has run %s still in flight; "
                        "skipping this tick.",
                        src["schedule_id"],
                        inflight,
                    )
                    results.append(
                        {
                            "schedule_id": src["schedule_id"],
                            "snapshot_id": None,
                            "skipped": "import_in_flight",
                            "in_flight_run_id": inflight,
                        }
                    )
                    continue
            try:
                r = ReportScheduleService._execute_one(
                    es,
                    src,
                    now=now,
                    predispatched_import=batch_echo if is_echo else None,
                    # run_due is headless, so it WAITS inline for the import
                    # to land before building (run_now defers instead).
                    wait_for_import=True,
                )
            except Exception as e:  # noqa: BLE001 — batch isolation boundary
                logging.error("ReportScheduleService.run_due: %s blew up: %s", src["schedule_id"], e)
                r = {"schedule_id": src["schedule_id"], "snapshot_id": None, "error": "Run failed; see the server log."}
            if r.get("error"):
                errors += 1
            results.append(r)
        return {"due": len(hits), "ran": len(results), "errors": errors, "swept": swept, "results": results}

    @staticmethod
    def run_now(schedule_id: str, *, scope=None) -> dict:
        """Execute a single schedule immediately, ignoring next_run (the
        "Run now" AC). Honors ``scope`` when provided. Active OR paused — Run
        now is an explicit user action, so a paused schedule is allowed to run
        on demand without resuming its cadence.
        """
        es = get_elasticsearch_client()
        if es is None:
            return {"success": False, "error": "Could not connect to Elasticsearch"}
        schedule = ReportScheduleService.get_schedule(schedule_id)
        if not schedule:
            return {"success": False, "error": "schedule not found"}
        if scope is not None and not ReportScheduleService._scope_allows(
            scope, schedule.get("org_id", ""), schedule.get("team_id", "")
        ):
            return {"success": False, "error": "not authorized for this schedule"}
        # run_now must NOT block the HTTP request, so wait_for_import=False:
        # when an import is dispatched, the snapshot build is DEFERRED (run_state stays
        # phase=importing; snapshot_id is None) and a later run_due poll builds it once
        # the import lands. A run with no import to wait on still builds inline here, so
        # snapshot_id is populated for those. Either way the import is fire-and-forget
        # from the request's perspective; the progress bar polls de_import_progress.
        r = ReportScheduleService._execute_one(es, schedule, wait_for_import=False)
        if r.get("error"):
            return {"success": False, "error": r["error"], "schedule_id": schedule_id}
        return {
            "success": True,
            "schedule_id": schedule_id,
            "snapshot_id": r.get("snapshot_id"),
            "import_dispatched": bool(r.get("import_dispatched")),
            "connector_id": schedule.get("connector_id") or "",
        }

    # Days a backfill may cover, counting back from today (ticket cap).
    BACKFILL_MAX_DAYS = 365

    @staticmethod
    def backfill_now(
        schedule_id: str,
        date_range: dict,
        *,
        overwrite: bool = False,
        requested_by: str = "",
        scope=None,
    ) -> dict:
        """start a one-time, scoped historical backfill.

        Fires an IMPORT-ONLY scoped pull over the user-selected ``date_range``
        ({from,to} ISO dates, inclusive span capped at BACKFILL_MAX_DAYS) for the
        schedule's connector + teams, reusing the exact dispatch path a scheduled
        run uses (``_trigger_scoped_import``): the importer's per-source billing gate
        (``billing_entitlement.source_billing_block`` — lapsed owners blocked,
        over-cap sources skipped) applies UNCHANGED. Unlike Run-now this builds NO
        snapshot — a backfill refreshes historical data, it does not produce a
        report. Progress + Cancel reuse the schedule's ``last_run_state`` /
        ``progress_run_id`` machinery.

        Meta is now supported (the route no longer rejects it). Two knobs:

        * ``overwrite`` — when True the FB importer re-writes existing days
          (``--force_refetch``); when False (default) days that already have data are
          SKIPPED. Non-FB connectors have no force path, so overwrite is a no-op for
          them (the route only exposes the checkbox for Meta).
        * Meta backfills always run in bail-out mode: a Meta rate limit STOPS the run
          gracefully and keeps the data written so far, so day-by-day backfills can be
          strung together until the full history is in — without an approved FB app.

        A backfill RUN record is written to the snapshot index (record_type=
        "backfill") so the History modal can note what was backfilled.

        Returns ``{success, schedule_id, progress_run_id, import_dispatched,
        connector_id, window, overwrite}`` or ``{success: False, error}``.
        """
        es = get_elasticsearch_client()
        if es is None:
            return {"success": False, "error": "Could not connect to Elasticsearch"}
        schedule = ReportScheduleService.get_schedule(schedule_id)
        if not schedule:
            return {"success": False, "error": "schedule not found"}
        if scope is not None and not ReportScheduleService._scope_allows(
            scope, schedule.get("org_id", ""), schedule.get("team_id", "")
        ):
            return {"success": False, "error": "not authorized for this schedule"}

        connector_id = (schedule.get("connector_id") or "").strip()
        if not connector_id:
            return {"success": False, "error": "this schedule has no connector to backfill"}

        # validate the user-selected from/to range server-side (the route
        # validates too; this is defence-in-depth so a caller reaching the service
        # directly can't exceed the window or pass a reversed/future range either).
        now = _now()
        today = now.date()
        frm = ((date_range or {}).get("from") or "").strip()
        to = ((date_range or {}).get("to") or "").strip()
        try:
            d_from = datetime.strptime(frm, "%Y-%m-%d").date()
            d_to = datetime.strptime(to, "%Y-%m-%d").date()
        except (TypeError, ValueError):
            return {"success": False, "error": "from and to must be YYYY-MM-DD dates"}
        if d_from > d_to:
            return {"success": False, "error": "from date must be on or before to date"}
        if d_to > today:
            return {"success": False, "error": "to date cannot be in the future"}
        span_days = (d_to - d_from).days + 1
        if span_days > ReportScheduleService.BACKFILL_MAX_DAYS:
            return {
                "success": False,
                "error": f"date range must be {ReportScheduleService.BACKFILL_MAX_DAYS} days or fewer",
            }
        # Normalize to the {from,to} shape the importer + history record expect.
        date_range = {"from": d_from.isoformat(), "to": d_to.isoformat()}

        team_esids = ReportScheduleService._schedule_team_esids(schedule)
        if not team_esids:
            return {"success": False, "error": "this schedule has no teams to backfill"}

        # Meta runs in bail-out mode (rate limit → stop + keep partial);
        # overwrite is FB-only. Non-FB connectors ignore both flags.
        is_meta = connector_id == "meta_ads"
        backfill_bailout = is_meta

        # Seed run-state so the card's progress bar + the cancel route engage,
        # exactly as a Run-now does.
        run_state = {
            "phase": RUN_PHASE_IMPORTING,
            "started_at": now.isoformat(),
            "finished_at": None,
            "snapshot_id": None,
            "import_dispatched": False,
            "connector_id": connector_id,
            "progress_run_id": None,
            "import_pids": [],
            "import_lock_scope": "",
            "error": None,
            "import_errors": [],
            "import_errors_run_id": None,
            # mark this in-flight state as a backfill so the UI can label
            # it (and so a future reader can distinguish it from a report run).
            "is_backfill": True,
        }
        ReportScheduleService._set_run_state(es, schedule_id, run_state)

        range_days = ReportScheduleService._date_range_days(date_range)
        progress_run_id = ReportScheduleService._start_progress_run(connector_id, schedule_id, range_days=range_days)
        if progress_run_id:
            run_state["progress_run_id"] = progress_run_id
            ReportScheduleService._set_run_state(es, schedule_id, run_state)

        imp = ReportScheduleService._trigger_scoped_import(
            es,
            connector_id,
            team_esids,
            date_range,
            progress_run_id=progress_run_id,
            overwrite=bool(overwrite),
            backfill_bailout=backfill_bailout,
            is_backfill=True,
        )
        dispatched = bool(imp.get("dispatched"))
        run_state["import_dispatched"] = dispatched
        run_state["import_pids"] = list(imp.get("pids") or [])
        run_state["import_lock_scope"] = imp.get("lock_scope") or ""

        if not dispatched:
            # Nothing was dispatched (no scope values resolved, or connector not
            # team-scopable). Resolve the bar immediately + surface the reason.
            if progress_run_id:
                ReportScheduleService._fail_progress_run(
                    progress_run_id, error=imp.get("skipped_reason") or "backfill not dispatched"
                )
                run_state["progress_run_id"] = None
            run_state["phase"] = RUN_PHASE_FAILED
            run_state["finished_at"] = _now().isoformat()
            run_state["error"] = imp.get("skipped_reason") or "backfill not dispatched"
            run_state["import_pids"] = []
            ReportScheduleService._set_run_state(es, schedule_id, run_state)
            return {
                "success": False,
                "error": run_state["error"],
                "schedule_id": schedule_id,
                "connector_id": connector_id,
            }

        # record the backfill in history (best-effort, never fatal).
        # ``days`` is now the inclusive span of the user's range (kept for
        # the History modal's existing "N days" label); overwrite is recorded too.
        history_id = ReportScheduleService.write_backfill_history_record(
            es,
            schedule,
            days=span_days,
            date_range=date_range,
            team_esids=team_esids,
            requested_by=requested_by,
            overwrite=bool(overwrite),
            now=now,
        )
        # stash the history record id + requested window + team scope on the
        # run-state so _finalize_backfill (poll/cron) can update the SAME record with
        # the actual covered range + bail-out/error outcome once the import settles.
        if history_id:
            run_state["backfill_history_id"] = history_id
            run_state["backfill_requested_range"] = dict(date_range)
            run_state["backfill_team_esids"] = list(team_esids)
            run_state["connector_id"] = connector_id
            ReportScheduleService._set_run_state(es, schedule_id, run_state)
            # follow-up — backfill history records piled up unbounded
            # (prune_snapshots only ran on report-snapshot writes). Honor the
            # schedule's delivery.snapshot_keep for backfill records too, pruned
            # on their own independent count so they never evict report
            # snapshots. Best-effort — a prune failure must not fail the backfill.
            try:
                keep = clamp_snapshot_keep((schedule.get("delivery") or {}).get("snapshot_keep"))
                ReportScheduleService.prune_snapshots(es, schedule_id, keep=keep, record_type="backfill")
            except Exception as e:  # noqa: BLE001 — prune is advisory, never fatal to the backfill
                logging.warning("backfill prune failed for %s: %s", schedule_id, e)

        return {
            "success": True,
            "schedule_id": schedule_id,
            "progress_run_id": progress_run_id or "",
            "import_dispatched": dispatched,
            "connector_id": connector_id,
            "window": date_range,
            "overwrite": bool(overwrite),
        }

    @staticmethod
    def write_backfill_history_record(
        es,
        schedule: dict,
        *,
        days: int,
        date_range: dict,
        team_esids: list,
        requested_by: str = "",
        overwrite: bool = False,
        now: datetime | None = None,
    ) -> str | None:
        """write a backfill RUN record to the snapshot index so the
        History modal can note what was backfilled.

        record_type="backfill" + a ``backfill`` sub-object; denormalises org_id/
        team_id like a snapshot so it stays scope-filterable in ``list_snapshots``
        after its schedule is deleted. Best-effort: a write failure is logged and
        swallowed (the import already dispatched — history is advisory)."""
        now = now or _now()
        doc = {
            "schedule_id": schedule.get("schedule_id") or "",
            "org_id": schedule.get("org_id", ""),
            "team_id": schedule.get("team_id", ""),
            "generated_at": now.isoformat(),
            "date_range": dict(date_range or {}),
            "record_type": "backfill",
            "backfill": {
                "days": int(days),
                "connector_id": (schedule.get("connector_id") or "").strip(),
                "team_esids": list(team_esids or []),
                "requested_by": requested_by or "",
                # whether this backfill overwrote existing data.
                "overwrite": bool(overwrite),
            },
        }
        try:
            res = es.index(index=INDEX_REPORT_SNAPSHOTS, body=doc, refresh="wait_for")
            return res.get("_id")
        except Exception as e:  # noqa: BLE001 — history record is advisory, never fatal
            logging.warning(
                "write_backfill_history_record failed for %s: %s",
                schedule.get("schedule_id") or "",
                e,
            )
            return None

    # ─────────────────────────────────────────────────────────────────────────
    # Cancel — belt-and-suspenders: persisted cancel flag + killpg
    # ─────────────────────────────────────────────────────────────────────────

    # How long to wait for SIGTERM'd process groups to exit before escalating to
    # SIGKILL. Short — the importer's cancel checkpoint exits between API batches,
    # and a wedged process should be killed promptly so the lock releases.
    _KILL_GRACE_SECONDS = 3.0
    # After SIGKILL, how long to keep re-checking before declaring a survivor.
    # SIGKILL on a process blocked in a long syscall (a paged FB Graph read) is not
    # instantaneous — the kernel reaps it when the call returns. Without this settle
    # the verify raced the reap and falsely reported kill_confirmed=False (staging
    # 2026-06-18). 5s comfortably covers a Graph call returning + reaping.
    _POST_KILL_SETTLE_SECONDS = 5.0

    @staticmethod
    def _pgid_alive(pgid: int) -> bool:
        """True iff process group ``pgid`` still has at least one NON-zombie member.

        we scan ``/proc/<pid>/stat`` rather than ``os.killpg(pgid, 0)``
        because a SIGKILL'd leader becomes a ZOMBIE (reaped lazily by its parent),
        and a zombie's pgid still answers ``killpg(.,0)`` — so the signal-0 probe
        reported a successfully-killed group as "alive" and falsely flipped
        kill_confirmed to False on every successful cancel (found in the staging
        smoke 2026-06-18). A process is dead-for-our-purposes when it's gone OR a
        zombie (stat state 'Z'/'X'): it holds no resources and is moments from being
        reaped. Only a live (non-zombie) member keeps the group alive.

        stat layout: ``pid (comm) state ppid pgrp ...`` — comm can contain spaces
        and ')' so we split on the LAST ')'; after it, field[0]=state, field[2]=pgrp.
        Returns False on /proc-less hosts (Windows dev) — nothing to verify there
        (killpg is unsupported anyway)."""
        if not os.path.isdir("/proc"):
            return False
        try:
            entries = os.listdir("/proc")
        except OSError:
            return False
        for name in entries:
            if not name.isdigit():
                continue
            try:
                with open("/proc/%s/stat" % name, "r") as fh:
                    data = fh.read()
            except (OSError, IOError):
                continue  # process vanished mid-scan — fine
            rparen = data.rfind(")")
            if rparen == -1:
                continue
            fields = data[rparen + 2 :].split()
            if len(fields) < 3:
                continue
            state = fields[0]
            try:
                pgrp = int(fields[2])
            except ValueError:
                continue
            if pgrp == pgid and state not in ("Z", "X", "x"):
                return True
        return False

    @staticmethod
    def _kill_pgids(pids: list) -> dict:
        """SIGTERM each recorded process group, wait a grace period, escalate to
        SIGKILL any survivors, then report whether all groups are confirmed gone.

        Mirrors ImportService.cancel_import's process-group kill: signalling
        the group (negative pgid) reaches the whole import subtree
        (import.py → run_daily_imports → get_campaign_data/get_conversations), so a
        cancel can't orphan the long paged API calls. Returns ``{kill_confirmed,
        signalled, killpg_supported}``: ``kill_confirmed`` is True only when every
        group is verified dead (or there was nothing to kill) — a group we couldn't
        confirm dead leaves it False so the caller surfaces that rather than lying
        "cancelled". On Windows dev (no killpg) ``killpg_supported`` is False and
        ``kill_confirmed`` is False when there were pids to kill (the flag is the
        only lever there)."""
        pgids = sorted({int(p.get("pgid")) for p in (pids or []) if p and p.get("pgid")})
        if not pgids:
            return {"kill_confirmed": True, "signalled": [], "killpg_supported": hasattr(os, "killpg")}
        if not hasattr(os, "killpg"):
            logging.warning("_kill_pgids: os.killpg unavailable (non-POSIX); cannot reap %s", pgids)
            return {"kill_confirmed": False, "signalled": [], "killpg_supported": False}

        signalled = []
        for pgid in pgids:
            try:
                os.killpg(pgid, signal.SIGTERM)
                signalled.append(pgid)
            except ProcessLookupError:
                pass  # already gone — fine
            except OSError as e:
                logging.warning("_kill_pgids: SIGTERM pgid=%s failed: %s", pgid, e)

        # Grace, then SIGKILL anything still alive.
        deadline = time.monotonic() + ReportScheduleService._KILL_GRACE_SECONDS
        while time.monotonic() < deadline:
            if not any(ReportScheduleService._pgid_alive(p) for p in pgids):
                break
            time.sleep(0.1)
        # SIGKILL is POSIX-only (same platforms as os.killpg). getattr-guard so a
        # mock-killpg test path on a SIGKILL-less host can't AttributeError.
        sigkill = getattr(signal, "SIGKILL", signal.SIGTERM)
        for pgid in pgids:
            if ReportScheduleService._pgid_alive(pgid):
                try:
                    os.killpg(pgid, sigkill)
                except OSError as e:
                    logging.warning("_kill_pgids: SIGKILL pgid=%s failed: %s", pgid, e)

        # (staging 2026-06-18) — SIGKILL is NOT instantaneous when the
        # target is blocked in a syscall (a long FB Graph API read): the kernel
        # delivers the signal when the call returns, then the parent reaps it. So a
        # check IMMEDIATELY after killpg can still see it alive and falsely report
        # kill_confirmed=False — even though the process dies a moment later. Wait a
        # short post-SIGKILL settle, re-checking each tick, before declaring failure.
        post_deadline = time.monotonic() + ReportScheduleService._POST_KILL_SETTLE_SECONDS
        survivors = [p for p in pgids if ReportScheduleService._pgid_alive(p)]
        while survivors and time.monotonic() < post_deadline:
            time.sleep(0.1)
            survivors = [p for p in pgids if ReportScheduleService._pgid_alive(p)]
        if survivors:
            logging.error("_kill_pgids: process groups still alive after SIGKILL + settle: %s", survivors)
        return {"kill_confirmed": not survivors, "signalled": signalled, "killpg_supported": True}

    @staticmethod
    def cancel_run(schedule_id: str, *, scope=None) -> dict:
        """Cancel a schedule's in-flight run — belt-and-suspenders.

        1. Persist the cancel: flip ``last_run_state.phase`` to ``cancelled`` and set
           ``cancel_requested=True``. The importer polls this via
           ``ImportProgressService.is_cancelling`` at its checkpoints and
           exits cleanly between API batches — catching the case where a raw signal
           alone would race a mid-batch importer.
        2. Flip the progress doc to ``cancelling`` (the flag the importer actually
           reads) when this run wired a ``progress_run_id``.
        3. ``os.killpg`` the persisted process group(s) (SIGTERM → grace → SIGKILL)
           so the whole import subtree dies even if it's mid-API-call and won't reach
           a checkpoint — this is the "suspenders".
        4. Verify: report ``kill_confirmed`` (all groups dead) so a kill that
           couldn't be confirmed is surfaced, not hidden behind a "cancelled" badge.

        Returns ``{success, schedule_id, kill_confirmed, killpg_supported,
        import_dispatched}`` or ``{success: False, error}``. Honors ``scope`` like the
        other mutators. A run that's already terminal (no live phase) is a no-op
        success (nothing to cancel)."""
        es = get_elasticsearch_client()
        if es is None:
            return {"success": False, "error": "Could not connect to Elasticsearch"}
        schedule = ReportScheduleService.get_schedule(schedule_id)
        if not schedule:
            return {"success": False, "error": "schedule not found"}
        if scope is not None and not ReportScheduleService._scope_allows(
            scope, schedule.get("org_id", ""), schedule.get("team_id", "")
        ):
            return {"success": False, "error": "not authorized for this schedule"}

        run_state = dict(schedule.get("last_run_state") or {})
        phase = run_state.get("phase")
        # Only importing / generating / done-with-tail are cancellable; a run that
        # already reached a terminal state has nothing in flight.
        if phase not in (RUN_PHASE_IMPORTING, RUN_PHASE_GENERATING, RUN_PHASE_DONE):
            return {
                "success": True,
                "schedule_id": schedule_id,
                "kill_confirmed": True,
                "killpg_supported": hasattr(os, "killpg"),
                "import_dispatched": bool(run_state.get("import_dispatched")),
                "note": "no run in flight",
            }

        # (1) Persist the cancel intent + terminal phase so the bar resolves and the
        # importer's checkpoint sees the flag. Stamp before killing so a crash mid-
        # kill still leaves a cancelled record.
        run_state["phase"] = RUN_PHASE_CANCELLED
        run_state["cancel_requested"] = True
        run_state["finished_at"] = _now().isoformat()
        pids = list(run_state.get("import_pids") or [])
        run_state["import_pids"] = []  # terminal — drop so a recycled pid isn't re-killed
        ReportScheduleService._set_run_state(es, schedule_id, run_state)

        # (2) Flip the progress doc the importer actually polls (belt).
        progress_run_id = (schedule.get("last_run_state") or {}).get("progress_run_id") or ""
        if progress_run_id:
            try:
                from lib.import_progress_service import ImportProgressService

                ImportProgressService.cancel_run(progress_run_id)
            except Exception as exc:  # noqa: BLE001 — boundary; the killpg below still reaps it
                logging.warning("cancel_run: progress cancel_run(%r) failed: %s", progress_run_id, exc)

        # (3)+(4) killpg the import subtree (suspenders) and verify death.
        kill = ReportScheduleService._kill_pgids(pids)

        # A group we SIGKILL'd is going down even if our verify raced the reap — so
        # for cleanup we trust "we signalled it" (kill["signalled"]), which is wider
        # than "verified dead" (kill["kill_confirmed"]). A group already dead by the
        # time cancel arrived (kill_confirmed, nothing left alive) also counts as
        # reached. The ONLY case we must NOT clean up is when killpg couldn't reach a
        # STILL-ALIVE group (no signal landed, not confirmed dead → wrong namespace /
        # permission / killpg unsupported): a truly-live importer still legitimately
        # holds its lock, and yanking it would let a second import race the survivor.
        reached = (not pids) or bool(kill.get("signalled")) or kill["kill_confirmed"]

        # (5) Release the connector's import lock + stamp the progress doc terminal.
        # A SIGKILL'd importer never runs its own clean release/stamp, so without
        # this the per-connector lock sits held AND the progress doc sticks at
        # ``cancelling`` until the 30-min reaper — blocking the next import and
        # hanging any bar reading that doc (found via the UI cancel on staging
        # 2026-06-18, where a mid-Graph-call importer was SIGKILL'd before it could
        # self-clean). Do this whenever we REACHED the group (signalled it), not only
        # when verified dead.
        connector_id = schedule.get("connector_id") or ""
        lock_key = ReportScheduleService.import_lock_connector(connector_id)
        # the per-scope lock key this run acquired (stamped on run_state
        # at dispatch). "" for connector-global connectors, so force_release clears
        # the connector-global doc exactly as before; for google_ads / ga4 it
        # clears the per-scope doc the killed importer was holding.
        lock_scope = run_state.get("import_lock_scope") or ""
        if reached and pids and lock_key:
            try:
                from lib.import_lock_service import ImportLockService

                ImportLockService.force_release(lock_key, scope_id=lock_scope)
            except Exception as exc:  # noqa: BLE001 — boundary; a stuck lock self-expires
                logging.warning("cancel_run: force_release(%r, scope=%r) failed: %s", lock_key, lock_scope, exc)
        if reached and pids and progress_run_id:
            # cancel_run only flipped it to ``cancelling``; the SIGKILL'd importer
            # won't reach its checkpoint to stamp ``cancelled``, so finalize it here.
            try:
                from lib.import_progress_service import ImportProgressService

                ImportProgressService.complete_run(run_id=progress_run_id, status="cancelled")
            except Exception as exc:  # noqa: BLE001 — boundary
                logging.warning("cancel_run: complete_run(cancelled) %r failed: %s", progress_run_id, exc)

        # Honesty: only flag a degraded cancel when we could NOT reach the group at
        # all (killpg unsupported / failed). A signalled-but-not-yet-reaped group is
        # going down and has been cleaned up, so it is NOT degraded — surfacing
        # "couldn't confirm" there would be the false-negative the UI showed.
        if pids and not reached:
            run_state["error"] = "cancel requested but the import process could not be signalled to stop"
            ReportScheduleService._set_run_state(es, schedule_id, run_state)

        return {
            "success": True,
            "schedule_id": schedule_id,
            "kill_confirmed": kill["kill_confirmed"],
            "kill_reached": reached,
            "killpg_supported": kill["killpg_supported"],
            "import_dispatched": bool(run_state.get("import_dispatched")),
        }

    @staticmethod
    def import_lock_connector(connector_id: str) -> str | None:
        """The cluster-lock key a schedule's background import holds while
        running, or None when there's no live signal to poll (the connector
        has no scoped import, or its importer takes no lock — youtube)."""
        source = (_CONNECTOR_IMPORT_DISPATCH.get(connector_id) or {}).get("source") or ""
        return source if source in _LOCKED_IMPORT_SOURCES else None

    @staticmethod
    def _connector_supports_progress(connector_id: str) -> bool:
        """True iff this connector's importer honors --progress_run_id
        and writes a live de_import_progress doc the Run-now bar can read."""
        source = (_CONNECTOR_IMPORT_DISPATCH.get(connector_id) or {}).get("source") or ""
        return source in _PROGRESS_IMPORT_SOURCES

    @staticmethod
    def _progress_total(connector_id: str, range_days: int) -> int:
        """the denominator for a connector's progress percentage.

        Most importers report progress against the date-range day count (the FB
        per-day loop, the google_ads/ga4 milestone bumps that are fractions of
        days_total). YouTube has no per-date loop — it runs 3 fixed sequential
        steps (fetch/index/archive) and bumps days_fetched to step 1/2/3 — so its
        denominator is the step count, not the date range. Returns the value to
        pass as start_run's ``days_total``.
        """
        source = (_CONNECTOR_IMPORT_DISPATCH.get(connector_id) or {}).get("source") or ""
        # Phase 3 — bitly mirrors YouTube's fixed 3-step phase
        # progress (fetch/index/archive; _BITLY_STEPS_TOTAL in its importer).
        # tiktok too (_TIKTOK_STEPS_TOTAL).
        if source in ("youtube", "bitly", "tiktok"):
            return _YOUTUBE_PROGRESS_STEPS
        return range_days

    @staticmethod
    def _start_progress_run(connector_id: str, schedule_id: str, *, range_days: int) -> str | None:
        """mint a de_import_progress doc for a Run-now import so the
        progress bar can show a real percentage + stage message.

        Keyed on a synthetic ``connection_esid`` of ``schedule:<schedule_id>``
        (mirrors the importer's existing ``legacy_team:<esid>`` synthetic key) so
        ImportProgressService.start_run's per-connection idempotency dedupes rapid
        re-clicks of Run-now on the same schedule. ``days_total`` is the connector's
        progress denominator (date-range days, or fixed step count for YouTube —
        see _progress_total). Returns the run_id, or None when the connector has no
        progress-capable importer, the denominator is unknown, or ES is down (the
        bar then falls back to the coarse lock signal).
        """
        if not ReportScheduleService._connector_supports_progress(connector_id):
            return None
        days_total = ReportScheduleService._progress_total(connector_id, range_days)
        if days_total <= 0:
            return None
        try:
            from lib.import_progress_service import ImportProgressService

            return ImportProgressService.start_run(
                username=f"scheduler:{schedule_id}",
                connection_esid=f"schedule:{schedule_id}",
                connector_id=connector_id,
                days_total=days_total,
            )
        except Exception as exc:  # noqa: BLE001 — boundary; progress is a UX nicety
            logging.warning("_start_progress_run failed for %s/%s: %s", connector_id, schedule_id, exc)
            return None

    @staticmethod
    def _fail_progress_run(run_id: str, *, error: str) -> None:
        """stamp a minted-but-never-dispatched progress doc terminal so
        the bar resolves immediately instead of polling a doc that will never
        advance (the resolver-skip case for google_ads / ga4). Soft-fail."""
        if not run_id:
            return
        try:
            from lib.import_progress_service import ImportProgressService

            ImportProgressService.complete_run(run_id=run_id, status="failed", error=error)
        except Exception as exc:  # noqa: BLE001 — boundary
            logging.warning("_fail_progress_run %r: %s", run_id, exc)

    @staticmethod
    def import_progress(run_id: str) -> dict | None:
        """live progress for a Run-now import, read from the
        de_import_progress doc the importer writes.

        Returns a UI-ready dict ``{status, phase, days_total, days_fetched,
        percent}`` or None when the run_id is empty / unknown / ES is down. The
        caller (api_import_status) maps None to the lock-only fallback.
        """
        if not run_id:
            return None
        try:
            from lib.import_progress_service import ImportProgressService

            doc = ImportProgressService.get_run(run_id)
        except Exception as exc:  # noqa: BLE001 — boundary
            logging.warning("import_progress get_run(%r) raised: %s", run_id, exc)
            return None
        if not doc:
            return None
        days_total = int(doc.get("days_total") or 0)
        days_fetched = int(doc.get("days_fetched") or 0)
        # Percent is the per-day fetch fraction (the ad-insights loop's signal).
        # Post-loop stages (page/instagram) advance ``phase`` without bumping the
        # day counter, so we hold percent at its last value and let the phase
        # message carry the "still working" signal — never let it read 100% until
        # the run is terminal.
        percent = 0
        if days_total > 0:
            percent = min(99, int(round(100.0 * days_fetched / days_total)))
        status = doc.get("status") or "pending"
        if status == "succeeded":
            percent = 100
        from lib.import_slots import status_label

        return {
            "status": status,
            "phase": doc.get("phase") or "",
            "days_total": days_total,
            "days_fetched": days_fetched,
            "percent": percent,
            "error": doc.get("error") or "",
            # waiting for a global import slot: the bar reads
            # status_label ("queued (#2)") instead of a stage message.
            "queue_position": doc.get("queue_position"),
            "status_label": status_label(status, doc.get("queue_position")),
        }
