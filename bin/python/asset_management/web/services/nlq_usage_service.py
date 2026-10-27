"""
NLQ Usage Tracking Service
Logs every NLQ query to Elasticsearch for cost monitoring and auditing.
"""

import logging
from datetime import datetime, timezone
from typing import Any

try:
    from lib.es_helpers import get_elasticsearch_client, add_document, create_index_if_not_exists
except ImportError:
    from lib.es_helpers import get_elasticsearch_client, add_document, create_index_if_not_exists


NLQ_USAGE_INDEX = "nlq_usage_log"

# Pricing per million tokens, USD (Anthropic list prices, re-checked
# 2026-10-22 for update if pricing changes). Keys are the exact
# ``model`` strings the two AI features send on EITHER provider: the direct
# Anthropic API uses bare ids (``claude-haiku-5-5``), Bedrock uses inference
# profiles (``us.anthropic.…``). A model missing here is charged
# _DEFAULT_PRICING, which undercounts Opus about five times and starves the
# daily caps — PRICING-002 pins every id the features can resolve.
#
# ``long_input`` / ``long_output`` (Haiku 5.5 only): the rate card for a
# prompt longer than _LONG_PROMPT_TOKENS.
_LONG_PROMPT_TOKENS = 100_000
# prompt caching (Anthropic pricing page, 2026-10-22): a 5-minute
# cache write costs 1.25x the input rate; a cache read 0.1x, except Opus 5.5
# and Sonnet 5.5 at 0.05x (a pricing row may carry "cache_read_multiplier").
_CACHE_WRITE_MULTIPLIER = 1.25
_CACHE_READ_MULTIPLIER = 0.10
_PRICING = {
    # ── Anthropic direct API ─────────────────────────────────────────────
    # current generation: NLQ haiku/sonnet routes, canvas tiers.
    "claude-haiku-5-5": {"input": 0.10, "output": 0.50, "long_input": 0.50, "long_output": 2.50},
    "claude-sonnet-5-5": {"input": 2.00, "output": 10.00, "cache_read_multiplier": 0.05},
    "claude-opus-5-5": {"input": 4.00, "output": 20.00, "cache_read_multiplier": 0.05},
    # Previous generation, still valid direct-API ids (master-config overrides).
    "claude-sonnet-5": {"input": 2.00, "output": 10.00},
    "claude-opus-4-8": {"input": 5.00, "output": 25.00},
    # Haiku 4.5 is $1 / $5 (the old $0.80 / $4 rows were Haiku 3.5's).
    "claude-haiku-4-5": {"input": 1.00, "output": 5.00},
    "claude-haiku-4-5-20251001": {"input": 1.00, "output": 5.00},
    "claude-sonnet-4-20250514": {"input": 3.00, "output": 15.00},
    "claude-sonnet-4-5-20250514": {"input": 3.00, "output": 15.00},
    "claude-sonnet-4-6-20250627": {"input": 3.00, "output": 15.00},
    # real direct-API alias (no date suffix)
    "claude-sonnet-4-6": {"input": 3.00, "output": 15.00},
    # ── AWS Bedrock (same on-demand pricing) ─────────────────────────────
    "us.anthropic.claude-haiku-4-5-20251001-v1:0": {"input": 1.00, "output": 5.00},
    "us.anthropic.claude-haiku-4-5-v1-20251001": {"input": 1.00, "output": 5.00},
    "us.anthropic.claude-sonnet-4-20250514-v1:0": {"input": 3.00, "output": 15.00},
    "us.anthropic.claude-sonnet-4-5-v2-20250514": {"input": 3.00, "output": 15.00},
    "us.anthropic.claude-sonnet-4-6-v1-20250627": {"input": 3.00, "output": 15.00},
    # real Sonnet 4.6 inference-profile IDs (global. is 10% cheaper
    # per AWS global cross-region routing pricing on Sonnet 4.5+)
    "us.anthropic.claude-sonnet-4-6": {"input": 3.00, "output": 15.00},
    "global.anthropic.claude-sonnet-4-6": {"input": 2.70, "output": 13.50},
    # Canvas AI tier map — Haiku 4.5 / Sonnet 5 / Opus 4.8 via
    # inference profiles.
    "us.anthropic.claude-sonnet-4-5-20250929-v1:0": {"input": 3.00, "output": 15.00},
    "us.anthropic.claude-opus-4-5-20251101-v1:0": {"input": 5.00, "output": 25.00},
    "global.anthropic.claude-haiku-4-5": {"input": 1.00, "output": 5.00},
    # (superseded): the bare us. Haiku alias is NOT an active Bedrock
    # inference profile in us-west-2 (it 400s "provided model identifier is
    # invalid"), unlike the bare Sonnet 5 / Opus 4.8 aliases which DO resolve.
    # The fast-tier default is the versioned profile above. Kept here only so
    # any already-logged rows carrying the bare id still price correctly.
    "us.anthropic.claude-haiku-4-5": {"input": 1.00, "output": 5.00},
    "us.anthropic.claude-sonnet-5": {"input": 3.00, "output": 15.00},
    "global.anthropic.claude-sonnet-5": {"input": 2.70, "output": 13.50},
    "us.anthropic.claude-opus-4-8": {"input": 5.00, "output": 25.00},
}
_DEFAULT_PRICING = {"input": 1.00, "output": 5.00}

# once the account's API credit is spent the direct API refuses
# every call with HTTP 400 invalid_request_error ("Your credit balance is too
# low to access the Anthropic API …"). Running out is a deliberate hard stop
# until the credit renews, so this is an expected state, not a bug — the
# services say so instead of "API error — see server logs".
CREDIT_EXHAUSTED_ERROR = "The monthly AI credit is used up — AI features return when it renews."


def is_credit_exhausted(err: Exception) -> bool:
    """True when a provider error is the Anthropic credit-balance refusal."""
    return "credit balance" in str(err).lower()


# models charged at the default rate are logged once per process.
_unpriced_models_seen: set[str] = set()

NLQ_USAGE_MAPPING = {
    "properties": {
        "@timestamp": {"type": "date"},
        "user": {"type": "keyword"},
        "query_text": {"type": "text"},
        "data_view": {"type": "keyword"},
        # which AI surface spent the tokens: "nlq" (Data
        # Explorer, the default for all pre-existing docs) or "canvas_ai"
        # (canvas-editor chat). Budget checks aggregate on it.
        "feature": {"type": "keyword"},
        # the workspace that BILLS the user who spent the tokens
        # (WorkspaceBillingService.resolve_workspace_esid at log time).
        # Powers the per-workspace spend readout. Rows logged before the
        # field existed (or when resolution fails) have no workspace and are
        # invisible to workspace sums — the counter starts at rollout.
        "workspace_esid": {"type": "keyword"},
        "model": {"type": "keyword"},
        "input_tokens": {"type": "integer"},
        "output_tokens": {"type": "integer"},
        # prompt-cache activity per logged call (0 before caching shipped).
        "cache_read_input_tokens": {"type": "integer"},
        "cache_creation_input_tokens": {"type": "integer"},
        "total_tokens": {"type": "integer"},
        "estimated_cost_usd": {"type": "float"},
        "success": {"type": "boolean"},
        "error": {"type": "keyword"},
        "user_rating": {"type": "integer"},  # 1 = helpful, -1 = not helpful, 0 = unrated
    }
}


def ensure_usage_index() -> None:
    """Create the NLQ usage index if it doesn't exist.

    Uses put_regular_index_template to prevent data-stream-only templates
    from intercepting the index creation.
    """
    es = get_elasticsearch_client()
    if es is None:
        return
    try:
        from lib.es_helpers import put_regular_index_template

        put_regular_index_template(es, NLQ_USAGE_INDEX, NLQ_USAGE_MAPPING, priority=1000)
    except Exception as e:
        logging.warning(f"NLQ: could not set index template: {e}")
    create_index_if_not_exists(es, NLQ_USAGE_INDEX, NLQ_USAGE_MAPPING)
    # the index predates the ``feature`` field on staging/prod;
    # without an explicit put_mapping it would dynamic-map as text and the
    # budget check's term filter would run against an analyzed field.
    # put_mapping is additive/idempotent, so this is safe on every call.
    try:
        es.indices.put_mapping(index=NLQ_USAGE_INDEX, properties=NLQ_USAGE_MAPPING["properties"])
    except Exception as e:
        logging.warning(f"NLQ: could not update usage-index mapping: {e}")


def _estimate_cost(
    model: str,
    input_tokens: int,
    output_tokens: int,
    cache_read_tokens: int = 0,
    cache_write_tokens: int = 0,
) -> float:
    """Calculate estimated USD cost for a query.

    ``input_tokens`` is the UNCACHED remainder (what the API reports);
    cache writes bill at 1.25x and cache reads at 0.1x the input rate. The
    rate card is chosen on the whole prompt (uncached + cached tokens).

    a model with no pricing row is charged _DEFAULT_PRICING and
    logged once, because the fallback silently under-meters the daily caps.
    A model with a long-prompt card (Haiku 5.5) moves to it when
    ``input_tokens`` exceeds _LONG_PROMPT_TOKENS. The card is chosen per
    REQUEST, so callers must price each request's own tokens (the NLQ tool
    loop does, and passes the total through ``usage["estimated_cost_usd"]``)
    rather than a summed loop total.
    """
    pricing = _PRICING.get(model)
    if pricing is None:
        if model not in _unpriced_models_seen:
            _unpriced_models_seen.add(model)
            logging.warning("NLQ usage: model %r has no pricing row; charging the default rate", model)
        pricing = _DEFAULT_PRICING
    in_rate, out_rate = pricing["input"], pricing["output"]
    prompt_tokens = input_tokens + cache_read_tokens + cache_write_tokens
    if prompt_tokens > _LONG_PROMPT_TOKENS and "long_input" in pricing:
        in_rate, out_rate = pricing["long_input"], pricing["long_output"]
    cost = (
        input_tokens * in_rate
        + cache_write_tokens * in_rate * _CACHE_WRITE_MULTIPLIER
        + cache_read_tokens * in_rate * pricing.get("cache_read_multiplier", _CACHE_READ_MULTIPLIER)
        + output_tokens * out_rate
    ) / 1_000_000
    return round(cost, 6)


# fix (2026-07-28, staging): the mapping guarantee must live with
# the WRITER, not one caller. Only the canvas-chat route called
# ensure_usage_index before logging; the Data Explorer call sites never
# did, so the first stamped write after rollout dynamic-mapped
# workspace_esid as text — and the later ensure's additive put_mapping
# then failed ATOMICALLY on the text-vs-keyword conflict, silently
# breaking the spend readout's term filter. Once per process, before the
# first write. The flag sets even on failure (ensure_usage_index swallows
# ES errors) so a down cluster isn't re-probed on every request — the
# next process gets a fresh attempt, same posture as the route-level flag.
_usage_index_ready = False


def _ensure_usage_index_once() -> None:
    global _usage_index_ready
    if _usage_index_ready:
        return
    ensure_usage_index()
    _usage_index_ready = True


def resolve_billing_workspace(user: str) -> str | None:
    """Best-effort: the workspace_esid that bills ``user``.

    Thin wrapper over ``WorkspaceBillingService.resolve_ai_workspace_esid``
    (lazy import — that service transitively pulls in the authz stack) that
    swallows every failure. Usage logging must never break on workspace
    resolution: a row without ``workspace_esid`` still meters the daily
    budget caps, it just doesn't count toward the workspace readout.

    Uses the AI-attribution resolver, not the Stripe billing one, so a
    logged row lands on the same workspace the gate admitted the user
    through and the readout sums over. See resolve_ai_workspace_esid.
    """
    if not user:
        return None
    try:
        from asset_management.web.services.workspace_billing_service import (
            WorkspaceBillingService,
        )

        return WorkspaceBillingService.resolve_ai_workspace_esid(user)
    except Exception as e:  # noqa: BLE001 — logging must not break the request
        logging.warning("NLQ: workspace resolution failed for %s: %s", user, e)
        return None


def log_query(
    user: str,
    query_text: str,
    data_view: str,
    usage: dict[str, Any],
    success: bool,
    error: str | None = None,
    feature: str = "nlq",
    workspace_esid: str | None = None,
) -> None:
    """Log an NLQ query to the usage index.

    Args:
        user: Username of the person who ran the query.
        query_text: The natural language query string.
        data_view: Which data view was queried.
        usage: Dict with 'input_tokens', 'output_tokens', 'model' from the API response.
        success: Whether the query succeeded.
        error: Error message if the query failed.
        feature: Which AI surface spent the tokens — "nlq"
            (default, Data Explorer) or "canvas_ai" (canvas-editor chat).
        workspace_esid: The billing workspace to attribute the spend to
            . Callers that already resolved it pass it through;
            when None it is resolved best-effort from ``user`` so every
            call site stamps the field without repeating the lookup code.
    """
    _ensure_usage_index_once()
    input_tokens = usage.get("input_tokens", 0)
    output_tokens = usage.get("output_tokens", 0)
    model = usage.get("model", "unknown")
    # a caller that priced each request as it ran (the NLQ tool
    # loop) passes the total. Pricing the summed tokens here would put a
    # multi-step query on Haiku 5.5's long-prompt card that no single
    # request reached (seen 2026-10-22: $0.07 logged, $0.01 billed).
    cache_read_tokens = usage.get("cache_read_input_tokens", 0) or 0
    cache_write_tokens = usage.get("cache_creation_input_tokens", 0) or 0
    cost = usage.get("estimated_cost_usd")
    if cost is None:
        cost = _estimate_cost(model, input_tokens, output_tokens, cache_read_tokens, cache_write_tokens)

    doc = {
        "@timestamp": datetime.now(timezone.utc).isoformat(),
        "user": user,
        "query_text": query_text,
        "data_view": data_view,
        "feature": feature,
        "model": model,
        "input_tokens": input_tokens,
        "output_tokens": output_tokens,
        "cache_read_input_tokens": cache_read_tokens,
        "cache_creation_input_tokens": cache_write_tokens,
        "total_tokens": input_tokens + output_tokens,
        "estimated_cost_usd": cost,
        "success": success,
    }
    if error:
        doc["error"] = error
    workspace_esid = workspace_esid or resolve_billing_workspace(user)
    if workspace_esid:
        doc["workspace_esid"] = workspace_esid

    try:
        add_document(NLQ_USAGE_INDEX, doc)
    except Exception as e:
        logging.error(f"Failed to log NLQ usage: {e}")


def get_feature_spend_today(feature: str, user: str | None = None) -> float:
    """Sum today's estimated USD spend for an AI feature.

    ``user`` narrows the sum to one username; None sums the whole feature.
    Fails closed on ES errors: returns ``float("inf")`` so a broken usage
    index refuses further spend instead of allowing unmetered calls.
    """
    es = get_elasticsearch_client()
    if es is None:
        return float("inf")

    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    filters: list[dict[str, Any]] = [
        {"term": {"feature": feature}},
        {"range": {"@timestamp": {"gte": today}}},
    ]
    if user:
        filters.append({"term": {"user": user}})

    try:
        result = es.search(
            index=NLQ_USAGE_INDEX,
            body={
                "size": 0,
                "query": {"bool": {"filter": filters}},
                "aggs": {"cost": {"sum": {"field": "estimated_cost_usd"}}},
            },
            ignore_unavailable=True,
        )
    except Exception:
        logging.exception("Failed to fetch %s spend for budget check; failing closed", feature)
        return float("inf")

    return float(result.get("aggregations", {}).get("cost", {}).get("value", 0) or 0)


def get_workspace_spend(
    workspace_esid: str,
    since_iso: str,
    until_iso: str | None = None,
    feature: str | None = None,
) -> float | None:
    """Sum a workspace's estimated USD AI spend over a window.

    ``feature`` narrows to one AI surface; None sums every surface.
    ``until_iso`` is exclusive; None means "until now".

    Fails OPEN on ES errors: returns ``None`` so the caller renders a blank
    readout. This is deliberately the opposite posture from
    ``get_feature_spend_today`` (a budget gate, which fails closed with
    ``inf``) — an ``inf`` here would DISPLAY an infinite bill on a transient
    ES hiccup instead of refusing spend.
    """
    if not workspace_esid or not since_iso:
        return None
    es = get_elasticsearch_client()
    if es is None:
        return None

    ts_range: dict[str, Any] = {"gte": since_iso}
    if until_iso:
        ts_range["lt"] = until_iso
    filters: list[dict[str, Any]] = [
        {"term": {"workspace_esid": workspace_esid}},
        {"range": {"@timestamp": ts_range}},
    ]
    if feature:
        filters.append({"term": {"feature": feature}})

    try:
        result = es.search(
            index=NLQ_USAGE_INDEX,
            body={
                "size": 0,
                "query": {"bool": {"filter": filters}},
                "aggs": {"cost": {"sum": {"field": "estimated_cost_usd"}}},
            },
            ignore_unavailable=True,
        )
    except Exception:
        logging.exception("Failed to fetch workspace AI spend for %s; blanking readout", workspace_esid)
        return None

    return float(result.get("aggregations", {}).get("cost", {}).get("value", 0) or 0)


def log_feedback(
    user: str,
    query_text: str,
    rating: int,
) -> None:
    """Log user feedback (thumbs up/down) for a query result.

    Args:
        user: Username of the person who rated.
        query_text: The query that was rated.
        rating: 1 for helpful, -1 for not helpful.
    """
    doc = {
        "@timestamp": datetime.now(timezone.utc).isoformat(),
        "user": user,
        "query_text": query_text,
        "data_view": "feedback",
        "model": "",
        "input_tokens": 0,
        "output_tokens": 0,
        "total_tokens": 0,
        "estimated_cost_usd": 0.0,
        "success": True,
        "user_rating": rating,
    }
    try:
        add_document(NLQ_USAGE_INDEX, doc)
    except Exception as e:
        logging.error(f"Failed to log NLQ feedback: {e}")


def get_usage_stats(user: str | None = None) -> dict[str, Any]:
    """Return aggregate usage stats for the NLQ feature.

    ``user`` narrows every figure to that username's own rows (:
    non-superusers must not see other tenants' usernames or spend). None
    returns the platform-wide view.

    Returns:
        dict with: total_queries, total_tokens, total_cost_usd,
        queries_today, cost_today_usd, by_user (list), by_model (list)
    """
    es = get_elasticsearch_client()
    if es is None:
        return {"error": "Elasticsearch unavailable"}

    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    # ``is not None``: an empty username must match nothing, not everyone.
    query = {"term": {"user": user}} if user is not None else {"match_all": {}}

    try:
        result = es.search(
            index=NLQ_USAGE_INDEX,
            body={
                "size": 0,
                "query": query,
                "aggs": {
                    "total_tokens": {"sum": {"field": "total_tokens"}},
                    "total_cost": {"sum": {"field": "estimated_cost_usd"}},
                    "total_cache_read_tokens": {"sum": {"field": "cache_read_input_tokens"}},
                    "total_queries": {"value_count": {"field": "@timestamp"}},
                    "today": {
                        "filter": {"range": {"@timestamp": {"gte": today}}},
                        "aggs": {
                            "queries": {"value_count": {"field": "@timestamp"}},
                            "cost": {"sum": {"field": "estimated_cost_usd"}},
                            "tokens": {"sum": {"field": "total_tokens"}},
                        },
                    },
                    "by_user": {
                        "terms": {"field": "user", "size": 20},
                        "aggs": {
                            "cost": {"sum": {"field": "estimated_cost_usd"}},
                            "tokens": {"sum": {"field": "total_tokens"}},
                        },
                    },
                    "by_model": {
                        "terms": {"field": "model", "size": 10},
                        "aggs": {
                            "cost": {"sum": {"field": "estimated_cost_usd"}},
                            "tokens": {"sum": {"field": "total_tokens"}},
                        },
                    },
                },
            },
            ignore_unavailable=True,
        )
    except Exception:
        # don't echo ES exception text to the response body.
        logging.exception("Failed to fetch NLQ usage stats")
        return {"error": "Failed to fetch NLQ usage stats"}

    aggs = result.get("aggregations", {})
    today_aggs = aggs.get("today", {})

    return {
        "total_queries": int(aggs.get("total_queries", {}).get("value", 0)),
        "total_tokens": int(aggs.get("total_tokens", {}).get("value", 0)),
        "total_cost_usd": round(aggs.get("total_cost", {}).get("value", 0), 4),
        # tokens served from the prompt cache (billed at 0.1x).
        "total_cache_read_tokens": int(aggs.get("total_cache_read_tokens", {}).get("value", 0) or 0),
        "queries_today": int(today_aggs.get("queries", {}).get("value", 0)),
        "cost_today_usd": round(today_aggs.get("cost", {}).get("value", 0), 6),
        "tokens_today": int(today_aggs.get("tokens", {}).get("value", 0)),
        "by_user": [
            {
                "user": b["key"],
                "queries": b["doc_count"],
                "cost_usd": round(b["cost"]["value"], 4),
                "tokens": int(b["tokens"]["value"]),
            }
            for b in aggs.get("by_user", {}).get("buckets", [])
        ],
        "by_model": [
            {
                "model": b["key"],
                "queries": b["doc_count"],
                "cost_usd": round(b["cost"]["value"], 4),
                "tokens": int(b["tokens"]["value"]),
            }
            for b in aggs.get("by_model", {}).get("buckets", [])
        ],
    }
