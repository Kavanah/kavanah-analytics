"""Sweep: repoint page_posts_impressions -> page_posts_impressions_organic.

``page_posts_impressions`` (the organic+paid total) is INVALID on Graph v25.0
(#100) — as are ``_paid`` and ``_unique``; only ``page_posts_impressions_organic``
survives (staging probe, 2026-06-26). self-heal drops the bare name
from every import run, so anything charting it renders EMPTY. The importer now
requests ``_organic`` (see get_page_insights.PAGE_INSIGHTS_METRICS); this tool
repoints the saved dashboard docs that still read the dead field.

Confirmed prod inventory: 3 dashboards x 2 references
(table column + chart y_fields) = 6 spots — the two "Facebook Insights" copies
(a1bf967e…, 8721c4dc…) and "Daily FB Metrics" (3336226b86cd5af5). The sweep is
generic, though: it walks EVERY ``de_asset_echarts_dashboards`` doc plus every
``de_dashboard_templates`` doc, so per-workspace copies and save-as-template
clones are all caught (mirrors the sweep playbook).

Per chart it rewrites every field-bearing location:
  * chart ``y_field`` / ``y_fields[]``
  * chart ``y_field_labels`` (re-keys the entry, keeps custom display text)
  * ``table_columns[].field`` (+ nested ``data_sources[].field``)
  * chart- and dashboard-level ``filters[].field``

Because ``_organic`` UNDERCOUNTS the old total (organic-only, excludes paid),
labels are made honest: a rewritten spot whose label is the stock
"Page Posts Impressions" / "Post Impressions" (or absent) gets
"Page Posts Impressions (Organic)"; a custom label is kept but gets
" (Organic)" appended unless it already says organic.

Exact-match on the full field name — ``page_posts_impressions_unique`` (still
in the schema for historical docs) and an already-swapped ``_organic`` are
never touched. After the structured rewrite the tool regex-scans each doc's
raw JSON for any LEFTOVER bare reference it could not rewrite structurally
(calc-field expressions, canvas bindings, …) and logs a loud WARN naming the
doc — no silent misses. ``de_report_snapshots`` is frozen render history (it
never re-queries), so it is scanned read-only and reported, never rewritten.

**Dry-run by default.** Use ``--apply`` to write. Idempotent: a second pass
finds nothing to swap.

Usage::

    docker exec -w /app/python imports_container python3 \\
        -m tools.repoint_page_posts_impressions
    docker exec -w /app/python imports_container python3 \\
        -m tools.repoint_page_posts_impressions --apply
"""

from __future__ import annotations

import argparse
import copy
import json
import logging
import re
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

OLD_FIELD = "page_posts_impressions"
NEW_FIELD = "page_posts_impressions_organic"
NEW_LABEL = "Page Posts Impressions (Organic)"
# Stock labels the confirmed prod spots use — replaced wholesale with NEW_LABEL.
STOCK_LABELS = {"page posts impressions", "post impressions"}
SNAPSHOTS_INDEX = "de_report_snapshots"

# Leftover scan: the bare name NOT followed by a suffix character (won't match
# _organic/_unique/_paid after the structured rewrite).
_LEFTOVER_RE = re.compile(r"page_posts_impressions(?![_a-zA-Z0-9])")


def _relabel(old_label: Any) -> str:
    """The honest organic-only label for a spot whose field was swapped."""
    if not isinstance(old_label, str) or not old_label.strip() or old_label.strip().lower() in STOCK_LABELS:
        return NEW_LABEL
    if "organic" in old_label.lower():
        return old_label
    return f"{old_label} (Organic)"


def _swap_filters(filters: Any) -> int:
    swaps = 0
    for f in filters or []:
        if isinstance(f, dict) and f.get("field") == OLD_FIELD:
            f["field"] = NEW_FIELD
            swaps += 1
    return swaps


def swap_config(echarts_config: dict[str, Any]) -> tuple[dict[str, Any], int]:
    """Rewrite every OLD_FIELD reference in one echarts_config.

    Returns the mutated (deep-copied) config and the swap count. Exact-match
    on the full field name only — suffixed siblings are never touched.
    """
    out = copy.deepcopy(echarts_config)
    swaps = 0

    swaps += _swap_filters(out.get("filters"))

    for chart in out.get("charts") or []:
        if not isinstance(chart, dict):
            continue

        y_swapped = False
        if chart.get("y_field") == OLD_FIELD:
            chart["y_field"] = NEW_FIELD
            swaps += 1
            y_swapped = True

        y_fields = chart.get("y_fields")
        if isinstance(y_fields, list) and OLD_FIELD in y_fields:
            chart["y_fields"] = [NEW_FIELD if f == OLD_FIELD else f for f in y_fields]
            swaps += 1
            y_swapped = True

        # Re-key the label entry; without one the legend would fall back to the
        # raw new field name, so seed the honest label whenever a y-axis spot
        # was swapped THIS run (a chart already on _organic is left untouched).
        labels = chart.get("y_field_labels")
        if isinstance(labels, dict) and OLD_FIELD in labels:
            labels[NEW_FIELD] = _relabel(labels.pop(OLD_FIELD))
        elif y_swapped:
            labels = dict(labels or {})
            labels[NEW_FIELD] = NEW_LABEL
            chart["y_field_labels"] = labels

        for col in chart.get("table_columns") or []:
            if not isinstance(col, dict):
                continue
            if col.get("field") == OLD_FIELD:
                col["field"] = NEW_FIELD
                col["label"] = _relabel(col.get("label"))
                swaps += 1
            for ds in col.get("data_sources") or []:
                if isinstance(ds, dict) and ds.get("field") == OLD_FIELD:
                    ds["field"] = NEW_FIELD
                    swaps += 1

        swaps += _swap_filters(chart.get("filters"))

    return out, swaps


def _warn_leftovers(doc_id: str, index: str, source: dict) -> None:
    """Loudly flag bare references the structured rewrite could not reach."""
    n = len(_LEFTOVER_RE.findall(json.dumps(source)))
    if n:
        logger.warning(
            "LEFTOVER: %s/%s still carries %d bare %r reference(s) outside the "
            "known chart shapes (calc field? canvas binding?) — fix by hand.",
            index,
            doc_id,
            n,
            OLD_FIELD,
        )


def _sweep_index(es, index: str, apply: bool) -> int:
    """Rewrite every doc in ``index`` whose echarts_config references OLD_FIELD."""
    try:
        resp = es.search(index=index, body={"size": 1000, "query": {"match_all": {}}}, ignore_unavailable=True)
    except Exception as e:  # noqa: BLE001 — top-level boundary
        logger.warning("Could not query %s: %s", index, e)
        return 0

    hits = (resp.body if hasattr(resp, "body") else resp).get("hits", {}).get("hits", [])
    total_swaps = 0
    n_docs = 0
    for hit in hits:
        src = hit["_source"]
        new_config, swaps = swap_config(src.get("echarts_config") or {})
        if swaps:
            n_docs += 1
            total_swaps += swaps
            logger.info(
                "  %s/%s (title=%r): %d swap(s)",
                index,
                hit["_id"],
                src.get("title") or src.get("name"),
                swaps,
            )
            if apply:
                es.update(index=index, id=hit["_id"], body={"doc": {"echarts_config": new_config}}, refresh="wait_for")
        _warn_leftovers(hit["_id"], index, {**src, "echarts_config": new_config})
    logger.info(
        "%s: %d doc(s) with %d total swap(s)%s.",
        index,
        n_docs,
        total_swaps,
        " — applied" if apply and total_swaps else "",
    )
    return total_swaps


def _scan_snapshots(es) -> None:
    """Read-only: report (never rewrite) bare references in frozen snapshots."""
    try:
        resp = es.count(
            index=SNAPSHOTS_INDEX,
            body={"query": {"query_string": {"query": f'"{OLD_FIELD}"', "default_operator": "AND"}}},
            ignore_unavailable=True,
        )
    except Exception as e:  # noqa: BLE001 — top-level boundary
        logger.info("%s: scan skipped (%s).", SNAPSHOTS_INDEX, e)
        return
    count = (resp.body if hasattr(resp, "body") else resp).get("count", 0)
    logger.info(
        "%s: %d frozen snapshot(s) mention %r — historical renders, left as-is.",
        SNAPSHOTS_INDEX,
        count,
        OLD_FIELD,
    )


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--apply", action="store_true", help="Write the swap to ES. Default is dry-run.")
    args = parser.parse_args()

    logging.basicConfig(level=logging.INFO, format="%(message)s")

    es = get_elasticsearch_client()
    if es is None:
        logger.error("ES unavailable")
        return 2

    if not args.apply:
        logger.info("DRY-RUN — pass --apply to write. Swapping %r -> %r.", OLD_FIELD, NEW_FIELD)

    total = _sweep_index(es, DASHBOARDS_INDEX, args.apply)
    total += _sweep_index(es, TEMPLATES_INDEX, args.apply)
    _scan_snapshots(es)

    if total == 0:
        logger.info("Nothing to do — no %r references found.", OLD_FIELD)
    elif not args.apply:
        logger.info("DRY-RUN complete: %d swap(s) would be applied. Re-run with --apply.", total)
    else:
        logger.info("DONE: %d swap(s) applied.", total)
    return 0


if __name__ == "__main__":
    sys.exit(main())
