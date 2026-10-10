"""transparent de-duplicated-field routing (Phase R1 spike).

The redesign: a user builds a chart on ONE data view (``fb_ad_insights``) and
selects ``insight_reach`` like any other field. Additive fields (spend,
impressions, ...) are summed from the daily index; de-duplicated fields
(reach, frequency, recall) are read with ``max`` from a SEPARATE physical
window/dedup index. Neither the user nor the chart config picks a window data
view — the split is driven by the central ``DEDUP_WINDOW_REGISTRY`` and the two
ES responses are stitched back into one result the existing ``transform`` can
render.

This module is the SPIKE for the load-bearing unknown: can the split + stitch be
done cleanly in the app layer (ES has no server-side join), and what does it
cost? It is intentionally NOT yet wired into the live chart-render endpoints —
that is Phase R2+. The two functions here are pure (config in / dicts out) so
they can be unit-tested against synthetic ES responses and later dropped into the
``build_query`` → ``es.search`` → ``transform`` chokepoint.

Contract it must preserve (the canonical single-index path, see
public_report/routes.py):

    query_body = build_query(config)
    es_response = es.search(index=data_view, body=query_body)
    transform(es_response, config)

The routed version (R2 will wire this) becomes:

    daily_cfg, dedup_cfg, plan = split_config_by_index(config)
    daily_resp  = es.search(index=daily_index,  body=build_query(daily_cfg))
    dedup_resp  = es.search(index=dedup_index,  body=build_query(dedup_cfg))
    merged = stitch_table_responses(daily_resp, dedup_resp, plan)
    transform(merged, config)        # config unchanged — user sees one chart
"""

from __future__ import annotations

import logging
import os
import re
from datetime import date, datetime, timedelta, timezone

try:
    from web.services.es_query_builder import (
        DEDUP_WINDOW_REGISTRY,
        _WINDOW_SUFFIX_TO_SOURCE,
        source_from_data_view,
        dedup_grain_field,
        dedup_grain_equivalents,
        group_by_grain_value,
        window_carried_fields,
        source_reads_scalar_from_window,
        window_only_grain_value,
        window_only_grain_fields,
        is_window_summable_field,
        excluded_grains_by_default,
        grain_is_scope_dependent,
        _safe_agg_key,
        build_query,
    )
except ImportError:  # pragma: no cover - import path parity with the rest of the package
    from services.es_query_builder import (
        DEDUP_WINDOW_REGISTRY,
        _WINDOW_SUFFIX_TO_SOURCE,
        source_from_data_view,
        dedup_grain_field,
        dedup_grain_equivalents,
        group_by_grain_value,
        window_carried_fields,
        source_reads_scalar_from_window,
        window_only_grain_value,
        window_only_grain_fields,
        is_window_summable_field,
        excluded_grains_by_default,
        grain_is_scope_dependent,
        _safe_agg_key,
        build_query,
    )


# every stamped grain that coexists with the (default) ad-set grain in
# one fb_insights_window index. The ad-set path stays grain-UNFILTERED for legacy
# unstamped docs, so these must be excluded explicitly or a query at
# the ad-set grain also matches its child/parallel grains' docs.
# the per-source lists live in es_query_builder._EXCLUDED_GRAINS_BY_DEFAULT
# (fb_insights: this tuple; ga_metrics: the event grain). Kept under its
# name for the FB call sites and tests.
_NON_ADSET_GRAINS = excluded_grains_by_default("fb_insights")

# The base sources whose window index actually STAMPS multiple grains (the
# doc families). Only these get the exclusion:
# GA4/IG window docs carry no grain field, and uncharacterized sources keep the
# fail-open contract (never touch filters for a source we haven't
# mapped).
# derived from the per-source exclusion table so a source that stamps a
# new grain (ga_metrics: event) is enrolled in one place.
_GRAIN_STAMPED_WINDOW_SOURCES = frozenset(src for (src, _f) in DEDUP_WINDOW_REGISTRY if excluded_grains_by_default(src))


def _exclude_non_adset_grains(filters, grains=None):
    """Return ``filters`` plus ``grain != <g>`` clauses for every stamped
    non-ad-set grain — unless a grain filter is already present.

    the ad-set-grain dedup query is deliberately grain-unfiltered so
    legacy unstamped docs (which predate the grain field) stay readable
    . That was safe while every registered window agg was ``max``: a
    child-grain doc's value never exceeds its parent's, so blending grains could
    not change a max. made messaging conversations ``sum`` — and the
     (ad) / (asset) docs carry adset identity, so the
    unfiltered sum counted every conversation once per grain (one prod team:
    708 rendered vs Ads Manager's 236 — exactly the 3 grains carrying the
    field). ``neq`` compiles to ``must_not term``, which still matches docs
    with NO grain field, so legacy unstamped ad-set docs keep flowing.
    """
    out = list(filters or [])
    if any(isinstance(f, dict) and f.get("field") == "grain" for f in out):
        return out
    out.extend({"field": "grain", "op": "neq", "value": g} for g in (grains if grains is not None else _NON_ADSET_GRAINS))
    return out


def _base_source_of(data_view):
    """Return the registry BASE source for a chart's data_view, whether the chart
    is on the daily view (``*_fb_insights`` -> ``fb_insights``) OR already on the
    window view (``*_fb_insights_window`` -> ``fb_insights``).

    the Ad Sets table is built on the WINDOW view at the chart level
    (one data_view for all columns). split_config_by_index originally resolved the
    source straight from the suffix, so ``fb_insights_window`` never matched the
    registry (keyed by ``fb_insights``) and routing was a no-op for that chart.
    Map a window suffix back to its base source so dedup fields are recognized
    regardless of which view the chart sits on.
    """
    suffix = source_from_data_view(data_view)
    return _WINDOW_SUFFIX_TO_SOURCE.get(suffix, suffix)


def _daily_data_view(data_view):
    """Return the DAILY data_view for a chart, stripping a ``_window`` suffix if
    present so additive fields read from the per-day index. A chart already on the
    daily view is returned unchanged."""
    suffix = source_from_data_view(data_view)
    base = _WINDOW_SUFFIX_TO_SOURCE.get(suffix)
    if base is None:
        return data_view  # already a daily/base view
    dv = data_view or ""
    if "__" in dv:
        prefix = dv.rsplit("__", 1)[0]
        return f"{prefix}__{base}"
    # Wildcard form (``*_fb_insights_window`` -> ``*_fb_insights``) or bare.
    if dv.endswith(suffix):
        return dv[: -len(suffix)] + base
    return base


def dedup_routing_enabled():
    """R2 feature flag (default OFF).

    The routed read path (split → daily + dedup queries → stitch) only runs when
    ``DEDUP_ROUTING_ENABLED`` is truthy in the environment. OFF → callers take
    the unchanged single-query path, so wiring this in is a no-op until enabled.
    """
    return os.environ.get("DEDUP_ROUTING_ENABLED", "").strip().lower() in ("1", "true", "yes", "on")


# explained-empty-cell reason codes + their copy. A dedup cell with
# no value carries one of these so the UI renders an EXPLAINED empty state, never
# a bare blank and never a silent daily-sum fallback. Copy is centralized here so
# the server and the (batched) UI phase share one source of truth.
#
#   not_built  — the dedup field's source has no window importer yet (registry
#                status=planned, e.g. IG/GA4). Column-level.
#   no_window  — this team has NO pulled windows at all (never pulled, or a
#                shared/page-filtered account skipped entirely by the importer).
#                Column-level.
#   no_data    — the team HAS windows but this group-by value (ad-set) has no doc
#                in the snapped window (importer skips no-spend rows; or the ad-set
#                didn't run in that window). Cell-level.
#   unknown    — fallback.
#
# NOT distinguished yet: shared_account vs no_spend at the CELL. The importer logs
# the shared-account skip but writes no per-run marker, so the read path can't tell
# "this account is shared" from "this ad-set didn't spend." Surfaced as no_data /
# no_window for now; true shared_account detection needs an importer-side marker
# (follow-up KAN-TBD). Do NOT guess — an honest "no data for this range" beats a
# wrong reason.
# `multi_grain` is the ONE reason that carries a VALUE, not a blank.
# When a chart's scope spans MORE THAN ONE dedup grain (2+ GA4 properties, 2+ FB
# ad-sets), a true deduplicated figure is impossible (you can't dedup unique
# users / reach across grains without the raw per-user IDs). So instead of
# blanking the cell we KEEP the additive daily sum and LABEL it as the upper
# bound it provably is (summed per-day unique counts re-count returning users,
# so the sum >= the true dedup count, never below). "approx upper bound — not
# deduplicated" and "the daily sum" denote the SAME number; the label is the only
# thing distinguishing this honest ceiling from the silent ~3.72x inflation the
# line exists to kill. DECISIONS.md 2026-06-29. The fix for a real
# number is to narrow scope to ONE grain (GA4: select a property).
DEDUP_EMPTY_REASONS = {
    "not_built": "Deduplicated data isn't available for this source yet.",
    "no_window": "No deduplicated data has been pulled for this account.",
    "no_data": "No deduplicated data for this row in the selected window.",
    # decision (2026-08-26): a pulled window that does not even
    # OVERLAP the selected range is too far to be meaningful — showing May's
    # deduplicated reach on a June view misleads more than an honest blank.
    # Overlapping-but-inexact windows still serve (labeled + header-marked).
    "window_disjoint": "No pulled window overlaps your selected range — refresh to pull it.",
    "multi_grain": "approx upper bound — not deduplicated (select one property for an exact count)",
    "unknown": "Deduplicated data unavailable for this range.",
}

# follow-up — multi_grain has TWO causes that need different copy. The
# reason CODE stays "multi_grain" (the renderer's amber treatment is the same),
# but the message differs so the tooltip isn't misleading:
#   * multi_property — selecting ONE property resolves it → say so.
#   * sub_dimension  — a per-breakdown dedup is impossible (GA4 window is
#     property-level); selecting a property does NOT help, so do NOT tell the
#     user to. The exact figure lives in the property-total tile above.
# The default copy is GA4-shaped ("property"). an FB reach table
# grouped by TEAM (or any dimension coarser/other than the ad-set grain) also
# trips sub_dimension, but "property" is the wrong vocabulary there: FB dedups
# reach per AD SET, and a team row sums ad-set reach (re-counting anyone reached
# by >1 ad set — and, for a cross-account team, across accounts Meta can't dedup).
# Source-specific copy keeps each tooltip honest for its own grain.
_MULTI_GRAIN_MESSAGES = {
    "multi_property": "approx upper bound — not deduplicated (select one property for an exact count)",
    "sub_dimension": "approx upper bound — this breakdown can't be deduplicated (the property total is exact)",
}

# per-source overrides for the multi_grain copy. Keyed by
# (source, sub_reason). Falls back to _MULTI_GRAIN_MESSAGES (GA4-shaped) when a
# source has no override, so GA4 behavior is unchanged.
_MULTI_GRAIN_MESSAGES_BY_SOURCE = {
    ("fb_insights", "sub_dimension"): (
        "approx upper bound — reach isn't deduplicated across ad sets (view a single ad set for an exact count)"
    ),
    ("fb_insights", "multi_property"): (
        "approx upper bound — reach isn't deduplicated across ad sets (view a single ad set for an exact count)"
    ),
    # a TEAM-grain reach row whose ads span 2+ ad accounts. Meta dedups
    # reach only within ONE account, so this row is the SUM of the per-account
    # deduped totals (an upper bound across accounts). A single-account team row is
    # exact and carries NO label — only cross-account rows get this.
    ("fb_insights", "cross_account"): (
        "approx upper bound — summed across this team's ad accounts (Meta can't deduplicate reach across accounts)"
    ),
    # a PAGE row whose ads span 2+ ad accounts (page_reach_accounts > 1).
    ("fb_insights", "cross_account_page"): (
        "approx upper bound — summed across the ad accounts this page's ads ran on "
        "(Meta can't deduplicate reach across accounts)"
    ),
    # a demographics row for a team whose ads span 2+ ad accounts
    # (demo_reach_accounts > 1): each (age, gender) cell is the SUM of the
    # per-account deduped cells.
    ("fb_insights", "cross_account_demographics"): (
        "approx upper bound — summed across this team's ad accounts (Meta can't deduplicate reach across accounts)"
    ),
    ("ig_insights", "sub_dimension"): (
        "approx upper bound — reach isn't deduplicated across accounts (view a single account for an exact count)"
    ),
    ("ig_insights", "multi_property"): (
        "approx upper bound — reach isn't deduplicated across accounts (view a single account for an exact count)"
    ),
}


def multi_grain_message(sub_reason, source=None):
    """User copy for a multi_grain sub-reason; source-specific when available.

    an FB/IG reach table's grain is the ad set / IG account, not a GA4
    property, so the default "property"-worded copy misleads. Prefer a
    (source, sub_reason) override, then the generic sub_reason line, then the
    catch-all. GA4 (no override) is unchanged."""
    if source is not None:
        specific = _MULTI_GRAIN_MESSAGES_BY_SOURCE.get((source, sub_reason))
        if specific:
            return specific
    return _MULTI_GRAIN_MESSAGES.get(sub_reason, DEDUP_EMPTY_REASONS["multi_grain"])


def dedup_empty_copy(reason):
    """Return the user-facing copy for an empty-cell reason code (safe fallback)."""
    return DEDUP_EMPTY_REASONS.get(reason, DEDUP_EMPTY_REASONS["unknown"])


# The daily source key a base data view resolves to (e.g. ``fb_insights``).
# The registry is keyed by this base source; the dedup docs live in the
# ``index_suffix`` window index. For the spike we resolve the dedup index
# suffix from the registry entry of any matched field.
def _registry_entry_for(source, field):
    return DEDUP_WINDOW_REGISTRY.get((source, field))


def dedup_fields_for_data_view(data_view):
    """Return the dedup field names that belong to a data view's source.

    used to augment the builder's field list so dedup fields are
    selectable on the ONE daily data view (`fb_ad_insights`) even when the field
    physically lives only in the window index. Registry-driven: the field set is
    every ``field`` whose ``(source, field)`` is registered for this data view's
    source. Returns [] for a non-dedup source.
    """
    source = source_from_data_view(data_view)
    return [field for (src, field) in DEDUP_WINDOW_REGISTRY if src == source]


def window_dimension_fields_for_data_view(data_view):
    """the window-only grain DIMENSION fields for a data view's
    source (fb_insights → ["age", "gender"]).

    Sibling of :func:`dedup_fields_for_data_view` for dimensions instead of
    metrics: these fields exist only on window docs (the demographics grain has
    no daily-index counterpart), so the builder field picker and the canvas AI
    catalog must surface them on the canonical daily view explicitly — the
    routed read path then serves their group-bys wholly from the window index.
    Returns [] for a source with no window-only grain."""
    source = source_from_data_view(data_view)
    return window_only_grain_fields(_WINDOW_SUFFIX_TO_SOURCE.get(source, source))


def _iter_config_fields(config):
    """Yield (field, kind, ref) for every metric field a chart config names.

    kind is 'y' for a y_fields entry or 'col' for a table column; ref is the
    original list/dict so the caller can rebuild a filtered sub-config. Mirrors
    the locations es_query_builder._chart_field_dataviews walks, but here we are
    partitioning a SINGLE-data-view config (no per-field data_view), so the
    field name alone decides routing via the registry.
    """
    y_fields = config.get("y_fields") or []
    if not y_fields and config.get("y_field"):
        y_fields = [config["y_field"]]
    for yf in y_fields:
        if yf:
            yield yf, "y", None

    for col in config.get("table_columns") or config.get("columns") or []:
        if not isinstance(col, dict):
            continue
        if col.get("aggregation") == "calculation":
            continue
        if col.get("field"):
            yield col["field"], "col", col


def split_config_by_index(config):
    """Partition a single-data-view chart config into (daily_cfg, dedup_cfg, plan).

    Routing rule: a metric field is a DEDUP field iff ``(source, field)`` is in
    the registry, where ``source`` is the base source of the chart's data view
    (e.g. ``fb_insights`` from ``{esid}__fb_insights``). Everything else stays
    additive on the daily index.

    Returns:
      daily_cfg  — a copy of config with dedup fields removed (additive read).
      dedup_cfg  — a copy of config carrying ONLY the dedup fields, pointed at
                   the dedup (window) index suffix, so build_query forces ``max``.
                   ``None`` when the chart has no dedup field (no second query).
      plan       — {
                     'dedup_fields': [..],           # field names routed to dedup
                     'dedup_index_suffix': str|None, # window index suffix
                     'group_by': str|None,           # the stitch key (x_field / first text col)
                     'status': 'live'|'planned'|None # gating for R3/R4
                   }

    The plan is what the stitch + the (R3) nearest-window + (R4) empty-cell
    logic key off. When dedup_cfg is None the caller runs the unchanged single
    query — back-compat for any non-dedup chart.
    """
    # Resolve the registry BASE source, accepting a chart that sits on either the
    # daily view (``*_fb_insights``) or the window view (``*_fb_insights_window``)
    # — the Ad Sets table is built on the window view at the chart level.
    base_source = _base_source_of(config.get("data_view"))

    dedup_fields = []
    status = None
    dedup_suffix = None
    for field, _kind, _ref in _iter_config_fields(config):
        entry = _registry_entry_for(base_source, field)
        if entry is None:
            continue
        dedup_fields.append(field)
        dedup_suffix = entry.index_suffix
        # If any matched field is live, the chart has live dedup data.
        if entry.status == "live" or status is None:
            status = entry.status

    group_by = config.get("x_field")
    if not group_by:
        # Table: the first text/label column is the group-by key.
        for col in config.get("table_columns") or []:
            if isinstance(col, dict) and col.get("field") and col.get("aggregation") in (None, "", "text"):
                group_by = col["field"]
                break

    # a calculation column whose formula references a dedup field
    # (the Ad Sets "Frequency" = insight_impressions/insight_reach) must read
    # EVERY operand from the same window. Left alone, the stitch overwrote only
    # the dedup operand (reach) with the snapped window's value while the other
    # operand (impressions) stayed the selected range's daily sum, so a 2-day
    # view divided 2 days of impressions by 7-day reach (0.26 — an impossible
    # frequency). Collect those "window calcs" and their operands; the dedup leg
    # reads the operands too (additive ones summed over the per-platform docs,
    # dedup ones with their registry agg) and the stitch carries them under
    # window_metric_*.
    window_calcs = {}
    for col in config.get("table_columns") or config.get("columns") or []:
        if not (isinstance(col, dict) and col.get("aggregation") == "calculation" and col.get("field")):
            continue
        operands = [f for f in (col.get("formula_fields") or []) if f]
        dedup_operands = [(f, _registry_entry_for(base_source, f)) for f in operands]
        dedup_operands = [(f, e) for f, e in dedup_operands if e is not None]
        if not dedup_operands:
            continue
        window_calcs[col["field"]] = operands
        for _f, entry in dedup_operands:
            dedup_suffix = dedup_suffix or entry.index_suffix
            if entry.status == "live" or status is None:
                status = entry.status
    window_operand_fields = []
    for operands in window_calcs.values():
        for f in operands:
            if f not in window_operand_fields:
                window_operand_fields.append(f)

    plan = {
        "dedup_fields": dedup_fields,
        "dedup_index_suffix": dedup_suffix,
        "group_by": group_by,
        "status": status,
        "window_calcs": window_calcs,
        "window_operand_fields": window_operand_fields,
    }

    if not dedup_fields and not window_calcs:
        # No dedup field — single query, unchanged behavior.
        return dict(config), None, plan

    daily_cfg = _without_fields(config, dedup_fields)
    # additive fields MUST read from the per-day index. If the chart is
    # built on the window view (Ad Sets), rewrite the daily query's data_view back
    # to the daily index so additive metrics are summed over the per-day docs (not
    # the accumulated overlapping window docs, which over-count ~3.5x).
    daily_cfg["data_view"] = _daily_data_view(config.get("data_view"))
    dedup_cfg = _only_fields(config, dedup_fields, dedup_suffix)
    if window_operand_fields:
        dedup_cfg = _with_window_operand_columns(dedup_cfg, window_operand_fields)
    return daily_cfg, dedup_cfg, plan


def _with_window_operand_columns(dedup_cfg, operand_fields):
    """add a column to the dedup (window) config for each
    window-calc operand it doesn't already carry, so the window response holds
    every operand of the formula for the SAME window.

    An additive operand (impressions, spend) is ``sum``med: an ad set's window
    docs are one per publisher_platform PLUS the combined ``publisher_platform=
    "all"`` doc, and the importer strips every additive metric from the combined
    doc (get_insights_window._COMBINED_KEEP_INSIGHT_FIELDS) so the
    per-platform docs alone carry it — their sum is the ad set's total, and
    under a platform filter it is that platform's value. ``max`` would read the
    largest single platform (prod 2026-10-01: 64,876 Facebook-only vs the
    69,284 total). The ad-set grain filter (_only_fields) already keeps the
    ad/asset docs out of the sum. A registered dedup operand that is not its own
    column (a Frequency column on a table with no Reach column) is added the
    same way — build_query forces its registry window agg (``max`` for reach)."""
    out = dict(dedup_cfg)
    key = "table_columns" if out.get("table_columns") else "columns"
    cols = list(out.get(key) or [])
    present = {c.get("field") for c in cols if isinstance(c, dict)}
    for f in operand_fields:
        if f not in present:
            cols.append({"field": f, "aggregation": "sum"})
            present.add(f)
    out[key] = cols
    return out


def _without_fields(config, drop_fields):
    """Copy config with the named metric fields removed from y_fields + columns."""
    out = dict(config)
    drop = set(drop_fields)
    if out.get("y_fields"):
        out["y_fields"] = [f for f in out["y_fields"] if f not in drop]
    if out.get("y_field") in drop:
        out.pop("y_field", None)
    for key in ("table_columns", "columns"):
        if out.get(key):
            out[key] = [c for c in out[key] if not (isinstance(c, dict) and c.get("field") in drop)]
    return out


def _only_fields(config, keep_fields, dedup_index_suffix):
    """Copy config carrying ONLY the dedup metric fields, pointed at the dedup index.

    The data_view is rewritten to the dedup (window) suffix so build_query's
     rule forces ``max`` for these non-additive fields. The group-by
    (x_field / label column) is preserved so the stitch can align rows.
    """
    out = dict(config)
    keep = set(keep_fields)

    # Point at the dedup index. Preserve the prefix so scoping/round-trip stays
    # consistent — team-prefixed (``{esid}__<suffix>``) OR wildcard (``*_<suffix>``,
    # the builder/repoint storage form). A chart already on the window view maps to
    # the same window suffix (no-op).
    dv = config.get("data_view") or ""
    cur_suffix = source_from_data_view(dv)
    if "__" in dv:
        prefix = dv.rsplit("__", 1)[0]
        out["data_view"] = f"{prefix}__{dedup_index_suffix}"
    elif dv.endswith(cur_suffix) and dv != cur_suffix:
        # Wildcard / non-__ prefix (e.g. ``*_fb_insights`` -> ``*_fb_insights_window``).
        out["data_view"] = dv[: -len(cur_suffix)] + dedup_index_suffix
    else:
        out["data_view"] = dedup_index_suffix

    if out.get("y_fields"):
        out["y_fields"] = [f for f in out["y_fields"] if f in keep]
    if config.get("y_field") in keep:
        out["y_field"] = config["y_field"]
    elif "y_field" in out and out.get("y_field") not in keep:
        out.pop("y_field", None)

    for key in ("table_columns", "columns"):
        if config.get(key):
            # Keep dedup metric columns + the label/group-by column(s) so the
            # dedup query buckets on the same key as the daily query.
            kept = []
            for c in config[key]:
                if not isinstance(c, dict):
                    continue
                fld = c.get("field")
                is_label = c.get("aggregation") in (None, "", "text")
                if fld in keep or is_label:
                    kept.append(c)
            out[key] = kept

    # window docs carry every dimension as a bare ``keyword`` (no window
    # schema defines a ``.keyword`` sub-field), while a chart built on the daily
    # view may persist its group-by WITH the suffix — the builder offers the
    # aggregatable variant when the daily field is text (GA4's daily eventName is
    # text+.keyword). A composite source pointed at ``eventName.keyword`` on the
    # window index matches nothing -> zero buckets -> every dedup cell renders
    # ``no_data`` (staging repro, 2026-09-09). Normalize the window leg's
    # dimension fields to their bare names; the stitch matches keys the same way
    # (``_stitch_key``), so the daily leg keeps its own field spelling.
    for dim_key in ("x_field", "x_field_2", "breakdown", "split_field"):
        dim = out.get(dim_key)
        if isinstance(dim, str) and dim.endswith(".keyword"):
            out[dim_key] = dim[: -len(".keyword")]
    if out.get("x_field"):
        # build_query re-appends .keyword to a bare field typed ``text``; on the
        # window index the field IS the keyword.
        out["x_field_type"] = "keyword"
    for key in ("table_columns", "columns"):
        if out.get(key):
            cols = []
            for c in out[key]:
                if (
                    isinstance(c, dict)
                    and c.get("aggregation") in (None, "", "text")
                    and isinstance(c.get("field"), str)
                    and c["field"].endswith(".keyword")
                ):
                    c = dict(c, field=c["field"][: -len(".keyword")])
                cols.append(c)
            out[key] = cols

    # strip chart filters the window doc can't honor. A chart on the
    # raw daily index may carry a filter on a dimension the WINDOW doc doesn't
    # have (e.g. a GA4 chart's ``report_type=events``, required for its additive
    # columns but absent from the property-grain window doc). Left on the dedup
    # query it matches zero window docs and blanks the value. Keep only filters on
    # fields the window doc actually carries (grain + importer-written dims). The
    # daily_cfg keeps every filter — this prunes ONLY the window (dedup) query.
    # Fail open when the source is uncharacterized (carried is None) so a filter
    # is never silently dropped for a source we haven't mapped.
    carried = window_carried_fields(_base_source_of(config.get("data_view")))
    if carried is not None and out.get("filters"):
        out["filters"] = [f for f in out["filters"] if not isinstance(f, dict) or _normalize_field(f.get("field")) in carried]

    # inject the ``grain`` discriminator so the dedup query reads ONLY
    # the docs at the grain the table groups by. A team-grouped table
    # (group_by team_esid/team_title) reads the ONE grain="team" doc (its exact
    # cross-ad-set deduped reach); every other fb table (adset_id/adset_name, or
    # any other group-by) reads grain="adset" — the per-ad-set docs, unchanged.
    # Without this the team query would match BOTH grains and ``max`` would blend
    # them. Default "adset" keeps earlier docs (no grain field) readable: the
    # importer now stamps grain, but a term filter on grain would also EXCLUDE old
    # unstamped docs — so we only inject the filter for the TEAM grain (the new
    # doc always carries grain="team"); the ad-set path stays unfiltered so it
    # still reads legacy unstamped ad-set docs. See group_by_grain_value.
    base_source = _base_source_of(config.get("data_view"))
    grain_value = group_by_grain_value(base_source, _group_by_of(config))
    # a page sub-row under a team group-by reads the (team, page)
    # grain="page" docs; a page GROUP-BY reads them too. Both are always-stamped
    # new docs, so (like "team") the eq filter can't exclude legacy docs.
    sub_grain = group_by_grain_value(base_source, _normalize_field(config.get("x_field_2")))
    target_grain = None
    if grain_value == "team" and sub_grain in ("page", "demographics"):
        target_grain = sub_grain
    # an AD sub-row under an AD-SET group-by reads the grain="ad"
    # docs: each ad doc carries adset_id/adset_name too, so the (ad set, ad)
    # composite buckets match. Always-stamped new docs, eq filter safe.
    elif grain_value == "adset" and sub_grain == "ad":
        target_grain = "ad"
    # an ASSET sub-row under an AD-SET group-by reads the grain=
    # "asset" cell docs: each carries its parent adset identity, so the
    # (ad set, asset) composite matches. Always-stamped, eq filter safe.
    elif grain_value == "adset" and sub_grain == "asset":
        target_grain = "asset"
    # an age group-by with a gender sub-row (or vice versa) is still
    # the demographics grain: both dimensions live on the same (age, gender)
    # cell doc. Always-stamped new docs, so (like "team"/"page"/"ad") the eq
    # filter can't exclude legacy docs.
    elif grain_value in ("team", "page", "demographics", "ad", "asset"):
        target_grain = grain_value
    # the GA4 EVENT grain: an eventName group-by reads the always-
    # stamped grain="event" docs (eq filter safe, like the FB grains above).
    elif grain_value == "event":
        target_grain = "event"
    existing = [f for f in (out.get("filters") or []) if not (isinstance(f, dict) and f.get("field") == "grain")]
    if target_grain:
        existing.append({"field": "grain", "op": "eq", "value": target_grain})
        out["filters"] = existing
    elif base_source in _GRAIN_STAMPED_WINDOW_SOURCES:
        # the source's DEFAULT grain path: exclude the stamped
        # non-default grains instead of matching everything. fb_insights: the
        # ad-set path excludes team/page/demographics/ad/asset. the GA4
        # property path excludes the event grain (legacy property docs are
        # unstamped, so ``grain == property`` would drop them; ``neq`` keeps
        # them). Uncharacterized sources keep the fail-open contract.
        out["filters"] = _exclude_non_adset_grains(out.get("filters"), excluded_grains_by_default(base_source))
    return out


def _group_by_of(config):
    """The table's group-by field: x_field, else the first text/label column.
    Mirrors split_config_by_index's group_by resolution."""
    gb = config.get("x_field")
    if gb:
        return gb
    for col in config.get("table_columns") or []:
        if isinstance(col, dict) and col.get("field") and col.get("aggregation") in (None, "", "text"):
            return col.get("field")
    return None


def stitch_table_responses(daily_resp, dedup_resp, plan):
    """Merge a dedup-index table response INTO a daily table response by group-by key.

    Both responses are ``aggregations.table_buckets.buckets[]`` shaped (the table
    branch of build_query). For each daily bucket, find the dedup bucket with the
    same ``key`` and copy the dedup ``metric_<field>`` sub-aggs across, so the
    merged response carries additive metrics (daily, summed) AND dedup metrics
    (dedup index, max) keyed by the same group-by value. The merged response is
    returned in the daily response's shape so the existing ``transform`` renders
    it unchanged.

    A daily bucket with no matching dedup bucket keeps no dedup metric — that is
    the R4 explained-empty-cell case (a group-by value with no window doc:
    no-spend / shared-account / grain-with-no-dedup). The stitch does NOT
    fabricate a value (never a silent daily-sum fallback).
    """
    daily = dict(daily_resp or {})
    daily_aggs = dict(daily.get("aggregations") or {})
    daily_tb = dict(daily_aggs.get("table_buckets") or {})
    daily_buckets = list(daily_tb.get("buckets") or [])

    dedup_aggs = (dedup_resp or {}).get("aggregations") or {}
    dedup_buckets = (dedup_aggs.get("table_buckets") or {}).get("buckets") or []

    dedup_fields = list(plan.get("dedup_fields") or [])
    dedup_metric_keys = [(f, f"metric_{_safe_agg_key(f)}") for f in dedup_fields]
    # window-calc operands ride under window_metric_<f> so the daily
    # metric_<f> (e.g. the Impressions column) keeps the selected range's value.
    window_calcs = dict(plan.get("window_calcs") or {})
    window_operand_keys = [
        (f"metric_{_safe_agg_key(f)}", f"window_metric_{_safe_agg_key(f)}") for f in plan.get("window_operand_fields") or []
    ]
    # match rows by NORMALIZED key: the window leg groups on the bare
    # field (see _only_fields) while the daily leg may group on ``<field>.keyword``,
    # so the composite key dicts differ in field NAME but not in value.
    by_key = {_stitch_key(b): b for b in dedup_buckets}

    # the COLUMN-level reason (if any) explains why the whole dedup
    # column may be empty (not_built / no_window). When the column is fine, a
    # MISS on an individual row is the cell-level `no_data` reason. Either way,
    # never a daily-sum fallback.
    column_reason = plan.get("empty_reason")  # set by run_routed_query

    # team rows whose ads span 2+ ad accounts: their team-grain reach IS
    # a real (dedup-per-account) value, but SUMMED across accounts Meta can't dedup
    # together — an upper bound. KEEP the value (unlike an empty cell) and attach a
    # multi_grain-style label to just those rows. Single-account team rows are exact
    # and unlabeled. Message resolved once (source-specific cross_account copy).
    cross_keys = plan.get("cross_account_keys") or set()
    # with a page sub-row the cross keys are (group value, sub value)
    # PAIRS from the grain="page" docs; the per-row match below compares the
    # same pair. Absent (team grain / page group-by) the keys stay single values.
    cross_sub = plan.get("cross_sub_row")
    cross_msg = (
        multi_grain_message(plan.get("cross_sub_reason") or "cross_account", plan.get("base_source")) if cross_keys else None
    )

    merged_buckets = []
    for b in daily_buckets:
        nb = dict(b)
        match = by_key.get(_stitch_key(b))
        cell_reasons = {}
        for field, mk in dedup_metric_keys:
            if match is not None and mk in match:
                nb[mk] = match[mk]
            else:
                # No dedup doc for this group-by value. DELETE any dedup metric the
                # daily response might carry so a daily-summed value can NEVER
                # survive as the reach — the whole point of these tickets.
                nb.pop(mk, None)
                cell_reasons[field] = column_reason or "no_data"
        # copy the window operands; a row with no window doc gets no
        # operands (its window calc renders the same explained empty as reach).
        if match is not None:
            for mk, wk in window_operand_keys:
                if mk in match:
                    nb[wk] = match[mk]
        else:
            for calc_field in window_calcs:
                cell_reasons[calc_field] = column_reason or "no_data"
        # cross-account team row: value kept, labeled as upper bound.
        # Applied only where the dedup value is PRESENT (a missing cell keeps its
        # own no_data reason above — cross-account labeling a blank makes no sense).
        # Phase C fix: cross_keys holds PLAIN terms-agg keys (team titles), while
        # _bucket_key on a composite (dict) key returns an items-tuple — they can
        # never compare equal, so the per-row marker silently never fired. Compare
        # by the bucket's group-by VALUE instead.
        if cross_keys:
            row_val = _group_by_value(b, plan.get("group_by"))
            if cross_sub:
                row_val = (row_val, _group_by_value(b, cross_sub))
            if row_val in cross_keys:
                for field, mk in dedup_metric_keys:
                    if mk in nb and field not in cell_reasons:
                        cell_reasons[field] = "multi_grain"
                # a window calc over that row's upper-bound reach is
                # approximate too; label it alongside the reach cell.
                for calc_field in window_calcs:
                    if calc_field not in cell_reasons:
                        cell_reasons[calc_field] = "multi_grain"
        if cell_reasons:
            # Per-cell reason map the transform/UI reads to render explained empties.
            nb["dedup_empty"] = cell_reasons
        merged_buckets.append(nb)

    daily_tb["buckets"] = merged_buckets
    daily_aggs["table_buckets"] = daily_tb
    daily["aggregations"] = daily_aggs
    # tells the transform which calc columns evaluate over the
    # window_metric_* operands instead of the daily metric_* ones.
    if window_calcs:
        daily["dedup_window_calcs"] = list(window_calcs)

    # surface which window the dedup values came from so the UI can
    # LABEL the reach cells/column ("90-day window: Jun 1 – Sep 1"). Carried as
    # response-level metadata (the label applies to the whole dedup column for
    # this query), keyed by the dedup field names it covers. Absent when no snap
    # happened (exact match or no window).
    snapped = plan.get("snapped_window")
    if snapped:
        # carry whether the snap matches the user's selected range
        # exactly, computed server-side (day ordinals) so the UI never does
        # date math. exact=False drives the per-column header marker; absent
        # requested_range (older callers) counts as exact to avoid false
        # alarms on surfaces that never pass a range.
        requested = plan.get("requested_range") or {}
        rf, rt = _to_ordinal(requested.get("from")), _to_ordinal(requested.get("to"))
        exact = True
        if rf is not None and rt is not None:
            exact = _to_ordinal(snapped.get("start")) == rf and _to_ordinal(snapped.get("end")) == rt
        daily["dedup_window"] = {
            "start": snapped.get("start"),
            "end": snapped.get("end"),
            "label": snapped.get("label") or _window_label(snapped.get("start"), snapped.get("end")),
            # window calcs are served from the same window, so they
            # carry the ≈ header marker too (charts.js reads this list).
            "fields": dedup_fields + [c for c in window_calcs if c not in dedup_fields],
            "exact": exact,
        }

    # column-level empty reason (not_built / no_window). Carried so
    # the UI can render one explanation for the whole dedup column instead of a
    # per-cell repeat. Absent when the column has data.
    column_reason = plan.get("empty_reason")
    if column_reason:
        daily["dedup_empty"] = {
            "reason": column_reason,
            "message": dedup_empty_copy(column_reason),
            "fields": dedup_fields,
        }
    elif cross_keys and cross_msg:
        # no column-wide empty, but some team rows are cross-account
        # upper bounds. Carry the multi_grain reason + cross-account copy so the UI
        # tooltip has the message for the per-row markers set above. Single-account
        # tables (no cross_keys) carry nothing — the reach column is fully exact.
        daily["dedup_empty"] = {
            "reason": "multi_grain",
            "message": cross_msg,
            "fields": dedup_fields,
        }
    return daily


def _stamp_multi_grain(resp, plan):
    """label a multi-grain table response WITHOUT blanking its values.

    Unlike ``stitch_table_responses`` (which DELETES the dedup metric on a miss),
    multi_grain KEEPS the daily-summed dedup value already present in ``resp`` and
    only attaches the explanatory reason: a column-level ``dedup_empty`` (one
    message for the whole dedup column) plus a per-row ``dedup_empty`` marker so
    the reach-warning detector (``_attach_reach_warning``) still sees the column
    fell back on every row. The value rides through as the labeled upper bound.
    """
    out = dict(resp or {})
    dedup_fields = list(plan.get("dedup_fields") or [])
    # follow-up — pick copy by sub-reason (multi_property vs
    # sub_dimension); the reason CODE stays "multi_grain" for the renderer.
    message = multi_grain_message(plan.get("multi_grain_sub_reason"), plan.get("base_source"))
    aggs = dict(out.get("aggregations") or {})
    tb = dict(aggs.get("table_buckets") or {})
    buckets = []
    for b in tb.get("buckets") or []:
        nb = dict(b)
        nb["dedup_empty"] = {f: "multi_grain" for f in dedup_fields}
        buckets.append(nb)
    if tb:
        tb["buckets"] = buckets
        aggs["table_buckets"] = tb
        out["aggregations"] = aggs
    out["dedup_empty"] = {
        "reason": "multi_grain",
        "message": message,
        "fields": dedup_fields,
    }
    return out


def _window_label(start, end):
    """Compact label for a snapped window, e.g. '2026-06-01 – 2026-09-01'."""
    if start and end:
        return f"{start} – {end}"
    return start or end or ""


def _bucket_key(bucket):
    """Normalise a composite/terms bucket's key to a hashable comparison value."""
    k = bucket.get("key")
    if isinstance(k, dict):
        # composite agg key — order-stable tuple of its values
        return tuple(sorted(k.items()))
    return k


def _stitch_key(bucket):
    """``_bucket_key`` with ``.keyword`` drift removed from composite
    field names, so a daily ``{"eventName.keyword": v}`` bucket and a window
    ``{"eventName": v}`` bucket compare equal. Plain terms keys pass through."""
    k = bucket.get("key")
    if isinstance(k, dict):
        return tuple(sorted((_normalize_field(name), value) for name, value in k.items()))
    return k


def _group_by_value(bucket, group_by):
    """Phase C — the bucket's group-by VALUE (e.g. the team title).

    A composite bucket keys by field name (possibly with ``.keyword`` drift on
    either side); a plain terms bucket keys by the value directly. Used to match
    stitched daily buckets against ``_cross_account_team_keys``'s plain terms-agg
    key set — ``_bucket_key`` (an items-tuple for dict keys) can never equal a
    plain string, which silently disabled the per-row cross-account marker.
    """
    k = bucket.get("key")
    if not isinstance(k, dict):
        return k
    base = _normalize_field(group_by or "")
    for cand in (group_by, base, f"{base}.keyword" if base else None):
        if cand and cand in k:
            return k[cand]
    if len(k) == 1:
        # Single-source composite — its only value IS the group-by value.
        return next(iter(k.values()))
    return None


def _normalize_field(field):
    """Strip a trailing ``.keyword`` so a group-by compares equal to its grain."""
    f = field or ""
    return f[:-8] if f.endswith(".keyword") else f


def is_multi_grain(config, daily_cfg, search_fn, base_source, *, dashboard_filters=None):
    """does this chart's scope span MORE THAN ONE dedup grain?

    A deduplicated figure (GA4 unique users, FB reach) is only valid within ONE
    grain value (one GA4 property; one FB ad-set). It is multi-grain — and so
    cannot show a real dedup number — when EITHER:

      (a) the scoped query spans 2+ distinct grain-field values (e.g. a GA4 chart
          over 15 workspace properties, no property selected), OR
      (b) the chart groups by a dimension FINER than / different from the grain
          (e.g. GA4 activeUsers grouped by city: the window doc is property-level
          zero-dimension, so no per-city dedup exists — every such table is
          multi-grain regardless of how many properties are in scope).

    (b) is a pure config check (no ES hit). (a) needs one cheap cardinality agg on
    the grain field within the SAME scoped+filtered query the daily read uses, so
    a selected-property dashboard_filter collapses the count to 1. Fail-soft: any
    error → treat as multi-grain (the safe, honest fallback — label rather than
    risk a false-precise number).
    """
    return multi_grain_reason(config, daily_cfg, search_fn, base_source, dashboard_filters=dashboard_filters) is not None


def multi_grain_reason(config, daily_cfg, search_fn, base_source, *, dashboard_filters=None):
    """WHY a chart is multi-grain, or ``None`` if it is single-grain.

    Returns one of:
      * ``"sub_dimension"`` — branch (b): the chart groups by a dimension FINER
        than the grain (e.g. GA4 activeUsers by city). This is PERMANENT — even
        with one property selected, no per-sub-dimension dedup exists. The
        user-facing copy must NOT say "select one property" (they may already
        have; it won't help).
      * ``"multi_property"`` — branch (a): the scope spans 2+ distinct grain
        values (e.g. no property selected over 15 properties). Here selecting ONE
        property DOES resolve it — the copy should say so.
      * ``None`` — single grain; the real dedup path applies.

    The distinction drives the tooltip (follow-up: the old single
    "select one property" message was misleading on a sub-dimension table that
    stayed amber after the user already selected a property).
    """
    grain = dedup_grain_field(base_source)
    if not grain:
        return None  # source has no defined grain → leave legacy behavior

    # (b) group-by finer than the grain — config-only, no ES round-trip. PERMANENT.
    # compare against the grain's EQUIVALENTS (the id PLUS its 1:1 name
    # alias), not just the id. Grouping the FB Ad Sets table by ``adset_name`` is
    # the SAME grain as ``adset_id`` (one name per id), so it is single-grain and
    # deduplicable — without this it tripped sub_dimension and rendered the
    # inflated daily sum with a GA4-shaped "can't be deduplicated" label.
    grain_equivalents = dedup_grain_equivalents(base_source) or {grain}
    group_by = _normalize_field(config.get("x_field"))
    if not group_by:
        for col in config.get("table_columns") or []:
            if isinstance(col, dict) and col.get("field") and col.get("aggregation") in (None, "", "text"):
                group_by = _normalize_field(col.get("field"))
                break

    # a sub-row (x_field_2) splits every rendered row FINER than the
    # row's own grain unless it is a 1:1 alias of that same grain (ad-set rows
    # sub-labeled by adset_name). Nothing finer has a window doc, so the table
    # is permanently sub_dimension — the labeled daily-sum upper bound. Before
    # this gate the dedup leg carried the sub-row composite source into the
    # window index, matched nothing, and silently blanked the reach column on
    # every row (even teams with real window docs).
    #
    # a PAGE sub-row under a TEAM group-by is a real grain
    # pair, not a finer breakdown: grain="page" window docs are keyed by
    # (team, page) with Meta's exact deduped reach per page. The router injects
    # grain=page (see _only_fields) and the stitch matches the (team, page)
    # composite — so this pair falls through as single-grain-per-row.
    sub_row = _normalize_field(config.get("x_field_2"))
    if sub_row:
        sub_grain = group_by_grain_value(base_source, sub_row)
        gb_grain = group_by_grain_value(base_source, group_by)
        if sub_grain == "page" and gb_grain == "team":
            pass  # (team, page) reads the page-grain docs
        elif sub_grain == "ad" and gb_grain == "adset":
            # (ad set, ad) reads the ad-grain docs: each grain="ad"
            # doc carries its parent adset_id/adset_name, so the composite
            # bucket matches one ad doc exactly. Not a finer-than-grain case.
            pass
        elif sub_grain is None or sub_grain != gb_grain:
            return "sub_dimension"

    if group_by and group_by not in grain_equivalents:
        return "sub_dimension"

    # a TABLE grouped BY the grain (or its 1:1 name alias) is
    # single-grain PER ROW: each row is one ad-set / property, and the table path
    # (stitch_table_responses) reads that row's own window doc. So it is NOT
    # multi-grain regardless of how many rows (grain values) are in scope — the
    # cardinality branch (a) below must NOT fire for it. (Branch (a) is for a
    # SCALAR/non-grain-grouped tile that sums a dedup field across many grains and
    # genuinely can't be deduplicated.) Before the table dedup-worked
    # because this multi_grain gate didn't exist; added branch (a) for
    # GA4 scalars and it caught grain-grouped tables as collateral. Only reach the
    # cardinality count when the chart is NOT grouped by the grain.
    #
    # a SCOPE-DEPENDENT grain (GA4 eventName → grain="event",
    # one doc per property × event) is exact only within ONE property. Across 2+
    # properties ``max`` would pick one property's per-event users and present
    # it as the total, so fall through to the cardinality check below: a
    # multi-property scope renders the labeled upper bound with the existing
    # "select one property for an exact count" copy — which is the fix here.
    if group_by and group_by in grain_equivalents:
        if not grain_is_scope_dependent(base_source, group_by_grain_value(base_source, group_by)):
            return None

    # (a) NO grain group-by (a scalar tile): does the scoped query touch 2+
    # distinct grain values? If so the summed dedup field is the labeled upper
    # bound; exactly 1 is the single grain's own figure.
    try:
        body = {
            "size": 0,
            "query": build_query(daily_cfg, dashboard_filters=dashboard_filters).get("query", {"match_all": {}}),
            "aggs": {"grain_card": {"cardinality": {"field": grain}}},
        }
        resp = search_fn(daily_cfg["data_view"], body) or {}
        count = (((resp.get("aggregations") or {}).get("grain_card") or {}).get("value")) or 0
        return "multi_property" if count > 1 else None
    except Exception:  # noqa: BLE001 — fail-soft: an uncountable scope is treated as multi-grain
        logging.warning("grain-count failed; treating as multi-grain (labeled upper bound)")
        return "multi_property"


def run_routed_query(config, search_fn, *, dashboard_filters=None):
    """Execute a chart config via the dedup-routed read path; return an ES-response dict.

     R2. The single integration point a live chokepoint calls instead of
    inline ``build_query`` + ``es.search``. ``search_fn(data_view, body)`` runs
    one scoped ES search and returns the response dict — the CALLER owns scoping
    (RoleFilterService.scoped_index_pattern) and applies it identically to BOTH
    the daily and dedup queries.

    Behavior:
      * flag OFF, or the chart has no dedup field → ONE query (the daily/base
        config), identical to today. Returns its response unchanged.
      * flag ON and the chart has a dedup field → daily query THEN dedup query
        (SEQUENTIAL — R1 staging finding: concurrency is a pessimization at this
        query size), then ``stitch_table_responses`` merges them. Returns the
        stitched response in the daily shape so ``transform`` renders it unchanged.

    The returned dict is fed to the existing ``transform(resp, config)`` — config
    is never mutated, so the user sees one chart.
    """
    if not dedup_routing_enabled():
        return search_fn(config["data_view"], build_query(config, dashboard_filters=dashboard_filters))

    # the split/stitch is TABLE-shaped (stitch_table_responses merges
    # aggregations.table_buckets by group-by key). A metric/scalar tile or a
    # bar/line/pie chart has no table_buckets, so it takes the single daily query.
    #
    # (overrides the 2026-06-21 "silent approximation"): a NON-table tile
    # bound to a dedup field that is MULTI-GRAIN (2+ properties / ad-sets, or no
    # property selected) is the un-deduplicated daily sum — the inflated upper
    # bound. We still return that daily query (a scalar can't be stitched from
    # per-grain window docs — the wall), but we STAMP the response with
    # the multi_grain reason so the transform/UI labels the headline number
    # "approx upper bound" instead of presenting it as exact. A SINGLE-grain tile
    # (one GA4 property selected) keeps the unlabeled daily query — that scalar IS
    # the property's own figure. DECISIONS.md 2026-06-29.
    if (config.get("chart_type") or "").lower() != "table":
        base_source = _base_source_of(config.get("data_view"))
        # a bar/line/pie grouped (x_field) or split (breakdown) by a
        # WINDOW-ONLY grain dimension (demographics age/gender) has no daily
        # data at all; route it wholly to the window index like the
        # table path. Stat tiles carry no x_field and are unaffected.
        _wo = window_only_grain_value(base_source, _normalize_field(config.get("x_field"))) or window_only_grain_value(
            base_source, _normalize_field(config.get("breakdown"))
        )
        if _wo:
            return _run_window_only_grain_table(config, search_fn, base_source, dashboard_filters=dashboard_filters)
        daily_cfg, dedup_cfg, _plan = split_config_by_index(config)
        sub_reason = (
            multi_grain_reason(config, daily_cfg, search_fn, base_source, dashboard_filters=dashboard_filters)
            if dedup_cfg is not None
            else None
        )

        # a SINGLE-grain scalar tile bound to a LIVE dedup
        # field must SNAP to one window before querying, reading the WINDOW index.
        # Otherwise it reads the raw daily index: activeUsers/totalUsers return the
        # single busiest DAY (max over per-day rows — the one-team "158 vs 3536"
        # symptom), and a sum/sum RATIO (averageEngagementTime) BLENDS every window
        # doc in scope (prod: 9.78m vs the exact-window 11.10m).
        #
        # only snapped a chart ALREADY on the window view.
        # extends it to a chart on the RAW view (the property-first GA4 scorecards,
        # which have no builder-side window picker): route to ``dedup_cfg``'s window
        # index (built by ``_only_fields``, with raw-only filters like report_type
        # already stripped) and snap that. The window doc carries the
        # property-level deduplicated figure, so ``max`` over the one snapped doc IS
        # the exact per-property value. Only when NOT multi-grain (a multi-grain
        # tile is the labeled upper bound below) and the field is live.
        snapped_tile = False
        no_window = False
        # A chart already on the window view snaps itself (shape, ALL
        # sources). A chart on the RAW view routes to its dedup_cfg window index
        # ONLY when the source reads a scalar cleanly from a zero-dimension
        # property-level window doc — an ad-set-grain source
        # (FB/IG) keeps the daily-sum top-line for a bare scalar, so its
        # Summary Report reach tile is untouched.
        on_window = "_window" in (config.get("data_view") or "")
        raw_scalar_routable = not on_window and source_reads_scalar_from_window(base_source)
        if dedup_cfg is not None and not sub_reason and _plan.get("status") == "live" and (on_window or raw_scalar_routable):
            snap_target = config if on_window else dedup_cfg
            snap_view = snap_target["data_view"]
            req_from, req_to = _requested_range(config)
            # enumerate windows within the tile's own filter scope.
            windows = available_windows(search_fn, snap_view, snap_target, dashboard_filters=dashboard_filters)
            snapped = resolve_nearest_window(windows, req_from, req_to) if windows else None
            if snapped:
                snap_cfg = _rescope_to_window(snap_target, snapped)
                # a window-summable tile (messaging conversations)
                # must sum ONE grain: without the exclusion the snapped sum
                # sweeps the ad-set docs PLUS the ad / asset
                # docs and multiplies the figure. Gated on window-summable so a
                # max-agg tile (reach) keeps its earlier doc set
                # byte-identically. The on_window snap_target is the raw chart
                # config, which never passed through _only_fields' injection.
                if any(is_window_summable_field(f) for f in (_plan.get("dedup_fields") or [])):
                    snap_cfg = dict(snap_cfg, filters=_exclude_non_adset_grains(snap_cfg.get("filters")))
                resp = search_fn(snap_view, build_query(snap_cfg, dashboard_filters=dashboard_filters))
                snapped_tile = True
            elif raw_scalar_routable:
                # a raw-view dedup scalar we tried to route found NO
                # pulled window for this (single) grain. Falling through to the raw
                # query would show the per-day max as if it were the dedup figure —
                # the silent-wrong number these tickets kill. Stamp no_window so the
                # tile renders an explained empty (Refresh to pull one) instead. A
                # chart already ON the window view keeps its legacy fall-through
                # (no behavior change).
                no_window = True
        if not snapped_tile:
            resp = search_fn(config["data_view"], build_query(config, dashboard_filters=dashboard_filters))

        if sub_reason and isinstance(resp, dict):
            resp = dict(resp)
            resp["dedup_empty"] = {
                "reason": "multi_grain",
                "message": multi_grain_message(sub_reason, base_source),
                "fields": list(_plan.get("dedup_fields") or []),
            }
        elif no_window and isinstance(resp, dict):
            resp = dict(resp)
            resp["dedup_empty"] = {
                "reason": "no_window",
                "message": dedup_empty_copy("no_window"),
                "fields": list(_plan.get("dedup_fields") or []),
            }
        return resp

    # a table grouped (or sub-rowed) by a WINDOW-ONLY grain dimension
    # (demographics: age/gender). The dimension exists on NO daily doc, so the
    # split/stitch path could only render an empty table: zero daily buckets to
    # stitch into, and additive columns routed to a daily index that has no
    # values for the group-by. Route the WHOLE table to the window index instead.
    _wo_source = _base_source_of(config.get("data_view"))
    _wo_grain = window_only_grain_value(_wo_source, _group_by_of(config)) or window_only_grain_value(
        _wo_source, _normalize_field(config.get("x_field_2"))
    )
    if _wo_grain:
        return _run_window_only_grain_table(config, search_fn, _wo_source, dashboard_filters=dashboard_filters)

    daily_cfg, dedup_cfg, plan = split_config_by_index(config)
    if dedup_cfg is None:
        # No dedup field — single query, unchanged.
        return search_fn(config["data_view"], build_query(config, dashboard_filters=dashboard_filters))

    # a TABLE that is multi-grain (spans 2+ properties, or groups by a
    # dimension finer than the dedup grain — e.g. GA4 activeUsers by city) cannot
    # be deduplicated. Render the labeled upper bound: run the ORIGINAL config
    # (dedup field KEPT, summed on the daily index) so each row carries the
    # daily-summed dedup value, then stamp the multi_grain reason so the UI labels
    # it rather than presenting it as exact. We query `config` (NOT daily_cfg,
    # which strips the dedup field) precisely so the summed value survives.
    # Checked before the window lookups so a sub-dimension table never appears to
    # "have no window." DECISIONS.md 2026-06-29.
    base_source = _base_source_of(config.get("data_view"))
    mg_sub = multi_grain_reason(config, daily_cfg, search_fn, base_source, dashboard_filters=dashboard_filters)
    if mg_sub:
        daily_view = _daily_data_view(config.get("data_view"))
        sum_cfg = dict(config, data_view=daily_view)
        sum_resp = search_fn(daily_view, build_query(sum_cfg, dashboard_filters=dashboard_filters))
        plan = dict(plan, empty_reason="multi_grain", multi_grain_sub_reason=mg_sub, base_source=base_source)
        return _stamp_multi_grain(sum_resp, plan)

    daily_resp = search_fn(daily_cfg["data_view"], build_query(daily_cfg, dashboard_filters=dashboard_filters))

    # `not_built`: the dedup field's source has no window importer
    # yet (registry status=planned, e.g. IG/GA4). The window index doesn't exist,
    # so skip the dedup query entirely and stitch with a column-level reason — the
    # cells render an explained empty instead of a wrong daily-sum.
    if plan.get("status") != "live":
        plan = dict(plan, empty_reason="not_built")
        return stitch_table_responses(daily_resp, {}, plan)

    # nearest-window snap. The dedup docs are keyed to SPECIFIC
    # pulled [start,end] ranges; the viewing range rarely matches one exactly
    # (R2 staging smoke: 18/133). Resolve the nearest available window to the
    # requested range and re-scope the dedup query to its exact bounds so (almost)
    # every ad-set gets a value, LABELED with which window it came from.
    req_from, req_to = _requested_range(config)
    # enumerate windows within the chart + dashboard filter scope,
    # so a team-filtered table snaps to a window THAT TEAM pulled (not the
    # globally newest one another team pulled).
    windows = available_windows(search_fn, dedup_cfg["data_view"], dedup_cfg, dashboard_filters=dashboard_filters)
    if not windows:
        # `no_window`: this team has NO pulled windows at all (never
        # pulled, or a shared/page-filtered account skipped entirely). No dedup
        # query to run; stitch with the column reason.
        plan = dict(plan, empty_reason="no_window")
        return stitch_table_responses(daily_resp, {}, plan)

    snapped = resolve_nearest_window(windows, req_from, req_to)
    # a snapped window must OVERLAP the requested range.
    # NEAREST alone could serve May's dedup number on a June view (18/133
    # exact matches in the R2 smoke); a real-but-wrong-period value misleads
    # more than the explained blank + Refresh. Disjoint → treated like
    # no-window with its own reason copy. Unbounded requests (no from/to)
    # keep the most-recent-window behavior unchanged.
    if snapped and not _window_overlaps_request(snapped, req_from, req_to):
        plan = dict(plan, empty_reason="window_disjoint")
        return stitch_table_responses(daily_resp, {}, plan)
    if snapped:
        dedup_cfg = _rescope_to_window(dedup_cfg, snapped)
        plan = dict(plan, snapped_window=snapped, requested_range={"from": req_from, "to": req_to})

    dedup_resp = search_fn(dedup_cfg["data_view"], build_query(dedup_cfg, dashboard_filters=dashboard_filters))

    # a TEAM-grain table: mark the rows whose team's ads span 2+ ad
    # accounts (team_reach_accounts > 1) as the labeled cross-account upper bound.
    # Single-account rows stay clean (exact). Detected with ONE cheap agg on the
    # same scoped+windowed dedup query so it honors the team_filter selection.
    # the same treatment per (team, page) pair when a page sub-row is
    # active (grain="page" docs, page_reach_accounts), and per page row when the
    # table GROUPS by the page grain directly.
    grain_value = group_by_grain_value(base_source, _group_by_of(config))
    sub_row = _normalize_field(config.get("x_field_2"))
    sub_grain = group_by_grain_value(base_source, sub_row) if sub_row else None
    if grain_value == "team" and sub_grain == "page":
        cross = _cross_account_pair_keys(
            dedup_cfg, search_fn, sub_row, accounts_field="page_reach_accounts", dashboard_filters=dashboard_filters
        )
        if cross:
            plan = dict(
                plan,
                cross_account_keys=cross,
                cross_sub_row=sub_row,
                cross_sub_reason="cross_account_page",
                base_source=base_source,
            )
    elif grain_value == "team":
        cross = _cross_account_team_keys(dedup_cfg, search_fn, dashboard_filters=dashboard_filters)
        if cross:
            plan = dict(plan, cross_account_keys=cross, base_source=base_source)
    elif grain_value == "page":
        cross = _cross_account_team_keys(
            dedup_cfg, search_fn, accounts_field="page_reach_accounts", dashboard_filters=dashboard_filters
        )
        if cross:
            plan = dict(plan, cross_account_keys=cross, cross_sub_reason="cross_account_page", base_source=base_source)
    return stitch_table_responses(daily_resp, dedup_resp, plan)


def _run_window_only_grain_table(config, search_fn, base_source, *, dashboard_filters=None):
    """serve a window-only-grain (demographics) table WHOLLY from the
    window index. No split, no stitch:

      * ``_only_fields`` with EVERY metric field kept repoints the config at the
        window index, prunes filters the window doc can't honor, and
        injects the ``grain`` discriminator.
      * every metric column aggregates with ``max``: a group-by bucket holds its
        (age, gender) cell docs PLUS the "all" marginal doc, whose value IS the
        bucket's exact total — summing would double-count the marginal, and this
        holds for the additive metrics (impressions/spend/clicks) too, because
        the demographics docs carry them per cell and per marginal.
      * nearest-window snap and the cross-account upper-bound
        label (demo_reach_accounts > 1) apply exactly like the other grains.

    Returned in the standard table_buckets shape, so the transform renders it
    unchanged.
    """
    all_fields = [f for f, _kind, _ref in _iter_config_fields(config)]
    suffix = None
    for (src, _field), meta in DEDUP_WINDOW_REGISTRY.items():
        if src == base_source:
            suffix = meta.index_suffix
            break
    if not suffix:
        # A window-only grain implies a window index; without a registry entry
        # there is nothing to route to — fall back to the plain single query.
        return search_fn(config["data_view"], build_query(config, dashboard_filters=dashboard_filters))

    win_cfg = _only_fields(config, set(all_fields), suffix)
    # Force max on every metric column/series (copy dicts — _only_fields keeps
    # the original column objects).
    for key in ("table_columns", "columns"):
        if win_cfg.get(key):
            cols = []
            for col in win_cfg[key]:
                if (
                    isinstance(col, dict)
                    and col.get("field")
                    and col.get("aggregation") not in (None, "", "text", "calculation")
                ):
                    col = dict(col, aggregation="max")
                cols.append(col)
            win_cfg[key] = cols
    # non-table (bar) configs carry one chart-level aggregation for
    # their y_fields; force it to max for the same marginal-doc reason.
    if win_cfg.get("y_fields") or win_cfg.get("y_field"):
        win_cfg["aggregation"] = "max"

    # exclude the "all" SENTINEL bucket for each window-only dim the
    # chart actually GROUPS by (x_field / sub-row / bar breakdown): a grouped
    # dim reads its own per-value docs, so its marginal ("all") doc would render
    # as a bogus extra category/row/series. Marginals of UN-grouped dims must
    # stay in — they are exactly what max reads for the bucket total.
    #
    # the asset grain's marginal sentinel lives on a COMPANION dim
    # (adset_id/adset_name = "all" marks the per-asset cross-ad-set marginal),
    # so an (ad set × asset) table must also exclude adset="all" or the
    # marginal renders as a bogus "all" ad-set row. Companion sentinels are
    # grain-specific; demographics needs none (age/gender are both window-only
    # dims already covered by the rule above).
    _grouped_raw = {
        f
        for f in (
            _normalize_field(_group_by_of(config)),
            _normalize_field(config.get("x_field_2")),
            _normalize_field(config.get("breakdown")),
        )
        if f
    }
    grouped_dims = {f for f in _grouped_raw if window_only_grain_value(base_source, f)}
    active_wo_grain = next(
        (window_only_grain_value(base_source, f) for f in _grouped_raw if window_only_grain_value(base_source, f)),
        None,
    )
    companion_sentinels = {"asset": {"adset_id", "adset_name"}}.get(active_wo_grain, set())
    grouped_dims |= _grouped_raw & companion_sentinels
    if grouped_dims:
        filters = list(win_cfg.get("filters") or [])
        filters.extend({"field": f, "op": "neq", "value": "all"} for f in sorted(grouped_dims))
        win_cfg["filters"] = filters

    # Nearest-window snap (R3). No pulled window at all → run the (empty) query
    # anyway for shape and stamp the explained no_window reason (R4) — never a
    # silent blank, and never a daily fallback (the daily index has no data for
    # this group-by by definition).
    req_from, req_to = _requested_range(config)
    # the demographics chart that motivated the fix: enumerate
    # windows within the filter scope so a team filter snaps to that team's
    # own window instead of blanking against another team's newest pull.
    windows = available_windows(search_fn, win_cfg["data_view"], win_cfg, dashboard_filters=dashboard_filters)
    snapped = resolve_nearest_window(windows, req_from, req_to) if windows else None
    if snapped:
        win_cfg = _rescope_to_window(win_cfg, snapped)

    resp = search_fn(win_cfg["data_view"], build_query(win_cfg, dashboard_filters=dashboard_filters)) or {}
    resp = dict(resp)

    dedup_fields = [f for f in all_fields if _registry_entry_for(base_source, f) is not None]
    if snapped:
        resp["dedup_window"] = {
            "start": snapped.get("start"),
            "end": snapped.get("end"),
            "label": snapped.get("label") or _window_label(snapped.get("start"), snapped.get("end")),
            "fields": dedup_fields or all_fields,
        }
    elif not windows:
        resp["dedup_empty"] = {
            "reason": "no_window",
            "message": dedup_empty_copy("no_window"),
            "fields": dedup_fields or all_fields,
        }
        return resp

    # Cross-account rows: a (team spanning 2+ ad accounts) demographics cell is
    # the SUM of per-account deduped cells — label its non-additive (reach)
    # columns as the upper bound, exactly like the team/page grains. Additive
    # columns (impressions/spend) sum exactly across accounts and stay clean.
    if dedup_fields:
        # each window-only grain stamps its own accounts counter on
        # the docs that can span accounts (demographics cells; asset marginals).
        _accounts_field = {"asset": "asset_reach_accounts"}.get(active_wo_grain, "demo_reach_accounts")
        cross = _cross_account_team_keys(
            win_cfg, search_fn, accounts_field=_accounts_field, dashboard_filters=dashboard_filters
        )
        if cross:
            message = multi_grain_message("cross_account_demographics", base_source)
            aggs = dict(resp.get("aggregations") or {})
            tb = dict(aggs.get("table_buckets") or {})
            buckets = []
            for b in tb.get("buckets") or []:
                nb = dict(b)
                if _group_by_value(b, _group_by_of(config)) in cross:
                    nb["dedup_empty"] = {f: "multi_grain" for f in dedup_fields}
                buckets.append(nb)
            if tb:
                tb["buckets"] = buckets
                aggs["table_buckets"] = tb
                resp["aggregations"] = aggs
            resp["dedup_empty"] = {"reason": "multi_grain", "message": message, "fields": dedup_fields}
    return resp


def _cross_account_team_keys(dedup_cfg, search_fn, *, accounts_field="team_reach_accounts", dashboard_filters=None):
    """the set of group-by keys whose grain doc reports ads across 2+
    ad accounts (``max(<accounts_field>) > 1``) within the scoped + snapped dedup
    query. Those rows are the summed-across-accounts upper bound and get the
    cross_account label; single-account rows (max == 1) stay clean.

    ``accounts_field`` parameterizes the doc field so a page-GROUPED
    table reads ``page_reach_accounts`` from the grain="page" docs with the same
    mechanics (default unchanged: the team grain).

    One lightweight agg on the SAME dedup_cfg (scope + window + grain filter
    already applied by _only_fields) so the selection/window match the main query.
    Fail-soft: any error → empty set (no false labels; a missing label is safer
    than a wrong one on an exact single-account row)."""
    group_by = _group_by_of(dedup_cfg)
    if not group_by:
        return set()
    field = group_by if group_by.endswith(".keyword") else group_by
    try:
        body = {
            "size": 0,
            "query": build_query(dedup_cfg, dashboard_filters=dashboard_filters).get("query", {"match_all": {}}),
            "aggs": {
                "by_team": {
                    "terms": {"field": field, "size": 1000},
                    "aggs": {"max_accounts": {"max": {"field": accounts_field}}},
                }
            },
        }
        resp = search_fn(dedup_cfg["data_view"], body) or {}
        buckets = (((resp.get("aggregations") or {}).get("by_team") or {}).get("buckets")) or []
        cross = set()
        for b in buckets:
            n = ((b.get("max_accounts") or {}).get("value")) or 0
            if n and n > 1:
                cross.add(b.get("key"))
        return cross
    except Exception:  # noqa: BLE001 — fail-soft: no label beats a wrong label
        logging.warning("cross-account detection failed; team rows unlabeled (assumed exact).")
        return set()


def _cross_account_pair_keys(dedup_cfg, search_fn, sub_field, *, accounts_field, dashboard_filters=None):
    """the set of (group-by value, sub-row value) pairs whose
    grain="page" doc reports ads across 2+ ad accounts. The page-sub-row analog
    of ``_cross_account_team_keys``: a nested terms agg over (group_by, sub_field)
    with ``max(page_reach_accounts)`` per pair. Fail-soft to an empty set."""
    group_by = _group_by_of(dedup_cfg)
    if not group_by or not sub_field:
        return set()
    try:
        body = {
            "size": 0,
            "query": build_query(dedup_cfg, dashboard_filters=dashboard_filters).get("query", {"match_all": {}}),
            "aggs": {
                "by_group": {
                    "terms": {"field": group_by, "size": 1000},
                    "aggs": {
                        "by_sub": {
                            "terms": {"field": sub_field, "size": 1000},
                            "aggs": {"max_accounts": {"max": {"field": accounts_field}}},
                        }
                    },
                }
            },
        }
        resp = search_fn(dedup_cfg["data_view"], body) or {}
        cross = set()
        for gb in (((resp.get("aggregations") or {}).get("by_group") or {}).get("buckets")) or []:
            for sb in ((gb.get("by_sub") or {}).get("buckets")) or []:
                n = ((sb.get("max_accounts") or {}).get("value")) or 0
                if n and n > 1:
                    cross.add((gb.get("key"), sb.get("key")))
        return cross
    except Exception:  # noqa: BLE001 — fail-soft: no label beats a wrong label
        logging.warning("page cross-account detection failed; page rows unlabeled (assumed exact).")
        return set()


# the ES date math the dashboard time presets send (charts.js passes
# the global range through raw for non-DT views): ``now`` and ``now-<N><unit>``
# with an optional ``/d`` rounding. Units: d (day), w (week), M (month), y (year).
_DATE_MATH_RE = re.compile(r"^now(?:-(\d+)([dwMy]))?(?:/[dwMy])?$")


def _resolve_date_math_day(value, today=None):
    """resolve ES date math to the ``yyyy-MM-dd`` UTC day it names.

    The window snap measures day distances, so it needs calendar dates. Before
    this, ``now-7d/d`` / ``now`` reached ``_to_ordinal`` unparsed, the request
    looked unbounded, and every preset (last 7 / 30 / 90 days) snapped to the
    most-recent window — the rolling last-30, because the nightly last-30 and
    last-7 share an end date and the last-30 sorts first. UTC matches how ES
    evaluates ``now`` (build_query sets no time_zone). Anything that is not
    date math (an ISO date, None) is returned unchanged.
    """
    if not isinstance(value, str):
        return value
    m = _DATE_MATH_RE.match(value.strip())
    if not m:
        return value
    day = today or datetime.now(timezone.utc).date()
    n, unit = m.group(1), m.group(2)
    if n:
        n = int(n)
        if unit == "d":
            day = day - timedelta(days=n)
        elif unit == "w":
            day = day - timedelta(weeks=n)
        else:
            months = n if unit == "M" else n * 12
            y, mo = divmod(day.year * 12 + (day.month - 1) - months, 12)
            mo += 1
            last = (date(y + mo // 12, mo % 12 + 1, 1) - timedelta(days=1)).day
            day = date(y, mo, min(day.day, last))
    return day.isoformat()


def _requested_range(config, today=None):
    """Return (from, to) the chart is viewing — from date_range or global filters.

    None values mean "unbounded"; the snap then has no anchor and falls back to
    the most-recent window (handled in resolve_nearest_window). —
    date-math bounds (the time presets) are resolved to UTC days first.
    """
    dr = config.get("date_range") or {}
    return _resolve_date_math_day(dr.get("from"), today), _resolve_date_math_day(dr.get("to"), today)


def available_windows(search_fn, dedup_index, scope_cfg=None, *, dashboard_filters=None):
    """Return the distinct pulled windows present in a dedup index.

    Each is ``{"start", "end", "pulled_at"}``. Aggregates on
    ``insight_window_start`` (keyword) and reads the paired end + a representative
    ``@timestamp`` from a top hit per window. Reuses the window-field
    convention. ``search_fn(index, body)`` runs the (scoped) ES search.

    ``scope_cfg`` (the dedup/window chart config the caller is about
    to query) constrains the enumeration to windows that exist WITHIN the
    chart + dashboard filter scope. Without it, a team-filtered chart snapped to
    the globally newest window (pulled by OTHER teams only), pinned the query to
    it, and the ANDed team filter matched nothing → blank chart. The cfg's
    ``date_range`` is dropped for enumeration — windows are listed across all
    time; the snap picks the nearest to the requested range. Fail-open: if the
    scoped query can't be built, fall back to the unscoped enumeration (the
    earlier behavior) rather than hiding every window.
    """
    exists_clause = {"exists": {"field": "insight_window_start"}}
    query = exists_clause
    if scope_cfg is not None:
        scoped = None
        try:
            cfg = dict(scope_cfg)
            cfg.pop("date_range", None)
            scoped = build_query(cfg, dashboard_filters=dashboard_filters).get("query")
        except Exception:  # noqa: BLE001 — fail-open: unscoped enumeration beats no windows at all
            logging.warning("scoped window enumeration failed; falling back to unscoped")
        if scoped:
            query = {"bool": {"must": [scoped, exists_clause]}}
    # enumerate by the (start, end) PAIR, not start alone. Multiple
    # windows share a start (a team pulled 2026-06-01..09, ..12, AND ..17). A
    # terms-on-start agg collapsed them into one bucket and a top_hits size=1
    # arbitrarily kept ONE end — hiding the others (incl. the one that exactly
    # matches the view) from the snap. A composite agg over both fields yields
    # every distinct window. ``top`` carries a representative @timestamp (pulled_at)
    # per pair so the unbounded-request fallback can still prefer fresher pulls.
    body = {
        "size": 0,
        "query": query,
        "aggs": {
            "windows": {
                "composite": {
                    "size": 500,
                    "sources": [
                        {"start": {"terms": {"field": "insight_window_start"}}},
                        {"end": {"terms": {"field": "insight_window_end"}}},
                    ],
                },
                "aggs": {
                    "top": {
                        "top_hits": {
                            "size": 1,
                            "_source": ["insight_window_start", "insight_window_end", "@timestamp"],
                        }
                    }
                },
            }
        },
    }
    resp = search_fn(dedup_index, body) or {}
    buckets = (((resp.get("aggregations") or {}).get("windows") or {}).get("buckets")) or []
    windows = []
    for b in buckets:
        hits = (((b.get("top") or {}).get("hits") or {}).get("hits")) or []
        src = (hits[0].get("_source") if hits else None) or {}
        # Prefer the doc's own start/end; fall back to the composite key (which is
        # epoch-millis for a date field — the _source values are the iso strings).
        key = b.get("key") or {}
        start = src.get("insight_window_start") or key.get("start")
        end = src.get("insight_window_end") or key.get("end")
        if start and end:
            windows.append({"start": start, "end": end, "pulled_at": src.get("@timestamp")})
    return windows


def _to_ordinal(date_str):
    """yyyy-MM-dd → integer day ordinal for distance math. None on unparseable."""
    if not date_str or not isinstance(date_str, str):
        return None
    parts = date_str[:10].split("-")
    if len(parts) != 3:
        return None
    # a real calendar ordinal. The old y*365 + m*31 + d made every
    # 30-day month boundary a 2-day gap, which skewed window distances.
    try:
        return date(int(parts[0]), int(parts[1]), int(parts[2])).toordinal()
    except ValueError:
        return None


def _window_overlaps_request(window, req_from, req_to):
    """True when the window shares at least one day with the
    requested range (or the request is unbounded / unparseable, which keeps
    the pre-existing most-recent-window behavior). Pure."""
    rf, rt = _to_ordinal(req_from), _to_ordinal(req_to)
    if rf is None or rt is None:
        return True
    ws, we = _to_ordinal(window.get("start")), _to_ordinal(window.get("end"))
    if ws is None or we is None:
        return True
    return not (we < rf or ws > rt)


def resolve_nearest_window(windows, req_from, req_to):
    """Pick the window nearest the requested range. Pure.

    Distance = |window_start − requested_from| + |window_end − requested_to| (day
    ordinals;, was the midpoint distance). a teammate's rule
    (DECISIONS 2026-06-20): plain NEAREST (not nearest-≥) + inline refresh for the
    exact range. Deterministic tie-break: the LATER (more recent) window wins, so
    ties resolve to fresher data. When the requested range is unbounded (no
    from/to), fall back to the most-recent window by end date.
    """
    if not windows:
        return None

    rf, rt = _to_ordinal(req_from), _to_ordinal(req_to)
    if rf is None or rt is None:
        # Unbounded request → most-recent window (greatest end).
        return max(windows, key=lambda w: (_to_ordinal(w["end"]) or -1))

    # EXACT match first (a teammate's rule). When a pulled window's bounds
    # equal the requested range, use it — so a Jun 1–17 view with a Jun 1–17 pull
    # shows that window's exact value + a label the user can trust. Compare by day
    # ordinal so iso strings and epoch keys normalise the same way.
    for w in windows:
        if _to_ordinal(w["start"]) == rf and _to_ordinal(w["end"]) == rt:
            return w

    # distance = how far each bound is from the requested bound.
    # The midpoint distance it replaces ignored span: the nightly import keeps a
    # rolling last-7/8/30 window for every day, so for a Last 30 days view an
    # 8-day window centred in the range scored as near as the 30-day window that
    # matches it (prod: 09-17..09-24 picked for 09-05..10-05).
    def _dist(w):
        ws, we = _to_ordinal(w["start"]), _to_ordinal(w["end"])
        if ws is None or we is None:
            return (float("inf"), -1)
        # Tie-break: smaller distance first; then LATER end (fresher) first.
        return (abs(ws - rf) + abs(we - rt), -(we))

    return min(windows, key=_dist)


def _rescope_to_window(dedup_cfg, window):
    """Return a dedup cfg copy pinned to the snapped window's EXACT bounds.

    pin BOTH ``insight_window_start`` AND ``insight_window_end`` to the
    snapped window's dates, so the dedup query reads exactly ONE window's docs.
    The previous version set a date_range only on ``insight_window_start`` covering
    [start, end] — which matched EVERY window sharing that start (a team had three
    Jun-1 windows: ..09, ..12, ..17), so ``max(reach)`` blended them and returned
    the largest by accident. Pinning both bounds isolates the chosen window.

    Pin start via ``date_range`` (exact day, [start, start]) and end via an ``eq``
    filter. Both fields are dates; ES coerces the ISO term/range bounds.
    """
    out = dict(dedup_cfg)
    dr = dict(out.get("date_range") or {})
    dr["field"] = "insight_window_start"
    dr["from"] = window["start"]
    dr["to"] = window["start"]  # exact start day, not [start, end]
    out["date_range"] = dr
    # Pin the end too (exact). Appended as an eq filter so build_query AND-clauses it.
    existing = [f for f in (out.get("filters") or []) if f.get("field") != "insight_window_end"]
    existing.append({"field": "insight_window_end", "op": "eq", "value": window["end"]})
    out["filters"] = existing
    return out
