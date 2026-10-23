"""
Server-side allowlist for model-built Elasticsearch bodies.

Ask Kavanah lets the LLM write the whole ES request body for
``search_elasticsearch`` / ``count_documents``. The tenant filter only
wraps ``body["query"]``, so anything outside the query — a ``global``
aggregation, ``runtime_mappings``, ``script_fields``, a terms lookup into
another index — could read past it. The model's input is untrusted: a user can
ask for any shape, and imported text the model reads back can carry an
indirect prompt injection.

This module accepts only a small, known-safe subset of the Query DSL and
rejects everything else. It is an allowlist, not a denylist: an unknown key,
query type or aggregation type is refused, so a new ES feature can't open a
hole by default. Callers treat a rejection as a tool error the model can read
and retry with a simpler query; nothing rejected is ever sent to ES.

Validation never mutates the caller's dict; ``validate_search_body`` returns a
copy with the size caps applied.
"""

import copy
from typing import Any

# ── Caps ──────────────────────────────────────────────────────────────────────

MAX_NLQ_HITS = 100  # matches the search_elasticsearch tool description ("max 100")
MAX_NLQ_FROM = 1000
MAX_NLQ_TERMS_SIZE = 500
_MAX_QUERY_DEPTH = 20
_MAX_AGG_DEPTH = 6
_MAX_AGG_COUNT = 50

# ── Top-level body keys ───────────────────────────────────────────────────────

_ALLOWED_TOP_LEVEL = frozenset({"query", "size", "from", "sort", "_source", "aggs", "aggregations", "track_total_hits"})

# ── Query DSL ─────────────────────────────────────────────────────────────────

# Leaf queries: they match on fields of the documents already being searched
# and cannot read any other index. Each takes ``{field: value-or-params}`` (or a
# flat params dict for the *_string / multi_match family).
_LEAF_QUERIES = frozenset(
    {
        "match_all",
        "match_none",
        "term",
        "terms",
        "range",
        "exists",
        "prefix",
        "wildcard",
        "regexp",
        "fuzzy",
        "ids",
        "match",
        "match_phrase",
        "match_phrase_prefix",
        "match_bool_prefix",
        "multi_match",
        "query_string",
        "simple_query_string",
    }
)

# Keys that must never appear anywhere under a leaf query, an aggregation's
# parameters or a sort: they run code. Defence in depth on top of the per-type
# allowlists. The only allowed leaf query that can read ANOTHER index is the
# terms lookup (``{"terms": {"f": {"index": ..., "id": ..., "path": ...}}}``);
# ``validate_query`` refuses it structurally (terms values must be a plain
# list) rather than by key name, so a real field called ``id`` or ``path``
# still works in a ``term`` / ``match`` query.
_FORBIDDEN_NESTED_KEYS = frozenset({"script", "_script"})

_BOOL_CLAUSES = frozenset({"must", "should", "filter", "must_not"})
_BOOL_SCALARS = frozenset({"minimum_should_match", "boost", "_name"})

# ── Aggregations ──────────────────────────────────────────────────────────────

# Per-type allowed parameter keys. Every allowed bucket aggregation narrows the
# documents its parent already matched, so sub-aggregations stay inside the
# scoped query. ``global`` (the reported escape), ``sampler``, ``top_hits``,
# ``scripted_metric``, ``bucket_script`` and every other type are refused.
_METRIC_PARAMS = frozenset({"field", "missing", "format"})
_AGG_PARAMS: dict[str, frozenset[str]] = {
    "terms": frozenset(
        {
            "field",
            "size",
            "shard_size",
            "order",
            "min_doc_count",
            "shard_min_doc_count",
            "missing",
            "show_term_doc_count_error",
        }
    ),
    "date_histogram": frozenset(
        {
            "field",
            "calendar_interval",
            "fixed_interval",
            "interval",
            "format",
            "time_zone",
            "min_doc_count",
            "extended_bounds",
            "hard_bounds",
            "offset",
            "order",
            "missing",
            "keyed",
        }
    ),
    "histogram": frozenset(
        {"field", "interval", "min_doc_count", "extended_bounds", "hard_bounds", "offset", "order", "missing", "keyed"}
    ),
    "range": frozenset({"field", "ranges", "keyed", "missing", "format"}),
    "date_range": frozenset({"field", "ranges", "keyed", "missing", "format", "time_zone"}),
    "avg": _METRIC_PARAMS,
    "sum": _METRIC_PARAMS,
    "min": _METRIC_PARAMS,
    "max": _METRIC_PARAMS,
    "value_count": _METRIC_PARAMS,
    "cardinality": _METRIC_PARAMS | {"precision_threshold"},
    "stats": _METRIC_PARAMS,
    "extended_stats": _METRIC_PARAMS | {"sigma"},
    "percentiles": _METRIC_PARAMS | {"percents", "keyed"},
}
# ``filter`` / ``filters`` carry queries rather than params — validated as queries.
_QUERY_BUCKET_AGGS = frozenset({"filter", "filters"})
_FILTERS_PARAMS = frozenset({"filters", "other_bucket", "other_bucket_key", "keyed"})
_SUB_AGG_KEYS = frozenset({"aggs", "aggregations"})
_TERMS_SIZE_KEYS = ("size", "shard_size")


def validate_search_body(body: Any) -> tuple[dict | None, str | None]:
    """Validate a model-built ``search_elasticsearch`` body.

    Returns ``(capped_copy, None)`` when the body is acceptable, or
    ``(None, reason)`` when it is not. The reason is short and model-readable.
    """
    if body is None:
        body = {}
    if not isinstance(body, dict):
        return None, "query_body must be a JSON object"

    unknown = sorted(set(body) - _ALLOWED_TOP_LEVEL)
    if unknown:
        return None, f"top-level key(s) not allowed: {', '.join(unknown)}"

    capped = copy.deepcopy(body)

    for key, cap in (("size", MAX_NLQ_HITS), ("from", MAX_NLQ_FROM)):
        if key in capped:
            value = capped[key]
            if not _is_int(value) or value < 0:
                return None, f"'{key}' must be a non-negative integer"
            capped[key] = min(value, cap)

    if "track_total_hits" in capped and not (
        isinstance(capped["track_total_hits"], bool) or _is_int(capped["track_total_hits"])
    ):
        return None, "'track_total_hits' must be a boolean or integer"

    if "query" in capped:
        error = validate_query(capped["query"])
        if error:
            return None, error

    if "sort" in capped:
        error = _validate_sort(capped["sort"])
        if error:
            return None, error

    if "_source" in capped:
        error = _validate_source(capped["_source"])
        if error:
            return None, error

    if "aggs" in capped and "aggregations" in capped:
        return None, "use either 'aggs' or 'aggregations', not both"
    for key in _SUB_AGG_KEYS:
        if key in capped:
            counter = [0]
            error = _validate_aggs(capped[key], depth=1, counter=counter)
            if error:
                return None, error

    return capped, None


def validate_query(query: Any, depth: int = 0) -> str | None:
    """Validate one Query DSL clause. Returns a reason string, or None if safe."""
    if depth > _MAX_QUERY_DEPTH:
        return "query is nested too deeply"
    if not isinstance(query, dict) or len(query) != 1:
        return "each query clause must be an object with exactly one query type"
    qtype, params = next(iter(query.items()))

    if qtype == "bool":
        return _validate_bool(params, depth)
    if qtype == "constant_score":
        return _validate_compound(params, depth, {"filter": "one"}, {"boost", "_name"})
    if qtype == "dis_max":
        return _validate_compound(params, depth, {"queries": "many"}, {"tie_breaker", "boost", "_name"})
    if qtype == "boosting":
        return _validate_compound(params, depth, {"positive": "one", "negative": "one"}, {"negative_boost", "boost", "_name"})
    if qtype not in _LEAF_QUERIES:
        return f"query type '{qtype}' is not allowed"

    if not isinstance(params, dict):
        if qtype in ("match_all", "match_none") and params is None:
            return None
        return f"'{qtype}' query parameters must be an object"
    error = _reject_forbidden_keys(params, f"'{qtype}' query")
    if error:
        return error
    if qtype == "terms":
        for field, values in params.items():
            if field in ("boost", "_name"):
                continue
            if not isinstance(values, list) or not all(_is_scalar(v) for v in values):
                return "'terms' values must be a list of plain values (terms lookup is not allowed)"
    if qtype == "query_string" and params.get("allow_leading_wildcard"):
        return "'query_string' with allow_leading_wildcard is not allowed"
    return None


def _validate_bool(params: Any, depth: int) -> str | None:
    if not isinstance(params, dict):
        return "'bool' query parameters must be an object"
    for key, value in params.items():
        if key in _BOOL_SCALARS:
            if not _is_scalar(value):
                return f"'bool.{key}' must be a plain value"
            continue
        if key not in _BOOL_CLAUSES:
            return f"'bool' key '{key}' is not allowed"
        clauses = value if isinstance(value, list) else [value]
        for clause in clauses:
            error = validate_query(clause, depth + 1)
            if error:
                return error
    return None


def _validate_compound(params: Any, depth: int, query_keys: dict[str, str], scalar_keys: set[str]) -> str | None:
    if not isinstance(params, dict):
        return "compound query parameters must be an object"
    for key, value in params.items():
        if key in scalar_keys:
            if not _is_scalar(value):
                return f"'{key}' must be a plain value"
            continue
        arity = query_keys.get(key)
        if arity is None:
            return f"compound query key '{key}' is not allowed"
        clauses = value if arity == "many" else [value]
        if not isinstance(clauses, list):
            return f"'{key}' must be a list of queries"
        for clause in clauses:
            error = validate_query(clause, depth + 1)
            if error:
                return error
    return None


def _validate_aggs(aggs: Any, depth: int, counter: list[int]) -> str | None:
    if depth > _MAX_AGG_DEPTH:
        return "aggregations are nested too deeply"
    if not isinstance(aggs, dict):
        return "'aggs' must be an object of named aggregations"
    for name, agg in aggs.items():
        counter[0] += 1
        if counter[0] > _MAX_AGG_COUNT:
            return "too many aggregations in one query"
        if not isinstance(agg, dict):
            return f"aggregation '{name}' must be an object"
        sub_keys = [k for k in agg if k in _SUB_AGG_KEYS]
        types = [k for k in agg if k not in _SUB_AGG_KEYS]
        if len(types) != 1:
            return f"aggregation '{name}' must have exactly one aggregation type"
        if len(sub_keys) > 1:
            return f"aggregation '{name}' uses both 'aggs' and 'aggregations'"
        agg_type = types[0]
        error = _validate_agg_body(name, agg_type, agg[agg_type])
        if error:
            return error
        for sub_key in sub_keys:
            error = _validate_aggs(agg[sub_key], depth + 1, counter)
            if error:
                return error
    return None


def _validate_agg_body(name: str, agg_type: str, params: Any) -> str | None:
    if agg_type == "filter":
        return validate_query(params)
    if agg_type == "filters":
        return _validate_filters_agg(params)
    allowed = _AGG_PARAMS.get(agg_type)
    if allowed is None:
        return f"aggregation type '{agg_type}' (in '{name}') is not allowed"
    if not isinstance(params, dict):
        return f"aggregation '{name}' parameters must be an object"
    unknown = sorted(set(params) - allowed)
    if unknown:
        return f"'{agg_type}' parameter(s) not allowed: {', '.join(unknown)}"
    if "field" in params and not isinstance(params["field"], str):
        return f"'{agg_type}.field' must be a field name"
    error = _reject_forbidden_keys(params, f"'{agg_type}' aggregation")
    if error:
        return error
    if agg_type == "terms":
        for key in _TERMS_SIZE_KEYS:
            if key in params:
                if not _is_int(params[key]) or params[key] < 0:
                    return f"'terms.{key}' must be a non-negative integer"
                params[key] = min(params[key], MAX_NLQ_TERMS_SIZE)
    return None


def _validate_filters_agg(params: Any) -> str | None:
    if not isinstance(params, dict):
        return "'filters' aggregation parameters must be an object"
    unknown = sorted(set(params) - _FILTERS_PARAMS)
    if unknown:
        return f"'filters' parameter(s) not allowed: {', '.join(unknown)}"
    filters = params.get("filters")
    if isinstance(filters, dict):
        clauses = list(filters.values())
    elif isinstance(filters, list):
        clauses = filters
    else:
        return "'filters.filters' must be an object or list of queries"
    for clause in clauses:
        error = validate_query(clause)
        if error:
            return error
    return None


def _validate_sort(sort: Any) -> str | None:
    # A ``_script`` sort is only executable with a ``script`` key, which the
    # forbidden-key walk refuses; ``nested`` carries a filter query (a terms
    # lookup could hide there), so it is refused outright.
    entries = sort if isinstance(sort, list) else [sort]
    for entry in entries:
        if isinstance(entry, str):
            continue
        if not isinstance(entry, dict):
            return "sort entries must be field names or {field: order} objects"
        for order in entry.values():
            if isinstance(order, dict):
                error = _reject_forbidden_keys(order, "sort")
                if error:
                    return error
                if "nested" in order:
                    return "nested sort is not allowed"
            elif not isinstance(order, str):
                return "sort order must be 'asc'/'desc' or an options object"
    return None


def _validate_source(source: Any) -> str | None:
    if isinstance(source, bool) or isinstance(source, str):
        return None
    if isinstance(source, list):
        return None if all(isinstance(s, str) for s in source) else "'_source' list must contain field names"
    if isinstance(source, dict):
        if set(source) - {"includes", "excludes", "include", "exclude"}:
            return "'_source' object may only have includes/excludes"
        for value in source.values():
            items = value if isinstance(value, list) else [value]
            if not all(isinstance(s, str) for s in items):
                return "'_source' includes/excludes must be field names"
        return None
    return "'_source' must be a boolean, field name, list or includes/excludes object"


def _reject_forbidden_keys(node: Any, where: str) -> str | None:
    """Walk ``node`` and reject any dict key in ``_FORBIDDEN_NESTED_KEYS``."""
    stack = [node]
    while stack:
        current = stack.pop()
        if isinstance(current, dict):
            for key, value in current.items():
                if key in _FORBIDDEN_NESTED_KEYS:
                    return f"'{key}' is not allowed inside {where}"
                stack.append(value)
        elif isinstance(current, list):
            stack.extend(current)
    return None


def _is_int(value: Any) -> bool:
    return isinstance(value, int) and not isinstance(value, bool)


def _is_scalar(value: Any) -> bool:
    return value is None or isinstance(value, (str, int, float, bool))
