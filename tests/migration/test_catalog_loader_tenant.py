"""per-tenant field-picker resolution in the catalog loader.

``request_fields_for_tenant`` derives a tenant's API request list from the
catalog plus that workspace's sparse override doc; ``disabled_es_fields`` is the
doc-level filter for the action metrics the request can't exclude (they ride the
always-requested ``actions`` / ``cost_per_action_type`` parents).
"""

import pytest

from migration.catalog_loader import (
    clear_cache,
    disabled_es_fields,
    load_catalog,
    request_fields,
    request_fields_for_tenant,
)


@pytest.fixture(autouse=True)
def _fresh_cache():
    clear_cache()
    yield
    clear_cache()


def _entry(field_id):
    catalog = load_catalog("fb_ads")
    return next(e for e in catalog["fields"] if e["field_id"] == field_id)


# ---- parity: empty overrides is the behavior-preserving default ----


@pytest.mark.parametrize("dataset", ["daily", "window"])
def test_should_match_global_request_list_when_overrides_empty(dataset):
    assert request_fields_for_tenant("fb_ads", dataset, {}) == request_fields("fb_ads", dataset)


@pytest.mark.parametrize("dataset", ["daily", "window"])
def test_should_match_global_request_list_when_overrides_none(dataset):
    assert request_fields_for_tenant("fb_ads", dataset, None) == request_fields("fb_ads", dataset)


# ---- disable semantics ----


def test_should_drop_api_field_when_plain_metric_disabled():
    overrides = {"fb_ads.cost_per_inline_link_click": {"enabled": False}}
    fields = request_fields_for_tenant("fb_ads", "daily", overrides)
    assert "cost_per_inline_link_click" not in fields
    baseline = request_fields("fb_ads", "daily")
    assert set(baseline) - set(fields) == {"cost_per_inline_link_click"}


def test_should_keep_core_field_when_disable_attempted():
    overrides = {"fb_ads.spend": {"enabled": False}, "fb_ads.reach": {"enabled": False}}
    fields = request_fields_for_tenant("fb_ads", "daily", overrides)
    assert "spend" in fields
    assert "reach" in fields


def test_should_keep_actions_parent_when_one_action_metric_disabled():
    # actions rides the non-toggleable action_metric_parent entries.
    overrides = {"fb_ads.actions_link_click": {"enabled": False}}
    fields = request_fields_for_tenant("fb_ads", "daily", overrides)
    assert "actions" in fields


def test_should_reenable_field_when_override_enabled_true():
    overrides = {"fb_ads.cost_per_inline_link_click": {"enabled": True}}
    assert request_fields_for_tenant("fb_ads", "daily", overrides) == request_fields("fb_ads", "daily")


# ---- non-additive routing guard (AC: none -> window only) ----


def test_should_never_enable_nonadditive_field_into_daily_when_not_base_requested(monkeypatch):
    # Synthetic: a non-additive optional field with no requested_in legs may only
    # be enabled into the window dataset, regardless of what the API write says.
    import migration.catalog_loader as cl

    synthetic = {
        "fields": [
            {
                "field_id": "syn.reach_like",
                "origin": "insight",
                "api_field": "reach_like",
                "kind": "metric",
                "aggregation": "none",
                "requested": False,
                "requested_in": [],
                "mapped": True,
                "mapped_in": ["daily", "window"],
                "es_field": "insight_reach_like",
            }
        ]
    }
    monkeypatch.setitem(cl._CACHE, "syn", synthetic)
    overrides = {"syn.reach_like": {"enabled": True}}
    assert request_fields_for_tenant("syn", "daily", overrides) == []
    assert request_fields_for_tenant("syn", "window", overrides) == ["reach_like"]


# ---- doc-level filter ----


def test_should_return_es_fields_when_action_metrics_disabled():
    overrides = {
        "fb_ads.actions_link_click": {"enabled": False},
        "fb_ads.cost_per_action_type_link_click": {"enabled": False},
    }
    dropped = disabled_es_fields("fb_ads", overrides)
    assert dropped == {
        "insight_actions_link_click",
        "insight_cost_per_action_type_link_click",
    }


def test_should_ignore_core_and_unknown_fields_in_doc_filter():
    overrides = {
        "fb_ads.spend": {"enabled": False},  # core — never droppable
        "fb_ads.nonexistent": {"enabled": False},  # unknown — ignored
    }
    assert disabled_es_fields("fb_ads", overrides) == set()


def test_should_return_empty_doc_filter_when_no_overrides():
    assert disabled_es_fields("fb_ads", {}) == set()
    assert disabled_es_fields("fb_ads", None) == set()


# ---- catalog invariants the picker relies on ----


def test_should_mark_exactly_the_core_set_in_catalog():
    catalog = load_catalog("fb_ads")
    core = {e["field_id"] for e in catalog["fields"] if e.get("core")}
    assert core == {
        "fb_ads.spend",
        "fb_ads.impressions",
        "fb_ads.clicks",
        "fb_ads.reach",
        "fb_ads.frequency",
        "fb_ads.date_start",
        "fb_ads.date_stop",
        "fb_ads.account_currency",
    }


def test_should_have_mapped_kaf16_scalars_in_daily_schema():
    for fid in (
        "fb_ads.attribution_setting",
        "fb_ads.canvas_avg_view_percent",
        "fb_ads.canvas_avg_view_time",
        "fb_ads.cost_per_estimated_ad_recallers",
        "fb_ads.full_view_impressions",
    ):
        entry = _entry(fid)
        assert entry["mapped"] is True
        assert entry["mapped_in"] == ["daily"]
