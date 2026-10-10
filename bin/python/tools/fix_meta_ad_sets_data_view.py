"""One-shot fix: swap the Ad Sets template's data_view from weekly to daily.

The ``meta_ad_sets`` template in ``de_dashboard_templates`` was authored
against ``*_fb_insights_weekly``. Wizard signups only have data in
``*_fb_insights`` until the weekly rollup runs, so installing this
template via the upcoming Builder picker would surface an empty
dashboard for every fresh user.

The fix swaps every ``data_view: "*_fb_insights_weekly"`` reference
inside the template's ``echarts_config`` (dashboard-level filters +
per-chart ``data_view``) to ``"*_fb_insights"``. The Ad Sets chart's
columns are all daily-compatible — ``insight_impressions``,
``insight_reach``, ``insight_spend``, etc. all exist in the daily
index with the same names.

**Dry-run by default.** Use ``--apply`` to write.

Idempotent: re-running after a successful pass is a no-op (no
``*_fb_insights_weekly`` strings remain). Other templates that
legitimately use the weekly index (e.g. ``first_look_meta_ads``'s
weekly bar chart) are untouched because we filter by template_id.

Usage::

    docker exec -w /app/python imports_container python3 \\
        -m tools.fix_meta_ad_sets_data_view

    docker exec -w /app/python imports_container python3 \\
        -m tools.fix_meta_ad_sets_data_view --apply
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
for _candidate in (
    _HERE.parents[1],
    _HERE.parents[2] / "bin" / "python",
    Path("/app/python"),
):
    if (_candidate / "asset_management").exists():
        sys.path.insert(0, str(_candidate))
        break

from asset_management.web.services.template_service import (  # noqa: E402
    TEMPLATES_INDEX,
)
from lib.es_helpers import get_elasticsearch_client  # noqa: E402

logger = logging.getLogger(__name__)

TEMPLATE_ID = "meta_ad_sets"
OLD_DATA_VIEW = "*_fb_insights_weekly"
NEW_DATA_VIEW = "*_fb_insights"


def _swap_data_views(echarts_config: dict[str, Any]) -> tuple[dict[str, Any], int]:
    """Walk ``echarts_config`` and swap every matching ``data_view``.

    Returns the mutated config and a count of swaps performed.
    """
    out = copy.deepcopy(echarts_config)
    swaps = 0
    for f in out.get("filters") or []:
        if f.get("data_view") == OLD_DATA_VIEW:
            f["data_view"] = NEW_DATA_VIEW
            swaps += 1
    for chart in out.get("charts") or []:
        if chart.get("data_view") == OLD_DATA_VIEW:
            chart["data_view"] = NEW_DATA_VIEW
            swaps += 1
        for f in chart.get("filters") or []:
            if f.get("data_view") == OLD_DATA_VIEW:
                f["data_view"] = NEW_DATA_VIEW
                swaps += 1
    return out, swaps


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--apply",
        action="store_true",
        help="Write the swap to ES. Default is dry-run.",
    )
    args = parser.parse_args()

    logging.basicConfig(level=logging.INFO, format="%(message)s")

    es = get_elasticsearch_client()
    if es is None:
        logger.error("ES unavailable")
        return 2

    try:
        doc = es.get(index=TEMPLATES_INDEX, id=TEMPLATE_ID)
    except Exception as e:  # noqa: BLE001 — top-level boundary
        logger.error("Template %r not found in %s: %s", TEMPLATE_ID, TEMPLATES_INDEX, e)
        return 2

    source = doc["_source"]
    echarts_config = source.get("echarts_config") or {}
    new_config, swaps = _swap_data_views(echarts_config)

    if swaps == 0:
        logger.info("No %r references found in %r — nothing to do.", OLD_DATA_VIEW, TEMPLATE_ID)
        return 0

    logger.info("Found %d %r → %r swap(s) in %r.", swaps, OLD_DATA_VIEW, NEW_DATA_VIEW, TEMPLATE_ID)

    if not args.apply:
        logger.info("DRY-RUN — pass --apply to write. Resulting echarts_config:")
        logger.info(json.dumps(new_config, indent=2, default=str))
        return 0

    try:
        es.update(
            index=TEMPLATES_INDEX,
            id=TEMPLATE_ID,
            body={"doc": {"echarts_config": new_config}},
            refresh="wait_for",
        )
    except Exception as e:  # noqa: BLE001 — top-level boundary
        logger.error("Update failed: %s", e)
        return 2

    logger.info("Applied. Template %r now uses %r.", TEMPLATE_ID, NEW_DATA_VIEW)
    return 0


if __name__ == "__main__":
    sys.exit(main())
