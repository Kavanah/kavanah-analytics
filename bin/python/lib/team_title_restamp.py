"""Keep the team name on a team's imported data in step with its team record.

Importers copy ``team_title`` from ``de_asset_teams`` onto every data doc they
write, and the dashboards filter on that copy (``team_title``), not on
``team_esid``. A rename that only rewrites the team record therefore leaves the
team's whole history under the old name: every dashboard, Summary section and
published link for the team comes back empty from the moment of the rename,
for every date range. On 2026-10-21 a team renamed in the Asset Explorer lost
all of its quarterly reports this way.

``restamp_team_title`` rewrites ``team_title`` on the team's own
``{team_esid}__*`` indices to the current name. Every rename path calls it, and
``tools/restamp_team_titles.py`` sweeps teams whose data drifted before this
existed (or whose import was mid-run during a rename and stamped the old name).
"""

from __future__ import annotations

from typing import Any

from lib.es_helpers import build_data_index_name, dual_term

_RESTAMP_SCRIPT = "ctx._source.team_title = params.team_title"


def team_data_index_pattern(team_esid: str) -> str:
    """``{team_esid}__*`` (lowercase): every data index of one team."""
    return build_data_index_name(team_esid, "*")


def _stale_title_query(team_esid: str, team_title: str) -> dict[str, Any]:
    # team_esid narrows to the team's own docs even if a pattern ever matched
    # more than one team's indices. Mapping shapes drift between bare keyword
    # and text + .keyword across envs, hence dual_term on both fields.
    return {
        "bool": {
            "filter": [dual_term("team_esid", team_esid)],
            "must_not": [dual_term("team_title", team_title)],
        }
    }


def _clean_args(team_esid: str, team_title: str) -> tuple[str, str]:
    esid = str(team_esid or "").strip()
    title = str(team_title or "").strip()
    if not esid or not title:
        raise ValueError("restamp_team_title: team_esid and team_title are required")
    return esid, title


def count_stale_team_title_docs(es: Any, team_esid: str, team_title: str) -> int:
    """Number of the team's data docs whose ``team_title`` is not ``team_title``."""
    esid, title = _clean_args(team_esid, team_title)
    result = es.count(
        index=team_data_index_pattern(esid),
        body={"query": _stale_title_query(esid, title)},
        ignore_unavailable=True,
        allow_no_indices=True,
    )
    return int(result.get("count", 0))


def restamp_team_title(es: Any, team_esid: str, team_title: str, *, wait_for_completion: bool = False) -> dict[str, Any]:
    """Set ``team_title`` to ``team_title`` on every data doc of the team that differs.

    Doc ids never include the team name, so a restamped doc is the same doc
    the next import overwrites; nothing is duplicated. With
    ``wait_for_completion=False`` (the request paths) ES runs the update as a
    background task and the response carries its ``task`` id; a large team's
    history would otherwise hold the request open for minutes.
    """
    esid, title = _clean_args(team_esid, team_title)
    return es.update_by_query(
        index=team_data_index_pattern(esid),
        body={
            "query": _stale_title_query(esid, title),
            "script": {"source": _RESTAMP_SCRIPT, "lang": "painless", "params": {"team_title": title}},
        },
        conflicts="proceed",
        refresh=True,
        ignore_unavailable=True,
        allow_no_indices=True,
        wait_for_completion=wait_for_completion,
    )
