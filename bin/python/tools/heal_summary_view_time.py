"""One-shot heal: repoint drifted Summary "View Time" bindings to YouTube
minutes.

Decision (a teammate, 2026-08-20, the private docs): the Summary dashboard's
View Time card is a combined cross-platform total in MINUTES. YouTube
(``estimated_minutes_watched``) is its only source until FB daily watch time
exists in our data. FB's ``page_video_avg_sec_watched`` is a
weighted lifetime AVERAGE over recent videos (see get_page_insights.py) — a
tile that SUMS it across days displays a statistically meaningless number,
and its unit (seconds) contradicts the minutes card.

Two saved-doc shapes carry the drifted binding (staging, found 2026-08-20):

* **canva chart ``metric_overrides``** — the echarts "Summary Report" doc's
  ``metric_overrides["vid-time"]`` pins ``*fb_page_insights`` /
  ``page_video_avg_sec_watched``, overriding the (correct, earlier)
  registry default. The heal repoints exactly that override to
  ``*_youtube_analytics`` / ``estimated_minutes_watched``.
* **canvas ``data_field`` objects** — the "Summary Report — Canvas" doc's
  View Time tile binds the same FB pair and is labeled "View Time (Seconds)".
  The heal rebinds it and relabels to "View Time (Minutes)".

Deliberately NOT touched: any other use of ``page_video_avg_sec_watched``
(the Maps and Trends dashboards chart it as an average, which is legitimate);
only the ``vid-time`` override key and canvas tiles whose label starts with
"View Time" are in scope.

Safety (same contract as heal_echo_first_look_date_field):

* **Dry-run by default.** ``--apply`` writes. A per-doc JSON snapshot of the
  original is written to ``--snapshot-dir`` before any write.
* **Idempotent.** A second ``--apply`` finds no drifted bindings -> healed=0.
* **Scope.** All docs in ``de_asset_echarts_dashboards`` and
  ``de_dashboard_templates`` are swept; pass ``--dashboard-id ID``
  (repeatable) to restrict to specific dashboard ids.

Usage::

    docker exec -w /app/python imports_container python3 \\
        -m tools.heal_summary_view_time

    docker exec -w /app/python imports_container python3 \\
        -m tools.heal_summary_view_time --apply
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
DEFAULT_SNAPSHOT_DIR = "/app/data/snapshots_4"

# The drifted binding: FB's lifetime-average watch time (never summable).
WRONG_FIELD = "page_video_avg_sec_watched"
# The decided binding: YouTube daily total minutes.
CORRECT_DV = "*_youtube_analytics"
CORRECT_FIELD = "estimated_minutes_watched"
CORRECT_LABEL = "View Time (Minutes)"

VID_TIME_KEY = "vid-time"


def heal_metric_overrides(overrides: dict) -> int:
    """Repoint a drifted ``vid-time`` metric_override in place.

    Only the ``vid-time`` key is in scope, and only when it carries the FB
    average-seconds field — a vid-time override an author deliberately pointed
    somewhere else is left alone. Returns the number of repoints (0 or 1).
    """
    ov = (overrides or {}).get(VID_TIME_KEY)
    if not isinstance(ov, dict) or ov.get("field") != WRONG_FIELD:
        return 0
    ov["dv"] = CORRECT_DV
    ov["field"] = CORRECT_FIELD
    ov["agg"] = "sum"
    return 1


def heal_canvas_objects(objects: list) -> int:
    """Rebind + relabel drifted View Time canvas tiles in place.

    In scope: ``data_field`` objects whose label starts with "View Time" AND
    whose binding reads the FB average-seconds field. Other objects — and
    other legitimate uses of the field — are untouched. Returns rebind count.
    """
    healed = 0
    for obj in objects or []:
        if not isinstance(obj, dict) or obj.get("object_type") != "data_field":
            continue
        if not str(obj.get("label") or "").startswith("View Time"):
            continue
        binding = obj.get("binding") or {}
        if binding.get("field") != WRONG_FIELD:
            continue
        binding["data_view"] = CORRECT_DV
        binding["field"] = CORRECT_FIELD
        binding["aggregation"] = "sum"
        obj["binding"] = binding
        obj["label"] = CORRECT_LABEL
        healed += 1
    return healed


def heal_doc_source(src: dict) -> int:
    """Heal one dashboard/template ``_source`` in place; return change count."""
    changes = 0
    for config_key in ("echarts_config", "canvas_config"):
        cfg = src.get(config_key) or {}
        for chart in cfg.get("charts") or []:
            if isinstance(chart, dict):
                changes += heal_metric_overrides(chart.get("metric_overrides") or {})
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
    ap = argparse.ArgumentParser(description="Heal  drifted Summary View Time bindings.")
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
