"""One-shot backfill: stamp ``owning_team_esid`` on chatwoot assets.

A SHARED connector asset — one esid linked by teams across multiple orgs — was
counted under EVERY org whose team linked it, so a chatwoot account owned by org A's
team (one team) also showed under org B's Billing -> Current Plan because an org B team
(another team) cross-linked the same ``chatwoot_account_esid``. attributes a
shared asset to exactly one org — its owning team's PRIMARY org — keyed on a new
``owning_team_esid`` field on the asset doc.

This backfill stamps that field on EXISTING chatwoot assets so the fix takes
effect on already-wired data (new assets are stamped at wizard-create time by
``connector_wizard_service._stamp_asset_owning_team``).

Owner-selection rule (decided with a teammate, 2026-06-23 — "best title match, else
first"):

  * Find every team whose ``chatwoot_account_esid`` == the asset esid.
  * 0 linking teams      -> ORPHAN: left unstamped (reported, no owner to assign).
  * 1 linking team       -> that team is the owner.
  * >1 linking teams     -> the team whose title (and org title) best overlaps the
                            asset title by token-set match; ties broken by earliest
                            ``@timestamp`` then team esid sort (fully deterministic).

The stamp is a single human-correctable field — a wrong guess can be fixed in
Asset Explorer. Assets that ALREADY carry ``owning_team_esid`` are skipped, so the
script is idempotent (a second --apply run reports updated=0).

Dry-run by default; --apply writes. Read-only until --apply.

Usage::

    docker exec -w /app/python imports_container python3 \\
        -m tools.backfill_owning_team_esid

    docker exec -w /app/python imports_container python3 \\
        -m tools.backfill_owning_team_esid --apply
"""

from __future__ import annotations

import argparse
import logging
import re
import sys
from pathlib import Path

# Same three-candidate resolver as the other tools.backfill_* scripts — a
# tools/* module that imports asset_management.* and runs inside imports_container
# needs this, not parents[2]/bin/python (see CLAUDE memory).
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

TEAMS_INDEX = "de_asset_teams"
ASSET_INDEX = "de_asset_chatwoot_accounts"
ASSET_TITLE_FIELD = "chatwoot_account_title"
TEAM_LINK_FIELD = "chatwoot_account_esid"
OWNING_FIELD = "owning_team_esid"

# Generic English / connector words that carry no ownership signal — excluded
# from the token-overlap so an asset titled "<Org Name> <City> <State>" matches its
# team code / org "<Org Name>" on the meaningful tokens, not on filler.
_STOPWORDS = frozenset(
    {"the", "a", "an", "of", "and", "for", "team", "church", "ministries", "ministry", "inc", "account", "chatwoot"}
)


def _tokens(text: str) -> set[str]:
    """Lowercased alphanumeric tokens (len>=2) minus stopwords."""
    return {t for t in re.split(r"[^a-z0-9]+", (text or "").lower()) if len(t) >= 2 and t not in _STOPWORDS}


def _to_str(value) -> str:
    if isinstance(value, list):
        return " ".join(str(v) for v in value if v)
    return str(value or "")


def _primary_org(team_source: dict) -> str:
    org = team_source.get("organization_esid")
    if isinstance(org, list):
        return str(org[0]) if org else ""
    return str(org or "")


def _fetch_all(es, index: str, query: dict | None = None) -> list[dict]:
    body = {"query": query or {"match_all": {}}, "size": 10000}
    resp = es.search(index=index, body=body, ignore_unavailable=True)
    return resp.get("hits", {}).get("hits", [])


def _linking_teams(team_hits: list[dict], asset_esid: str) -> list[dict]:
    """Team hits whose TEAM_LINK_FIELD points at this asset esid."""
    out = []
    for h in team_hits:
        if str((h.get("_source") or {}).get(TEAM_LINK_FIELD) or "") == asset_esid:
            out.append(h)
    return out


def _choose_owner(asset_title, linking_teams, org_text=None) -> dict | None:
    """Pick the owning team hit by best name overlap, else first (deterministic).

    ``org_text`` is an optional ``team_hit -> str`` callable yielding the team's
    PRIMARY-org title + description text; the asset's real subject lives there
    (e.g. asset "<Org Name> <City> <State>" matches the org whose
    description is "<Org Name>" — the raw team_title alone shares no
    tokens). The candidate token set is the team title PLUS that org text.

    Returns the chosen team hit, or None when there are no linking teams.
    """
    if not linking_teams:
        return None
    if len(linking_teams) == 1:
        return linking_teams[0]

    asset_tokens = _tokens(asset_title)

    def score(team_hit: dict) -> tuple:
        src = team_hit.get("_source") or {}
        cand_text = _to_str(src.get("team_title"))
        if org_text is not None:
            cand_text += " " + (org_text(team_hit) or "")
        overlap = len(asset_tokens & _tokens(cand_text))
        # Higher overlap wins; ties -> earliest @timestamp -> lowest esid.
        ts = _to_str(src.get("@timestamp"))
        return (-overlap, ts, team_hit.get("_id") or "")

    return sorted(linking_teams, key=score)[0]


def _build_org_text_lookup(es) -> dict:
    """Map org esid -> "<org_title> <org_description>" tok'able text.

    The asset's matchable subject (e.g. the org name) commonly lives in the
    ORG description, not the team. We resolve a team's PRIMARY org to this text in
    the owner-selection scorer. Fail-soft: an unreadable org index returns {}.
    """
    out: dict = {}
    try:
        for h in _fetch_all(es, "de_asset_organization"):
            s = h.get("_source") or {}
            out[h.get("_id") or ""] = (
                _to_str(s.get("organization_title")) + " " + _to_str(s.get("organization_description"))
            ).strip()
    except Exception as e:  # noqa: BLE001 — heuristic enrichment only
        logger.warning("Could not load org text (matching falls back to team title only): %s", e)
    return out


def run(*, apply: bool) -> int:
    es = get_elasticsearch_client()
    if es is None:
        logger.error("Could not connect to ES; aborting.")
        return 2

    assets = _fetch_all(es, ASSET_INDEX)
    team_hits = _fetch_all(es, TEAMS_INDEX)
    org_text_by_esid = _build_org_text_lookup(es)
    logger.info("Loaded %d chatwoot assets, %d teams, %d orgs.", len(assets), len(team_hits), len(org_text_by_esid))

    def _org_text(team_hit: dict) -> str:
        """The PRIMARY-org title+description text for a team hit (for matching)."""
        return org_text_by_esid.get(_primary_org(team_hit.get("_source") or {}), "")

    planned = applied = skipped_existing = orphan = 0
    for asset in assets:
        esid = asset.get("_id") or ""
        src = asset.get("_source") or {}
        title = _to_str(src.get(ASSET_TITLE_FIELD)) or esid

        if str(src.get(OWNING_FIELD) or "").strip():
            skipped_existing += 1
            continue

        linking = _linking_teams(team_hits, esid)
        owner = _choose_owner(title, linking, org_text=_org_text)
        if owner is None:
            orphan += 1
            logger.info("  ORPHAN  asset=%s title=%r — no linking team; left unstamped.", esid, title)
            continue

        owner_id = owner.get("_id") or ""
        owner_src = owner.get("_source") or {}
        owner_title = _to_str(owner_src.get("team_title")) or owner_id
        logger.info(
            "  OWNER   asset=%s title=%r -> team=%s (%r) primary_org=%s  [%d linking team(s)]",
            esid,
            title,
            owner_id,
            owner_title,
            _primary_org(owner_src),
            len(linking),
        )
        planned += 1
        if apply:
            try:
                es.update(index=ASSET_INDEX, id=esid, doc={OWNING_FIELD: owner_id}, refresh="wait_for")
                applied += 1
            except Exception as e:  # noqa: BLE001
                logger.error("    FAILED %s/%s: %s", ASSET_INDEX, esid, e)

    logger.info("")
    logger.info(
        "%s: planned=%d applied=%d skipped_existing=%d orphan=%d",
        "APPLY" if apply else "DRY-RUN",
        planned,
        applied,
        skipped_existing,
        orphan,
    )
    if not apply:
        logger.info("Dry-run only — pass --apply to write.")
        return 0
    if applied < planned:
        logger.error("Some updates failed (applied=%d < planned=%d).", applied, planned)
        return 1
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--apply", action="store_true", help="Write the stamps. Default is dry-run.")
    args = parser.parse_args(argv)
    logging.basicConfig(format="%(asctime)s %(levelname)s %(message)s", level=logging.INFO, stream=sys.stderr)
    return run(apply=args.apply)


if __name__ == "__main__":
    raise SystemExit(main())
