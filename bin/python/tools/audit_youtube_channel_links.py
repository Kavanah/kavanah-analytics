"""Read-only audit of the CHANNEL side of the YouTube asset graph.

. The two existing audits both walk accounts -> channels, so
they are structurally blind to defects that live on the channel rows themselves:

* ``audit_youtube_account_ownership`` — per account: does its token own its
  linked channels?
* ``audit_youtube_duplicate_accounts`` — per account pair: same identity?
* ``diagnose_youtube_accounts`` — per account: token/identity/links/delete-safety.

Walking channels -> accounts instead surfaces three things none of them can:

* **unlinked** — a channel doc with no ``youtube_account_esid``. It has no
  account, therefore no token, therefore can never import — and because every
  other audit enumerates accounts first, nothing reports it. Found on prod
  2026-07-31 (Prabhu Yeshu Ke Saath had one).
* **dangling** — ``youtube_account_esid`` points at an account doc that does not
  exist. Same silent-no-import failure, usually from a deleted account doc.
* **duplicate** — the same ``youtube_channel_id`` in more than one channel doc.
  When the copies name DIFFERENT accounts, which one the importer honors depends
  on scan order, and one of them is doing nothing. This is also what makes an
  account doc look delete-safe when it is not.

Also reports, per account, how many channels reference it — the "which account
goes with which channel" mapping that Asset Explorer does not surface (the
 label ambiguity, closed Won't-do).

Read-only: never writes ES, never touches tokens or links. Makes NO Google API
calls, so it costs no quota and cannot be rate-limited.

Each duplicate copy is listed with the teams that link it, so the
report says which team loses what before anyone deletes a copy.

``--duplicates-only`` narrows the report and the exit code to duplicates. It is
the daily scheduled check (, ``youtube_duplicate_check`` in
deployment/03_provision_cron_jobs.sh, run through tools/run_tracked_job.py so a
failure reaches the superuser banner). Unlinked and dangling rows are real but
older defects, and a check that is red for them every day would hide a new
duplicate. The last line printed is always the one-line summary the banner shows.

Exit codes: 0 = no channel-side defects (with ``--duplicates-only``: no
duplicates); 1 = at least one unlinked, dangling, or duplicate channel row (with
``--duplicates-only``: at least one duplicate); 2 = ES unavailable.

Usage (inside imports_container)::

    docker exec -w /app/python imports_container python3 \\
        -m tools.audit_youtube_channel_links [--duplicates-only]
"""

from __future__ import annotations

import argparse
import logging
import sys
from collections import defaultdict
from pathlib import Path
from typing import Any

# Same three-candidate resolver as the sibling YouTube tools.
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
from tools.audit_youtube_account_ownership import _fetch_all_accounts  # noqa: E402
from tools.split_youtube_account import ACCOUNTS_INDEX, CHANNELS_INDEX  # noqa: E402

logger = logging.getLogger(__name__)

TEAMS_INDEX = "de_asset_teams"


def _fetch_all_channels(es: Any) -> list[dict[str, Any]]:
    """Every de_asset_youtube_channels doc as ``{"_id", ...source}``."""
    resp = es.search(
        index=CHANNELS_INDEX,
        body={"query": {"match_all": {}}, "size": 10000},
        ignore_unavailable=True,
    )
    out = []
    for hit in resp.get("hits", {}).get("hits", []):
        src = dict(hit.get("_source") or {})
        src["_id"] = hit["_id"]
        out.append(src)
    return out


def _fetch_team_links(es: Any) -> dict[str, list[dict[str, str]]]:
    """``{channel_esid: [{"_id", "title"}]}`` for every team linking a channel,
    through the esid list or the legacy scalar ``youtube_channel_esid``."""
    resp = es.search(
        index=TEAMS_INDEX,
        body={
            "query": {"match_all": {}},
            "size": 10000,
            "_source": ["team_title", "team_youtube_channel_handle_esids", "youtube_channel_esid"],
        },
        ignore_unavailable=True,
    )
    links: dict[str, list[dict[str, str]]] = defaultdict(list)
    for hit in resp.get("hits", {}).get("hits", []):
        src = hit.get("_source") or {}
        esids = src.get("team_youtube_channel_handle_esids") or []
        if isinstance(esids, str):
            esids = [esids]
        linked = {str(e) for e in esids if e}
        if src.get("youtube_channel_esid"):
            linked.add(str(src["youtube_channel_esid"]))
        for esid in sorted(linked):
            links[esid].append({"_id": hit["_id"], "title": src.get("team_title") or hit["_id"]})
    return dict(links)


def build_report(
    channels: list[dict[str, Any]],
    account_ids: set[str],
    team_links: dict[str, list[dict[str, str]]] | None = None,
) -> dict[str, Any]:
    """Pure report builder — inputs are plain lists/sets so this is unit-testable.

    ``team_links`` (``{channel_esid: [{"_id", "title"}]}``) puts each row's
    linking teams on it as ``teams``; without it every row has ``teams == []``.

    Returns ``{"unlinked": [...], "dangling": [...], "duplicates": {cid: [...]},
    "by_account": {esid: [...]}, "counts": {...}}``.
    """
    unlinked: list[dict[str, Any]] = []
    dangling: list[dict[str, Any]] = []
    by_channel_id: dict[str, list[dict[str, Any]]] = defaultdict(list)
    by_account: dict[str, list[dict[str, Any]]] = defaultdict(list)

    for ch in channels:
        row = {
            "_id": ch["_id"],
            "channel_id": str(ch.get("youtube_channel_id") or "").strip(),
            "title": ch.get("youtube_channel_title") or "",
            "account_esid": str(ch.get("youtube_account_esid") or "").strip(),
            "account_title": ch.get("youtube_account_title") or "",
            "workspace_esid": ch.get("workspace_esid") or "",
            "teams": list((team_links or {}).get(ch["_id"], [])),
        }
        if row["channel_id"]:
            by_channel_id[row["channel_id"]].append(row)

        if not row["account_esid"]:
            unlinked.append(row)
            continue
        by_account[row["account_esid"]].append(row)
        # An esid that names no existing account doc — the channel points into
        # the void and can never import.
        if row["account_esid"] not in account_ids:
            dangling.append(row)

    duplicates = {cid: rows for cid, rows in by_channel_id.items() if len(rows) > 1}
    return {
        "unlinked": unlinked,
        "dangling": dangling,
        "duplicates": duplicates,
        "by_account": dict(by_account),
        "counts": {
            "channels": len(channels),
            "unlinked": len(unlinked),
            "dangling": len(dangling),
            "duplicate_channel_ids": len(duplicates),
        },
    }


def _print_summary(line: str) -> None:
    """The run's last line, printed bare (no log prefix): run_tracked_job records
    the last line as the job summary, and the superuser banner shows it as-is."""
    sys.stderr.flush()
    sys.stdout.write(line + "\n")
    sys.stdout.flush()


def _summary(counts: dict[str, int], duplicates_only: bool) -> str:
    if duplicates_only:
        return f"youtube duplicate check: {counts['duplicate_channel_ids']} channel id(s) held by more than one record"
    return (
        f"youtube channel links: unlinked={counts['unlinked']} dangling={counts['dangling']} "
        f"duplicate_channel_ids={counts['duplicate_channel_ids']}"
    )


def run(duplicates_only: bool = False) -> int:
    es = get_elasticsearch_client()
    if es is None:
        logger.error("Could not connect to ES; aborting.")
        return 2

    accounts = _fetch_all_accounts(es)
    account_title = {a["_id"]: (a.get("youtube_account_title") or a["_id"]) for a in accounts}
    channels = _fetch_all_channels(es)
    report = build_report(channels, set(account_title), _fetch_team_links(es))
    c = report["counts"]
    if duplicates_only:
        _log_duplicates(report, account_title)
        logger.info("")
        _print_summary(_summary(c, duplicates_only=True))
        return 1 if c["duplicate_channel_ids"] else 0

    logger.info(
        "%d %s doc(s), %d %s doc(s) — unlinked=%d dangling=%d duplicate_channel_ids=%d",
        len(accounts),
        ACCOUNTS_INDEX,
        c["channels"],
        CHANNELS_INDEX,
        c["unlinked"],
        c["dangling"],
        c["duplicate_channel_ids"],
    )

    for row in report["unlinked"]:
        logger.info("")
        logger.warning(
            "UNLINKED  channel doc %s %r (%s) — no youtube_account_esid, so NO token and it can "
            "never import. Invisible to every account-side audit.",
            row["_id"],
            row["title"],
            row["channel_id"] or "no channel id",
        )

    for row in report["dangling"]:
        logger.info("")
        logger.warning(
            "DANGLING  channel doc %s %r (%s) -> account %s which DOES NOT EXIST; cannot import.",
            row["_id"],
            row["title"],
            row["channel_id"],
            row["account_esid"],
        )

    _log_duplicates(report, account_title)

    logger.info("")
    logger.info("── Channel -> account map (what Asset Explorer doesn't show) ───")
    for esid, rows in sorted(report["by_account"].items(), key=lambda kv: -len(kv[1])):
        logger.info(
            "%s  %r — %d channel(s)%s",
            esid,
            account_title.get(esid, "MISSING ACCOUNT DOC"),
            len(rows),
            "" if esid in account_title else "   <-- DANGLING",
        )
        for r in sorted(rows, key=lambda x: x["title"]):
            logger.info("        %s  (%s)", r["title"], r["channel_id"])

    logger.info("")
    _print_summary(_summary(c, duplicates_only=False))
    return 1 if (c["unlinked"] or c["dangling"] or c["duplicate_channel_ids"]) else 0


def _log_duplicates(report: dict[str, Any], account_title: dict[str, str]) -> None:
    for cid, rows in sorted(report["duplicates"].items()):
        accts = {r["account_esid"] or "(none)" for r in rows}
        logger.info("")
        logger.warning(
            "DUPLICATE channel id %s (%r) in %d channel docs across account(s): %s",
            cid,
            rows[0]["title"],
            len(rows),
            ", ".join(sorted(accts)),
        )
        for r in rows:
            teams = ", ".join(f"{t['title']} ({t['_id']})" for t in r["teams"]) or "no team links"
            logger.warning(
                "    doc=%s account=%s (%s) — linked by: %s",
                r["_id"],
                r["account_esid"] or "(none)",
                account_title.get(r["account_esid"], "MISSING") if r["account_esid"] else "unlinked",
                teams,
            )
        if len(accts) > 1:
            logger.warning(
                "    -> copies name DIFFERENT accounts: which one the importer honors depends on "
                "scan order, and at least one copy is dead weight. This also makes an account doc "
                "look delete-safe when a copy still references it."
            )
    if report["duplicates"]:
        logger.warning("    fix: python -m tools.dedupe_youtube_channels   (dry-run first; )")


def main(argv: list[str] | None = None) -> int:
    logging.basicConfig(format="%(asctime)s %(levelname)s %(message)s", level=logging.INFO, stream=sys.stderr)
    ap = argparse.ArgumentParser(description="Read-only audit of YouTube channel docs.")
    ap.add_argument(
        "--duplicates-only",
        action="store_true",
        help="report and fail only on duplicate channel ids (the scheduled check)",
    )
    args = ap.parse_args(argv)
    return run(duplicates_only=args.duplicates_only)


if __name__ == "__main__":
    raise SystemExit(main())
