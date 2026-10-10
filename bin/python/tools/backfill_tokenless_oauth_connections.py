"""One-shot heal: flip active-but-tokenless Google Ads / GA4 connections to
``pending_token``.

The Data Connectors wizard's tokenless "configure" path could land a Google Ads
or GA4 ``de_connection`` at ``status=active`` with no ``oauth_token_enc``.
earlier/1040 that was harmless (a shared service account did the import);
after the service accounts were retired such a connection is un-importable —
the importer records ``missing_credentials`` while the UI shows it "connected".

``create_tokenless_connection`` now lands these as ``pending_token`` going
forward. This backfill heals the rows that already exist: every
``de_connection`` whose provider is ``google_ads`` or ``ga4``, whose status is
``active``, and whose ``oauth_token_enc`` is empty → re-stamped
``pending_token`` so the card prompts a real OAuth authorization.

Deliberately NOT touched:
* ``echo`` / ``disciple_tools`` — legitimately active without an
  ``oauth_token_enc`` in the connection doc (cluster service token / Site-Link).
* any row that already carries a token (it imports fine).
* ``revoked`` / ``expired`` rows (already non-active; their own teardown signal).

The script is **dry-run by default**. Use ``--apply`` to write. Idempotent: a
second ``--apply`` finds nothing to flip (the rows are now ``pending_token``).

Usage::

    # Dry-run (default) — lists what would flip, writes nothing.
    docker exec -w /app/python imports_container python3 \\
        -m tools.backfill_tokenless_oauth_connections

    # Apply.
    docker exec -w /app/python imports_container python3 \\
        -m tools.backfill_tokenless_oauth_connections --apply
"""

from __future__ import annotations

import argparse
import logging
import sys

logging.basicConfig(level=logging.INFO, format="%(message)s")
logger = logging.getLogger(__name__)


def _now_iso() -> str:
    from datetime import datetime, timezone

    return datetime.now(tz=timezone.utc).isoformat()


def run(*, apply: bool) -> int:
    """Flip active-but-tokenless google_ads/ga4 connections to pending_token.

    Returns the number of rows flipped (or that WOULD be flipped in dry-run).
    """
    from lib.es_helpers import get_elasticsearch_client
    from asset_management.web.services.connection_service import (
        INDEX_CONNECTION,
        PROVIDER_GA4,
        PROVIDER_GOOGLE_ADS,
        STATUS_ACTIVE,
        STATUS_PENDING_TOKEN,
    )

    es = get_elasticsearch_client()
    if es is None:
        logger.error("Could not connect to Elasticsearch")
        return 0

    # active google_ads/ga4 rows; the tokenless filter is applied client-side
    # because oauth_token_enc is a free-text secret field (not reliably a
    # keyword we can `exists`-filter the empty string on across mappings).
    body = {
        "size": 1000,
        "query": {
            "bool": {
                "filter": [
                    {"terms": {"provider": [PROVIDER_GOOGLE_ADS, PROVIDER_GA4]}},
                    {"term": {"status": STATUS_ACTIVE}},
                ]
            }
        },
    }
    try:
        resp = es.search(index=INDEX_CONNECTION, body=body, ignore_unavailable=True, allow_no_indices=True)
    except Exception as e:  # noqa: BLE001 — boundary
        logger.error("search failed: %s", e)
        return 0

    hits = (resp.get("hits") or {}).get("hits") or []
    flipped = 0
    for hit in hits:
        src = hit.get("_source") or {}
        if (src.get("oauth_token_enc") or "").strip():
            continue  # has a token → imports fine, leave active
        esid = hit["_id"]
        logger.info(
            "%s %s (provider=%s, account=%s) active+tokenless -> pending_token",
            "WOULD FLIP" if not apply else "FLIP",
            esid,
            src.get("provider"),
            src.get("provider_account_id"),
        )
        flipped += 1
        if apply:
            try:
                es.update(
                    index=INDEX_CONNECTION,
                    id=esid,
                    body={"doc": {"status": STATUS_PENDING_TOKEN, "updated_at": _now_iso()}},
                    refresh="wait_for",
                )
            except Exception as e:  # noqa: BLE001 — boundary
                logger.error("  update failed for %s: %s", esid, e)
                flipped -= 1

    logger.info("%s: %d connection(s) %s", "APPLIED" if apply else "DRY-RUN", flipped, "flipped" if apply else "would flip")
    return flipped


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--apply", action="store_true", help="Write the changes (default: dry-run).")
    args = parser.parse_args()
    run(apply=args.apply)


if __name__ == "__main__":
    sys.exit(main())
