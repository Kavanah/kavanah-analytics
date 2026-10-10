"""One-shot: repoint the ADDITIVE Ad Sets columns back to the daily index.

 moved EVERY meta_ad_sets column to ``*_fb_insights_window`` on the premise
that the window index held correct, complete values for all metrics. Staging
verification (2026-06-20, team 60_s4jsbim2adjkusvbg, ad set
``2026_06_AFM_BaH_Happy_M&W_FB&IG``) disproved that for the ADDITIVE metrics:

  * The window index accumulates ONE doc per (ad set × publisher_platform × every
    pulled [start,end] window). A single ad set had 23 docs spanning 5 overlapping
    windows. Summing an additive field (impressions/spend/clicks) over them gives
    1,258,660 impressions — ~3.5x Data Studio's 367,049.
  * The DAILY index (``*_fb_insights``), once the daily importer has filled the
    range, sums to 366,299 impressions / $289.65 — == Data Studio.
  * Reach/frequency are NON-additive and remain CORRECT on the window (the
    matched-window ``publisher_platform="all"`` doc == Data Studio's 79,073).

So the fix is SELECTIVE: additive columns go back to ``*_fb_insights`` (daily,
summed over the viewing range); the 5 registry dedup fields STAY on
``*_fb_insights_window``. This is the inverse of
``repoint_meta_ad_sets_to_window_2`` but field-aware — it only swaps a
data_view back when the field at that location is NOT a dedup field.

A column/data_source pairs a ``field`` with a ``data_view``. We swap
``*_fb_insights_window`` -> ``*_fb_insights`` ONLY when ``field`` is additive
(not one of the registry's FB dedup fields). Label/group-by columns (adset_name)
carry no metric and are treated as additive (daily is fine for the label).

Chart-level / filter-level data_views (no per-field metric) are LEFT on the
window — the dedup columns still need the window as their default; the additive
columns now carry their own explicit daily data_view, which overrides the chart
default for those columns.

**Dry-run by default.** Use ``--apply`` to write. Idempotent: re-running after a
successful pass is a no-op (additive columns already on daily).

Usage::

    docker exec -w /app/python imports_container python3 \\
        -m tools.repoint_meta_ad_sets_additive_to_daily
    docker exec -w /app/python imports_container python3 \\
        -m tools.repoint_meta_ad_sets_additive_to_daily --apply \\
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

try:
    from asset_management.web.services.es_query_builder import DEDUP_WINDOW_REGISTRY  # noqa: E402
except ImportError:  # pragma: no cover - import-path parity in-container
    sys.path.insert(0, str(_HERE.parents[1] / "asset_management" / "web"))
    from services.es_query_builder import DEDUP_WINDOW_REGISTRY

logger = logging.getLogger(__name__)

TEMPLATE_ID = "meta_ad_sets"
WINDOW_DATA_VIEW = "*_fb_insights_window"
DAILY_DATA_VIEW = "*_fb_insights"

# The FB fields that must STAY on the window index (read deduplicated). Anything
# else at a window data_view is additive and belongs on the daily index. Sourced
# from the central registry so it can't drift from the read-path's own notion.
DEDUP_FIELDS_FB = frozenset(field for (source, field) in DEDUP_WINDOW_REGISTRY if source == "fb_insights")


def _swap_additive(obj: dict) -> int:
    """If ``obj`` is on the window DV and its ``field`` is additive, move it to
    the daily DV. Returns 0/1. A dedup field (or no field) is left untouched."""
    if not isinstance(obj, dict):
        return 0
    if obj.get("data_view") != WINDOW_DATA_VIEW:
        return 0
    field = obj.get("field") or obj.get("y_field")
    if field in DEDUP_FIELDS_FB:
        return 0  # dedup field — stays on the window
    obj["data_view"] = DAILY_DATA_VIEW
    return 1


def swap_additive_to_daily(echarts_config: dict[str, Any]) -> tuple[dict[str, Any], int]:
    """Walk every field-bearing location; move additive window columns to daily.

    Mirrors the tool's walk, but only at locations that carry a metric
    ``field`` (columns + nested data_sources). Chart-level / dashboard-filter
    data_views (no per-field metric) are left on the window so the dedup columns
    keep it as their default; additive columns now carry their own daily DV.
    """
    out = copy.deepcopy(echarts_config)
    swaps = 0

    for chart in out.get("charts") or []:
        for col in chart.get("table_columns") or chart.get("columns") or []:
            if not isinstance(col, dict):
                continue
            # A column may carry the (field, data_view) directly...
            swaps += _swap_additive(col)
            # ...or nest it under data_sources[] (the Ad Sets table shape).
            for ds in col.get("data_sources") or []:
                swaps += _swap_additive(ds)

    return out, swaps


def dump_data_views(echarts_config: dict[str, Any]) -> None:
    """Print every data_view-bearing location in a config, so we can see EXACTLY
    where the Ad Sets table pins its data views (the repoint found 0 swaps, so the
    additive columns aren't where this tool looked — find out where they are)."""
    print("  dashboard-level filters[]:")
    for f in echarts_config.get("filters") or []:
        if isinstance(f, dict) and "data_view" in f:
            print(f"    filter data_view={f.get('data_view')!r}")
    for ci, chart in enumerate(echarts_config.get("charts") or []):
        if not isinstance(chart, dict):
            continue
        print(f"  chart[{ci}] type={chart.get('chart_type')!r} chart.data_view={chart.get('data_view')!r}")
        for f in chart.get("filters") or []:
            if isinstance(f, dict) and "data_view" in f:
                print(f"      chart.filter data_view={f.get('data_view')!r}")
        for col in chart.get("table_columns") or chart.get("columns") or []:
            if not isinstance(col, dict):
                continue
            print(
                f"      col header={col.get('header')!r} field={col.get('field')!r} "
                f"agg={col.get('aggregation')!r} col.data_view={col.get('data_view')!r}"
            )
            for ds in col.get("data_sources") or []:
                if isinstance(ds, dict):
                    print(
                        f"        data_source field={ds.get('field') or ds.get('y_field')!r} "
                        f"data_view={ds.get('data_view')!r}"
                    )


def _repoint_template(es, apply: bool) -> int:
    try:
        doc = es.get(index=TEMPLATES_INDEX, id=TEMPLATE_ID)
    except Exception as e:  # noqa: BLE001 — top-level boundary
        logger.warning("Template %r not found in %s: %s — skipping.", TEMPLATE_ID, TEMPLATES_INDEX, e)
        return 0
    new_config, swaps = swap_additive_to_daily(doc["_source"].get("echarts_config") or {})
    if swaps == 0:
        logger.info("Template %r: no additive window columns (already on daily?).", TEMPLATE_ID)
        return 0
    logger.info("Template %r: %d additive column(s) -> daily.", TEMPLATE_ID, swaps)
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
        new_config, swaps = swap_additive_to_daily(src.get("echarts_config") or {})
        if swaps == 0:
            continue
        n += 1
        total_swaps += swaps
        logger.info(
            "  instance %s (team=%s, title=%r): %d additive col(s) -> daily",
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
    parser.add_argument(
        "--dump-config",
        action="store_true",
        help="Print every data_view location for the targeted template/dashboard, then exit (no writes).",
    )
    args = parser.parse_args()

    logging.basicConfig(level=logging.INFO, format="%(message)s")
    logger.info("Dedup fields that STAY on the window: %s", sorted(DEDUP_FIELDS_FB))

    es = get_elasticsearch_client()
    if es is None:
        logger.error("ES unavailable")
        return 2

    if args.dump_config:
        try:
            tdoc = es.get(index=TEMPLATES_INDEX, id=TEMPLATE_ID)
            print(f"\n=== TEMPLATE {TEMPLATE_ID} data_views ===")
            dump_data_views(tdoc["_source"].get("echarts_config") or {})
        except Exception as e:  # noqa: BLE001 — diagnostic boundary
            print(f"template dump error: {e!r}")
        queries = [({"term": {"template_id": TEMPLATE_ID}}, "template instances")]
        if args.dashboard_id:
            queries.append(({"term": {"dashboard_id": args.dashboard_id}}, f"id={args.dashboard_id}"))
        if args.title:
            queries.append(
                (
                    {
                        "bool": {
                            "should": [
                                {"term": {"title.keyword": args.title}},
                                {"match_phrase": {"title": args.title}},
                            ],
                            "minimum_should_match": 1,
                        }
                    },
                    f"title={args.title!r}",
                )
            )
        for q, label in queries:
            resp = es.search(index=DASHBOARDS_INDEX, body={"size": 20, "query": q}, ignore_unavailable=True)
            hits = (resp.body if hasattr(resp, "body") else resp).get("hits", {}).get("hits", [])
            for h in hits:
                print(f"\n=== DASHBOARD {h['_id']} ({label}) title={h['_source'].get('title')!r} ===")
                dump_data_views(h["_source"].get("echarts_config") or {})
            if not hits:
                print(f"\n(no dashboards matched {label})")
        return 0

    if not args.apply:
        logger.info("DRY-RUN — pass --apply to write. Additive %r -> %r.", WINDOW_DATA_VIEW, DAILY_DATA_VIEW)

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
        logger.info("Nothing to do — no additive window columns found in the targeted docs.")
    elif not args.apply:
        logger.info("DRY-RUN complete: %d swap(s) would be applied. Re-run with --apply.", total)
    else:
        logger.info("DONE: %d swap(s) applied.", total)
    return 0


if __name__ == "__main__":
    sys.exit(main())
