"""One-shot: repoint the meta_ad_sets template to the windowed reach index.

 produces FB-deduplicated, ad-set-grain reach in ``*_fb_insights_window``
(verified == Data Studio). The Ad Sets table (``meta_ad_sets``) currently reads
``*_fb_insights`` (the per-day index), whose reach OVER-COUNTS (~54% high) and is
often INCOMPLETE (missing days). had pointed it at the per-day index so
fresh wizard users saw data; moves it to the window index where reach,
frequency AND the additive metrics are all correct and complete for a pulled
window (decision: all columns → one DV so a single platform filter scopes the
whole table consistently — see DECISIONS.md 2026-06-03).

This swaps every ``*_fb_insights`` data_view reference inside the template's
``echarts_config`` to ``*_fb_insights_window`` — at ALL the places one can live:
  * dashboard-level ``filters[].data_view``
  * per-chart ``charts[].data_view``
  * per-chart ``charts[].filters[].data_view``
  * per-chart table columns ``charts[].table_columns[].data_view``  (the Ad Sets
    table keys each metric column to its own data_view — tool missed
    these because the meta_ad_sets columns at that time shared the chart default)

It deliberately swaps ONLY the bare ``*_fb_insights`` value, never
``*_fb_insights_weekly`` (a different retired index) — exact-match so the
``_window`` / ``_weekly`` siblings are untouched.

**Dry-run by default.** Use ``--apply`` to write. Idempotent: re-running after a
successful pass is a no-op (no bare ``*_fb_insights`` references remain).

Usage::

    docker exec -w /app/python imports_container python3 \\
        -m tools.repoint_meta_ad_sets_to_window_2
    docker exec -w /app/python imports_container python3 \\
        -m tools.repoint_meta_ad_sets_to_window_2 --apply
"""

from __future__ import annotations

import argparse
import copy
import json
import logging
import sys
from pathlib import Path
from typing import Any

_HERE = Path(__file__).resolve().parent
for _candidate in (_HERE.parents[1], _HERE.parents[2] / "bin" / "python", Path("/app/python")):
    if (_candidate / "asset_management").exists():
        sys.path.insert(0, str(_candidate))
        break

from asset_management.web.services.template_service import (  # noqa: E402
    DASHBOARDS_INDEX,
    TEMPLATES_INDEX,
)
from lib.es_helpers import get_elasticsearch_client  # noqa: E402

logger = logging.getLogger(__name__)

TEMPLATE_ID = "meta_ad_sets"
OLD_DATA_VIEW = "*_fb_insights"
NEW_DATA_VIEW = "*_fb_insights_window"


def _swap(obj: dict, key: str = "data_view") -> int:
    """Swap a single dict's data_view if it equals OLD_DATA_VIEW. Returns 0/1."""
    if isinstance(obj, dict) and obj.get(key) == OLD_DATA_VIEW:
        obj[key] = NEW_DATA_VIEW
        return 1
    return 0


def swap_data_views(echarts_config: dict[str, Any]) -> tuple[dict[str, Any], int]:
    """Walk every data_view-bearing location and swap exact ``*_fb_insights`` refs.

    Returns the mutated config and a count of swaps. Exact-match only — never
    touches ``*_fb_insights_weekly`` or an already-swapped ``*_fb_insights_window``.
    """
    out = copy.deepcopy(echarts_config)
    swaps = 0

    for f in out.get("filters") or []:
        swaps += _swap(f)

    for chart in out.get("charts") or []:
        swaps += _swap(chart)
        for f in chart.get("filters") or []:
            swaps += _swap(f)
        for col in chart.get("table_columns") or []:
            swaps += _swap(col)
            # grouped_table / multi-source columns can nest data_sources.
            for ds in col.get("data_sources") or []:
                swaps += _swap(ds)

    return out, swaps


def _repoint_template(es, apply: bool) -> int:
    """Swap the meta_ad_sets TEMPLATE in de_dashboard_templates. Returns swap count."""
    try:
        doc = es.get(index=TEMPLATES_INDEX, id=TEMPLATE_ID)
    except Exception as e:  # noqa: BLE001 — top-level boundary
        logger.warning("Template %r not found in %s: %s — skipping template.", TEMPLATE_ID, TEMPLATES_INDEX, e)
        return 0

    new_config, swaps = swap_data_views(doc["_source"].get("echarts_config") or {})
    if swaps == 0:
        logger.info("Template %r: no %r refs (already on %r?).", TEMPLATE_ID, OLD_DATA_VIEW, NEW_DATA_VIEW)
        return 0
    logger.info("Template %r: %d swap(s).", TEMPLATE_ID, swaps)
    if apply:
        es.update(index=TEMPLATES_INDEX, id=TEMPLATE_ID, body={"doc": {"echarts_config": new_config}}, refresh="wait_for")
        logger.info("  applied.")
    return swaps


def _repoint_dashboards(es, apply: bool, query: dict, label: str) -> int:
    """Swap data_views in every de_asset_echarts_dashboards doc matching ``query``.

    Covers two cases:
      * template_id == meta_ad_sets — dashboards instantiated FROM the template
        (instantiate() deep-copies echarts_config, so a template-only swap misses
        them).
      * an explicit dashboard id/title — the SOURCE dashboard a template was
        built FROM via "Save as template" carries NO template_id, so it isn't
        found by the template_id query even though it has the same Ad Sets table
        on *_fb_insights. Target it directly with --dashboard-id / --title.
    """
    try:
        resp = es.search(index=DASHBOARDS_INDEX, body={"size": 1000, "query": query}, ignore_unavailable=True)
    except Exception as e:  # noqa: BLE001 — top-level boundary
        logger.warning("Could not query %s (%s): %s", DASHBOARDS_INDEX, label, e)
        return 0

    hits = (resp.body if hasattr(resp, "body") else resp).get("hits", {}).get("hits", [])
    total_swaps = 0
    n_dashboards = 0
    for hit in hits:
        src = hit["_source"]
        new_config, swaps = swap_data_views(src.get("echarts_config") or {})
        if swaps == 0:
            continue
        n_dashboards += 1
        total_swaps += swaps
        logger.info(
            "  instance %s (team=%s, title=%r): %d swap(s)",
            hit["_id"],
            src.get("team_esid") or src.get("template_team_esid"),
            src.get("title"),
            swaps,
        )
        if apply:
            es.update(index=DASHBOARDS_INDEX, id=hit["_id"], body={"doc": {"echarts_config": new_config}}, refresh="wait_for")
    logger.info(
        "%s: %d dashboard(s) with %d total swap(s)%s.",
        label,
        n_dashboards,
        total_swaps,
        " — applied" if apply and total_swaps else "",
    )
    return total_swaps


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--apply", action="store_true", help="Write the swap to ES. Default is dry-run.")
    parser.add_argument(
        "--title",
        default=None,
        help="Also repoint a standalone dashboard by exact title (e.g. 'Ad Sets (Delivery)'). "
        "Use for a hand-built dashboard that a template was created FROM — it carries no template_id.",
    )
    parser.add_argument(
        "--dashboard-id",
        default=None,
        help="Also repoint a standalone dashboard by its dashboard_id.",
    )
    args = parser.parse_args()

    logging.basicConfig(level=logging.INFO, format="%(message)s")

    es = get_elasticsearch_client()
    if es is None:
        logger.error("ES unavailable")
        return 2

    if not args.apply:
        logger.info("DRY-RUN — pass --apply to write. Swapping %r -> %r.", OLD_DATA_VIEW, NEW_DATA_VIEW)

    # 1. The template (future instantiations).
    total = _repoint_template(es, args.apply)
    # 2. Every dashboard instantiated FROM the template (matched by template_id).
    total += _repoint_dashboards(es, args.apply, {"term": {"template_id": TEMPLATE_ID}}, "Template instances")
    # 3. An explicit standalone dashboard (the hand-built source a template was
    #    saved from — no template_id, so steps 1-2 miss it).
    if args.dashboard_id:
        total += _repoint_dashboards(
            es, args.apply, {"term": {"dashboard_id": args.dashboard_id}}, f"Dashboard id={args.dashboard_id}"
        )
    if args.title:
        # title is a text field in most mappings; match both the analyzed field
        # and a .keyword subfield so an exact title resolves regardless of mapping.
        title_query = {
            "bool": {
                "should": [
                    {"term": {"title.keyword": args.title}},
                    {"match_phrase": {"title": args.title}},
                ],
                "minimum_should_match": 1,
            }
        }
        total += _repoint_dashboards(es, args.apply, title_query, f"Dashboard title={args.title!r}")

    if total == 0:
        logger.info("Nothing to do — no %r references found in the targeted docs.", OLD_DATA_VIEW)
    elif not args.apply:
        logger.info("DRY-RUN complete: %d swap(s) would be applied. Re-run with --apply.", total)
    else:
        logger.info("DONE: %d swap(s) applied.", total)
    return 0


if __name__ == "__main__":
    sys.exit(main())
