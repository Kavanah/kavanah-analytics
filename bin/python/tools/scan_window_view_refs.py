"""read-only inventory: find every dashboard/template referencing the
FB dedup "window" data view, by scanning the raw stored JSON.

WHY NOT an ES text query: ``echarts_config`` is a nested object and a
``match`` query on it analyzes the text — ``*_fb_insights_window`` is tokenized
(the ``*``, the underscores) so a ``match: {echarts_config: "fb_insights_window"}``
returns FALSE NEGATIVES (verified on staging 2026-06-28: it returned 0 while a
real ``Ad Sets (Delivery)`` dashboard had 3 window data_view pins). This walks
the raw ``_source`` instead, the same traversal the repoint tool uses, so the
inventory is trustworthy. Use THIS, not a Dev Tools ``match``, as the step-1
inventory and the step-3 re-scan gate before flipping DEDUP_ROUTING_ENABLED.

Writes nothing. Reports per doc: index, id, title, template_id, and the exact
JSON path of each reference (so you can tell a chart/filter ``data_view`` pin —
which the repoint tool fixes — from a stray reference somewhere else).

Run in imports_container:
  docker exec -w /app/python imports_container python3 -m tools.scan_window_view_refs
"""

from __future__ import annotations

import sys
from pathlib import Path

_HERE = Path(__file__).resolve().parent
for _candidate in (_HERE.parents[1], _HERE.parents[2] / "bin" / "python", Path("/app/python")):
    if (_candidate / "lib").exists():
        sys.path.insert(0, str(_candidate))
        break

from lib.es_helpers import get_elasticsearch_client  # noqa: E402

NEEDLE = "fb_insights_window"
INDICES = ["de_dashboard_templates", "de_asset_echarts_dashboards"]


def _find_paths(obj, path=""):
    """Yield (json_path, value) for every leaf string containing NEEDLE."""
    if isinstance(obj, dict):
        for k, v in obj.items():
            yield from _find_paths(v, f"{path}.{k}")
    elif isinstance(obj, list):
        for i, v in enumerate(obj):
            yield from _find_paths(v, f"{path}[{i}]")
    elif isinstance(obj, str) and NEEDLE in obj:
        yield (path, obj)


def main() -> int:
    es = get_elasticsearch_client()
    if es is None:
        print("ES unavailable")
        return 2

    grand_total = 0
    for index in INDICES:
        print(f"\n===== {index} =====")
        try:
            resp = es.search(index=index, body={"size": 2000, "query": {"match_all": {}}}, ignore_unavailable=True)
        except Exception as e:  # noqa: BLE001 — top-level boundary
            print(f"  query failed: {e}")
            continue
        hits = (resp.body if hasattr(resp, "body") else resp).get("hits", {}).get("hits", [])
        print(f"  scanned {len(hits)} doc(s)")
        index_hits = 0
        for hit in hits:
            src = hit.get("_source", {})
            paths = list(_find_paths(src.get("echarts_config") or {}, "echarts_config"))
            if not paths:
                continue
            index_hits += 1
            grand_total += 1
            print(
                f"\n  >>> {hit['_id']}  title={src.get('title')!r}  "
                f"template_id={src.get('template_id')!r}  "
                f"team={src.get('team_esid') or src.get('template_team_esid')!r}"
            )
            for p, val in paths:
                short = val if len(val) < 80 else val[:77] + "..."
                print(f"        {p} = {short!r}")
        print(f"  -- {index}: {index_hits} doc(s) reference {NEEDLE}")

    print(f"\n===== TOTAL: {grand_total} doc(s) reference {NEEDLE} across {len(INDICES)} indices =====")
    if grand_total:
        print("Repoint each before flipping DEDUP_ROUTING_ENABLED. The repoint tool's default")
        print("pass only covers the meta_ad_sets TEMPLATE; a standalone dashboard (template_id=None)")
        print("needs --title '<title>' or --dashboard-id '<id>'. Re-run this until it reports 0.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
