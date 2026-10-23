"""Keep the org name on a team's record and imported data in step with the org.

Importers copy ``organization_title`` from the TEAM record (``de_asset_teams``)
onto every data doc they write, and org-scoped dashboards and public links
filter on that copy. Two things let it drift:

* an org rename (Asset Explorer edit or onboarding wizard) rewrote only the
  org record, so the team records kept the old name and every later import
  kept stamping it;
* the wizard and invite team-create paths wrote ``organization_esid`` but no
  ``organization_title``, so every import for those teams stamped "Unknown".

The org record is the authority: a team's org names are resolved from its
``organization_esid`` list (a team may belong to several orgs, so the field is
a list, in the same order). :func:`sync_team_org_titles` writes them onto the
team record; :func:`restamp_org_titles` rewrites the team's data docs that
carry anything else. :func:`propagate_org_title` does both for every team of
a renamed org. ``tools/restamp_org_titles.py`` sweeps drift that predates this.

A team whose orgs cannot all be resolved is skipped and reported, never
stamped with a guess.
"""

from __future__ import annotations

import logging
from typing import Any

from lib.es_helpers import dual_term
from lib.team_title_restamp import team_data_index_pattern

ORG_INDEX = "de_asset_organization"
TEAM_INDEX = "de_asset_teams"

_RESTAMP_SCRIPT = "ctx._source.organization_title = params.organization_title"


def _as_list(value: Any) -> list[str]:
    if isinstance(value, str):
        value = [value]
    return [str(v).strip() for v in (value or []) if str(v or "").strip()]


def resolve_org_titles(es: Any, org_esids: list[str] | str | None) -> list[str] | None:
    """The current ``organization_title`` of each org, in ``org_esids`` order.

    None when the list is empty or any org is missing or untitled: a partial
    list would silently drop an org from the team's data.
    """
    esids = _as_list(org_esids)
    if not esids:
        return None
    docs = es.mget(index=ORG_INDEX, body={"ids": esids}).get("docs") or []
    by_id = {d.get("_id"): ((d.get("_source") or {}).get("organization_title") or "").strip() for d in docs if d.get("found")}
    titles = [by_id.get(e, "") for e in esids]
    return titles if all(titles) else None


def sync_team_org_titles(es: Any, team_esid: str) -> list[str] | None:
    """Write the team's current org names onto its record; return them.

    None when the team or any of its orgs cannot be resolved (nothing is
    written). An already-correct record is not rewritten.
    """
    esid = str(team_esid or "").strip()
    if not esid:
        raise ValueError("sync_team_org_titles: team_esid is required")
    team = es.get(index=TEAM_INDEX, id=esid).get("_source") or {}
    titles = resolve_org_titles(es, team.get("organization_esid"))
    if titles is None:
        logging.warning("Team %s: cannot resolve its org name(s); its record and data are left as they are", esid)
        return None
    if _as_list(team.get("organization_title")) != titles:
        es.update(index=TEAM_INDEX, id=esid, doc={"organization_title": titles}, refresh="wait_for")
    return titles


def _present_org_titles(es: Any, team_esid: str) -> set[str]:
    """Every distinct ``organization_title`` value in the team's data.

    The field is bare ``keyword`` in some indices and ``text`` + ``.keyword``
    in others, and a terms aggregation needs the keyword leaf, so the field
    is picked per index from its mapping.
    """
    pattern = team_data_index_pattern(team_esid)
    mappings = es.indices.get_mapping(index=pattern, ignore_unavailable=True, allow_no_indices=True)
    by_field: dict[str, list[str]] = {}
    for index, mapping in dict(mappings).items():
        prop = (mapping.get("mappings") or {}).get("properties", {}).get("organization_title") or {}
        if prop.get("type") == "keyword":
            by_field.setdefault("organization_title", []).append(index)
        elif "keyword" in (prop.get("fields") or {}):
            by_field.setdefault("organization_title.keyword", []).append(index)
    present: set[str] = set()
    for field, indices in by_field.items():
        resp = es.search(index=",".join(indices), size=0, aggs={"t": {"terms": {"field": field, "size": 1000}}})
        present.update(b["key"] for b in resp["aggregations"]["t"]["buckets"])
    return present


def _stale_org_title_query(team_esid: str, titles: list[str], present: set[str]) -> dict[str, Any]:
    # A doc is current when its organization_title holds every current name
    # and nothing else. "Nothing else" cannot be asked of ES directly, so the
    # other names actually present in the team's data are listed. Docs with
    # no organization_title at all (sources that never carry it) are left out.
    stale_if = [{"bool": {"must_not": [{"bool": {"filter": [dual_term("organization_title", t) for t in titles]}}]}}]
    others = sorted(present - set(titles))
    if others:
        stale_if.append({"bool": {"should": [dual_term("organization_title", o) for o in others], "minimum_should_match": 1}})
    return {
        "bool": {
            "filter": [dual_term("team_esid", team_esid), {"exists": {"field": "organization_title"}}],
            "should": stale_if,
            "minimum_should_match": 1,
        }
    }


def _clean_args(team_esid: str, titles: list[str]) -> tuple[str, list[str]]:
    esid = str(team_esid or "").strip()
    clean = _as_list(titles)
    if not esid or not clean:
        raise ValueError("restamp_org_titles: team_esid and at least one org title are required")
    return esid, clean


def count_stale_org_title_docs(es: Any, team_esid: str, titles: list[str]) -> int:
    """Number of the team's data docs whose org names are not exactly ``titles``."""
    esid, clean = _clean_args(team_esid, titles)
    result = es.count(
        index=team_data_index_pattern(esid),
        body={"query": _stale_org_title_query(esid, clean, _present_org_titles(es, esid))},
        ignore_unavailable=True,
        allow_no_indices=True,
    )
    return int(result.get("count", 0))


def restamp_org_titles(es: Any, team_esid: str, titles: list[str], *, wait_for_completion: bool = False) -> dict[str, Any]:
    """Set ``organization_title`` to ``titles`` on the team's data docs that differ.

    Same shape as :func:`lib.team_title_restamp.restamp_team_title`: with
    ``wait_for_completion=False`` ES runs it as a background task and the
    response carries its ``task`` id.
    """
    esid, clean = _clean_args(team_esid, titles)
    return es.update_by_query(
        index=team_data_index_pattern(esid),
        body={
            "query": _stale_org_title_query(esid, clean, _present_org_titles(es, esid)),
            "script": {"source": _RESTAMP_SCRIPT, "lang": "painless", "params": {"organization_title": clean}},
        },
        conflicts="proceed",
        refresh=True,
        ignore_unavailable=True,
        allow_no_indices=True,
        wait_for_completion=wait_for_completion,
    )


def sync_and_restamp_team(es: Any, team_esid: str, *, wait_for_completion: bool = False) -> dict[str, Any] | None:
    """Sync the team record's org names, then restamp its data. None when unresolvable."""
    titles = sync_team_org_titles(es, team_esid)
    if titles is None:
        return None
    return restamp_org_titles(es, team_esid, titles, wait_for_completion=wait_for_completion)


def team_esids_for_org(es: Any, org_esid: str) -> list[str]:
    """Every team whose ``organization_esid`` list includes ``org_esid``."""
    resp = es.search(
        index=TEAM_INDEX,
        body={"query": {"bool": {"filter": [dual_term("organization_esid", org_esid)]}}, "_source": False, "size": 10000},
    )
    return [h["_id"] for h in resp.get("hits", {}).get("hits", [])]


def propagate_org_title(es: Any, org_esid: str, *, wait_for_completion: bool = False) -> dict[str, Any]:
    """After an org rename: sync and restamp every team of the org.

    One team's failure is logged and does not stop the others; the sweep
    tool repairs whatever is left. Returns ``{team_esid: result}`` (None for
    a team that could not be resolved, the exception for one that failed).
    """
    esid = str(org_esid or "").strip()
    if not esid:
        raise ValueError("propagate_org_title: org_esid is required")
    results: dict[str, Any] = {}
    for team_esid in team_esids_for_org(es, esid):
        try:
            results[team_esid] = sync_and_restamp_team(es, team_esid, wait_for_completion=wait_for_completion)
        except Exception as e:  # noqa: BLE001 — one team must not block the rest; the sweep tool repairs it
            logging.error("Org %s renamed but updating team %s failed: %s", esid, team_esid, e)
            results[team_esid] = e
    return results
