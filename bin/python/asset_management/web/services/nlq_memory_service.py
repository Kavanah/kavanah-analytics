"""
NLQ Memory Store Service — Learning loop for Ask Kavanah.

Stores every query execution as a memory record in Elasticsearch.
Retrieves similar past queries for few-shot prompt injection.
Powers suggested query chips and the memory badge count.
"""

import json
import logging
from datetime import datetime, timezone
from typing import Any

try:
    from lib.es_helpers import (
        get_elasticsearch_client,
        add_document,
        create_index_if_not_exists,
        put_regular_index_template,
    )
except ImportError:
    from lib.es_helpers import (
        get_elasticsearch_client,
        add_document,
        create_index_if_not_exists,
        put_regular_index_template,
    )


NLQ_MEMORY_INDEX = "nlq_memory_store"

NLQ_MEMORY_MAPPING = {
    "properties": {
        "@timestamp": {"type": "date"},
        "query_text": {"type": "text", "fields": {"keyword": {"type": "keyword"}}},
        "es_query": {"type": "object", "enabled": False},
        "result_summary": {"type": "text"},
        "intent_class": {"type": "keyword"},
        "user_rating": {"type": "integer"},
        "indices_used": {"type": "keyword"},
        "date_range": {"type": "keyword"},
        "user": {"type": "keyword"},
        # the team_titles in the writer's tenant scope at store time.
        # Reads filter on overlap with the caller's scope so one tenant's queries
        # never surface in another tenant's few-shot examples / suggestions.
        "tenant_teams": {"type": "keyword"},
        "chart_type": {"type": "keyword"},
        "stat_labels": {"type": "keyword"},
    }
}


def _tenant_filter(tenant_keys: list[str] | None) -> list[dict]:
    """Build the ES filter clause list that scopes a memory query to a tenant
    .

    Contract:
      * ``None``  -> no filter (unrestricted; back-compat for non-NLQ callers).
      * ``[...]`` -> ``terms`` on ``tenant_teams``; the caller sees a memory only
        if its stored tenant_teams overlaps these keys.
      * ``[]``    -> a ``terms`` against an empty list, which matches nothing —
        the fail-closed default a caller with no resolvable scope gets.
    """
    if tenant_keys is None:
        return []
    return [{"terms": {"tenant_teams": tenant_keys}}]


def ensure_memory_index() -> None:
    """Create the memory store index if it doesn't exist."""
    es = get_elasticsearch_client()
    if es is None:
        return
    try:
        put_regular_index_template(es, NLQ_MEMORY_INDEX, NLQ_MEMORY_MAPPING, priority=1000)
    except Exception as e:
        logging.warning(f"NLQ memory: could not set index template: {e}")
    create_index_if_not_exists(es, NLQ_MEMORY_INDEX, NLQ_MEMORY_MAPPING)


def store_query_memory(
    query_text: str,
    es_query: dict | None = None,
    result_summary: str = "",
    intent_class: str = "",
    chart_type: str = "",
    stat_labels: list[str] | None = None,
    indices_used: list[str] | None = None,
    date_range: str = "",
    user: str = "unknown",
    tenant_teams: list[str] | None = None,
) -> None:
    """Store a query execution as a memory record.

    Args:
        query_text: The original natural language question.
        es_query: The generated Elasticsearch query body.
        result_summary: First ~500 chars of the AI narrative.
        intent_class: Classified intent (e.g., "spend_comparison").
        chart_type: Chart type used (bar, line, doughnut).
        stat_labels: Labels from the 4 stat cards.
        indices_used: ES indices that were queried.
        date_range: The time period queried.
        user: Username who ran the query.
        tenant_teams: team_titles in the writer's tenant scope. A
            memory with no tenant_teams is unreachable by any tenant-scoped read.
    """
    doc: dict[str, Any] = {
        "@timestamp": datetime.now(timezone.utc).isoformat(),
        "query_text": query_text,
        "result_summary": result_summary[:500] if result_summary else "",
        "intent_class": intent_class,
        "user_rating": 0,
        "date_range": date_range,
        "user": user,
        "chart_type": chart_type,
        "stat_labels": stat_labels or [],
    }
    if es_query:
        doc["es_query"] = es_query
    if indices_used:
        doc["indices_used"] = indices_used
    if tenant_teams:
        doc["tenant_teams"] = tenant_teams

    try:
        add_document(NLQ_MEMORY_INDEX, doc)
    except Exception as e:
        logging.error(f"Failed to store NLQ memory: {e}")


def update_memory_rating(query_text: str, rating: int, tenant_keys: list[str] | None = None) -> None:
    """Update the user_rating for the most recent memory matching a query.

    Args:
        query_text: The query to find and rate.
        rating: 1 (helpful) or -1 (not helpful).
        tenant_keys: tenant scope of the caller; a caller may only
            rate a memory whose tenant_teams overlaps their scope.
    """
    es = get_elasticsearch_client()
    if es is None:
        return
    try:
        result = es.search(
            index=NLQ_MEMORY_INDEX,
            body={
                "size": 1,
                "query": {
                    "bool": {
                        "must": [{"match_phrase": {"query_text": query_text}}],
                        "filter": _tenant_filter(tenant_keys),
                    }
                },
                "sort": [{"@timestamp": "desc"}],
            },
            ignore_unavailable=True,
        )
        hits = result.get("hits", {}).get("hits", [])
        if hits:
            doc_id = hits[0]["_id"]
            es.update(
                index=NLQ_MEMORY_INDEX,
                id=doc_id,
                body={"doc": {"user_rating": rating}},
            )
    except Exception as e:
        logging.error(f"Failed to update NLQ memory rating: {e}")


def find_similar_queries(query_text: str, n: int = 5, tenant_keys: list[str] | None = None) -> list[dict[str, Any]]:
    """Find the N most similar past queries for few-shot injection.

    Uses Elasticsearch text matching (more_like_this + match) to find
    semantically similar queries. Excludes negatively-rated queries.

    Args:
        query_text: The new question to find similar queries for.
        n: Number of results to return.
        tenant_keys: tenant scope of the caller. Only memories whose
            tenant_teams overlaps these keys are eligible as few-shot examples;
            ``[]`` (no resolvable scope) matches nothing.

    Returns:
        List of dicts with: query_text, es_query, result_summary, chart_type.
    """
    es = get_elasticsearch_client()
    if es is None:
        return []

    try:
        result = es.search(
            index=NLQ_MEMORY_INDEX,
            body={
                "size": n,
                "query": {
                    "bool": {
                        "must": [
                            {
                                "match": {
                                    "query_text": {
                                        "query": query_text,
                                        "fuzziness": "AUTO",
                                    }
                                }
                            }
                        ],
                        "filter": _tenant_filter(tenant_keys),
                        "must_not": [
                            {"term": {"user_rating": -1}},
                        ],
                        "should": [
                            {"term": {"user_rating": 1}},
                        ],
                    }
                },
                "sort": [{"_score": "desc"}, {"@timestamp": "desc"}],
                "_source": [
                    "query_text",
                    "es_query",
                    "result_summary",
                    "chart_type",
                    "intent_class",
                ],
            },
            ignore_unavailable=True,
        )

        hits = result.get("hits", {}).get("hits", [])
        return [
            {
                "query_text": h["_source"].get("query_text", ""),
                "es_query": h["_source"].get("es_query"),
                "result_summary": h["_source"].get("result_summary", ""),
                "chart_type": h["_source"].get("chart_type", ""),
                "intent_class": h["_source"].get("intent_class", ""),
            }
            for h in hits
        ]

    except Exception as e:
        logging.error(f"NLQ memory: similarity search failed: {e}")
        return []


def get_suggested_queries(n: int = 5, tenant_keys: list[str] | None = None) -> list[str]:
    """Return popular/recent query suggestions from the memory store.

    Prioritizes positively-rated queries, then falls back to recent unique queries.

    Args:
        n: Number of suggestions to return.
        tenant_keys: tenant scope of the caller. Suggestions are drawn
            only from memories whose tenant_teams overlaps these keys; ``[]``
            (no resolvable scope) yields no suggestions.

    Returns:
        List of query text strings.
    """
    es = get_elasticsearch_client()
    if es is None:
        return []

    try:
        # Get top-rated and recent unique queries
        result = es.search(
            index=NLQ_MEMORY_INDEX,
            body={
                "size": 0,
                "query": {"bool": {"filter": _tenant_filter(tenant_keys)}},
                "aggs": {
                    "unique_queries": {
                        "terms": {
                            "field": "query_text.keyword",
                            "size": n * 3,
                            "order": {"top_score": "desc"},
                        },
                        "aggs": {
                            "top_score": {"max": {"field": "user_rating"}},
                            "latest": {"max": {"field": "@timestamp"}},
                        },
                    }
                },
            },
            ignore_unavailable=True,
        )

        buckets = result.get("aggregations", {}).get("unique_queries", {}).get("buckets", [])

        # Sort: positively-rated first, then by recency
        suggestions = []
        for b in buckets:
            suggestions.append(
                {
                    "query": b["key"],
                    "rating": b.get("top_score", {}).get("value", 0) or 0,
                    "latest": b.get("latest", {}).get("value_as_string", ""),
                }
            )

        # Positively rated first, then by recency
        suggestions.sort(key=lambda s: (-s["rating"], s["latest"]), reverse=False)
        suggestions.sort(key=lambda s: -s["rating"])

        return [s["query"] for s in suggestions[:n]]

    except Exception as e:
        logging.error(f"NLQ memory: suggested queries failed: {e}")
        return []


def get_memory_count(tenant_keys: list[str] | None = None) -> int:
    """Return the number of queries stored in the memory store, scoped to the
    caller's tenant. ``[]`` (no resolvable scope) counts nothing."""
    es = get_elasticsearch_client()
    if es is None:
        return 0
    try:
        body = {"query": {"bool": {"filter": _tenant_filter(tenant_keys)}}}
        result = es.count(index=NLQ_MEMORY_INDEX, body=body, ignore_unavailable=True)
        return result.get("count", 0)
    except Exception:
        return 0
