#!/usr/bin/env python3
"""
Generate anonymized demo data for any team.

Creates randomized documents per data source and indexes them into the
team-specific Elasticsearch indices. All numeric fields have values > 0.
Geographic fields use randomized real-world locations.

Usage:
    # Seed DemoTeam (default, backwards-compatible — random dates)
    python3 /app/python/import_scripts/migration/generate_demo_data.py

    # Seed a specific team/org (random dates, single pass)
    python3 /app/python/import_scripts/migration/generate_demo_data.py --team "Kavanah Team" --org "Kavanah"

    # Backfill 90 days of historical data
    python3 /app/python/import_scripts/migration/generate_demo_data.py --team "Kavanah Team" --org "Kavanah" --days 90

    # Generate data for today only (use in daily cron)
    python3 /app/python/import_scripts/migration/generate_demo_data.py --team "Kavanah Team" --org "Kavanah" --days 1

    # Generate data for a specific date
    python3 /app/python/import_scripts/migration/generate_demo_data.py --team "Kavanah Team" --org "Kavanah" --target-date 2026-03-15

Disciple Tools is NOT generated here. The push→pull cutover
removed the staging→promote pipeline this script used to feed; demo DT data is
now maintained by the daily ``scripts/seed_kavanah_demo_dt.py --reseed`` cron
(exact curated totals, sliding 90-day window). ``--dt-days`` is accepted but
ignored so earlier cron lines don't break.

If the team does not exist in de_asset_teams, it is auto-created.
Run from inside the imports_container or any environment with ES access.
"""

import argparse
import calendar
import os
import sys
import json
import random
import logging
from datetime import date, datetime, timedelta

# Add python root to path (two levels up from import_scripts/migration/)
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "../..")))

from lib.es_helpers import (
    get_elasticsearch_client,
    create_index_if_not_exists,
    put_regular_index_template,
    build_data_index_name,
)

logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(levelname)s - %(message)s")
logger = logging.getLogger(__name__)

# ─── Configuration ────────────────────────────────────────────────────────────

# Real-world locations with coordinates for geographic fields
DEMO_LOCATIONS = [
    {"city": "New York", "country": "United States", "iso3": "USA", "iso2": "US", "lat": 40.7128, "lon": -74.0060},
    {"city": "London", "country": "United Kingdom", "iso3": "GBR", "iso2": "GB", "lat": 51.5074, "lon": -0.1278},
    {"city": "Tokyo", "country": "Japan", "iso3": "JPN", "iso2": "JP", "lat": 35.6762, "lon": 139.6503},
    {"city": "Sydney", "country": "Australia", "iso3": "AUS", "iso2": "AU", "lat": -33.8688, "lon": 151.2093},
    {"city": "Berlin", "country": "Germany", "iso3": "DEU", "iso2": "DE", "lat": 52.5200, "lon": 13.4050},
    {"city": "Sao Paulo", "country": "Brazil", "iso3": "BRA", "iso2": "BR", "lat": -23.5505, "lon": -46.6333},
    {"city": "Lagos", "country": "Nigeria", "iso3": "NGA", "iso2": "NG", "lat": 6.5244, "lon": 3.3792},
    {"city": "Mumbai", "country": "India", "iso3": "IND", "iso2": "IN", "lat": 19.0760, "lon": 72.8777},
    {"city": "Dubai", "country": "United Arab Emirates", "iso3": "ARE", "iso2": "AE", "lat": 25.2048, "lon": 55.2708},
    {"city": "Toronto", "country": "Canada", "iso3": "CAN", "iso2": "CA", "lat": 43.6532, "lon": -79.3832},
    {"city": "Nairobi", "country": "Kenya", "iso3": "KEN", "iso2": "KE", "lat": -1.2921, "lon": 36.8219},
    {"city": "Seoul", "country": "South Korea", "iso3": "KOR", "iso2": "KR", "lat": 37.5665, "lon": 126.9780},
    {"city": "Mexico City", "country": "Mexico", "iso3": "MEX", "iso2": "MX", "lat": 19.4326, "lon": -99.1332},
    {"city": "Jakarta", "country": "Indonesia", "iso3": "IDN", "iso2": "ID", "lat": -6.2088, "lon": 106.8456},
    {"city": "Cairo", "country": "Egypt", "iso3": "EGY", "iso2": "EG", "lat": 30.0444, "lon": 31.2357},
]

# Country centroids for location_countries field (matches geocoding.py _COUNTRY_CENTROIDS)
_COUNTRY_CENTROIDS = {
    "US": {"lat": 37.09, "lon": -95.71},
    "GB": {"lat": 55.38, "lon": -3.44},
    "JP": {"lat": 36.20, "lon": 138.25},
    "AU": {"lat": -25.27, "lon": 133.78},
    "DE": {"lat": 52.52, "lon": 13.41},
    "BR": {"lat": -14.24, "lon": -51.93},
    "NG": {"lat": 9.08, "lon": 8.68},
    "IN": {"lat": 20.59, "lon": 78.96},
    "AE": {"lat": 23.42, "lon": 53.85},
    "CA": {"lat": 56.13, "lon": -106.35},
    "KE": {"lat": -0.02, "lon": 37.91},
    "KR": {"lat": 35.91, "lon": 127.77},
    "MX": {"lat": 23.63, "lon": -102.55},
    "ID": {"lat": -0.79, "lon": 113.92},
    "EG": {"lat": 26.82, "lon": 30.80},
}

# ─── Helpers ──────────────────────────────────────────────────────────────────


def ri(lo, hi):
    """Random integer in [lo, hi]."""
    return random.randint(lo, hi)


def rf(lo, hi, dp=2):
    """Random float in [lo, hi] rounded to dp decimal places."""
    return round(random.uniform(lo, hi), dp)


def rand_loc():
    return random.choice(DEMO_LOCATIONS)


def date_str(days_ago=None, target_date=None):
    """yyyy-MM-dd string for a recent date.

    If target_date (a datetime.date) is provided, return it directly.
    Otherwise fall back to the original random behavior.
    """
    if target_date is not None:
        return target_date.strftime("%Y-%m-%d")
    offset = days_ago if days_ago is not None else ri(0, 30)
    return (datetime.utcnow() - timedelta(days=offset)).strftime("%Y-%m-%d")


def _epoch_seconds(dt):
    """Return a naive-UTC datetime as integer epoch seconds (ES ``epoch_second``)."""
    return calendar.timegm(dt.utctimetuple())


def now_iso(target_date=None):
    """ISO-8601 timestamp.

    If target_date is provided, return a timestamp on that date at a
    randomised hour/minute so documents spread naturally within the day.
    """
    if target_date is not None:
        dt = datetime(target_date.year, target_date.month, target_date.day, ri(6, 22), ri(0, 59), ri(0, 59))
        return dt.isoformat() + "Z"
    return datetime.utcnow().isoformat() + "Z"


def echo_fmt(dt=None, target_date=None):
    """Date in Echo format: 'January 15, 2024 10:30:00 UTC'

    If target_date is provided and dt is None, generate a random
    datetime on that date instead of a random recent date.
    """
    if dt is None:
        if target_date is not None:
            dt = datetime(target_date.year, target_date.month, target_date.day, ri(6, 22), ri(0, 59), ri(0, 59))
        else:
            dt = datetime.utcnow() - timedelta(days=ri(0, 30), hours=ri(0, 23))
    return dt.strftime("%B %d, %Y %H:%M:%S UTC")


def org_slug(team):
    """Derive a short lowercase slug from the org title for use in usernames, URLs, etc."""
    org = team.get("organization_title", "demo")
    if isinstance(org, list):
        org = org[0] if org else "demo"
    return str(org).lower().replace(" ", "")


def sanitize(team_title):
    s = team_title.lower().replace(" ", "_").replace("-", "_")
    s = "".join(c if c.isalnum() or c == "_" else "_" for c in s)
    while "__" in s:
        s = s.replace("__", "_")
    return s


def ensure_org_exists(es, org_title):
    """Ensure an organization document exists in de_asset_organization.

    Idempotent — does nothing if the org already exists.
    """
    try:
        resp = es.search(
            index="de_asset_organization",
            body={"query": {"match": {"organization_title": org_title}}, "size": 5},
        )
        exact = next(
            (h for h in resp["hits"]["hits"] if h["_source"].get("organization_title") == org_title),
            None,
        )
        if exact:
            logger.info(f"Org '{org_title}' already exists in de_asset_organization.")
            return
        logger.warning(f"Org '{org_title}' not found in de_asset_organization. Auto-creating.")
        es.index(
            index="de_asset_organization",
            document={"@timestamp": now_iso(), "organization_title": org_title},
            refresh="wait_for",
        )
        logger.info(f"Auto-created org '{org_title}'.")
    except Exception as e:
        logger.error(f"Error ensuring org '{org_title}' exists: {e}")


def find_team(es, team_title, org_title):
    """Search de_asset_teams for the given team.

    Returns the source document enriched with the ES _id as team_esid.
    If no matching team is found, auto-creates a minimal team document
    and returns it so the script can seed any team without manual setup.
    """
    try:
        resp = es.search(
            index="de_asset_teams",
            body={"query": {"match": {"team_title": team_title}}, "size": 5},
        )
        hits = resp["hits"]["hits"]
        if hits:
            # workspaces allow duplicate team_title across tenants
            # (a foreign "Kavanah Team" has existed), so disambiguate
            # exact-title hits by organization_title before falling back.
            exact = [h for h in hits if h["_source"].get("team_title") == team_title]

            def _org_matches(src):
                org = src.get("organization_title")
                orgs = org if isinstance(org, list) else [org]
                return org_title in orgs

            hit = next((h for h in exact if _org_matches(h["_source"])), None)
            if hit is None:
                if len(exact) > 1:
                    logger.warning(
                        f"Multiple teams titled '{team_title}' and none match org "
                        f"'{org_title}' — using the first hit ({exact[0]['_id']}). "
                        "Verify this is the intended team."
                    )
                hit = exact[0] if exact else hits[0]
            team = hit["_source"]
            # Backfill fields that may be absent on newly-created teams
            if not team.get("team_esid"):
                team["team_esid"] = hit["_id"]
            if not team.get("team_country"):
                team["team_country"] = "United States"
            if not team.get("organization_title"):
                team["organization_title"] = org_title
            if not team.get("team_site_url"):
                team["team_site_url"] = "https://demo.example.com"
            if not team.get("team_country_iso3_code"):
                team["team_country_iso3_code"] = "USA"
            return team

        # Team not found — auto-create a minimal asset document
        logger.warning(f"Team '{team_title}' not found in de_asset_teams. Auto-creating.")
        doc = {
            "@timestamp": now_iso(),
            "team_title": team_title,
            "organization_title": org_title,
            "team_country": "United States",
            "team_city": "New York",
            "team_region": "North America",
            "team_site_url": "https://demo.example.com",
            "team_country_iso3_code": "USA",
        }
        result = es.index(index="de_asset_teams", document=doc, refresh="wait_for")
        doc["team_esid"] = result["_id"]
        logger.info(f"Auto-created team '{team_title}' with esid: {doc['team_esid']}")
        return doc
    except Exception as e:
        logger.error(f"Error finding/creating team '{team_title}': {e}")
        return None


def index_doc(es, index_name, mapping, doc, doc_id=None):
    """Create index if needed and index the document."""
    # a failed create must not be followed by the write: the write
    # would auto-create the index under dynamic mapping (the drift).
    if not create_index_if_not_exists(es, index_name, mapping):
        raise RuntimeError(f"failed to create {index_name} with its schema mapping — refusing to write into it")
    kwargs = dict(index=index_name, document=doc, refresh="wait_for")
    if doc_id:
        kwargs["id"] = doc_id
    es.index(**kwargs)
    logger.info(f"  ✓ Indexed document into {index_name}")


# ─── Index Mappings ───────────────────────────────────────────────────────────


def load_schema_properties(schema_name):
    """Load schema JSON from schemas/ dir and return the properties mapping dict."""
    schemas_dir = os.path.join(os.path.dirname(__file__), "..", "..", "schemas")
    path = os.path.join(schemas_dir, f"{schema_name}.json")
    with open(path) as f:
        schema = json.load(f)
    # Return {"properties": {...}} for create_index_if_not_exists
    if "properties" in schema:
        return schema
    if "mappings" in schema:
        return schema["mappings"]
    return schema


# ─── Document Generators ──────────────────────────────────────────────────────


def _org_name(team):
    """Extract organization title as a plain string (handles list values)."""
    org = team.get("organization_title", "Demo")
    if isinstance(org, list):
        org = org[0] if org else "Demo"
    return str(org)


# Ad configurations: (ad_name_suffix, campaign_name, adset_city, objective, optimization_goal)
_FB_AD_CONFIGS = [
    ("Hope for Today App Install Lagos", "Hope for Today App Install", "Lagos 18-45", "APP_INSTALLS", "APP_INSTALLS"),
    ("Hope for Today App Install Nairobi", "Hope for Today App Install", "Nairobi 18-45", "APP_INSTALLS", "APP_INSTALLS"),
    ("Hope for Today App Install Mumbai", "Hope for Today App Install", "Mumbai 18-45", "APP_INSTALLS", "APP_INSTALLS"),
    ("Hope for Today App Install Jakarta", "Hope for Today App Install", "Jakarta 18-45", "APP_INSTALLS", "APP_INSTALLS"),
    ("Easter Story Video Views Cairo", "Easter Story Video Views", "Cairo 18-55", "VIDEO_VIEWS", "THRUPLAY"),
    ("Easter Story Video Views Sao Paulo", "Easter Story Video Views", "Sao Paulo 18-55", "VIDEO_VIEWS", "THRUPLAY"),
    ("Easter Story Video Views Mexico City", "Easter Story Video Views", "Mexico City 18-55", "VIDEO_VIEWS", "THRUPLAY"),
    ("Prayer & Community Messaging Seoul", "Prayer & Community Messaging", "Seoul 18-40", "MESSAGES", "CONVERSATIONS"),
    ("Prayer & Community Messaging Berlin", "Prayer & Community Messaging", "Berlin 18-40", "MESSAGES", "CONVERSATIONS"),
    ("Prayer & Community Messaging London", "Prayer & Community Messaging", "London 18-40", "MESSAGES", "CONVERSATIONS"),
]


def gen_fb_insights(team, target_date=None):
    impressions = ri(10000, 150000)
    clicks = ri(200, max(201, int(impressions * 0.04)))
    spend = rf(15, 150)
    reach = ri(int(impressions * 0.65), impressions)
    locs = random.sample(DEMO_LOCATIONS, k=min(ri(5, 8), len(DEMO_LOCATIONS)))
    ds = date_str(target_date=target_date)
    org = _org_name(team)
    ad_suffix, campaign_name, adset_city, objective, opt_goal = random.choice(_FB_AD_CONFIGS)

    return {
        "@timestamp": now_iso(target_date),
        "kibana_date": ds,
        "insight_date_start": ds,
        "insight_date_stop": ds,
        "ad_account_id": f"act_{ri(100000000, 999999999)}",
        "ad_id": str(ri(100000000000, 999999999999)),
        "ad_name": f"{org} {ad_suffix}",
        "ad_status": "ACTIVE",
        "ad_created": f"{ds}T{ri(6, 22):02d}:{ri(0, 59):02d}:00+0000",
        "ad_campaign_id": str(ri(100000000000, 999999999999)),
        "ad_campaign_name": f"{org} {campaign_name}",
        "adset_id": str(ri(100000000000, 999999999999)),
        "adset_name": f"{org} Ad Set - {adset_city}",
        "adset_optimization_goal": opt_goal,
        "adset_billing_event": "IMPRESSIONS",
        "adset_bid_strategy": "LOWEST_COST_WITHOUT_CAP",
        "creative_id": str(ri(100000000000, 999999999999)),
        "creative_title": "Download Our App Today",
        "creative_body": "Connect with your community. Download now and stay informed.",
        "insight_account_currency": "USD",
        "insight_impressions": impressions,
        "insight_clicks": clicks,
        "insight_reach": reach,
        "insight_spend": spend,
        "insight_frequency": rf(1.1, 3.5),
        "insight_cpc": round(spend / clicks, 2),
        "insight_cpm": round(spend / impressions * 1000, 2),
        "insight_cpp": round(spend / reach * 1000, 2),
        "insight_ctr": round(clicks / impressions * 100, 4),
        "insight_inline_link_clicks": ri(150, clicks),
        "insight_inline_link_click_ctr": rf(0.5, 3.0),
        "insight_inline_post_engagement": ri(clicks, clicks * 3),
        "insight_actions_link_click": rf(100, clicks),
        "insight_actions_page_engagement": rf(200, 6000),
        "insight_actions_post_engagement": rf(300, 9000),
        "insight_actions_post_reaction": rf(50, 2000),
        "insight_actions_video_view": rf(1000, 30000),
        "insight_actions_landing_page_view": rf(80, 1500),
        "insight_actions_onsite_conversion_messaging_conversation_started_7d": rf(10, 250),
        "insight_actions_onsite_conversion_lead_grouped": rf(8, 180),
        "insight_actions_lead": rf(5, 120),
        "insight_actions_video_view_total": ri(1000, 40000),
        "insight_video_p25_watched_actions_video_view_total": ri(800, 30000),
        "insight_video_p50_watched_actions_video_view_total": ri(500, 20000),
        "insight_video_p75_watched_actions_video_view_total": ri(300, 12000),
        "insight_video_p95_watched_actions_video_view_total": ri(100, 5000),
        "insight_actions_comment": ri(5, 300),
        "insight_actions_onsite_conversion_post_save": ri(3, 150),
        "insight_actions_like": ri(20, 1500),
        "insight_actions_offsite_conversion_fb_pixel_purchase": rf(3, 60),
        "insight_buying_type": "AUCTION",
        "insight_objective": objective,
        "insight_optimization_goal": opt_goal,
        "insight_quality_ranking": "ABOVE_AVERAGE",
        "insight_engagement_rate_ranking": "AVERAGE",
        "insight_conversion_rate_ranking": "ABOVE_AVERAGE",
        "insight_cost_per_inline_link_click": rf(0.3, 2.5),
        "insight_cost_per_inline_post_engagement": rf(0.05, 0.5),
        "insight_cost_per_unique_click": rf(0.5, 3.0),
        "insight_cost_per_unique_inline_link_click": rf(0.5, 3.0),
        "insight_cost_per_action_type_link_click": rf(0.3, 2.5),
        "insight_cost_per_action_type_onsite_conversion_messaging_conversation_started_7d": rf(3, 25),
        "insight_cost_per_action_type_landing_page_view": rf(0.5, 4.0),
        "insight_cost_per_action_type_post_engagement": rf(0.05, 0.6),
        "insight_cost_per_action_type_onsite_conversion_lead_grouped": rf(5, 35),
        "insight_cost_per_action_type_offsite_conversion_fb_pixel_purchase": rf(20, 120),
        "page_id": str(ri(100000000000, 999999999999)),
        "page_name": org,
        "page_username": org_slug(team),
        "page_fan_count": ri(5000, 80000),
        "page_followers_count": ri(5000, 90000),
        "page_new_like_count": ri(20, 600),
        "page_engagement": ri(100, 3000),
        "page_is_published": True,
        "page_is_permanently_closed": False,
        "page_is_unclaimed": False,
        "page_talking_about_count": ri(20, 700),
        "page_verification_status": "blue_verified",
        "page_link": f"https://www.facebook.com/{org_slug(team)}",
        "result_type": "app_installs",
        "result_count": rf(30, 400),
        "result_label": "App Installs",
        "cost_per_result": rf(2, 30),
        "cost_result_conversations": rf(5, 60),
        "cost_result_link_clicks": rf(0.3, 2.5),
        "location_cities": [
            {
                "name": loc["city"],
                "coordinates": {
                    "lat": round(loc["lat"] + rf(-0.3, 0.3, 4), 4),
                    "lon": round(loc["lon"] + rf(-0.3, 0.3, 4), 4),
                },
            }
            for loc in locs
        ],
        "location_countries": [
            {
                "name": loc["iso2"],
                "iso2": loc["iso2"],
                "coordinates": _COUNTRY_CENTROIDS[loc["iso2"]],
            }
            for loc in locs
        ],
        "team_title": team["team_title"],
        "team_esid": team["team_esid"],
        "team_country": team.get("team_country", "United States"),
        "team_city": team.get("team_city", "New York"),
        "team_site_url": team.get("team_site_url", "https://demo.example.com"),
        "organization_title": team.get("organization_title", "Demo Organization"),
    }


def gen_fb_page_insights(team, target_date=None):
    ds = date_str(target_date=target_date)
    return {
        "@timestamp": now_iso(target_date),
        "kibana_date": ds,
        "fb_page_id": str(ri(100000000000, 999999999999)),
        "fb_page_name": team.get("organization_title", "Demo"),
        "fb_page_username": org_slug(team),
        "page_impressions_unique": ri(5000, 100000),
        "page_follows": ri(500, 8000),
        "page_posts_shares_total": ri(600, 12000),
        "page_positive_feedback_by_type_comment": ri(20, 500),
        "page_actions_post_reactions_total": ri(200, 4000),
        "page_positive_feedback_by_type_link": ri(10, 200),
        "page_positive_feedback_by_type_other": ri(5, 150),
        "page_messages_total_messaging_connections": ri(200, 5000),
        "page_messages_new_conversations_unique": ri(15, 300),
        "page_post_engagements": ri(500, 15000),
        # bare page_posts_impressions is invalid on Graph v25.0; real
        # docs now carry the _organic replacement, so demo docs match.
        "page_posts_impressions_organic": ri(5000, 200000),
        "page_views_total": ri(1000, 50000),
        "page_actions_post_reactions_total_like": ri(100, 5000),
        "page_actions_post_reactions_total_love": ri(20, 1500),
        "page_video_views_15s_total": ri(1000, 40000),
        "page_video_avg_sec_watched": rf(8, 90),
        "comments_replied_to": ri(10, 150),
        "team_title": team["team_title"],
        "team_esid": team["team_esid"],
        "team_country": team.get("team_country", "United States"),
        "team_city": team.get("team_city", "New York"),
        "team_site_url": team.get("team_site_url", "https://demo.example.com"),
        "organization_title": team.get("organization_title", "Demo Organization"),
    }


def gen_google_ads(team, target_date=None):
    ds = date_str(target_date=target_date)
    impressions = ri(20000, 600000)
    clicks = ri(500, max(501, int(impressions * 0.04)))
    cost = rf(20, 200)
    conversions = rf(30, 600)
    app_installs = ri(20, max(21, int(conversions * 0.8)))

    return {
        "@timestamp": now_iso(target_date),
        "kibana_date": ds,
        "date": ds,
        "campaign_id": str(ri(1000000000, 9999999999)),
        "campaign_name": f"{team.get('organization_title', 'Demo')} App Campaign - Android",
        "campaign_channel_type": "MULTI_CHANNEL",
        "campaign_status": "ENABLED",
        "app_id": f"com.{org_slug(team)}.app.{ri(1000, 9999)}",
        "app_store": "GOOGLE_APP_STORE",
        "ad_group_id": str(ri(100000000000, 999999999999)),
        "ad_group_name": f"{team.get('organization_title', 'Demo')} Ad Group - Text Ads",
        "ad_group_status": "ENABLED",
        "ad_id": str(ri(100000000000, 999999999999)),
        "ad_name": f"{team.get('organization_title', 'Demo')} App Ad - Primary",
        "impressions": impressions,
        "clicks": clicks,
        "cost_micros": int(cost * 1000000),
        "cost": cost,
        "conversions": conversions,
        "all_conversions": round(conversions * rf(1.1, 1.5), 2),
        "ctr": round(clicks / impressions * 100, 4),
        "app_installs": app_installs,
        "cost_per_install": round(cost / app_installs, 2),
        "conversion_rate": round(conversions / clicks * 100, 4),
        "cost_per_conversion": round(cost / conversions, 2),
        "google_ads_customer_id": str(ri(1000000000, 9999999999)),
        "google_ads_customer_esid": f"gads_{ri(10000, 99999)}",
        "google_ads_customer_title": f"{team.get('organization_title', 'Demo')} Google Ads Account",
        "team_esid": team["team_esid"],
        "team_title": team["team_title"],
        "team_url": team.get("team_site_url", "https://demo.example.com"),
        "team_country": team.get("team_country", "United States"),
        "team_city": team.get("team_city", "New York"),
        "organization_title": team.get("organization_title", "Demo Organization"),
    }


def gen_google_ads_geo(team, target_date=None):
    """Generate per-country Google Ads geo breakdown documents.

    Returns a list of record_type='geo' documents that the map query
    (_query_google_ads_installs_geo_map) aggregates by geo_country_name.
    Each document represents installs in one country for one campaign.
    """
    ds = date_str(target_date=target_date)
    campaign_id = str(ri(1000000000, 9999999999))
    campaign_name = f"{team.get('organization_title', 'Demo')} App Campaign - Android"
    customer_id = str(ri(1000000000, 9999999999))

    docs = []
    # Pick 5-8 random countries for this day's geo breakdown
    countries = random.sample(DEMO_LOCATIONS, k=min(ri(5, 8), len(DEMO_LOCATIONS)))
    for loc in countries:
        installs = ri(5, 200)
        impressions = ri(installs * 50, installs * 200)
        cost = rf(5, 50)
        docs.append(
            {
                "@timestamp": now_iso(target_date),
                "kibana_date": ds,
                "date": ds,
                "record_type": "geo",
                "campaign_id": campaign_id,
                "campaign_name": campaign_name,
                "geo_criterion_id": ri(2000, 2999),
                "geo_country_name": loc["country"],
                "geo_country_code": loc["iso2"],
                "location": _COUNTRY_CENTROIDS[loc["iso2"]],
                "app_installs": installs,
                "impressions": impressions,
                "cost_micros": int(cost * 1000000),
                "cost": cost,
                "google_ads_customer_id": customer_id,
                "google_ads_customer_esid": f"gads_{ri(10000, 99999)}",
                "google_ads_customer_title": f"{team.get('organization_title', 'Demo')} Google Ads Account",
                "team_esid": team["team_esid"],
                "team_title": team["team_title"],
                "team_url": team.get("team_site_url", "https://demo.example.com"),
                "team_country": team.get("team_country", "United States"),
                "team_city": team.get("team_city", "New York"),
                "organization_title": team.get("organization_title", "Demo Organization"),
            }
        )
    return docs


def gen_ga_metrics(team, target_date=None):
    """Generate one GA4 document per platform (web, iOS, Android).

    Returns a list of documents so every dashboard filter has matching data.
    """
    pages = [
        ("Home", "/"),
        ("About Us", "/about"),
        ("Contact Us", "/contact"),
        ("Sign Up", "/signup"),
        ("Download App", "/download"),
        ("Features", "/features"),
        ("Blog", "/blog"),
    ]
    docs = []
    for platform in ("web", "iOS", "Android"):
        loc = rand_loc()
        ds = date_str(target_date=target_date)
        page_views = ri(100, 8000)
        users = ri(50, max(51, int(page_views * 0.7)))
        page_title, page_path = random.choice(pages)

        docs.append(
            {
                "@timestamp": now_iso(target_date),
                "kibana_date": ds,
                "date": ds.replace("-", ""),
                "property_id": str(ri(100000000, 999999999)),
                "property_name": f"{team.get('organization_title', 'Demo')} Website Analytics",
                "pageTitle": page_title,
                "pagePath": page_path,
                "sessionSource": random.choice(["google", "facebook.com", "instagram.com", "youtube.com", "twitter.com"]),
                "sessionMedium": random.choice(["organic", "cpc", "social", "referral", "email"]),
                "country": loc["country"],
                "city": loc["city"],
                "platform": platform,
                "screenPageViews": page_views,
                "scrolledUsers": ri(30, max(31, int(users * 0.6))),
                "activeUsers": users,
                "userEngagementDuration": rf(45, 900),
                "eventCount": ri(200, 20000),
                "sessions": ri(60, max(61, int(users * 1.2))),
                "totalUsers": users,
                "engagedSessions": ri(40, max(41, int(users * 0.8))),
                "team_esid": team["team_esid"],
                "team_title": team["team_title"],
                "organization_title": team.get("organization_title", "Demo Organization"),
            }
        )
    return docs


def gen_echo_conversations(team, target_date=None):
    """Generate multiple Echo conversation documents.

    Returns a list so Outcomes/Messaging Analytics dashboards show
    realistic aggregated values instead of 1.
    """
    names = [
        "Alex Johnson",
        "Maria Garcia",
        "James Lee",
        "Sarah Williams",
        "Ahmed Hassan",
        "Priya Sharma",
    ]
    topics = [
        "Prayer Request",
        "Seeking Truth",
        "Faith Questions",
        "Community Support",
        "General Inquiry",
        "Spiritual Guidance",
    ]
    outcomes = ["spiritual_conversation", "follow_up", "prayer_request", "information"]
    users = ["volunteer_01", "volunteer_02", "agent_01", "agent_02"]
    tags_list = ["follow-up", "gospel", "prayer", "new-believer"]
    site_url = team.get("team_site_url", f"https://{org_slug(team)}.example.com")

    docs = []
    for _ in range(ri(8, 15)):
        if target_date is not None:
            created_dt = datetime(target_date.year, target_date.month, target_date.day, ri(6, 22), ri(0, 59), ri(0, 59))
        else:
            created_dt = datetime.utcnow() - timedelta(days=ri(0, 30), hours=ri(0, 23))
        closed_dt = created_dt + timedelta(minutes=ri(5, 90))
        user_name = random.choice(users)

        docs.append(
            {
                "@timestamp": now_iso(target_date),
                # unified_schema_echo_conversations maps kibana_date as
                # date format yyyy-MM-dd — the Echo long-form string is
                # rejected by schema-created indices (Kavanah demo outage,
                # 2026-07-13).
                "kibana_date": created_dt.strftime("%Y-%m-%d"),
                "created_at": echo_fmt(created_dt),
                "closed_at": echo_fmt(closed_dt),
                "expiration": echo_fmt(closed_dt + timedelta(days=30)),
                "id": f"{org_slug(team)}_{ri(100000, 999999)}",
                "client_id": ri(100000, 999999),
                "chat_id": ri(100000, 999999),
                "type": random.choice(["chat", "sms", "email"]),
                "topic": random.choice(topics),
                "name": random.choice(names),
                "user": user_name,
                "claimed_by_user_name": user_name,
                "claimed_by_user_id": str(ri(1000, 9999)),
                "duration": f"{ri(5, 60)} minutes",
                "referrer": site_url,
                "client_contact": f"client_{ri(1000, 9999)}@example.com",
                "outcome": random.choice(outcomes),
                "spiritual_conversation": ri(1, 5),
                "multiplier": ri(1, 8),
                "no_response": ri(0, 3),
                "language_barrier": ri(0, 2),
                "Other": ri(0, 3),
                "incomingMessagesCount": ri(3, 25),
                "outboundMessagesCount": ri(3, 25),
                "link": f"https://echo.example.com/conversations/{org_slug(team)}_{ri(100000, 999999)}",
                "status": random.choice(["closed", "open", "pending"]),
                "flag": "none",
                "tags": random.choice(tags_list),
                "notes": f"{team.get('organization_title', 'Demo')} conversation for testing purposes.",
                "country": (echo_loc := rand_loc())["country"],
                "contact_country_iso2": echo_loc["iso2"],
                "echo_source": "GraphQL API",
                "team_country": team.get("team_country", "United States"),
                "team_country_iso3_code": team.get("team_country_iso3_code", "USA"),
                "team_city": team.get("team_city", "New York"),
                "team_site_url": site_url,
                "team_title": team["team_title"],
                "team_esid": team["team_esid"],
                "organization_title": team.get("organization_title", ""),
            }
        )
    return docs


def gen_ig_insights(team, target_date=None):
    ds = date_str(target_date=target_date)
    followers = ri(3000, 120000)

    def gen_media():
        med_impressions = ri(500, 15000)
        med_reach = ri(int(med_impressions * 0.55), med_impressions)
        likes = ri(30, max(31, int(med_reach * 0.04)))
        return {
            "ig_media_id": str(ri(10000000000000000, 99999999999999999)),
            "ig_media_type": random.choice(["IMAGE", "VIDEO", "CAROUSEL_ALBUM"]),
            "ig_media_caption": f"{team.get('organization_title', 'Demo')} post caption. #engagement #community",
            "ig_media_timestamp": now_iso(target_date),
            "ig_media_like_count": likes,
            "ig_media_comments_count": ri(2, 80),
            "ig_media_url": f"https://www.instagram.com/p/{org_slug(team)}{ri(10000, 99999)}/",
            "ig_media_permalink": f"https://www.instagram.com/p/{org_slug(team)}{ri(10000, 99999)}/",
            "ig_media_insight_impressions": med_impressions,
            "ig_media_insight_reach": med_reach,
            "ig_media_insight_engagement": ri(50, 1500),
            "ig_media_insight_saved": ri(10, 300),
            "ig_media_insight_video_views": ri(200, 8000),
            "ig_media_insight_likes": likes,
            "ig_media_insight_comments": ri(2, 80),
            "ig_media_insight_shares": ri(5, 150),
            "ig_media_insight_plays": ri(200, 8000),
            "ig_media_insight_total_interactions": ri(80, 2000),
            "ig_media_insight_carousel_album_engagement": ri(15, 300),
        }

    return {
        "@timestamp": now_iso(target_date),
        "kibana_date": ds,
        "ig_user_id": str(ri(10000000000000000, 99999999999999999)),
        "ig_user_username": org_slug(team),
        "ig_user_name": team.get("organization_title", "Demo"),
        "ig_user_profile_picture_url": f"https://example.com/profile_{org_slug(team)}.jpg",
        "ig_user_followers_count": followers,
        "ig_user_follows_count": ri(100, 2000),
        "ig_user_media_count": ri(50, 600),
        "ig_user_biography": f"Official Instagram account of {team.get('organization_title', 'Demo')}.",
        "ig_user_website": team.get("team_site_url", "https://demo.example.com"),
        "ig_insight_impressions": ri(8000, 300000),
        "ig_insight_reach": ri(3000, max(3001, int(followers * 0.4))),
        "ig_insight_profile_views": ri(200, 8000),
        "ig_insight_follower_count": followers,
        "ig_insight_website_clicks": ri(20, 800),
        "ig_insight_email_contacts": ri(5, 150),
        "ig_insight_phone_call_clicks": ri(3, 80),
        "ig_insight_get_directions_clicks": ri(2, 50),
        "ig_media": (media_items := [gen_media() for _ in range(ri(3, 6))]),
        "ig_media_total_likes": sum(m["ig_media_insight_likes"] for m in media_items),
        "ig_media_total_shares": sum(m["ig_media_insight_shares"] for m in media_items),
        "ig_media_total_comments": sum(m["ig_media_insight_comments"] for m in media_items),
        "ig_media_total_impressions": sum(m["ig_media_insight_impressions"] for m in media_items),
        "ig_media_total_reach": sum(m["ig_media_insight_reach"] for m in media_items),
        "ig_media_total_engagement": sum(m["ig_media_insight_engagement"] for m in media_items),
        "ig_media_total_saves": sum(m["ig_media_insight_saved"] for m in media_items),
        "ig_media_total_video_views": sum(m["ig_media_insight_video_views"] for m in media_items),
        "ig_media_total_plays": sum(m["ig_media_insight_plays"] for m in media_items),
        "ig_media_total_interactions": sum(m["ig_media_insight_total_interactions"] for m in media_items),
        "team_site_url": team.get("team_site_url", "https://demo.example.com"),
        "team_title": team["team_title"],
        "team_country": team.get("team_country", "United States"),
        "team_city": team.get("team_city", "New York"),
        "team_esid": team["team_esid"],
        "organization_title": team.get("organization_title", "Demo Organization"),
    }


def gen_youtube_analytics(team, target_date=None):
    ds = date_str(target_date=target_date)
    views = ri(1000, 80000)

    return {
        "@timestamp": now_iso(target_date),
        "kibana_date": ds,
        "date": ds,
        "youtube_channel_id": f"UC{ri(10000000000000000, 99999999999999999)}",
        "youtube_channel_title": team.get("organization_title", "Demo"),
        "youtube_channel_handle": f"@{org_slug(team)}",
        "youtube_channel_esid": f"yt_{ri(10000, 99999)}",
        "views": views,
        "estimated_minutes_watched": ri(int(views * 0.8), int(views * 6)),
        "average_view_duration": rf(45, 720),
        "subscribers_gained": ri(10, 600),
        "subscribers_lost": ri(1, 60),
        "likes": ri(15, max(16, int(views * 0.04))),
        "comments": ri(5, 150),
        "shares": ri(5, 300),
        "videos_added_to_playlists": ri(1, 30),
        "videos_removed_from_playlists": ri(1, 8),
        "team_esid": team["team_esid"],
        "team_title": team["team_title"],
        "team_url": team.get("team_site_url", "https://demo.example.com"),
        "team_country": team.get("team_country", "United States"),
        "team_city": team.get("team_city", "New York"),
        "organization_title": team.get("organization_title", "Demo Organization"),
    }


def gen_youtube_country_breakdown(team, target_date=None):
    """Generate per-country YouTube analytics breakdown documents.

    Returns a list of record_type='country_breakdown' documents that the
    map query (_query_youtube_country_map) aggregates by country code.
    Each document represents one (day, country) combination.
    """
    ds = date_str(target_date=target_date)
    channel_id = f"UC{ri(10000000000000000, 99999999999999999)}"

    docs = []
    countries = random.sample(DEMO_LOCATIONS, k=min(ri(5, 10), len(DEMO_LOCATIONS)))
    for loc in countries:
        views = ri(50, 5000)
        docs.append(
            {
                "@timestamp": now_iso(target_date),
                "kibana_date": ds,
                "date": ds,
                "record_type": "country_breakdown",
                "country": loc["iso2"],
                "youtube_channel_id": channel_id,
                "youtube_channel_title": team.get("organization_title", "Demo"),
                "youtube_channel_handle": f"@{org_slug(team)}",
                "youtube_channel_esid": f"yt_{ri(10000, 99999)}",
                "views": views,
                "estimated_minutes_watched": ri(int(views * 0.8), int(views * 6)),
                "subscribers_gained": ri(1, 60),
                "subscribers_lost": ri(0, 5),
                "likes": ri(1, max(2, int(views * 0.04))),
                "comments": ri(0, 20),
                "shares": ri(0, 30),
                "team_esid": team["team_esid"],
                "team_title": team["team_title"],
                "team_url": team.get("team_site_url", "https://demo.example.com"),
                "team_country": team.get("team_country", "United States"),
                "team_city": team.get("team_city", "New York"),
                "organization_title": team.get("organization_title", "Demo Organization"),
            }
        )
    return docs


def gen_chatwoot_insights(team, target_date=None):
    ds = date_str(target_date=target_date)
    conversations = ri(20, 300)
    resolutions = ri(int(conversations * 0.5), conversations)
    incoming = ri(conversations * 2, conversations * 6)
    outgoing = ri(incoming - ri(50, 200), incoming + ri(50, 200))
    outgoing = max(1, outgoing)

    return {
        "@timestamp": now_iso(target_date),
        "kibana_date": ds,
        "date": ds,
        "chatwoot_account_title": f"{team.get('organization_title', 'Demo')} Chatwoot Account",
        "chatwoot_account_id": "999999",
        "chatwoot_account_esid": f"{org_slug(team)}_chatwoot_{ri(10000, 99999)}",
        "team_title": team["team_title"],
        "team_esid": team["team_esid"],
        "organization_title": team.get("organization_title", ""),
        "conversations_count": conversations,
        "incoming_messages_count": incoming,
        "outgoing_messages_count": outgoing,
        "resolutions_count": resolutions,
        "avg_first_response_time": round(rf(30, 600), 2),  # seconds
        "avg_resolution_time": round(rf(300, 14400), 2),  # seconds
        "avg_reply_time": round(rf(20, 300), 2),  # seconds
    }


def gen_chatwoot_conversations(team, target_date=None):
    """Generate sample Chatwoot conversation documents.

    Returns a list of conversations so the Chat Analytics dashboard
    (which queries *chatwoot_conversations for sender_id cardinality) has data.
    """
    docs = []
    statuses = ["open", "resolved", "pending"]
    channels = ["web_widget", "facebook", "instagram", "email"]
    names = ["Alex Johnson", "Maria Garcia", "James Lee", "Sarah Williams", "Ahmed Hassan", "Priya Sharma"]

    for i in range(ri(5, 10)):
        if target_date is not None:
            created = datetime(target_date.year, target_date.month, target_date.day, ri(6, 22), ri(0, 59), ri(0, 59))
        else:
            created = datetime.utcnow() - timedelta(days=ri(0, 30), hours=ri(0, 23))
        ds = created.strftime("%Y-%m-%d")
        docs.append(
            {
                "@timestamp": created.isoformat() + "Z",
                "kibana_date": ds,
                "date": ds,
                "conversation_id": ri(10000, 99999),
                "conversation_uuid": f"{org_slug(team)}-conv-{ri(10000, 99999)}",
                "sender_id": ri(1000, 9999),
                "sender_name": random.choice(names),
                "sender_email": f"sender{ri(100, 999)}@example.com",
                "assignee_id": ri(1, 20),
                "assignee_name": random.choice(["volunteer_01", "volunteer_02", "agent_01", "agent_02"]),
                "status": random.choice(statuses),
                "channel_type": random.choice(channels),
                "inbox_id": ri(1, 5),
                # the unified schema maps these as date/epoch_second
                # (the real importer passes Chatwoot's epoch-second ints through),
                # so the demo docs must be ints too — ISO strings here were
                # rejected by the schema-conformant index.
                "created_at": _epoch_seconds(created),
                "last_activity_at": _epoch_seconds(created + timedelta(minutes=ri(5, 120))),
                "first_reply_created_at": _epoch_seconds(created + timedelta(minutes=ri(1, 30))),
                "unread_count": ri(0, 5),
                "muted": False,
                "can_reply": True,
                "chatwoot_account_id": "999999",
                "chatwoot_account_esid": f"{org_slug(team)}_chatwoot_{ri(10000, 99999)}",
                "chatwoot_account_title": f"{team.get('organization_title', 'Demo')} Chatwoot Account",
                "team_title": team["team_title"],
                "team_esid": team["team_esid"],
                "organization_title": team.get("organization_title", ""),
            }
        )
    return docs


# ─── Main ─────────────────────────────────────────────────────────────────────


def _build_target_dates(days):
    """Return a list of dates ending at today, or [None] for legacy random mode."""
    if days is None:
        return [None]
    today = date.today()
    return [today - timedelta(days=i) for i in range(days - 1, -1, -1)]


def _run_source_day(es, team, source, td):
    """Invoke a single source generator for one target date and index the docs.

    Returns True on success, False on exception (logged, not re-raised).
    """
    logger.info(f"\n  [{source['name']}] → {source['index']}")
    try:
        if "pre_hook" in source:
            source["pre_hook"](source["index"], source["mapping"])
        result = source["generator"](team, target_date=td)
        docs = result if isinstance(result, list) else [result]
        for doc in docs:
            # mapping=None (raw es.index, no index creation) was the DT-staging
            # path; every remaining source carries an explicit mapping.
            index_doc(es, source["index"], source["mapping"], doc)
        logger.info(f"    ✓ Indexed {len(docs)} document(s) into {source['index']}")
        return True
    except Exception as e:
        logger.error(f"    ✗ Failed: {e}", exc_info=True)
        return False


def _select_sources(sources, wanted):
    """Restrict ``sources`` to the comma-separated slugs in ``wanted`` (None = all).

    Exits 2 on an unknown slug rather than silently running nothing — a cron
    line with a typo must fail loudly, not report "0/0 succeeded".
    """
    if not wanted:
        return sources
    requested = [w.strip() for w in wanted.split(",") if w.strip()]
    known = {src["source"] for src in sources}
    unknown = sorted(set(requested) - known)
    if unknown:
        logger.error(f"--sources: unknown source slug(s) {unknown}; known: {sorted(known)}")
        sys.exit(2)
    selected = [src for src in sources if src["source"] in requested]
    logger.info(f"--sources: running {len(selected)} of {len(sources)} sources: {requested}")
    return selected


def _run_sources_over_dates(es, team, sources, target_dates, label):
    """Run each source in `sources` once per date in `target_dates`.

    Returns (total_success, total_source_days).
    """
    total_days = len(target_dates)
    total_success = 0
    total_source_days = 0
    for day_idx, td in enumerate(target_dates, 1):
        day_label = td.strftime("%Y-%m-%d") if td else "random"
        logger.info(f"\n{'='*50}")
        logger.info(f"Generating {label} data for {day_label} ({day_idx}/{total_days})")
        logger.info(f"{'='*50}")
        day_success = 0
        for source in sources:
            if _run_source_day(es, team, source, td):
                day_success += 1
        total_success += day_success
        total_source_days += len(sources)
        logger.info(f"  Day {day_label}: {day_success}/{len(sources)} sources succeeded.")
    return total_success, total_source_days


def main():
    parser = argparse.ArgumentParser(description="Generate anonymized demo data for a team.")
    parser.add_argument("--team", default="DemoTeam", help="team_title to seed (default: DemoTeam)")
    parser.add_argument("--org", default=None, help="organization_title override (default: derived from team name)")
    parser.add_argument(
        "--days",
        type=int,
        default=None,
        help="Number of days to generate, ending at today (e.g. 90 for backfill, 1 for today only). "
        "When omitted the script runs once with random dates (legacy behaviour).",
    )
    parser.add_argument(
        "--target-date",
        default=None,
        help="Generate data for a specific date (yyyy-MM-dd). Overrides --days.",
    )
    parser.add_argument(
        "--sources",
        default=None,
        help="Comma-separated source slugs to generate (e.g. chatwoot_insights,chatwoot_conversations). "
        "Default: every source. the prod cron restricts this script to the sources the "
        "anonymized clone does not cover.",
    )
    parser.add_argument(
        "--dt-days",
        type=int,
        default=None,
        help="DEPRECATED no-op. Disciple Tools demo data is no longer "
        "generated here — the daily seed_kavanah_demo_dt.py --reseed cron owns it. "
        "Accepted so earlier cron lines keep running.",
    )
    args = parser.parse_args()

    if args.dt_days is not None:
        logger.warning(
            "--dt-days is deprecated and ignored: Disciple Tools demo data "
            "is maintained by scripts/seed_kavanah_demo_dt.py --reseed."
        )

    team_title = args.team
    if args.org:
        org_title = args.org
    elif team_title == "DemoTeam":
        org_title = "DemoOrg"
    else:
        # Derive org from team name: "Kavanah Team" → "Kavanah"
        org_title = team_title.split()[0]

    # Build the list of target dates to generate data for
    if args.target_date:
        target_dates = [datetime.strptime(args.target_date, "%Y-%m-%d").date()]
    else:
        target_dates = _build_target_dates(args.days)

    random.seed()  # Use system entropy

    logger.info("Connecting to Elasticsearch...")
    es = get_elasticsearch_client()
    if es is None:
        logger.error("Failed to connect to Elasticsearch. Exiting.")
        sys.exit(1)
    logger.info("Connected.")

    logger.info(f"Ensuring org exists: {org_title}")
    ensure_org_exists(es, org_title)

    logger.info(f"Looking up team: {team_title}")
    team = find_team(es, team_title, org_title)
    if team is None:
        sys.exit(1)
    logger.info(f"Found team: {team.get('team_title')} (esid: {team.get('team_esid')})")

    # index names are esid-form ({team_esid}__{source}) like every real
    # importer. The slug-form f"{sanitize(title)}_{source}" names this
    # script used to derive were reverse aliases dropped them; after
    # that the generator silently re-created them as concrete indices and the demo
    # team split into two index families.
    logger.info(f"Index family: '{build_data_index_name(team['team_esid'], '<source>')}'")

    sources = [
        {
            "name": "Facebook Ads Insights",
            "source": "fb_insights",
            "index": build_data_index_name(team["team_esid"], "fb_insights"),
            "mapping": load_schema_properties("unified_schema_fb_insights"),
            "generator": gen_fb_insights,
        },
        {
            "name": "Facebook Page Insights",
            "source": "fb_page_insights",
            "index": build_data_index_name(team["team_esid"], "fb_page_insights"),
            "mapping": load_schema_properties("unified_schema_fb_page_insights"),
            "generator": gen_fb_page_insights,
        },
        {
            "name": "Google Ads",
            "source": "google_ads",
            "index": build_data_index_name(team["team_esid"], "google_ads"),
            "mapping": load_schema_properties("unified_schema_google_ads"),
            "generator": gen_google_ads,
        },
        {
            "name": "Google Ads (Geo Breakdown)",
            "source": "google_ads",
            "index": build_data_index_name(team["team_esid"], "google_ads"),
            "mapping": load_schema_properties("unified_schema_google_ads"),
            "generator": gen_google_ads_geo,
        },
        {
            "name": "Google Analytics",
            "source": "ga_metrics",
            "index": build_data_index_name(team["team_esid"], "ga_metrics"),
            "mapping": load_schema_properties("unified_schema_ga_metrics"),
            "generator": gen_ga_metrics,
        },
        {
            "name": "Echo Conversations",
            "source": "echo_conversations",
            "index": build_data_index_name(team["team_esid"], "echo_conversations"),
            "mapping": load_schema_properties("unified_schema_echo_conversations"),
            "generator": gen_echo_conversations,
        },
        {
            "name": "Instagram Insights",
            "source": "ig_insights",
            "index": build_data_index_name(team["team_esid"], "ig_insights"),
            "mapping": load_schema_properties("unified_schema_ig_insights"),
            "generator": gen_ig_insights,
        },
        {
            "name": "YouTube Analytics",
            "source": "youtube_analytics",
            "index": build_data_index_name(team["team_esid"], "youtube_analytics"),
            "mapping": load_schema_properties("unified_schema_youtube_analytics"),
            "generator": gen_youtube_analytics,
        },
        {
            "name": "YouTube Analytics (Country Breakdown)",
            "source": "youtube_analytics",
            "index": build_data_index_name(team["team_esid"], "youtube_analytics"),
            "mapping": load_schema_properties("unified_schema_youtube_analytics"),
            "generator": gen_youtube_country_breakdown,
        },
        # Disciple Tools intentionally absent: the push-era
        # staging→promote pipeline this script fed was retired at the
        # cutover; the daily seed_kavanah_demo_dt.py --reseed cron owns demo DT.
        {
            "name": "Chatwoot Insights",
            "source": "chatwoot_insights",
            "index": build_data_index_name(team["team_esid"], "chatwoot_insights"),
            "mapping": load_schema_properties("unified_schema_chatwoot_insights"),
            "generator": gen_chatwoot_insights,
            # Ensure a higher-priority regular index template exists so ES
            # doesn't try to apply a data-stream template to this index.
            "pre_hook": lambda idx, m: put_regular_index_template(es, idx, m),
        },
        {
            "name": "Chatwoot Conversations",
            "source": "chatwoot_conversations",
            "index": build_data_index_name(team["team_esid"], "chatwoot_conversations"),
            "mapping": load_schema_properties("unified_schema_chatwoot_conversations"),
            "generator": gen_chatwoot_conversations,
            "pre_hook": lambda idx, m: put_regular_index_template(es, idx, m),
        },
    ]

    sources = _select_sources(sources, args.sources)

    total_success, total_sources = _run_sources_over_dates(es, team, sources, target_dates, label="demo")

    logger.info(f"\n{'='*50}")
    logger.info(
        f"Done. {total_success}/{total_sources} source-days indexed for {team['team_title']} " f"({len(target_dates)} day(s))."
    )
    failed = total_sources - total_success
    if failed:
        # a source-day that raised was logged with ✗ and swallowed by
        # _run_source_day, and this script then exited 0 — so the 05:30 cron
        # could never be told apart from a good run by anything but reading
        # its log. Exit 1 with a one-line verdict LAST, so the tracked-job
        # wrapper records it as the run's summary.
        logger.error(
            f"FAILED: {failed}/{total_sources} source-day(s) did not index for {team['team_title']} — see the ✗ lines above."
        )
        sys.exit(1)


if __name__ == "__main__":
    main()
