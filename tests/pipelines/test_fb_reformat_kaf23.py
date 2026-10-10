"""reformat_insights keys list-valued fields by their own source name.

The old catch-all funneled every non-actions / non-cost_per_action_type list
into ``insight_actions_<action_type>``: cost_per_thruplay's COST overwrote the
insight_actions_video_view COUNT, the video percentile counts overwrote each
other, and conversion custom types leaked into the actions namespace. These
tests pin the new per-source keying and the preserved legacy behavior for the
two action families.
"""

from __future__ import annotations

import import_scripts.facebook.get_insights as gi


def test_should_keep_actions_family_keys_unchanged():
    row = {
        "actions": [
            {"action_type": "link_click", "value": "7"},
            {"action_type": "offsite_conversion.fb_pixel_purchase", "value": "2"},
        ],
        "cost_per_action_type": [{"action_type": "link_click", "value": "0.5"}],
    }
    doc = gi.reformat_insights(row)
    assert doc["insight_actions_link_click"] == 7.0
    assert doc["insight_actions_offsite_conversion_fb_pixel_purchase"] == 2.0
    assert doc["insight_cost_per_action_type_link_click"] == 0.5


def test_should_keep_video_type_breakdown_keys_unchanged():
    row = {"actions": [{"action_type": "video_view", "action_video_type": "total", "value": "11"}]}
    assert gi.reformat_insights(row)["insight_actions_video_view_total"] == 11.0


def test_should_key_other_list_fields_by_their_own_source_name():
    row = {
        "outbound_clicks": [{"action_type": "outbound_click", "value": "9"}],
        "website_ctr": [{"action_type": "link_click", "value": "1.2"}],
        "cost_per_thruplay": [{"action_type": "video_view", "value": "0.04"}],
        "video_p25_watched_actions": [{"action_type": "video_view", "value": "100"}],
        "conversions": [{"action_type": "offsite_conversion.custom.123", "value": "3"}],
    }
    doc = gi.reformat_insights(row)
    assert doc["insight_outbound_clicks_outbound_click"] == 9.0
    assert doc["insight_website_ctr_link_click"] == 1.2
    assert doc["insight_cost_per_thruplay_video_view"] == 0.04
    assert doc["insight_video_p25_watched_actions_video_view"] == 100.0
    assert doc["insight_conversions_offsite_conversion_custom_123"] == 3.0
    # None of them leak into the actions namespace anymore.
    assert "insight_actions_outbound_click" not in doc
    assert "insight_actions_video_view" not in doc
    assert "insight_actions_offsite_conversion_custom_123" not in doc


def test_should_not_let_cost_per_thruplay_overwrite_video_view_count():
    # The exact earlier corruption: whichever list flattened last won.
    row = {
        "actions": [{"action_type": "video_view", "value": "500"}],
        "cost_per_thruplay": [{"action_type": "video_view", "value": "0.04"}],
        "video_p25_watched_actions": [{"action_type": "video_view", "value": "400"}],
        "video_p100_watched_actions": [{"action_type": "video_view", "value": "120"}],
    }
    doc = gi.reformat_insights(row)
    assert doc["insight_actions_video_view"] == 500.0
    assert doc["insight_cost_per_thruplay_video_view"] == 0.04
    assert doc["insight_video_p25_watched_actions_video_view"] == 400.0
    assert doc["insight_video_p100_watched_actions_video_view"] == 120.0


def test_should_point_thruplay_cost_at_flattened_key():
    ad = {
        "adset_optimization_goal": "THRUPLAY",
        "insight_actions_video_view": 500.0,
        "insight_cost_per_thruplay_video_view": 0.04,
    }
    out = gi.compute_optimization_result(dict(ad))
    assert out["result_type"] == "THRUPLAY"
    assert out["result_count"] == 500.0
    assert out["cost_per_result"] == 0.04


def test_should_have_catalog_es_fields_matching_the_flattened_keys():
    # The catalog's es_field for each newly mapped list field must equal what
    # reformat_insights actually emits, or the mapping pins a dead key.
    # the video family's synthetic items now carry
    # action_video_type='total': that is how the API actually returns them
    # (the daily request breaks down by action_video_type), so the catalog
    # pins the _total forms. This test originally fed plain items and thereby
    # blessed the suffix-less pins that landed in ZERO real docs.
    from migration.catalog_loader import load_catalog, clear_cache

    clear_cache()
    catalog = load_catalog("fb_ads")
    by_api = {e["api_field"]: e for e in catalog["fields"] if e.get("api_field")}
    for api, action_type, video_type in [
        ("outbound_clicks", "outbound_click", None),
        ("outbound_clicks_ctr", "outbound_click", None),
        ("cost_per_outbound_click", "outbound_click", None),
        ("cost_per_unique_outbound_click", "outbound_click", None),
        ("website_ctr", "link_click", None),
        ("cost_per_thruplay", "video_view", None),
        ("video_play_actions", "video_view", "total"),
        ("video_30_sec_watched_actions", "video_view", "total"),
        ("video_avg_time_watched_actions", "video_view", "total"),
        ("video_p25_watched_actions", "video_view", "total"),
        ("video_p50_watched_actions", "video_view", "total"),
        ("video_p75_watched_actions", "video_view", "total"),
        ("video_p95_watched_actions", "video_view", "total"),
        ("video_p100_watched_actions", "video_view", "total"),
    ]:
        entry = by_api[api]
        item = {"action_type": action_type, "value": "1"}
        if video_type:
            item["action_video_type"] = video_type
        emitted = gi.reformat_insights({api: [item]})
        assert entry["mapped"] is True, api
        assert entry["es_field"] in emitted, f"{api}: catalog es_field {entry['es_field']} != emitted {list(emitted)}"
