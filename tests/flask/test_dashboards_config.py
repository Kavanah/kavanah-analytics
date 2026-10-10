"""
Tests for dashboard configuration module.

Covers:
  get_dashboard_url      — URL generation with space, embed params
  get_enabled_dashboards — filtering by enabled flag
"""

import pytest

from asset_management.web.dashboards_config import (
    DASHBOARD_CONFIGS,
    get_dashboard_url,
    get_enabled_dashboards,
)


class TestGetDashboardUrl:

    def test_valid_key_returns_url(self):
        url = get_dashboard_url("fb_ads_overview")
        assert "/app/dashboards#/view/" in url
        assert "/s/" not in url
        assert "1efd66f1-c66d-4602-9170-0cd2e3ebba4b" in url

    def test_valid_key_includes_embed_params(self):
        url = get_dashboard_url("fb_ads_overview")
        assert "embed=true" in url
        assert "viewMode:view" in url

    def test_invalid_key_raises_value_error(self):
        with pytest.raises(ValueError, match="not found"):
            get_dashboard_url("nonexistent_dashboard")

    def test_default_space_omits_space_path(self):
        from unittest import mock
        original = DASHBOARD_CONFIGS.get("fb_ads_overview", {}).copy()
        test_config = {**original, "space": "default"}
        with mock.patch.dict(
            "asset_management.web.dashboards_config.DASHBOARD_CONFIGS",
            {"fb_ads_overview": test_config},
        ):
            url = get_dashboard_url("fb_ads_overview")
            assert "/s/" not in url
            assert "/app/dashboards#/view/" in url

    def test_no_space_key_omits_space_path(self):
        from unittest import mock
        original = DASHBOARD_CONFIGS.get("fb_ads_overview", {}).copy()
        test_config = {k: v for k, v in original.items() if k != "space"}
        with mock.patch.dict(
            "asset_management.web.dashboards_config.DASHBOARD_CONFIGS",
            {"fb_ads_overview": test_config},
        ):
            url = get_dashboard_url("fb_ads_overview")
            assert "/s/" not in url


class TestGetEnabledDashboards:

    def test_returns_enabled_dashboards(self):
        result = get_enabled_dashboards()
        for key, config in result.items():
            assert config.get("enabled", True) is True

    def test_filters_out_disabled(self):
        from unittest import mock
        with mock.patch.dict(
            "asset_management.web.dashboards_config.DASHBOARD_CONFIGS",
            {
                "enabled_dash": {"title": "Enabled", "dashboard_id": "1", "enabled": True},
                "disabled_dash": {"title": "Disabled", "dashboard_id": "2", "enabled": False},
            },
            clear=True,
        ):
            result = get_enabled_dashboards()
            assert "enabled_dash" in result
            assert "disabled_dash" not in result
