"""One-shot heal: re-square canvas icon badges squashed by page resize
.

``scaleDocToPage`` scaled every object frame per-axis when the
page's dimensions or orientation changed, squashing the seeded 44x44 section
badges into pills (44x37 on staging, 50x32 on prod's Summary Report for one team)
while ``style.radius`` / ``style.padding`` stayed fixed — the circle
distorted and the contain-fit glyph shrank to the short axis. The editor fix
(icons now scale uniformly) ships with this script; the script repairs docs
that were already squashed.

Scope: icon objects that carry a badge background (``style.bg_color``) whose
frame is NOT square — a non-square badge is per-axis-scaling damage by
definition (the seeder only ever writes squares). Each is re-squared to the
seeded 44x44 about its current center with ``radius: 22``. Square badges of
any size are deliberately left alone (an author may have resized one on
purpose).

Already applied by hand (Kibana painless, 2026-08-20): staging
"Summary Report — Canvas" and prod one team's Summary Report. This script is
the recorded, repeatable version for every other doc/environment; re-running
it against those docs is a no-op.

Safety (same contract as heal_summary_view_time):

* **Dry-run by default.** ``--apply`` writes. A per-doc JSON snapshot of the
  original is written to ``--snapshot-dir`` before any write.
* **Idempotent.** A second ``--apply`` finds no non-square badges -> healed=0.
* **Scope.** All canvas docs in ``de_asset_echarts_dashboards`` and
  ``de_dashboard_templates`` are swept; pass ``--dashboard-id ID``
  (repeatable) to restrict to specific dashboard ids.

Usage::

    docker exec -w /app/python imports_container python3 \\
        -m tools.heal_canvas_icon_badge_frames

    docker exec -w /app/python imports_container python3 \\
        -m tools.heal_canvas_icon_badge_frames --apply
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
DEFAULT_SNAPSHOT_DIR = "/app/data/snapshots_6"

# The seeded badge geometry (canvas_seed.py badge(): badge_r = 22).
BADGE_SIZE = 44
BADGE_RADIUS = 22


def _is_badge_icon(obj: dict) -> bool:
    """An icon object rendered as a section badge: colored circle behind it."""
    if not isinstance(obj, dict) or obj.get("object_type") != "icon":
        return False
    style = obj.get("style")
    return isinstance(style, dict) and bool(style.get("bg_color"))


def heal_objects(objects: list) -> int:
    """Re-square non-square badge icons in place; returns the heal count.

    Only badges whose frame is non-square are touched (per-axis-scaling
    damage); each is centered back onto a 44x44 square with radius 22.
    Square badges — including deliberately resized ones — are left alone.
    """
    healed = 0
    for obj in objects or []:
        if not _is_badge_icon(obj):
            continue
        frame = obj.get("frame")
        if not isinstance(frame, dict):
            continue
        w = frame.get("w") or 0
        h = frame.get("h") or 0
        if w == h:
            continue
        cx = (frame.get("x") or 0) + w / 2
        cy = (frame.get("y") or 0) + h / 2
        frame["w"] = BADGE_SIZE
        frame["h"] = BADGE_SIZE
        frame["x"] = round(cx - BADGE_SIZE / 2)
        frame["y"] = round(cy - BADGE_SIZE / 2)
        obj["style"]["radius"] = BADGE_RADIUS
        healed += 1
    return healed


def _canvas_docs(es, index: str, only_ids: set[str] | None):
    body = {"query": {"match": {"dashboard_type": "canvas"}}, "size": 500}
    try:
        hits = es.search(index=index, body=body)["hits"]["hits"]
    except Exception as e:  # noqa: BLE001 — a missing templates index is fine
        logger.warning("skipping %s: %s", index, e)
        return
    for h in hits:
        if only_ids and h["_id"] not in only_ids:
            continue
        yield h["_id"], h["_source"]


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--apply", action="store_true", help="write changes (default: dry-run)")
    parser.add_argument("--dashboard-id", action="append", default=[], help="restrict to this dashboard id (repeatable)")
    parser.add_argument("--snapshot-dir", default=DEFAULT_SNAPSHOT_DIR)
    args = parser.parse_args()

    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    es = get_elasticsearch_client()
    if es is None:
        logger.error("Elasticsearch unavailable")
        return 1

    only_ids = set(args.dashboard_id) or None
    total_docs = 0
    total_healed = 0
    for index in (DASHBOARDS_INDEX, TEMPLATES_INDEX):
        for doc_id, src in _canvas_docs(es, index, only_ids):
            objects = (src.get("canvas_config") or {}).get("objects") or []
            before = json.dumps(src) if args.apply else None
            healed = heal_objects(objects)
            if not healed:
                continue
            total_docs += 1
            total_healed += healed
            logger.info(
                "%s %s/%s (%r): %d badge(s) re-squared",
                "HEAL" if args.apply else "WOULD HEAL",
                index,
                doc_id,
                src.get("title"),
                healed,
            )
            if args.apply:
                snap_dir = Path(args.snapshot_dir)
                snap_dir.mkdir(parents=True, exist_ok=True)
                (snap_dir / f"{index}__{doc_id}.json").write_text(before, encoding="utf-8")
                es.index(index=index, id=doc_id, document=src, refresh="wait_for")
    logger.info(
        "%s: %d badge(s) across %d doc(s)", "healed" if args.apply else "dry-run, would heal", total_healed, total_docs
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
