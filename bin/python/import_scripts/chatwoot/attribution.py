"""Which DED team owns which Chatwoot data.

A Chatwoot account can serve several teams: one self-hosted server hosts inboxes
for different ministries. A team lists the inboxes it owns in
``de_asset_teams.chatwoot_inbox_ids``, beside its ``chatwoot_account_esid`` link.

Rules:

* **Inbox mode.** Once any team on an account lists inboxes, data is attributed
  per inbox: each listed inbox goes to its one owning team.
* **Account mode (back-compat).** No team on the account lists inboxes and
  exactly one team links it: that team gets the whole account, as before.
* **Decline, never guess.** A team on a shared account that lists no inboxes, or
  an inbox listed by two teams, is skipped and reported. Guessing would put one
  ministry's conversations in another's index (the CLAUDE.md cross-tenant rule).

Scoped runs (``--team_ids``) only ever produce data for the teams in scope, but
the decisions above always consider every team linked to the account.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any

TEAMS_INDEX = "de_asset_teams"
TEAMS_PAGE_SIZE = 100


@dataclass(frozen=True)
class Target:
    """One slice of an account's data and the team it belongs to.

    ``inbox`` is None in account mode (the whole account), else the inbox's
    ``{"id", "name", "channel_type", "page_id", "instagram_id"}``.
    """

    team_esid: str | None
    team: dict[str, Any] | None
    inbox: dict[str, Any] | None = None

    @property
    def inbox_id(self) -> str | None:
        return None if self.inbox is None else str(self.inbox["id"])


@dataclass
class Plan:
    """The attribution decision for one account."""

    inbox_mode: bool
    targets: list[Target] = field(default_factory=list)
    # inbox id (str) -> owning team esid, for every inbox exactly one team owns.
    owners: dict[str, str] = field(default_factory=dict)
    problems: list[str] = field(default_factory=list)


def team_inbox_ids(team: dict[str, Any] | None) -> list[str]:
    """The team's listed inbox ids as strings, order kept, duplicates dropped."""
    seen: list[str] = []
    for raw in (team or {}).get("chatwoot_inbox_ids") or []:
        value = str(raw).strip()
        if value and value not in seen:
            seen.append(value)
    return seen


def get_teams_for_account(es: Any, account_esid: str) -> list[tuple[str, dict[str, Any]]]:
    """Every team with the Chatwoot connector on that links this account, as
    ``(team_esid, team_doc)``, sorted by esid so runs are deterministic.

    Raises on an ES failure: an outage must not look like "no team links the
    account".
    """
    # bare chatwoot_account_esid — explicit keyword post-reindex.
    query = {"bool": {"must": [{"term": {"chatwoot_account_esid": account_esid}}, {"term": {"ds_chatwoot": "true"}}]}}
    result = es.search(index=TEAMS_INDEX, body={"query": query, "size": TEAMS_PAGE_SIZE})
    hits = result.get("hits", {}).get("hits", [])
    return sorted(((h["_id"], h["_source"]) for h in hits), key=lambda pair: pair[0])


def plan_account(
    account_title: str,
    teams: list[tuple[str, dict[str, Any]]],
    inboxes: list[dict[str, Any]] | None,
    scope_team_ids: set[str] | None = None,
) -> Plan:
    """Decide which team gets which slice of one account's data.

    ``inboxes`` is the account's inbox list from Chatwoot. None or empty means it
    could not be read; listed inbox ids are then used without names rather than
    dropped. ``scope_team_ids`` limits the targets to those teams (None = all).
    """
    in_scope = (lambda esid: True) if scope_team_ids is None else (lambda esid: esid in scope_team_ids)
    listed = {esid: team_inbox_ids(doc) for esid, doc in teams}

    if not any(listed.values()):
        if len(teams) > 1:
            names = ", ".join(sorted(str(doc.get("team_title") or esid) for esid, doc in teams))
            return Plan(
                inbox_mode=False,
                problems=[
                    f"Chatwoot account '{account_title}' is linked to {len(teams)} teams ({names}) and none has "
                    "picked its inboxes — not importing it for any of them. Pick each team's inboxes."
                ],
            )
        if not teams:
            return Plan(inbox_mode=False, targets=[Target(None, None)])
        esid, doc = teams[0]
        return Plan(inbox_mode=False, targets=[Target(esid, doc)] if in_scope(esid) else [])

    plan = Plan(inbox_mode=True)
    claims: dict[str, list[str]] = {}
    for esid, ids in listed.items():
        for inbox_id in ids:
            claims.setdefault(inbox_id, []).append(esid)
    for inbox_id, claimants in sorted(claims.items()):
        if len(claimants) > 1:
            plan.problems.append(
                f"Chatwoot inbox {inbox_id} on '{account_title}' is picked by {len(claimants)} teams "
                f"({', '.join(sorted(claimants))}) — importing it for none of them."
            )
        else:
            plan.owners[inbox_id] = claimants[0]
    for esid, doc in teams:
        if not listed[esid]:
            plan.problems.append(
                f"Team '{doc.get('team_title') or esid}' shares Chatwoot account '{account_title}' with other teams "
                "but has not picked its inboxes — skipped."
            )

    by_id = {str(i.get("id")): i for i in (inboxes or []) if i.get("id") is not None}
    docs = dict(teams)
    for inbox_id, esid in sorted(plan.owners.items(), key=lambda kv: (kv[1], _sort_key(kv[0]))):
        if not in_scope(esid):
            continue
        inbox = by_id.get(inbox_id)
        if inbox is None:
            if by_id:
                plan.problems.append(
                    f"Chatwoot inbox {inbox_id} picked by team '{docs[esid].get('team_title') or esid}' does not "
                    f"exist on '{account_title}' — skipped."
                )
                continue
            inbox = {"id": inbox_id, "name": None, "channel_type": None, "page_id": None, "instagram_id": None}
        plan.targets.append(Target(esid, docs[esid], inbox))
    return plan


def log_problems(plan: Plan) -> None:
    for problem in plan.problems:
        logging.error(problem)


def prune_foreign_docs(es: Any, index: str, account_esid: str, owned_inbox_ids: list[str], date: str | None = None) -> int:
    """Delete this account's docs in ``index`` that are not from an owned inbox.

    That covers account-level rows (no ``inbox_id``) and rows from inboxes the
    team no longer owns. ``date`` limits it to one day. Returns the count
    deleted; a missing index counts as 0.
    """
    query: dict[str, Any] = {
        "bool": {
            "filter": [{"term": {"chatwoot_account_esid": account_esid}}],
            "must_not": [{"terms": {"inbox_id": [int(i) for i in owned_inbox_ids if i.isdigit()]}}],
        }
    }
    if date is not None:
        query["bool"]["filter"].append({"term": {"date": date}})
    result = es.delete_by_query(index=index, body={"query": query}, ignore_unavailable=True, refresh=True, conflicts="proceed")
    deleted = int((result or {}).get("deleted") or 0)
    if deleted:
        logging.info(f"Removed {deleted} Chatwoot doc(s) from {index} that no longer belong to it.")
    return deleted


def _sort_key(inbox_id: str) -> tuple[int, str]:
    return (int(inbox_id), "") if inbox_id.isdigit() else (1 << 62, inbox_id)
