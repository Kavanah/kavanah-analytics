"""Seed / remove an artificial fixture for the label fix.

**Non-production test tool.** Creates throwaway ``de_asset_youtube_accounts`` and
``de_asset_youtube_channels`` docs that reproduce the shape of the prod bug so the
UI fix can be eyeballed on staging, where the real team data does not exist.

The bug being reproduced: one Google login can front several Brand Account
identities, each needing its own account doc (one doc = one token = one identity,
). Title and email are IDENTICAL across those docs, so the picker, the
connector dialog's group headings, and the Asset Explorer list all rendered
indistinguishable rows. The only distinguishing field is
``youtube_account_owned_channel_titles``, stamped at OAuth time.

Because the defect is purely a function of STORED fields, reproducing it needs no
OAuth, no Google identity, and no tokens — just two same-titled account docs with
different owned-channel stamps, plus channels pointing at them. That is what this
seeds.

It deliberately also seeds the prod DIVERGENCE (a channel linked to an account
whose stamped owned set does NOT contain it), because that is what makes the
"authorized as:" wording load-bearing: the heading names one-team-equivalent while
listing other channels beneath it, and the label must read as information rather
than as a mislabel.

**Scope — what this fixture CAN and CANNOT verify.**

Verified working with it (2026-07-31): the Asset Explorer YouTube Accounts list
and the Edit-Channel "YouTube Account" select. Both read account/channel docs
directly, so seeded docs are sufficient.

**It cannot reach the connector dialog's group headings.** That card resolves its
state from a ``de_connection`` row, not from the asset docs — so a seeded team
reads "disconnected" no matter how the assets and ``ds_youtube`` are set. Faking
a connection would mean inventing a principal, provider and natural key
(OAuth-adjacent state) on a shared cluster, which is not worth it for a label
check: that surface calls the same ``_youtube_picker_label`` helper the two
verified surfaces already exercise, so only the string's RENDERING inside the
heading element is unproven, and that is directly observable in production.
``--team-esid`` is kept because the link + flag it writes are correct and
harmless, but it is not sufficient on its own.

Safety:

* Every doc created carries ``fixture_3: true`` and a title prefixed with
  ``[TEST]``. ``--remove`` deletes ONLY docs carrying that marker, so it can
  never reap real data even if a title collides.
* No tokens are written, so nothing here can import or be mistaken for a live
  grant. The account docs are deliberately tokenless.
* ``--apply`` is required to write; the default run only prints what it would do.
* ``--i-am-not-on-production`` is required in addition to ``--apply``. There is
  no reliable env marker in this codebase to detect prod automatically, so the
  operator asserts it. Do NOT run this on production.

Usage (inside imports_container on STAGING)::

    # preview
    docker exec -w /app/python imports_container python3 \\
        -m tools.seed_youtube_label_fixture

    # create
    docker exec -w /app/python imports_container python3 \\
        -m tools.seed_youtube_label_fixture --apply --i-am-not-on-production

    # clean up afterwards
    docker exec -w /app/python imports_container python3 \\
        -m tools.seed_youtube_label_fixture --remove --apply --i-am-not-on-production
"""

from __future__ import annotations

import argparse
import logging
import sys
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
from tools.split_youtube_account import ACCOUNTS_INDEX, CHANNELS_INDEX  # noqa: E402

logger = logging.getLogger(__name__)

# Marker on every doc this tool creates. --remove keys on it, never on title.
FIXTURE_FLAG = "fixture_3"
TITLE = "[TEST] Shared Bucket Fixture"
EMAIL = "fixture_2@example.invalid"  # .invalid can never be a real Google login


def _rand(n: int) -> str:
    # CSPRNG, same generator as the wizard's auto_generate.
    from lib.oauth_state import random_alnum

    return random_alnum(n)


def build_fixture(workspace_esid: str | None) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """Return ``(accounts, channels)`` doc bodies for the fixture.

    Pure — no ES, no randomness that affects assertions beyond the auth slugs,
    so the shape is unit-testable.

    Two accounts share TITLE and EMAIL (the bug's precondition) but stamp
    different owned channels:

    * account A owns "Channel Alpha" and is linked to Alpha — the healthy case.
    * account B owns "Channel Bravo" but is linked to Charlie AND Delta — the
      prod divergence (owned != linked), which is what makes the heading read
      "authorized as: Channel Bravo" above a list of Charlie and Delta.
    """
    common: dict[str, Any] = {
        "youtube_account_title": TITLE,
        "youtube_account_email": EMAIL,
        FIXTURE_FLAG: True,
        # No youtube_account_token on purpose: a tokenless doc cannot import and
        # cannot be mistaken for a live grant.
    }
    if workspace_esid:
        common["workspace_esid"] = workspace_esid

    acct_a = dict(
        common,
        youtube_account_auth_url=_rand(15),
        youtube_account_auth_state=_rand(25),
        youtube_account_identity="FIXTURE-IDENTITY-A",
        youtube_account_owned_channel_ids=["UC_FIXTURE_ALPHA"],
        youtube_account_owned_channel_titles=["[TEST] Channel Alpha"],
    )
    acct_b = dict(
        common,
        youtube_account_auth_url=_rand(15),
        youtube_account_auth_state=_rand(25),
        youtube_account_identity="FIXTURE-IDENTITY-B",
        youtube_account_owned_channel_ids=["UC_FIXTURE_BRAVO"],
        youtube_account_owned_channel_titles=["[TEST] Channel Bravo"],
    )

    # Channels are returned with a placeholder account key the caller fills in
    # once the account esids exist. Index 0 -> account A, 1 and 2 -> account B.
    channels = [
        {
            "youtube_channel_title": "[TEST] Channel Alpha",
            "youtube_channel_id": "UC_FIXTURE_ALPHA",
            "_fixture_account": "A",
            FIXTURE_FLAG: True,
        },
        {
            "youtube_channel_title": "[TEST] Channel Charlie",
            "youtube_channel_id": "UC_FIXTURE_CHARLIE",
            "_fixture_account": "B",
            FIXTURE_FLAG: True,
        },
        {
            "youtube_channel_title": "[TEST] Channel Delta",
            "youtube_channel_id": "UC_FIXTURE_DELTA",
            "_fixture_account": "B",
            FIXTURE_FLAG: True,
        },
    ]
    if workspace_esid:
        for ch in channels:
            ch["workspace_esid"] = workspace_esid
    return [acct_a, acct_b], channels


def _fixture_hits(es: Any, index: str) -> list[dict[str, Any]]:
    resp = es.search(
        index=index,
        body={"query": {"term": {FIXTURE_FLAG: True}}, "size": 500},
        ignore_unavailable=True,
    )
    return resp.get("hits", {}).get("hits", [])


TEAM_CHANNELS_FIELD = "team_youtube_channel_handle_esids"
# The connector card reads this flag (connector_status_service: `enabled =
# team_source.get("ds_youtube")`), and EVERYTHING downstream is gated on it —
# linked-asset resolution, the card state, and therefore the dialog. Linking
# channels without it leaves the connector reading "not configured", which is
# why a fixture team showed no connections at all.
TEAM_DS_FLAG = "ds_youtube"
# Records whether the fixture turned the flag on, so cleanup only turns it back
# off for a team that was NOT already using YouTube. Without this, cleaning up
# would disable a real connector.
TEAM_FLAG_MARKER = "fixture_set_ds_youtube"


def merge_team_channels(existing: Any, new_esids: list[str]) -> list[str]:
    """Union of a team's current channel links and the fixture's, order-stable.

    Pure, so the ADDITIVE guarantee is unit-testable: the connector dialog reads
    this field, and clobbering it would silently unlink a real team's channels.
    Handles the scalar-string legacy shape the read path also tolerates.
    """
    if isinstance(existing, str):
        current = [existing] if existing else []
    else:
        current = [str(e) for e in (existing or []) if e]
    out = list(current)
    for esid in new_esids:
        if esid not in out:
            out.append(esid)
    return out


def _attach_to_team(es: Any, team_esid: str, channel_esids: list[str]) -> None:
    """Append the fixture channels to a team's link list (never replace it)."""
    try:
        got = es.get(index="de_asset_teams", id=team_esid)
    except Exception as e:  # noqa: BLE001 — CLI boundary; a bad esid is operator error
        logger.error("could not read team %s: %s", team_esid, e)
        return
    source = got.get("_source") or {}
    before = source.get(TEAM_CHANNELS_FIELD)
    source[TEAM_CHANNELS_FIELD] = merge_team_channels(before, channel_esids)

    # Enable the connector. The card (and therefore the dialog) is gated on this
    # flag, so linking channels alone leaves the team reading "not configured".
    # Remember whether WE turned it on, so cleanup can restore the prior state
    # instead of disabling a team that genuinely uses YouTube.
    was_enabled = str(source.get(TEAM_DS_FLAG)).lower() in ("true", "1", "yes")
    if not was_enabled:
        source[TEAM_DS_FLAG] = True
        source[TEAM_FLAG_MARKER] = True
        logger.info("set %s=true on team %s (was off)", TEAM_DS_FLAG, team_esid)
    else:
        logger.info("%s already true on team %s — leaving it (real connector)", TEAM_DS_FLAG, team_esid)

    es.index(index="de_asset_teams", id=team_esid, document=source)
    es.indices.refresh(index="de_asset_teams", ignore_unavailable=True)
    logger.info(
        "attached %d fixture channel(s) to team %s (%s -> %d link(s))",
        len(channel_esids),
        team_esid,
        len(before) if isinstance(before, list) else (1 if before else 0),
        len(source[TEAM_CHANNELS_FIELD]),
    )
    logger.info("--remove will delete these docs AND unpick them from this team's %s.", TEAM_CHANNELS_FIELD)


def resolve_workspace_esid(es: Any, value: str) -> str | None:
    """Accept either a real workspace esid or a workspace TITLE, return the esid.

    ``workspace_esid`` stores an ES ``_id`` from ``de_asset_workspace``, not a
    slug — so passing a title (e.g. "kavanah-media") stamps a value that matches
    no workspace and every workspace-scoped read drops the doc. The asset is
    created but invisible, which reads exactly like "the fix didn't work". Accept
    both forms rather than let that happen again.
    """
    if not value:
        return None
    try:
        es.get(index="de_asset_workspace", id=value)
        return value  # already a valid esid
    except Exception:
        pass
    try:
        resp = es.search(
            index="de_asset_workspace",
            body={
                "query": {
                    "bool": {
                        "should": [
                            {"term": {"workspace_title": value}},
                            {"term": {"workspace_title.keyword": value}},
                        ],
                        "minimum_should_match": 1,
                    }
                },
                "size": 2,
            },
        )
    except Exception as e:  # noqa: BLE001 — CLI boundary
        logger.error("could not resolve workspace %r: %s", value, e)
        return None
    hits = resp.get("hits", {}).get("hits", [])
    if not hits:
        logger.error(
            "no workspace matches %r (neither an esid nor a workspace_title). The docs would be "
            "created but INVISIBLE to workspace-scoped users. Aborting.",
            value,
        )
        return None
    if len(hits) > 1:
        logger.warning("workspace title %r matched %d docs; using %s", value, len(hits), hits[0]["_id"])
    logger.info("resolved workspace %r -> esid %s", value, hits[0]["_id"])
    return hits[0]["_id"]


def seed(es: Any, workspace_esid: str | None, apply: bool, team_esid: str | None = None) -> int:
    accounts, channels = build_fixture(workspace_esid)
    if not apply:
        logger.info("[dry-run] would create %d account doc(s), both titled %r:", len(accounts), TITLE)
        for a in accounts:
            logger.info("    owned: %s", a["youtube_account_owned_channel_titles"])
        logger.info("[dry-run] would create %d channel doc(s):", len(channels))
        for ch in channels:
            logger.info("    %s -> account %s", ch["youtube_channel_title"], ch["_fixture_account"])
        if team_esid:
            logger.info("[dry-run] would append %d channel link(s) to team %s", len(channels), team_esid)
        logger.info("Re-run with --apply --i-am-not-on-production to write.")
        return 0

    esids: dict[str, str] = {}
    for key, body in (("A", accounts[0]), ("B", accounts[1])):
        created = es.index(index=ACCOUNTS_INDEX, document=body)
        esids[key] = created["_id"]
        logger.info("created account %s (%s) owned=%s", esids[key], key, body["youtube_account_owned_channel_titles"])

    channel_esids_created: list[str] = []
    for ch in channels:
        body = {k: v for k, v in ch.items() if k != "_fixture_account"}
        body["youtube_account_esid"] = esids[ch["_fixture_account"]]
        body["youtube_account_title"] = TITLE
        created = es.index(index=CHANNELS_INDEX, document=body)
        channel_esids_created.append(created["_id"])
        logger.info("created channel %s %r -> %s", created["_id"], body["youtube_channel_title"], body["youtube_account_esid"])

    es.indices.refresh(index=f"{ACCOUNTS_INDEX},{CHANNELS_INDEX}", ignore_unavailable=True)

    # optionally attach the fixture channels to a real team so the
    # CONNECTOR DIALOG's group headings can be viewed. That surface builds its
    # groups purely from ES (team.team_youtube_channel_handle_esids -> channel ->
    # account), with no token and no Google call, so a fixture team renders it
    # fully without any OAuth. Without this the dialog cannot be reached at all,
    # which is exactly the blocker a teammate hit.
    if team_esid:
        _attach_to_team(es, team_esid, list(channel_esids_created))
    logger.info("")
    logger.info("Fixture seeded. Check these surfaces:")
    logger.info("  1. Asset Explorer -> YouTube Accounts: two %r rows, differing in", TITLE)
    logger.info("     the 'Authorized For (channels owned)' column (Alpha vs Bravo).")
    logger.info("  2. Edit a YouTube Channel -> 'YouTube Account' select: two options,")
    logger.info("     suffixed 'authorized as: [TEST] Channel Alpha' / '... Bravo'.")
    logger.info("  3. Data Connectors -> YouTube wizard -> 'Use existing' dropdown: same suffixes.")
    logger.info("")
    logger.warning(
        "NOT reachable with this fixture: the connector dialog's group headings. That card "
        "resolves state from a de_connection row, not these asset docs, so a seeded team reads "
        "'disconnected' regardless. Verify that one surface in production after the merge."
    )
    logger.info("")
    logger.info("Clean up with: --remove --apply --i-am-not-on-production")
    return 0


def prune_team_links(existing: Any, dead_esids: set[str]) -> list[str]:
    """A team's channel links with the fixture esids removed, order preserved.

    Pure so the guarantee is unit-testable: only esids in ``dead_esids`` are
    dropped, every real link survives. Handles the legacy scalar-string shape.
    """
    if isinstance(existing, str):
        current = [existing] if existing else []
    else:
        current = [str(e) for e in (existing or []) if e]
    return [e for e in current if e not in dead_esids]


def _detach_from_teams(es: Any, dead_channel_esids: set[str], apply: bool) -> None:
    """Unpick deleted fixture channels from every team that references them.

    Without this, --remove leaves dangling esids on the team doc and the
    connector dialog renders empty groups — a confusing remnant on a REAL team.
    Scans all teams because the seeder does not record which team it attached to.
    """
    if not dead_channel_esids:
        return
    try:
        resp = es.search(
            index="de_asset_teams",
            body={"query": {"match_all": {}}, "size": 10000},
            ignore_unavailable=True,
        )
    except Exception as e:  # noqa: BLE001 — CLI boundary
        logger.warning("could not scan teams to unpick fixture links: %s", e)
        return

    for hit in resp.get("hits", {}).get("hits", []):
        source = hit.get("_source") or {}
        before = source.get(TEAM_CHANNELS_FIELD)
        after = prune_team_links(before, dead_channel_esids)
        had = before if isinstance(before, list) else ([before] if before else [])
        # Skip only when there is nothing to undo at all. A team can carry the
        # flag marker without any fixture links left (e.g. links already pruned
        # by an earlier partial run), and that flag still needs resetting.
        if len(after) == len(had) and not source.get(TEAM_FLAG_MARKER):
            continue
        removed = len(had) - len(after)
        # Only turn the connector back off if the fixture turned it ON, and only
        # once no links remain — a team that genuinely uses YouTube keeps its flag.
        restore_flag = bool(source.get(TEAM_FLAG_MARKER)) and not after
        if not apply:
            logger.info(
                "[dry-run] would unpick %d fixture link(s) from team %s (%r)%s",
                removed,
                hit["_id"],
                source.get("team_title"),
                f" and reset {TEAM_DS_FLAG}" if restore_flag else "",
            )
            continue
        source[TEAM_CHANNELS_FIELD] = after
        if restore_flag:
            source[TEAM_DS_FLAG] = False
            logger.info("reset %s=false on team %s (fixture had enabled it)", TEAM_DS_FLAG, hit["_id"])
        source.pop(TEAM_FLAG_MARKER, None)
        es.index(index="de_asset_teams", id=hit["_id"], document=source)
        logger.info(
            "unpicked %d fixture link(s) from team %s (%r); %d real link(s) kept",
            removed,
            hit["_id"],
            source.get("team_title"),
            len(after),
        )
    if apply:
        es.indices.refresh(index="de_asset_teams", ignore_unavailable=True)


def remove(es: Any, apply: bool) -> int:
    total = 0
    dead_channels: set[str] = set()
    for index in (ACCOUNTS_INDEX, CHANNELS_INDEX):
        hits = _fixture_hits(es, index)
        total += len(hits)
        for hit in hits:
            title = (hit.get("_source") or {}).get("youtube_account_title") or (hit.get("_source") or {}).get(
                "youtube_channel_title"
            )
            if index == CHANNELS_INDEX:
                dead_channels.add(hit["_id"])
            if not apply:
                logger.info("[dry-run] would delete %s/%s %r", index, hit["_id"], title)
                continue
            es.delete(index=index, id=hit["_id"])
            logger.info("deleted %s/%s %r", index, hit["_id"], title)

    # Unpick BEFORE reporting done, so a team never keeps a link to a doc that
    # no longer exists.
    _detach_from_teams(es, dead_channels, apply)

    if not total:
        logger.info("No fixture docs found (nothing carries %s=true).", FIXTURE_FLAG)
    elif not apply:
        logger.info("Re-run with --apply --i-am-not-on-production to delete.")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description="Seed/remove the  label fixture. NOT for production.")
    parser.add_argument("--apply", action="store_true", help="Actually write/delete (default: preview only).")
    parser.add_argument("--remove", action="store_true", help="Delete fixture docs instead of creating them.")
    parser.add_argument(
        "--i-am-not-on-production",
        action="store_true",
        help="Required with --apply. Asserts this is not the production cluster.",
    )
    parser.add_argument(
        "--team-esid",
        default=None,
        help=(
            "Also attach the fixture channels to this team so the CONNECTOR DIALOG's group "
            "headings can be viewed. Additive: existing links are preserved."
        ),
    )
    parser.add_argument(
        "--workspace-esid",
        default=None,
        help="Stamp this workspace_esid so the docs are visible to workspace-scoped users.",
    )
    args = parser.parse_args()

    logging.basicConfig(format="%(asctime)s %(levelname)s %(message)s", level=logging.INFO, stream=sys.stderr)

    if args.apply and not args.i_am_not_on_production:
        logger.error(
            "--apply requires --i-am-not-on-production. This tool writes throwaway docs and "
            "must never run against the production cluster."
        )
        return 2

    es = get_elasticsearch_client()
    if es is None:
        logger.error("Could not connect to ES; aborting.")
        return 2

    if args.remove:
        return remove(es, apply=args.apply)

    # accept a workspace title OR esid. A title stamps a value that
    # matches no workspace, so the docs are created but invisible; abort instead.
    ws = args.workspace_esid
    if ws:
        ws = resolve_workspace_esid(es, ws)
        if ws is None:
            return 2
    return seed(es, workspace_esid=ws, apply=args.apply, team_esid=args.team_esid)


if __name__ == "__main__":
    raise SystemExit(main())
