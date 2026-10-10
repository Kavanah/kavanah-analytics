"""One-shot heal: fix Echo first-look dashboards that render empty under a date
range, plus the stale single-team pin on the same docs.

Two independent defects leave Echo first-look dashboards (and the template they
clone from) showing zero data even when the conversations are present in ES:

* **Defect A — wrong date field.** The two Echo bar charts
  (``fl_echo_outcomes_daily`` / ``fl_echo_multiplier_daily``) carry
  ``x_field="@timestamp"`` — the doc's import/write time — instead of
  ``kibana_date``, the conversation's actual date (ISO ``yyyy-MM-dd``). A
  date-bounded render (a report snapshot, or a dashboard with a time window)
  filters on ``@timestamp`` and matches nothing, because a historical window
  never contains today's import timestamp. The heal repoints those charts to
  ``kibana_date`` (the field that holds the queryable event date; verified
  mapped ``date`` / ``format yyyy-MM-dd``).

* **Defect B — stale single-team pin.** Charts instantiated
  BEFORE the 2026-06-18 drop-the-pin fix carry a hard
  ``{field: team_esid, op: match_phrase, value: <one team>}`` filter. Per the
  product decision, an Echo template must show the viewer's in-scope data
  aggregated (Layer-1 index scoping already constrains to the viewer's teams),
  not pin to a single team. The heal DROPS those team_esid pin filters — but
  ONLY ones carrying a concrete team value; an unresolved-sentinel or
  placeholder filter is left for the heal / the install path to own.

Targets BOTH:

* the live template doc ``de_dashboard_templates/first_look_echo`` — so future
  installs are correct without a separate re-seed (the Python source in
  ``tools.migrate_first_look_to_templates`` is fixed in the same PR, but the
  ES doc is what ``TemplateService.instantiate`` actually clones from); and
* every instantiated Echo first-look dashboard in
  ``de_asset_echarts_dashboards`` (``from_template`` + ``template_id`` ==
  ``first_look_echo``, or any dashboard carrying ``fl_echo_*`` charts).

Safety:

* **Dry-run by default.** ``--apply`` writes. A per-doc JSON snapshot of the
  original is written to ``--snapshot-dir`` before any write, so every change
  is reversible.
* **Idempotent.** A second ``--apply`` finds no ``@timestamp`` Echo bar charts
  and no concrete team_esid pins -> ``healed=0``.
* **Scope.** By default heals the template doc + every Echo first-look
  dashboard. Pass ``--dashboard-id ID`` (repeatable) to restrict the dashboard
  pass to specific ids (the template doc is always considered unless
  ``--skip-template``).

Usage::

    docker exec -w /app/python imports_container python3 \\
        -m tools.heal_echo_first_look_date_field

    docker exec -w /app/python imports_container python3 \\
        -m tools.heal_echo_first_look_date_field --apply
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
ECHO_TEMPLATE_ID = "first_look_echo"
DEFAULT_SNAPSHOT_DIR = "/app/data/snapshots_3"

# Defect A — the wrong date field and its correction.
WRONG_X_FIELD = "@timestamp"
CORRECT_X_FIELD = "kibana_date"

# Defect B — the placeholder values that mean "not a concrete pin"; a team_esid
# filter carrying one of these is left alone (the install path / heal
# owns the placeholder/sentinel lifecycle). We only drop concrete-valued pins.
_PLACEHOLDER_MARKERS = ("PLACEHOLDER", "__UNRESOLVED")


def _is_echo_first_look_charts(charts: list) -> bool:
    """True if any chart id is an Echo first-look chart (``fl_echo_*``)."""
    for c in charts or []:
        if str((c or {}).get("chart_id") or "").startswith("fl_echo"):
            return True
    return False


def _is_concrete_team_pin(f: dict) -> bool:
    """True for a team_esid filter carrying a concrete team value (the stale
     pin) — NOT a placeholder/sentinel slot."""
    if not isinstance(f, dict) or f.get("field") != "team_esid":
        return False
    val = f.get("value")
    sv = f.get("selected_values")
    candidates = []
    if val not in (None, ""):
        candidates.append(str(val))
    if isinstance(sv, list):
        candidates.extend(str(v) for v in sv if v not in (None, ""))
    if not candidates:
        return False
    # If EVERY value is a placeholder/sentinel marker, it's not a concrete pin.
    for v in candidates:
        if any(m in v for m in _PLACEHOLDER_MARKERS):
            return False
    return True


def heal_charts(charts: list) -> tuple[int, int]:
    """Repoint @timestamp Echo bar charts to kibana_date and drop concrete
    team_esid pins, in place. Returns (date_repoints, pins_dropped)."""
    date_repoints = 0
    pins_dropped = 0
    for chart in charts or []:
        if chart.get("x_field") == WRONG_X_FIELD:
            chart["x_field"] = CORRECT_X_FIELD
            date_repoints += 1
        kept = []
        for f in chart.get("filters") or []:
            if _is_concrete_team_pin(f):
                pins_dropped += 1
                continue
            kept.append(f)
        chart["filters"] = kept
    return date_repoints, pins_dropped


def _charts_of(echarts_config: dict) -> list:
    return (echarts_config or {}).get("charts") or []


def find_target_dashboards(es, dashboard_ids: list[str] | None) -> list[dict]:
    """Echo first-look dashboards: explicit ids, or every from_template
    dashboard whose template is first_look_echo OR carrying fl_echo_* charts.

    ``echarts_config`` is mapped ``{type: object, enabled: false}`` — its
    contents are NOT indexed, so we fetch and filter client-side."""
    if dashboard_ids:
        out = []
        for did in dashboard_ids:
            try:
                d = es.get(index=DASHBOARDS_INDEX, id=did)
                out.append({"_id": did, "_source": d["_source"]})
            except Exception as e:  # noqa: BLE001 — boundary
                logger.warning("dashboard %s not found: %s", did, e)
        return out
    resp = es.search(index=DASHBOARDS_INDEX, body={"size": 2000, "query": {"match_all": {}}})
    hits = resp.get("hits", {}).get("hits", [])
    out = []
    for h in hits:
        src = h.get("_source") or {}
        ec = src.get("echarts_config") or {}
        if src.get("template_id") == ECHO_TEMPLATE_ID or _is_echo_first_look_charts(_charts_of(ec)):
            out.append(h)
    return out


def heal(es, *, apply: bool, snapshot_dir: str, dashboard_ids: list[str] | None, skip_template: bool) -> dict:
    snap_dir = Path(snapshot_dir)
    if apply:
        snap_dir.mkdir(parents=True, exist_ok=True)

    healed = 0
    date_repoints = 0
    pins_dropped = 0

    def _heal_doc(index: str, did: str, src: dict, config_key: str) -> None:
        nonlocal healed, date_repoints, pins_dropped
        ec = src.get(config_key) or {}
        charts = _charts_of(ec)
        if not charts:
            return
        dr, pd = heal_charts(charts)
        if not (dr or pd):
            return
        if apply:
            (snap_dir / f"{index}__{did}.json").write_text(json.dumps(src), encoding="utf-8")
        logger.info(
            "%s %s/%s: date_repoints=%d pins_dropped=%d",
            "APPLY" if apply else "DRY-RUN",
            index,
            did,
            dr,
            pd,
        )
        date_repoints += dr
        pins_dropped += pd
        healed += 1
        if apply:
            es.update(index=index, id=did, doc={config_key: ec}, refresh="wait_for")

    # (1) Template doc — what future installs clone from.
    if not skip_template:
        try:
            tdoc = es.get(index=TEMPLATES_INDEX, id=ECHO_TEMPLATE_ID)
            _heal_doc(TEMPLATES_INDEX, ECHO_TEMPLATE_ID, tdoc["_source"], "echarts_config")
        except Exception as e:  # noqa: BLE001 — boundary; a missing template just isn't healed
            logger.warning("template %s not found / unreadable: %s", ECHO_TEMPLATE_ID, e)

    # (2) Instantiated dashboards.
    for hit in find_target_dashboards(es, dashboard_ids):
        _heal_doc(DASHBOARDS_INDEX, hit["_id"], hit["_source"], "echarts_config")

    return {
        "healed": healed,
        "date_repoints": date_repoints,
        "pins_dropped": pins_dropped,
        "applied": apply,
    }


def main() -> int:
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    ap = argparse.ArgumentParser(description="Heal  Echo first-look date field + stale team pin.")
    ap.add_argument("--apply", action="store_true", help="write changes (default: dry-run)")
    ap.add_argument("--snapshot-dir", default=DEFAULT_SNAPSHOT_DIR, help="where to write reversible snapshots")
    ap.add_argument("--dashboard-id", action="append", dest="dashboard_ids", help="restrict dashboard pass to id(s)")
    ap.add_argument("--skip-template", action="store_true", help="don't touch the template doc, only dashboards")
    args = ap.parse_args()

    es = get_elasticsearch_client()
    if es is None:
        logger.error("Elasticsearch unavailable")
        return 1

    result = heal(
        es,
        apply=args.apply,
        snapshot_dir=args.snapshot_dir,
        dashboard_ids=args.dashboard_ids,
        skip_template=args.skip_template,
    )
    logger.info("RESULT: %s", json.dumps(result, indent=2))
    if not args.apply:
        logger.info("DRY-RUN only. Re-run with --apply to write.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
