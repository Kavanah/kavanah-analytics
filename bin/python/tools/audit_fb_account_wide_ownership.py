"""Read-only audit: FB ad accounts imported account-wide by a team that is
not their registered owner.

 (FB sibling to YouTube ownership audit). An account-wide
FB team imports EVERY ad in the ad accounts inside its resolved scope
(``resolve_team_filter_scope`` -> ``fb_import_mode == 'account_wide'``), and the
per-ad owner resolver stamps each ad's ``team_esid`` from the ad-account's
registered ``de_asset_fb_ad_accounts`` doc. When a team's
*resolved* account-wide scope includes an ad account whose registered
``team_esid`` is a DIFFERENT team, that team imports and writes docs for an
account it does not own — the importing team's ``{esid}__fb_insights`` index
fills with another team's ad data, and neither team's own page delivered any of
it. (Found 2026-07-14: one team (zU_525...) resolved account-wide to a sibling
team's ad account ``123456789012345``, whose only registered owner doc is that
sibling team (0Hsphp...); both teams wrote the same 26 daily docs into their
separate indices.)

Token auth is NOT the failure here — every token authenticates. Yesterday's
``audit_youtube_account_ownership`` checks token-vs-linked-channel scope; this
tool checks a different axis: registration-vs-resolution. It answers "does each
ad account get imported account-wide by exactly one team, and is that team its
registered owner?" — the question the import logs cannot answer on their own.

For each ad account (grouped by ``fb_ad_account_id``), classify by comparing
its registered owner set against the set of account-wide teams whose resolved
scope claims it:

* **healthy** — claimed account-wide by exactly the team(s) registered as its
  owner (or claimed by no account-wide team at all: nothing imports it).
* **contended** — claimed account-wide by a team that is NOT a registered
  owner. That team writes foreign ad data into its own index. This is the
  bug the tool exists to catch.
* **unowned** — claimed account-wide by some team but has NO registered
  ``team_esid`` on any of its docs (legacy-only twin with no team back-link);
  the import runs but attribution rests on the resolver's fallback, not a
  registration. Informational — a human decides whether to register an owner.

Read-only: never writes ES, never touches tokens or team links.

Exit codes: 0 = no contended accounts (unowned is not a failure);
1 = at least one contended account; 2 = ES unavailable.

Usage (inside imports_container)::

    docker exec -w /app/python imports_container python3 \\
        -m tools.audit_fb_account_wide_ownership
"""

from __future__ import annotations

import logging
import sys
from collections import defaultdict
from pathlib import Path
from typing import Any, Callable

# Same three-candidate resolver as the sibling FB/YT audit tools.
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
from tools.audit_fb_ad_account_twins import _fetch_all_docs  # noqa: E402

logger = logging.getLogger(__name__)

TEAMS_INDEX = "de_asset_teams"


def _account_wide_scope(es: Any) -> dict[str, str]:
    """``{team_esid: team_title}`` for every account-wide FB team.

    An account-wide team imports EVERY ad in the ad accounts its scope
    resolves to; a page-filtered team only imports ads on its own pages, so it
    cannot contend for an account it doesn't own. We therefore only need the
    account-wide teams here.
    """
    resp = es.search(
        index=TEAMS_INDEX,
        body={
            "query": {"term": {"fb_import_mode": "account_wide"}},
            "size": 10000,
            "_source": ["team_title", "fb_ad_account_esid"],
        },
        ignore_unavailable=True,
    )
    out: dict[str, str] = {}
    for hit in resp.get("hits", {}).get("hits", []):
        out[hit["_id"]] = (hit.get("_source") or {}).get("team_title") or hit["_id"]
    return out


def _account_wide_claims(es: Any) -> dict[str, set[str]]:
    """``{fb_ad_account_id: {team_esid, ...}}`` — which account-wide teams
    resolve each ad account into their scope.

    An account-wide team's scope is the set of ad accounts whose
    ``de_asset_fb_ad_accounts`` doc it references via ``fb_ad_account_esid``,
    OR (the failure mode this audit exists for) any account the scope resolver
    hands it. The registered-owner truth we compare against lives on the
    ad-account doc's own ``team_esid``; the claim side is the team back-link.
    We build claims from the team side so a team claiming an account it does
    not own still shows up.
    """
    account_wide = _account_wide_scope(es)
    # Map every ad-account doc _id -> its fb_ad_account_id so a team's
    # fb_ad_account_esid back-links resolve to the natural account key.
    docs = _fetch_all_docs(es)
    esid_to_id = {d["_id"]: str(d["fb_ad_account_id"]) for d in docs if d.get("fb_ad_account_id")}

    claims: dict[str, set[str]] = defaultdict(set)
    resp = es.search(
        index=TEAMS_INDEX,
        body={
            "query": {"term": {"fb_import_mode": "account_wide"}},
            "size": 10000,
            "_source": ["fb_ad_account_esid"],
        },
        ignore_unavailable=True,
    )
    for hit in resp.get("hits", {}).get("hits", []):
        team_esid = hit["_id"]
        if team_esid not in account_wide:
            continue
        raw = (hit.get("_source") or {}).get("fb_ad_account_esid")
        backlinks = raw if isinstance(raw, list) else ([raw] if raw else [])
        for acct_esid in backlinks:
            acct_id = esid_to_id.get(acct_esid)
            if acct_id:
                claims[acct_id].add(team_esid)
    return dict(claims)


def _registered_owners(docs: list[dict]) -> dict[str, set[str]]:
    """``{fb_ad_account_id: {team_esid, ...}}`` — the team(s) each ad account's
    own docs register as owner (the ``team_esid`` on the ad-account doc)."""
    owners: dict[str, set[str]] = defaultdict(set)
    for src in docs:
        fid = src.get("fb_ad_account_id")
        team = src.get("team_esid")
        if fid and team:
            owners[str(fid)].add(team)
    return dict(owners)


def classify_account(owners: set[str], claimants: set[str]) -> tuple[str, set[str]]:
    """Classify one ad account from its registered owners vs account-wide
    claimants. Returns ``(kind, contenders)`` where ``contenders`` is the subset
    of claimants that are NOT registered owners (empty unless kind ==
    "contended").

    * no claimant -> healthy (nothing imports it account-wide).
    * every claimant is a registered owner -> healthy.
    * a claimant is not a registered owner:
        - if there ARE registered owners -> contended (the bug).
        - if there are NO registered owners -> unowned (informational).
    """
    if not claimants:
        return "healthy", set()
    contenders = claimants - owners
    if not contenders:
        return "healthy", set()
    if owners:
        return "contended", contenders
    return "unowned", contenders


def build_report(
    account_ids: list[str],
    owners_of: Callable[[str], set[str]],
    claimants_of: Callable[[str], set[str]],
    title_of: Callable[[str], str],
) -> dict[str, Any]:
    """Pure report builder — I/O injected so it is unit-testable without ES.

    Returns::

        {"accounts": [{"fb_ad_account_id", "kind", "owners", "claimants",
                       "contenders"}], "counts": {kind: n}}
    """
    rows = []
    counts: dict[str, int] = {"healthy": 0, "contended": 0, "unowned": 0}
    for acct_id in sorted(account_ids):
        owners = owners_of(acct_id)
        claimants = claimants_of(acct_id)
        kind, contenders = classify_account(owners, claimants)
        counts[kind] = counts.get(kind, 0) + 1
        rows.append(
            {
                "fb_ad_account_id": acct_id,
                "title": title_of(acct_id),
                "kind": kind,
                "owners": sorted(owners),
                "claimants": sorted(claimants),
                "contenders": sorted(contenders),
            }
        )
    return {"accounts": rows, "counts": counts}


def run() -> int:
    es = get_elasticsearch_client()
    if es is None:
        logger.error("Could not connect to ES; aborting.")
        return 2

    docs = _fetch_all_docs(es)
    owners = _registered_owners(docs)
    claims = _account_wide_claims(es)
    titles = {str(d["fb_ad_account_id"]): d.get("fb_ad_account_title") or "" for d in docs if d.get("fb_ad_account_id")}

    # Every ad account that either has a doc or is claimed by some team.
    account_ids = sorted(set(titles) | set(owners) | set(claims))

    report = build_report(
        account_ids,
        owners_of=lambda a: owners.get(a, set()),
        claimants_of=lambda a: claims.get(a, set()),
        title_of=lambda a: titles.get(a, ""),
    )
    counts = report["counts"]

    logger.info(
        "%d ad account(s) — healthy=%d contended=%d unowned=%d",
        len(account_ids),
        counts.get("healthy", 0),
        counts.get("contended", 0),
        counts.get("unowned", 0),
    )

    for row in report["accounts"]:
        if row["kind"] == "contended":
            logger.info("")
            logger.warning(
                "CONTENDED fb_ad_account_id=%s title=%r — registered owner(s) %s but "
                "also imported account-wide by non-owner team(s) %s; the non-owner "
                "team's {esid}__fb_insights fills with this account's ads.",
                row["fb_ad_account_id"],
                row["title"],
                row["owners"],
                row["contenders"],
            )
            logger.warning(
                "    fix: remove fb_ad_account_esid back-link to %s from the non-owner "
                "team(s), or re-register ownership if the sharing is intentional.",
                row["fb_ad_account_id"],
            )
        elif row["kind"] == "unowned":
            logger.warning(
                "UNOWNED  fb_ad_account_id=%s title=%r — imported account-wide by %s but "
                "no ad-account doc registers a team_esid owner; attribution rests on the "
                "resolver fallback, not registration.",
                row["fb_ad_account_id"],
                row["title"],
                row["claimants"],
            )

    return 1 if counts.get("contended", 0) else 0


def main() -> int:
    logging.basicConfig(
        format="%(asctime)s %(levelname)s %(message)s",
        level=logging.INFO,
        stream=sys.stderr,
    )
    return run()


if __name__ == "__main__":
    raise SystemExit(main())
