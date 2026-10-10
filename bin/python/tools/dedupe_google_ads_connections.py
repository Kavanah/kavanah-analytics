"""One-shot dedupe: duplicate google_ads de_connection rows for one customer
.

``provider_account_id`` was written verbatim but read back dash-stripped, so
the same Google Ads customer entered as "646-706-0525" and "6467060525"
produced two rows that BOTH matched every lookup — non-deterministic
credential selection (the principal-blind first-match warning firing
on every resolve). The write path is fixed (connection_service canonicalizes
at write and the natural-key finder compares canonically); this tool heals
the rows that already exist:

* groups google_ads connections by (principal, canonical id);
* in each duplicate group the SURVIVOR is the newest-``updated_at`` row that
  is active and tokened (falling back to newest overall);
* the survivor's ``provider_account_id`` is normalized to the canonical
  spelling; the losers get ``status: superseded`` (never deleted — the same
  disposition a teammate's abandoned wizard rows carry) plus a note naming the
  survivor;
* any ``de_asset_teams.google_ads_connection_esid`` stamp pointing at a loser
  is REPOINTED to the survivor — the ``prefer_connection_esid``
  resolver reads that stamp, so leaving it on a superseded row would break
  the team's credential scoping.

Dry-run by default; ``--apply`` writes. Idempotent: a second run finds no
duplicate groups.

Usage::

    docker exec -w /app/python imports_container python3 \\
        -m tools.dedupe_google_ads_connections [--apply]
"""

from __future__ import annotations

import argparse
import logging
import sys
from pathlib import Path

_HERE = Path(__file__).resolve().parent
for _candidate in (
    _HERE.parents[1],
    _HERE.parents[2] / "bin" / "python",
    Path("/app/python"),
):
    if (_candidate / "asset_management").exists():
        sys.path.insert(0, str(_candidate))
        break

from lib.es_helpers import get_elasticsearch_client  # noqa: E402

logger = logging.getLogger(__name__)

INDEX_CONNECTION = "de_connection"
TEAMS_INDEX = "de_asset_teams"


def _canonical(value: str) -> str:
    return (value or "").strip().replace("-", "")


def plan_groups(rows: list[dict]) -> list[dict]:
    """Pure decision logic: group rows and pick survivors.

    ``rows``: [{"esid", "principal", "provider_account_id", "status",
    "has_token", "updated_at"}]. Returns one plan per DUPLICATE group:
    {"survivor", "losers": [...], "canonical_id", "survivor_needs_normalize"}.
    """
    groups: dict = {}
    for r in rows:
        key = (r.get("principal") or "", _canonical(r.get("provider_account_id") or ""))
        groups.setdefault(key, []).append(r)

    plans = []
    for (principal, canonical_id), members in sorted(groups.items()):
        if len(members) < 2 or not canonical_id:
            continue

        def rank(r):
            # Active+tokened first, then newest updated_at.
            return (
                r.get("status") == "active",
                bool(r.get("has_token")),
                r.get("updated_at") or "",
            )

        members = sorted(members, key=rank, reverse=True)
        survivor, losers = members[0], members[1:]
        plans.append(
            {
                "principal": principal,
                "canonical_id": canonical_id,
                "survivor": survivor,
                "losers": losers,
                "survivor_needs_normalize": (survivor.get("provider_account_id") or "") != canonical_id,
            }
        )
    return plans


def run(es, *, apply: bool) -> dict:
    resp = es.search(
        index=INDEX_CONNECTION,
        body={
            "size": 1000,
            "query": {"term": {"provider": "google_ads"}},
            "_source": ["principal", "provider_account_id", "status", "oauth_token_enc", "updated_at"],
        },
    )
    rows = []
    for h in resp.get("hits", {}).get("hits", []):
        src = h.get("_source") or {}
        if src.get("status") == "superseded":
            continue  # already retired — never a survivor candidate
        rows.append(
            {
                "esid": h["_id"],
                "principal": src.get("principal"),
                "provider_account_id": src.get("provider_account_id"),
                "status": src.get("status"),
                "has_token": bool(src.get("oauth_token_enc")),
                "updated_at": src.get("updated_at"),
            }
        )

    plans = plan_groups(rows)
    summary = {"groups": len(plans), "superseded": 0, "repointed_teams": 0, "normalized": 0, "applied": apply}
    mode = "APPLY" if apply else "DRY-RUN"
    for plan in plans:
        surv = plan["survivor"]
        logger.info(
            "%s principal=%s customer=%s: survivor=%s (%s, token=%s, updated=%s); superseding %s",
            mode,
            plan["principal"],
            plan["canonical_id"],
            surv["esid"],
            surv["status"],
            surv["has_token"],
            surv["updated_at"],
            [(loser["esid"], loser["provider_account_id"]) for loser in plan["losers"]],
        )
        if plan["survivor_needs_normalize"]:
            summary["normalized"] += 1
            if apply:
                es.update(
                    index=INDEX_CONNECTION,
                    id=surv["esid"],
                    doc={"provider_account_id": plan["canonical_id"]},
                    refresh="wait_for",
                )
        for loser in plan["losers"]:
            summary["superseded"] += 1
            if apply:
                es.update(
                    index=INDEX_CONNECTION,
                    id=loser["esid"],
                    doc={
                        "status": "superseded",
                        "superseded_by": surv["esid"],
                        "superseded_reason": "duplicate provider_account_id spelling",
                    },
                    refresh="wait_for",
                )
            # Repoint team stamps off the loser.
            t = es.search(
                index=TEAMS_INDEX,
                body={
                    "size": 50,
                    "query": {"term": {"google_ads_connection_esid": loser["esid"]}},
                    "_source": ["team_title"],
                },
            )
            for th in t.get("hits", {}).get("hits", []):
                summary["repointed_teams"] += 1
                logger.info(
                    "%s   team %s (%s): google_ads_connection_esid %s -> %s",
                    mode,
                    th["_id"],
                    (th.get("_source") or {}).get("team_title"),
                    loser["esid"],
                    surv["esid"],
                )
                if apply:
                    es.update(
                        index=TEAMS_INDEX,
                        id=th["_id"],
                        doc={"google_ads_connection_esid": surv["esid"]},
                        refresh="wait_for",
                    )
    logger.info("RESULT: %s", summary)
    return summary


def main() -> int:
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    ap = argparse.ArgumentParser(description="Dedupe google_ads de_connection rows.")
    ap.add_argument("--apply", action="store_true", help="write changes (default: dry-run)")
    args = ap.parse_args()
    es = get_elasticsearch_client()
    if es is None:
        logger.error("Elasticsearch unavailable")
        return 1
    run(es, apply=args.apply)
    if not args.apply:
        logger.info("DRY-RUN only. Re-run with --apply to write.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
