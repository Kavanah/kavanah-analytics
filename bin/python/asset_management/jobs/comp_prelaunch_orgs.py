#!/usr/bin/env python3
"""One-off: comp the ORGS under a pre-launch workspace.

Pre-launch orgs are 100% comped (decision a teammate 2026-06-16; they may be billed
later). The billing/comp entity is the ORG, not the workspace: a single
workspace can hold many orgs — ``kavanah-media`` holds 20 — and comping one must
not comp its siblings, so comp lives on a per-org ``de_billing_org`` record that
``get_state`` consults before the workspace path.

This job creates/sets a comped ``de_billing_org`` record (via
``OrgBillingService.set_org_comp``) for every org under the named workspace(s).
It does NOT touch the ``de_billing_workspace`` record and does NOT propagate a
paying ``paid_tier`` onto any org — comp is the only verdict it writes, so it can
never accidentally fabricate paid orgs out of one paying workspace record.

Why a deliberate comp rather than a blind workspace->org mirror: a single
workspace billing record (e.g. kavanah-media is paid_tier=true) maps to 20 orgs;
copying that verdict would mint 20 paid orgs. The pre-launch action we actually
want is "comp these orgs", so this job writes exactly that.

DRY-RUN by default — reports which orgs WOULD be comped and changes nothing. Pass
``--apply`` to write. Always run the dry-run first and review.

    # comp every org under kavanah-media (dry-run, then apply)
    python3 /app/python/asset_management/jobs/comp_prelaunch_orgs.py --workspace kavanah-media
    python3 /app/python/asset_management/jobs/comp_prelaunch_orgs.py --workspace kavanah-media --apply

    # comp a single org by esid
    python3 /app/python/asset_management/jobs/comp_prelaunch_orgs.py --org kE_H25sBIm2AdJKuZWXe --apply

ES-runtime data (CLAUDE.md ``update_vN`` caveat): applying on staging does NOT
apply on prod. Run separately per environment.
"""

from __future__ import annotations

import argparse
import logging
import sys

sys.path.insert(0, "/app/python")

ORGANIZATION_INDEX = "de_asset_organization"


def _orgs_under_workspace(es, workspace_esid: str) -> list[dict]:
    """Return [{esid, title}] for every org whose ``workspace_esid`` matches."""
    try:
        resp = es.search(
            index=ORGANIZATION_INDEX,
            body={
                "query": {"term": {"workspace_esid": workspace_esid}},
                "_source": ["organization_title"],
                "size": 10000,
            },
        )
    except Exception as exc:  # noqa: BLE001
        logging.error("comp_prelaunch_orgs: org enumerate failed for ws=%s: %s", workspace_esid, exc)
        return []
    out = []
    for h in resp.get("hits", {}).get("hits", []):
        out.append({"esid": h.get("_id"), "title": (h.get("_source") or {}).get("organization_title") or h.get("_id")})
    return out


def comp_orgs(*, workspace_esid: str | None, org_esid: str | None, set_by: str, apply: bool) -> dict:
    """Comp the targeted orgs. Pure of argv so it's unit-testable. Returns stats."""
    from lib.es_helpers import get_elasticsearch_client
    from asset_management.web.services.org_billing_service import OrgBillingService

    stats = {"comped": 0, "skip_already": 0, "error": 0, "targets": 0}

    es = get_elasticsearch_client()
    if es is None:
        logging.error("comp_prelaunch_orgs: ES unavailable")
        stats["error"] += 1
        return stats

    if apply:
        OrgBillingService.ensure_index_exists()

    # Build the target org list.
    if org_esid:
        targets = [{"esid": org_esid, "title": org_esid}]
    elif workspace_esid:
        targets = _orgs_under_workspace(es, workspace_esid)
    else:
        logging.error("comp_prelaunch_orgs: pass --workspace or --org")
        stats["error"] += 1
        return stats

    stats["targets"] = len(targets)
    mode = "APPLY" if apply else "DRY-RUN"
    logging.info("=== Comp pre-launch orgs (%s) — %d target org(s) ===", mode, len(targets))

    for org in targets:
        oe = org["esid"]
        # Idempotent report: note if already comped (we still re-stamp on apply,
        # which is harmless — set_org_comp is upsert).
        existing = OrgBillingService._get_org_billing_doc(es, oe) if hasattr(OrgBillingService, "_get_org_billing_doc") else {}
        already = bool(existing.get("comped_active"))
        if already:
            stats["skip_already"] += 1
            logging.info("ALREADY-COMPED   org=%s (%s)", oe, org["title"])
            if not apply:
                continue

        logging.info("COMP             org=%s (%s)", oe, org["title"])
        if apply:
            res = OrgBillingService.set_org_comp(oe, set_by, workspace_esid=workspace_esid or "")
            if res.get("ok"):
                stats["comped"] += 1
            else:
                stats["error"] += 1
                logging.warning("ERROR            org=%s — %s", oe, res.get("error"))

    logging.info("=== %s complete: %s ===", mode, stats)
    if not apply:
        logging.info("DRY-RUN only — no records written. Re-run with --apply to comp.")
    return stats


def main() -> int:
    parser = argparse.ArgumentParser(description="Comp pre-launch orgs.")
    parser.add_argument("--workspace", help="Comp every org under this workspace_esid.")
    parser.add_argument("--org", help="Comp a single org by org_esid.")
    parser.add_argument("--set-by", default="comp_prelaunch_orgs", help="Actor recorded as comped_by.")
    parser.add_argument("--apply", action="store_true", help="Actually write records (default: dry-run).")
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
    stats = comp_orgs(
        workspace_esid=args.workspace,
        org_esid=args.org,
        set_by=args.set_by,
        apply=args.apply,
    )
    return 1 if stats.get("error") else 0


if __name__ == "__main__":
    sys.exit(main())
