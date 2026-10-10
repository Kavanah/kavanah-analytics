"""/ PHASE-106 — field-catalog fidelity tests.

The catalog (``bin/python/catalog/fb_ads.json``) is the single source of truth
that (a) generates the ES index schemas and (b) feeds the importer request lists.
v1 is a behavior-preserving refactor, so these tests pin BOTH outputs against the
historical baselines:

  - the importer request lists must equal the field sets that were hardcoded in
    ``get_insights.py`` / ``get_insights_window.py`` before the refactor
    (FROZEN_DAILY / FROZEN_WINDOW below — copied verbatim from those literals);
  - the generated schemas must equal the committed ``unified_schema_*.json``.

If the catalog drifts from either baseline, a real import would request a
different field set or build a different index mapping — exactly what this gate
exists to catch.

DELIBERATE earlier field additions update the baseline here, tagged with
their ticket — the gate catches ACCIDENTAL drift, not planned catalog changes:
  - video_thruplay_watched_actions
"""

import json
import os

import pytest

# -- frozen baselines (verbatim from the earlier hardcoded literals) ---
FROZEN_DAILY = {
    "account_currency",
    "buying_type",
    "canvas_avg_view_percent",
    "conversion_values",
    "conversion_rate_ranking",
    "conversions",
    "cost_per_action_type",
    "clicks",
    "date_start",
    "date_stop",
    "dda_results",
    "engagement_rate_ranking",
    "estimated_ad_recall_rate",
    "impressions",
    "inline_link_click_ctr",
    "video_30_sec_watched_actions",
    "total_postbacks_detailed_v4",
    "video_p25_watched_actions",
    "video_avg_time_watched_actions",
    "total_postbacks_detailed",
    "video_p75_watched_actions",
    "video_p100_watched_actions",
    "spend",
    "video_p50_watched_actions",
    "video_p95_watched_actions",
    "video_play_actions",
    "video_thruplay_watched_actions",  # deliberate addition, not earlier
    "website_ctr",
    "reach",
    "quality_ranking",
    "outbound_clicks",
    "canvas_avg_view_time",
    "attribution_setting",
    "cost_per_unique_outbound_click",
    "cost_per_unique_inline_link_click",
    "cost_per_unique_click",
    "cost_per_unique_action_type",
    "cost_per_thruplay",
    "cost_per_outbound_click",
    "cost_per_inline_post_engagement",
    "cpp",
    "cpm",
    "cpc",
    "estimated_ad_recallers",
    "cost_per_inline_link_click",
    "cost_per_estimated_ad_recallers",
    "frequency",
    "ctr",
    "converted_product_value",
    "converted_product_quantity",
    "cost_per_conversion",
    "actions",
    "action_values",
    "full_view_impressions",
    "full_view_reach",
    "inline_link_clicks",
    "inline_post_engagement",
    "objective",
    "outbound_clicks_ctr",
    "optimization_goal",
}
FROZEN_WINDOW = {
    "account_currency",
    "reach",
    "frequency",
    "impressions",
    "clicks",
    "spend",
    "cpc",
    "cpm",
    "cpp",
    "ctr",
    "inline_link_clicks",
    "inline_link_click_ctr",
    "inline_post_engagement",
    "cost_per_inline_link_click",
    "cost_per_unique_click",
    "estimated_ad_recallers",
    "estimated_ad_recall_rate",
    "full_view_reach",
    "objective",
    "optimization_goal",
    "buying_type",
    "actions",
    "cost_per_action_type",
}

_PY_ROOT = os.path.join(os.path.dirname(os.path.dirname(os.path.dirname(__file__))), "bin", "python")


def _committed_schema(name):
    with open(os.path.join(_PY_ROOT, "schemas", f"{name}.json"), "r", encoding="utf-8") as fh:
        return json.load(fh)


# ---- request-list fidelity (catalog -> importer fields=[]) ----


def test_request_fields_daily_matches_frozen_baseline():
    """request_fields('fb_ads','daily') equals the pre-refactor hardcoded daily list."""
    from migration.catalog_loader import request_fields, clear_cache

    clear_cache()
    assert set(request_fields("fb_ads", "daily")) == FROZEN_DAILY


def test_request_fields_window_matches_frozen_baseline():
    """request_fields('fb_ads','window') equals the pre-refactor hardcoded window list."""
    from migration.catalog_loader import request_fields, clear_cache

    clear_cache()
    assert set(request_fields("fb_ads", "window")) == FROZEN_WINDOW


def test_importer_modules_use_catalog_lists_equal_to_frozen():
    """The wired importer constants resolve to the frozen baselines (end-to-end)."""
    import import_scripts.facebook.get_insights as gi
    import import_scripts.facebook.get_insights_window as giw

    assert set(gi._DAILY_INSIGHT_FIELDS) == FROZEN_DAILY
    assert set(giw._INSIGHT_FIELDS) == FROZEN_WINDOW


# ---- schema-generation fidelity (catalog -> unified_schema_*.json) ----


def test_generated_daily_schema_matches_committed():
    """catalog_to_schema reproduces unified_schema_fb_insights.json exactly."""
    from tools.catalog_to_schema import load_catalog, generate_schema

    generated = generate_schema(load_catalog("fb_ads"), "daily")
    assert generated == _committed_schema("unified_schema_fb_insights")


def test_generated_window_schema_matches_committed():
    """catalog_to_schema reproduces unified_schema_fb_insights_window.json exactly."""
    from tools.catalog_to_schema import load_catalog, generate_schema

    generated = generate_schema(load_catalog("fb_ads"), "window")
    assert generated == _committed_schema("unified_schema_fb_insights_window")


def test_drift_check_passes_for_committed_catalog():
    """The --check guard reports no drift for the committed catalog+schemas."""
    from tools.catalog_to_schema import run

    assert run("fb_ads", check=True) == 0
