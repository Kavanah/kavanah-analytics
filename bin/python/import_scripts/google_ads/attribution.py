"""Which DED team owns which Google Ads campaign.

One Google Ads account (customer) can run campaigns for several teams. A team
claims campaigns with RULES stored on its team doc:

* ``team_google_ads_campaign_labels``: campaign labels, matched exactly
  (e.g. ``KAT: TEAM1``; labels are imported);
* ``team_google_ads_campaign_prefixes``: campaign-name prefixes, matched
  case-insensitively (e.g. ``Ministry-``).

Rules, not picked campaign ids, because these accounts create campaigns every
month: picks would go stale, rules keep attributing new campaigns.

Modes, per customer:

* **Legacy** — the account is linked to ONE team (rules are irrelevant: a team's
  rules exist to split shared accounts, and must not filter an account it has
  to itself), or no linked team has any rule. The lowest team_esid gets the
  whole account, exactly as before.
* **Rules** — the account is linked to several teams and any has a rule. Each campaign goes to the one team
  whose rules match it. A campaign matching no team, or several, is declined and
  reported — never guessed (a guess writes one team's campaigns onto another
  team's index). A linked team with no rules gets nothing, and that is reported.
"""

from __future__ import annotations

import json
import logging
import os
from dataclasses import dataclass, field
from typing import Any

from lib.es_helpers import get_elasticsearch_client

TEAMS_INDEX = "de_asset_teams"
LABELS_FIELD = "team_google_ads_campaign_labels"
PREFIXES_FIELD = "team_google_ads_campaign_prefixes"
DATA_ROOT = "/app/data/google_ads"

UNASSIGNED = "unassigned"
AMBIGUOUS = "ambiguous"


@dataclass(frozen=True)
class Rules:
    labels: frozenset[str] = frozenset()
    prefixes: tuple[str, ...] = ()

    def __bool__(self) -> bool:
        return bool(self.labels or self.prefixes)

    def matches(self, name: str | None, labels: list[str] | None) -> bool:
        if self.labels and self.labels.intersection(labels or []):
            return True
        lowered = (name or "").strip().lower()
        return any(lowered.startswith(p) for p in self.prefixes)


def team_rules(team: dict[str, Any] | None) -> Rules:
    """The team's campaign rules, cleaned: blank entries dropped, prefixes
    lower-cased for case-insensitive matching."""
    doc = team or {}
    labels = frozenset(str(v).strip() for v in doc.get(LABELS_FIELD) or [] if str(v).strip())
    prefixes = tuple(dict.fromkeys(str(v).strip().lower() for v in doc.get(PREFIXES_FIELD) or [] if str(v).strip()))
    return Rules(labels, prefixes)


def get_linked_teams(customer_esid: str) -> list[tuple[str, dict[str, Any]]]:
    """Every Active, Google-Ads-enabled team linking this customer (via the
     list field or the legacy scalar), sorted by team_esid.

    Raises on an ES failure: an outage must not read as "no team is linked"
    (es_helpers.search_index swallows errors into an empty result, so the client
    is queried directly)."""
    es = get_elasticsearch_client()
    if es is None:
        raise RuntimeError("Elasticsearch is unavailable — cannot resolve Google Ads team links")
    results = es.search(
        index=TEAMS_INDEX,
        size=100,
        query={
            "bool": {
                # bare keyword fields on de_asset_teams.
                "must": [
                    {"term": {"team_google_ads_integration_status": "Active"}},
                    {"term": {"ds_google_ads": "true"}},
                ],
                "should": [
                    {"term": {"team_google_ads_customer_esids": customer_esid}},
                    {"term": {"google_ads_customer_esid": customer_esid}},
                ],
                "minimum_should_match": 1,
            }
        },
    )
    hits = results.get("hits", {}).get("hits", [])
    return sorted(((h["_id"], h.get("_source") or {}) for h in hits), key=lambda pair: pair[0])


def team_metadata(team_esid: str, team: dict[str, Any]) -> dict[str, Any]:
    """The team fields stamped onto every saved row (the shape
    get_team_by_google_ads_customer_esid has always returned)."""
    return {
        "team_esid": team_esid,
        "team_title": team.get("team_title", "Unknown"),
        "team_url": team.get("team_url", "unknown"),
        "team_country": team.get("team_country", "unknown"),
        "team_city": team.get("team_city", "unknown"),
        "organization_title": team.get("organization_title", "Unknown"),
    }


@dataclass
class CustomerPlan:
    """The attribution decision for one customer."""

    teams: list[tuple[str, dict[str, Any]]]
    rules_mode: bool
    rules: dict[str, Rules] = field(default_factory=dict)
    problems: list[str] = field(default_factory=list)

    @property
    def legacy_team(self) -> tuple[str, dict[str, Any]] | None:
        """Legacy mode's single owner: the lowest team_esid."""
        return None if self.rules_mode or not self.teams else self.teams[0]

    def owner_of(self, name: str | None, labels: list[str] | None) -> tuple[str | None, str]:
        """(team_esid, "matched") for a campaign, or (None, UNASSIGNED / AMBIGUOUS)."""
        if not self.rules_mode:
            legacy = self.legacy_team
            return (legacy[0], "matched") if legacy else (None, UNASSIGNED)
        owners = [esid for esid, rules in self.rules.items() if rules.matches(name, labels)]
        if len(owners) == 1:
            return owners[0], "matched"
        return None, (UNASSIGNED if not owners else AMBIGUOUS)


def plan_customer(customer_title: str, teams: list[tuple[str, dict[str, Any]]]) -> CustomerPlan:
    rules = {esid: team_rules(doc) for esid, doc in teams}
    if len(teams) < 2 or not any(rules.values()):
        plan = CustomerPlan(teams=teams, rules_mode=False)
        if len(teams) > 1:
            names = ", ".join(str(doc.get("team_title") or esid) for esid, doc in teams)
            plan.problems.append(
                f"Google Ads customer {customer_title} is linked to {len(teams)} teams ({names}) and none has campaign "
                f"rules — importing every campaign to {teams[0][1].get('team_title') or teams[0][0]} only. "
                "Set each team's campaign labels or name prefixes to split it."
            )
        return plan
    plan = CustomerPlan(teams=teams, rules_mode=True, rules={esid: r for esid, r in rules.items() if r})
    for esid, doc in teams:
        if not rules[esid]:
            plan.problems.append(
                f"Team {doc.get('team_title') or esid} shares Google Ads customer {customer_title} with other teams but "
                "has no campaign rules — it receives none of its campaigns."
            )
    return plan


def prune_foreign_docs(es: Any, index: str, customer_esid: str, foreign_campaign_ids: set[str]) -> int:
    """Delete this customer's rows in ``index`` for campaigns that belong to
    another team or to none (perf, geo and metadata alike).

    Only campaigns SEEN this run are named: a campaign that no longer exists in
    Google (deleted, or gone from the account) is left alone, so a team never
    loses the history of a campaign it rightly owned. A missing index counts as 0."""
    if not foreign_campaign_ids:
        return 0
    query: dict[str, Any] = {
        "bool": {
            "filter": [
                {"term": {"google_ads_customer_esid": customer_esid}},
                {"terms": {"campaign_id": sorted(foreign_campaign_ids)}},
            ]
        }
    }
    result = es.delete_by_query(index=index, body={"query": query}, ignore_unavailable=True, refresh=True, conflicts="proceed")
    deleted = int((result or {}).get("deleted") or 0)
    if deleted:
        logging.info(f"removed {deleted} Google Ads row(s) from {index} for campaigns this team does not own.")
    return deleted


def prune_foreign_files(customer_esid: str, team_esid: str, foreign_campaign_ids: set[str]) -> int:
    """Delete this team's saved JSON (pending and archived) for campaigns that
    belong to another team or to none. The indexer re-reads pending files and
     trusts archived ones, so a stale file would put the rows back."""
    removed = 0
    for sub in ("app_campaigns", "app_campaigns_imported"):
        directory = os.path.join(DATA_ROOT, sub, customer_esid, team_esid)
        if not os.path.isdir(directory):
            continue
        for name in os.listdir(directory):
            if not name.endswith(".json"):
                continue
            path = os.path.join(directory, name)
            try:
                with open(path) as f:
                    campaign_id = str(json.load(f).get("campaign_id") or "")
            except (OSError, json.JSONDecodeError):
                continue
            if campaign_id in foreign_campaign_ids:
                os.remove(path)
                removed += 1
    if removed:
        logging.info(f"removed {removed} stale file(s) for customer {customer_esid}, team {team_esid}.")
    return removed
