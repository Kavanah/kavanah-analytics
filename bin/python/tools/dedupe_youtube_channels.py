"""One-shot cleanup: duplicate YouTube channel records left by reconnects
.

Before, reconnecting an already-connected channel under a different
Google account minted a second ``de_asset_youtube_channels`` record for the same
``youtube_channel_id``. The old copy stayed on an account whose token can no
longer read the channel, so any team still linked to it failed "analytics
forbidden" on every run and spent a plan slot on it. The wizard now moves the
record instead; this tool heals what already accumulated:

* for each channel id held by more than one record, the KEEPER is the one copy
  whose account's token owns the channel (one Google ``channels.list`` probe per
  account involved). No owning copy, more than one, or unknowable ownership =
  ambiguous = the group is reported and left alone (decline and report);
* each stale copy's team links — ``team_youtube_channel_handle_esids`` (with its
  positional title list) and the legacy scalar ``youtube_channel_esid`` — are
  repointed to the keeper, and only THEN is the copy deleted. The delete cascade
  (``ReferenceCleanupService``) strips a deleted ESID from teams, so deleting
  first would drop the channel from the team. Just before each delete, the
  tool re-reads the teams and skips the delete if anything still links it;
* a stale copy linked to a team that shares no org with the keeper's teams is
  left alone and reported (the cross-org case keeps its own record, as in the
  Phase 1 wizard fix); so is one whose teams can't be placed at all;
* ``de_connection_asset`` rows are dropped when their record is deleted, or when
  their connection's account no longer holds the record (a record the wizard
  moved in Phase 1 left the old connection listing it on its Manage page);
* accounts left holding no channel are LISTED, never deleted. An account is
  deleted only when named with ``--delete-account``, holds no channel after the
  plan, and is not named with ``--protect-account``. Its ``de_connection`` rows
  are listed, not touched.

Dry-run by default — the dry run IS the census. Run it on prod and read the
plan before ``--apply``. Idempotent: after a clean apply no channel id has more
than one record, so a second run plans nothing.

Exit codes: 0 = nothing left for a human; 1 = something was skipped, refused or
failed (read the WARN lines); 2 = ES unavailable or a read failed.

Usage (inside imports_container)::

    docker exec -w /app/python imports_container python3 \\
        -m tools.dedupe_youtube_channels \\
        [--protect-account <esid> ...] [--delete-account <esid> ...] [--apply]
"""

from __future__ import annotations

import argparse
import logging
import sys
from collections import defaultdict
from pathlib import Path
from typing import Any, Callable

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
from tools.audit_youtube_channel_links import _fetch_all_channels  # noqa: E402
from tools.split_youtube_account import (  # noqa: E402
    ACCOUNTS_INDEX,
    CHANNELS_INDEX,
    _dual_term,
    _owned_channel_ids,
)

logger = logging.getLogger(__name__)

TEAMS_INDEX = "de_asset_teams"
CONNECTION_INDEX = "de_connection"
CONNECTION_ASSET_INDEX = "de_connection_asset"
ASSET_TYPE_YOUTUBE_CHANNEL = "youtube_channel"
PROVIDER_YOUTUBE = "youtube"
TEAM_ESIDS_FIELD = "team_youtube_channel_handle_esids"
TEAM_TITLES_FIELD = "team_youtube_channel_handle"
TEAM_LEGACY_FIELD = "youtube_channel_esid"
_TEAM_SOURCE_FIELDS = [
    "team_title",
    "organization_esid",
    "workspace_esid",
    TEAM_ESIDS_FIELD,
    TEAM_TITLES_FIELD,
    TEAM_LEGACY_FIELD,
]


def _as_list(value: Any) -> list[str]:
    """An ESID-list field (list, legacy scalar string, or absent) as a list."""
    if isinstance(value, list):
        return [str(v) for v in value if v]
    return [str(value)] if value else []


def _links(team: dict[str, Any], channel_esid: str) -> bool:
    return channel_esid in _as_list(team.get(TEAM_ESIDS_FIELD)) or team.get(TEAM_LEGACY_FIELD) == channel_esid


def _merge_allowed(stale_teams: list[dict[str, Any]], keeper: dict[str, Any], keeper_teams: list[dict[str, Any]]) -> bool:
    """May the stale copy's team links move onto the keeper?

    Same tenancy rule as the Phase 1 wizard fix: every team linked to the stale
    copy must share an org with the keeper's teams. When the keeper has no team
    yet, every stale-linked team must sit in the keeper's workspace. A team with
    no org or workspace can't be placed, so it blocks the merge.
    """
    if not stale_teams:
        return True
    if keeper_teams:
        keeper_orgs = {o for t in keeper_teams for o in _as_list(t.get("organization_esid"))}
        return all(set(_as_list(t.get("organization_esid"))) & keeper_orgs for t in stale_teams)
    workspace = str(keeper.get("workspace_esid") or "").strip()
    return bool(workspace) and all(str(t.get("workspace_esid") or "").strip() == workspace for t in stale_teams)


def relinked_team_fields(team: dict[str, Any], mapping: dict[str, str]) -> dict[str, Any]:
    """The team fields that change when each ``mapping`` key (a stale record) is
    replaced by its value (the keeper). Returns ``{}`` when nothing changes.

    The stale ESID is replaced in place, so the positional title list stays
    aligned (same channel, same title). When the keeper is already linked, the
    stale entry and its title are dropped instead of duplicated.
    """
    changed: dict[str, Any] = {}
    esids = _as_list(team.get(TEAM_ESIDS_FIELD))
    titles = team.get(TEAM_TITLES_FIELD)
    titles = list(titles) if isinstance(titles, list) else None
    if any(e in mapping for e in esids):
        new_esids: list[str] = []
        new_titles: list[Any] = []
        for i, esid in enumerate(esids):
            target = mapping.get(esid, esid)
            if target in new_esids:
                continue
            new_esids.append(target)
            if titles is not None and i < len(titles):
                new_titles.append(titles[i])
        changed[TEAM_ESIDS_FIELD] = new_esids
        if titles is not None:
            changed[TEAM_TITLES_FIELD] = new_titles
    legacy = team.get(TEAM_LEGACY_FIELD)
    if legacy in mapping:
        changed[TEAM_LEGACY_FIELD] = mapping[legacy]
    return changed


def plan_cleanup(
    *,
    channels: list[dict[str, Any]],
    accounts: list[dict[str, Any]],
    teams: list[dict[str, Any]],
    connection_assets: list[dict[str, Any]],
    connections: list[dict[str, Any]],
    owned_of: Callable[[dict[str, Any]], set[str] | None],
    protect_accounts: set[str] | frozenset[str] = frozenset(),
    delete_accounts: set[str] | frozenset[str] = frozenset(),
) -> dict[str, Any]:
    """Pure planner — every input is a list of ``{"_id", ...source}`` dicts and
    Google is injected via ``owned_of``, so this is testable without ES or Google.
    """
    accounts_by_id = {a["_id"]: a for a in accounts}
    owned_cache: dict[str, set[str] | None] = {}

    def owned(account_esid: str) -> set[str] | None:
        if account_esid not in owned_cache:
            account = accounts_by_id.get(account_esid)
            owned_cache[account_esid] = owned_of(account) if account else None
        return owned_cache[account_esid]

    def linking(channel_esid: str) -> list[dict[str, Any]]:
        return [t for t in teams if _links(t, channel_esid)]

    by_cid: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for ch in channels:
        cid = str(ch.get("youtube_channel_id") or "").strip()
        if cid:
            by_cid[cid].append(ch)

    groups: list[dict[str, Any]] = []
    skipped: list[dict[str, Any]] = []
    mapping: dict[str, str] = {}
    delete_records: list[dict[str, Any]] = []
    for cid, copies in sorted(by_cid.items()):
        if len(copies) < 2:
            continue
        title = next((c.get("youtube_channel_title") for c in copies if c.get("youtube_channel_title")), cid)
        on_accounts = [c for c in copies if c.get("youtube_account_esid")]
        unknown = sorted({c["youtube_account_esid"] for c in on_accounts if owned(c["youtube_account_esid"]) is None})
        owners = [c for c in on_accounts if cid in (owned(c["youtube_account_esid"]) or set())]
        if len(owners) != 1:
            if owners:
                reason = f"{len(owners)} copies sit on accounts that own the channel"
            elif unknown:
                reason = "no copy's account is known to own the channel; ownership unknown for " + ", ".join(unknown)
            else:
                reason = "no copy's account owns the channel"
            skipped.append({"channel_id": cid, "title": title, "reason": reason, "records": [c["_id"] for c in copies]})
            continue
        keeper = owners[0]
        keeper_teams = linking(keeper["_id"])
        group = {
            "channel_id": cid,
            "title": title,
            "keeper": keeper["_id"],
            "keeper_account": keeper.get("youtube_account_esid"),
            "stale": [],
        }
        for stale in copies:
            if stale["_id"] == keeper["_id"]:
                continue
            stale_teams = linking(stale["_id"])
            if not _merge_allowed(stale_teams, keeper, keeper_teams):
                skipped.append(
                    {
                        "channel_id": cid,
                        "title": title,
                        "reason": "stale copy is linked to a team outside the keeper's org or workspace",
                        "records": [stale["_id"]],
                        "teams": [t["_id"] for t in stale_teams],
                    }
                )
                continue
            mapping[stale["_id"]] = keeper["_id"]
            delete_records.append(stale)
            group["stale"].append(
                {
                    "record": stale["_id"],
                    "account": stale.get("youtube_account_esid") or "",
                    "teams": [{"_id": t["_id"], "title": t.get("team_title") or t["_id"]} for t in stale_teams],
                }
            )
        if group["stale"]:
            groups.append(group)

    team_updates = []
    for team in teams:
        fields = relinked_team_fields(team, mapping)
        if fields:
            team_updates.append({"_id": team["_id"], "title": team.get("team_title") or team["_id"], "fields": fields})

    deleted_ids = set(mapping)
    record_account = {c["_id"]: c.get("youtube_account_esid") or "" for c in channels}
    connection_account = {
        c["_id"]: c.get("legacy_asset_esid") or ""
        for c in connections
        if c.get("legacy_asset_index") == ACCOUNTS_INDEX and c.get("legacy_asset_esid")
    }
    stale_rows: list[dict[str, Any]] = []
    dangling_rows: list[dict[str, Any]] = []
    for row in connection_assets:
        if row.get("asset_type") != ASSET_TYPE_YOUTUBE_CHANNEL or row.get("legacy_asset_index") != CHANNELS_INDEX:
            continue
        record = row.get("legacy_asset_esid") or ""
        conn_acct = connection_account.get(row.get("connection_esid") or "")
        entry = {"_id": row["_id"], "connection": row.get("connection_esid") or "", "record": record}
        if record in deleted_ids:
            stale_rows.append({**entry, "reason": "its record is deleted"})
        elif record not in record_account:
            dangling_rows.append(entry)
        elif conn_acct and conn_acct != record_account[record]:
            stale_rows.append({**entry, "reason": f"its connection's account {conn_acct} no longer holds the record"})

    remaining = defaultdict(int)
    for ch in channels:
        if ch["_id"] not in deleted_ids and ch.get("youtube_account_esid"):
            remaining[ch["youtube_account_esid"]] += 1
    empty_accounts = [
        {
            "_id": a["_id"],
            "title": a.get("youtube_account_title") or a["_id"],
            "connections": sorted(cid for cid, acct in connection_account.items() if acct == a["_id"]),
        }
        for a in accounts
        if remaining[a["_id"]] == 0
    ]
    account_deletes, refused = [], []
    for esid in sorted(delete_accounts):
        if esid in protect_accounts:
            refused.append({"_id": esid, "reason": "named with --protect-account"})
        elif esid not in accounts_by_id:
            refused.append({"_id": esid, "reason": "no such account"})
        elif remaining[esid]:
            refused.append({"_id": esid, "reason": f"still holds {remaining[esid]} channel record(s) after this plan"})
        else:
            account_deletes.append(esid)

    return {
        "groups": groups,
        "skipped": skipped,
        "team_updates": team_updates,
        "delete_records": [c["_id"] for c in delete_records],
        "stale_connection_assets": stale_rows,
        "dangling_connection_assets": dangling_rows,
        "empty_accounts": empty_accounts,
        "account_deletes": account_deletes,
        "refused_account_deletes": refused,
    }


def fetch_state(es: Any) -> dict[str, list[dict[str, Any]]]:
    """Everything the planner needs, as ``{"_id", ...source}`` lists. Raises on
    any read error, so a partial read can never become a plan."""

    def search(index: str, query: dict[str, Any], source: list[str] | None = None) -> list[dict[str, Any]]:
        body: dict[str, Any] = {"query": query, "size": 10000}
        if source is not None:
            body["_source"] = source
        resp = es.search(index=index, body=body)
        return [{**(h.get("_source") or {}), "_id": h["_id"]} for h in resp.get("hits", {}).get("hits", [])]

    return {
        "channels": _fetch_all_channels(es),
        "accounts": _fetch_all_accounts(es),
        "teams": search(TEAMS_INDEX, {"match_all": {}}, _TEAM_SOURCE_FIELDS),
        "connection_assets": search(CONNECTION_ASSET_INDEX, _dual_term("asset_type", ASSET_TYPE_YOUTUBE_CHANNEL)),
        "connections": search(
            CONNECTION_INDEX,
            _dual_term("provider", PROVIDER_YOUTUBE),
            ["provider", "principal", "status", "legacy_asset_index", "legacy_asset_esid"],
        ),
    }


def _still_linked(es: Any, channel_esid: str) -> int:
    query = {
        "bool": {
            "should": [_dual_term(TEAM_ESIDS_FIELD, channel_esid), _dual_term(TEAM_LEGACY_FIELD, channel_esid)],
            "minimum_should_match": 1,
        }
    }
    return int(es.count(index=TEAMS_INDEX, body={"query": query}).get("count", 0))


def apply_plan(es: Any, plan: dict[str, Any], delete_asset: Callable[[str, str], bool]) -> int:
    """Write the plan in the only safe order: re-link teams, drop connection
    rows, then delete records (each re-checked first), then accounts. Returns
    the number of failed or skipped steps."""
    failures = 0
    for update in plan["team_updates"]:
        es.update(index=TEAMS_INDEX, id=update["_id"], doc=update["fields"], refresh="wait_for")
        logger.info("RELINKED team %s %r: %s", update["_id"], update["title"], sorted(update["fields"]))
    for row in plan["stale_connection_assets"]:
        es.delete(index=CONNECTION_ASSET_INDEX, id=row["_id"], refresh="wait_for")
        logger.info("DELETED  connection row %s (connection %s, record %s)", row["_id"], row["connection"], row["record"])
    for record in plan["delete_records"]:
        linked = _still_linked(es, record)
        if linked:
            failures += 1
            logger.warning("SKIPPED  delete of record %s: %d team(s) still link it; not deleting", record, linked)
            continue
        if delete_asset(CHANNELS_INDEX, record):
            logger.info("DELETED  channel record %s", record)
        else:
            failures += 1
            logger.warning("FAILED   delete of channel record %s", record)
    for account in plan["account_deletes"]:
        held = int(es.count(index=CHANNELS_INDEX, body={"query": _dual_term("youtube_account_esid", account)}).get("count", 0))
        if held:
            failures += 1
            logger.warning("SKIPPED  delete of account %s: it now holds %d channel record(s)", account, held)
            continue
        if delete_asset(ACCOUNTS_INDEX, account):
            logger.info("DELETED  account %s", account)
        else:
            failures += 1
            logger.warning("FAILED   delete of account %s", account)
    return failures


def print_plan(plan: dict[str, Any], apply: bool) -> None:
    mode = "APPLY" if apply else "DRY-RUN"
    for g in plan["groups"]:
        logger.info("")
        logger.info(
            "CHANNEL %s %r — keep record %s (account %s owns it)",
            g["channel_id"],
            g["title"],
            g["keeper"],
            g["keeper_account"],
        )
        for s in g["stale"]:
            teams = ", ".join(f"{t['title']} ({t['_id']})" for t in s["teams"]) or "no team links"
            logger.info(
                "    stale record %s on account %s — move links: %s — then delete",
                s["record"],
                s["account"] or "(none)",
                teams,
            )
    for s in plan["skipped"]:
        logger.info("")
        logger.warning(
            "SKIPPED channel %s %r — %s; records %s%s",
            s["channel_id"],
            s["title"],
            s["reason"],
            ", ".join(s["records"]),
            f"; teams {', '.join(s['teams'])}" if s.get("teams") else "",
        )
    for u in plan["team_updates"]:
        logger.info(
            "TEAM %s %r — rewrite %s", u["_id"], u["title"], ", ".join(f"{k}={v}" for k, v in sorted(u["fields"].items()))
        )
    for r in plan["stale_connection_assets"]:
        logger.info(
            "CONNECTION ROW %s (connection %s -> record %s) — delete: %s", r["_id"], r["connection"], r["record"], r["reason"]
        )
    for r in plan["dangling_connection_assets"]:
        logger.warning(
            "DANGLING connection row %s (connection %s) -> record %s that does not exist; listed only",
            r["_id"],
            r["connection"],
            r["record"],
        )
    for a in plan["empty_accounts"]:
        conns = ", ".join(a["connections"]) or "none"
        logger.info(
            "EMPTY ACCOUNT %s %r — holds no channel after this plan; de_connection rows: %s (listed, not deleted)",
            a["_id"],
            a["title"],
            conns,
        )
    for esid in plan["account_deletes"]:
        logger.info("ACCOUNT %s — delete (named with --delete-account)", esid)
    for r in plan["refused_account_deletes"]:
        logger.warning("REFUSED delete of account %s — %s", r["_id"], r["reason"])
    logger.info("")
    logger.info(
        "%s: %d duplicated channel(s) to fix, %d skipped; %d team(s) to re-link, %d record(s), %d connection row(s), %d account(s) to delete%s",
        mode,
        len(plan["groups"]),
        len(plan["skipped"]),
        len(plan["team_updates"]),
        len(plan["delete_records"]),
        len(plan["stale_connection_assets"]),
        len(plan["account_deletes"]),
        "" if apply else " — re-run with --apply to write",
    )


def run(es: Any, *, apply: bool, protect_accounts: set[str], delete_accounts: set[str]) -> int:
    try:
        state = fetch_state(es)
    except Exception as e:  # noqa: BLE001 — CLI boundary; a partial read must never become a plan
        logger.error("could not read the YouTube asset graph: %s", e)
        return 2
    plan = plan_cleanup(
        **state, owned_of=_owned_channel_ids, protect_accounts=protect_accounts, delete_accounts=delete_accounts
    )
    print_plan(plan, apply)
    needs_human = bool(plan["skipped"] or plan["refused_account_deletes"] or plan["dangling_connection_assets"])
    if not apply:
        return 1 if needs_human else 0

    from asset_management.web.services.asset_service import AssetService

    failures = apply_plan(es, plan, AssetService.delete)
    return 1 if (failures or needs_human) else 0


def main() -> int:
    logging.basicConfig(format="%(asctime)s %(levelname)s %(message)s", level=logging.INFO, stream=sys.stderr)
    ap = argparse.ArgumentParser(description="Collapse duplicate YouTube channel records.")
    ap.add_argument("--apply", action="store_true", help="write changes (default: dry-run)")
    ap.add_argument(
        "--protect-account", action="append", default=[], help="account ESID that must never be deleted (repeatable)"
    )
    ap.add_argument(
        "--delete-account", action="append", default=[], help="account ESID to delete once it holds no channel (repeatable)"
    )
    args = ap.parse_args()
    es = get_elasticsearch_client()
    if es is None:
        logger.error("Could not connect to ES; aborting.")
        return 2
    return run(es, apply=args.apply, protect_accounts=set(args.protect_account), delete_accounts=set(args.delete_account))


if __name__ == "__main__":
    raise SystemExit(main())
