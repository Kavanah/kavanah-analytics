"""One-shot heal: add FB daily watch time as the second View Time source
.

 decided the Summary "View Time" card is a combined cross-platform
total in MINUTES and shipped it YouTube-only because FB had no summable daily
watch-time field. ships that field —
``page_video_view_time_daily_min`` (lifetime-delta method,
get_page_insights.py) — and this heal adds it to the LIVE saved docs, which a
code-side seeder change alone cannot reach (saved dashboards are ES state):

* **grouped_table columns** — a column whose label starts with "View Time"
  and whose ``data_sources`` reads YouTube ``estimated_minutes_watched`` gains
  ``{*_fb_page_insights, page_video_view_time_daily_min}`` as a second source
  (the grouped_table renderer sums per-cell across sources; both are minutes).
* **canvas ``data_field`` tiles** — canvas bindings are single-source, so a
  "View Time" tile bound to YouTube minutes is converted to the builder's
  native multi-source shape: a ``calc_field`` with ``formula: "yt + fb"``
  over two variables (same machinery shipped; the renderer treats
  data_field and calc_field tiles identically).

Idempotent: a column already carrying the FB source, and a tile already
converted, are skipped. Deliberately untouched: the single-source canva
metric registry ("vid-time" metric_overrides — no multi-source support
there), and any View Time surface an author pointed elsewhere.

Safety (same contract as heal_summary_view_time):
dry-run by default; ``--apply`` writes with a per-doc JSON snapshot to
``--snapshot-dir`` first; ``--dashboard-id`` restricts scope.

Usage::

    docker exec -w /app/python imports_container python3 \\
        -m tools.heal_summary_view_time_2 [--apply]
"""

from __future__ import annotations

import argparse
import json
import logging
import sys
from pathlib import Path

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

DASHBOARDS_INDEX = "de_asset_echarts_dashboards"
TEMPLATES_INDEX = "de_dashboard_templates"
DEFAULT_SNAPSHOT_DIR = "/app/data/snapshots_5"

YT_DV = "*_youtube_analytics"
YT_FIELD = "estimated_minutes_watched"
FB_DV = "*_fb_page_insights"
FB_FIELD = "page_video_view_time_daily_min"


FB_AVG_FIELD = "page_video_avg_sec_watched"


def heal_grouped_table_columns(chart: dict) -> int:
    """Heal in-scope View Time grouped_table columns.

    Two shapes:
    * a column already on YouTube minutes gains the FB
      daily field as a second source;
    * a column still carrying the its only source is the FB
      lifetime-average seconds field (prod's old "Summary" doc; the
      heal covered only the canva shapes, never grouped_table columns) — is
      rebound to the decided pair (YT minutes + FB daily, summed) and
      relabeled "View Time (Minutes)". Summing a lifetime average is
      statistically meaningless, which is the whole point.
    """
    if not isinstance(chart, dict) or chart.get("chart_type") != "grouped_table":
        return 0
    changes = 0
    for group in chart.get("groups") or []:
        for col in group.get("columns") or []:
            if not str(col.get("label") or "").startswith("View Time"):
                continue
            sources = col.get("data_sources") or []
            has_yt = any(s.get("data_view") == YT_DV and s.get("field") == YT_FIELD for s in sources)
            has_fb = any(s.get("field") == FB_FIELD for s in sources)
            only_drifted = bool(sources) and all(s.get("field") == FB_AVG_FIELD for s in sources)
            if only_drifted:
                col["data_sources"] = [
                    {"data_view": YT_DV, "field": YT_FIELD},
                    {"data_view": FB_DV, "field": FB_FIELD},
                ]
                col["aggregation"] = "sum"
                col["label"] = "View Time (Minutes)"
                # The legacy single-binding twins, if present, must not shadow
                # the healed sources on old renderers.
                col.pop("data_view", None)
                col.pop("field", None)
                changes += 1
                continue
            if not has_yt or has_fb:
                continue
            sources.append({"data_view": FB_DV, "field": FB_FIELD})
            col["data_sources"] = sources
            changes += 1
    return changes


def heal_canvas_objects(objects: list) -> int:
    """Convert in-scope View Time data_field tiles to yt+fb calc_fields."""
    changes = 0
    for obj in objects or []:
        if not isinstance(obj, dict) or obj.get("object_type") != "data_field":
            continue
        if not str(obj.get("label") or "").startswith("View Time"):
            continue
        binding = obj.get("binding") or {}
        if binding.get("computed") or binding.get("data_view") != YT_DV or binding.get("field") != YT_FIELD:
            continue
        obj["object_type"] = "calc_field"
        obj["calc"] = {
            "formula": "yt + fb",
            "variables": {
                "yt": dict(binding),
                "fb": {"data_view": FB_DV, "field": FB_FIELD, "aggregation": "sum"},
            },
        }
        obj.pop("binding", None)
        changes += 1
    return changes


def heal_doc_source(src: dict) -> int:
    """Heal one dashboard/template ``_source`` in place; return change count."""
    changes = 0
    for config_key in ("echarts_config", "canvas_config"):
        cfg = src.get(config_key) or {}
        for chart in cfg.get("charts") or []:
            changes += heal_grouped_table_columns(chart)
        changes += heal_canvas_objects(cfg.get("objects") or [])
    return changes


def heal(es, *, apply: bool, snapshot_dir: str, dashboard_ids: list[str] | None) -> dict:
    snap_dir = Path(snapshot_dir)
    if apply:
        snap_dir.mkdir(parents=True, exist_ok=True)

    healed_docs = 0
    total_changes = 0

    def _heal_hit(index: str, did: str, src: dict) -> None:
        nonlocal healed_docs, total_changes
        changes = heal_doc_source(src)
        if not changes:
            return
        if apply:
            (snap_dir / f"{index}__{did}.json").write_text(json.dumps(src), encoding="utf-8")
            body = {k: src[k] for k in ("echarts_config", "canvas_config") if k in src}
            es.update(index=index, id=did, doc=body, refresh="wait_for")
        logger.info(
            "%s %s/%s (%s): changes=%d",
            "APPLY" if apply else "DRY-RUN",
            index,
            did,
            src.get("title", "?"),
            changes,
        )
        healed_docs += 1
        total_changes += changes

    if dashboard_ids:
        for did in dashboard_ids:
            try:
                d = es.get(index=DASHBOARDS_INDEX, id=did)
                _heal_hit(DASHBOARDS_INDEX, did, d["_source"])
            except Exception as e:  # noqa: BLE001 — boundary
                logger.warning("dashboard %s not found: %s", did, e)
    else:
        # echarts_config/canvas_config are not indexed — fetch and filter
        # client-side (same constraint as the heal).
        for index in (DASHBOARDS_INDEX, TEMPLATES_INDEX):
            try:
                resp = es.search(index=index, body={"size": 2000, "query": {"match_all": {}}})
            except Exception as e:  # noqa: BLE001 — boundary; a missing index just isn't healed
                logger.warning("index %s not searchable: %s", index, e)
                continue
            for h in resp.get("hits", {}).get("hits", []):
                _heal_hit(index, h["_id"], h.get("_source") or {})

    return {"healed_docs": healed_docs, "total_changes": total_changes, "applied": apply}


def main() -> int:
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    ap = argparse.ArgumentParser(description="Add FB daily watch time as second Summary View Time source.")
    ap.add_argument("--apply", action="store_true", help="write changes (default: dry-run)")
    ap.add_argument("--snapshot-dir", default=DEFAULT_SNAPSHOT_DIR, help="where to write reversible snapshots")
    ap.add_argument("--dashboard-id", action="append", dest="dashboard_ids", help="restrict to dashboard id(s)")
    args = ap.parse_args()

    es = get_elasticsearch_client()
    if es is None:
        logger.error("Elasticsearch unavailable")
        return 1

    result = heal(es, apply=args.apply, snapshot_dir=args.snapshot_dir, dashboard_ids=args.dashboard_ids)
    logger.info("RESULT: %s", json.dumps(result, indent=2))
    if not args.apply:
        logger.info("DRY-RUN only. Re-run with --apply to write.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
