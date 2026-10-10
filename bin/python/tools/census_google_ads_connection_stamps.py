"""Read-only census: what would change for each Google Ads customer.

 makes the Google Ads importer pass the authenticating team's
``google_ads_connection_esid`` stamp to ``resolve_google_ads_oauth_config``.
Until now the stamp was dropped, so every customer used the legacy
principal-blind customer-id match. With the stamp, the resolver accepts ONLY
that connection — so a stale stamp turns a working import into a
skip. This census reports, per customer, which of these it is:

* ``legacy``   — the authenticating team has no stamp: behaviour unchanged.
* ``same``     — the stamp is the connection the legacy match already picks.
* ``switch``   — the stamp is valid but differs from the legacy pick: the
  import will now authenticate with the team's own connection.
* ``SKIP``     — the stamp is not an active, tokened connection for this
  customer: the import would be skipped with a reconnect error.
* ``no-conn``  — no active, tokened connection for this customer at all
  (already skipped today; unchanged).

The authenticating team is the lowest team_esid among the teams linked to
the customer, exactly as ``get_campaign_data.main`` picks it (/
). Nothing is written.

Usage::

    docker exec -w /app/python imports_container python3 \\
        -m tools.census_google_ads_connection_stamps
"""

from __future__ import annotations

import logging
import sys
from collections import Counter
from pathlib import Path
from typing import Any

_HERE = Path(__file__).resolve().parent
for _candidate in (
    _HERE.parents[1],
    _HERE.parents[2] / "bin" / "python",
    Path("/app/python"),
):
    if (_candidate / "asset_management").exists():
        sys.path.insert(0, str(_candidate))
        break

from lib.es_helpers import get_all_documents_paginated, get_elasticsearch_client  # noqa: E402

logger = logging.getLogger(__name__)

CUSTOMERS_INDEX = "de_asset_google_ads_customers"
TEAMS_INDEX = "de_asset_teams"
CONNECTIONS_INDEX = "de_connection"
STAMP_FIELD = "google_ads_connection_esid"


def _normalize(customer_id: Any) -> str:  # Any: ES values are untyped
    return str(customer_id or "").replace("-", "").strip()


def classify(
    customer_id: str,
    linked_team_docs: list[tuple[str, dict[str, Any]]],
    connections: dict[str, dict[str, Any]],
) -> tuple[str, str]:
    """``(outcome, detail)`` for one customer — see the module docstring.

    ``linked_team_docs`` must be sorted by team_esid (the importer's order);
    ``connections`` maps esid → source for ALL google_ads connections."""
    cid = _normalize(customer_id)
    matches = [
        esid
        for esid, src in sorted(connections.items())
        if src.get("status") == "active" and _normalize(src.get("provider_account_id")) == cid and src.get("oauth_token_enc")
    ]
    if not linked_team_docs:
        return "unlinked", "no active team links this customer"
    auth_esid, auth_doc = linked_team_docs[0]
    stamp = str(auth_doc.get(STAMP_FIELD) or "").strip()
    team = f"{auth_doc.get('team_title') or auth_esid}"
    if not stamp:
        if not matches:
            return "no-conn", f"{team}: no stamp; no active tokened connection"
        return "legacy", f"{team}: no stamp; legacy match {matches[0]} ({len(matches)} candidate(s))"
    if stamp in matches:
        if matches and matches[0] == stamp:
            return "same", f"{team}: stamp {stamp} is the legacy pick"
        return "switch", f"{team}: stamp {stamp} replaces legacy pick {matches[0] if matches else '-'}"
    conn = connections.get(stamp)
    if conn is None:
        why = "connection does not exist"
    else:
        why = (
            f"status={conn.get('status')!r} customer={_normalize(conn.get('provider_account_id')) or '-'} "
            f"tokened={bool(conn.get('oauth_token_enc'))}"
        )
    if not matches:
        return "no-conn", f"{team}: stamp {stamp} ({why}); no active tokened connection either"
    return "SKIP", f"{team}: stamp {stamp} invalid for {cid} ({why}); today uses {matches[0]}"


def main() -> int:
    logging.basicConfig(level=logging.INFO, format="%(message)s")
    es = get_elasticsearch_client()
    if es is None:
        logger.error("ABORT: Elasticsearch unavailable")
        return 2
    customers = get_all_documents_paginated(CUSTOMERS_INDEX, {"match_all": {}}, es=es)
    teams = get_all_documents_paginated(
        TEAMS_INDEX,
        {"bool": {"must": [{"term": {"team_google_ads_integration_status": "Active"}}, {"term": {"ds_google_ads": "true"}}]}},
        es=es,
    )
    conns = get_all_documents_paginated(CONNECTIONS_INDEX, {"term": {"provider": "google_ads"}}, es=es)
    connections = {h["_id"]: h.get("_source") or {} for h in conns}

    outcomes: Counter[str] = Counter()
    for customer in sorted(customers, key=lambda h: h["_id"]):
        src = customer.get("_source") or {}
        cesid = customer["_id"]
        linked = sorted(
            (
                (t["_id"], t.get("_source") or {})
                for t in teams
                if cesid in ((t.get("_source") or {}).get("team_google_ads_customer_esids") or [])
                or (t.get("_source") or {}).get("google_ads_customer_esid") == cesid
            ),
            key=lambda pair: pair[0],
        )
        outcome, detail = classify(src.get("google_ads_customer_id"), linked, connections)
        outcomes[outcome] += 1
        if outcome != "unlinked":
            logger.info("%-8s %s (%s) — %s", outcome, src.get("google_ads_customer_title") or cesid, cesid, detail)
    logger.info("customers=%d teams=%d connections=%d  %s", len(customers), len(teams), len(connections), dict(outcomes))
    return 0


if __name__ == "__main__":
    sys.exit(main())
