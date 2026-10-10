"""Authoritative per-team FB import health for the unified-app migration.

For every team with ``ds_facebook`` enabled, reports — in one table —
which token path its pages take (legacy app vs wizard/unified), whether
ad-insights docs are still landing, and whether page-insights docs are
landing with REAL metrics (non-null) or as rows of zeros. Teams are
classified failing-first so the output doubles as the re-auth worklist
for the FB-app consolidation
(the private docs).

This complements the existing tools:
- ``fb_page_insights_app_inventory`` — which legacy apps shipped data
  (app-first view). This tool is team-first and adds ads freshness.
- ``fb_app_status_audit`` — whether an app secret authenticates.

A team is FAILING when any of:
- ``ads_stale``: ad-insights docs exist but the newest is older than
  ``--stale-days`` (default 3).
- ``ads_missing``: no ad-insights docs at all.
- ``pages_stale``: page-insights docs exist but the newest is older
  than ``--stale-days``.
- ``page_metrics_dead``: page-insights docs are landing but none in the
  last ``--real-window-days`` (default 14) carries real metrics
  (``page_actions_post_reactions_total > 0`` AND
  ``page_impressions_unique > 0`` — same bar as the inventory tool).

Usage (inside imports_container)::

    docker exec -w /app/python imports_container python3 \\
        -m tools.fb_legacy_team_health

    # JSON output for piping
    docker exec -w /app/python imports_container python3 \\
        -m tools.fb_legacy_team_health --json
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import date, datetime
from pathlib import Path
from typing import Any

# Three-candidate resolver — matches the other tools in this directory.
_HERE = Path(__file__).resolve().parent
for _candidate in (
    _HERE.parents[1],
    _HERE.parents[2] / "bin" / "python",
    Path("/app/python"),
):
    if (_candidate / "lib" / "es_helpers.py").exists():
        if str(_candidate) not in sys.path:
            sys.path.insert(0, str(_candidate))
        break

from lib.es_helpers import (  # noqa: E402
    build_data_index_name,
    get_all_documents,
    get_elasticsearch_client,
)
from lib.fb_app_attribution import resolve_page_path  # noqa: E402

_TEAMS_INDEX = "de_asset_teams"
_PAGES_INDEX = "de_asset_fb_pages"
_APPS_INDEX = "de_asset_fb_apps"

# Same "real metrics" bar as fb_page_insights_app_inventory — a doc where
# Meta returned non-zero engagement, not just a row the importer wrote.
_REAL_PAGE_METRIC_FILTERS = [
    {"range": {"page_actions_post_reactions_total": {"gt": 0}}},
    {"range": {"page_impressions_unique": {"gt": 0}}},
]


def _parse_iso_date(value: str | None) -> date | None:
    """``"2026-07-12"`` / ``"2026-07-12T00:00:00.000Z"`` → date, else None."""
    if not value:
        return None
    try:
        return datetime.fromisoformat(value[:10]).date()
    except ValueError:
        return None


def classify_team_health(
    *,
    path: str,
    ads_latest: date | None,
    ads_doc_count: int,
    pages_latest: date | None,
    pages_doc_count: int,
    pages_real_recent: int,
    today: date,
    stale_days: int = 3,
    ads_only: bool = False,
) -> tuple[str, list[str]]:
    """Return ``(status, reasons)`` for one team.

    ``status`` is ``"OK"``, ``"FAILING"``, or ``"MIGRATED"`` (wizard path
    and healthy). ``reasons`` lists every failing condition; empty when OK.
    ``ads_only`` skips the page-insights checks — as of Meta v23 the page
    metrics are deprecated platform-wide, so ``page_metrics_dead`` fires on
    every team and drowns the ads signal. Pure function — all ES reads
    happen in the caller.
    """
    reasons: list[str] = []

    if ads_doc_count == 0:
        reasons.append("ads_missing")
    elif ads_latest is None or (today - ads_latest).days > stale_days:
        reasons.append("ads_stale")

    if not ads_only:
        if pages_doc_count == 0:
            reasons.append("pages_missing")
        else:
            if pages_latest is None or (today - pages_latest).days > stale_days:
                reasons.append("pages_stale")
            if pages_real_recent == 0:
                reasons.append("page_metrics_dead")

    if reasons:
        return "FAILING", reasons
    if path == "wizard":
        return "MIGRATED", []
    return "OK", []


def _load_lookups() -> dict[str, Any]:
    """One ES round-trip per asset index; keyed maps for the chain walk."""
    teams: list[dict] = []
    for hit in get_all_documents(_TEAMS_INDEX) or []:
        if isinstance(hit, dict) and hit.get("_id"):
            src = hit.get("_source", {})
            src["_id"] = hit["_id"]
            teams.append(src)

    pages_by_esid: dict[str, dict] = {}
    for hit in get_all_documents(_PAGES_INDEX) or []:
        if isinstance(hit, dict) and hit.get("_id"):
            pages_by_esid[hit["_id"]] = hit.get("_source", {})

    app_titles_by_fb_account_esid: dict[str, list[str]] = {}
    for hit in get_all_documents(_APPS_INDEX) or []:
        if not isinstance(hit, dict) or not hit.get("_id"):
            continue
        src = hit.get("_source", {})
        fb_account_esid = src.get("fb_account_esid")
        if fb_account_esid:
            app_titles_by_fb_account_esid.setdefault(fb_account_esid, []).append(
                src.get("fb_app_title") or src.get("fb_app_id") or hit["_id"]
            )

    return {
        "teams": teams,
        "pages_by_esid": pages_by_esid,
        "app_titles_by_fb_account_esid": app_titles_by_fb_account_esid,
    }


def _index_freshness(es: Any, index_name: str, real_window_days: int) -> dict[str, Any]:
    """Return ``{doc_count, latest, real_recent}`` for one data index.

    ``no_data: true`` marker docs (earlier leftovers) are excluded so a
    marker can't masquerade as a fresh real doc. ``real_recent`` only makes
    sense for page-insights indices; callers ignore it for fb_insights.
    Missing index → all-zero result (ignore_unavailable).
    """
    body = {
        "size": 0,
        "track_total_hits": True,
        "query": {"bool": {"must_not": [{"term": {"no_data": True}}]}},
        "aggs": {
            "latest": {"max": {"field": "kibana_date"}},
            "real_recent": {
                "filter": {
                    "bool": {
                        "must": _REAL_PAGE_METRIC_FILTERS + [{"range": {"kibana_date": {"gte": f"now-{real_window_days}d/d"}}}]
                    }
                }
            },
        },
    }
    try:
        resp = es.search(index=index_name, body=body, ignore_unavailable=True, allow_no_indices=True)
    except Exception as e:  # noqa: BLE001 — surface but don't abort the sweep
        print(f"search failed for {index_name}: {e}", file=sys.stderr)
        return {"doc_count": 0, "latest": None, "real_recent": 0}
    doc_count = int(((resp.get("hits", {}).get("total") or {}).get("value")) or 0)
    latest = (resp.get("aggregations", {}).get("latest") or {}).get("value_as_string")
    real_recent = int(((resp.get("aggregations", {}).get("real_recent") or {}).get("doc_count")) or 0)
    return {"doc_count": doc_count, "latest": _parse_iso_date(latest), "real_recent": real_recent}


def _resolve_page_path(team: dict, lookups: dict[str, Any]) -> tuple[str, int, int, list[str]]:
    """Walk the team's pages. Returns ``(path, wizard_pages, total_pages, legacy_apps)``.

    ``path`` is ``wizard`` (every page stamped ``wizard_connection_esid``),
    ``legacy`` (none stamped), ``mixed``, or ``no-pages``. The classification
    itself lives in :mod:`lib.fb_app_attribution` (extracted so
    the Report Scheduler surfaces share the same rule); this wrapper only
    adapts this tool's ``lookups`` shape.
    """
    page_esids = team.get("team_facebook_page_esids") or []
    if isinstance(page_esids, str):
        page_esids = [page_esids]
    return resolve_page_path(page_esids, lookups["pages_by_esid"], lookups["app_titles_by_fb_account_esid"])


def build_report(stale_days: int, real_window_days: int, ads_only: bool = False) -> list[dict[str, Any]]:
    """Run the sweep. Returns one row per ds_facebook team, failing-first."""
    es = get_elasticsearch_client()
    if es is None:
        raise SystemExit("could not connect to Elasticsearch")

    lookups = _load_lookups()
    today = date.today()

    rows: list[dict[str, Any]] = []
    for team in lookups["teams"]:
        if str(team.get("ds_facebook")).lower() != "true":
            continue
        team_esid = team["_id"]
        path, wizard_pages, total_pages, legacy_apps = _resolve_page_path(team, lookups)

        ads = _index_freshness(es, build_data_index_name(team_esid, "fb_insights"), real_window_days)
        pages = _index_freshness(es, build_data_index_name(team_esid, "fb_page_insights"), real_window_days)

        status, reasons = classify_team_health(
            path=path,
            ads_latest=ads["latest"],
            ads_doc_count=ads["doc_count"],
            pages_latest=pages["latest"],
            pages_doc_count=pages["doc_count"],
            pages_real_recent=pages["real_recent"],
            today=today,
            stale_days=stale_days,
            ads_only=ads_only,
        )
        rows.append(
            {
                "team_title": team.get("team_title") or "(untitled)",
                "team_esid": team_esid,
                "status": status,
                "reasons": reasons,
                "path": path,
                "pages_stamped": f"{wizard_pages}/{total_pages}",
                "legacy_apps": legacy_apps,
                "ads_latest": ads["latest"].isoformat() if ads["latest"] else "",
                "ads_docs": ads["doc_count"],
                "pages_latest": pages["latest"].isoformat() if pages["latest"] else "",
                "pages_docs": pages["doc_count"],
                "pages_real_recent": pages["real_recent"],
            }
        )

    _STATUS_ORDER = {"FAILING": 0, "OK": 1, "MIGRATED": 2}
    rows.sort(key=lambda r: (_STATUS_ORDER.get(r["status"], 9), r["team_title"].lower()))
    return rows


def _print_report(rows: list[dict[str, Any]], stale_days: int, real_window_days: int) -> None:
    """Render the report as one ASCII table plus a legend."""
    print(f"ds_facebook teams: {len(rows)}   stale after: {stale_days}d   real-metrics window: {real_window_days}d")
    print()
    if not rows:
        print("(no ds_facebook teams found)")
        return

    headers = ("Team", "Status", "Reasons", "Path", "Stamped", "Legacy apps", "Ads latest", "Pg latest", "Pg real")
    table = [
        (
            r["team_title"][:20],
            r["status"],
            ",".join(r["reasons"])[:40],
            r["path"],
            r["pages_stamped"],
            ", ".join(r["legacy_apps"])[:40],
            r["ads_latest"],
            r["pages_latest"],
            str(r["pages_real_recent"]),
        )
        for r in rows
    ]
    widths = [max(len(headers[i]), *(len(row[i]) for row in table)) for i in range(len(headers))]
    fmt = "  ".join(f"{{:<{w}}}" for w in widths)
    print(fmt.format(*headers))
    print(fmt.format(*("-" * w for w in widths)))
    for row in table:
        print(fmt.format(*row))

    print()
    print(
        "Status: FAILING = re-auth candidate (see Reasons); OK = legacy path but\n"
        "healthy (can wait for App Review); MIGRATED = wizard/unified path and healthy.\n"
        "'Stamped' = pages carrying wizard_connection_esid / total pages.\n"
        f"'Pg real' = page-insights docs in the last {real_window_days}d with non-zero\n"
        "reactions AND unique impressions (the inventory tool's 'Real' bar).\n"
        "ads_missing on a page-only team may be expected — check the team's setup\n"
        "before treating it as a failure."
    )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="fb_legacy_team_health",
        description="Per-team FB import health / re-auth worklist for the unified-app migration.",
    )
    parser.add_argument("--json", action="store_true", help="Emit JSON instead of the table.")
    parser.add_argument("--stale-days", type=int, default=3, help="Newest doc older than N days = stale (default 3).")
    parser.add_argument(
        "--real-window-days",
        type=int,
        default=14,
        help="Window for the real-page-metrics check (default 14).",
    )
    parser.add_argument(
        "--ads-only",
        action="store_true",
        help="Verdict on ad-insights freshness only (page metrics are deprecated platform-wide as of Meta v23).",
    )
    args = parser.parse_args(argv)

    rows = build_report(stale_days=args.stale_days, real_window_days=args.real_window_days, ads_only=args.ads_only)
    if args.json:
        print(json.dumps(rows, indent=2, default=str))
    else:
        _print_report(rows, args.stale_days, args.real_window_days)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
