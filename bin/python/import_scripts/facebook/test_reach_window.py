"""
 Phase 1 — validation: does FB return deduplicated reach when we ask for
a multi-day time_range, vs. the inflated sum of daily reaches we currently store?

Makes two ad-level Insights calls for the same ad:
  1. time_range = {since: <date>, until: <date>}                  (single-day)
  2. time_range = {since: <window_start>, until: <window_end>}    (multi-day)

Prints both, plus the matching daily-summed reach from our ES index for the
same window, so the three numbers can be compared side by side.

Usage:
    python -m import_scripts.facebook.test_reach_window \
        --ad_account_id 456001023325657 \
        --ad_id 120... \
        --since 2026-04-01 --until 2026-04-08
"""

import argparse
import logging
import sys

import lib.logging
from facebook_business.adobjects.ad import Ad
from import_scripts.facebook.get_insights import (
    init_facebook_api,
    get_fb_app_user_token,
    set_import_params,
)
from import_scripts.facebook.lib.fb_helpers import strip_ad_account_prefix
from lib.es_helpers import get_elasticsearch_client, sanitize_team_title


def fetch_insights(ad_id: str, since: str, until: str) -> dict:
    insights = Ad(ad_id).get_insights(
        fields=["impressions", "reach", "spend", "frequency"],
        params={
            "time_range": {"since": since, "until": until},
            "action_attribution_windows": ["7d_click", "1d_view"],
            "attribution_setting": "7d_click_1d_view",
        },
    )
    if not insights:
        return {}
    return dict(insights[0])


def get_daily_summed_reach_from_es(team_title: str, ad_account_id: str, ad_id: str, since: str, until: str) -> dict:
    es = get_elasticsearch_client()
    if es is None:
        return {"error": "could not connect to ES"}

    index_name = f"{sanitize_team_title(team_title)}_fb_insights"
    body = {
        "size": 0,
        "query": {
            "bool": {
                "filter": [
                    {"term": {"ad_account_id": strip_ad_account_prefix(ad_account_id)}},
                    {"term": {"ad_id": ad_id}},
                    {"range": {"insight_date_start": {"gte": since, "lte": until}}},
                ]
            }
        },
        "aggs": {
            "doc_count": {"value_count": {"field": "ad_id"}},
            "impressions": {"sum": {"field": "insight_impressions"}},
            "reach": {"sum": {"field": "insight_reach"}},
            "spend": {"sum": {"field": "insight_spend"}},
        },
    }
    result = es.search(index=index_name, body=body)
    aggs = result.get("aggregations", {})
    return {
        "doc_count": aggs.get("doc_count", {}).get("value"),
        "impressions": aggs.get("impressions", {}).get("value"),
        "reach": aggs.get("reach", {}).get("value"),
        "spend": aggs.get("spend", {}).get("value"),
    }


def find_ad_account_entry(ad_account_id: str) -> dict | None:
    target = strip_ad_account_prefix(ad_account_id)
    for entry in set_import_params():
        stored = strip_ad_account_prefix(entry["fb_ad_account"].get("fb_ad_account_id", ""))
        if stored == target:
            return entry
    return None


def main() -> int:
    lib.logging.define("fb-test-reach-window")

    parser = argparse.ArgumentParser(description="reach-window validation")
    parser.add_argument("--ad_account_id", required=True, help="Numeric ad account ID")
    parser.add_argument("--ad_id", required=True, help="The ad to test")
    parser.add_argument("--since", required=True, help="Window start, YYYY-MM-DD")
    parser.add_argument("--until", required=True, help="Window end, YYYY-MM-DD")
    parser.add_argument("--team_title", default=None, required=True, help="Team title for the ES index lookup")
    args = parser.parse_args()

    entry = find_ad_account_entry(args.ad_account_id)
    if entry is None:
        logging.error(f"No ad account entry found for {args.ad_account_id}")
        return 1

    fb_app = entry["fb_app"]
    fb_ad_account = entry["fb_ad_account"]

    token = get_fb_app_user_token(fb_ad_account.get("fb_app_user_esid"))
    if not token:
        logging.error("Could not resolve fb_app_user token for this ad account")
        return 1

    init_facebook_api(fb_app.get("fb_app_id"), fb_app.get("fb_app_secret"), token)

    single_day = fetch_insights(args.ad_id, args.since, args.since)
    window = fetch_insights(args.ad_id, args.since, args.until)
    es_summed = get_daily_summed_reach_from_es(args.team_title, args.ad_account_id, args.ad_id, args.since, args.until)

    print()
    print(f"Ad: {args.ad_id}")
    print(f"Window: {args.since} .. {args.until}")
    print()
    print(f"{'Source':<40} {'Impressions':>14} {'Reach':>10} {'Spend':>10}")
    print("-" * 78)
    print(
        f"{'FB API single-day (' + args.since + ')':<40} "
        f"{int(float(single_day.get('impressions', 0))):>14,} "
        f"{int(float(single_day.get('reach', 0))):>10,} "
        f"{float(single_day.get('spend', 0)):>10.2f}"
    )
    print(
        f"{'FB API window (' + args.since + '..' + args.until + ')':<40} "
        f"{int(float(window.get('impressions', 0))):>14,} "
        f"{int(float(window.get('reach', 0))):>10,} "
        f"{float(window.get('spend', 0)):>10.2f}"
    )
    print(
        f"{'ES daily-summed (our stored data)':<40} "
        f"{int(es_summed.get('impressions') or 0):>14,} "
        f"{int(es_summed.get('reach') or 0):>10,} "
        f"{float(es_summed.get('spend') or 0):>10.2f}"
    )
    print()
    print(f"ES daily doc count over window:  {es_summed.get('doc_count')}")
    print()
    if window.get("reach") and es_summed.get("reach"):
        ratio = float(es_summed["reach"]) / float(window["reach"])
        print(f"Daily-summed inflation factor (ES daily-summed / FB window): {ratio:.2f}x")

    return 0


if __name__ == "__main__":
    sys.exit(main())
