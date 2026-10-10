"""One-shot: repoint the Ad Sets charts to the SINGLE canonical view.

The end state is one data view: a chart is built on ``*_fb_insights``
and the routed read path (``run_routed_query``) transparently fetches the dedup
fields (reach/frequency/recall) from the window index, while additive fields
(impressions/spend/clicks) sum from the daily index — the split happens per-field
at query time, driven by ``DEDUP_WINDOW_REGISTRY``. The chart author no longer
picks the window view at all.

Staging inspection (2026-06-21, template ``meta_ad_sets`` + its instances) showed
the window view is pinned in exactly TWO places, both chart/dashboard-level (NOT
per-column — every ``col.data_view`` is ``None`` and inherits the chart default):

  * ``chart.data_view = "*_fb_insights_window"`` — the chart default;
  * dashboard-level ``filters[].data_view = "*_fb_insights_window"`` (the
    publisher_platform / ad_account_id / ad_campaign_name filter fields, all of
    which also exist on the daily ``*_fb_insights`` index).

So the migration is a straight rewrite of those window data_views to
``*_fb_insights``. After it, the chart sits on the canonical view; the Task-1
gate (``chart_dedup_matches``, flag-gated) recognizes the dedup fields there and
routing fetches them from the window index. Per-column data_views (currently
None) are left alone — they correctly inherit the new canonical chart default.

A defensive note on the publisher_platform filter: the window index carries a
synthetic ``publisher_platform="all"`` combined-dedup doc that hid from
the window-view dropdown. On the daily view that sentinel never exists, so moving
the filter to ``*_fb_insights`` is safe — the value dropdown lists real platforms,
and "all platforms" is expressed by selecting nothing (routing max-reads the
combined doc for reach regardless).

**Dry-run by default.** Use ``--apply`` to write. Idempotent: a second pass after
a successful apply is a no-op (nothing left on the window view).

Usage::

    docker exec -w /app/python imports_container python3 \\
        -m tools.repoint_meta_ad_sets_to_single_view
    docker exec -w /app/python imports_container python3 \\
        -m tools.repoint_meta_ad_sets_to_single_view --apply \\
        --title 'Ad Sets (Delivery)'
"""

from __future__ import annotations

import argparse
import copy
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
WINDOW_DATA_VIEW = "*_fb_insights_window"
CANONICAL_DATA_VIEW = "*_fb_insights"


def repoint_to_single_view(echarts_config: dict[str, Any]) -> tuple[dict[str, Any], int]:
    """Rewrite every ``*_fb_insights_window`` data_view to ``*_fb_insights``.

    Walks the three locations a window data_view appears (verified on staging):
      * dashboard-level ``filters[].data_view``;
      * each ``chart.data_view``;
      * each ``chart.filters[].data_view`` (defensive — none seen on staging, but
        a chart may carry its own filters).

    Per-column / per-data_source data_views that are explicitly the window view
    are ALSO rewritten (defensive: today they're None and inherit the chart
    default, but a hand-edited chart could pin one). Returns (new_config, count).
    """
    out = copy.deepcopy(echarts_config)
    swaps = 0

    def _swap_dv(obj: dict) -> int:
        if isinstance(obj, dict) and obj.get("data_view") == WINDOW_DATA_VIEW:
            obj["data_view"] = CANONICAL_DATA_VIEW
            return 1
        return 0

    for f in out.get("filters") or []:
        swaps += _swap_dv(f)

    for chart in out.get("charts") or []:
        if not isinstance(chart, dict):
            continue
        swaps += _swap_dv(chart)
        for f in chart.get("filters") or []:
            swaps += _swap_dv(f)
        for col in chart.get("table_columns") or chart.get("columns") or []:
            if not isinstance(col, dict):
                continue
            swaps += _swap_dv(col)
            for ds in col.get("data_sources") or []:
                swaps += _swap_dv(ds)

    return out, swaps


def _repoint_template(es, apply: bool) -> int:
    try:
        doc = es.get(index=TEMPLATES_INDEX, id=TEMPLATE_ID)
    except Exception as e:  # noqa: BLE001 — top-level boundary
        logger.warning("Template %r not found in %s: %s — skipping.", TEMPLATE_ID, TEMPLATES_INDEX, e)
        return 0
    new_config, swaps = repoint_to_single_view(doc["_source"].get("echarts_config") or {})
    if swaps == 0:
        logger.info("Template %r: nothing on the window view (already migrated?).", TEMPLATE_ID)
        return 0
    logger.info("Template %r: %d window data_view(s) -> canonical.", TEMPLATE_ID, swaps)
    if apply:
        es.update(index=TEMPLATES_INDEX, id=TEMPLATE_ID, body={"doc": {"echarts_config": new_config}}, refresh="wait_for")
        logger.info("  applied.")
    return swaps


def _repoint_dashboards(es, apply: bool, query: dict, label: str) -> int:
    try:
        resp = es.search(index=DASHBOARDS_INDEX, body={"size": 1000, "query": query}, ignore_unavailable=True)
    except Exception as e:  # noqa: BLE001 — top-level boundary
        logger.warning("Could not query %s (%s): %s", DASHBOARDS_INDEX, label, e)
        return 0
    hits = (resp.body if hasattr(resp, "body") else resp).get("hits", {}).get("hits", [])
    total_swaps = 0
    n = 0
    for hit in hits:
        src = hit["_source"]
        new_config, swaps = repoint_to_single_view(src.get("echarts_config") or {})
        if swaps == 0:
            continue
        n += 1
        total_swaps += swaps
        logger.info(
            "  instance %s (team=%s, title=%r): %d window data_view(s) -> canonical",
            hit["_id"],
            src.get("team_esid") or src.get("template_team_esid"),
            src.get("title"),
            swaps,
        )
        if apply:
            es.update(index=DASHBOARDS_INDEX, id=hit["_id"], body={"doc": {"echarts_config": new_config}}, refresh="wait_for")
    logger.info("%s: %d dashboard(s), %d swap(s)%s.", label, n, total_swaps, " — applied" if apply and total_swaps else "")
    return total_swaps


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--apply", action="store_true", help="Write the swap to ES. Default is dry-run.")
    parser.add_argument("--title", default=None, help="Also repoint a standalone dashboard by exact title.")
    parser.add_argument("--dashboard-id", default=None, help="Also repoint a standalone dashboard by dashboard_id.")
    args = parser.parse_args()

    logging.basicConfig(level=logging.INFO, format="%(message)s")
    logger.info("Repointing %r -> %r (chart + filter data_views).", WINDOW_DATA_VIEW, CANONICAL_DATA_VIEW)

    es = get_elasticsearch_client()
    if es is None:
        logger.error("ES unavailable")
        return 2

    if not args.apply:
        logger.info("DRY-RUN — pass --apply to write.")

    total = _repoint_template(es, args.apply)
    total += _repoint_dashboards(es, args.apply, {"term": {"template_id": TEMPLATE_ID}}, "Template instances")
    if args.dashboard_id:
        total += _repoint_dashboards(
            es, args.apply, {"term": {"dashboard_id": args.dashboard_id}}, f"Dashboard id={args.dashboard_id}"
        )
    if args.title:
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
        logger.info("Nothing to do — no window data_views found in the targeted docs.")
    elif not args.apply:
        logger.info("DRY-RUN complete: %d swap(s) would be applied. Re-run with --apply.", total)
    else:
        logger.info("DONE: %d swap(s) applied.", total)
    return 0


if __name__ == "__main__":
    sys.exit(main())
