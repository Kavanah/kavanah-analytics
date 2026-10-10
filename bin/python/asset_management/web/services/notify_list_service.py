"""Connector Notify-List Service.

Captures user interest in roadmap connectors during onboarding step-4.
Marketing queries the list to gauge demand; the send-hook (``bin/
python/tools/connector_notify_list.py``) emails subscribers when a
connector ships.

Two tile classes feed this index:

* ``placeholder=True`` tiles — Google Ads / GA4 today. The
  OAuth wizard is half-built; clicking the tile surfaces the inline
  "OAuth wizard pending" banner with a Notify-me-when-ready button.
* ``coming_soon=True`` tiles — Snapchat / Pinterest / Reddit / etc.
  . Roadmap connectors that don't have an OAuth wizard at
  all. makes these clickable (previously dead-greyed) so they
  also route into the banner.

Both paths write the same doc shape — ``source`` differentiates them
so marketing can slice the report by which UX a subscriber came from.

Doc shape::

    {
        "email":          "jane@acme.com",        # session email at click time
        "username": "jane@acme.com", # ES Security login name
        "connector_id":   "snapchat_ads",         # ConnectorCatalogService id
        "connector_name": "Snapchat Ads",         # denormalised for the marketing export
        "source":         "coming_soon",          # "coming_soon" | "placeholder"
        "@timestamp":     "2026-05-14T13:14:15Z", # first-time interest captured
        "notified_at":    null,                   # set by the send-hook; null until sent
        "ip_address":     "203.0.113.4",
        "user_agent":     "Mozilla/5.0 ...",
    }

Doc id is deterministic — ``{username}:{connector_id}`` — so a user
clicking the same tile twice updates the existing record instead of
piling up duplicates. Marketing exports aggregate by ``connector_id``
+ ``notified_at IS NULL`` to find unsent subscribers.

Storage policy: this index is included in the daily ES snapshot
because the captured emails are user-supplied signal that marketing
relies on for launch comms — losing it on a cluster rebuild would
be a UX regression and a marketing-promise breach. Distinct from
``.kibana*`` which is intentionally excluded (saved objects are
rebuildable from git per CLAUDE.md).
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone

INDEX = "de_connector_notify_list"

# Allowed values for the ``source`` keyword. Pinned at the service
# boundary so a future caller can't silently grow the enum and break
# marketing's slice queries. ships ``placeholder``;
# adds ``coming_soon``. A third source (e.g. dashboard checklist
# notify-me) would need a new constant here.
SOURCE_PLACEHOLDER = "placeholder"
SOURCE_COMING_SOON = "coming_soon"
_VALID_SOURCES = frozenset({SOURCE_PLACEHOLDER, SOURCE_COMING_SOON})

INDEX_MAPPING = {
    "properties": {
        "@timestamp": {"type": "date"},
        "email": {"type": "keyword"},
        "username": {"type": "keyword"},
        "connector_id": {"type": "keyword"},
        "connector_name": {"type": "keyword"},
        "source": {"type": "keyword"},
        "notified_at": {"type": "date"},
        "ip_address": {"type": "ip"},
        "user_agent": {
            "type": "text",
            "fields": {"keyword": {"type": "keyword", "ignore_above": 512}},
        },
    }
}


def _doc_id(username: str, connector_id: str) -> str:
    """Deterministic id so re-clicks update rather than duplicate.

    Uses ``username`` (the ES Security login name, also the wizard's
    session key) rather than email so a user who later updates their
    email address on their ES Security record still maps to the same
    subscription record. The connector_id segment lets a single user
    subscribe to multiple connectors.
    """
    return f"{username}:{connector_id}"


class NotifyListService:
    """Read/write helpers for the connector notify-list."""

    # ------------------------------------------------------------------
    # Index bootstrap
    # ------------------------------------------------------------------

    @staticmethod
    def ensure_index_exists() -> None:
        """Create the notify-list index if it does not already exist.

        Same shape as AuditLogService — high-priority regular-index
        template + create_index_if_not_exists + idempotent put_mapping
        so a new field can be added on a future deploy without
        recreating the index.
        """
        from lib.es_helpers import (
            get_elasticsearch_client,
            put_regular_index_template,
            create_index_if_not_exists,
        )

        es = get_elasticsearch_client()
        if es is None:
            logging.warning("NotifyListService: could not connect to ES")
            return

        put_regular_index_template(es, INDEX, INDEX_MAPPING, priority=500)
        create_index_if_not_exists(es, INDEX, INDEX_MAPPING)
        try:
            es.indices.put_mapping(index=INDEX, body=INDEX_MAPPING)
        except Exception as e:
            logging.warning("NotifyListService: put_mapping update: %s", e)

    # ------------------------------------------------------------------
    # Write
    # ------------------------------------------------------------------

    @staticmethod
    def record_interest(
        *,
        username: str,
        email: str,
        connector_id: str,
        connector_name: str,
        source: str,
        ip_address: str = "",
        user_agent: str = "",
    ) -> bool:
        """Record one notify-me click. Idempotent on (username, connector_id).

        Returns True on success, False on any failure (ES outage,
        validation, write error). Fire-and-forget at the call site —
        the caller logs the audit event regardless so a failed ES
        write still leaves an observable trail in ``de_audit_log``.

        Re-clicks update the same doc (preserve original ``@timestamp``
        when present so marketing can slice by first-interest date, not
        last-click date — that's what they actually want to know).
        """
        if source not in _VALID_SOURCES:
            logging.warning("NotifyListService.record_interest: invalid source %r", source)
            return False
        if not username or not connector_id:
            return False

        from lib.es_helpers import get_elasticsearch_client

        es = get_elasticsearch_client()
        if es is None:
            return False

        doc_id = _doc_id(username, connector_id)
        # Preserve the original first-click timestamp on re-clicks so
        # marketing's "demand over time" chart isn't skewed by users
        # who tap the button twice.
        existing_timestamp: str | None = None
        try:
            existing = es.get(index=INDEX, id=doc_id, ignore=[404])
            if existing.get("found"):
                existing_timestamp = (existing.get("_source") or {}).get("@timestamp")
        except Exception:  # noqa: BLE001 — top-level boundary; ES outage path
            existing_timestamp = None

        now = datetime.now(timezone.utc).isoformat()
        doc: dict = {
            "@timestamp": existing_timestamp or now,
            "email": email or "",
            "username": username,
            "connector_id": connector_id,
            "connector_name": connector_name or connector_id,
            "source": source,
            # ``notified_at`` is intentionally absent on the first write
            # — the send-hook stamps it on successful email dispatch.
            # An ES ``null`` would also work but absent is cheaper to
            # filter on (``must_not exists``).
        }
        if ip_address:
            doc["ip_address"] = ip_address
        if user_agent:
            doc["user_agent"] = user_agent

        try:
            es.index(index=INDEX, id=doc_id, document=doc)
            return True
        except Exception as e:  # noqa: BLE001 — top-level boundary
            logging.error("NotifyListService: failed to record interest: %s", e)
            return False

    # ------------------------------------------------------------------
    # Read — used by the admin CLI and marketing exports
    # ------------------------------------------------------------------

    @staticmethod
    def list_subscribers(
        connector_id: str,
        *,
        unsent_only: bool = True,
        size: int = 10000,
    ) -> list[dict]:
        """Return notify-list rows for one connector.

        Default ``unsent_only=True`` is the send-hook's mode — only
        rows that haven't been emailed yet. Marketing exports pass
        ``unsent_only=False`` to see the full demand curve.
        """
        from lib.es_helpers import get_elasticsearch_client

        es = get_elasticsearch_client()
        if es is None:
            return []

        filters: list[dict] = [{"term": {"connector_id": connector_id}}]
        if unsent_only:
            filters.append({"bool": {"must_not": [{"exists": {"field": "notified_at"}}]}})

        try:
            result = es.search(
                index=INDEX,
                body={
                    "query": {"bool": {"filter": filters}},
                    "sort": [{"@timestamp": {"order": "asc"}}],
                    "size": size,
                },
            )
            rows: list[dict] = []
            for hit in result.get("hits", {}).get("hits", []):
                src = dict(hit.get("_source") or {})
                src["_id"] = hit.get("_id")
                rows.append(src)
            return rows
        except Exception as e:  # noqa: BLE001 — top-level boundary
            logging.error("NotifyListService: list_subscribers failed: %s", e)
            return []

    @staticmethod
    def mark_notified(doc_id: str, *, notified_at: str | None = None) -> bool:
        """Stamp a subscriber's ``notified_at`` after a successful send.

        Called by the admin CLI per-recipient so a partial send (e.g.
        SMTP croaks halfway through) leaves the remaining rows
        marked-unsent and resumable on the next run.
        """
        from lib.es_helpers import get_elasticsearch_client

        es = get_elasticsearch_client()
        if es is None:
            return False
        stamp = notified_at or datetime.now(timezone.utc).isoformat()
        try:
            es.update(
                index=INDEX,
                id=doc_id,
                body={"doc": {"notified_at": stamp}},
            )
            return True
        except Exception as e:  # noqa: BLE001 — top-level boundary
            logging.error("NotifyListService: mark_notified failed for %s: %s", doc_id, e)
            return False

    @staticmethod
    def counts_by_connector() -> list[dict]:
        """Return per-connector demand counts (for marketing exports).

        Each row: ``{connector_id, connector_name, total, unsent}``.
        Empty list on ES outage so the admin CLI's ``list`` command
        prints a clean empty result rather than crashing.
        """
        from lib.es_helpers import get_elasticsearch_client

        es = get_elasticsearch_client()
        if es is None:
            return []
        try:
            result = es.search(
                index=INDEX,
                body={
                    "size": 0,
                    "aggs": {
                        "per_connector": {
                            "terms": {"field": "connector_id", "size": 100},
                            "aggs": {
                                "name_sample": {"top_hits": {"size": 1, "_source": ["connector_name"]}},
                                "unsent": {
                                    "filter": {
                                        "bool": {
                                            "must_not": [{"exists": {"field": "notified_at"}}],
                                        }
                                    }
                                },
                            },
                        }
                    },
                },
            )
        except Exception as e:  # noqa: BLE001 — top-level boundary
            logging.error("NotifyListService: counts_by_connector failed: %s", e)
            return []

        buckets = result.get("aggregations", {}).get("per_connector", {}).get("buckets", [])
        rows: list[dict] = []
        for b in buckets:
            sample_hits = b.get("name_sample", {}).get("hits", {}).get("hits", [])
            sample_src = (sample_hits[0].get("_source") if sample_hits else {}) or {}
            rows.append(
                {
                    "connector_id": b.get("key", ""),
                    "connector_name": sample_src.get("connector_name") or b.get("key", ""),
                    "total": b.get("doc_count", 0),
                    "unsent": b.get("unsent", {}).get("doc_count", 0),
                }
            )
        return rows
