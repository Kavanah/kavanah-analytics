"""
ES Query Builder
Builds Elasticsearch query bodies from dashboard chart config dicts.
"""

import hashlib
import os
import re
from datetime import datetime, timedelta, timezone

# ISO yyyy-MM-dd values must carry an explicit input format on the
# range clause; otherwise ES tries to parse them using the field's stored
# format. (earlier Echo's kibana_date was stored as ``MMMM dd, yyyy
# HH:mm:ss 'UTC'`` and rejected bare yyyy-MM-dd; reindexed Echo to
# yyyy-MM-dd, but other custom-format date fields could still appear.)
_ISO_DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")

# ``terms_size`` arrives in the client's chart config (public and
# authenticated query paths alike). Unbounded, one request asks ES for an
# arbitrary bucket count; clamp it. No shipped chart sends more than the default.
_TERMS_SIZE_DEFAULT = 100
_TERMS_SIZE_MAX = 1000


def _clamped_terms_size(raw) -> int:
    """Return ``raw`` as an int in [1, _TERMS_SIZE_MAX]; the default when unparseable."""
    try:
        value = int(raw)
    except (TypeError, ValueError):
        return _TERMS_SIZE_DEFAULT
    return max(1, min(value, _TERMS_SIZE_MAX))


def _iso_to_epoch_ms(iso_value, *, upper_bound=False):
    """Convert a yyyy-MM-dd string to an epoch-millisecond integer.

    Used for ``date_histogram.extended_bounds``, which has no
    ``format`` slot of its own. Epoch_millis is accepted by ES on any date
    field regardless of the field's stored format. Non-ISO inputs (date
    math like ``now-3M/d`` or numbers already in epoch_millis) pass
    through unchanged so callers can mix and match.
    """
    if not isinstance(iso_value, str) or not _ISO_DATE_RE.match(iso_value):
        return iso_value
    dt = datetime.strptime(iso_value, "%Y-%m-%d").replace(tzinfo=timezone.utc)
    if upper_bound:
        # Inclusive end-of-day so a ``to=2024-12-31`` bucket actually contains
        # docs from that day, when the field stores a full timestamp.
        dt = dt + timedelta(days=1)
    return int(dt.timestamp() * 1000)


def _date_range_clause(date_from, date_to):
    """Return a range clause dict for the given from/to values.

    Adds ``format: yyyy-MM-dd`` when either bound is a plain ISO date so ES
    parses the input regardless of the field's stored format. Date math
    (``now-3M/d``) and epoch_millis numbers are left alone — ES handles those
    natively on any date field.
    """
    clause = {}
    if date_from:
        clause["gte"] = date_from
    if date_to:
        clause["lte"] = date_to
    if not clause:
        return None
    iso_bound = (isinstance(date_from, str) and _ISO_DATE_RE.match(date_from)) or (
        isinstance(date_to, str) and _ISO_DATE_RE.match(date_to)
    )
    if iso_bound:
        clause["format"] = "yyyy-MM-dd"
    return clause


def _field_variants(field):
    """Return the field-name variants a filter clause should target.

    Dashboard charts persist filter fields with the ``.keyword`` suffix
    (the front-end picks the aggregatable variant when the dashboard is
    built). But the importer schemas on the IG, FB Page, GA, and Chatwoot
    sides map fields like ``team_esid`` / ``team_title`` / ``organization_title``
    as bare ``keyword`` with no ``.keyword`` sub-field. A bare ``term``
    query against ``team_esid.keyword`` returns 0 hits silently because
    the sub-field doesn't exist in the mapping. The dashboard then
    renders empty.

    For ``.keyword``-suffixed fields we emit clauses against both the
    suffixed and bare forms via ``bool/should``. ES skips a clause that
    targets a non-existent field (no error, no match), so whichever shape
    the index actually has wins. Bare-field filters are left alone —
    if the user / front-end picked the bare variant, we trust it.

    Returns a list with 1 or 2 entries; callers decide whether to emit
    a single clause or a ``bool/should`` over the variants.
    """
    if field and field.endswith(".keyword"):
        return [field, field[: -len(".keyword")]]
    return [field]


def _term_variants(field, value):
    """Return a ``term`` clause, expanded to ``bool/should`` over both
    field-name variants when ``field`` ends with ``.keyword``."""
    variants = _field_variants(field)
    if len(variants) == 1:
        return {"term": {variants[0]: value}}
    return {
        "bool": {
            "should": [{"term": {v: value}} for v in variants],
            "minimum_should_match": 1,
        }
    }


def _terms_variants(field, values):
    """Same shape as ``_term_variants`` but for the ``terms`` operator (``in``)."""
    variants = _field_variants(field)
    if len(variants) == 1:
        return {"terms": {variants[0]: values}}
    return {
        "bool": {
            "should": [{"terms": {v: values}} for v in variants],
            "minimum_should_match": 1,
        }
    }


def _match_phrase_variants(field, value):
    """Same shape but for ``match_phrase``."""
    variants = _field_variants(field)
    if len(variants) == 1:
        return {"match_phrase": {variants[0]: value}}
    return {
        "bool": {
            "should": [{"match_phrase": {v: value}} for v in variants],
            "minimum_should_match": 1,
        }
    }


OPERATOR_MAP = {
    "eq": lambda f, v: _term_variants(f, v),
    "neq": lambda f, v: {"bool": {"must_not": _term_variants(f, v)}},
    "gt": lambda f, v: {"range": {f: {"gt": v}}},
    "gte": lambda f, v: {"range": {f: {"gte": v}}},
    "lt": lambda f, v: {"range": {f: {"lt": v}}},
    "lte": lambda f, v: {"range": {f: {"lte": v}}},
    "between": lambda f, v: {"range": {f: {"gte": v[0], "lte": v[1]}}},
    "in": lambda f, v: _terms_variants(f, v),
    "match_phrase": lambda f, v: _match_phrase_variants(f, v),
    "match_phrase_any": lambda f, v: {
        "bool": {
            "should": [_match_phrase_variants(f, t) for t in (v if isinstance(v, list) else [v])],
            "minimum_should_match": 1,
        }
    },
}

METRIC_AGG_MAP = {
    "sum": "sum",
    "avg": "avg",
    "min": "min",
    "max": "max",
    "count": "value_count",
    "cardinality": "cardinality",
}

# sources whose importers write MULTIPLE record_types per entity/day:
# one primary/perf record (no record_type) plus breakdown records stored
# ALONGSIDE it (not instead of it). A generic tile that sums a metric over all
# record_types re-counts the breakdown's cost/impressions on top of the primary
# total. Map: source slug -> the breakdown record_types to exclude by default.
# The primary record carries no ``record_type`` value, so excluding the
# breakdown types via ``must_not`` leaves exactly the perf docs.
_BREAKDOWN_RECORD_TYPES = {
    "google_ads": ["geo", "campaign_metadata"],
    "youtube_analytics": ["country_breakdown", "video_retention"],
    # one account_daily doc per account per day plus one video_daily
    # doc per video per day. Account-level tiles read the account docs; a
    # per-video tile targets record_type=video_daily explicitly (the override).
    "tiktok_organic": ["video_daily"],
}


def _filters_reference_record_type(chart_filters):
    """True if any filter dict in the list targets ``record_type`` (any op).

    A user (or a seeded tile) that explicitly targets ``record_type`` overrides
    the default exclusion — we must not stack our must_not on top of an
    intentional filter. Covers every op the builder UI can emit.
    """
    for f in chart_filters or []:
        if isinstance(f, dict) and f.get("field") == "record_type":
            return True
    return False


def breakdown_exclusion_clause(data_view, *, include_breakdown_records=False, chart_filters=None):
    """Return a ``must_not`` clause excluding breakdown record_types, or ``None``.

    shared by every query path that aggregates a metric over a
    multi-record-type source (``build_query``/``_build_filters`` and the
    trend-line + trend-export paths in routes.py). Returns ``None`` when no
    exclusion should apply:

      * the data view's source is not multi-record-type (fb_insights, ga, echo…);
      * ``include_breakdown_records`` is truthy (the Builder checkbox opt-in);
      * an explicit ``record_type`` filter is already present (user override).

    Otherwise returns a ``bool.must_not`` over ``match`` clauses — ``match`` (not
    ``term``) so it holds whether ``record_type`` is mapped keyword or text on
    older indices, mirroring the geo endpoints. A must_not on an unmapped field
    is a no-op, so a misfire on a non-guarded index is harmless.
    """
    if include_breakdown_records:
        return None
    breakdown_types = _BREAKDOWN_RECORD_TYPES.get(source_from_data_view(data_view))
    if not breakdown_types:
        return None
    if _filters_reference_record_type(chart_filters):
        return None
    return {"bool": {"must_not": [{"match": {"record_type": rt}} for rt in breakdown_types]}}


def _safe_agg_key(field_name):
    """Convert a field name to a safe ES aggregation key (no dots or hyphens)."""
    return field_name.replace(".", "__").replace("-", "_")


def _equals_col_agg_key(field, equals_value):
    """agg key for a standard-table column using the ``equals`` aggregation.

    Table agg results are normally keyed by field name only, which would
    collapse multiple equals columns on the SAME field (an outcomes table
    counts ``outcome == <several values>``, one column per value). Equals
    columns are instead keyed by field + a sanitized digest of their
    equals_value — deterministic, so ``echarts_transformer`` derives the
    identical key when mapping columns back to row values. The value (not a
    column position) is the discriminator because the auto-promote branch
    filters ``table_columns``, which would shift positional indices between
    the query builder and the transformer.
    """
    raw = str(equals_value or "")
    sanitized = re.sub(r"\W+", "_", raw)[:24]
    # usedforsecurity=False: this is a stable key derivation, not a hash for
    # integrity/auth — md5 keeps the agg key short.
    digest = hashlib.md5(raw.encode("utf-8"), usedforsecurity=False).hexdigest()[:6]
    return f"metric_{_safe_agg_key(field)}__eq_{sanitized}_{digest}"


def table_metric_agg_keys(table_columns):
    """map each field-bound table column to its ES sub-agg key,
    disambiguating columns that share a ``field`` but differ by ``aggregation``.

    A table's metric sub-aggs were keyed ``metric_<field>`` (field name only),
    so two columns on the same field (e.g. client_id counted AND client_id
    unique) collided: only the first column's aggregation ran and BOTH output
    columns read its value (the one-team "Conversations" == "Unique
    Conversations" symptom). When a field appears once, its key is the plain
    ``metric_<field>`` (byte-identical to before — single-aggregation tables,
    the overwhelming majority, are completely unchanged). When a field appears
    on 2+ columns with DIFFERENT aggregations, each distinct aggregation gets a
    suffixed key ``metric_<field>__agg_<aggregation>``.

    Keyed by ``(field, aggregation)`` — NOT column position — because the
    auto-promote branch filters ``table_columns`` between the query builder and
    the transformer (same stability constraint as ``_equals_col_agg_key``); a
    positional scheme would drift between the two sides. Returns
    ``{(field, aggregation): agg_key}``. ``equals`` and ``calculation`` columns
    are excluded — they own their key schemes (``_equals_col_agg_key`` / the
    per-formula-field sub-aggs) and never collide on the bare field key.

    Two columns with the SAME field AND SAME aggregation still share a key —
    they are identical columns showing identical data, harmless and unchanged.
    """
    from collections import defaultdict

    aggs_per_field = defaultdict(set)
    for col in table_columns or []:
        field = col.get("field")
        agg = col.get("aggregation")
        if not field or agg in ("equals", "calculation"):
            continue
        aggs_per_field[field].add(agg)

    keys = {}
    for field, aggs in aggs_per_field.items():
        base = f"metric_{_safe_agg_key(field)}"
        if len(aggs) <= 1:
            # Single aggregation for this field → the legacy plain key.
            for agg in aggs:
                keys[(field, agg)] = base
        else:
            # Field carries multiple distinct aggregations → suffix each so the
            # sub-aggs (and the values read back) stay distinct per column.
            for agg in aggs:
                keys[(field, agg)] = f"{base}__agg_{_safe_agg_key(str(agg))}"
    return keys


def _terms_field(field_name, field_type=None, *, field_types=None):
    """Return the aggregatable version of a field for use in terms aggregations.

    ES text fields don't support terms aggregations — automatically use the
    .keyword sub-field when the field type is text and no .keyword suffix exists.

    the inverse case. A dashboard chart may persist its Row/x_field
    WITH a ``.keyword`` suffix (the builder UI offered the aggregatable variant
    when the field was text). But some importer schemas map that field as a
    bare ``keyword`` with NO ``.keyword`` sub-field (e.g. YouTube's
    ``youtube_channel_handle``). Unlike a *filter* clause — which
    ``_field_variants`` expands to a bool/should over both forms so the
    non-existent one is harmlessly skipped — a ``terms``/composite agg SOURCE
    can target only one field, and a source pointed at a missing field yields
    ZERO buckets (no error), so the whole table/chart silently renders empty.

    So when the field carries a ``.keyword`` suffix but its base field is itself
    a ``keyword`` (or otherwise aggregatable non-text) type, strip the redundant
    suffix and aggregate on the field that actually exists. ``field_types`` is
    the ``{field_name: es_type}`` map the caller resolved from the live mapping;
    when it's absent or the base type is unknown we leave the field unchanged
    (the prior behavior — safe for every chart that worked before).
    """
    if field_type == "text" and not field_name.endswith(".keyword"):
        return f"{field_name}.keyword"
    if field_name.endswith(".keyword") and field_types:
        base = field_name[: -len(".keyword")]
        # The .keyword sub-field genuinely exists → keep it. Otherwise, if the
        # base field is a non-text aggregatable type, the suffix is redundant
        # and points at nothing — fall back to the base field.
        if field_name not in field_types and field_types.get(base) not in (None, "text"):
            return base
    return field_name


# the Date Format dropdown in index.html emits human-friendly
# labels designed for client-side rendering (charts.js _applyDateFormat),
# NOT Java DateTimeFormatter patterns ES expects in ``date_histogram.format``.
# earlier the labels were passed through unchanged, so picking
# "DD Mon YYYY" expecting "01 Jan 2024" yielded either an ES rejection or a
# bucket key formatted with literal "DD" / "Mon" tokens (where Java's "DD"
# means day-of-year, not day-of-month, and "Mon" is a literal string).
#
# This table maps each dropdown label to its Java DateTimeFormatter equivalent.
# Pass-through default — an unrecognised value (custom format, blank, or
# already-Java-syntax) is returned as-is so existing dashboards keep working.
_DATE_FORMAT_LABEL_TO_JAVA = {
    "": "yyyy-MM-dd",
    # Legacy 6-option line/bar labels (kept so old dashboards keep working).
    "Mon YYYY": "MMM yyyy",
    "Month YYYY": "MMMM yyyy",
    "YYYY-MM": "yyyy-MM",
    "DD Mon YYYY": "dd MMM yyyy",
    "DD Mon": "dd MMM",
    # the table column DATE_FORMAT_OPTIONS token set, now shared by
    # the line/bar "Data Format" dropdown. Each maps to its ES Java
    # DateTimeFormatter pattern (dd=day-of-month, MM=month, MMM/MMMM=short/long
    # month name, yyyy=year). "MM/YYYY" already existed above; the rest are new.
    "YYYY-MM-DD": "yyyy-MM-dd",
    "DD/MM/YYYY": "dd/MM/yyyy",
    "MM/DD/YYYY": "MM/dd/yyyy",
    "MMM DD, YYYY": "MMM dd, yyyy",
    "MMMM DD, YYYY": "MMMM dd, yyyy",
    "MMM YYYY": "MMM yyyy",
    "MMMM YYYY": "MMMM yyyy",
    "YYYY": "yyyy",
    "MM/YYYY": "MM/yyyy",
    "DD": "dd",
    "MM": "MM",
}


def _map_date_format_to_es(label):
    """Translate a Date Format dropdown label to its ES Java DateTimeFormatter
    equivalent. Unknown values pass through unchanged so custom formats
    (or already-Java-syntax strings from old dashboards) keep working.
    """
    if label is None:
        return "yyyy-MM-dd"
    return _DATE_FORMAT_LABEL_TO_JAVA.get(label, label)


def _build_filters(config, *, dashboard_filters=None, active_data_view=None):
    """Return a list of ES filter clauses from config filters and date_range.

    when ``dashboard_filters`` is supplied, each entry is merged in
    as a ``terms`` clause on its ``field`` provided two conditions hold:

    1. The filter's ``data_view`` matches ``active_data_view`` (filters apply
       only to charts whose DV they target — plan, decision 6).
    2. The filter's ``selected_values`` is a non-empty list (empty selection
       means "no filter applied" — decision 7).

    Multiple dashboard filters AND together (each adds its own clause to the
    ``filter`` array; ES bool ANDs them). Values within one filter OR via the
    terms clause's natural OR semantics (decision 8).

    The keyword-only signature keeps every existing caller working unchanged.
    """
    filters = []

    for f in config.get("filters", []):
        op = f.get("op", "eq")
        handler = OPERATOR_MAP.get(op)
        if handler:
            filters.append(handler(f["field"], f["value"]))

    dr = config.get("date_range")
    if dr and dr.get("field") and (dr.get("from") or dr.get("to")):
        range_clause = _date_range_clause(dr.get("from"), dr.get("to"))
        if range_clause:
            filters.append({"range": {dr["field"]: range_clause}})

    if dashboard_filters and active_data_view:
        for df in dashboard_filters:
            if not isinstance(df, dict):
                continue
            if df.get("data_view") != active_data_view:
                continue
            field = df.get("field")
            values = df.get("selected_values") or []
            if not field or not values:
                continue
            # Expand .keyword-suffixed fields to bool/should over both
            # variants so the filter resolves against indices mapped as
            # bare ``keyword`` (no sub-field) — same drift the chart
            # filters above handle.
            filters.append(_terms_variants(field, list(values)))

    # legacy "no data" marker docs (``no_data: true``) were retired
    # (nothing writes or reads them) but were left in the data
    # indices as inert: team_title + zero metrics + no dimension fields. They
    # never influenced sums, but they DO materialize aggregation buckets —
    # most visibly the phantom all-zero "(none)" sub-rows the
    # missing_bucket exposed. Exclude them from every chart query; a term on
    # an unmapped field inside must_not is a no-op, so indices without the
    # field are unaffected.
    filters.append({"bool": {"must_not": {"term": {"no_data": True}}}})

    # default-exclude breakdown record_types on multi-record-type
    # sources so a generic tile sums primary/perf docs only, not perf + breakdown.
    # Opt back in via the Builder checkbox (include_breakdown_records) or an
    # explicit record_type filter. See breakdown_exclusion_clause for the rules.
    breakdown_clause = breakdown_exclusion_clause(
        active_data_view,
        include_breakdown_records=config.get("include_breakdown_records"),
        chart_filters=config.get("filters"),
    )
    if breakdown_clause:
        filters.append(breakdown_clause)

    return filters


_NUMERIC_ONLY_AGGS = {"sum", "avg", "min", "max"}


# fields that must aggregate as ratios: sum(numerator) / sum(denominator).
# When a metric column's field is in this registry, build_query ignores the
# user's stored aggregation value and emits two `sum` sub-aggs plus a
# `bucket_script` divide. The bucket_script result is keyed under the field's
# normal `metric_<field>` slot so the response transformer reads it without
# any change. Phase 26 seeds the registry with one entry; Phase 27 expands
# to the remaining FB cost_per_* family. See DECISIONS.md 2026-05-12 for the
# Option A aggregation contract this enforces.
RATIO_FIELDS = {
    "cost_per_result": {"numerator": "insight_spend", "denominator": "result_count"},
    # (Phase 27 expansion): CTR-family fields are ratios just like
    # cost_per_result. `avg(per-platform ctr)` over-weights low-impression
    # platforms (audience_network with 10 imps and 1 click would contribute
    # 10% CTR equally to facebook with 100k imps and 1k clicks at 1%). The
    # correct aggregation is sum(clicks) / sum(impressions) — what FB's own
    # combined-row CTR reports — emitted via the same bucket_script path
    # wired for cost_per_result.
    #
    # ``scale``: multiplier applied inside the bucket_script. FB stores CTR
    # as a percentage already (e.g. clicks=4192 / imps=194160 = 0.02159 raw
    # ratio, but ``insight_ctr`` is stored as ``2.159``). Our bucket_script
    # output must match FB's percentage convention so the displayed value
    # tracks both the per-platform docs and Data Studio. Default 1 (no
    # scaling) preserves the cost_per_result contract.
    "insight_ctr": {
        "numerator": "insight_clicks",
        "denominator": "insight_impressions",
        "scale": 100,
    },
    "insight_inline_link_click_ctr": {
        "numerator": "insight_inline_link_clicks",
        "denominator": "insight_impressions",
        "scale": 100,
    },
    # Cost per New Messaging Contact is spend over conversations
    # started, exactly like cost_per_result. It was unregistered, so a column
    # bound to it summed FB's per-(day, platform) cost values — wrong per row
    # and wildly wrong in the grand-totals row that surfaced the bug. Both
    # operands ride every doc (mapped daily+window), so the ratio works on the
    # Ad Sets window tables too.
    "insight_cost_per_action_type_onsite_conversion_messaging_conversation_started_7d": {
        "numerator": "insight_spend",
        "denominator": "insight_actions_onsite_conversion_messaging_conversation_started_7d",
    },
    # frequency is impressions over reach, the sibling that
    # never got registered. Unregistered, it fell through to the stored agg on
    # the daily index (avg of per-(day, platform) frequencies reads ~0.2 low —
    # daily frequencies are ~1.0-1.3 while the multi-day figure is ~1.5+) and
    # to the ``max`` on window views (the largest slice's frequency,
    # verified 2.11 vs Ads Manager's 1.54 on one team). Both operands ride daily
    # AND window docs. On a window doc sum/sum == the doc's own FB-computed
    # ratio (exact); on the daily index the denominator is summed (non-dedup)
    # reach, making this the same labeled approximation as the reach tile
    # itself — and keeping reach × frequency == impressions
    # internally consistent on every surface.
    "insight_frequency": {
        "numerator": "insight_impressions",
        "denominator": "insight_reach",
    },
    # GA4 "Average engagement time" is a ratio, not a stored field:
    # total userEngagementDuration (seconds) over activeUsers, matching GA's own
    # "average engagement time per active user" definition. The affected tile was
    # bound to averageSessionDuration (mean SESSION length, incl. idle time),
    # which read ~2x high (23m47s vs GA's 11m15s). Computing the ratio via the
    # same bucket_script path as the FB cost/CTR fields gives the exact GA figure
    # and needs no importer/schema change — both operands already ride on the
    # window doc. On the window index there is one row per property, so
    # sum(ued)/sum(au) == ued/au for one property, and a correctly impression-
    # weighted blend across properties (a duration ratio IS poolable, unlike the
    # deduplicated user count). No scale (seconds stay seconds; value_format
    # "duration" formats the display).
    "averageEngagementTime": {
        "numerator": "userEngagementDuration",
        "denominator": "activeUsers",
    },
}


def is_ratio_field(field):
    """Return True when `field` aggregates as a ratio per the RATIO_FIELDS registry."""
    return field in RATIO_FIELDS


# GA4 per-page ratio metrics. Unlike RATIO_FIELDS (FB ratios stored
# as separate numerator/denominator and recombined via sum(num)/sum(denom)),
# GA4 returns ``bounceRate`` / ``engagementRate`` as a finished 0..1 ratio PER
# ROW (per page), with no stored numerator/denominator to recombine. They must
# never be summed (summing per-page rates is meaningless); the correct cross-row
# aggregation is ``avg`` (a blended rate across pages). This is the same
# "ignore the user's stored aggregation" contract as RATIO_FIELDS /
# NON_ADDITIVE_FIELDS, but with ``avg`` and no bucket_script. Property-level
# ratios for KPI tiles come from the window index instead (one row per property,
# no aggregation needed).
GA4_AVG_RATIO_FIELDS = frozenset({"bounceRate", "engagementRate"})
GA4_AVG_RATIO_AGG = "avg"


def is_ga4_avg_ratio_field(field):
    """Return True when `field` is a GA4 per-row ratio that must aggregate as avg, not sum."""
    return field in GA4_AVG_RATIO_FIELDS


# non-additive FB metrics. These come from the windowed
# *_fb_insights_window index (one doc per ad set × publisher_platform for a
# pulled window), where FB has ALREADY deduplicated the value over the whole
# window. They must NOT be summed: reach is a unique-user count (summing
# platforms/ad-sets double-counts overlapping audiences); frequency is a ratio
# FB computed per slice. When the dashboard filters to a single platform — the
# normal Ad Sets table use — there is exactly one doc per ad-set bucket, so the
# aggregation degenerates to "read that one doc". We use ``max`` (not ``sum``)
# so the single-doc case is exact AND the all-platforms case returns the largest
# platform's reach rather than an inflated cross-platform sum. (A TRUE
# cross-platform/cross-ad-set dedup total is not derivable from stored per-slice
# numbers — FB only dedups server-side; see DECISIONS.md 2026-06-03.) The user's
# stored ``aggregation`` is intentionally ignored for these fields, exactly like
# the RATIO_FIELDS contract.
# The aggregation a non-additive field uses in place of the (wrong) sum.
NON_ADDITIVE_AGG = "max"


# central de-duplication registry. Each entry maps a
# non-additive ``(source, field)`` pair to the windowed index where the
# provider has ALREADY deduplicated the value over the pulled window, plus the
# aggregation to read it with and a ``status`` flag.
#
# This is the single source of truth that supersedes the old field-only
# ``NON_ADDITIVE_FIELDS`` frozenset. It removes the hardcoded
# ``"fb_insights_window"`` string the plan flagged as load-bearing and
# lets the frontend ask the backend "does this dashboard have dedup charts?"
# (via the dedup-info endpoint) instead of string-matching index names in JS.
#
#   ``index_suffix`` — the source segment of a data-view name
#     (``{team_esid}__<index_suffix>``); how a chart's data_view is matched.
#   ``agg``    — the aggregation used in place of the (wrong) ``sum``.
#   ``status`` — ``live``    : window importer ships, Refresh button enabled.
#                ``planned`` : no window index yet; reads → ``not_built``
#                              empty state, Refresh button hidden.
#
# FB read behavior is byte-for-byte identical to the pre-registry frozenset:
# all five FB fields map to the one ``fb_insights_window`` index with ``max``.
class WindowMeta:
    """Window-index metadata for a non-additive ``(source, field)`` pair."""

    __slots__ = ("index_suffix", "agg", "status")

    def __init__(self, index_suffix, agg=NON_ADDITIVE_AGG, status="planned"):
        self.index_suffix = index_suffix
        self.agg = agg
        self.status = status


DEDUP_WINDOW_REGISTRY = {
    # FB ad insights — the proven, live case.
    ("fb_insights", "insight_reach"): WindowMeta("fb_insights_window", "max", "live"),
    ("fb_insights", "insight_frequency"): WindowMeta("fb_insights_window", "max", "live"),
    ("fb_insights", "insight_full_view_reach"): WindowMeta("fb_insights_window", "max", "live"),
    ("fb_insights", "insight_estimated_ad_recallers"): WindowMeta("fb_insights_window", "max", "live"),
    ("fb_insights", "insight_estimated_ad_recall_rate"): WindowMeta("fb_insights_window", "max", "live"),
    # messaging conversations started is NON-ADDITIVE ACROSS DAYS in
    # Meta's own reporting (verified via direct Graph API 2026-08-22: a range
    # query reproduces the Ads Manager export exactly — 2,078/2,013/2,084 per
    # ad set — while summing Meta's own per-day rows reads 4–25% LOW, worst on
    # ended ad sets, so not mere attribution maturation). Unlike reach it IS
    # additive ACROSS AD SETS within one window (Ads Manager's own totals row
    # sums it: 642+680+686=2,008), so the window-side agg is ``sum``, not
    # ``max`` — the per-field agg the read path resolves via
    # ``non_additive_window_agg``. Window docs carry the field.
    # Other messaging_* metrics stay unregistered until each is empirically
    # audited against a range query (this ticket's own instruction: do NOT
    # assume non-additivity per metric).
    ("fb_insights", "insight_actions_onsite_conversion_messaging_conversation_started_7d"): WindowMeta(
        "fb_insights_window", "sum", "live"
    ),
    # (2026-08-26) — flipped live: the IG window importer ships
    # (get_instagram_insights --window_since/--window_until → per-profile
    # range-deduplicated reach via metric_type=total_value, empirically
    # verified: 30d window 6,354 vs 9,129 daily-sum). IG's API caps a window
    # at 30 days, so long custom ranges get the explained blank.
    ("ig_insights", "reach"): WindowMeta("ig_insights_window", "max", "live"),
    # GA4 window importer (get_ga_metrics_window.py) + schema +
    # daily/on-demand wiring all ship; flipped live. Single-grain (one property
    # selected) → real deduplicated window value; multi-grain (2+
    # properties / no selection) → the labeled "approx upper bound" daily sum.
    # Window data is pull-on-demand (Refresh / scheduled range import), so a
    # property with no pulled window shows the no_window explained-empty + Refresh.
    ("ga_metrics", "totalUsers"): WindowMeta("ga_metrics_window", "max", "live"),
    ("ga_metrics", "activeUsers"): WindowMeta("ga_metrics_window", "max", "live"),
    # averageEngagementTime is a RATIO (RATIO_FIELDS:
    # userEngagementDuration/activeUsers), not a stored count, so its build_query
    # path emits sum(num)/sum(denom) — which BLENDS across every overlapping
    # window doc when more than one is in scope (prod: month window + 9 rolling
    # 7-day windows summed → 9.78m vs the exact-window 11.10m). Registering it
    # routes it through the window snap (run_routed_query) so the read is pinned
    # to ONE window doc, where sum/sum == the doc's own ratio. The ``agg`` here is
    # moot — the RATIO_FIELDS bucket_script wins in build_query regardless — but
    # ``max`` keeps the WindowMeta shape uniform.
    ("ga_metrics", "averageEngagementTime"): WindowMeta("ga_metrics_window", "max", "live"),
    # GA4's per-event ratio (eventCount / activeUsers), written ONLY on
    # the grain="event" window docs (no daily counterpart). Registering it makes
    # the builder surface it on the daily view and routes its read to the window
    # index with ``max`` over the one snapped, grain-filtered doc per event.
    ("ga_metrics", "eventCountPerUser"): WindowMeta("ga_metrics_window", "max", "live"),
}


# The set of field names that are non-additive for ANY source. Derived from the
# registry so the field-only ``is_non_additive_field`` check stays a cheap
# lookup and keeps the exact contract (the read path forces the
# field's window agg on these fields only on a window data view — see
# ``_metric_agg`` / ``non_additive_window_agg``).
NON_ADDITIVE_FIELDS = frozenset(field for (_source, field) in DEDUP_WINDOW_REGISTRY)


def non_additive_window_agg(field):
    """the aggregation a non-additive field uses on a WINDOW view.

    Reach-family fields are non-additive in every direction (FB dedups them
    per slice; ``max`` reads the single-doc case exactly and never inflates).
    Messaging conversations are non-additive only ACROSS DAYS — within one
    window they sum across ad sets, exactly like Ads Manager's totals row —
    so their registry entries carry ``agg="sum"``. Fields absent from the
    registry fall back to the ``max`` (defensive; callers gate on
    ``is_non_additive_field`` first)."""
    for (_source, f), meta in DEDUP_WINDOW_REGISTRY.items():
        if f == field:
            return meta.agg
    return NON_ADDITIVE_AGG


def is_window_summable_field(field):
    """True when a registered non-additive field's window agg is ``sum`` —
    i.e. a cross-row total IS derivable on the window grain (
    messaging), unlike the reach family whose totals cell stays blank."""
    return is_non_additive_field(field) and non_additive_window_agg(field) == "sum"


# All window index suffixes the registry knows about. ``_is_window_data_view``
# matches a data-view name against these instead of a single hardcoded string.
_WINDOW_INDEX_SUFFIXES = frozenset(meta.index_suffix for meta in DEDUP_WINDOW_REGISTRY.values())

# Reverse map: a window index suffix → its base source key. The registry is
# keyed by the BASE (daily) source (e.g. ``fb_insights``) but a dedup chart's
# data view points at the WINDOW index (``fb_insights_window``). This lets a
# window data-view name resolve back to the registry's source key.
_WINDOW_SUFFIX_TO_SOURCE = {meta.index_suffix: source for (source, _field), meta in DEDUP_WINDOW_REGISTRY.items()}


# the GRAIN field per source: the dimension at which the source's
# window/dedup docs are deduplicated in one call. A deduplicated figure is only
# valid WITHIN one grain value; a chart whose scope spans 2+ grain values (or
# groups by a dimension finer than the grain) cannot show a real dedup number
# and falls back to the labeled "approx upper bound" daily sum (multi_grain).
# fb_insights — ad-set grain.
# ga_metrics — property grain (GA4 dedups unique users per property,
#                 zero-dimension; the window doc has NO sub-dimension breakdown).
_DEDUP_GRAIN_FIELD = {
    "fb_insights": "adset_id",
    "ig_insights": "ig_account_id",
    "ga_metrics": "property_id",
}

# fields that are 1:1 NAME equivalents of a grain id. Grouping a table
# by the grain's human-readable name (adset_name) is the SAME grain as its id
# (adset_id) — there is exactly one name per id — so such a table is single-grain
# and IS deduplicable. The sub_dimension check (group_by != grain) would
# otherwise misread name!=id as a finer breakdown and render the inflated daily
# sum with a GA4-shaped "can't be deduplicated" label on a perfectly dedup-able FB
# Ad Sets table (the user's dedup Refresh then never reaches the table). GA4's
# property has no name-grain table grouping, so it is unaffected.
_DEDUP_GRAIN_ALIASES = {
    "fb_insights": {"adset_name"},
    "ig_insights": {"ig_account_name"},
    "ga_metrics": set(),
}


# a source can have MORE THAN ONE independent dedup grain, each with
# its own doc set in the SAME window index, discriminated by the ``grain`` field.
# fb_insights now has two: the ad-set grain (, ``grain="adset"``, one
# doc per ad set × platform) AND the team grain (, ``grain="team"``, one
# cross-ad-set deduped doc per team). A table's group-by field decides which grain
# it reads: grouping by adset_id/adset_name → the ad-set docs; grouping by
# team_esid/team_title → the team doc. The router injects a ``grain=<value>``
# filter into the dedup query so a team table reads the ONE team doc (its exact
# cross-ad-set deduped reach) instead of SUMMING the ad-set docs (the
# upper-bound bug this ticket kills). Map keyed by the (source, group_by_field);
# a group-by not in the map keeps the legacy single-grain behavior.
#
# The default when a group-by maps to no grain here BUT the source has a defined
# grain is handled by the equivalents check (adset_id path). This map is ONLY the
# multi-grain discriminator: which physical ``grain`` value a group-by field reads.
_GROUP_BY_GRAIN_VALUE = {
    ("fb_insights", "adset_id"): "adset",
    ("fb_insights", "adset_name"): "adset",
    ("fb_insights", "team_esid"): "team",
    ("fb_insights", "team_title"): "team",
    # the page grain: one grain="page" doc per (team, page) with
    # Meta's exact deduped reach for that page's ads. A page_id/page_name
    # group-by (or a page sub-row under a team group-by) reads these docs.
    ("fb_insights", "page_id"): "page",
    ("fb_insights", "page_name"): "page",
    # the demographics grain: one grain="demographics" doc per
    # (team, age, gender) cell with Meta's deduped reach for that cell (plus
    # "all"-sentinel marginals). An age/gender group-by or sub-row reads these.
    ("fb_insights", "age"): "demographics",
    ("fb_insights", "gender"): "demographics",
    # the ad grain: one grain="ad" doc per (ad, platform) with Meta's
    # deduped per-ad reach for the window. An ad_id/ad_name group-by (the Ad
    # Report) or an ad sub-row under an ad-set group-by reads these. The docs
    # also carry adset_id/adset_name, so the (ad set, ad) composite matches.
    ("fb_insights", "ad_id"): "ad",
    ("fb_insights", "ad_name"): "ad",
    # the asset grain: one grain="asset" doc per (ad set, video
    # asset) plus per-asset "all"-adset marginals. WINDOW-ONLY (no daily doc
    # carries asset identity) — served by the window-only table path.
    ("fb_insights", "video_asset_id"): "asset",
    ("fb_insights", "video_asset_name"): "asset",
    # the GA4 EVENT grain: one grain="event" doc per (property,
    # eventName, window) with GA4's deduplicated per-event totalUsers /
    # activeUsers (a teammate's Firebase events table). An eventName group-by reads
    # these; every other GA4 read excludes them (see _EXCLUDED_GRAINS_BY_DEFAULT).
    # NOT window-only: eventName also exists on the daily ``events`` report, so
    # the additive eventCount column stitches from the daily index as usual.
    ("ga_metrics", "eventName"): "event",
}

# grains a source's DEFAULT (non-grain-grouped) window read must exclude.
# GA4's legacy property docs are unstamped (no ``grain`` field), so the router
# cannot require ``grain=property`` without dropping them; instead it injects
# ``grain != <each excluded grain>``. Without this a property-level read would
# also match the event docs: ``max(totalUsers)`` happens to survive (the property
# figure is a superset), but the ratio read sum(userEngagementDuration)
# / sum(activeUsers) would inflate its denominator with every event doc's
# activeUsers and silently shrink Average engagement time.
_EXCLUDED_GRAINS_BY_DEFAULT = {
    # every stamped grain that coexists with the (default) ad-set
    # grain in one fb_insights_window index (the
    # doc families). The ad-set path stays grain-UNFILTERED for legacy unstamped
    # docs, so these are excluded explicitly (list moved here from dedup_routing).
    "fb_insights": ("team", "page", "demographics", "ad", "asset"),
    "ga_metrics": ("event",),
}

# grains whose docs are deduplicated WITHIN one value of the source's
# base grain field (the GA4 event doc is per property). A table grouped by such
# a grain is exact only when the scope holds ONE base-grain value; across 2+
# properties ``max`` would pick one property's figure and present it as the
# total. The multi-grain classifier therefore still runs its base-grain
# cardinality check for these (the labeled "select one property" upper bound),
# instead of the "grouped by the grain → single-grain per row" shortcut
# that is right for FB's team/page/adset grains (one doc per row, cross-account
# handled by their own accounts counters).
_SCOPE_DEPENDENT_GRAINS = frozenset({("ga_metrics", "event")})


def excluded_grains_by_default(source):
    """grain values a non-grain-grouped read of ``source`` must exclude
    (``()`` for sources with no exclusive grain)."""
    return _EXCLUDED_GRAINS_BY_DEFAULT.get(source, ())


def grain_is_scope_dependent(source, grain):
    """True when a table grouped by ``grain`` is only exact within one
    base-grain value (GA4 event docs are per property)."""
    return (source, grain) in _SCOPE_DEPENDENT_GRAINS


# grains whose group-by dimensions exist ONLY on the window index
# (no daily-index counterpart at all). The team/page grains group by fields the
# daily docs also carry (team_title, page_id), so their tables split additive
# metrics to the daily index and stitch. A WINDOW-ONLY grain has no daily side:
# its table routes WHOLLY to the window index (grain-filtered, max-aggregated —
# the "all" marginal doc IS each bucket's exact total), and its dimension
# fields surface on the canonical data view registry-style (the
# dedup_fields_for_data_view precedent), not by mapping them onto daily docs
# that would never carry a value.
_WINDOW_ONLY_GRAINS = frozenset({"demographics", "asset"})


def window_only_grain_value(source, group_by_field):
    """The window-only grain a group-by field reads for this source, or ``None``
    when the field maps to no grain / a grain that also has a daily side."""
    grain = group_by_grain_value(source, group_by_field)
    return grain if grain in _WINDOW_ONLY_GRAINS else None


def window_only_grain_fields(source):
    """The group-by dimension fields of this source's window-only grains
    (fb_insights → ["age", "gender"]). These exist on NO daily doc, so the
    builder field picker / AI catalog must surface them explicitly."""
    return sorted(gb for (src, gb), grain in _GROUP_BY_GRAIN_VALUE.items() if src == source and grain in _WINDOW_ONLY_GRAINS)


# cross-data-view sub-row equivalence for multi-DV tables. When the
# table's sub-row (x_field_2) belongs to the default DV, a column bound to a
# source listed here groups by ITS OWN field instead of dropping the sub-row
# (the row-grain fallback), so its metrics split per sub-row: an Echo
# conversation doc carries the matched Page's id as fb_page_id — the same Meta
# page-id value space as fb_insights page_id — so a Team x Page table splits
# conversation counts per page. Keyed (default source, sub-row field) ->
# {other source: its field}.
_CROSS_DV_SUBROW_EQUIVALENTS = {
    ("fb_insights", "page_id"): {"echo_conversations": "fb_page_id"},
    # a page_NAME sub-row also splits echo metrics: echo docs carry
    # only ``fb_page_id`` (an id, not a name), so the echo group still groups by
    # ``fb_page_id`` and its bucket keys are TRANSLATED id→name at merge time
    # (see cross_dv_subrow_needs_translation / the routes id→name map). The
    # equivalent field is the same ``fb_page_id``; only the value space differs.
    ("fb_insights", "page_name"): {"echo_conversations": "fb_page_id"},
}

# the subset of the registry whose default-DV sub-row is a page NAME
# but whose equivalent field on the other DV is an id-valued field. For these,
# the other DV's composite bucket keys are page ids and must be translated to
# names before they can merge onto the default DV's name-keyed sub-rows.
_CROSS_DV_SUBROW_ID_TO_NAME = {
    ("fb_insights", "page_name"): {"echo_conversations": ("fb_page_id", "page_id", "page_name")},
}


def cross_dv_subrow_field(default_view, sub_field, other_view):
    """the OTHER data view's equivalent of the default DV's sub-row
    field, or ``None`` when that DV cannot split by this sub-row (the
    row-grain fallback applies). Resolves both views to registry BASE sources
    (window suffixes fold back) and strips a ``.keyword`` suffix first.
    """
    base = sub_field[: -len(".keyword")] if sub_field and sub_field.endswith(".keyword") else sub_field
    if not base:
        return None

    def _base_source(view):
        suffix = source_from_data_view(view)
        return _WINDOW_SUFFIX_TO_SOURCE.get(suffix, suffix)

    per_source = _CROSS_DV_SUBROW_EQUIVALENTS.get((_base_source(default_view), base))
    return (per_source or {}).get(_base_source(other_view))


def cross_dv_subrow_id_to_name(default_view, sub_field, other_view):
    """when the OTHER DV splits by an id-valued field but the default
    DV's sub-row is a NAME, return the ``(other_id_field, map_id_field,
    map_name_field)`` needed to translate the other DV's composite bucket keys
    (page ids) to the default sub-row's names before merging. ``None`` when no
    translation is needed (same value space, e.g. a page_id sub-row).

    ``map_id_field``/``map_name_field`` are the fields on the DEFAULT DV that
    carry the id and its name together (fb_insights docs hold both ``page_id``
    and ``page_name``), so the id→name map is built from the query-scoped fb
    data itself — its names ARE the labels the fb sub-row shows, so echo rows
    can't drift onto a phantom label. The DEAM inventory is the fallback.
    """
    base = sub_field[: -len(".keyword")] if sub_field and sub_field.endswith(".keyword") else sub_field
    if not base:
        return None

    def _base_source(view):
        suffix = source_from_data_view(view)
        return _WINDOW_SUFFIX_TO_SOURCE.get(suffix, suffix)

    per_source = _CROSS_DV_SUBROW_ID_TO_NAME.get((_base_source(default_view), base))
    return (per_source or {}).get(_base_source(other_view))


def group_by_grain_value(source, group_by_field):
    """the physical ``grain`` value a table's group-by field reads for a
    source, or ``None`` when the field maps to no defined grain (legacy behavior).

    ``team_esid``/``team_title`` → ``"team"`` (the one cross-ad-set deduped doc);
    ``adset_id``/``adset_name`` → ``"adset"`` (the per-ad-set docs). The router adds
    ``grain=<value>`` to the dedup query so the two grains never mix. Strips a
    trailing ``.keyword`` so a persisted group-by compares equal to the base name."""
    if not group_by_field:
        return None
    base = group_by_field[:-8] if group_by_field.endswith(".keyword") else group_by_field
    return _GROUP_BY_GRAIN_VALUE.get((source, base))


# sources whose window/dedup doc is a ZERO-DIMENSION property-level
# scalar: one row per grain value, the metric already deduplicated over the whole
# window with no sub-breakdown. A metric/scorecard TILE on such a source reads the
# exact figure straight from the snapped window doc, so a raw-view scalar tile can
# be routed to the window index.
#
# NOT listed: fb_insights / ig_insights. Their window doc is keyed at ad-set ×
# platform, so a bare scalar (no ad-set selected) has no single window row to read
# — the Summary Report FB reach tile deliberately stays the daily-sum top-line
# approximation; precision is a drill-in TABLE concern there. Routing a
# raw-view FB scalar would pick an arbitrary ad-set's reach. Keep them off this
# list so the scalar snap is GA-only until an FB scalar grain is defined.
_SCALAR_WINDOW_SOURCES = frozenset({"ga_metrics"})


def source_reads_scalar_from_window(source):
    """True when a metric/scorecard tile on this source's RAW view can
    be routed to the window index and read the exact per-grain figure from one
    snapped doc (property-level zero-dimension window). False for ad-set-grain
    sources (FB/IG), where a bare scalar has no single window row to read."""
    return source in _SCALAR_WINDOW_SOURCES


def dedup_grain_field(source):
    """Return the grain field for a registry base source, or ``None``."""
    return _DEDUP_GRAIN_FIELD.get(source)


def dedup_grain_equivalents(source):
    """the set of fields that count AS a grain for source: the grain id
    plus its 1:1 name alias(es), AND any OTHER independent grain the
    source deduplicates at (fb_insights: the team grain, team_esid/team_title).
    Grouping a table by any of these is single-grain (deduplicable via that grain's
    own doc), NOT a finer sub-dimension. Empty if no grain.

    without the team fields here, a team-grouped table would trip the
     sub_dimension branch (group_by != adset_id) and render the inflated
    daily sum labeled "can't be deduplicated" — never reaching the grain="team"
    doc. Including them makes the team table single-grain; the router then injects
    grain=team (group_by_grain_value) so it reads that doc, not the ad-set sum."""
    grain = _DEDUP_GRAIN_FIELD.get(source)
    if not grain:
        return set()
    extra_grains = {gb for (src, gb) in _GROUP_BY_GRAIN_VALUE if src == source}
    return {grain} | _DEDUP_GRAIN_ALIASES.get(source, set()) | extra_grains


# the DIMENSION fields a source's window/dedup doc actually carries,
# so a chart filter on one of them survives when a chart is routed to the window
# index. A filter on any field NOT listed here is a raw-daily-index dimension the
# window doc does not have (the importer never wrote it) — applying it to the
# window query silently matches ZERO docs and blanks the dedup value. The
# canonical break: a GA4 chart carrying ``report_type=events`` (required for the
# additive columns on the raw ``*_ga_metrics`` index) also rode onto the window
# query, where report_type is unwritten -> 0 hits -> blank Active Users.
#
# Driven by what each window importer WRITES, not a report_type-specific hack:
#   ga_metrics — get_ga_metrics_window: property-grain, ZERO sub-dimension. The
#     window doc has only property_id (+ team/window scoping the router applies
# separately). No report_type, platform, city, etc. added the
#     grain="event" docs, which carry eventName (and every doc now carries grain).
#   fb_insights — get_insights_window: keyed at ad-set x publisher_platform, and
#     also carries campaign/account/effective_status. A chart filter on any of
#     these is legitimate on the window doc and MUST survive (the Ad Sets table
# filters by platform/account).
#   ig_insights — grain only (parity with GA's zero-dimension shape until its
#     window importer ships richer dimensions).
#
# ``None`` for a source means "unknown — do not strip" (legacy fail-open: never
# silently drop a filter for a source we haven't characterized).
_WINDOW_CARRIED_FIELDS = {
    # plus the grain discriminator and the event grain's own dimension,
    # so the injected grain filter and an eventName chart filter both survive.
    "ga_metrics": {"property_id", "grain", "eventName"},
    "fb_insights": {
        "adset_id",
        "adset_name",
        "ad_campaign_id",
        "ad_campaign_name",
        "ad_account_id",
        "publisher_platform",
        "effective_status",
        # the grain discriminator + the team-grain group-by fields. The
        # router injects a grain=<value> filter that MUST survive routing; a team
        # table groups by team_esid/team_title, present on the team-grain doc.
        "grain",
        "team_esid",
        "team_title",
        # the page-grain doc's own dimensions (page_name only when the
        # page is inventoried; wizard-cohort pages carry page_id alone).
        "page_id",
        "page_name",
        # the demographics-grain doc's own dimensions.
        "age",
        "gender",
        # the asset-grain doc's own dimensions.
        "video_asset_id",
        "video_asset_name",
    },
    "ig_insights": {"ig_account_id", "ig_account_name"},
}


def window_carried_fields(source):
    """the dimension fields a source's window doc carries, or ``None``
    when the source is uncharacterized (caller must NOT strip — fail open).

    A chart filter whose field is not in this set (and not a grain equivalent) is a
    raw-index-only dimension absent from the window doc; routing it to the window
    query would zero the dedup value, so the router drops it from the dedup_cfg
    while keeping it on the daily_cfg."""
    fields = _WINDOW_CARRIED_FIELDS.get(source)
    if fields is None:
        return None
    return set(fields) | dedup_grain_equivalents(source)


def is_non_additive_field(field):
    """Return True when `field` must NOT be summed."""
    return field in NON_ADDITIVE_FIELDS


def source_from_data_view(data_view):
    """Return the source/index-suffix segment of a data-view name.

    Two stored shapes both reduce to the same ``index_suffix`` (the registry key
    segment, e.g. ``fb_insights_window``):

      * WILDCARD — ``*_<source>`` (e.g. ``*_fb_insights_window``). This is what
        the builder + the repoint tool actually store in echarts_config;
        the team prefix is resolved to a concrete index at QUERY time
        (``scoped_index_pattern``). Mirrors builder.js's ``s.replace(/^\\*_?/, '')``
        normalizer.
      * TEAM-PREFIXED — ``<team_esid>__<source>`` (e.g.
        ``abc123__fb_insights_window``). Built at runtime by the routing helpers.

    the matcher previously only handled the ``__`` form, so a config's
    wildcard ``*_fb_insights_window`` returned the whole string (no ``__`` to
    split) and never resolved to a registry source — dedup-info reported
    ``charts: []`` / ``has_dedup: false`` and the Refresh button stayed hidden.
    Strip a leading wildcard first, then split on ``__``. Bare suffixes (no
    prefix at all) pass through unchanged.
    """
    dv = data_view or ""
    # Strip a leading "*_" / "*" wildcard prefix (builder/repoint storage form).
    if dv.startswith("*_"):
        dv = dv[2:]
    elif dv.startswith("*"):
        dv = dv[1:]
    return dv.rsplit("__", 1)[-1]


def dedup_meta_for(source, field):
    """Return the ``WindowMeta`` for a ``(source, field)`` pair, or ``None``.

    ``source`` is a registry source key (e.g. ``fb_insights``) — pass
    ``source_from_data_view(data_view)`` to resolve it from a chart's data view.
    """
    return DEDUP_WINDOW_REGISTRY.get((source, field))


def _chart_field_dataviews(chart):
    """Yield ``(field, data_view)`` pairs referenced by a chart config.

    Walks every place a chart pairs a metric field with a data view — the SAME
    set of locations the repoint tool (``repoint_meta_ad_sets_to_window``)
    and the viewer's own ``_firstWindowDataViewFromConfig`` walk:

      * chart-level ``y_field`` / ``y_fields`` (with the chart's ``data_view``);
      * table ``columns[]`` / ``table_columns[]`` — each column may carry its
        OWN ``data_view`` (the Ad Sets table keys each metric column to its own
        data view; the chart may carry none), falling back to the chart's;
      * ``table_columns[].data_sources[]`` — grouped / multi-source columns nest
        the (field, data_view) pair here. THIS is where the Ad Sets reach column
        lives after the repoint, so missing it (the original bug)
        made the dedup-info endpoint report has_dedup=false for Ad Sets.

    Calculation/formula columns are skipped — they reference component fields,
    not a single stored metric, and a dedup field is never a calc component.
    """
    if not isinstance(chart, dict):
        return
    chart_dv = chart.get("data_view")

    y_fields = chart.get("y_fields") or []
    if not y_fields and chart.get("y_field"):
        y_fields = [chart["y_field"]]
    for yf in y_fields:
        if yf:
            yield yf, chart_dv

    for col in chart.get("columns") or chart.get("table_columns") or []:
        if not isinstance(col, dict):
            continue
        if col.get("aggregation") == "calculation":
            continue
        col_dv = col.get("data_view") or chart_dv
        field = col.get("field")
        if field:
            yield field, col_dv
        # Grouped / multi-source columns nest (field, data_view) per source —
        # each source carries its own data_view (falling back to the column's).
        for ds in col.get("data_sources") or []:
            if not isinstance(ds, dict):
                continue
            ds_field = ds.get("field") or ds.get("y_field")
            if ds_field:
                yield ds_field, (ds.get("data_view") or col_dv)


def _dedup_routing_enabled():
    """Local copy of the ``DEDUP_ROUTING_ENABLED`` flag read.

    Mirrors ``dedup_routing.dedup_routing_enabled`` byte-for-byte. Kept local
    (not imported) because ``dedup_routing`` imports FROM this module — importing
    it back here would be circular. The flag is a trivial env read, so the small
    duplication is cheaper than restructuring the import graph.
    """
    return os.environ.get("DEDUP_ROUTING_ENABLED", "").strip().lower() in ("1", "true", "yes", "on")


def chart_dedup_matches(chart):
    """Return the list of dedup ``(source, field, status)`` hits for one chart.

    A hit means the chart reads a registered non-additive ``(source, field)``
    pair that the system will route to a window index. Two shapes count:

      * WINDOW-VIEW (always): the field sits on the field's window data view
        (``*_fb_insights_window``) — the chart's ``data_view`` resolves to the
        registry's ``index_suffix``. This is the Ad Sets shape.
      * CANONICAL-VIEW (only when ``DEDUP_ROUTING_ENABLED`` is on): the
        field sits on the plain daily/base view (``*_fb_insights``) and is a
        registered dedup field. The routed read path (``run_routed_query``)
        recognizes dedup fields on EITHER view and fetches them from the window
        index, so the gate must agree — otherwise a chart migrated to the single
        canonical view would route correctly but lose its Refresh button. With
        the flag OFF this path is NOT a hit: the field sums daily exactly as
        before (prod stays unchanged until the flag flips).

    A non-additive field on a non-window view with the flag OFF is the ordinary
    per-day path (the case), not a dedup chart.

    Returns a list of dicts ``{source, field, status}`` (deduplicated, order
    preserved). Empty list when the chart has no dedup field.
    """
    routing_on = _dedup_routing_enabled()
    seen = set()
    hits = []
    for field, data_view in _chart_field_dataviews(chart):
        suffix = source_from_data_view(data_view)
        # WINDOW-VIEW hit: the suffix is a window suffix → map back to its base
        # source. Always counts (the field is physically read from the window).
        source = _WINDOW_SUFFIX_TO_SOURCE.get(suffix)
        # CANONICAL-VIEW hit (flag-gated): the suffix is a base source (e.g.
        # ``fb_insights``) and the field is registered for it. Routing will fetch
        # it from the window index, so it's a dedup chart when routing is on.
        if source is None and routing_on and dedup_meta_for(suffix, field) is not None:
            source = suffix
        if source is None:
            continue
        meta = dedup_meta_for(source, field)
        if meta is None:
            continue
        key = (source, field)
        if key in seen:
            continue
        seen.add(key)
        hits.append({"source": source, "field": field, "status": meta.status})

    # a chart grouped / sub-rowed / split by a WINDOW-ONLY grain
    # dimension (demographics age/gender) reads WHOLLY from the window index
    # (the routed path) even when none of its metrics is a
    # registered dedup field (e.g. impressions by age). It is therefore a
    # dedup chart for Refresh purposes: a missing/stale window blanks it, and
    # the inline window pull is exactly the remedy. Same flag gate as the
    # canonical-view metric hit: on a base (daily) view this only routes when
    # DEDUP_ROUTING_ENABLED; a chart already on the window view always counts.
    if isinstance(chart, dict):
        chart_dv = chart.get("data_view")
        suffix = source_from_data_view(chart_dv)
        base = _WINDOW_SUFFIX_TO_SOURCE.get(suffix)
        on_window = base is not None
        if base is None:
            base = suffix
        if on_window or routing_on:
            source_status = (
                "live"
                if any(src == base and m.status == "live" for (src, _f), m in DEDUP_WINDOW_REGISTRY.items())
                else "planned"
            )
            for dim_raw in (chart.get("x_field"), chart.get("x_field_2"), chart.get("breakdown"), chart.get("split_field")):
                dim = dim_raw[:-8] if isinstance(dim_raw, str) and dim_raw.endswith(".keyword") else dim_raw
                if not dim or not window_only_grain_value(base, dim):
                    continue
                key = (base, dim)
                if key in seen:
                    continue
                seen.add(key)
                hits.append({"source": base, "field": dim, "status": source_status})
    return hits


def _canvas_object_chart_configs(obj):
    """yield chart-config-shaped dicts for one canvas object's data
    reads, so :func:`chart_dedup_matches` can inspect them like builder charts.

    Mirrors the read paths canvas_view.js / canvas_charts.js actually query:
      * ``data_field`` — ``binding`` (simple), or ``numerator``/``denominator``
        when computed;
      * ``calc_field`` — one binding per ``calc.variables`` entry;
      * ``table`` — the payload IS a legacy table chart config; yielded as-is;
      * ``chart`` — the ``geo`` binding plus every ``series[]`` entry (and each
        entry's ``sources[]``), each stamped with the chart's ``x_field`` /
        ``split_field`` so window-only grain dimensions are detected too.
    """
    if not isinstance(obj, dict):
        return
    otype = obj.get("object_type")

    def _binding_cfg(binding):
        if isinstance(binding, dict) and binding.get("data_view") and binding.get("field"):
            return {"data_view": binding["data_view"], "y_field": binding["field"]}
        return None

    if otype == "data_field":
        binding = obj.get("binding")
        if isinstance(binding, dict):
            if binding.get("computed"):
                for part in (binding.get("numerator"), binding.get("denominator")):
                    cfg = _binding_cfg(part)
                    if cfg:
                        yield cfg
            else:
                cfg = _binding_cfg(binding)
                if cfg:
                    yield cfg
    elif otype == "calc_field":
        calc = obj.get("calc")
        variables = calc.get("variables") if isinstance(calc, dict) else None
        if isinstance(variables, dict):
            for vbind in variables.values():
                cfg = _binding_cfg(vbind)
                if cfg:
                    yield cfg
    elif otype == "table":
        table_payload = obj.get("table")
        if isinstance(table_payload, dict):
            yield table_payload
    elif otype == "chart":
        chart_payload = obj.get("chart")
        if not isinstance(chart_payload, dict):
            return
        dims = {
            "x_field": chart_payload.get("x_field"),
            "split_field": chart_payload.get("split_field"),
        }
        geo_cfg = _binding_cfg(chart_payload.get("geo"))
        if geo_cfg:
            yield dict(geo_cfg, **dims)
        for s in chart_payload.get("series") or []:
            if not isinstance(s, dict):
                continue
            for src in s.get("sources") or [s]:
                cfg = _binding_cfg(src)
                if cfg:
                    yield dict(cfg, **dims)


def dashboard_dedup_matches(source_doc):
    """``(chart_id, match)`` dedup hits for a whole dashboard doc.

    Builder (echarts) dashboards walk ``echarts_config.charts`` exactly as the
    dedup-info endpoint always did; canvas dashboards (which store data reads in
    ``canvas_config.objects``, not charts) walk each object's bindings so the
    Refresh button covers them too — before this, dedup-info saw no charts on a
    canvas page and ``has_dedup`` was unconditionally false.
    """
    source_doc = source_doc or {}
    out = []
    if source_doc.get("dashboard_type") == "canvas":
        for obj in (source_doc.get("canvas_config") or {}).get("objects") or []:
            if not isinstance(obj, dict):
                continue
            oid = obj.get("object_id") or ""
            for cfg in _canvas_object_chart_configs(obj):
                for match in chart_dedup_matches(cfg):
                    out.append((oid, match))
        return out
    charts = (source_doc.get("echarts_config") or {}).get("charts") or []
    if isinstance(charts, dict):
        charts = list(charts.values())
    for chart in charts:
        if not isinstance(chart, dict):
            continue
        chart_id = chart.get("chart_id", "")
        for match in chart_dedup_matches(chart):
            out.append((chart_id, match))
    return out


# plain-language labels for the inflated-reach warning. Keyed by the
# FB dedup fields whose daily SUM overstates true reach (people reached on
# multiple days are counted once per day). The label is woven into the tooltip
# copy the viewer shows on the warning triangle. Only the FB ``live`` reach
# family is listed: these are the fields a per-day sum demonstrably inflates and
# for which a deduplicated window exists via the dedup-reach Refresh flow.
_INFLATED_REACH_FIELD_LABELS = {
    "insight_reach": "reach",
    "insight_full_view_reach": "full-view reach",
    "insight_estimated_ad_recallers": "estimated ad recallers",
}


def chart_inflated_reach_fields(chart):
    """Return the FB reach fields this chart shows as an un-deduplicated daily sum.

    a chart bound to a FB reach field that reads it from the daily
    ``*_fb_insights`` index sums it per day, which OVERSTATES true reach (a
    person reached across N days is counted N times). The deduplicated value
    lives on the window index (``*_fb_insights_window``) and is surfaced via the
    dedup-reach Refresh flow.

    A field is reported here (warn) when, for at least one (field, data_view)
    the chart references:

      * the field is in ``_INFLATED_REACH_FIELD_LABELS`` (a FB reach field), AND
      * the data view is NOT a window view (``_is_window_data_view`` is False) —
        i.e. it is the daily-sum path, never the already-deduplicated window.

    Conservative by design (the ticket's requirement): we warn wherever reach
    could be inflated, regardless of whether a deduplicated window happens to be
    available for the account. A window-view chart (the Ad Sets shape)
    reads the deduplicated value and is NOT warned.

    This is the CONFIG-level detector: "does the chart bind an inflatable reach
    field to a non-window view." Whether dedup ROUTING actually deduplicated that
    field at query time is data-dependent (a matching window doc must exist) and
    is NOT knowable here — that suppression happens in ``_attach_reach_warning``
    against the routed response, which only clears fields routing
    genuinely served deduplicated and KEEPS the rest (no_window / not_built /
    non-table → still a daily sum). Keeping this detector config-pure means the
    flag-off path and the routed-but-fell-back path both still warn correctly.

    Returns the list of distinct field names (order preserved); empty when the
    chart shows no inflated reach field.
    """
    if not isinstance(chart, dict):
        return []
    seen = set()
    out = []
    for field, data_view in _chart_field_dataviews(chart):
        if field not in _INFLATED_REACH_FIELD_LABELS:
            continue
        if _is_window_data_view(data_view):
            continue  # deduplicated window — already correct, no warning
        if field in seen:
            continue
        seen.add(field)
        out.append(field)
    return out


def inflated_reach_warning(chart):
    """Return the warning payload for a chart, or ``None``.

    Shape (consumed by charts.js to render the ⚠ triangle + hover tooltip)::

        {"fields": ["insight_reach", ...], "message": "<tooltip copy>"}

    ``None`` when the chart shows no inflated reach field, so callers can attach
    it unconditionally and the frontend simply sees nothing to render.
    """
    fields = chart_inflated_reach_fields(chart)
    if not fields:
        return None
    labels = [_INFLATED_REACH_FIELD_LABELS[f] for f in fields]
    if len(labels) == 1:
        subject = f"This {labels[0]} figure is a daily sum"
    else:
        subject = "These figures are daily sums"
    message = (
        f"{subject} and may overstate true reach — people reached on more than "
        "one day are counted each day. Deduplicated reach is available via the "
        "dedup-reach Refresh flow."
    )
    return {"fields": fields, "message": message}


def _is_window_data_view(data_view):
    """True for a windowed dedup data view.

    the force-to-``max`` rule for NON_ADDITIVE_FIELDS only applies to
    the windowed index, where the provider has already deduplicated
    reach/frequency over the pulled window and there is one doc per (ad set ×
    platform). On the per-day ``*fb_insights`` index those same fields are
    ordinary per-day, per-ad values whose total over a range IS a meaningful
    ``sum`` — forcing ``max`` there returned the single largest ad's reach
    (date-invariant), which is what froze the Summary Report reach tile.

    generalized from the single ``"fb_insights_window"`` literal to
    every window suffix in ``DEDUP_WINDOW_REGISTRY`` (the FB suffix is in that
    set, so FB behavior is unchanged).
    """
    dv = data_view or ""
    return any(suffix in dv for suffix in _WINDOW_INDEX_SUFFIXES)


def _ratio_script(scale=1):
    """Build the Painless script for a registered ratio's bucket_script.

    Always carries the ``params.denom > 0`` divide-by-zero guard.
    ``scale`` (default 1) is baked into the script as a literal so the
    bucket_script body is self-contained — no params plumbing needed.

    scale=100 is the FB-CTR convention (FB stores ratios already
    multiplied by 100, e.g. ``insight_ctr=2.159`` for 2.159%); scale=1
    is the cost_per_result convention (raw ratio).
    """
    if scale == 1:
        return "params.denom > 0 ? params.num / params.denom : null"
    return f"params.denom > 0 ? {scale} * params.num / params.denom : null"


def _ratio_metric_aggs(field, agg_key):
    """Return the three-agg dict for a registered ratio field.

    Emits sum(numerator), sum(denominator), and a bucket_script that
    divides them. The bucket_script lands under `agg_key` so the response
    transformer's existing `metric_<field>` lookup picks up the ratio.
    Divide-by-zero returns null (ES bucket_script behavior when the
    denominator parameter is 0 and the script guards it).

    ``scale`` on the registry entry multiplies the output so the
    script result matches the field's storage convention (CTR fields are
    stored as percentages, so the bucket_script must also emit a percentage
    to track per-platform docs and Data Studio).
    """
    spec = RATIO_FIELDS[field]
    num_key = f"{agg_key}_num"
    denom_key = f"{agg_key}_denom"
    scale = spec.get("scale", 1)
    script = _ratio_script(scale)
    return {
        num_key: {"sum": {"field": spec["numerator"]}},
        denom_key: {"sum": {"field": spec["denominator"]}},
        agg_key: {
            "bucket_script": {
                "buckets_path": {"num": num_key, "denom": denom_key},
                "script": script,
            }
        },
    }


def _table_totals_aggs(metric_aggs):
    """the whole-scope variants of a table's per-bucket metric aggs,
    for the grand-totals row.

    The totals row summed (or averaged) EVERY numeric column client-side
    over the rendered rows, which is meaningless for ratio and deduplicated
    metrics (frequency 1.93 + 2.07 + 2.36 is not a total of anything). The honest
    totals are the same aggregations run over the WHOLE filtered doc set — the
    ratio bucket_scripts then divide sum(numerator) by sum(denominator), exactly
    what Ads Manager's own totals row reports.

    Reuses the per-bucket ``metric_aggs`` verbatim, minus the columns that have
    no honest whole-scope total:
      - ``top_hits`` (text/date columns) — no numeric total exists;
      - non-additive window metrics (reach, frequency, ...) — FB deduplicates
        these per slice server-side; no cross-row total is derivable from the
        stored per-slice numbers (DECISIONS.md 2026-06-03), so their totals
        cell renders blank rather than a fabricated number.
    An excluded column's agg key is simply absent from the totals bucket; the
    transformer maps absence to a blank cell.
    """
    # Keys a ratio bucket_script divides — never excluded, even when the raw
    # operand field is itself non-additive (averageEngagementTime sums
    # activeUsers as its denominator; the RATIO is poolable even
    # though the bare user count is not). Dropping a referenced operand would
    # leave the bucket_script with a dangling buckets_path and fail the query.
    referenced = set()
    for agg in metric_aggs.values():
        if isinstance(agg, dict) and "bucket_script" in agg:
            referenced.update((agg["bucket_script"].get("buckets_path") or {}).values())
    totals = {}
    for key, agg in metric_aggs.items():
        if not isinstance(agg, dict) or "top_hits" in agg:
            continue
        if key not in referenced and "bucket_script" not in agg:
            inner = next(iter(agg.values()), None)
            field = inner.get("field") if isinstance(inner, dict) else None
            # window-summable fields (messaging conversations) keep
            # their totals cell: the cross-row sum IS the honest whole-scope
            # answer on the window grain (Ads Manager's totals row does the
            # same). Only max-agg window fields (reach family) stay blank.
            if field and is_non_additive_field(field) and not is_window_summable_field(field):
                continue
        totals[key] = agg
    return totals


def _metric_agg(y_field, aggregation, equals_value=None, date_range=None, field_type=None, data_view=None):
    """Return a single metric aggregation dict.

    If a numeric-only aggregation (sum/avg/min/max) is requested on a text or
    keyword field, falls back to value_count to avoid a 400 from ES.
    """
    # non-additive window metrics (reach, frequency,...) must never
    # be summed. Force ``max`` regardless of the stored aggregation BEFORE the
    # text/keyword fallback below (these are numeric fields, so max is valid).
    # Same "ignore the user's stored aggregation" contract as RATIO_FIELDS.
    #
    # but ONLY on the windowed *_fb_insights_window data view, the
    # context this rule was written for (FB-deduplicated, one doc per ad set ×
    # platform). On the per-day *fb_insights index these are ordinary per-day
    # values and the configured ``sum`` is correct; forcing ``max`` there
    # returned the single largest ad's reach (date-invariant) and froze the
    # Summary Report reach tile.
    if y_field and is_non_additive_field(y_field) and _is_window_data_view(data_view):
        # per-field window agg from the registry: ``max`` for the
        # reach family, ``sum`` for metrics that
        # are non-additive across days but additive across ad sets (messaging
        # conversations — Ads Manager's own totals row sums them).
        aggregation = non_additive_window_agg(y_field)
    # GA4 per-page ratios (bounceRate/engagementRate) must aggregate
    # as ``avg``, never ``sum``. Unlike the case this is NOT gated on a
    # window data view: these are daily per-page values on the `pages` report,
    # and summing them is always wrong (a page-1 bounce of 0.4 + page-2 of 0.6
    # is not a 1.0 bounce rate). avg across the page buckets is the blended rate.
    if y_field and is_ga4_avg_ratio_field(y_field):
        aggregation = GA4_AVG_RATIO_AGG
    if aggregation in _NUMERIC_ONLY_AGGS and field_type in ("text", "keyword", "date"):
        aggregation = "count"
    if aggregation in ("text", "date"):
        # Use top_hits to fetch the actual field value rather than aggregating numerically.
        # Strip .keyword suffix — sub-fields are not stored in _source; use the base field name.
        source_field = y_field[:-8] if y_field.endswith(".keyword") else y_field
        return {"top_hits": {"size": 1, "_source": {"includes": [source_field]}}}
    if aggregation == "equals" and equals_value is not None:
        # Use filter aggregation to count docs where field equals value
        return {
            "filter": {"term": {y_field: equals_value}},
        }
    if aggregation == "exists":
        # Count docs where the field has any value
        return {
            "filter": {"exists": {"field": y_field}},
        }
    if aggregation == "query date":
        if date_range:
            # Use filter aggregation to count docs where date field falls within range.
            # Prefer date_range['field'] over y_field so callers can specify .keyword or
            # epoch-safe subfields without renaming the y_field used elsewhere.
            date_field = date_range.get("field") or y_field
            range_clause = _date_range_clause(date_range.get("from"), date_range.get("to"))
            if range_clause:
                return {"filter": {"range": {date_field: range_clause}}}
        # If no date range specified, just check field exists
        return {"filter": {"exists": {"field": y_field}}}
    agg_type = METRIC_AGG_MAP.get(aggregation, "sum")
    if agg_type == "cardinality":
        # the unique-contacts metric (cardinality of client_id) must
        # match Echo's "Clients" KPI exactly. ES cardinality is approximate above
        # its precision_threshold (default 3,000, exact-ish to ~40,000); pin it to
        # the 40,000 ceiling so per-team/per-page contact counts stay exact at our
        # volumes. (A tenant exceeding 40k distinct contacts in one bucket would
        # still be within HLL's <1% error — acceptable and flagged if it ever hits.)
        return {"cardinality": {"field": y_field, "precision_threshold": 40000}}
    return {agg_type: {"field": y_field}}


def build_query(config, *, dashboard_filters=None):
    """
    Build an Elasticsearch query body from a chart config dict.

    config keys:
        data_view, chart_type, x_field, y_fields (list), aggregation,
        breakdown (optional), filters (list), date_range (optional)

    Legacy y_field (single string) is accepted and normalised to y_fields.

    ``dashboard_filters`` is an optional list of dashboard-level
    filter dicts that get merged in by ``_build_filters`` for charts whose
    ``data_view`` matches each filter's ``data_view``. The chart's own
    ``data_view`` is used as the active DV.
    """
    chart_type = config.get("chart_type", "bar")

    # full {field: es_type} map resolved from the live mapping (when
    # the caller supplies it). Lets _terms_field detect a ``.keyword`` suffix
    # pointed at a field that's already bare-``keyword`` (no sub-field) and fall
    # back to the existing field, instead of aggregating on nothing → 0 buckets.
    field_types = config.get("field_types") or {}
    x_field_raw = config.get("x_field")
    x_field_type = config.get("x_field_type", "")
    x_field = _terms_field(x_field_raw, x_field_type, field_types=field_types) if x_field_raw else x_field_raw

    # Normalise y_fields — accept y_fields list or legacy y_field string
    y_fields_raw = config.get("y_fields") or []
    if not y_fields_raw and config.get("y_field"):
        y_fields_raw = [config["y_field"]]
    y_fields = [yf for yf in y_fields_raw if yf]
    y_field = y_fields[0] if y_fields else ""  # primary field for breakdown / pie

    aggregation = config.get("aggregation", "sum")
    equals_value = config.get("equals_value")
    date_range = config.get("date_range")
    breakdown = config.get("breakdown")
    y_field_types = config.get("y_field_types") or {}
    active_dv = config.get("data_view")  # gates the window-only force-max

    filter_clauses = _build_filters(
        config,
        dashboard_filters=dashboard_filters,
        active_data_view=config.get("data_view"),
    )
    query_clause = {"bool": {"filter": filter_clauses}} if filter_clauses else {"match_all": {}}

    # ── Metric (KPI) ──────────────────────────────────────
    if chart_type == "metric":
        if breakdown:
            return {
                "size": 0,
                "query": query_clause,
                "aggs": {
                    "breakdown_buckets": {
                        "terms": {
                            "field": breakdown,
                            "size": 20,
                            "order": {"_count": "desc"},
                        },
                    }
                },
            }
        agg_key = f"metric_{_safe_agg_key(y_field)}" if y_field else "metric_value"
        # ratio fields on a KPI chart. ES requires bucket_script
        # to have a multi-bucket parent (terms, composite, filters, ...).
        # Verified empirically (Kibana Dev Tools, 2026-05-12) that a
        # single-bucket `filter` is rejected with "Expected a multi bucket
        # aggregation but got [InternalFilter]"; a `filters` agg (plural)
        # with one named bucket is the minimal accepted shape. The
        # response carries the sums + the divided value under
        # aggs["{agg_key}_scope"]["buckets"]["all"]; the transformer
        # unwraps that hop.
        if y_field and is_ratio_field(y_field):
            spec = RATIO_FIELDS[y_field]
            wrapper_key = f"{agg_key}_scope"
            num_key = f"{agg_key}_num"
            denom_key = f"{agg_key}_denom"
            script = _ratio_script(spec.get("scale", 1))
            return {
                "size": 0,
                "query": query_clause,
                "aggs": {
                    wrapper_key: {
                        "filters": {"filters": {"all": {"match_all": {}}}},
                        "aggs": {
                            num_key: {"sum": {"field": spec["numerator"]}},
                            denom_key: {"sum": {"field": spec["denominator"]}},
                            agg_key: {
                                "bucket_script": {
                                    "buckets_path": {"num": num_key, "denom": denom_key},
                                    "script": script,
                                }
                            },
                        },
                    },
                },
            }
        return {
            "size": 0,
            "query": query_clause,
            "aggs": (
                {
                    agg_key: _metric_agg(
                        y_field,
                        aggregation,
                        equals_value,
                        date_range,
                        y_field_types.get(y_field),
                        data_view=active_dv,
                    )
                }
                if y_field
                else {}
            ),
        }

    # ── Table ─────────────────────────────────────────────
    if chart_type == "table":
        sources = []
        if x_field:
            # Row date-format support. When the Row field is a
            # date (``x_field_type=date``), bucket via ``date_histogram``
            # so the row labels are calendar buckets rather than a raw
            # ``terms`` agg over every distinct timestamp value. Honors
            # the chart's ``date_format`` if set; falls back to
            # ``yyyy-MM-dd``. ``calendar_interval`` defaults to ``day``
            # — the natural granularity of the *_insights data views,
            # which carry one doc per (team, date).
            if x_field_type == "date":
                # Piece B — translate the Date Format dropdown's
                # human-friendly label (e.g. "DD Mon YYYY") to its Java
                # DateTimeFormatter equivalent ("dd MMM yyyy"). Pre-fix the
                # raw label was passed straight to ES, which either rejected
                # it or formatted with the wrong tokens (Java "DD" means
                # day-of-year, not day-of-month).
                date_fmt = _map_date_format_to_es(config.get("date_format"))
                sources.append(
                    {
                        x_field_raw: {
                            "date_histogram": {
                                "field": x_field_raw,
                                "calendar_interval": config.get("calendar_interval", "day"),
                                "format": date_fmt,
                            }
                        }
                    }
                )
            else:
                sources.append({x_field: {"terms": {"field": x_field}}})
        table_columns = config.get("table_columns") or []

        if not sources:
            # No x_field — auto-promote the first text column as the group-by source
            # so users get a list of distinct values without having to set an x_field.
            first_text_col = next(
                (col for col in table_columns if col.get("field") and col.get("aggregation") in ("text", "date")),
                None,
            )
            if first_text_col:
                # Piece A — mirror explicit-Row branch for
                # the auto-promote path. When the auto-promoted column has
                # ``aggregation=date``, bucket via ``date_histogram`` so the
                # row labels render as calendar dates instead of raw
                # epoch_millis. fixed the explicit-x_field path but
                # the auto-promotion path inherited the original terms agg.
                if first_text_col.get("aggregation") == "date":
                    date_fmt = _map_date_format_to_es(first_text_col.get("date_format"))
                    sources.append(
                        {
                            first_text_col["field"]: {
                                "date_histogram": {
                                    "field": first_text_col["field"],
                                    "calendar_interval": config.get("calendar_interval", "day"),
                                    "format": date_fmt,
                                }
                            }
                        }
                    )
                else:
                    auto_field = _terms_field(first_text_col["field"], "keyword", field_types=field_types)
                    sources.append({auto_field: {"terms": {"field": auto_field}}})
                # Remove it from metric_aggs — it's now the group-by key, not a sub-agg
                table_columns = [c for c in table_columns if c.get("field") != first_text_col["field"]]
            else:
                return {"size": 0, "query": {"match_none": {}}}

        x_field_2_raw = config.get("x_field_2")
        x_field_2 = _terms_field(x_field_2_raw, field_types=field_types) if x_field_2_raw else x_field_2_raw
        if x_field_2:
            # missing_bucket keeps docs that lack the sub-row field
            # (sparse fields like page_id: absent on wizard-cohort teams and on
            # non-page-promoting ad sets). Without it a composite terms source
            # EXCLUDES those docs, silently dropping whole rows from the table;
            # the null-key bucket renders as "(none)". The row source (x_field)
            # deliberately keeps the strict behavior — a doc without the row
            # label has nothing to group under.
            sources.append({x_field_2: {"terms": {"field": x_field_2, "missing_bucket": True}}})

        if table_columns:
            # Derive y_fields and per-column aggs from table_columns.
            # Phase 18 — calculation columns expand into sum
            # sub-aggs for each entry in their formula_fields list,
            # rather than a single metric_<field> sub-agg. Field-name
            # keys naturally dedup if multiple calc columns reference
            # the same field. The synthesized __calc_<ts> field on the
            # column itself is skipped — there's no real ES field to
            # query for it.
            metric_aggs = {}
            # per-(field, aggregation) key map so two columns on one
            # field but different aggregations don't collide on metric_<field>
            # (only the first ran; both read its value). Single-aggregation
            # fields keep the plain key, so existing tables are unchanged.
            _col_agg_keys = table_metric_agg_keys(table_columns)
            for col in table_columns:
                if col.get("aggregation") == "calculation":
                    for fname in col.get("formula_fields") or []:
                        key = f"metric_{_safe_agg_key(fname)}"
                        if key not in metric_aggs:
                            metric_aggs[key] = _metric_agg(
                                fname,
                                "sum",  # per-field default for calc references
                                equals_value,
                                date_range,
                                data_view=active_dv,
                            )
                    continue
                if not col.get("field"):
                    continue
                # equals columns count docs where field == the
                # column's own equals_value. Keyed by value digest (see
                # _equals_col_agg_key) so several equals columns on the same
                # field each get their own filter agg. A column without an
                # equals_value falls through to the legacy field-only path.
                if col.get("aggregation") == "equals" and col.get("equals_value") is not None:
                    key = _equals_col_agg_key(col["field"], col["equals_value"])
                    if key not in metric_aggs:
                        metric_aggs[key] = _metric_agg(
                            col["field"],
                            "equals",
                            col["equals_value"],
                            date_range,
                            data_view=active_dv,
                        )
                    continue
                # disambiguated key when this field carries multiple
                # aggregations; plain metric_<field> otherwise.
                key = _col_agg_keys.get(
                    (col["field"], col.get("aggregation", aggregation)),
                    f'metric_{_safe_agg_key(col["field"])}',
                )
                # registered ratio fields aggregate as
                # sum(num) / sum(denom). User's stored `aggregation`
                # value is intentionally ignored here.
                if is_ratio_field(col["field"]):
                    for k, v in _ratio_metric_aggs(col["field"], key).items():
                        if k not in metric_aggs:
                            metric_aggs[k] = v
                    continue
                if key not in metric_aggs:
                    metric_aggs[key] = _metric_agg(
                        col["field"],
                        col.get("aggregation", aggregation),
                        equals_value,
                        date_range,
                        data_view=col.get("data_view") or active_dv,
                    )
        else:
            metric_aggs = {
                f"metric_{_safe_agg_key(yf)}": _metric_agg(
                    yf, aggregation, equals_value, date_range, y_field_types.get(yf), data_view=active_dv
                )
                for yf in y_fields
            }

        table_aggs = {
            "table_buckets": {
                "composite": {
                    "size": 500,
                    "sources": sources,
                },
                "aggs": metric_aggs,
            }
        }
        # whole-scope totals for the grand-totals row, as a sibling
        # of the bucketed query so both share one request and one filter
        # context. The single-bucket ``filters`` wrapper is the KPI
        # pattern: bucket_script needs a multi-bucket parent, so the ratio
        # columns' sum/sum/divide triplets work here unchanged.
        if config.get("include_totals_row") is True:
            totals_aggs = _table_totals_aggs(metric_aggs)
            if totals_aggs:
                table_aggs["table_totals"] = {
                    "filters": {"filters": {"all": {"match_all": {}}}},
                    "aggs": totals_aggs,
                }
        return {
            "size": 0,
            "query": query_clause,
            "aggs": table_aggs,
        }

    # ── Line / Bar (date x-axis) ──────────────────────────
    # bar charts over a date X now bucket via date_histogram too
    # (previously only line did, so a bar chart's Interval control did nothing
    # and it rendered one bar per raw timestamp). The keyword-name heuristic
    # fallback applies to both line and bar for an x_field whose type wasn't
    # explicitly stamped 'date' by the builder.
    is_date_x = (x_field_type == "date") or (
        chart_type in ("line", "bar")
        and x_field
        and any(kw in x_field.lower() for kw in ("date", "time", "day", "month", "year"))
    )

    if chart_type in ("line", "bar") and is_date_x:
        bucket_agg = {
            "date_histogram": {
                "field": x_field,
                "calendar_interval": config.get("calendar_interval", "day"),
                # Keep the bucket-key string ISO (yyyy-MM-dd) — a stable,
                # JS-parseable key. Display formatting (the chart's chosen
                # Data Format) is applied entirely client-side in charts.js
                # via _applyDateFormat, exactly as the table column path does.
                # Formatting here too would double-format (and break re-parse
                # for keys like "15"/"Jan 2024").
                "format": "yyyy-MM-dd",
                "min_doc_count": 1,
            }
        }
    else:
        # pie, bar/line (keyword x)
        bucket_agg = {
            "terms": {
                "field": x_field,
                "size": _clamped_terms_size(config.get("terms_size", _TERMS_SIZE_DEFAULT)),
                "order": {"_count": "desc"},
            }
        }

    # One metric sub-agg per Y field
    inner_aggs = {
        f"metric_{_safe_agg_key(yf)}": _metric_agg(
            yf, aggregation, equals_value, date_range, y_field_types.get(yf), data_view=active_dv
        )
        for yf in y_fields
    }

    # ── Breakdown ─────────────────────────────────────────
    if breakdown and y_field:
        inner_aggs["breakdown_buckets"] = {
            "terms": {
                "field": _terms_field(breakdown),
                "size": 20,
            },
            "aggs": {
                f"metric_{_safe_agg_key(y_field)}": _metric_agg(
                    y_field, aggregation, equals_value, date_range, data_view=active_dv
                ),
            },
        }

    return {
        "size": 0,
        "query": query_clause,
        "aggs": {
            "x_buckets": {
                **bucket_agg,
                "aggs": inner_aggs,
            }
        },
    }
