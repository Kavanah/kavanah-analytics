"""One-shot migration: seed the 4 first-look dicts into ``de_dashboard_templates``.

The legacy first-look service (``FirstLookDashboardService``)
reads templates from a hardcoded Python dict in
``first_look_templates.py`` and clones one dashboard per wizard
signup. supersedes that with an ES-backed registry
(``de_dashboard_templates``) so curators can author templates via the
Save-as-template UI.

The wizard step-7 finish handler (analytics/routes.py) now reads
from the new registry, so on day 1 of the cutover the registry
needs to contain the same four templates the old service shipped —
otherwise existing wizard signups get an empty /dashboard on the
next redirect.

This script reads ``first_look_templates.TEMPLATES`` (the 4 dicts:
``meta_ads``, ``google_ads``, ``google_analytics_4``, ``echo``) and
writes each as a ``de_dashboard_templates`` doc with:

* ``template_id`` = ``"first_look_<connector_id>"`` (stable slug)
* ``pack_id`` = ``connector_id`` (single-template pack)
* ``menu_title`` = derived from the connector display name
  (``Meta`` / ``Google Ads`` / ``Google Analytics`` / ``Echo``)
* ``placeholder_field`` = the connector's substitution key
  (``ad_account_id`` / ``google_ads_customer_id`` / ``property_id``
  / ``team_esid``)
* ``echarts_config`` = the dict's ``charts`` shape, with the
  ``ASSET_PLACEHOLDER`` tuple in chart filters left untouched (the
  detector recognizes both legacy tuple and new bare-string shapes,
  so this is safe).

The script is **dry-run by default**. Use ``--apply`` to write.

Idempotency: writes by ``template_id`` so re-running produces the
same docs. Existing docs with the same ``template_id`` are
overwritten — fine because the source-of-truth is the Python dict
until slice 5 deletes it.

Usage::

    docker exec -w /app/python imports_container python3 \\
        -m tools.migrate_first_look_to_templates

    docker exec -w /app/python imports_container python3 \\
        -m tools.migrate_first_look_to_templates --apply
"""

from __future__ import annotations

import argparse
import copy
import logging
import sys
from datetime import datetime, timezone
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
    ASSET_PLACEHOLDER,
    TEMPLATES_INDEX,
)
from lib.es_helpers import get_elasticsearch_client  # noqa: E402

logger = logging.getLogger(__name__)


# ─── Inlined TEMPLATES registry ────────────────────────────
#
# Before slice 5, this script read TEMPLATES from
# ``asset_management.web.services.first_look_templates``. Slice 5 deleted
# that service file along with the rest of the legacy path; the
# helpers + dict are inlined here so the migration script stays runnable
# (operators still execute it as Stage 3.5e of the prod runbook to
# seed the new ``de_dashboard_templates`` registry on cutover day). Once
# every environment has been seeded and the docs in ES are the canonical
# source, this script can also be retired — the inlined data is dead
# weight after that.
#
# The helpers below are byte-identical to the ones that lived in
# first_look_templates.py up through commit 6f8bb070d59.


def _metric_chart(
    *,
    chart_id: str,
    title: str,
    data_view: str,
    field: str,
    aggregation: str = "sum",
    col: int,
    row: int,
    width: int = 3,
    height: int = 2,
    placeholder_field: str = "ad_account_id",
) -> dict[str, Any]:
    return {
        "chart_id": chart_id,
        "chart_type": "metric",
        "title": title,
        "data_view": data_view,
        "y_fields": [field],
        "aggregation": aggregation,
        "filters": [
            {
                "field": placeholder_field,
                "op": "match_phrase_any",
                "value": ASSET_PLACEHOLDER,
            }
        ],
        "position": {"col": col, "row": row, "w": width, "h": height},
        "hide_title": False,
    }


def _bar_chart(
    *,
    chart_id: str,
    title: str,
    field: str,
    aggregation: str = "sum",
    col: int,
    row: int,
    width: int = 6,
    height: int = 4,
    data_view: str,
    placeholder_field: str = "ad_account_id",
    x_field: str = "kibana_date",
) -> dict[str, Any]:
    return {
        "chart_id": chart_id,
        "chart_type": "bar",
        "title": title,
        "data_view": data_view,
        "x_field": x_field,
        "y_fields": [field],
        "aggregation": aggregation,
        "filters": [
            {
                "field": placeholder_field,
                "op": "match_phrase_any",
                "value": ASSET_PLACEHOLDER,
            }
        ],
        "position": {"col": col, "row": row, "w": width, "h": height},
        "hide_title": False,
    }


TEMPLATES: dict[str, dict[str, Any]] = {
    "meta_ads": {
        "title": "Meta Ads — Spend & impressions",
        "charts": [
            _metric_chart(
                chart_id="fl_meta_spend_total", title="Spend", data_view="*_fb_insights", field="insight_spend", col=0, row=0
            ),
            _metric_chart(
                chart_id="fl_meta_impressions_total",
                title="Impressions",
                data_view="*_fb_insights",
                field="insight_impressions",
                col=3,
                row=0,
            ),
            _metric_chart(
                chart_id="fl_meta_reach_total", title="Reach", data_view="*_fb_insights", field="insight_reach", col=6, row=0
            ),
            _metric_chart(
                chart_id="fl_meta_clicks_total",
                title="Link clicks",
                data_view="*_fb_insights",
                field="insight_clicks",
                col=9,
                row=0,
            ),
            # these two bar charts read the retired *_fb_insights_weekly
            # index until the weekly pipeline was decommissioned — repointed at
            # the daily index (spend/impressions are additive, so per-day is the
            # correct grain).
            _bar_chart(
                chart_id="fl_meta_spend_daily",
                title="Daily spend",
                field="insight_spend",
                data_view="*_fb_insights",
                col=0,
                row=2,
            ),
            _bar_chart(
                chart_id="fl_meta_impressions_daily",
                title="Daily impressions",
                field="insight_impressions",
                data_view="*_fb_insights",
                col=6,
                row=2,
            ),
        ],
    },
    "google_ads": {
        "title": "Google Ads — Spend & conversions, last 30d",
        "charts": [
            _metric_chart(
                chart_id="fl_gads_cost_total",
                title="Cost (last 30d)",
                data_view="*_google_ads",
                field="cost",
                placeholder_field="google_ads_customer_id",
                col=0,
                row=0,
            ),
            _metric_chart(
                chart_id="fl_gads_clicks_total",
                title="Clicks (last 30d)",
                data_view="*_google_ads",
                field="clicks",
                placeholder_field="google_ads_customer_id",
                col=3,
                row=0,
            ),
            _metric_chart(
                chart_id="fl_gads_impressions_total",
                title="Impressions (last 30d)",
                data_view="*_google_ads",
                field="impressions",
                placeholder_field="google_ads_customer_id",
                col=6,
                row=0,
            ),
            _metric_chart(
                chart_id="fl_gads_conversions_total",
                title="Conversions (last 30d)",
                data_view="*_google_ads",
                field="conversions",
                placeholder_field="google_ads_customer_id",
                col=9,
                row=0,
            ),
            _bar_chart(
                chart_id="fl_gads_cost_daily",
                title="Daily cost",
                field="cost",
                data_view="*_google_ads",
                placeholder_field="google_ads_customer_id",
                x_field="date",
                col=0,
                row=2,
            ),
            _bar_chart(
                chart_id="fl_gads_conversions_daily",
                title="Daily conversions",
                field="conversions",
                data_view="*_google_ads",
                placeholder_field="google_ads_customer_id",
                x_field="date",
                col=6,
                row=2,
            ),
        ],
    },
    "google_analytics_4": {
        "title": "Google Analytics 4 — Sessions & engagement, last 30d",
        "charts": [
            _metric_chart(
                chart_id="fl_ga4_sessions_total",
                title="Sessions (last 30d)",
                data_view="*_ga_metrics",
                field="sessions",
                placeholder_field="property_id",
                col=0,
                row=0,
            ),
            # the unique-users tile reads the DEDUPLICATED window
            # index, not the per-day *_ga_metrics index. activeUsers is a unique
            # count; summing per-day × per-dimension rows over-counts ~3-10x
            # (reproduced on staging). The *_ga_metrics_window index holds one
            # property-level deduped row per pulled window; the dashboard's
            # date-picker pins to the latest window (get_latest_window), so the
            # sum here is across the team's properties within that window — not
            # across days. The daily-active-users bar chart below still reads the
            # per-day index (per-day is the correct grain there).
            _metric_chart(
                chart_id="fl_ga4_users_total",
                title="Active users (deduplicated, window)",
                data_view="*_ga_metrics_window",
                field="activeUsers",
                placeholder_field="property_id",
                col=3,
                row=0,
            ),
            _metric_chart(
                chart_id="fl_ga4_pageviews_total",
                title="Page views (last 30d)",
                data_view="*_ga_metrics",
                field="screenPageViews",
                placeholder_field="property_id",
                col=6,
                row=0,
            ),
            _metric_chart(
                chart_id="fl_ga4_engagement_total",
                title="Engagement duration (s)",
                data_view="*_ga_metrics",
                field="userEngagementDuration",
                placeholder_field="property_id",
                col=9,
                row=0,
            ),
            _bar_chart(
                chart_id="fl_ga4_sessions_daily",
                title="Daily sessions",
                field="sessions",
                data_view="*_ga_metrics",
                placeholder_field="property_id",
                x_field="kibana_date",
                col=0,
                row=2,
            ),
            _bar_chart(
                chart_id="fl_ga4_users_daily",
                title="Daily active users",
                field="activeUsers",
                data_view="*_ga_metrics",
                placeholder_field="property_id",
                x_field="kibana_date",
                col=6,
                row=2,
            ),
        ],
    },
    "echo": {
        "title": "Echo — Conversations & outcomes, last 30d",
        "charts": [
            _metric_chart(
                chart_id="fl_echo_conversations_total",
                title="Conversations (last 30d)",
                data_view="*_echo_conversations",
                field="spiritual_conversation",
                aggregation="value_count",
                placeholder_field="team_esid",
                col=0,
                row=0,
            ),
            _metric_chart(
                chart_id="fl_echo_spiritual_total",
                title="Spiritual conversations",
                data_view="*_echo_conversations",
                field="spiritual_conversation",
                placeholder_field="team_esid",
                col=3,
                row=0,
            ),
            _metric_chart(
                chart_id="fl_echo_multiplier_total",
                title="Connected with multiplier",
                data_view="*_echo_conversations",
                field="multiplier",
                placeholder_field="team_esid",
                col=6,
                row=0,
            ),
            _metric_chart(
                chart_id="fl_echo_no_response_total",
                title="No response",
                data_view="*_echo_conversations",
                field="no_response",
                placeholder_field="team_esid",
                col=9,
                row=0,
            ),
            # these two bar charts originally used x_field="@timestamp"
            # (the doc's import/write time) instead of "kibana_date" (the
            # conversation's actual date in ISO yyyy-MM-dd). A date-bounded render
            # then matched nothing — a historical report window never contains the
            # import timestamp. kibana_date is the queryable event-date field (the
            # _bar_chart default); the explicit @timestamp overrides are removed.
            _bar_chart(
                chart_id="fl_echo_outcomes_daily",
                title="Daily spiritual conversations",
                field="spiritual_conversation",
                data_view="*_echo_conversations",
                placeholder_field="team_esid",
                col=0,
                row=2,
            ),
            _bar_chart(
                chart_id="fl_echo_multiplier_daily",
                title="Daily multiplier connections",
                field="multiplier",
                data_view="*_echo_conversations",
                placeholder_field="team_esid",
                col=6,
                row=2,
            ),
        ],
    },
}


# Connector display names for the workspace menu the cloned dashboards
# land under. Keys are the ``first_look_templates.TEMPLATES`` keys
# (connector_id values); values are the user-facing menu titles.
# Adding a new key here when first_look_templates gains a connector
# would extend the migration without further code changes.
CONNECTOR_MENU_TITLES: dict[str, str] = {
    "meta_ads": "Meta",
    "google_ads": "Google Ads",
    "google_analytics_4": "Google Analytics",
    "echo": "Echo",
}


# Connector → ``placeholder_field`` (the doc field whose value the
# ``ASSET_PLACEHOLDER`` sentinel resolves to at instantiate time).
# Cribbed from the per-chart ``placeholder_field`` argument the
# first_look chart-builder helpers used (see _metric_chart /
# _bar_chart (renamed from _weekly_bar_chart) in first_look_templates.py).
CONNECTOR_PLACEHOLDER_FIELDS: dict[str, str] = {
    "meta_ads": "ad_account_id",
    "google_ads": "google_ads_customer_id",
    "google_analytics_4": "property_id",
    "echo": "team_esid",
}


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _build_template_doc(connector_id: str, source: dict[str, Any]) -> dict[str, Any]:
    """Translate one ``first_look_templates.TEMPLATES`` entry into a
    ``de_dashboard_templates`` doc.

    The source ``source`` dict has shape::

        {"title": "...", "charts": [...]}

    The destination shape mirrors what
    ``TemplateService.freeze_dashboard_as_template`` would have
    produced if a curator had hand-saved this dashboard via the
    Save-as-template UI.
    """
    template_id = f"first_look_{connector_id}"
    pack_id = connector_id
    menu_title = CONNECTOR_MENU_TITLES.get(connector_id, "Reports")
    placeholder_field = CONNECTOR_PLACEHOLDER_FIELDS.get(connector_id, "")

    # Deep-copy so the migration doesn't mutate the in-memory TEMPLATES
    # registry (the legacy code path still reads from it).
    charts = copy.deepcopy(source.get("charts") or [])

    now = _now_iso()
    return {
        "template_id": template_id,
        "pack_id": pack_id,
        "connector_id": connector_id,
        "name": source.get("title") or template_id,
        "description": f"First-look {menu_title} dashboard.",
        "menu_title": menu_title,
        "menu_position": 0,
        "required_sources": [connector_id],
        "placeholder_field": placeholder_field,
        "echarts_config": {
            "global_date_range": None,
            "filters": [],
            "charts": charts,
        },
        "frozen_from_dashboard_id": None,
        "created_at": now,
        "updated_at": now,
    }


def migrate(*, apply: bool) -> dict[str, Any]:
    """Read the 4 dicts, optionally write them as ``de_dashboard_templates``
    docs. Returns a summary dict for the CLI to print.
    """
    summary: dict[str, Any] = {"planned": 0, "applied": 0, "skipped": 0, "errors": []}

    if not TEMPLATES:
        logger.warning("No first-look templates to migrate.")
        return summary

    es = get_elasticsearch_client()
    if apply and es is None:
        summary["errors"].append("ES unavailable; cannot apply.")
        return summary

    for connector_id, source in TEMPLATES.items():
        doc = _build_template_doc(connector_id, source)
        summary["planned"] += 1
        logger.info(
            "%s: template_id=%r name=%r menu=%r placeholder_field=%r charts=%d",
            "WOULD WRITE" if not apply else "WRITING",
            doc["template_id"],
            doc["name"],
            doc["menu_title"],
            doc["placeholder_field"],
            len(doc["echarts_config"]["charts"]),
        )

        if not apply:
            continue

        try:
            es.index(
                index=TEMPLATES_INDEX,
                id=doc["template_id"],
                document=doc,
                refresh="wait_for",
            )
            summary["applied"] += 1
        except Exception as e:  # noqa: BLE001 — boundary
            logger.error("Write failed for %r: %s", doc["template_id"], e)
            summary["errors"].append(f"{doc['template_id']}: {e}")

    return summary


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Migrate the 4  first-look templates into de_dashboard_templates.",
    )
    parser.add_argument(
        "--apply",
        action="store_true",
        help="Actually write the docs. Default is dry-run.",
    )
    parser.add_argument(
        "-v",
        "--verbose",
        action="store_true",
        help="Enable DEBUG logging.",
    )
    args = parser.parse_args()

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s [%(levelname)s] %(message)s",
    )

    summary = migrate(apply=args.apply)

    # CLI summary via logger.info — matches other tools/ scripts
    # (account_type_backfill, backfill_*) for consistent stdout shape.
    logger.info("")
    logger.info("Migration summary:")
    logger.info("  Planned: %d", summary["planned"])
    logger.info("  Applied: %d", summary["applied"])
    logger.info("  Skipped: %d", summary["skipped"])
    if summary["errors"]:
        logger.info("  Errors:  %d", len(summary["errors"]))
        for err in summary["errors"]:
            logger.info("    - %s", err)
    if not args.apply:
        logger.info("")
        logger.info("(dry-run — re-run with --apply to write)")

    return 1 if summary["errors"] else 0


if __name__ == "__main__":
    sys.exit(main())
