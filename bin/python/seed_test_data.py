#!/usr/bin/env python3
"""
Step 4 — Seed Test Indices
Creates test indices from schema definitions and seeds them with
anonymized sample documents. Runs inside imports_container.

Usage: python3 /app/python/seed_test_data.py
"""

import os
import sys
import json
import ssl
from datetime import datetime, timedelta, timezone
from elasticsearch import Elasticsearch


# ── ES connection ──────────────────────────────────────────────────────────────
def get_es():
    ctx = ssl.create_default_context()
    ctx.verify_flags &= ~ssl.VERIFY_X509_STRICT
    ca = "/app/data/certs/es01_http_ca.crt"
    if not os.path.exists(ca):
        print(f"[ERROR] CA cert not found at {ca}")
        sys.exit(1)
    ctx.load_verify_locations(cafile=ca)
    es = Elasticsearch(
        [f"https://{os.environ['ES01_INTERNAL_IP']}:9200"],
        basic_auth=("elastic", os.environ["ES01_PASSWORD"]),
        ssl_context=ctx,
        ssl_show_warn=False,
        verify_certs=True,
    )
    if not es.ping():
        print("[ERROR] Cannot connect to Elasticsearch")
        sys.exit(1)
    return es


# ── Load schema ────────────────────────────────────────────────────────────────
SCHEMAS_DIR = "/app/python/schemas"


def load_schema(filename):
    with open(os.path.join(SCHEMAS_DIR, filename)) as f:
        data = json.load(f)
    # Some schemas (e.g. youtube) have a top-level "mappings" wrapper;
    # unwrap to get the properties-level object for es.indices.create(mappings=...)
    if "mappings" in data and "properties" not in data:
        return data["mappings"]
    return data


# ── Sample documents (fully anonymized) ───────────────────────────────────────
def make_timestamp(days_ago=0):
    dt = datetime.now(timezone.utc) - timedelta(days=days_ago)
    return dt.strftime("%Y-%m-%dT%H:%M:%SZ")


def make_date(days_ago=0):
    return (datetime.now(timezone.utc) - timedelta(days=days_ago)).strftime("%Y-%m-%d")


SAMPLE_DOCS = {
    "test_fb_insights": [
        {
            "ad_account_id": "test_acct_001",
            "ad_campaign_id": "test_camp_001",
            "ad_id": "test_ad_001",
            "ad_name": "[TEST] Sample Ad Alpha",
            "ad_status": "ACTIVE",
            "ad_campaign_name": "[TEST] Campaign Alpha",
            "adset_id": "test_adset_001",
            "adset_name": "[TEST] Adset Alpha",
            "insight_impressions": 12500,
            "insight_clicks": 320,
            "insight_spend": 45.50,
            "insight_ctr": 2.56,
            "insight_cpm": 3.64,
            "insight_cpc": 0.14,
            "insight_reach": 10200,
            "insight_frequency": 1.22,
            "insight_date_start": make_date(7),
            "insight_date_stop": make_date(1),
            "kibana_date": make_date(1),
            "@timestamp": make_timestamp(1),
            "team_title": "Test Organisation A",
            "team_esid": "testa",
            "organization_title": "Test Organisation A",
            "page_id": "test_page_001",
            "page_name": "[TEST] Page Alpha",
            "result_type": "link_clicks",
            "result_count": 320.0,
        },
        {
            "ad_account_id": "test_acct_002",
            "ad_campaign_id": "test_camp_002",
            "ad_id": "test_ad_002",
            "ad_name": "[TEST] Sample Ad Beta",
            "ad_status": "PAUSED",
            "ad_campaign_name": "[TEST] Campaign Beta",
            "adset_id": "test_adset_002",
            "adset_name": "[TEST] Adset Beta",
            "insight_impressions": 8750,
            "insight_clicks": 198,
            "insight_spend": 31.25,
            "insight_ctr": 2.26,
            "insight_cpm": 3.57,
            "insight_cpc": 0.16,
            "insight_reach": 7100,
            "insight_frequency": 1.23,
            "insight_date_start": make_date(14),
            "insight_date_stop": make_date(7),
            "kibana_date": make_date(7),
            "@timestamp": make_timestamp(7),
            "team_title": "Test Organisation B",
            "team_esid": "testb",
            "organization_title": "Test Organisation B",
            "page_id": "test_page_002",
            "page_name": "[TEST] Page Beta",
            "result_type": "link_clicks",
            "result_count": 198.0,
        },
        {
            "ad_account_id": "test_acct_001",
            "ad_campaign_id": "test_camp_003",
            "ad_id": "test_ad_003",
            "ad_name": "[TEST] Sample Ad Gamma",
            "ad_status": "ACTIVE",
            "ad_campaign_name": "[TEST] Campaign Gamma",
            "adset_id": "test_adset_003",
            "adset_name": "[TEST] Adset Gamma",
            "insight_impressions": 21000,
            "insight_clicks": 560,
            "insight_spend": 88.00,
            "insight_ctr": 2.67,
            "insight_cpm": 4.19,
            "insight_cpc": 0.16,
            "insight_reach": 18500,
            "insight_frequency": 1.14,
            "insight_date_start": make_date(3),
            "insight_date_stop": make_date(1),
            "kibana_date": make_date(1),
            "@timestamp": make_timestamp(1),
            "team_title": "Test Organisation A",
            "team_esid": "testa",
            "organization_title": "Test Organisation A",
            "page_id": "test_page_001",
            "page_name": "[TEST] Page Alpha",
            "result_type": "page_engagement",
            "result_count": 560.0,
        },
    ],
    "test_ga_metrics": [
        {
            "@timestamp": make_timestamp(1),
            "kibana_date": make_date(1),
            "team_esid": "testa",
            "team_title": "Test Organisation A",
            "organization_title": "Test Organisation A",
            "ga_sessions": 1540,
            "ga_users": 1200,
            "ga_new_users": 880,
            "ga_pageviews": 4320,
            "ga_bounce_rate": 42.5,
            "ga_avg_session_duration": 185.3,
            "ga_goal_completions": 48,
        },
        {
            "@timestamp": make_timestamp(7),
            "kibana_date": make_date(7),
            "team_esid": "testb",
            "team_title": "Test Organisation B",
            "organization_title": "Test Organisation B",
            "ga_sessions": 980,
            "ga_users": 790,
            "ga_new_users": 560,
            "ga_pageviews": 2800,
            "ga_bounce_rate": 48.2,
            "ga_avg_session_duration": 142.7,
            "ga_goal_completions": 22,
        },
    ],
    "test_echo_conversations": [
        {
            "@timestamp": make_timestamp(1),
            "team_esid": "testa",
            "team_title": "Test Organisation A",
            "organization_title": "Test Organisation A",
            "conversation_id": "test_conv_001",
            "conversation_status": "resolved",
            "conversation_channel": "webchat",
            "message_count": 8,
            "response_time_seconds": 45,
        },
        {
            "@timestamp": make_timestamp(2),
            "team_esid": "testb",
            "team_title": "Test Organisation B",
            "organization_title": "Test Organisation B",
            "conversation_id": "test_conv_002",
            "conversation_status": "open",
            "conversation_channel": "sms",
            "message_count": 3,
            "response_time_seconds": 120,
        },
    ],
    "test_ig_insights": [
        {
            "@timestamp": make_timestamp(1),
            "kibana_date": make_date(1),
            "team_esid": "testa",
            "team_title": "Test Organisation A",
            "organization_title": "Test Organisation A",
            "ig_account_id": "test_ig_001",
            "ig_impressions": 5600,
            "ig_reach": 4200,
            "ig_likes": 340,
            "ig_comments": 28,
            "ig_saves": 95,
        }
    ],
    "test_fb_page_insights": [
        {
            "@timestamp": make_timestamp(1),
            "kibana_date": make_date(1),
            "team_esid": "testa",
            "team_title": "Test Organisation A",
            "organization_title": "Test Organisation A",
            "page_id": "test_page_001",
            "page_name": "[TEST] Page Alpha",
            "page_fan_count": 12500,
            "page_followers_count": 11800,
            "page_impressions": 34000,
            "page_reach": 28000,
            "page_engaged_users": 1450,
        }
    ],
    "test_youtube_analytics": [
        {
            "@timestamp": make_timestamp(1),
            "kibana_date": make_date(1),
            "team_esid": "testa",
            "team_title": "Test Organisation A",
            "organization_title": "Test Organisation A",
            "yt_channel_id": "test_yt_001",
            "yt_views": 8900,
            "yt_watch_time_minutes": 22400,
            "yt_subscribers_gained": 45,
            "yt_likes": 380,
        }
    ],
}

# ── Schema to index mapping ────────────────────────────────────────────────────
SCHEMA_INDEX_MAP = {
    "test_fb_insights": "unified_schema_fb_insights.json",
    "test_ga_metrics": "unified_schema_ga_metrics.json",
    "test_echo_conversations": "unified_schema_echo_conversations.json",
    "test_ig_insights": "unified_schema_ig_insights.json",
    "test_fb_page_insights": "unified_schema_fb_page_insights.json",
    "test_youtube_analytics": "unified_schema_youtube_analytics.json",
}


def create_index(es, index_name, schema_file):
    mapping = load_schema(schema_file)
    if es.indices.exists(index=index_name):
        print(f"  [SKIP] Index '{index_name}' already exists")
        return True
    es.indices.create(index=index_name, settings={"number_of_replicas": 0}, mappings=mapping)
    print(f"  [OK]   Created index '{index_name}'")
    return True


def seed_documents(es, index_name, docs):
    seeded = 0
    for doc in docs:
        es.index(index=index_name, document=doc, refresh="wait_for")
        seeded += 1
    print(f"  [OK]   Seeded {seeded} document(s) into '{index_name}'")
    return seeded


def main():
    print("=" * 60)
    print("Test Data Seed")
    print(f"Time: {datetime.now(timezone.utc).isoformat()}")
    print("=" * 60)

    es = get_es()
    es_ip = os.environ.get("ES01_INTERNAL_IP", "unknown")
    print(f"\n[OK] Connected to Elasticsearch at {es_ip}")

    total_docs = 0
    print("\n--- Creating Indices & Seeding Documents ---")
    for index_name, schema_file in SCHEMA_INDEX_MAP.items():
        schema_path = os.path.join(SCHEMAS_DIR, schema_file)
        if not os.path.exists(schema_path):
            print(f"  [WARN] Schema not found: {schema_file} — skipping {index_name}")
            continue
        create_index(es, index_name, schema_file)
        docs = SAMPLE_DOCS.get(index_name, [])
        if docs:
            total_docs += seed_documents(es, index_name, docs)
        else:
            print(f"  [INFO] No sample docs defined for '{index_name}'")

    print("\n--- Index Summary ---")
    for index_name in SCHEMA_INDEX_MAP:
        try:
            resp = es.count(index=index_name)
            count = resp.body["count"] if hasattr(resp, "body") else resp["count"]
            print(f"  {index_name:<35} docs: {count}")
        except Exception as e:
            print(f"  {index_name:<35} ERROR: {e}")

    print("\n" + "=" * 60)
    print(f"Seed complete. Total documents inserted: {total_docs}")
    print("=" * 60)


if __name__ == "__main__":
    main()
