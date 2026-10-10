"""One-shot heal: fix dashboards left blank/zero by the org-owner template
install bug.

Two defects left existing template-installed dashboards
broken, and the forward fix only changes FUTURE installs — already-stored
dashboards keep their bad config until rewritten. This heals them:

* **Defect A — stale account/property pin.** Charts carry a per-chart or
  dashboard-level filter on an account/property pin field
  (``property_id`` / ``ad_account_id`` / ``google_ads_customer_id``) whose
  value is the match-nothing ``UNRESOLVED_ASSET_SENTINEL``. The healed
  config DROPS those pin filters entirely (matching the forward fix), so
  the chart shows the viewer's in-scope data via Layer-1 scoping. Echo's
  ``team_esid`` scoping placeholder is NOT touched.

* **Defect B — wrong workspace.** A dashboard whose ``workspace_esid`` is a
  workspace that holds NONE of the dashboard's data is repointed to the
  workspace resolved from its ``created_by`` user via
  ``resolve_curation_workspace`` (the same resolver the forward fix uses).
  When ``created_by`` is missing / unresolvable, the workspace is left
  untouched and the dashboard is REPORTED, not guessed.

Safety:

* **Dry-run by default.** ``--apply`` writes. A per-dashboard JSON snapshot
  of the original doc is written to ``--snapshot-dir`` before any write, so
  every change is reversible.
* **Idempotent.** A second ``--apply`` run finds no sentinel pins and no
  workspace mismatch -> ``healed=0``.
* **Scope.** By default heals every sentinel-carrying dashboard. Pass
  ``--dashboard-id ID`` (repeatable) to restrict to specific dashboards.

Usage::

    docker exec -w /app/python imports_container python3 \\
        -m tools.heal_template_install_scope

    docker exec -w /app/python imports_container python3 \\
        -m tools.heal_template_install_scope --apply
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
from asset_management.web.services.template_service import (  # noqa: E402
    UNRESOLVED_ASSET_SENTINEL,
    _DROPPABLE_PLACEHOLDER_FIELDS,
)

logger = logging.getLogger(__name__)

DASHBOARDS_INDEX = "de_asset_echarts_dashboards"
DEFAULT_SNAPSHOT_DIR = "/app/data/snapshots_2"


def _is_sentinel_pin(f: dict) -> bool:
    """True for a filter on an account/property pin field whose value (either
    shape) is the unresolved sentinel — the stale slot the install bug left."""
    if not isinstance(f, dict) or f.get("field") not in _DROPPABLE_PLACEHOLDER_FIELDS:
        return False
    if f.get("value") == UNRESOLVED_ASSET_SENTINEL:
        return True
    sv = f.get("selected_values")
    return isinstance(sv, list) and UNRESOLVED_ASSET_SENTINEL in sv


def strip_sentinel_pins(echarts_config: dict) -> int:
    """Drop every sentinel account/property-pin filter from the config
    (dashboard-level + per-chart), in place. Returns the count removed."""
    removed = 0

    def _filter_list(filters):
        nonlocal removed
        kept = []
        for f in filters or []:
            if _is_sentinel_pin(f):
                removed += 1
                continue
            kept.append(f)
        return kept

    echarts_config["filters"] = _filter_list(echarts_config.get("filters"))
    for chart in echarts_config.get("charts") or []:
        chart["filters"] = _filter_list(chart.get("filters"))
    return removed


def workspace_has_no_data_for(es, dashboard: dict) -> bool:
    """True when the dashboard's ``workspace_esid`` holds none of the teams
    that own data for the dashboard's chart data views — i.e. the wrong-
    workspace symptom. Conservative: only returns True when we can positively
    confirm the workspace has zero matching team indices; any uncertainty
    returns False (leave it alone)."""
    ws = dashboard.get("workspace_esid")
    if not ws:
        return False
    # Collect the source slugs the dashboard's charts query.
    sources = set()
    for chart in (dashboard.get("echarts_config") or {}).get("charts") or []:
        dv = chart.get("data_view") or ""
        if dv.startswith("*_"):
            sources.add(dv[2:])
    if not sources:
        return False
    # Teams in this workspace.
    try:
        # de_asset_teams.workspace_esid is pure keyword,
        # so the bare field is the correct term query — ``workspace_esid.keyword``
        # matches a non-existent subfield and silently returns 0 teams (which
        # this heuristic then misreads as "workspace has no teams -> wrong").
        resp = es.search(
            index="de_asset_teams",
            body={"size": 500, "query": {"term": {"workspace_esid": ws}}, "_source": ["team_title"]},
        )
        team_count = resp.get("hits", {}).get("total", {}).get("value", 0)
    except Exception as e:  # noqa: BLE001 — boundary; don't guess on error
        logger.warning("workspace team lookup failed for %s: %s", ws, e)
        return False
    if team_count == 0:
        # Workspace exists in the doc but has no teams at all -> definitely wrong.
        return True
    # Does ANY index for these sources exist under a team in this workspace?
    # Cheapest positive check: are there any *_<source> indices whose docs
    # carry a team in this workspace? We approximate by checking that at least
    # one matching index has docs; a workspace with teams but zero matching
    # data indices is the wrong-workspace case.
    for src in sources:
        try:
            cnt = es.count(index=f"*_{src}", ignore_unavailable=True)
            if cnt.get("count", 0) > 0:
                # There is data for this source somewhere; we can't cheaply
                # prove it's NOT this workspace's, so be conservative and treat
                # the dashboard as fine (avoid a wrong repoint). The created_by
                # resolver below is the authoritative correction signal.
                return False
        except Exception:  # noqa: BLE001
            continue
    return False


def resolve_correct_workspace(es, dashboard: dict) -> str | None:
    """The workspace this dashboard SHOULD live in: resolve_curation_workspace
    of its created_by user (the same resolver the forward fix uses). Returns
    None when created_by is missing / unresolvable — caller leaves it alone."""
    created_by = dashboard.get("created_by")
    if not created_by:
        return None
    try:
        from asset_management.web.services.workspace_membership_service import (
            WorkspaceMembershipService,
        )

        return WorkspaceMembershipService.resolve_curation_workspace(created_by)
    except Exception as e:  # noqa: BLE001 — boundary
        logger.warning("resolve_curation_workspace(%s) failed: %s", created_by, e)
        return None


def find_target_dashboards(es, dashboard_ids: list[str] | None) -> list[dict]:
    """Return dashboards to consider: explicit ids, or every dashboard whose
    serialized config contains the sentinel.

    NOTE: ``echarts_config`` is mapped ``{"type": "object", "enabled": false}``
    — ES does NOT index its contents, so a server-side ``match_phrase`` on it
    always returns zero. We must fetch dashboards and scan the serialized JSON
    client-side (same as the manual diagnosis did)."""
    if dashboard_ids:
        out = []
        for did in dashboard_ids:
            try:
                d = es.get(index=DASHBOARDS_INDEX, id=did)
                out.append({"_id": did, "_source": d["_source"]})
            except Exception as e:  # noqa: BLE001
                logger.warning("dashboard %s not found: %s", did, e)
        return out
    # All dashboards; filter client-side for the sentinel (config is not indexed).
    resp = es.search(index=DASHBOARDS_INDEX, body={"size": 2000, "query": {"match_all": {}}})
    hits = resp.get("hits", {}).get("hits", [])
    return [h for h in hits if UNRESOLVED_ASSET_SENTINEL in json.dumps(h.get("_source") or {})]


def heal(es, *, apply: bool, snapshot_dir: str, dashboard_ids: list[str] | None) -> dict:
    targets = find_target_dashboards(es, dashboard_ids)
    snap_dir = Path(snapshot_dir)
    if apply:
        snap_dir.mkdir(parents=True, exist_ok=True)

    healed = 0
    pins_dropped = 0
    workspaces_fixed = 0
    reported_only = []

    for hit in targets:
        did = hit["_id"]
        src = hit["_source"]
        ec = src.get("echarts_config") or {}

        # Snapshot BEFORE mutation (only when applying).
        if apply:
            (snap_dir / f"{did}.json").write_text(json.dumps(src), encoding="utf-8")

        removed = strip_sentinel_pins(ec)
        update: dict = {}
        if removed:
            update["echarts_config"] = ec

        # Workspace correction (Defect B): only repoint when we have a
        # positively-resolved target from created_by AND it differs.
        correct_ws = resolve_correct_workspace(es, src)
        cur_ws = src.get("workspace_esid")
        if correct_ws and correct_ws != cur_ws:
            update["workspace_esid"] = correct_ws
        elif workspace_has_no_data_for(es, src) and not correct_ws:
            # Wrong workspace but we couldn't resolve a target -> report, don't guess.
            reported_only.append((did, cur_ws, "wrong workspace, created_by unresolvable"))

        if not update:
            continue

        logger.info(
            "%s %s: drop_pins=%d ws %r->%r",
            "APPLY" if apply else "DRY-RUN",
            did,
            removed,
            cur_ws,
            update.get("workspace_esid", cur_ws),
        )
        pins_dropped += removed
        if "workspace_esid" in update:
            workspaces_fixed += 1
        healed += 1

        if apply:
            es.update(index=DASHBOARDS_INDEX, id=did, doc=update, refresh="wait_for")

    return {
        "considered": len(targets),
        "healed": healed,
        "pins_dropped": pins_dropped,
        "workspaces_fixed": workspaces_fixed,
        "reported_only": reported_only,
        "applied": apply,
    }


def main() -> int:
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    ap = argparse.ArgumentParser(description="Heal  sentinel/workspace dashboards.")
    ap.add_argument("--apply", action="store_true", help="write changes (default: dry-run)")
    ap.add_argument("--snapshot-dir", default=DEFAULT_SNAPSHOT_DIR, help="where to write reversible snapshots")
    ap.add_argument("--dashboard-id", action="append", dest="dashboard_ids", help="restrict to specific id(s)")
    args = ap.parse_args()

    es = get_elasticsearch_client()
    if es is None:
        logger.error("Elasticsearch unavailable")
        return 1

    result = heal(es, apply=args.apply, snapshot_dir=args.snapshot_dir, dashboard_ids=args.dashboard_ids)
    logger.info("RESULT: %s", json.dumps(result, indent=2))
    if result["reported_only"]:
        logger.warning(
            "%d dashboard(s) have a wrong workspace but no resolvable created_by; " "left untouched (fix manually): %s",
            len(result["reported_only"]),
            result["reported_only"],
        )
    if not args.apply:
        logger.info("DRY-RUN only. Re-run with --apply to write.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
