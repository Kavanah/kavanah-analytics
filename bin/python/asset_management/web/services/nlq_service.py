"""
NLQ (Natural Language Query) Service — Conversational AI Agent
Uses Anthropic's tool-use API to let Claude query Elasticsearch interactively,
interpret results, and respond with meaningful narrative answers.
"""

import json
import logging
from datetime import datetime, timezone
from typing import Any

import anthropic

try:
    from web.config import config
    from lib.es_helpers import get_elasticsearch_client, merge_index_mapping_properties
    from web.services.es_query_builder import build_query
    from web.services.echarts_transformer import transform as echarts_transform
    from web.services.nlq_memory_service import store_query_memory, find_similar_queries
except ImportError:
    from ..config import config
    from lib.es_helpers import get_elasticsearch_client, merge_index_mapping_properties
    from ..services.es_query_builder import build_query
    from ..services.echarts_transformer import transform as echarts_transform
    from ..services.nlq_memory_service import store_query_memory, find_similar_queries


# ── Provider abstraction ───────────────────────────────────────────────────────


def _create_client() -> anthropic.Anthropic:
    """Create the appropriate Anthropic client based on configured provider.

    Returns anthropic.Anthropic for direct API or anthropic.AnthropicBedrock
    for AWS Bedrock. Both expose the same messages.create() interface.
    """
    provider = config.NLQ_PROVIDER
    if provider == "bedrock":
        # Explicit keys (SSM -> .env.aws -> env) win over a named profile.
        # Never log the key values — credential source only.
        access_key = config.NLQ_AWS_ACCESS_KEY_ID
        secret_key = config.NLQ_AWS_SECRET_ACCESS_KEY
        if access_key and secret_key:
            logging.info("NLQ: using AWS Bedrock provider (region=%s, credentials=explicit env keys)", config.NLQ_AWS_REGION)
            return anthropic.AnthropicBedrock(
                aws_region=config.NLQ_AWS_REGION,
                aws_access_key=access_key,
                aws_secret_key=secret_key,
            )
        profile = config.NLQ_AWS_PROFILE or None
        logging.info("NLQ: using AWS Bedrock provider (region=%s, profile=%s)", config.NLQ_AWS_REGION, profile)
        return anthropic.AnthropicBedrock(aws_region=config.NLQ_AWS_REGION, aws_profile=profile)
    else:
        api_key = config.NLQ_ANTHROPIC_API_KEY
        if not api_key:
            raise ValueError("Anthropic API key not configured")
        return anthropic.Anthropic(api_key=api_key)


def _resolve_model(choice: str = "haiku") -> str:
    """Resolve a model choice ('haiku' or 'sonnet') to the actual model ID.

    Uses the configured provider to pick the right ID format.
    """
    provider = config.NLQ_PROVIDER
    if provider == "bedrock":
        if choice == "sonnet":
            return config.NLQ_BEDROCK_MODEL_SONNET
        return config.NLQ_BEDROCK_MODEL_HAIKU
    else:
        if choice == "sonnet":
            return config.NLQ_MODEL_SONNET
        return config.NLQ_MODEL


# ── Data view + field discovery (reused from dashboard builder) ───────────────


def get_available_data_views() -> list[dict[str, str]]:
    """Return data views from Kibana via the dashboard builder's fetch logic."""
    try:
        from web.blueprints.dashboard_builder.routes import _fetch_kibana_data_views, _without_switched_off_sources
    except ImportError:
        from ..blueprints.dashboard_builder.routes import _fetch_kibana_data_views, _without_switched_off_sources

    data_views = _fetch_kibana_data_views("default")
    data_views = [
        dv
        for dv in data_views
        if not dv["id"].startswith(".")
        and dv["id"] not in ("logs", "metrics")
        and not dv["id"].startswith("logs-")
        and not dv["id"].startswith("metrics-")
    ]
    # a switched-off connector's view is not queryable either.
    return _without_switched_off_sources(data_views)


def get_allowed_index_names() -> set[str]:
    """Return the set of index patterns the NLQ tool loop is allowed to query
    .

    The model picks the ``index`` / ``data_view`` for every ES tool call from
    free-form tool input. ``dv["id"]`` is the ES index pattern (Kibana data-view
    title, e.g. ``*_fb_insights`` / ``disciple-tools``) — the same string the
    model is told to pass — so this is an exact-match allowlist. ``.``-prefixed
    system indices and the logs/metrics views are already excluded by
    get_available_data_views(). Resolved once per request and passed into
    _execute_tool; an empty set (Kibana unreachable) fails closed — every index
    tool is denied rather than letting an unconstrained name through.
    """
    return {dv["id"] for dv in get_available_data_views() if dv.get("id")}


def _simplify_es_type(es_type: str) -> str:
    """Map ES field types to simplified categories."""
    if es_type == "date":
        return "date"
    if es_type == "text":
        return "text"
    if es_type in ("keyword", "constant_keyword", "wildcard"):
        return "keyword"
    if es_type in (
        "long",
        "integer",
        "short",
        "byte",
        "float",
        "double",
        "half_float",
        "scaled_float",
        "unsigned_long",
    ):
        return "numeric"
    if es_type == "boolean":
        return "boolean"
    return "keyword"


def _flatten_mapping(props: dict, prefix: str = "") -> list[dict]:
    """Recursively flatten ES mapping properties into [{name, type}]."""
    fields = []
    for fname, fmeta in props.items():
        full_name = f"{prefix}.{fname}" if prefix else fname
        ftype = fmeta.get("type", "")
        # a never-populated dynamic object reports
        # `"type": "object"`; emitting it would offer a field that always
        # aggregates to zero buckets. After the deep merge a node can
        # carry BOTH `type: object` (from a bare-object index) and
        # `properties` (from a populated one) — skip the parent, keep
        # recursing into its real sub-fields below.
        if ftype and ftype != "object":
            fields.append({"name": full_name, "type": _simplify_es_type(ftype)})
        if "properties" in fmeta:
            fields.extend(_flatten_mapping(fmeta["properties"], full_name))
        if "fields" in fmeta:
            fields.extend(_flatten_mapping(fmeta["fields"], full_name))
    return fields


def get_fields_for_data_view(data_view_id: str) -> list[dict]:
    """Fetch field names and types from ES mapping for a data view."""
    es = get_elasticsearch_client()
    if es is None:
        return []
    try:
        mapping = es.indices.get_mapping(index=data_view_id)
        # deep-union across indices (see get_fields in the
        # dashboard_builder blueprint); shallow first-index-wins made
        # dynamic-object sub-fields a per-request coin flip.
        merged_props = merge_index_mapping_properties(mapping)
        fields = _flatten_mapping(merged_props)
        fields.sort(key=lambda f: f["name"])
        return fields
    except Exception as e:
        logging.error(f"NLQ: failed to get fields for {data_view_id}: {e}")
        return []


# ── Tool definitions for Anthropic API ────────────────────────────────────────

TOOLS = [
    {
        "name": "list_data_views",
        "description": (
            "List all available data views (Elasticsearch index patterns) with their labels. "
            "Call this to discover what data sources are available before querying."
        ),
        "input_schema": {
            "type": "object",
            "properties": {},
            "required": [],
        },
    },
    {
        "name": "get_fields",
        "description": (
            "Get the field names and types for a data view. Call this before writing a query "
            "to know what fields are available. Returns fields with their types "
            "(keyword, text, numeric, date, boolean)."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "data_view": {
                    "type": "string",
                    "description": "The data view ID (ES index pattern), e.g. '*_fb_insights' or 'disciple-tools'",
                },
            },
            "required": ["data_view"],
        },
    },
    {
        "name": "search_elasticsearch",
        "description": (
            "Execute an Elasticsearch query and return results. Use the full ES Query DSL. "
            "Always set size: 0 for aggregation queries. For raw document queries, set size to "
            "the number of documents you want (max 100). Results include hits and aggregations."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "index": {
                    "type": "string",
                    "description": "ES index name or pattern (e.g. '*_fb_insights')",
                },
                "query_body": {
                    "type": "object",
                    "description": "Full Elasticsearch query body (JSON object with query, aggs, size, etc.)",
                },
            },
            "required": ["index", "query_body"],
        },
    },
    {
        "name": "count_documents",
        "description": (
            "Quick document count for an index with optional query filter. " "Faster than search when you just need a count."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "index": {
                    "type": "string",
                    "description": "ES index name or pattern",
                },
                "query": {
                    "type": "object",
                    "description": 'Optional ES query clause (e.g. {"bool": {"filter": [...]}}). Omit for total count.',
                },
            },
            "required": ["index"],
        },
    },
    {
        "name": "render_chart",
        "description": (
            "Render an interactive chart inline in your response. Use this when a visual "
            "would be more useful than a table or narrative — e.g. trends over time, "
            "comparisons across categories, or proportional breakdowns. "
            "The chart is rendered as an interactive ECharts component in the chat. "
            "You MUST still provide a brief narrative interpretation after the chart."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "data_view": {
                    "type": "string",
                    "description": "ES index pattern (e.g. '*_fb_insights')",
                },
                "chart_type": {
                    "type": "string",
                    "enum": ["bar", "line", "pie"],
                    "description": "Type of chart to render",
                },
                "x_field": {
                    "type": "string",
                    "description": "Field for the x-axis (category or date field)",
                },
                "x_field_type": {
                    "type": "string",
                    "enum": ["keyword", "date", "text"],
                    "description": "Type of the x_field (use 'date' for time-series)",
                },
                "y_fields": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "Numeric field(s) to aggregate on the y-axis",
                },
                "aggregation": {
                    "type": "string",
                    "enum": ["sum", "avg", "min", "max", "count", "cardinality"],
                    "description": "Aggregation function (default: sum)",
                },
                "filters": {
                    "type": "array",
                    "items": {
                        "type": "object",
                        "properties": {
                            "field": {"type": "string"},
                            "op": {"type": "string", "enum": ["eq", "neq", "gt", "gte", "lt", "lte", "in", "match_phrase"]},
                            "value": {},
                        },
                        "required": ["field", "op", "value"],
                    },
                    "description": "Optional filters to narrow the data",
                },
                "date_range": {
                    "type": "object",
                    "properties": {
                        "field": {"type": "string"},
                        "from": {"type": "string"},
                        "to": {"type": "string"},
                    },
                    "description": "Optional date range filter (ISO dates)",
                },
                "breakdown": {
                    "type": "string",
                    "description": "Optional field to split series by (creates multi-series chart)",
                },
                "title": {
                    "type": "string",
                    "description": "Chart title displayed above the chart",
                },
                "calendar_interval": {
                    "type": "string",
                    "enum": ["day", "week", "month", "quarter", "year"],
                    "description": "Interval for date histograms (default: day). Only used when x_field_type is 'date'.",
                },
            },
            "required": ["data_view", "chart_type", "x_field", "x_field_type", "y_fields", "aggregation"],
        },
    },
]


# ── Tool execution ────────────────────────────────────────────────────────────


# ── Tenant-scope enforcement ──────────────────────────────────────────
#
# The Data Explorer endpoints accept client-supplied indices / organizations /
# teams, but those only ever reached the model as advisory prompt text — it was
# *asked* to add org/team filters. Prompt-following is not an access control. We
# enforce tenant isolation server-side: every ES query the tool loop executes is
# AND-ed with the caller's allowed-team filter (the same
# RoleFilterService.get_filter_for_current_user() clause used across the app),
# resolved once per request and injected at execution time regardless of what the
# model (or the client) asked for. Callers with no resolvable scope fail closed.

# team_title value that matches no tenant data (mirrors
# RoleFilterService._DENY_ALL_FILTER) — the canonical deny-all sentinel.
_NLQ_DENY_ALL_FILTER = {"term": {"team_title": "__NO_TEAMS_FOR_ORG__"}}


def _resolve_scope_filter() -> dict:
    """Resolve the mandatory ES tenant-scope clause for the current Flask-session
    user. Fail closed: a caller with no resolvable scope (or any resolution
    error) gets the deny-all sentinel, never unrestricted access.
    """
    try:
        from web.services.role_filter_service import RoleFilterService
    except ImportError:
        from ..services.role_filter_service import RoleFilterService
    try:
        # Deny-all floor: no resolvable scope from any source -> see nothing.
        if not RoleFilterService.current_user_has_any_scope():
            return _NLQ_DENY_ALL_FILTER
        clause = RoleFilterService.get_filter_for_current_user()
    except Exception:
        logging.exception("NLQ: tenant-scope resolution failed; failing closed")
        return _NLQ_DENY_ALL_FILTER
    # None historically means "genuinely unscoped pass-through"; on the NLQ
    # surface that must mean deny-all, not see-all (has_any_scope already let
    # the legitimately-scoped users through above).
    if not clause:
        return _NLQ_DENY_ALL_FILTER
    return clause


def resolve_memory_tenant_keys() -> list[str]:
    """Resolve the tenant keys (team_titles) that scope NLQ memory for the
    current Flask-session user.

    Mirrors the data-scope posture so a user sees few-shot examples /
    suggestions drawn only from memories tagged with teams they can actually
    query: fail closed to ``[]`` (which matches no memory) when the caller has
    no resolvable scope, never the whole store. Uses the same unified
    ``resolve_user_scope`` resolver that backs ``get_filter_for_current_user``.
    """
    try:
        from web.services.role_filter_service import RoleFilterService, resolve_user_scope
    except ImportError:
        from ..services.role_filter_service import RoleFilterService, resolve_user_scope
    try:
        from flask import session

        if not RoleFilterService.current_user_has_any_scope():
            return []
        cfg = RoleFilterService.load_config()
        username = session.get("username", "")
        user_roles = session.get("roles", []) or []
        scope = resolve_user_scope(username, user_roles, cfg)
        return list(scope.team_titles)
    except Exception:
        logging.exception("NLQ memory: tenant-key resolution failed; failing closed")
        return []


def _apply_scope_filter(query_body: dict | None, scope_clause: dict) -> dict:
    """Return a copy of ``query_body`` whose query is AND-ed with the mandatory
    tenant ``scope_clause``. Whatever query the model built becomes a ``must``
    alongside a non-negotiable ``filter`` — so out-of-scope rows stay unreachable
    even if the model emits an unfiltered or cross-tenant query.
    """
    body = dict(query_body or {})
    inner = body.get("query") or {"match_all": {}}
    body["query"] = {"bool": {"must": [inner], "filter": [scope_clause]}}
    return body


def _deny_index(index: str, allowed_indices: set[str]) -> str:
    """JSON error returned when the model targets an index outside the allowlist
    . Names the requested index and points the model back at
    list_data_views so it can self-correct within the tool loop."""
    logging.warning("NLQ: blocked out-of-allowlist index %r (allowed=%d views)", index, len(allowed_indices))
    return json.dumps(
        {
            "error": (
                f"Index '{index}' is not an available data view and cannot be queried. "
                "Call list_data_views and use one of the returned data view IDs."
            )
        }
    )


def _execute_tool(tool_name: str, tool_input: dict, scope_clause: dict, allowed_indices: set[str]) -> str:
    """Execute a tool call and return the result as a JSON string.

    ``scope_clause`` is the mandatory tenant filter injected into every
    ES query this tool runs, enforcing the caller's org/team scope server-side.
    ``allowed_indices`` is the data-view allowlist: any tool that takes
    a model-chosen index/data_view is rejected unless the value is in the set, so
    the model can't target system indices the team_title scope clause doesn't
    cover.
    """
    try:
        if tool_name == "list_data_views":
            views = get_available_data_views()
            return json.dumps({"data_views": views}, default=str)

        elif tool_name == "get_fields":
            data_view = tool_input.get("data_view", "")
            if data_view not in allowed_indices:
                return _deny_index(data_view, allowed_indices)
            fields = get_fields_for_data_view(data_view)
            if not fields:
                return json.dumps({"error": f"No fields found for '{data_view}'. Check the data view ID."})
            return json.dumps({"fields": fields, "count": len(fields)}, default=str)

        elif tool_name == "search_elasticsearch":
            index = tool_input.get("index", "")
            if index not in allowed_indices:
                return _deny_index(index, allowed_indices)
            query_body = _apply_scope_filter(tool_input.get("query_body", {}), scope_clause)
            es = get_elasticsearch_client()
            if es is None:
                return json.dumps({"error": "Elasticsearch unavailable"})
            result = es.search(index=index, body=query_body, ignore_unavailable=True)
            # Trim large responses to stay within token limits
            result_str = json.dumps(dict(result), default=str)
            if len(result_str) > 15000:
                # Truncate hits to save tokens, keep aggregations
                trimmed = {
                    "hits": {
                        "total": result.get("hits", {}).get("total"),
                        "hits": result.get("hits", {}).get("hits", [])[:10],
                    },
                    "aggregations": result.get("aggregations", {}),
                }
                result_str = json.dumps(trimmed, default=str)
            return result_str

        elif tool_name == "count_documents":
            index = tool_input.get("index", "")
            if index not in allowed_indices:
                return _deny_index(index, allowed_indices)
            query = tool_input.get("query", {"match_all": {}})
            es = get_elasticsearch_client()
            if es is None:
                return json.dumps({"error": "Elasticsearch unavailable"})
            count_body = _apply_scope_filter({"query": query}, scope_clause)
            result = es.count(index=index, body=count_body, ignore_unavailable=True)
            return json.dumps({"count": result.get("count", 0)}, default=str)

        elif tool_name == "render_chart":
            data_view = tool_input.get("data_view", "")
            if data_view not in allowed_indices:
                return _deny_index(data_view, allowed_indices)
            chart_config = {
                "data_view": data_view,
                "chart_type": tool_input.get("chart_type", "bar"),
                "x_field": tool_input.get("x_field", ""),
                "x_field_type": tool_input.get("x_field_type", "keyword"),
                "y_fields": tool_input.get("y_fields", []),
                "aggregation": tool_input.get("aggregation", "sum"),
                "filters": tool_input.get("filters", []),
                "date_range": tool_input.get("date_range"),
                "breakdown": tool_input.get("breakdown"),
                "calendar_interval": tool_input.get("calendar_interval", "day"),
            }
            es = get_elasticsearch_client()
            if es is None:
                return json.dumps({"error": "Elasticsearch unavailable"})
            query_body = _apply_scope_filter(build_query(chart_config), scope_clause)
            es_response = es.search(
                index=chart_config["data_view"],
                body=query_body,
                ignore_unavailable=True,
            )
            echarts_option = echarts_transform(dict(es_response), chart_config)
            title = tool_input.get("title", "")
            if title and isinstance(echarts_option.get("title"), dict):
                echarts_option["title"]["text"] = title
            return json.dumps({"chart": echarts_option}, default=str)

        else:
            return json.dumps({"error": f"Unknown tool: {tool_name}"})

    except Exception as e:
        logging.error(f"NLQ tool '{tool_name}' error: {e}")
        return json.dumps({"error": str(e)})


# ── System prompt ─────────────────────────────────────────────────────────────

SYSTEM_PROMPT = """\
You are Kavanah, an AI data analytics assistant for digital marketing campaigns.
You help users explore and understand data stored in Elasticsearch indices \
covering Facebook Ads, Google Ads, Instagram, YouTube Analytics, \
Echo messaging, Chatwoot, and Disciple Tools.

You have tools to discover available data sources, inspect their fields, \
run Elasticsearch queries, and render interactive charts.

When answering:
- Provide clear, concise narrative answers with key numbers **highlighted in bold**
- Compare periods when relevant (e.g. month-over-month changes)
- Include specific numbers, percentages, and trends
- Format responses in markdown — use tables for structured data
- If the question is ambiguous, ask for clarification rather than guessing
- If you need to query data, first use get_fields to understand the schema, \
then construct an appropriate Elasticsearch query
- For keyword fields, use them directly in terms aggregations
- For text fields, append .keyword for aggregations
- Use "kibana_date" as the default date field for filtering by date range
- Today's date is {today}

## Disciple Tools date fields

When querying the `disciple-tools` index, use the correct date field based on what \
the user is asking about — do NOT default to `kibana_date` or `date_created_ms` for every query:

| Metric | Date field |
|---|---|
| New Contacts | `date_created_ms` |
| 1st Meetings / First contact | `meta.first_contact_date_ms` |
| Baptisms | `meta.baptism_date_ms` |
| New Groups | `meta.start_date_ms` |
| New Churches | `meta.church_start_date_ms` |
| General / fallback | `date_modified_ms` |

All DT `_ms` fields are mapped as ES `date` type with `epoch_millis` format. \
Use ES date math in range queries — e.g. `"gte": "now/y"` for start of this year, \
`"lte": "now"` for now. Do NOT try to compute epoch millisecond numbers yourself. \
For non-DT indices, use `kibana_date`.

When you don't know which data source to use, call list_data_views first \
to see what's available, then ask the user which one they're interested in.

## Charts

Use the render_chart tool when a visual would communicate the data better \
than a table or plain numbers — especially for trends over time, comparisons \
across categories, or proportional breakdowns.

When you call render_chart and receive a chart result, embed it in your \
response using this exact marker format on its own line:

<!--chart:CHART_JSON_HERE-->

Replace CHART_JSON_HERE with the exact JSON object from the "chart" key \
in the tool result. Do NOT wrap it in a code block. Place it where the \
chart should appear in your response, then continue with your narrative \
interpretation below it.

Chart type guidance:
- **line**: Best for trends over time. Set x_field_type to "date" and \
choose an appropriate calendar_interval (day, week, month).
- **bar**: Best for comparing categories or ranked values.
- **pie**: Best for showing proportional breakdowns (limit to ~8 slices).

Keep responses concise but informative. Don't show raw JSON to the user — \
interpret the data and present it meaningfully.\
"""

MAX_TOOL_ITERATIONS = 12


# ── Structured query system prompts (two-call pattern) ─────────────────────────


STRUCTURED_QUERY_PROMPT = """\
You are Kavanah, an AI data analytics assistant for digital marketing campaigns.
You help users explore data stored in Elasticsearch indices covering Facebook Ads, \
Google Ads, Instagram, YouTube Analytics, Echo messaging, Chatwoot, and Disciple Tools.

Your task: given a user's natural language question and context, use the available \
tools to query Elasticsearch and return a structured result.

## Important: Not every question requires an ES query

If the user asks a general question (e.g. "what can you do?", "help", "who are you?"), \
answer it directly without calling any tools. Return a JSON response with only the \
narrative field populated and empty stats/chart/table:
{"narrative": "Your answer here.", "stats": [], "chart": null, "table": {"headers": [], "rows": []}, "es_query": {}}

Only use tools and generate charts/tables when the user is asking about actual data.

## Instructions (for data questions only)

1. First, if you are unsure which index to use, call list_data_views to discover available indices.
2. Use get_fields to understand the schema of the target index.
3. Use search_elasticsearch to run the appropriate query.
4. After receiving ES results, return your answer as a JSON object with this exact structure:

```json
{
  "narrative": "2-4 sentence plain-English interpretation. Use **bold** for key figures.",
  "stats": [
    {
      "label": "Metric Name",
      "value": "$1.2M",
      "change": "up 18.4% YoY",
      "direction": "up"
    }
  ],
  "chart": {
    "type": "bar",
    "data": {
      "labels": ["Label1", "Label2"],
      "datasets": [
        {
          "label": "Series Name",
          "data": [100, 200]
        }
      ]
    }
  },
  "table": {
    "headers": ["Column1", "Column2"],
    "rows": [["val1", "val2"]]
  },
  "es_query": {}
}
```

## Rules
- Always provide exactly 4 stat cards in the stats array
- For stats.direction: use "up" for positive/improvement, "down" for negative/decline, "neutral" for informational
- Chart type should be "bar" for comparisons, "line" for trends over time, "doughnut" for composition/breakdown
- Table should contain the detailed data backing the chart
- es_query should be the actual ES query body you executed
- Use "kibana_date" as the default date field for filtering by date range
- For keyword fields, use them directly in terms aggregations
- For text fields, append .keyword for aggregations
- Today's date is {today}
- Target indices: {indices}

## Time Range — DEFAULT, user question takes precedence
- Selected time range: {date_range}

Apply this time range as the default date filter on every ES query that has \
a time dimension. Convert the label to ES date math on the appropriate date \
field (e.g. `kibana_date` for most indices, or the DT field mapping below for \
the `disciple-tools` index):

| Label | ES date math |
|---|---|
| Last 7 days | `"gte": "now-7d/d", "lte": "now"` |
| Last 30 days | `"gte": "now-30d/d", "lte": "now"` |
| Last 90 days | `"gte": "now-90d/d", "lte": "now"` |
| Last 6 months | `"gte": "now-6M/d", "lte": "now"` |
| Last 12 months | `"gte": "now-12M/d", "lte": "now"` |
| Year to date | `"gte": "now/y", "lte": "now"` |
| All time | (omit the date range filter entirely) |

**PRECEDENCE RULE — CRITICAL:** If the user's question mentions a specific \
date, month, year, quarter, or relative period (e.g. "in March", "last week", \
"Q1 2025", "from Jan to April", "yesterday", "this year"), the user's \
phrasing OVERRIDES the selected time range above. Use the period the user \
typed, NOT the dropdown selection. Only fall back to the selected time range \
when the user's question contains no time reference.

## Organization & Team Filtering — MANDATORY
- Selected organizations: {organizations}
- Selected teams: {teams}

**You MUST add these filters to EVERY search_elasticsearch query when orgs or teams \
are specified (i.e. not "all").** These are keyword fields that exist in all data indices.

If teams are specified, filter by team_title ONLY (skip org filter — teams already scope the data).
If only orgs are specified (no teams), filter by organization_title.

### Single team example:
Add to query_body.query.bool.filter:
{"match_phrase": {"team_title": "TeamName"}}

### Multiple teams example:
Add to query_body.query.bool.filter:
{"bool": {"should": [{"match_phrase": {"team_title": "Team A"}}, {"match_phrase": {"team_title": "Team B"}}], "minimum_should_match": 1}}

### Single org example (no teams selected):
Add to query_body.query.bool.filter:
{"match_phrase": {"organization_title": "OrgName"}}

### Multiple orgs example:
Add to query_body.query.bool.filter:
{"bool": {"should": [{"match_phrase": {"organization_title": "Org A"}}, {"match_phrase": {"organization_title": "Org B"}}], "minimum_should_match": 1}}

## Disciple Tools date fields

When querying the `disciple-tools` index, use the correct date field based on what \
the user is asking about. Do NOT default to `kibana_date` or `date_created_ms` for \
every DT query — pick the field that matches the metric:

| Metric | Date field | Notes |
|---|---|---|
| New Contacts | `date_created_ms` | When the contact was created |
| 1st Meetings / First contact | `meta.first_contact_date_ms` | Date of first contact |
| Baptisms | `meta.baptism_date_ms` | Date of baptism event |
| New Groups | `meta.start_date_ms` | Group start date |
| New Churches | `meta.church_start_date_ms` | Church plant date |
| General / fallback | `date_modified_ms` | Last modified date |

All DT `_ms` fields are mapped as ES `date` type with `epoch_millis` format. \
Use ES date math in range queries — e.g. `"gte": "now/y"` for start of this year, \
`"lte": "now"` for now. Do NOT try to compute epoch millisecond numbers yourself. \
For non-DT indices, continue using `kibana_date` as the default date field.

## CRITICAL
Your final response MUST be valid JSON matching the schema above. Do not include \
any text outside the JSON object. Do not wrap in markdown code fences.\
"""


def structured_query(
    query: str,
    indices: list[str] | None = None,
    organizations: list[str] | None = None,
    teams: list[str] | None = None,
    date_range: str | None = None,
    comparison: str | None = None,
    today: str | None = None,
    model_choice: str = "haiku",
) -> dict[str, Any]:
    """Run a structured Ask Kavanah query using the tool-use pattern.

    Uses the existing tool-use loop to let Claude query ES, then asks Claude
    to format the results as a structured JSON response with narrative, stats,
    chart config, table data, and the ES query.

    Args:
        query: Natural language question from the user.
        indices: List of ES index patterns to target.
        organizations: List of organization names to filter by.
        teams: List of team names to filter by.
        date_range: Date range context (e.g., "Q1 2025", "Last 30 days").
        comparison: Comparison period (e.g., "vs same period LY").
        today: Today's date string (YYYY-MM-DD).
        model_choice: "haiku" or "sonnet".

    Returns:
        dict with success, result (structured), and usage.
    """
    try:
        client = _create_client()
    except ValueError as e:
        # don't echo client construction error text (may include the
        # full config path / key fragment) to the response body.
        logging.exception("NLQ structured query: failed to create Anthropic client")
        return {"success": False, "error": "NLQ service is not configured correctly"}

    model = _resolve_model(model_choice)

    if not today:
        today = datetime.now(timezone.utc).strftime("%Y-%m-%d")

    indices_str = ", ".join(indices) if indices else "all available"
    orgs_str = ", ".join(organizations) if organizations else "all"
    teams_str = ", ".join(teams) if teams else "all"
    date_range_str = date_range or "Last 30 days"
    system_prompt = (
        STRUCTURED_QUERY_PROMPT.replace("{today}", today)
        .replace("{indices}", indices_str)
        .replace("{organizations}", orgs_str)
        .replace("{teams}", teams_str)
        .replace("{date_range}", date_range_str)
    )

    # resolve the caller's memory tenant scope once; few-shot reads and
    # the stored memory are both keyed on it so memory never crosses tenants.
    memory_tenant_keys = resolve_memory_tenant_keys()

    # Few-shot injection: retrieve similar past queries from memory store
    try:
        similar = find_similar_queries(query, n=3, tenant_keys=memory_tenant_keys)
        if similar:
            examples_text = "\n\n## Past Query Examples (for reference)\n"
            for ex in similar:
                examples_text += f"\n**Q:** {ex['query_text']}\n"
                if ex.get("es_query"):
                    examples_text += f"**ES Query:** {json.dumps(ex['es_query'])[:300]}\n"
                if ex.get("result_summary"):
                    examples_text += f"**Summary:** {ex['result_summary'][:200]}\n"
                if ex.get("chart_type"):
                    examples_text += f"**Chart:** {ex['chart_type']}\n"
            system_prompt += examples_text
    except Exception as e:
        logging.warning(f"NLQ memory: few-shot retrieval failed: {e}")

    total_input_tokens = 0
    total_output_tokens = 0

    current_messages: list[dict[str, Any]] = [
        {"role": "user", "content": query},
    ]

    last_es_index = None  # Track the index used in the last search_elasticsearch call

    # resolve the caller's mandatory tenant-scope filter once; every ES
    # query the tool loop runs is AND-ed with it (server-side enforcement).
    scope_clause = _resolve_scope_filter()
    # resolve the data-view allowlist once; every model-chosen index is
    # validated against it before any ES call.
    allowed_indices = get_allowed_index_names()

    for iteration in range(MAX_TOOL_ITERATIONS):
        try:
            response = client.messages.create(
                model=model,
                max_tokens=config.NLQ_MAX_TOKENS,
                system=system_prompt,
                tools=TOOLS,
                messages=current_messages,
            )
        except anthropic.AuthenticationError:
            return {"success": False, "error": "Invalid Anthropic API key"}
        except anthropic.RateLimitError:
            return {"success": False, "error": "API rate limit exceeded — try again shortly"}
        except Exception:
            # keep the traceback in the server log; surface a
            # generic message to the client.
            logging.exception("NLQ structured query: Anthropic API call failed")
            return {"success": False, "error": "API error — see server logs for detail"}

        total_input_tokens += response.usage.input_tokens
        total_output_tokens += response.usage.output_tokens
        stop_reason = response.stop_reason

        if stop_reason == "tool_use":
            current_messages.append({"role": "assistant", "content": response.content})

            tool_results = []
            for block in response.content:
                if block.type == "tool_use":
                    logging.info(f"NLQ structured tool call: {block.name}({json.dumps(block.input)[:200]})")
                    # Track the index used in ES tool calls
                    if block.name in ("search_elasticsearch", "count_documents"):
                        last_es_index = block.input.get("index", "")
                    elif block.name == "render_chart":
                        last_es_index = block.input.get("data_view", "")
                    result = _execute_tool(block.name, block.input, scope_clause, allowed_indices)
                    tool_results.append(
                        {
                            "type": "tool_result",
                            "tool_use_id": block.id,
                            "content": result,
                        }
                    )

            current_messages.append({"role": "user", "content": tool_results})
            continue

        # Claude is done — extract text and parse JSON
        text_parts = []
        for block in response.content:
            if hasattr(block, "text"):
                text_parts.append(block.text)

        raw_text = "\n".join(text_parts).strip()

        usage = {
            "input_tokens": total_input_tokens,
            "output_tokens": total_output_tokens,
            "model": model,
            "tool_iterations": iteration + 1,
        }

        # Try to parse the structured JSON response
        result = _parse_structured_response(raw_text)
        if result:
            logging.info("NLQ structured query: parsed structured JSON successfully")
            if last_es_index:
                result["es_index"] = last_es_index
            _store_memory(query, result, indices, date_range, memory_tenant_keys)
            return {"success": True, "result": result, "usage": usage}

        # If the model hit the output token cap, the JSON is truncated.
        # Don't bother with a reformat call — show the user a clear warning.
        if stop_reason == "max_tokens":
            logging.warning(
                "NLQ structured query: response truncated at max_tokens "
                f"(limit={config.NLQ_MAX_TOKENS}, output_tokens={response.usage.output_tokens})"
            )
            return {
                "success": True,
                "result": {
                    "narrative": (
                        "⚠️ **Response exceeded the token limit and was truncated.** "
                        "This usually happens with queries that return a large number of "
                        "rows or data points (e.g. daily values over a long date range). "
                        "Try one of these:\n\n"
                        "- Narrow the date range\n"
                        "- Ask for a weekly or monthly summary instead of daily\n"
                        "- Ask for just the totals or top N results"
                    ),
                    "stats": [],
                    "chart": None,
                    "table": {"headers": [], "rows": []},
                    "es_query": {},
                    "truncated": True,
                },
                "usage": usage,
            }

        # JSON parsing failed for a non-truncation reason — ask the model to reformat
        logging.info(
            "NLQ structured query: JSON parse failed on raw response "
            f"(len={len(raw_text)}, starts_with={raw_text[:80]!r}), "
            "making formatting call"
        )
        structured = _format_as_structured(client, raw_text, today, model)
        if structured:
            # Merge usage from both calls
            usage["input_tokens"] += structured.pop("_input_tokens", 0)
            usage["output_tokens"] += structured.pop("_output_tokens", 0)
            if last_es_index:
                structured["es_index"] = last_es_index
            _store_memory(query, structured, indices, date_range, memory_tenant_keys)
            return {"success": True, "result": structured, "usage": usage}

        # Final fallback — reformat also failed. Show a generic error rather than
        # dumping raw model output (which may contain JSON fragments or prose).
        logging.warning(
            "NLQ structured query: both primary and reformat parse failed "
            f"(raw_len={len(raw_text)}, starts_with={raw_text[:80]!r})"
        )
        return {
            "success": True,
            "result": {
                "narrative": (
                    "⚠️ **The response could not be parsed.** "
                    "The model returned output that didn't match the expected format. "
                    "Try rephrasing your question or asking for a simpler breakdown."
                ),
                "stats": [],
                "chart": None,
                "table": {"headers": [], "rows": []},
                "es_query": {},
            },
            "usage": usage,
        }

    return {
        "success": False,
        "error": "Reached maximum tool iterations. Try a simpler question.",
        "usage": {
            "input_tokens": total_input_tokens,
            "output_tokens": total_output_tokens,
            "model": model,
            "tool_iterations": MAX_TOOL_ITERATIONS,
        },
    }


def _store_memory(
    query: str,
    result: dict[str, Any],
    indices: list[str] | None,
    date_range: str | None,
    tenant_keys: list[str] | None = None,
) -> None:
    """Store a successful query result in the memory store (fire-and-forget).

    ``tenant_keys`` tags the record with the writer's tenant scope so
    later reads can be confined to the same tenant.
    """
    try:
        chart = result.get("chart") or {}
        stats = result.get("stats") or []
        store_query_memory(
            query_text=query,
            es_query=result.get("es_query"),
            result_summary=result.get("narrative", ""),
            intent_class="",
            chart_type=chart.get("type", ""),
            stat_labels=[s.get("label", "") for s in stats],
            indices_used=indices,
            date_range=date_range or "",
            tenant_teams=tenant_keys,
        )
    except Exception as e:
        logging.warning(f"NLQ memory: failed to store query: {e}")


def _parse_structured_response(text: str) -> dict[str, Any] | None:
    """Try to parse a structured JSON response from Claude's output.

    Handles cases where Claude wraps JSON in markdown code fences
    or includes extra text before/after.
    """
    # Strip markdown code fences if present
    cleaned = text.strip()
    if cleaned.startswith("```"):
        # Remove opening fence (with optional language tag)
        first_newline = cleaned.index("\n") if "\n" in cleaned else len(cleaned)
        cleaned = cleaned[first_newline + 1 :]
        if cleaned.endswith("```"):
            cleaned = cleaned[:-3]
        cleaned = cleaned.strip()

    # Try direct parse
    try:
        result = json.loads(cleaned)
        if isinstance(result, dict) and "narrative" in result:
            return result
    except json.JSONDecodeError:
        pass

    # Try to find JSON object in the text
    brace_start = cleaned.find("{")
    brace_end = cleaned.rfind("}")
    if brace_start != -1 and brace_end > brace_start:
        try:
            result = json.loads(cleaned[brace_start : brace_end + 1])
            if isinstance(result, dict) and "narrative" in result:
                return result
        except json.JSONDecodeError:
            pass

    return None


_FORMAT_PROMPT = """\
You are a data formatting assistant. Convert the following analytics response \
into a structured JSON object. Extract the key information and format it exactly \
as specified.

## Input (analyst response to format)
{response_text}

## Required output format
Return ONLY a JSON object with these keys:
{
  "narrative": "The main narrative text, 2-4 sentences. Use **bold** for key figures.",
  "stats": [
    {"label": "Metric Name", "value": "$1.2M", "change": "up 18.4% YoY", "direction": "up"},
    {"label": "Metric 2", "value": "3.8%", "change": "Baseline", "direction": "neutral"},
    {"label": "Metric 3", "value": "42", "change": "down 5%", "direction": "down"},
    {"label": "Metric 4", "value": "N/A", "change": "-", "direction": "neutral"}
  ],
  "chart": {
    "type": "bar",
    "data": {
      "labels": ["Label1", "Label2"],
      "datasets": [{"label": "Series", "data": [100, 200]}]
    }
  },
  "table": {
    "headers": ["Col1", "Col2", "Col3"],
    "rows": [["val1", "val2", "val3"]]
  },
  "es_query": {}
}

## Rules
- Always provide exactly 4 stat cards. If fewer metrics exist, fill remaining with relevant context.
- direction: "up" = positive/improvement, "down" = negative/decline, "neutral" = informational
- chart.type: "bar" for comparisons, "line" for trends, "doughnut" for composition
- Extract numeric data from the narrative/tables for the chart datasets
- table rows should contain the detailed breakdown data
- If the response contains an ES query, include it in es_query. Otherwise use {}.
- Return ONLY the JSON — no markdown fences, no extra text.\
"""


def _format_as_structured(
    client: "anthropic.Anthropic", raw_response: str, today: str, model: str = ""
) -> dict[str, Any] | None:
    """Make a second Claude call to format a free-text response into structured JSON.

    This is Step 2 of the two-call pattern: the first call queried ES and produced
    a narrative; this call restructures it into chart/table/stats format.
    """
    if not model:
        model = _resolve_model("haiku")
    try:
        prompt = _FORMAT_PROMPT.replace("{response_text}", raw_response)
        response = client.messages.create(
            model=model,
            max_tokens=config.NLQ_MAX_TOKENS,
            system="You are a JSON formatting assistant. Return only valid JSON.",
            messages=[{"role": "user", "content": prompt}],
        )

        text_parts = []
        for block in response.content:
            if hasattr(block, "text"):
                text_parts.append(block.text)

        raw_text = "\n".join(text_parts).strip()
        result = _parse_structured_response(raw_text)

        if result:
            # Attach token usage for the caller to merge
            result["_input_tokens"] = response.usage.input_tokens
            result["_output_tokens"] = response.usage.output_tokens
            return result

        logging.warning(f"NLQ format call: failed to parse JSON from formatting response (len={len(raw_text)})")
        return None

    except Exception as e:
        logging.error(f"NLQ format call error: {e}")
        return None


# ── Main chat function ────────────────────────────────────────────────────────


def chat(messages: list[dict], today: str | None = None) -> dict[str, Any]:
    """Run a conversational turn with tool-use support.

    Args:
        messages: Conversation history as list of {role, content} dicts.
        today: Today's date (YYYY-MM-DD). Defaults to UTC now.

    Returns:
        dict with:
          - success (bool)
          - response (str) — the assistant's markdown response
          - usage (dict) — cumulative token counts
          - error (str, optional)
    """
    try:
        client = _create_client()
    except ValueError:
        # don't echo client construction error text to the response.
        logging.exception("NLQ chat: failed to create Anthropic client")
        return {"success": False, "error": "NLQ service is not configured correctly"}

    model = _resolve_model("haiku")

    if not today:
        today = datetime.now(timezone.utc).strftime("%Y-%m-%d")

    system_prompt = SYSTEM_PROMPT.replace("{today}", today)

    # Track cumulative usage across tool-use iterations
    total_input_tokens = 0
    total_output_tokens = 0

    current_messages = list(messages)

    # resolve the caller's mandatory tenant-scope filter once; every ES
    # query the tool loop runs is AND-ed with it (server-side enforcement).
    scope_clause = _resolve_scope_filter()
    # resolve the data-view allowlist once; every model-chosen index is
    # validated against it before any ES call.
    allowed_indices = get_allowed_index_names()

    for iteration in range(MAX_TOOL_ITERATIONS):
        try:
            response = client.messages.create(
                model=model,
                max_tokens=config.NLQ_MAX_TOKENS,
                system=system_prompt,
                tools=TOOLS,
                messages=current_messages,
            )
        except anthropic.AuthenticationError:
            return {"success": False, "error": "Invalid Anthropic API key"}
        except anthropic.RateLimitError:
            return {"success": False, "error": "API rate limit exceeded — try again shortly"}
        except Exception:
            # keep the traceback in the server log; surface a
            # generic message to the client.
            logging.exception("NLQ chat: Anthropic API call failed")
            return {"success": False, "error": "API error — see server logs for detail"}

        total_input_tokens += response.usage.input_tokens
        total_output_tokens += response.usage.output_tokens

        # Check if Claude wants to use tools
        if response.stop_reason == "tool_use":
            # Add assistant's response (with tool_use blocks) to history
            current_messages.append({"role": "assistant", "content": response.content})

            # Execute each tool call and build tool results
            tool_results = []
            for block in response.content:
                if block.type == "tool_use":
                    logging.info(f"NLQ tool call: {block.name}({json.dumps(block.input)[:200]})")
                    result = _execute_tool(block.name, block.input, scope_clause, allowed_indices)
                    tool_results.append(
                        {
                            "type": "tool_result",
                            "tool_use_id": block.id,
                            "content": result,
                        }
                    )

            current_messages.append({"role": "user", "content": tool_results})
            continue

        # Claude is done — extract the text response
        text_parts = []
        for block in response.content:
            if hasattr(block, "text"):
                text_parts.append(block.text)

        usage = {
            "input_tokens": total_input_tokens,
            "output_tokens": total_output_tokens,
            "model": model,
            "tool_iterations": iteration + 1,
        }

        return {
            "success": True,
            "response": "\n".join(text_parts),
            "usage": usage,
        }

    # Hit max iterations
    return {
        "success": False,
        "error": "Reached maximum tool iterations. Try a simpler question.",
        "usage": {
            "input_tokens": total_input_tokens,
            "output_tokens": total_output_tokens,
            "model": model,
            "tool_iterations": MAX_TOOL_ITERATIONS,
        },
    }
