"""Read-only audit: duplicate ``de_asset_fb_ad_accounts`` docs per ad account.

the index can legitimately hold more than one doc for the same
``fb_ad_account_id``:

* **wizard + legacy twins** (by design — two token paths; the wizard doc
  serves account-wide import via a de_connection token, the legacy doc
  serves the page-filtered chain walk via an fb_app_user token; see
  the private docs 2026-07-12),
* **wizard + wizard dupes** (a bug backlog — ``tools/dedupe_wizard_fb_ad_accounts.py``
  deletes these),
* **legacy + legacy dupes** (hand-created twice; NO tool deletes these —
  surfaced here for a human call).

This tool classifies every duplicate group and reports which docs are
load-bearing (team ``fb_ad_account_esid`` back-links, page-filtered-walk
reachability via ``fb_app_esid``, wizard-connection token path). It never
writes.

Exit codes: 0 = no duplicate groups; 1 = duplicate group(s) found;
2 = ES unavailable.

Usage::

    docker exec -w /app/python imports_container python3 \\
        -m tools.audit_fb_ad_account_twins
"""

from __future__ import annotations

import logging
import sys
from collections import defaultdict
from pathlib import Path

# Same resolver as the sibling dedupe/backfill tools.
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
from tools.dedupe_wizard_fb_ad_accounts import _team_backlink_esids  # noqa: E402

logger = logging.getLogger(__name__)

INDEX = "de_asset_fb_ad_accounts"


def _fetch_all_docs(es) -> list[dict]:
    """Return every doc in the index as ``{"_id", ...selected source fields}``.

    Index sizes are tiny (double digits per env), so one 10000-cap search
    covers it — same posture as the dedupe tool.
    """
    resp = es.search(
        index=INDEX,
        body={
            "query": {"match_all": {}},
            "size": 10000,
            "_source": [
                "fb_ad_account_id",
                "fb_ad_account_title",
                "created_by_wizard",
                "wizard_connection_esid",
                "fb_app_esid",
                "fb_app_user_esid",
                "workspace_esid",
                "team_esid",
                "@timestamp",
            ],
        },
        ignore_unavailable=True,
    )
    out = []
    for hit in resp.get("hits", {}).get("hits", []):
        src = dict(hit.get("_source") or {})
        src["_id"] = hit["_id"]
        out.append(src)
    return out


def classify_doc(src: dict) -> str:
    """Kind of a single ad-account doc, from its token-path markers.

    * ``wizard`` — ``created_by_wizard`` true, no legacy chain fields.
    * ``legacy`` — parent-chain fields (``fb_app_esid`` / ``fb_app_user_esid``)
      and NOT wizard-created.
    * ``merged`` — BOTH marker sets on one doc. No writer produces this today;
      it would put the account in both the page-filtered walk and the
      account-wide entry set of one daily run (the exact hazard the two-doc
      design avoids), so it is flagged loudly.
    * ``orphan`` — neither marker set; no token path can serve it.
    """
    is_wizard = src.get("created_by_wizard") is True
    is_legacy = bool(src.get("fb_app_esid") or src.get("fb_app_user_esid"))
    if is_wizard and is_legacy:
        return "merged"
    if is_wizard:
        return "wizard"
    if is_legacy:
        return "legacy"
    return "orphan"


def build_report(docs: list[dict], referenced: set[str]) -> dict:
    """Group docs by ``fb_ad_account_id`` and classify each duplicate group.

    Returns ``{"groups": [...], "skipped_no_id": int, "merged_docs": [...]}``.
    Each group entry::

        {
          "fb_ad_account_id": str,
          "classes": sorted kind list, e.g. ["legacy", "wizard"],
          "members": [{"_id", "kind", "ts", "title", "workspace_esid",
                       "team_referenced": bool}],
        }

    ``referenced`` is the set of esids any team ``fb_ad_account_esid``
    back-link points at (a deletion candidate that is team-referenced is
    load-bearing — see the keep-rule in the dedupe tool).
    ``merged_docs`` lists anomalous both-marker docs regardless of group size.
    """
    by_id: dict[str, list[dict]] = defaultdict(list)
    skipped_no_id = 0
    merged_docs: list[str] = []
    for src in docs:
        if classify_doc(src) == "merged":
            merged_docs.append(src["_id"])
        fid = src.get("fb_ad_account_id")
        if not fid:
            skipped_no_id += 1
            continue
        by_id[str(fid)].append(src)

    groups = []
    for fid, members in sorted(by_id.items()):
        if len(members) < 2:
            continue
        entries = []
        for m in sorted(members, key=lambda d: d.get("@timestamp") or "", reverse=True):
            entries.append(
                {
                    "_id": m["_id"],
                    "kind": classify_doc(m),
                    "ts": m.get("@timestamp") or "",
                    "title": m.get("fb_ad_account_title") or "",
                    "workspace_esid": m.get("workspace_esid") or "",
                    "team_referenced": m["_id"] in referenced,
                }
            )
        groups.append(
            {
                "fb_ad_account_id": fid,
                "classes": sorted({e["kind"] for e in entries}),
                "members": entries,
            }
        )
    return {"groups": groups, "skipped_no_id": skipped_no_id, "merged_docs": merged_docs}


_GROUP_GUIDANCE = {
    ("legacy", "wizard"): "twin BY DESIGN (two token paths) — keep both; no action",
    ("wizard",): "wizard dupes — collapse with tools/dedupe_wizard_fb_ad_accounts.py",
    ("legacy",): "legacy dupes — NO tool deletes these; needs a human call",
}


def run() -> int:
    es = get_elasticsearch_client()
    if es is None:
        logger.error("Could not connect to ES; aborting.")
        return 2

    docs = _fetch_all_docs(es)
    referenced = _team_backlink_esids(es)
    report = build_report(docs, referenced)

    logger.info("%d %s docs; %d duplicate group(s).", len(docs), INDEX, len(report["groups"]))
    if report["skipped_no_id"]:
        logger.warning("%d doc(s) have no fb_ad_account_id (ungroupable).", report["skipped_no_id"])
    for esid in report["merged_docs"]:
        logger.warning(
            "ANOMALY esid=%s carries BOTH wizard and legacy markers on one doc — "
            "no writer produces this; it double-emits in mixed daily runs. Investigate.",
            esid,
        )

    for g in report["groups"]:
        guidance = _GROUP_GUIDANCE.get(tuple(g["classes"]), "mixed/unusual — investigate")
        logger.info("")
        logger.info(
            "fb_ad_account_id=%s  [%s]  -> %s",
            g["fb_ad_account_id"],
            "+".join(g["classes"]),
            guidance,
        )
        for e in g["members"]:
            logger.info(
                "    esid=%s kind=%s ts=%s ws=%s team_referenced=%s title=%r",
                e["_id"],
                e["kind"],
                e["ts"] or "<none>",
                e["workspace_esid"] or "<none>",
                e["team_referenced"],
                e["title"],
            )

    return 1 if report["groups"] else 0


def main() -> int:
    logging.basicConfig(
        format="%(asctime)s %(levelname)s %(message)s",
        level=logging.INFO,
        stream=sys.stderr,
    )
    return run()


if __name__ == "__main__":
    raise SystemExit(main())
