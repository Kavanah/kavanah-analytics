#!/usr/bin/env python3
"""One-off migration: per-user billing metadata -> per-workspace records.

 workspace-billing Phase 4B. Phases 1-3 added the workspace billing
model with a PER-USER FALLBACK: members read the workspace billing record only
when one exists, else their own metadata. This script creates those workspace
records from existing per-user state so real users move onto the workspace
path. Until it runs (and is approved), everyone stays on the fallback —
nothing changes.

Resolution rule (decision 2026-06-08):
  * billing comes from the workspace OWNER's per-user metadata (the canonical
    payer);
  * if a workspace ALREADY has a de_billing_workspace record, SKIP it (never
    overwrite);
  * log every workspace: migrate / skip-existing / skip-no-owner / skip-no-
    billing.

DRY-RUN by default — reports what WOULD be written and changes nothing. Pass
``--apply`` to actually write the workspace records. Always run the dry-run
first and review the report.

    python3 /app/python/asset_management/jobs/migrate_billing_to_workspace.py            # dry-run
    python3 /app/python/asset_management/jobs/migrate_billing_to_workspace.py --apply    # write
"""

from __future__ import annotations

import argparse
import logging
import sys

sys.path.insert(0, "/app/python")

# The billing fields copied from the owner's metadata onto the workspace record.
_BILLING_KEYS = [
    "paid_tier",
    "trial_started_at",
    "comped_active",
    "comped_by",
    "comped_at",
    "comp_revoked_at",
    "stripe_status",
    "stripe_customer_id",
    "trial_ending_email_sent_at",
]

_WORKSPACE_INDEX = "de_asset_workspace"


def _has_billing_signal(meta: dict) -> bool:
    """True if the owner's metadata carries ANY non-empty billing field worth
    migrating (so we don't create empty workspace records for never-billed
    workspaces — those correctly stay on the fresh-trial fallback)."""
    for k in _BILLING_KEYS:
        v = meta.get(k)
        if v not in (None, "", False):
            return True
    return False


def main() -> int:
    parser = argparse.ArgumentParser(description="Migrate per-user billing -> per-workspace records.")
    parser.add_argument("--apply", action="store_true", help="Actually write records (default: dry-run, no writes).")
    args = parser.parse_args()
    apply = args.apply

    logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")

    from lib.es_helpers import get_elasticsearch_client
    from asset_management.web.services.workspace_billing_service import (
        WorkspaceBillingService,
        INDEX_WORKSPACE_BILLING,
    )

    es = get_elasticsearch_client()
    if es is None:
        logging.error("migrate_billing_to_workspace: ES unavailable")
        return 1

    if apply:
        WorkspaceBillingService.ensure_index_exists()

    # Enumerate workspaces.
    try:
        resp = es.search(index=_WORKSPACE_INDEX, body={"query": {"match_all": {}}, "size": 10000})
        workspaces = resp.get("hits", {}).get("hits", [])
    except Exception as exc:  # noqa: BLE001
        logging.error("migrate_billing_to_workspace: workspace enumerate failed: %s", exc)
        return 1

    mode = "APPLY" if apply else "DRY-RUN"
    logging.info("=== Billing migration (%s) — %d workspace(s) ===", mode, len(workspaces))

    stats = {"migrate": 0, "skip_existing": 0, "skip_no_owner": 0, "skip_no_billing": 0, "error": 0}
    for hit in workspaces:
        ws_esid = hit.get("_id")
        ws_title = (hit.get("_source") or {}).get("workspace_title") or (hit.get("_source") or {}).get("title") or ""

        # Skip if a workspace billing record already exists (never overwrite).
        existing = WorkspaceBillingService._get_billing_doc(es, ws_esid)
        if existing:
            stats["skip_existing"] += 1
            logging.info("SKIP-EXISTING  ws=%s (%s) — already has a billing record", ws_esid, ws_title)
            continue

        owner = WorkspaceBillingService.find_workspace_owner(ws_esid)
        if not owner:
            stats["skip_no_owner"] += 1
            logging.info("SKIP-NO-OWNER  ws=%s (%s) — no workspace_owner found", ws_esid, ws_title)
            continue

        # Read the owner's per-user billing metadata.
        try:
            rec = dict(es.security.get_user(username=owner)).get(owner) or {}
        except Exception as exc:  # noqa: BLE001
            stats["error"] += 1
            logging.warning("ERROR          ws=%s owner=%s — get_user failed: %s", ws_esid, owner, exc)
            continue
        meta = rec.get("metadata") or {}

        if not _has_billing_signal(meta):
            stats["skip_no_billing"] += 1
            logging.info("SKIP-NO-BILLING ws=%s (%s) owner=%s — owner has no billing state", ws_esid, ws_title, owner)
            continue

        fields = {k: meta[k] for k in _BILLING_KEYS if meta.get(k) not in (None, "")}
        stats["migrate"] += 1
        logging.info(
            "MIGRATE        ws=%s (%s) owner=%s -> %s",
            ws_esid,
            ws_title,
            owner,
            {k: fields[k] for k in fields},
        )
        if apply:
            ok = WorkspaceBillingService._put_billing_doc(es, ws_esid, fields)
            if not ok:
                stats["error"] += 1
                logging.warning("ERROR          ws=%s — write failed", ws_esid)

    logging.info("=== %s complete: %s ===", mode, stats)
    if not apply:
        logging.info("DRY-RUN only — no records written. Re-run with --apply to migrate.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
