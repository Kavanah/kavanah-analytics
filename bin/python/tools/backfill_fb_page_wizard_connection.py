"""backfill ``wizard_connection_esid`` onto existing legacy FB page docs.

Repairs the already-broken prod state left by the bridge dedup-no-restamp bug
: teams that re-authed through the Data Connectors wizard onto the
unified Meta app minted a valid ``de_connection`` token, but their already-
onboarded pages were skipped by the bridge's dedup-reuse branch and never got
``wizard_connection_esid`` stamped — so the importer kept using the legacy app.

The forward fix (re-stamp in the bridge) only helps the NEXT re-auth. This tool
migrates the pages that are already broken, without asking every partner to
re-auth again.

Approach (mirrors the bridge's own discovery, connection-first):

  For each Facebook ``de_connection`` that is ``active`` and holds a token:
    1. Collect its accepted ad accounts from ``de_connection_asset``
       (``asset_type=fb_ad_account``).
    2. Walk each ad account via ``meta_graph_client.list_pages_on_ad_account``
       using the connection's decrypted token — the SAME discovery the bridge
       uses (so we only ever claim pages this connection can genuinely read).
    3. For each discovered page_id, find existing ``de_asset_fb_pages`` doc(s)
       with that ``fb_page_id`` that lack ``wizard_connection_esid`` and stamp
       them with this connection (+ ``created_by_wizard`` / ``wizard_principal``).

Safety:

* **Dry-run by default.** Writes nothing unless ``--apply`` is passed; the
  dry run logs every page it WOULD migrate and the per-team summary.
* **Only stamps pages this connection can actually read** (via the ad-account
  walk) — it never blindly claims a page by id.
* **Skips pages that already carry a ``wizard_connection_esid``** (idempotent;
  never overwrites a different/newer connection's claim — those are reported
  as conflicts for manual review, not silently clobbered).
* **Lists the migrated teams** (``team_esid`` + page count + the legacy
  ``fb_account_title`` each page came from) so imports can be verified
  team-by-team after the backfill. This is the report the operator uses to
  confirm each team now imports via the unified app.

Usage::

    # Dry-run (default): show what would be migrated, write nothing.
    python3 import.py  # (not via import.py — this is a standalone tool)
    python3 -m tools.backfill_fb_page_wizard_connection
    docker exec -w /app/python imports_container python3 \
        -m tools.backfill_fb_page_wizard_connection

    # Apply the stamps.
    docker exec -w /app/python imports_container python3 \
        -m tools.backfill_fb_page_wizard_connection --apply

    # Restrict to one connection (e.g. verify a single team first).
    docker exec -w /app/python imports_container python3 \
        -m tools.backfill_fb_page_wizard_connection --connection-esid eq50_54B6Pt_nSoNIS3U --apply
"""

from __future__ import annotations

import argparse
import logging
import sys
from collections import defaultdict
from pathlib import Path

# Same three-candidate package resolver as tools/connection_backfill.py.
_HERE = Path(__file__).resolve().parent
for _candidate in (
    _HERE.parents[1],
    _HERE.parents[2] / "bin" / "python",
    Path("/app/python"),
):
    if (_candidate / "asset_management" / "__init__.py").exists():
        if str(_candidate) not in sys.path:
            sys.path.insert(0, str(_candidate))
        break

from lib.es_helpers import get_elasticsearch_client  # noqa: E402
from asset_management.web.services import meta_graph_client  # noqa: E402
from asset_management.web.services.connection_service import ConnectionService  # noqa: E402

FB_PAGES = "de_asset_fb_pages"
CONNECTION = "de_connection"
CONNECTION_ASSET = "de_connection_asset"


def _scan(es, index: str, query: dict) -> list[dict]:
    if not es.indices.exists(index=index):
        return []
    res = es.search(index=index, body={"query": query, "size": 1000})
    return list(res.get("hits", {}).get("hits", []))


def _fb_connections(es, only_esid: str | None) -> list[dict]:
    """Active Facebook connections holding a token (the migration candidates)."""
    if only_esid:
        try:
            doc = es.get(index=CONNECTION, id=only_esid)
        except Exception:  # noqa: BLE001 — boundary, missing id
            logging.error("connection %s not found", only_esid)
            return []
        return [doc]
    # provider is 'facebook' on these docs (the wizard label is 'meta_ads —').
    return _scan(es, CONNECTION, {"term": {"provider": "facebook"}})


def _accepted_ad_accounts(es, connection_esid: str) -> list[str]:
    # de_connection_asset maps connection_esid / asset_type as bare keyword
    # with NO ``.keyword`` subfield (verified prod 2026-06-25:
    # asset_type.keyword -> 0 hits, asset_type -> 9). Querying ``.keyword``
    # here silently matched nothing for every connection. Use the bare field.
    rows = _scan(
        es,
        CONNECTION_ASSET,
        {
            "bool": {
                "must": [
                    {"term": {"connection_esid": connection_esid}},
                    {"term": {"asset_type": "fb_ad_account"}},
                ]
            }
        },
    )
    out: list[str] = []
    for r in rows:
        pid = (r.get("_source") or {}).get("provider_asset_id")
        if pid:
            out.append(str(pid))
    return out


def _existing_page_docs(es, fb_page_id: str) -> list[dict]:
    return _scan(es, FB_PAGES, {"term": {"fb_page_id.keyword": fb_page_id}})


def backfill(es, *, apply: bool, only_esid: str | None) -> dict:
    """Run the backfill. Returns a summary dict."""
    connections = _fb_connections(es, only_esid)
    logging.info("Examining %d Facebook connection(s)%s", len(connections), f" (filtered to {only_esid})" if only_esid else "")

    # team_esid -> {page migrations}
    migrated_by_team: dict[str, list[dict]] = defaultdict(list)
    conflicts: list[dict] = []
    skipped_already: list[dict] = []
    n_stamped = 0

    for conn in connections:
        cid = conn["_id"]
        src = conn.get("_source") or {}
        status = src.get("status") or ""
        principal = src.get("principal") or ""
        if status != "active":
            logging.info("  connection %s (%s) status=%s — skipping (not active)", cid, principal, status)
            continue
        token = ConnectionService.get_decrypted_token(cid)
        if not token:
            logging.info("  connection %s (%s) has no decryptable token — skipping", cid, principal)
            continue

        ad_accounts = _accepted_ad_accounts(es, cid)
        if not ad_accounts:
            logging.info("  connection %s (%s): no accepted fb_ad_account rows — skipping", cid, principal)
            continue

        # Discover the pages this connection can actually read.
        discovered: dict[str, str] = {}  # page_id -> page_name
        for acct in ad_accounts:
            try:
                pages = meta_graph_client.list_pages_on_ad_account(token, acct)
            except Exception as e:  # noqa: BLE001 — per-account fail-soft
                logging.warning("  connection %s: list_pages_on_ad_account(%s) raised: %s", cid, acct, e)
                continue
            for p in pages:
                pid = p.get("id")
                if pid:
                    discovered[str(pid)] = p.get("name") or str(pid)

        if not discovered:
            logging.info("  connection %s (%s): ad-account walk found 0 pages — skipping", cid, principal)
            continue

        logging.info(
            "  connection %s (%s): discovered %d page(s): %s",
            cid,
            principal,
            len(discovered),
            ", ".join(sorted(discovered.values())),
        )

        for page_id, page_name in discovered.items():
            for doc in _existing_page_docs(es, page_id):
                esid = doc["_id"]
                d = doc.get("_source") or {}
                existing_conn = d.get("wizard_connection_esid")
                team_esid = d.get("team_esid") or "(none)"
                ws = d.get("workspace_esid") or "(none)"
                acct_title = d.get("fb_account_title") or "(legacy account)"

                if existing_conn == cid:
                    skipped_already.append({"page_id": page_id, "esid": esid, "team": team_esid})
                    continue
                if existing_conn:
                    # Already claimed by a DIFFERENT connection — do not clobber.
                    conflicts.append(
                        {
                            "page_id": page_id,
                            "page_name": page_name,
                            "esid": esid,
                            "team": team_esid,
                            "existing": existing_conn,
                            "would_set": cid,
                        }
                    )
                    logging.warning(
                        "  CONFLICT page %s (%s) esid=%s already on connection %s; "
                        "this run would set %s — SKIPPING (manual review)",
                        page_id,
                        page_name,
                        esid,
                        existing_conn,
                        cid,
                    )
                    continue

                # Migration candidate: legacy page, no wizard connection yet.
                migrated_by_team[team_esid].append(
                    {
                        "page_id": page_id,
                        "page_name": page_name,
                        "esid": esid,
                        "workspace": ws,
                        "from_account": acct_title,
                        "connection": cid,
                    }
                )
                if apply:
                    es.update(
                        index=FB_PAGES,
                        id=esid,
                        doc={
                            "wizard_connection_esid": cid,
                            "created_by_wizard": True,
                            "wizard_principal": principal,
                        },
                        refresh="wait_for",
                    )
                    n_stamped += 1

    # ── Report ──────────────────────────────────────────────────────────────
    mode = "APPLIED" if apply else "DRY-RUN (no writes)"
    logging.info("\n" + "=" * 70)
    logging.info("page-wizard-connection backfill — %s", mode)
    logging.info("=" * 70)
    total_pages = sum(len(v) for v in migrated_by_team.values())
    logging.info(
        "Teams with pages to migrate: %d | pages: %d | conflicts: %d | already-stamped: %d",
        len(migrated_by_team),
        total_pages,
        len(conflicts),
        len(skipped_already),
    )

    logging.info("\n--- MIGRATED TEAMS (verify each imports via the unified app after this) ---")
    for team_esid in sorted(migrated_by_team):
        pages = migrated_by_team[team_esid]
        logging.info("  team_esid=%s  (%d page(s))", team_esid, len(pages))
        for pg in pages:
            logging.info(
                "      page %-18s %-28s  ws=%-18s  from=%s  -> conn=%s",
                pg["page_id"],
                pg["page_name"][:28],
                pg["workspace"],
                pg["from_account"],
                pg["connection"],
            )

    if conflicts:
        logging.info("\n--- CONFLICTS (page already on a different connection — NOT touched) ---")
        for c in conflicts:
            logging.info(
                "  page %s (%s) esid=%s  on %s  would-be %s",
                c["page_id"],
                c["page_name"],
                c["esid"],
                c["existing"],
                c["would_set"],
            )

    if not apply and total_pages:
        logging.info("\nDry-run only. Re-run with --apply to write %d stamp(s).", total_pages)

    return {
        "mode": mode,
        "teams": len(migrated_by_team),
        "pages": total_pages,
        "stamped": n_stamped,
        "conflicts": len(conflicts),
        "already_stamped": len(skipped_already),
        "migrated_by_team": {k: v for k, v in migrated_by_team.items()},
    }


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="backfill wizard_connection_esid onto legacy FB page docs")
    parser.add_argument("--apply", action="store_true", help="Write the stamps (default: dry-run, write nothing)")
    parser.add_argument("--connection-esid", default=None, help="Restrict to a single de_connection esid")
    args = parser.parse_args(argv)

    logging.basicConfig(level=logging.INFO, format="%(message)s")

    es = get_elasticsearch_client()
    if es is None:
        logging.error("Elasticsearch client unavailable")
        return 1

    backfill(es, apply=args.apply, only_esid=args.connection_esid)
    return 0


if __name__ == "__main__":
    sys.exit(main())
