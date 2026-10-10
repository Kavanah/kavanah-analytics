"""Read-only audit: which prod ads would be DROPPED by the fix.

the gate on shipping to ``main``.

 changed the second-chance account resolver
(:func:`~import_scripts.facebook.get_insights.get_account_wide_teams_by_ad_account_id`).
Before, an ad account claimed account-wide by two teams resolved to the lowest
team ``_id``; now it resolves to ``None`` (AMBIGUOUS), and an ad that reaches
the fallback there is dropped with a user-facing error instead of guessed onto a
team. Sharing an ad account is the expected Meta topology (rule 6 — many Ad
Accounts per Business Portfolio; rule 7 — one Ad Account per Ad; rule 10 — Ads
for the SAME Page can run from DIFFERENT Ad Accounts), so the account cannot
identify a team; only the Ad's Page can (rule 9 -> rule 1).

That is strictly safer — guessing writes one team's ads into another team's
index, a cross-tenant error — but it is not free: an ad whose Page is linked to
NO team USED to land somewhere (wrongly) and will now not land at all. This
tool answers the only question that gates the merge:

    On today's production data, how many ads would stop being imported,
    and were any of them being attributed CORRECTLY before?

There is no staging environment able to reproduce this (it needs two teams
sharing one ad account AND an ad whose Page is unlinked), so this audit is the
sole pre-merge evidence.

**It calls no Facebook API.** It reasons over already-imported
``*__fb_insights`` docs, which record each ad's ``page_id`` and
``ad_account_id`` — the two inputs the resolver uses. Every such doc is an ad
the importer DID attribute, so the historical record is exactly the population
at risk.

The tool deliberately imports the importer's real resolver functions rather
than reimplementing them, so the audit cannot drift from production behavior.

Per shared ad account, each Page seen in its history is classified:

* **safe** — the Page is linked to a team, so ``_resolve_team_for_page_ad``
  attributes its ads BEFORE the account fallback is consulted. Unaffected by
  .
* **will_drop** — the Page is linked to NO team, so its ads reached the account
  fallback. These ads stop importing. For each, the tool reports which team the
  docs currently sit under and whether the OLD tie-break would have chosen that
  same team, which splits them into:
    - *was_misattributed* — the old guess put them under a team that does not
      own the Page. fixes a live cross-tenant error here; dropping is
      an improvement.
    - *was_correct_by_luck* — the old guess happened to match. These are the
      only genuine regressions, and each needs its Page linked to keep
      importing.

Exit codes: 0 = no ads would drop (safe to merge); 1 = ads would drop (read the
report before merging); 2 = ES unavailable.

Usage (inside imports_container, on prod)::

    docker exec -w /app/python imports_container python3 \\
        -m tools.audit_fb_shared_account_dropped_ads

Add ``--days N`` to widen/narrow the history window (default 90) and
``--json`` to emit the machine-readable report on stdout for attaching to the
ticket.
"""

from __future__ import annotations

import argparse
import json
import logging
import sys
from collections import defaultdict
from pathlib import Path
from typing import Any, Callable

# Same three-candidate resolver as the sibling FB audit tools.
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

logger = logging.getLogger(__name__)

TEAMS_INDEX = "de_asset_teams"
PAGES_INDEX = "de_asset_fb_pages"
INSIGHTS_PATTERN = "*__fb_insights"


# ─────────────────────────────────────────────────────────────────────────────
# Pure classification — I/O injected so this is unit-testable without ES.
# ─────────────────────────────────────────────────────────────────────────────


def old_tie_break(claimants: set[str]) -> str | None:
    """The PRE- winner for a shared account: lowest team ``_id``.

    Reproduced here (rather than imported) precisely because deleted
    it — this is the only place the old behavior still exists, and the audit
    needs it to decide whether a drop is a regression or a fix.
    """
    return min(claimants) if claimants else None


def classify_page(
    page_id: str,
    page_team: str | None,
    doc_teams: set[str],
    claimants: set[str],
) -> dict[str, Any]:
    """Classify one Page inside one shared ad account.

    Args:
        page_id: the Facebook page id seen on the ads.
        page_team: team esid the Page is linked to, or None if unlinked.
        doc_teams: team esids whose indices currently hold ads for this
            (account, page) pair — i.e. where the old code actually put them.
        claimants: the account-wide teams sharing this ad account.

    Returns a row dict with ``kind`` in {safe, was_misattributed,
    was_correct_by_luck}.
    """
    if page_team:
        # The Page decides (rules 9 + 1) and it resolves BEFORE the account
        # fallback, so cannot affect these ads.
        return {
            "fb_page_id": page_id,
            "kind": "safe",
            "page_team_esid": page_team,
            "doc_teams": sorted(doc_teams),
            "old_winner": None,
        }

    old_winner = old_tie_break(claimants)
    # The old guess was "correct" only if the team it chose is in fact where the
    # docs sit AND nothing contradicts it. With no Page link there is no ground
    # truth for ownership, so "correct by luck" is the weaker claim: the old
    # winner is the sole team holding these docs.
    correct_by_luck = old_winner is not None and doc_teams == {old_winner}
    return {
        "fb_page_id": page_id,
        "kind": "was_correct_by_luck" if correct_by_luck else "was_misattributed",
        "page_team_esid": None,
        "doc_teams": sorted(doc_teams),
        "old_winner": old_winner,
    }


def build_report(
    shared_accounts: dict[str, set[str]],
    pages_of_account: Callable[[str], dict[str, set[str]]],
    page_team_of: Callable[[str], str | None],
    ad_counts_of: Callable[[str, str], int],
) -> dict[str, Any]:
    """Pure report builder over injected I/O.

    Args:
        shared_accounts: ``{fb_ad_account_id: {claimant_team_esid, ...}}`` for
            accounts claimed account-wide by 2+ teams (the ambiguous set).
        pages_of_account: account id -> ``{fb_page_id: {team_esid holding docs}}``
        page_team_of: page id -> linked team esid or None
        ad_counts_of: (account id, page id) -> number of insight docs

    Returns ``{"accounts": [...], "counts": {...}, "ads_at_risk": n}``.
    """
    rows: list[dict[str, Any]] = []
    counts: dict[str, int] = {"safe": 0, "was_misattributed": 0, "was_correct_by_luck": 0}
    ads_at_risk = 0

    for acct_id in sorted(shared_accounts):
        claimants = shared_accounts[acct_id]
        page_rows = []
        for page_id, doc_teams in sorted(pages_of_account(acct_id).items()):
            row = classify_page(page_id, page_team_of(page_id), doc_teams, claimants)
            row["ad_doc_count"] = ad_counts_of(acct_id, page_id)
            counts[row["kind"]] = counts.get(row["kind"], 0) + 1
            if row["kind"] != "safe":
                ads_at_risk += row["ad_doc_count"]
            page_rows.append(row)
        rows.append(
            {
                "fb_ad_account_id": acct_id,
                "claimant_team_esids": sorted(claimants),
                "pages": page_rows,
            }
        )

    return {"accounts": rows, "counts": counts, "ads_at_risk": ads_at_risk}


# ─────────────────────────────────────────────────────────────────────────────
# ES reads
# ─────────────────────────────────────────────────────────────────────────────


def _shared_accounts(es: Any) -> dict[str, set[str]]:
    """``{fb_ad_account_id: {team_esid, ...}}`` for accounts shared by 2+ teams.

    Built by re-running the importer's own resolver twice: once normally (which
    now returns ``None`` for every ambiguous account — that IS the set we want)
    and once per team in isolation to recover WHICH teams claim each. Using the
    real resolver means the audit inherits every guard the importer applies
    (doc guard link windows entitlement), so it
    cannot report an account the importer would never have mapped.
    """
    import import_scripts.facebook.get_insights as fbi

    full = fbi.get_account_wide_teams_by_ad_account_id()
    ambiguous = {acct for acct, team in full.items() if team is None}
    if not ambiguous:
        return {}

    # Recover the claimants: resolve one team at a time, so nothing collides.
    resp = es.search(
        index=TEAMS_INDEX,
        body={
            "query": {
                "bool": {
                    "must": [
                        {"term": {"fb_import_mode": "account_wide"}},
                        {"term": {"ds_facebook": "true"}},
                    ]
                }
            },
            "size": 10000,
            "_source": ["team_title"],
        },
        ignore_unavailable=True,
    )
    claimants: dict[str, set[str]] = defaultdict(set)
    for hit in resp.get("hits", {}).get("hits", []):
        team_esid = hit["_id"]
        solo = fbi.get_account_wide_teams_by_ad_account_id(team_esids=[team_esid])
        for acct_id, team in solo.items():
            if acct_id in ambiguous and team is not None:
                claimants[acct_id].add(team_esid)
    return {acct: claimants.get(acct, set()) for acct in sorted(ambiguous)}


def _page_to_team(es: Any) -> dict[str, str]:
    """``{fb_page_id: team_esid}`` — Page linkage, the real attribution authority.

    Read from the team side (``team_facebook_page_id``), which is what
    :func:`get_all_teams_by_page_id_multi` reads, so "unlinked" here means
    exactly "unlinked as the importer sees it".
    """
    import import_scripts.facebook.get_insights as fbi

    multimap = fbi.get_all_teams_by_page_id_multi()
    out: dict[str, str] = {}
    for page_id, teams in multimap.items():
        if teams:
            # Any claimant means "linked" for our purposes; the per-ad
            # tie-break decides which, and it never reaches the fallback.
            out[str(page_id)] = str(teams[0].get("_id") or "")
    return out


def _insight_pages(es: Any, account_ids: list[str], days: int) -> tuple[dict, dict]:
    """Scan ``*__fb_insights`` for the given accounts.

    Returns ``(pages_by_account, counts)`` where
    ``pages_by_account[acct][page_id] = {team_esid, ...}`` (teams whose docs
    hold that account+page) and ``counts[(acct, page_id)] = doc count``.

    The ad's page is stored as ``page_id`` (NOT ``fb_page_id`` — that is the
    asset-index field name; using it here silently matches nothing and the
    audit would report a false all-clear). The holding team comes from the
    doc's own ``team_esid``, which is what the importer stamped.
    """
    pages_by_account: dict[str, dict[str, set[str]]] = defaultdict(lambda: defaultdict(set))
    counts: dict[tuple[str, str], int] = defaultdict(int)
    if not account_ids:
        return pages_by_account, counts

    resp = es.search(
        index=INSIGHTS_PATTERN,
        body={
            "size": 0,
            "query": {
                "bool": {
                    "filter": [
                        {"terms": {"ad_account_id": account_ids}},
                        {"range": {"kibana_date": {"gte": f"now-{days}d/d"}}},
                    ]
                }
            },
            "aggs": {
                "by_team": {
                    "terms": {"field": "team_esid", "size": 500},
                    "aggs": {
                        "by_account": {
                            "terms": {"field": "ad_account_id", "size": 200},
                            "aggs": {"by_page": {"terms": {"field": "page_id", "size": 500}}},
                        }
                    },
                }
            },
        },
        ignore_unavailable=True,
    )

    for idx_bucket in resp.get("aggregations", {}).get("by_team", {}).get("buckets", []):
        team_esid = str(idx_bucket.get("key") or "")
        for acct_bucket in idx_bucket.get("by_account", {}).get("buckets", []):
            acct_id = str(acct_bucket.get("key") or "")
            for page_bucket in acct_bucket.get("by_page", {}).get("buckets", []):
                page_id = str(page_bucket.get("key") or "")
                pages_by_account[acct_id][page_id].add(team_esid)
                counts[(acct_id, page_id)] += int(page_bucket.get("doc_count") or 0)

    return pages_by_account, counts


def _team_titles(es: Any) -> dict[str, str]:
    resp = es.search(
        index=TEAMS_INDEX,
        body={"query": {"match_all": {}}, "size": 10000, "_source": ["team_title"]},
        ignore_unavailable=True,
    )
    return {h["_id"]: (h.get("_source") or {}).get("team_title") or h["_id"] for h in resp.get("hits", {}).get("hits", [])}


def _page_names(es: Any) -> dict[str, str]:
    resp = es.search(
        index=PAGES_INDEX,
        body={"query": {"match_all": {}}, "size": 10000, "_source": ["fb_page_id", "fb_page_name"]},
        ignore_unavailable=True,
    )
    out: dict[str, str] = {}
    for hit in resp.get("hits", {}).get("hits", []):
        src = hit.get("_source") or {}
        if src.get("fb_page_id"):
            out[str(src["fb_page_id"])] = src.get("fb_page_name") or ""
    return out


# ─────────────────────────────────────────────────────────────────────────────
# Reporting
# ─────────────────────────────────────────────────────────────────────────────


def run(days: int = 90, as_json: bool = False) -> int:
    es = get_elasticsearch_client()
    if es is None:
        logger.error("Could not connect to ES; aborting.")
        return 2

    shared = _shared_accounts(es)
    if not shared:
        logger.info("No ad account is claimed account-wide by more than one team.")
        logger.info("is INERT on current production data — no ad can reach the ambiguous fallback.")
        if as_json:
            print(json.dumps({"accounts": [], "counts": {}, "ads_at_risk": 0}, indent=2))
        return 0

    titles = _team_titles(es)
    page_names = _page_names(es)
    pages_by_account, counts_map = _insight_pages(es, sorted(shared), days)
    page_team = _page_to_team(es)

    report = build_report(
        shared,
        pages_of_account=lambda a: {p: set(t) for p, t in pages_by_account.get(a, {}).items()},
        page_team_of=lambda p: page_team.get(p),
        ad_counts_of=lambda a, p: counts_map.get((a, p), 0),
    )
    counts = report["counts"]

    def _team(esid: str) -> str:
        return f"{titles.get(esid, esid)} ({esid})"

    def _page(pid: str) -> str:
        name = (page_names.get(pid) or "").strip()
        return f"{pid} ({name})" if name else pid

    logger.info("")
    logger.info("=" * 78)
    logger.info(" blast-radius audit (last %d days, read-only)", days)
    logger.info("=" * 78)
    logger.info(
        "%d shared ad account(s); pages: safe=%d was_misattributed=%d was_correct_by_luck=%d",
        len(report["accounts"]),
        counts.get("safe", 0),
        counts.get("was_misattributed", 0),
        counts.get("was_correct_by_luck", 0),
    )
    logger.info("%d insight doc(s) would no longer be imported.", report["ads_at_risk"])

    for acct in report["accounts"]:
        logger.info("")
        logger.info(
            "ad account %s shared by: %s",
            acct["fb_ad_account_id"],
            ", ".join(_team(t) for t in acct["claimant_team_esids"]),
        )
        if not acct["pages"]:
            logger.info("    no imported ads in the window — nothing at risk.")
            continue
        for row in acct["pages"]:
            if row["kind"] == "safe":
                logger.info(
                    "    SAFE                page %s -> %s (%d docs) — page linkage decides; unaffected.",
                    _page(row["fb_page_id"]),
                    _team(row["page_team_esid"] or ""),
                    row["ad_doc_count"],
                )
            elif row["kind"] == "was_misattributed":
                logger.warning(
                    "    FIXES A BUG         page %s is linked to NO team; its %d doc(s) sit under %s "
                    "while the old tie-break chose %s — the ads were being written to a team that does "
                    "not own the page (cross-tenant).  drops them instead.",
                    _page(row["fb_page_id"]),
                    row["ad_doc_count"],
                    ", ".join(_team(t) for t in row["doc_teams"]) or "no index",
                    _team(row["old_winner"] or ""),
                )
            else:
                logger.warning(
                    "    REGRESSION RISK     page %s is linked to NO team; its %d doc(s) currently land "
                    "under %s and will STOP importing. Link this page to its owning team to keep them.",
                    _page(row["fb_page_id"]),
                    row["ad_doc_count"],
                    _team(row["old_winner"] or ""),
                )

    logger.info("")
    logger.info("-" * 78)
    if counts.get("was_correct_by_luck", 0):
        logger.warning(
            "VERDICT: %d page(s) would lose import coverage. Link each page above to its "
            "owning team (DEAM -> Asset Explorer -> Team -> Facebook pages) BEFORE merging "
            ", or accept the gap knowingly.",
            counts["was_correct_by_luck"],
        )
    elif counts.get("was_misattributed", 0):
        logger.info(
            "VERDICT: every affected page was being MISATTRIBUTED.  replaces a silent "
            "cross-tenant write with a visible, recoverable drop — safe to merge."
        )
    else:
        logger.info("VERDICT: no ad reaches the ambiguous fallback —  is inert. Safe to merge.")

    if as_json:
        print(json.dumps(report, indent=2, sort_keys=True))

    return 1 if report["ads_at_risk"] else 0


def main() -> int:
    parser = argparse.ArgumentParser(description="read-only audit of the  shared-account drop.")
    parser.add_argument("--days", type=int, default=90, help="history window to scan (default 90)")
    parser.add_argument("--json", action="store_true", dest="as_json", help="emit the JSON report on stdout")
    args = parser.parse_args()

    logging.basicConfig(format="%(asctime)s %(levelname)s %(message)s", level=logging.INFO, stream=sys.stderr)
    return run(days=args.days, as_json=args.as_json)


if __name__ == "__main__":
    raise SystemExit(main())
