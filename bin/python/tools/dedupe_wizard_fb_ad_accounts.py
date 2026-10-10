"""One-shot cleanup: de-duplicate wizard-created de_asset_fb_ad_accounts docs.

Repeated runs of the Data Connectors wizard write a fresh
``de_asset_fb_ad_accounts`` doc on every pass (random ES ``_id``, no dedup on
the natural key ``fb_ad_account_id``), so testing the wizard leaves multiple
rows for the same ad account. This tool collapses each duplicate group to a
single survivor.

Scope: ONLY docs with ``created_by_wizard == true`` are considered, so
hand-created / importer-created / legacy ad-account docs are never touched.

Keep rule: within each ``fb_ad_account_id`` group, keep the doc with the
latest ``@timestamp`` (the wizard stamps it on each write, so newest = the most
recent test run); delete the rest. Docs missing ``@timestamp`` sort oldest.

Safety:
* Dry-run by default. Use ``--apply`` to delete.
* A group of size 1 is never touched.
* Docs without ``created_by_wizard == true`` are skipped entirely (logged).
* A team back-link that points at a doomed esid is reported as a WARNING so the
  operator can re-point it — this tool does NOT rewrite team docs (that's the
   bridge's job; silently rewriting team state here is out of scope).

Usage::

    docker exec -w /app/python imports_container python3 \\
        -m tools.dedupe_wizard_fb_ad_accounts

    docker exec -w /app/python imports_container python3 \\
        -m tools.dedupe_wizard_fb_ad_accounts --apply
"""

from __future__ import annotations

import argparse
import logging
import sys
from collections import defaultdict
from pathlib import Path

# Same resolver as the sibling backfill tool.
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

INDEX = "de_asset_fb_ad_accounts"
TEAMS_INDEX = "de_asset_teams"


def _fetch_wizard_docs(es) -> list[dict]:
    """Return all created_by_wizard de_asset_fb_ad_accounts hits.

    Each entry is ``{"_id", "fb_ad_account_id", "ts", "title"}``. Beta doc
    counts are tiny (low double digits), so a single 10000-cap search covers
    it; pagination is a later cleanup if that ever changes.
    """
    resp = es.search(
        index=INDEX,
        body={
            "query": {"term": {"created_by_wizard": True}},
            "size": 10000,
        },
        ignore_unavailable=True,
    )
    out = []
    for hit in resp.get("hits", {}).get("hits", []):
        src = hit.get("_source") or {}
        out.append(
            {
                "_id": hit["_id"],
                "fb_ad_account_id": src.get("fb_ad_account_id"),
                # Missing @timestamp sorts oldest (empty string < any ISO date).
                "ts": src.get("@timestamp") or "",
                "title": src.get("fb_ad_account_title") or "",
            }
        )
    return out


def _team_backlink_esids(es) -> set[str]:
    """Return the set of fb_ad_account_esid values currently referenced by any
    team doc, so deletions that orphan a live back-link can be flagged."""
    try:
        resp = es.search(
            index=TEAMS_INDEX,
            body={
                "query": {"exists": {"field": "fb_ad_account_esid"}},
                "size": 10000,
                "_source": ["fb_ad_account_esid"],
            },
            ignore_unavailable=True,
        )
    except Exception as e:  # noqa: BLE001 — boundary; back-link check is advisory
        logger.warning("Could not read team back-links (advisory only): %s", e)
        return set()
    referenced: set[str] = set()
    for hit in resp.get("hits", {}).get("hits", []):
        val = (hit.get("_source") or {}).get("fb_ad_account_esid")
        if isinstance(val, list):
            referenced.update(v for v in val if v)
        elif val:
            referenced.add(val)
    return referenced


def plan_deletions(docs: list[dict], referenced: set[str] | None = None) -> tuple[list[dict], list[str]]:
    """Group by fb_ad_account_id; within each group pick ONE survivor.

    Keep-rule: prefer the doc a team ``fb_ad_account_esid`` back-link
    points at, if exactly one group member is referenced; otherwise fall back to
    the newest ``@timestamp``. This stops the cleanup from deleting the very doc
    a team imports off — the staging dry-run (2026-06-25) surfaced one team
    referencing an OLDER dupe than the newest, so plain newest-wins would have
    orphaned its import. The write-time guard (``_find_existing_ad_account``)
    keeps every NEW re-link pointing at the survivor; this aligns the one-shot
    backlog cleanup with that invariant.

    If MORE than one member is referenced (two teams each linking the same ad
    account to their own dupe — not seen in practice, the wizard re-points on
    re-link), we cannot collapse without orphaning a team: keep the newest of
    the referenced docs and log a WARNING so the operator re-points the loser
    before deleting.

    ``referenced`` is the set of esids any team back-link points at (from
    :func:`_team_backlink_esids`); ``None`` reverts to pure newest-wins.

    Returns ``(groups, doomed_esids)`` where ``groups`` is the per-id summary
    for logging and ``doomed_esids`` are the docs slated for deletion.
    """
    referenced = referenced or set()
    by_id: dict[str, list[dict]] = defaultdict(list)
    skipped_no_id = 0
    for d in docs:
        fid = d["fb_ad_account_id"]
        if not fid:
            # No natural key → can't safely group; leave it alone.
            skipped_no_id += 1
            continue
        by_id[str(fid)].append(d)

    if skipped_no_id:
        logger.warning("%d wizard doc(s) had no fb_ad_account_id; left untouched.", skipped_no_id)

    groups = []
    doomed: list[str] = []
    for fid, members in sorted(by_id.items()):
        # Newest first: ISO @timestamp sorts lexicographically.
        members_sorted = sorted(members, key=lambda m: m["ts"], reverse=True)
        ref_members = [m for m in members_sorted if m["_id"] in referenced]
        if ref_members:
            # Prefer the (newest) team-referenced doc as the survivor.
            keep = ref_members[0]
            if len(ref_members) > 1:
                logger.warning(
                    "id=%s: %d duplicates are each team-referenced; keeping newest "
                    "referenced esid=%s — re-point the other team(s) before --apply.",
                    fid,
                    len(ref_members),
                    keep["_id"],
                )
        else:
            keep = members_sorted[0]
        drop = [m for m in members_sorted if m["_id"] != keep["_id"]]
        groups.append({"fb_ad_account_id": fid, "keep": keep, "drop": drop})
        doomed.extend(m["_id"] for m in drop)
    return groups, doomed


def run(*, apply: bool) -> int:
    es = get_elasticsearch_client()
    if es is None:
        logger.error("Could not connect to ES; aborting.")
        return 2

    docs = _fetch_wizard_docs(es)
    logger.info("Found %d created_by_wizard %s docs.", len(docs), INDEX)
    if not docs:
        logger.info("Nothing to do.")
        return 0

    # Read team back-links BEFORE planning so the keep-rule can prefer the
    # team-referenced survivor rather than blindly keeping newest.
    referenced = _team_backlink_esids(es)
    groups, doomed = plan_deletions(docs, referenced)
    dup_groups = [g for g in groups if g["drop"]]

    logger.info("%d distinct ad-account id(s); %d have duplicates.", len(groups), len(dup_groups))

    for g in dup_groups:
        keep = g["keep"]
        logger.info(
            "  id=%s: keep esid=%s (ts=%s, %r); delete %d:",
            g["fb_ad_account_id"],
            keep["_id"],
            keep["ts"] or "<none>",
            keep["title"],
            len(g["drop"]),
        )
        for m in g["drop"]:
            warn = "  <-- TEAM BACK-LINK POINTS HERE; re-point the team first" if m["_id"] in referenced else ""
            logger.info("      esid=%s (ts=%s)%s", m["_id"], m["ts"] or "<none>", warn)

    if not doomed:
        logger.info("No duplicates to delete.")
        return 0

    if not apply:
        logger.info("")
        logger.info("DRY-RUN: %d doc(s) would be deleted. Pass --apply to delete.", len(doomed))
        return 0

    deleted = 0
    for esid in doomed:
        try:
            es.delete(index=INDEX, id=esid, refresh="wait_for")
            deleted += 1
        except Exception as e:  # noqa: BLE001 — per-doc boundary; keep going
            logger.error("    FAILED delete %s/%s: %s", INDEX, esid, e)
    logger.info("APPLY: deleted=%d / planned=%d.", deleted, len(doomed))
    return 0 if deleted == len(doomed) else 1


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--apply", action="store_true", help="Delete the duplicates. Default is dry-run.")
    args = parser.parse_args(argv)

    logging.basicConfig(
        format="%(asctime)s %(levelname)s %(message)s",
        level=logging.INFO,
        stream=sys.stderr,
    )
    return run(apply=args.apply)


if __name__ == "__main__":
    raise SystemExit(main())
