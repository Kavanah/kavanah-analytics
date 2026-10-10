"""Audit script: compare ds_* checkbox values against actual ES data presence and asset configuration."""

import sys
import os

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "../..")))
from lib.es_helpers import get_elasticsearch_client

es = get_elasticsearch_client()

DS_INDEX_PATTERNS = {
    "ds_facebook": ["*fb_insights", "*fb_page_insights"],
    "ds_echo": ["*echo_conversations"],
    "ds_chatwoot": ["*chatwoot_conversations", "*chatwoot_insights"],
    "ds_google_analytics": ["*ga_metrics"],
    "ds_google_ads": ["*google_ads"],
    "ds_instagram": ["*ig_insights"],
    "ds_youtube": ["*youtube_analytics"],
}

# Asset link field that, if non-empty, means the data source is actively
# configured. (2026-06-19): use the *_esid link fields, not the legacy
# display fields the ESID migration emptied (see update_v14 note). This
# audit is read-only, so a stale map only produced false mismatch reports — but
# it should mirror connector_status_service.CONNECTORS' team_esid_field.
# (ds_facebook also has a fb_ad_account_esid fallback for the account_wide
# cohort; not modelled here since this is a coarse audit, but a team wired only
# via the ad-account shape may report a benign "should be checked" mismatch.)
DS_ASSET_FIELDS = {
    "ds_facebook": "team_facebook_page_esids",
    "ds_echo": "echo_tenant_esid",
    "ds_chatwoot": "chatwoot_account_esid",
    "ds_google_analytics": "team_ga4_property_esids",
    "ds_google_ads": "google_ads_customer_esid",
    "ds_instagram": "team_instagram_profile_esids",
    "ds_youtube": "team_youtube_channel_handle_esids",
}


def _is_empty(value):
    if value is None:
        return True
    if isinstance(value, list):
        return len(value) == 0
    return str(value).strip() == ""


teams = es.search(index="de_asset_teams", body={"query": {"match_all": {}}, "size": 1000})["hits"]["hits"]

mismatches = []
print(f"{'Team':<23} {'DS field':<25} {'checkbox':<10} {'count':<7} {'asset':<6} status")
print("-" * 85)
for team in sorted(teams, key=lambda t: t["_source"].get("team_title", "")):
    title = team["_source"].get("team_title", "?")
    src = team["_source"]
    for ds, patterns in DS_INDEX_PATTERNS.items():
        cb = src.get(ds, "unset")
        asset_field = DS_ASSET_FIELDS.get(ds)
        has_asset = not _is_empty(src.get(asset_field)) if asset_field else False
        q = {
            "bool": {
                "should": [
                    {"term": {"team_title": title}},
                    {"term": {"team_title.keyword": title}},
                ],
                "minimum_should_match": 1,
            }
        }
        n = es.count(index=",".join(patterns), body={"query": q}, ignore_unavailable=True).get("count", 0)
        has_data = n > 0
        should_be_checked = has_data or has_asset

        if should_be_checked or cb == "true":
            flag = ""
            if cb != "true" and should_be_checked:
                flag = "*** SHOULD BE CHECKED"
                mismatches.append((title, ds, n, has_asset))
            elif cb == "true" and not should_be_checked:
                flag = "*** CHECKED / NO DATA / NO ASSET"
                mismatches.append((title, ds, n, has_asset))
            asset_indicator = "yes" if has_asset else "no"
            print(f"{title[:22]:<23} {ds:<25} {cb:<10} {n:<7} {asset_indicator:<6} {flag}")

print()
if mismatches:
    print(f"MISMATCHES FOUND: {len(mismatches)}")
    for t, d, n, a in mismatches:
        print(f"  {t} / {d}  (count={n}, asset={'yes' if a else 'no'})")
else:
    print("All checkboxes match data + asset presence.")
