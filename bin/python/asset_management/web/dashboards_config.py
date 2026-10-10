"""
Dashboard Configuration

This file is maintained only for:
- Dashboard URL generation (get_dashboard_url function)
- Platform-to-data-view mapping

Dashboard visibility for ECharts dashboards lives on the dashboard
document itself in ``de_asset_echarts_dashboards`` (managed via the
Dashboard Builder). removed the legacy Asset Manager "System
Configuration" surface that previously edited those visibility fields.
"""

import os

from asset_management.web.config import DEFAULT_KIBANA_URL

# Base Kibana URL from the environment; neutral compose-internal fallback
KIBANA_URL = os.environ.get("KIBANA_URL", DEFAULT_KIBANA_URL)

# Dashboard definitions
DASHBOARD_CONFIGS = {
    "fb_ads_overview": {
        "title": "Facebook Ads Overview",
        "dashboard_id": "1efd66f1-c66d-4602-9170-0cd2e3ebba4b",
        "space": "default",
        "description": "Overview of Facebook advertising performance",
        "icon": "📊",
        "category": "facebook",
        "enabled": True,
    },
    # Future dashboards can be added here:
    # 'google_ads_overview': {
    #     'title': 'Google Ads Overview',
    #     'dashboard_id': 'another-dashboard-id',
    #     ...
    # },
}

# Category definitions for grouping dashboards (future use)
DASHBOARD_CATEGORIES = {
    "facebook": {"title": "Facebook Advertising", "icon": "📘", "order": 1},
    "google": {"title": "Google Advertising", "icon": "🔍", "order": 2},
    "echo": {"title": "Echo Messaging", "icon": "💬", "order": 3},
}


def get_dashboard_url(dashboard_key):
    """Generate full Kibana URL for a dashboard.

    Args:
        dashboard_key: Key from DASHBOARD_CONFIGS

    Returns:
        str: Full URL to embed dashboard
    """
    if dashboard_key not in DASHBOARD_CONFIGS:
        raise ValueError(f"Dashboard '{dashboard_key}' not found")

    config = DASHBOARD_CONFIGS[dashboard_key]
    dashboard_id = config["dashboard_id"]
    space = config.get("space", "default")

    # Build URL based on space
    if space and space != "default":
        url = f"{KIBANA_URL}/s/{space}/app/dashboards#/view/{dashboard_id}"
    else:
        url = f"{KIBANA_URL}/app/dashboards#/view/{dashboard_id}"

    # Add embed parameter to hide Kibana chrome, enable auto-refresh, set time range, and force view mode
    # viewMode:view hides edit controls, hide-filter-bar=true removes filter bar, show-time-filter=true keeps time picker
    url += "?embed=true&_g=(refreshInterval:(pause:!f,value:300000),time:(from:now-30d/d,to:now))&_a=(viewMode:view)&hide-filter-bar=true&show-time-filter=true"

    return url


def get_enabled_dashboards():
    """Get list of enabled dashboards.

    Returns:
        dict: Enabled dashboards only
    """
    return {k: v for k, v in DASHBOARD_CONFIGS.items() if v.get("enabled", True)}
