"""One-shot dedupe: phantom Meta de_connection rows minted by wizard re-runs
.

Before Phase B, a SECOND person re-running the connector wizard for
an established account always minted a fresh connection row (the
merge keys on principal + Meta user id, both of which differ), while every
team/asset reference stayed pinned to the original — often dead — row. Phase
B stops new debris at the callback; this tool heals what already accumulated:

* enumerates non-superseded, non-pending Meta connections (``facebook`` /
  ``instagram``), EXCLUDING ``token_kind=system_user`` rows — a system-user
  row sharing assets with a wizard row is the two credential families
  coexisting, never a duplicate;
* groups rows by (provider, IDENTICAL set of bound ``de_connection_asset``
  legacy_asset_esids). Identical-set is the deliberate merge criterion:
  overlapping-but-different sets are REPORTED for review, never acted on
  (decline and report — house rule);
* in each duplicate group the SURVIVOR is the row the team docs reference
  (most references win; then live token, then newest ``updated_at``);
* if the survivor lacks a live token and EXACTLY ONE loser has one, the
  loser's credential is copied onto the survivor (token, expiry, scopes,
  status, principal AND provider_account_id — the token belongs to that Meta
  user, so the account id must follow it; ``token_from`` records the
  source row). Two live-tokened losers = ambiguous = no heal, reported;
* losers get ``status: superseded`` + ``superseded_by`` (never deleted),
  their token cleared, their team stamps (all five connection fields, bare
  and ``.keyword``) repointed to the survivor, and their now-redundant
  ``de_connection_asset`` link rows deleted (identical to the survivor's by
  construction; every deleted id is logged).

Dry-run by default — the dry run IS the census (run it on prod first and
read the plan). ``--apply`` writes. argparse ``store_true`` means the
``--apply=false`` truthiness trap (review) cannot exist here.
Idempotent: superseded rows are excluded from enumeration, so a second run
finds no groups. Fail-closed: any enumeration error aborts before planning.

Usage::

    docker exec -w /app/python imports_container python3 \\
        -m tools.dedupe_connections [--apply]
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
INDEX_CONNECTION_ASSET = "de_connection_asset"
TEAMS_INDEX = "de_asset_teams"

PROVIDERS = ("facebook", "instagram")

# Mirrors connections/routes._TEAM_CONNECTION_FIELDS — every field a team doc
# can use to point at a de_connection. Queried bare AND.keyword (:
# mapping drift between prod and staging).
TEAM_CONNECTION_FIELDS = (
    "connection_esid",
    "google_ads_connection_esid",
    "ga4_connection_esid",
    "youtube_connection_esid",
    "bitly_connection_esid",
)


def _is_live(row: dict) -> bool:
    return row.get("status") == "active" and bool(row.get("has_token"))


def plan_groups(rows: list[dict], assets_by_conn: dict, refs_by_conn: dict) -> dict:
    """Pure decision logic — no ES.

    ``rows``: [{"esid", "provider", "principal", "provider_account_id",
    "status", "has_token", "token_kind", "updated_at", "_token_fields"}].
    ``assets_by_conn``: esid -> set of bound legacy_asset_esids.
    ``refs_by_conn``: esid -> [(team_id, field), ...].

    Returns ``{"plans": [...], "review": [...]}``. Each plan:
    {"provider", "asset_set", "survivor", "losers", "token_heal_from"
    (loser esid or None), "token_heal_ambiguous" (bool)}.
    ``review`` carries overlapping-but-not-identical asset-set clusters —
    reported, never acted on.
    """
    groups: dict = {}
    for r in rows:
        if (r.get("token_kind") or "") == "system_user":
            continue  # the other credential family — never a duplicate
        asset_set = frozenset(assets_by_conn.get(r["esid"]) or ())
        if not asset_set:
            continue  # nothing bound — no basis to call anything a duplicate
        groups.setdefault((r.get("provider") or "", asset_set), []).append(r)

    plans = []
    grouped_keys = sorted(groups.keys(), key=lambda k: (k[0], sorted(k[1])))
    for provider, asset_set in grouped_keys:
        members = groups[(provider, asset_set)]
        if len(members) < 2:
            continue

        def rank(r):
            return (
                len(refs_by_conn.get(r["esid"]) or ()),  # referenced rows keep their identity
                _is_live(r),
                r.get("updated_at") or "",
                r["esid"],  # deterministic tiebreak
            )

        members = sorted(members, key=rank, reverse=True)
        survivor, losers = members[0], members[1:]

        token_heal_from = None
        token_heal_ambiguous = False
        if not _is_live(survivor):
            live_losers = [loser for loser in losers if _is_live(loser)]
            if len(live_losers) == 1:
                token_heal_from = live_losers[0]["esid"]
            elif len(live_losers) > 1:
                token_heal_ambiguous = True  # two live credentials — never guess

        plans.append(
            {
                "provider": provider,
                "asset_set": sorted(asset_set),
                "survivor": survivor,
                "losers": losers,
                "token_heal_from": token_heal_from,
                "token_heal_ambiguous": token_heal_ambiguous,
            }
        )

    # Overlap report: single-member asset-sets that intersect another set of
    # the same provider — the near-duplicates a strict criterion skips.
    review = []
    keys = [k for k in groups if len(groups[k]) >= 1]
    for i, (prov_a, set_a) in enumerate(keys):
        for prov_b, set_b in keys[i + 1 :]:
            if prov_a != prov_b or set_a == set_b:
                continue
            if set_a & set_b:
                review.append(
                    {
                        "provider": prov_a,
                        "connections_a": [r["esid"] for r in groups[(prov_a, set_a)]],
                        "connections_b": [r["esid"] for r in groups[(prov_b, set_b)]],
                        "shared_assets": sorted(set_a & set_b),
                    }
                )
    return {"plans": plans, "review": review}


def fetch_state(es) -> tuple[list[dict], dict, dict]:
    """Enumerate connections, their bound assets, and team references.
    Raises on any ES failure — the planner must never see a partial view."""
    resp = es.search(
        index=INDEX_CONNECTION,
        body={
            "size": 1000,
            "query": {
                "bool": {
                    "filter": [{"terms": {"provider": list(PROVIDERS)}}],
                    "must_not": [{"terms": {"status": ["superseded", "pending_token"]}}],
                }
            },
            "_source": [
                "provider",
                "principal",
                "provider_account_id",
                "status",
                "oauth_token_enc",
                "token_expires_at",
                "scopes",
                "token_kind",
                "updated_at",
            ],
        },
    )
    rows = []
    for h in resp.get("hits", {}).get("hits", []):
        src = h.get("_source") or {}
        rows.append(
            {
                "esid": h["_id"],
                "provider": src.get("provider"),
                "principal": src.get("principal"),
                "provider_account_id": src.get("provider_account_id"),
                "status": src.get("status"),
                "has_token": bool(src.get("oauth_token_enc")),
                "token_kind": src.get("token_kind"),
                "updated_at": src.get("updated_at"),
                "_token_fields": {
                    "oauth_token_enc": src.get("oauth_token_enc") or "",
                    "token_expires_at": src.get("token_expires_at"),
                    "scopes": src.get("scopes") or [],
                    "principal": src.get("principal") or "",
                    "provider_account_id": src.get("provider_account_id") or "",
                },
            }
        )

    esids = [r["esid"] for r in rows]
    assets_by_conn: dict = {e: set() for e in esids}
    if esids:
        links = es.search(
            index=INDEX_CONNECTION_ASSET,
            body={
                "size": 10000,
                "query": {"terms": {"connection_esid": esids}},
                "_source": ["connection_esid", "legacy_asset_esid"],
            },
        )
        for h in links.get("hits", {}).get("hits", []):
            src = h.get("_source") or {}
            conn = src.get("connection_esid")
            asset = (src.get("legacy_asset_esid") or "").strip()
            if conn in assets_by_conn and asset:
                assets_by_conn[conn].add(asset)

    refs_by_conn: dict = {e: [] for e in esids}
    if esids:
        should = []
        for f in TEAM_CONNECTION_FIELDS:
            should.append({"terms": {f: esids}})
            should.append({"terms": {f + ".keyword": esids}})
        teams = es.search(
            index=TEAMS_INDEX,
            body={
                "size": 10000,
                "query": {"bool": {"should": should, "minimum_should_match": 1}},
                "_source": ["team_title", *TEAM_CONNECTION_FIELDS],
            },
        )
        for h in teams.get("hits", {}).get("hits", []):
            src = h.get("_source") or {}
            for f in TEAM_CONNECTION_FIELDS:
                v = src.get(f)
                if v in refs_by_conn:
                    refs_by_conn[v].append((h["_id"], f, src.get("team_title") or ""))
    return rows, assets_by_conn, refs_by_conn


def run(es, *, apply: bool) -> dict:
    rows, assets_by_conn, refs_by_conn = fetch_state(es)
    logger.info(
        "Scanned %d Meta connection(s); %d carry bound assets.",
        len(rows),
        sum(1 for e, a in assets_by_conn.items() if a),
    )
    result = plan_groups(rows, assets_by_conn, refs_by_conn)
    plans, review = result["plans"], result["review"]

    summary = {
        "groups": len(plans),
        "superseded": 0,
        "repointed_stamps": 0,
        "token_heals": 0,
        "link_rows_deleted": 0,
        "review_clusters": len(review),
        "applied": apply,
    }
    mode = "APPLY" if apply else "DRY-RUN"

    for plan in plans:
        surv = plan["survivor"]
        logger.info(
            "%s %s group assets=%s: survivor=%s (status=%s token=%s refs=%d); superseding %s%s",
            mode,
            plan["provider"],
            plan["asset_set"],
            surv["esid"],
            surv["status"],
            surv["has_token"],
            len(refs_by_conn.get(surv["esid"]) or ()),
            [loser["esid"] for loser in plan["losers"]],
            " [TOKEN HEAL AMBIGUOUS — two live losers, not healing]" if plan["token_heal_ambiguous"] else "",
        )

        if plan["token_heal_from"]:
            summary["token_heals"] += 1
            donor = next(loser for loser in plan["losers"] if loser["esid"] == plan["token_heal_from"])
            logger.info(
                "%s   token heal: survivor %s takes credential from %s (principal=%s account=%s)",
                mode,
                surv["esid"],
                donor["esid"],
                donor["principal"],
                donor["provider_account_id"],
            )
            if apply:
                es.update(
                    index=INDEX_CONNECTION,
                    id=surv["esid"],
                    doc={
                        **donor["_token_fields"],
                        "status": "active",
                        "token_from": donor["esid"],
                        "updated_at": donor.get("updated_at"),
                    },
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
                        "superseded_reason": "duplicate connection (identical bound assets)",
                        "oauth_token_enc": "",
                    },
                    refresh="wait_for",
                )
            for team_id, field, title in refs_by_conn.get(loser["esid"]) or ():
                summary["repointed_stamps"] += 1
                logger.info("%s   team %s (%r): %s %s -> %s", mode, team_id, title, field, loser["esid"], surv["esid"])
                if apply:
                    es.update(index=TEAMS_INDEX, id=team_id, doc={field: surv["esid"]}, refresh="wait_for")
            if apply:
                deleted = es.delete_by_query(
                    index=INDEX_CONNECTION_ASSET,
                    body={"query": {"term": {"connection_esid": loser["esid"]}}},
                    refresh=True,
                    conflicts="proceed",
                )
                n = int(deleted.get("deleted", 0))
            else:
                n = len(assets_by_conn.get(loser["esid"]) or ())
            summary["link_rows_deleted"] += n
            logger.info("%s   %d link row(s) removed for %s (identical to survivor's)", mode, n, loser["esid"])

    for cluster in review:
        logger.warning(
            "REVIEW (no action): %s connections %s and %s share assets %s but bind DIFFERENT sets — inspect manually",
            cluster["provider"],
            cluster["connections_a"],
            cluster["connections_b"],
            cluster["shared_assets"],
        )

    logger.info("RESULT: %s", summary)
    return summary


def main() -> int:
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    ap = argparse.ArgumentParser(description="Dedupe phantom Meta de_connection rows.")
    ap.add_argument("--apply", action="store_true", help="write changes (default: dry-run)")
    args = ap.parse_args()
    es = get_elasticsearch_client()
    if es is None:
        logger.error("Elasticsearch unavailable")
        return 1
    try:
        run(es, apply=args.apply)
    except Exception as e:  # noqa: BLE001 — fail closed: a partial view must never drive merges
        logger.error("Aborting (no changes committed beyond those already logged): %s", e)
        return 1
    if not args.apply:
        logger.info("DRY-RUN only. Re-run with --apply to write.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
