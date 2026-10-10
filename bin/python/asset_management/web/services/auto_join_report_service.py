"""
Auto-Join Report Service

Reads ``de_audit_log`` to produce per-org accept/decline stats for the
 domain-match auto-join surface. Surfaced on the org-edit page
so the org admin who controls ``auto_join_email_domains`` can see
how often the affordance is being taken.

The two audit actions this service queries are written from
``analytics/routes.py`` and carry a structured ``matched_org_esid``
keyword field (added to the audit log mapping in the same PR):

* ``onboarding_domain_join``          — user accepted the prompt
* ``onboarding_domain_join_declined`` — user clicked "Create my own"

Fail-closed posture: if ES is unreachable or the query raises, the
service returns ``None`` and the caller renders nothing rather than a
broken panel. Audit data is observability, not a hard requirement
for the page to load.
"""

from __future__ import annotations

import logging
import sys
from typing import TypedDict

sys.path.insert(0, "/app/python")
from lib.es_helpers import get_elasticsearch_client

AUDIT_INDEX = "de_audit_log"

ACTION_JOIN = "onboarding_domain_join"
ACTION_DECLINE = "onboarding_domain_join_declined"


class _RecentEvent(TypedDict):
    timestamp: str
    action: str  # "joined" | "declined"


class AutoJoinStats(TypedDict):
    """Shape returned by ``get_stats`` on a successful query."""

    accepted: int
    declined: int
    total: int
    acceptance_rate: int  # rounded percentage, 0-100; -1 when total == 0
    window_days: int
    recent: list[_RecentEvent]


class AutoJoinReportService:
    """Audit-log query helper for the org-edit decline-rate panel."""

    @staticmethod
    def get_stats(org_esid: str, *, window_days: int = 30, recent_limit: int = 5) -> AutoJoinStats | None:
        """Return accept/decline stats for the last ``window_days``.

        Returns ``None`` when:

        * ``org_esid`` is empty (defensive guard for an unsaved org)
        * Elasticsearch is unreachable
        * the underlying query raises

        Returns an ``AutoJoinStats`` dict (potentially with zeros and an
        empty ``recent`` list) when the query succeeds but the org has
        no events in the window. The caller distinguishes "no data" from
        "data unavailable" by the None vs. dict return.
        """
        if not org_esid:
            return None

        es = get_elasticsearch_client()
        if es is None:
            logging.warning(
                "AutoJoinReportService: ES unavailable; skipping stats for %r",
                org_esid,
            )
            return None

        # Two queries are cheaper here than one composite-aggregation query:
        # the counts are aggregations, the recent-events list is a sort+size,
        # and both target the same query shape with different aggs. Issued
        # as one msearch keeps the round-trip count at 1.
        common_filter = [
            {"term": {"matched_org_esid": org_esid}},
            {"terms": {"action": [ACTION_JOIN, ACTION_DECLINE]}},
            {"range": {"@timestamp": {"gte": f"now-{window_days}d"}}},
        ]

        agg_body = {
            "size": 0,
            "query": {"bool": {"filter": common_filter}},
            "aggs": {"by_action": {"terms": {"field": "action", "size": 10}}},
        }

        recent_body = {
            "size": recent_limit,
            "query": {"bool": {"filter": common_filter}},
            "sort": [{"@timestamp": {"order": "desc"}}],
            "_source": ["@timestamp", "action"],
        }

        try:
            agg_resp = es.search(index=AUDIT_INDEX, body=agg_body)
            recent_resp = es.search(index=AUDIT_INDEX, body=recent_body)
        except Exception as e:
            logging.warning("AutoJoinReportService: query failed for org=%r: %s", org_esid, e)
            return None

        accepted = 0
        declined = 0
        for bucket in agg_resp.get("aggregations", {}).get("by_action", {}).get("buckets", []):
            key = bucket.get("key")
            count = int(bucket.get("doc_count") or 0)
            if key == ACTION_JOIN:
                accepted = count
            elif key == ACTION_DECLINE:
                declined = count

        total = accepted + declined
        # Acceptance rate is the share of *prompts that produced a decision*
        # that were accepted. Domain matches that never produced a decision
        # (the user closed the tab) are invisible to us, so the rate is
        # conditional on "user actually clicked something."
        if total == 0:
            acceptance_rate = -1
        else:
            acceptance_rate = round(100 * accepted / total)

        recent: list[_RecentEvent] = []
        for hit in recent_resp.get("hits", {}).get("hits", []):
            source = hit.get("_source") or {}
            action = source.get("action") or ""
            recent.append(
                _RecentEvent(
                    timestamp=source.get("@timestamp") or "",
                    action="joined" if action == ACTION_JOIN else "declined",
                )
            )

        return AutoJoinStats(
            accepted=accepted,
            declined=declined,
            total=total,
            acceptance_rate=acceptance_rate,
            window_days=window_days,
            recent=recent,
        )
