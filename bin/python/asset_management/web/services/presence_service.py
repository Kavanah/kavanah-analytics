"""
User Presence Service

Records a throttled per-request activity heartbeat in Elasticsearch so
"who is logged in right now" is queryable. Backed by the
``de_user_presence`` index; doc ID is the ``username``, so each user has
exactly one always-current row — the write is an upsert, not an append.
There is intentionally no presence history.

Why this exists: the User Management "active now" count used to be derived
only from ``login``/``logout`` audit events (``AuditLogService.active_usernames``).
Per-request activity is never written to ES — ``session["last_activity"]``
lives only in Flask's signed-cookie session — so the login-age proxy both
under- and over-counted. ``last_seen`` here is a *real* activity timestamp,
refreshed at most once per throttle interval while the user actually clicks,
so ``present_usernames`` answers the question accurately.

Storage decision recorded in ``the private docs``
(entry: ``2026-06-17 — ``).
"""

import logging
from datetime import datetime, timezone

INDEX = "de_user_presence"

INDEX_MAPPING = {
    "properties": {
        "username": {"type": "keyword"},
        "last_seen": {"type": "date"},
    }
}


class PresenceService:
    """Service for recording and querying live user presence."""

    # -------------------------------------------------------------------------
    # Index bootstrap
    # -------------------------------------------------------------------------

    @staticmethod
    def ensure_index_exists() -> None:
        """Create the presence index (with a high-priority regular-index
        template to avoid the single-node data-stream template) if it does
        not already exist. Mirrors ``AuditLogService.ensure_index_exists``."""
        from lib.es_helpers import (
            get_elasticsearch_client,
            put_regular_index_template,
            create_index_if_not_exists,
        )

        es = get_elasticsearch_client()
        if es is None:
            logging.warning("PresenceService: could not connect to ES")
            return

        put_regular_index_template(es, INDEX, INDEX_MAPPING, priority=500)
        create_index_if_not_exists(es, INDEX, INDEX_MAPPING)

        # Ensure new fields are added to an existing index.
        try:
            es.indices.put_mapping(index=INDEX, body=INDEX_MAPPING)
        except Exception as e:
            logging.warning("PresenceService: put_mapping update: %s", e)

    # -------------------------------------------------------------------------
    # Write
    # -------------------------------------------------------------------------

    @staticmethod
    def record_heartbeat(username: str, *, last_seen: datetime | None = None) -> None:
        """Upsert this user's presence row with ``last_seen = now``.

        Fire-and-forget — a write error is logged but NEVER raised, so a
        heartbeat failure cannot break the request that triggered it
        . Throttling is the caller's responsibility (the
        chokepoint in ``auth.py`` compares ``session["last_heartbeat"]``);
        this method always writes when called.

        ``last_seen`` defaults to the current UTC time. It is accepted as a
        parameter so callers / tests can stamp a deterministic timestamp.
        """
        if not username:
            return

        from lib.es_helpers import get_elasticsearch_client

        es = get_elasticsearch_client()
        if es is None:
            return

        ts = (last_seen or datetime.now(timezone.utc)).isoformat()
        try:
            # Upsert by _id=username: index() with an explicit id overwrites
            # the single always-current row rather than appending history.
            es.index(
                index=INDEX,
                id=username,
                document={"username": username, "last_seen": ts},
            )
        except Exception as e:  # noqa: BLE001 — fire-and-forget; must not raise
            logging.warning("PresenceService: heartbeat write failed for %r: %s", username, e)

    # -------------------------------------------------------------------------
    # Read
    # -------------------------------------------------------------------------

    @staticmethod
    def present_usernames(
        minutes: int | None = None,
        username_whitelist: set | None = None,
    ) -> set[str]:
        """Return usernames whose ``last_seen`` is within the presence window.

        A user is "present" iff they have a heartbeat row newer than
        ``now - minutes``. ``minutes`` defaults to ``SESSION_LIFETIME`` (the
        idle window) so "present" aligns with "has an alive session and acted
        within it" — the accurate replacement for the old login-age proxy.
        An operator can narrow / widen the window via the ``?minutes=`` query
        param on the active-users route (e.g. 5 for "clicked in the last 5
        minutes").

        Returns an empty set on ES outage / error (fail-soft — the count
        degrades to 0 rather than erroring the page).
        """
        from lib.es_helpers import get_elasticsearch_client
        from asset_management.web.config import config

        es = get_elasticsearch_client()
        if es is None:
            return set()

        if minutes is None:
            minutes = max(1, config.SESSION_LIFETIME // 60)

        filters: list[dict] = [
            {"range": {"last_seen": {"gte": f"now-{minutes}m"}}},
        ]
        if username_whitelist is not None:
            # Empty list deliberately matches zero docs.
            filters.append({"terms": {"username": list(username_whitelist)}})

        try:
            result = es.search(
                index=INDEX,
                body={
                    "size": 0,
                    "query": {"bool": {"filter": filters}},
                    "aggs": {
                        "present": {"terms": {"field": "username", "size": 10000}},
                    },
                },
                ignore_unavailable=True,
                allow_no_indices=True,
            )
            buckets = result.get("aggregations", {}).get("present", {}).get("buckets", [])
            return {b.get("key", "") for b in buckets if b.get("key")}
        except Exception as e:
            logging.error("PresenceService: present_usernames failed: %s", e)
            return set()

    # -------------------------------------------------------------------------
    # Retention cleanup
    # -------------------------------------------------------------------------

    @staticmethod
    def delete_older_than(days: int = 90) -> int:
        """Delete presence rows whose ``last_seen`` is older than ``days``.

        The index is self-pruning via upsert (one row per active user), but a
        deleted / long-dormant user's stale row lingers. This optional sweep
        keeps the index from accumulating rows for users who never return.
        Returns the number of deleted documents, or -1 on error.
        """
        from lib.es_helpers import get_elasticsearch_client

        es = get_elasticsearch_client()
        if es is None:
            return -1

        try:
            # proceed past docs the other gunicorn worker already deleted at startup (avoid 409).
            result = es.delete_by_query(
                index=INDEX,
                body={"query": {"range": {"last_seen": {"lt": f"now-{days}d"}}}},
                ignore_unavailable=True,
                allow_no_indices=True,
                conflicts="proceed",
            )
            deleted = result.get("deleted", 0)
            if deleted:
                logging.info(
                    "PresenceService: purged %d presence rows older than %d days",
                    deleted,
                    days,
                )
            return deleted
        except Exception as e:
            logging.error("PresenceService: retention cleanup failed: %s", e)
            return -1
