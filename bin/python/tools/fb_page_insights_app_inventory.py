"""Inventory which FB Apps have shipped real page-insights data.

For each ``*__fb_page_insights`` index that contains documents, walks
the asset chain (team → fb_pages → fb_account ← fb_apps) and prints
which FB Apps the team's pages are configured to use. Output is also
aggregated app-first so you can hand the list of "apps that actually
ship data" to the team for App-Review status checks.

This complements ``fb_app_status_audit`` — that one tells you whether
an app's stored secret authenticates to Graph; this one tells you
whether an app has actually shipped page-insights documents.

**Asset chain caveat — legacy vs wizard:**
The legacy import path resolves apps via
``team.team_facebook_page_esids[] → fb_page.fb_account_esid
 ← fb_app.fb_account_esid`` (one fb_account can be shared by multiple
apps). Wizard imports carry the app linkage in
``de_connection`` instead. This tool resolves the legacy chain only;
wizard-imported teams will show in the per-index list but with an
``apps_via_legacy_chain=[]`` annotation. Treat that as "check the
wizard connection record manually" rather than "no app."

Usage (inside imports_container)::

    docker exec -w /app/python imports_container python3 \\
        -m tools.fb_page_insights_app_inventory

    # JSON output for piping
    docker exec -w /app/python imports_container python3 \\
        -m tools.fb_page_insights_app_inventory --json

    # Only show indices with at least N docs (default 1)
    docker exec -w /app/python imports_container python3 \\
        -m tools.fb_page_insights_app_inventory --min-docs 100
"""

from __future__ import annotations

import argparse
import json
import sys
from collections import defaultdict
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

from lib.es_helpers import get_all_documents, get_elasticsearch_client  # noqa: E402

_PAGE_INSIGHTS_PATTERN = "*__fb_page_insights"
_TEAMS_INDEX = "de_asset_teams"
_PAGES_INDEX = "de_asset_fb_pages"
_APPS_INDEX = "de_asset_fb_apps"


def _list_physical_indices(es, pattern: str) -> list[str]:
    """Return concrete index names matching ``pattern``.

    Uses cat.indices so we only get backing indices, not aliases —
     scheme keeps both around and we don't want to count
    them twice.
    """
    try:
        rows = es.cat.indices(index=pattern, format="json", h="index")
    except Exception as e:  # noqa: BLE001 — top-level boundary
        print(f"cat.indices failed for {pattern}: {e}", file=sys.stderr)
        return []
    return sorted({r["index"] for r in rows if r.get("index")})


def _index_stats(es, index_name: str) -> tuple[int, int, str | None]:
    """Return ``(doc_count, populated_doc_count, latest_iso_timestamp)``.

    ``populated_doc_count`` = docs where BOTH
    ``page_actions_post_reactions_total > 0`` AND
    ``page_impressions_unique > 0``. This is the bar for "the importer
    didn't just write rows full of nulls" — useful for telling apart
    apps that are genuinely shipping data from apps whose imports are
    silently returning empty metrics (e.g. Meta v23 deprecations).

    Single search call: total hits + a filter agg for the populated
    count + a max agg for the latest timestamp.
    """
    try:
        body = {
            "size": 0,
            "track_total_hits": True,
            "aggs": {
                "latest": {"max": {"field": "@timestamp"}},
                "populated": {
                    "filter": {
                        "bool": {
                            "must": [
                                {"range": {"page_actions_post_reactions_total": {"gt": 0}}},
                                {"range": {"page_impressions_unique": {"gt": 0}}},
                            ]
                        }
                    }
                },
            },
        }
        result = es.search(index=index_name, body=body)
    except Exception as e:  # noqa: BLE001 — surface but don't abort the sweep
        print(f"search failed for {index_name}: {e}", file=sys.stderr)
        return 0, 0, None
    doc_count = (result.get("hits", {}).get("total") or {}).get("value", 0)
    populated = (result.get("aggregations", {}).get("populated") or {}).get("doc_count", 0)
    latest_raw = (result.get("aggregations", {}).get("latest") or {}).get("value_as_string")
    return int(doc_count or 0), int(populated or 0), latest_raw


def _parse_team_esid(index_name: str) -> str | None:
    """``{team_esid}__fb_page_insights`` → ``{team_esid}``.

    Returns None if the index doesn't follow the naming
    convention (pre-migration indices like ``test_account_fb_page_insights``
    don't have the double-underscore).
    """
    if "__" not in index_name:
        return None
    return index_name.split("__", 1)[0]


def _load_asset_lookups() -> dict[str, dict]:
    """Build the maps we'll use for the asset-chain walk.

    Returns a dict with:
      - ``teams_by_esid``: {team_esid: team_source}
      - ``pages_by_esid``: {page_esid: page_source}
      - ``apps_by_fb_account_esid``: {fb_account_esid: [app_source, ...]}

    Single ES round-trip each, no per-index N+1.
    """
    # Team _ids are mixed-case ES auto-generated IDs (e.g. "OFGbB54B4wpbhocFF-Vs"),
    # but the page-insights index name lowercases them via build_data_index_name
    # (so the on-disk index is "ofgbb54b4wpbhocff-vs__fb_page_insights"). When we
    # parse team_esid back out of the index name, all we have is the lowercased
    # form — so key the lookup map by lower(_id) to match.
    teams_by_esid: dict[str, dict] = {}
    for hit in get_all_documents(_TEAMS_INDEX) or []:
        if isinstance(hit, dict) and hit.get("_id"):
            src = hit.get("_source", {})
            src["_id"] = hit["_id"]  # preserve original-case _id for downstream joins
            teams_by_esid[hit["_id"].lower()] = src

    pages_by_esid: dict[str, dict] = {}
    for hit in get_all_documents(_PAGES_INDEX) or []:
        if isinstance(hit, dict) and hit.get("_id"):
            pages_by_esid[hit["_id"]] = hit.get("_source", {})

    apps_by_fb_account_esid: dict[str, list[dict]] = defaultdict(list)
    apps_by_esid: dict[str, dict] = {}
    for hit in get_all_documents(_APPS_INDEX) or []:
        if not isinstance(hit, dict) or not hit.get("_id"):
            continue
        src = hit.get("_source", {})
        src["_id"] = hit["_id"]
        apps_by_esid[hit["_id"]] = src
        fb_account_esid = src.get("fb_account_esid")
        if fb_account_esid:
            apps_by_fb_account_esid[fb_account_esid].append(src)

    return {
        "teams_by_esid": teams_by_esid,
        "pages_by_esid": pages_by_esid,
        "apps_by_fb_account_esid": dict(apps_by_fb_account_esid),
        "apps_by_esid": apps_by_esid,
    }


def _resolve_apps_for_team(team_esid: str, lookups: dict) -> tuple[str, list[dict]]:
    """Walk team → pages → fb_accounts → fb_apps.

    Returns ``(team_title, [app_summary, ...])``. Apps are deduped by
    ``_id`` because two pages on the same team can share an account.
    """
    team = lookups["teams_by_esid"].get(team_esid) or {}
    team_title = team.get("team_title") or "(unknown team)"
    page_esids = team.get("team_facebook_page_esids") or []
    if isinstance(page_esids, str):
        page_esids = [page_esids]

    fb_account_esids: set[str] = set()
    for page_esid in page_esids:
        page = lookups["pages_by_esid"].get(page_esid) or {}
        fb_account_esid = page.get("fb_account_esid")
        if fb_account_esid:
            fb_account_esids.add(fb_account_esid)

    seen_app_ids: set[str] = set()
    apps_out: list[dict] = []
    for fb_account_esid in fb_account_esids:
        for app in lookups["apps_by_fb_account_esid"].get(fb_account_esid, []):
            if app["_id"] in seen_app_ids:
                continue
            seen_app_ids.add(app["_id"])
            apps_out.append(
                {
                    "fb_app_esid": app["_id"],
                    "fb_app_title": app.get("fb_app_title"),
                    "fb_app_id": app.get("fb_app_id"),
                    "fb_account_esid": fb_account_esid,
                }
            )
    return team_title, apps_out


def _build_inventory(min_docs: int) -> dict[str, Any]:
    """Run the full sweep. Returns the structured report dict."""
    es = get_elasticsearch_client()
    if es is None:
        raise SystemExit("could not connect to Elasticsearch")

    indices = _list_physical_indices(es, _PAGE_INSIGHTS_PATTERN)
    lookups = _load_asset_lookups()

    per_index: list[dict[str, Any]] = []
    apps_aggregate: dict[str, dict[str, Any]] = {}

    for idx in indices:
        doc_count, populated_count, latest = _index_stats(es, idx)
        if doc_count < min_docs:
            continue
        team_esid = _parse_team_esid(idx)
        team_title = "(legacy non-ESID index)"
        apps_for_team: list[dict] = []
        if team_esid:
            team_title, apps_for_team = _resolve_apps_for_team(team_esid, lookups)

        per_index.append(
            {
                "index": idx,
                "team_esid": team_esid,
                "team_title": team_title,
                "doc_count": doc_count,
                "populated_doc_count": populated_count,
                "latest_doc": latest,
                "apps_via_legacy_chain": apps_for_team,
            }
        )

        # Aggregate app-first.
        for app in apps_for_team:
            esid = app["fb_app_esid"]
            bucket = apps_aggregate.setdefault(
                esid,
                {
                    "fb_app_esid": esid,
                    "fb_app_title": app["fb_app_title"],
                    "fb_app_id": app["fb_app_id"],
                    "teams_with_data": [],
                    "total_docs": 0,
                    "total_populated_docs": 0,
                },
            )
            bucket["teams_with_data"].append(
                {
                    "team_esid": team_esid,
                    "team_title": team_title,
                    "index": idx,
                    "doc_count": doc_count,
                    "populated_doc_count": populated_count,
                    "latest_doc": latest,
                }
            )
            bucket["total_docs"] += doc_count
            bucket["total_populated_docs"] += populated_count

    return {
        "indices_with_data": per_index,
        "apps_with_data": sorted(
            apps_aggregate.values(),
            key=lambda b: (b["total_populated_docs"], b["total_docs"]),
            reverse=True,
        ),
        "min_docs_threshold": min_docs,
        "pattern": _PAGE_INSIGHTS_PATTERN,
    }


def _print_report(report: dict[str, Any]) -> None:
    """Render the inventory as two ASCII tables."""
    indices = report["indices_with_data"]
    apps = report["apps_with_data"]

    print(f"Pattern: {report['pattern']}   Min docs: {report['min_docs_threshold']}")
    print()

    # 'Real' = docs where BOTH page_actions_post_reactions_total > 0 AND
    # page_impressions_unique > 0 (set in _index_stats). An index can have
    # high Docs and zero Real if every doc was written full of nulls — that
    # usually means the import succeeded but Meta returned no data (deprecated
    # metric, expired token, permission missing).

    # --- Table 1: per-index ---
    print("=== Indices with data ===")
    if not indices:
        print("(none)")
    else:
        rows = [
            (
                r["index"],
                (r.get("team_title") or "")[:30],
                str(r["doc_count"]),
                str(r.get("populated_doc_count", 0)),
                (r.get("latest_doc") or "")[:10],
                ", ".join((a.get("fb_app_title") or a.get("fb_app_id") or "?") for a in r.get("apps_via_legacy_chain", []))
                or "(no legacy-chain apps — wizard or orphan)",
            )
            for r in indices
        ]
        headers = ("Index", "Team", "Docs", "Real", "Latest", "Apps (via legacy chain)")
        widths = [max(len(headers[i]), *(len(row[i]) for row in rows)) for i in range(len(headers))]
        fmt = "  ".join(f"{{:<{w}}}" for w in widths)
        print(fmt.format(*headers))
        print(fmt.format(*("-" * w for w in widths)))
        for row in rows:
            print(fmt.format(*row))

    print()
    print("=== Apps with data (aggregated) ===")
    if not apps:
        print("(no apps resolved — likely all wizard-imported)")
    else:
        rows = [
            (
                (a.get("fb_app_title") or "?")[:30],
                a.get("fb_app_id") or "",
                str(a["total_populated_docs"]),
                str(a["total_docs"]),
                str(len(a["teams_with_data"])),
                ", ".join((t.get("team_title") or "?")[:20] for t in a["teams_with_data"]),
            )
            for a in apps
        ]
        headers = ("App Title", "App ID", "Real Docs", "Total Docs", "Teams", "Team list")
        widths = [max(len(headers[i]), *(len(row[i]) for row in rows)) for i in range(len(headers))]
        fmt = "  ".join(f"{{:<{w}}}" for w in widths)
        print(fmt.format(*headers))
        print(fmt.format(*("-" * w for w in widths)))
        for row in rows:
            print(fmt.format(*row))

    print()
    print(
        "'Real' / 'Real Docs' = docs where page_actions_post_reactions_total > 0\n"
        "AND page_impressions_unique > 0 — i.e. the importer wrote a row AND Meta\n"
        "returned non-zero metrics. A high 'Docs' with zero 'Real' usually means the\n"
        "import ran but the metric came back null/zero (deprecated metric, expired\n"
        "token, missing permission). Sort order is by Real Docs descending.\n"
        "\n"
        "Hand the 'Apps with data' list to your team — those are the App IDs that\n"
        "have actually shipped page-insights documents and are worth checking\n"
        "for App Review / Live-Mode status in the Meta dashboard.\n"
        "\n"
        "Indices listed under 'no legacy-chain apps' use the wizard import path\n"
        "; their app linkage lives in de_connection, not de_asset_fb_apps."
    )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="fb_page_insights_app_inventory",
        description="Inventory which FB Apps have shipped real page-insights data.",
    )
    parser.add_argument("--json", action="store_true", help="Emit JSON instead of tables.")
    parser.add_argument(
        "--min-docs",
        type=int,
        default=1,
        help="Skip indices with fewer than N docs (default 1 — i.e. any data).",
    )
    args = parser.parse_args(argv)

    report = _build_inventory(min_docs=args.min_docs)
    if args.json:
        print(json.dumps(report, indent=2, default=str))
    else:
        _print_report(report)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
