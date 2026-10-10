#!/usr/bin/env python3
"""One-off migration: existing workspace subscription -> per-team subscription.

 Option B Phase O4. Phases O1-O3 added the per-team subscription model
(de_billing_team_subscription) behind the ORG_BILLING_PER_TEAM flag (off). When
that flag is flipped on, a member's entitlement + cancel/resume resolve through
their TEAM's subscription. Existing paying orgs have ONE workspace-level Stripe
subscription and NO team-subscription doc, so they'd read as unlicensed the
moment the flag flips. This script associates each existing workspace
subscription with the org's PRIMARY team (decision 2026-06-09) by creating the
team-subscription doc, so existing payers keep working under the per-team model.

RUN THIS (and approve it) BEFORE flipping ORG_BILLING_PER_TEAM on.

Resolution rule:
  * for each de_billing_workspace record that has a stripe_customer_id AND an
    active/paying stripe_status, find the workspace OWNER's PRIMARY team;
  * fetch the customer's live subscription id from Stripe
    (get_subscription_summary);
  * write a de_billing_team_subscription doc (team_esid == doc id) linking that
    team -> the existing subscription, under the org customer;
  * if the team ALREADY has a team-subscription doc, SKIP (never overwrite);
  * log every workspace: migrate / skip-existing / skip-no-owner / skip-no-team
    / skip-no-customer / skip-no-subscription / skip-not-paying.

DRY-RUN by default — reports what WOULD be written and changes nothing. Pass
``--apply`` to write. Always run the dry-run first and review.

    python3 /app/python/asset_management/jobs/migrate_billing_to_org_teams.py            # dry-run
    python3 /app/python/asset_management/jobs/migrate_billing_to_org_teams.py --apply    # write
"""

from __future__ import annotations

import argparse
import logging
import sys

sys.path.insert(0, "/app/python")

_WORKSPACE_INDEX = "de_asset_workspace"

# Stripe statuses that represent a live/paying subscription worth migrating.
# A never-paid / canceled workspace has nothing to associate with a team.
_PAYING_STATUSES = frozenset({"active", "trialing", "past_due"})


def migrate(*, apply: bool) -> dict:
    """Run the migration (or dry-run). Returns the stats dict. Pure of argv so
    it's unit-testable; main() wraps it with arg parsing + logging."""
    from lib.es_helpers import get_elasticsearch_client
    from asset_management.web.config import config
    from asset_management.web.services.workspace_billing_service import (
        WorkspaceBillingService,
        INDEX_WORKSPACE_BILLING,
    )
    from asset_management.web.services.org_billing_service import OrgBillingService
    from asset_management.web.services.stripe_service import StripeService, StripeError

    stats = {
        "migrate": 0,
        "skip_existing": 0,
        "skip_no_owner": 0,
        "skip_no_team": 0,
        "skip_no_customer": 0,
        "skip_no_subscription": 0,
        "skip_not_paying": 0,
        "error": 0,
    }

    es = get_elasticsearch_client()
    if es is None:
        logging.error("migrate_billing_to_org_teams: ES unavailable")
        stats["error"] += 1
        return stats

    if apply:
        OrgBillingService.ensure_index_exists()

    # Enumerate workspace billing records (only billed workspaces have one).
    try:
        resp = es.search(index=INDEX_WORKSPACE_BILLING, body={"query": {"match_all": {}}, "size": 10000})
        rows = resp.get("hits", {}).get("hits", [])
    except Exception as exc:  # noqa: BLE001
        err = str(exc).lower()
        if "index_not_found" in err or "404" in err:
            logging.info("No de_billing_workspace index — nothing to migrate.")
            return stats
        logging.error("migrate_billing_to_org_teams: enumerate failed: %s", exc)
        stats["error"] += 1
        return stats

    mode = "APPLY" if apply else "DRY-RUN"
    logging.info("=== Org-team billing migration (%s) — %d workspace billing record(s) ===", mode, len(rows))

    secret_key = config.STRIPE_SECRET_KEY
    for hit in rows:
        ws_esid = hit.get("_id")
        src = hit.get("_source") or {}
        customer_id = src.get("stripe_customer_id") or ""
        status = (src.get("stripe_status") or "").strip().lower()

        if not customer_id:
            stats["skip_no_customer"] += 1
            logging.info("SKIP-NO-CUSTOMER ws=%s — no stripe_customer_id", ws_esid)
            continue
        if status not in _PAYING_STATUSES:
            stats["skip_not_paying"] += 1
            logging.info("SKIP-NOT-PAYING  ws=%s — status=%r not a live subscription", ws_esid, status)
            continue

        owner = WorkspaceBillingService.find_workspace_owner(ws_esid)
        if not owner:
            stats["skip_no_owner"] += 1
            logging.info("SKIP-NO-OWNER    ws=%s — no workspace_owner", ws_esid)
            continue

        team_esid = OrgBillingService.resolve_primary_team_esid(owner)
        if not team_esid:
            stats["skip_no_team"] += 1
            logging.info("SKIP-NO-TEAM     ws=%s owner=%s — no primary team", ws_esid, owner)
            continue

        # Idempotent: never overwrite an existing team-subscription doc.
        if OrgBillingService.get_team_subscription(team_esid):
            stats["skip_existing"] += 1
            logging.info("SKIP-EXISTING    ws=%s team=%s — already has a team-subscription", ws_esid, team_esid)
            continue

        # Fetch the customer's live subscription id from Stripe.
        subscription_id = ""
        if secret_key:
            try:
                summary = StripeService.get_subscription_summary(secret_key=secret_key, customer_id=customer_id)
                subscription_id = (summary or {}).get("subscription_id") or ""
            except StripeError as exc:
                logging.warning("ws=%s — Stripe summary failed: %s", ws_esid, exc)
        if not subscription_id:
            stats["skip_no_subscription"] += 1
            logging.info("SKIP-NO-SUB      ws=%s customer=%s — no live subscription id resolved", ws_esid, customer_id)
            continue

        stats["migrate"] += 1
        logging.info(
            "MIGRATE          ws=%s owner=%s -> team=%s sub=%s status=%s",
            ws_esid,
            owner,
            team_esid,
            subscription_id,
            status,
        )
        if apply:
            ok = OrgBillingService.upsert_team_subscription(
                team_esid=team_esid,
                workspace_esid=ws_esid,
                stripe_customer_id=customer_id,
                stripe_subscription_id=subscription_id,
                stripe_status=status,
            )
            if not ok:
                stats["error"] += 1
                logging.warning("ERROR            ws=%s team=%s — write failed", ws_esid, team_esid)

    logging.info("=== %s complete: %s ===", mode, stats)
    if not apply:
        logging.info("DRY-RUN only — no records written. Re-run with --apply to migrate.")
    return stats


def main() -> int:
    parser = argparse.ArgumentParser(description="Migrate workspace subscription -> per-team.")
    parser.add_argument("--apply", action="store_true", help="Actually write records (default: dry-run).")
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
    stats = migrate(apply=args.apply)
    return 1 if stats.get("error") else 0


if __name__ == "__main__":
    sys.exit(main())
