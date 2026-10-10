#!/usr/bin/env python3
"""Re-seed Kavanah Team data for indices that are missing or incomplete.

Disciple Tools is NOT re-seeded here: the legacy ``disciple-tools``
alias this script used to write is retired, and demo DT data
is owned by ``scripts/seed_kavanah_demo_dt.py --reseed`` (curated exact totals,
team-keyed index). ``--skip-dt`` is accepted as a no-op for back-compat;
``--dt-only`` errors and points at the seeder.

Usage (inside imports_container):
    python3 /app/python/scripts/reseed_kavanah.py --days 90
"""

import argparse
import sys
import os
import random
import logging
from datetime import date, datetime, timedelta

sys.path.insert(0, "/app/python")
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from lib.es_helpers import (
    build_data_index_name,
    create_index_if_not_exists,
    get_elasticsearch_client,
    put_regular_index_template,
)
from import_scripts.migration.generate_demo_data import (
    gen_fb_insights,
    gen_fb_page_insights,
    gen_google_ads,
    gen_google_ads_geo,
    gen_ga_metrics,
    gen_echo_conversations,
    gen_ig_insights,
    gen_youtube_analytics,
    gen_youtube_country_breakdown,
    gen_chatwoot_insights,
    gen_chatwoot_conversations,
    find_team,
    ensure_org_exists,
    load_schema_properties,
)

logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(levelname)s - %(message)s")
logger = logging.getLogger(__name__)


def index_docs(es, index_name, docs, mapping=None):
    """Index a list of documents, creating the index if needed."""
    if mapping:
        create_index_if_not_exists(es, index_name, mapping)
    for doc in docs:
        es.index(index=index_name, document=doc)


def main():
    parser = argparse.ArgumentParser(description="Re-seed Kavanah Team data.")
    parser.add_argument("--days", type=int, required=True, help="Number of days to generate")
    parser.add_argument(
        "--dt-only",
        action="store_true",
        help="REMOVED: use scripts/seed_kavanah_demo_dt.py --reseed instead",
    )
    parser.add_argument(
        "--skip-dt",
        action="store_true",
        help="DEPRECATED no-op: DT is never re-seeded here anymore",
    )
    args = parser.parse_args()

    if args.dt_only:
        logger.error(
            "--dt-only is removed. Demo DT data is owned by "
            "scripts/seed_kavanah_demo_dt.py --reseed (the legacy disciple-tools "
            "alias this script wrote is retired)."
        )
        sys.exit(2)
    if args.skip_dt:
        logger.warning("--skip-dt is a deprecated no-op: DT is never re-seeded here.")

    team_title = "Kavanah Team"
    org_title = "Kavanah"

    logger.info("Connecting to Elasticsearch...")
    es = get_elasticsearch_client()
    if es is None:
        logger.error("Failed to connect.")
        sys.exit(1)

    ensure_org_exists(es, org_title)
    team = find_team(es, team_title, org_title)
    if team is None:
        sys.exit(1)
    logger.info(f"Team: {team.get('team_title')} (esid: {team.get('team_esid')})")

    def _idx(source):
        # esid-form names like every importer; the slug-form
        # kavanah_team_* names were reverse aliases dropped them.
        return build_data_index_name(team["team_esid"], source)

    today = date.today()
    target_dates = [today - timedelta(days=i) for i in range(args.days - 1, -1, -1)]

    # Build source list. Every mapping comes from the unified schema files so a
    # reseeded index can't drift from the project_schema_* templates
    # (the old hand-maintained dicts mapped chatwoot *_at as plain
    # `date` and google_ads geo_criterion_id as integer).
    google_ads_mapping = load_schema_properties("unified_schema_google_ads")
    youtube_mapping = load_schema_properties("unified_schema_youtube_analytics")
    chatwoot_insights_mapping = load_schema_properties("unified_schema_chatwoot_insights")
    chatwoot_conversations_mapping = load_schema_properties("unified_schema_chatwoot_conversations")
    non_dt_sources = [
        (
            "Facebook Ads Insights",
            _idx("fb_insights"),
            load_schema_properties("unified_schema_fb_insights"),
            gen_fb_insights,
        ),
        (
            "Facebook Page Insights",
            _idx("fb_page_insights"),
            load_schema_properties("unified_schema_fb_page_insights"),
            gen_fb_page_insights,
        ),
        ("Google Ads", _idx("google_ads"), google_ads_mapping, gen_google_ads),
        ("Google Ads (Geo)", _idx("google_ads"), google_ads_mapping, gen_google_ads_geo),
        ("Google Analytics", _idx("ga_metrics"), load_schema_properties("unified_schema_ga_metrics"), gen_ga_metrics),
        (
            "Echo Conversations",
            _idx("echo_conversations"),
            load_schema_properties("unified_schema_echo_conversations"),
            gen_echo_conversations,
        ),
        ("Instagram Insights", _idx("ig_insights"), load_schema_properties("unified_schema_ig_insights"), gen_ig_insights),
        ("YouTube Analytics", _idx("youtube_analytics"), youtube_mapping, gen_youtube_analytics),
        ("YouTube (Country)", _idx("youtube_analytics"), youtube_mapping, gen_youtube_country_breakdown),
        ("Chatwoot Insights", _idx("chatwoot_insights"), chatwoot_insights_mapping, gen_chatwoot_insights),
        (
            "Chatwoot Conversations",
            _idx("chatwoot_conversations"),
            chatwoot_conversations_mapping,
            gen_chatwoot_conversations,
        ),
    ]

    # Ensure chatwoot templates exist (and carry the unified schema)
    put_regular_index_template(es, _idx("chatwoot_insights"), chatwoot_insights_mapping)
    put_regular_index_template(es, _idx("chatwoot_conversations"), chatwoot_conversations_mapping)

    sources = non_dt_sources

    random.seed()
    total_docs = 0

    for day_idx, td in enumerate(target_dates, 1):
        day_label = td.strftime("%Y-%m-%d")
        logger.info(f"Day {day_label} ({day_idx}/{len(target_dates)})")

        for name, index, mapping, generator in sources:
            try:
                result = generator(team, target_date=td)
                docs = result if isinstance(result, list) else [result]
                index_docs(es, index, docs, mapping)
                total_docs += len(docs)
            except Exception as e:
                logger.error(f"  {name}: FAILED - {e}")

    # Refresh all indices at the end instead of per-doc
    logger.info("Refreshing indices...")
    indices_to_refresh = set()
    for _, index, _, _ in sources:
        indices_to_refresh.add(index)
    for idx in indices_to_refresh:
        try:
            es.indices.refresh(index=idx)
        except Exception:
            pass

    logger.info(f"Done. {total_docs} documents indexed across {len(target_dates)} days.")


if __name__ == "__main__":
    main()
