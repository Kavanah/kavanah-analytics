"""one-shot cleanup of ``de_billing_org`` records whose organization is gone.

The org is the billing/comp entity, but deleting an organization
never removed its ``de_billing_org`` doc. Prod (2026-09-17) carried three
records with ``comped_active=true`` and no ``de_asset_organization`` doc —
all from the 2026-08-04 comp backfill, orgs deleted since. They render in the
superuser comp roster as bare ESIDs with a live Clear button, and would hand
a comp to any future org that reused the ESID.

Going forward the web app's org-delete path cascades the billing doc
(``OrgBillingService.delete_org_billing_doc``, wired in ``app.delete_asset``).
This sweep clears what accumulated before that landed.

**Classification.** A billing doc is an orphan iff ``mget`` on
``de_asset_organization`` returns ``found: false`` for its id. Nothing else
counts: an org with a billing doc and an *empty* title is a live org, not
debris. There are no sentinel ids in this index (unlike the authz
sweep) — every ``de_billing_org`` doc is keyed by a real org ESID.

**Safety.** DRY-RUN BY DEFAULT; ``--apply`` must be an explicit true value
(the contract via the shared parse). Every deleted doc's
full ``_source`` is logged before deletion so the state is recoverable from
the import log. Any enumeration failure refuses (fail closed, exit non-zero):
a partial view of live orgs must never widen the orphan set.

Execution:
    # Dry-run (default) — report only.
    python3 /app/python/import.py migration cleanup_orphaned_org_billing
    # Apply.
    python3 /app/python/import.py migration cleanup_orphaned_org_billing --apply
"""

import logging

import lib.logging
from lib.es_helpers import get_elasticsearch_client
from migration.snapshot_gate import (  # the shared parse
    MigrationRefused,
    resolve_apply_mode,
)

BILLING_INDEX = "de_billing_org"
ORG_INDEX = "de_asset_organization"


def _search_all(es, index, body):
    """Search with a fixed 10k window (de_billing_org is tiny — 21 docs on prod).
    Raises on failure — every caller must fail CLOSED."""
    resp = es.search(index=index, body=dict(body, size=10000))
    return resp.get("hits", {}).get("hits", [])


def _collect_orphans(es):
    """Return [(doc_id, source), ...] for every billing doc whose org is gone.

    Existence is decided by ``mget`` on the org index — ``found`` is the only
    signal. Raises on any ES failure so the caller refuses the run.
    """
    hits = _search_all(es, BILLING_INDEX, {"query": {"match_all": {}}})
    if not hits:
        return []
    by_id = {h["_id"]: (h.get("_source") or {}) for h in hits}
    found = es.mget(index=ORG_INDEX, body={"ids": list(by_id)})
    missing = [d["_id"] for d in found.get("docs", []) if not d.get("found")]
    return [(doc_id, by_id[doc_id]) for doc_id in missing]


def main(params):
    lib.logging.define("cleanup-orphaned-org-billing")

    try:
        dry_run, _ = resolve_apply_mode(params)
    except MigrationRefused as e:
        logging.error("Refusing: %s", e)
        return 1
    if dry_run:
        logging.info("DRY-RUN: nothing will be deleted. Pass --apply to mutate.")

    es = get_elasticsearch_client()
    if es is None:
        logging.error("Failed to connect to Elasticsearch.")
        return 1

    # Fail CLOSED: an enumeration error must refuse the whole run — a partial
    # view of the org index would misclassify live billing docs as orphans.
    try:
        orphans = _collect_orphans(es)
    except Exception as e:  # noqa: BLE001 — refusal boundary
        logging.error("Refusing: enumeration failed (%s). Re-run when ES is healthy.", e)
        return 1

    logging.info("Orphaned %s docs (no %s doc): %d", BILLING_INDEX, ORG_INDEX, len(orphans))
    deleted = 0
    for doc_id, src in orphans:
        if dry_run:
            logging.info("DRY-RUN would delete %s/%s  source=%s", BILLING_INDEX, doc_id, src)
            continue
        # Full source logged BEFORE the delete so the record is recoverable
        # from the import log if a sweep decision is ever questioned.
        logging.info("Deleting %s/%s  source=%s", BILLING_INDEX, doc_id, src)
        try:
            es.delete(index=BILLING_INDEX, id=doc_id, refresh=True)
            deleted += 1
        except Exception as e:
            err = str(e).lower()
            if "not_found" in err or "404" in err:
                continue  # already gone — idempotent re-run
            logging.error("Delete failed for %s/%s: %s", BILLING_INDEX, doc_id, e)

    logging.info(
        "Cleanup complete%s: %d orphaned billing doc(s) %s.",
        " (dry-run)" if dry_run else "",
        len(orphans),
        "would be deleted" if dry_run else f"deleted ({deleted} removed)",
    )
    return {
        "dry_run": dry_run,
        "orphans": [doc_id for doc_id, _ in orphans],
        "deleted": deleted,
    }


if __name__ == "__main__":
    # Direct execution parses no flags — always a report-only dry-run.
    main({})
