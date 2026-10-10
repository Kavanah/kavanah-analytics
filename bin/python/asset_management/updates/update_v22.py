"""seed the ``first_look_youtube`` dashboard template.

The YouTube connector's setup-time finalize (onboarding wizard AND the regular
Data Connectors flow) now runs the shared tokenless-finalize core, which
materialises the connector's first-look pack and — only when a pack installed —
kicks off the on-demand backfill. Without a ``first_look_youtube`` doc in
``de_dashboard_templates`` the pack lookup returns empty, first_look_status is
``no_pack``, and the kickoff is skipped — recreating the empty-dashboard bug
 fixes. This migration seeds that template, mirroring the four
 seeds in ``tools/migrate_first_look_to_templates.py``.

Chart fields match what the YouTube importer writes to ``*_youtube_analytics``
(see ``import_scripts/youtube/get_youtube_analytics.py``): ``views``,
``estimated_minutes_watched``, ``subscribers_gained``, ``likes``, with
``kibana_date`` as the date field and ``youtube_channel_id`` as the
placeholder / substitution field (the wizard substitutes the team's linked
channel ids at instantiate time).

Idempotent AND curator-safe: if a ``first_look_youtube`` doc already exists it
is left untouched (a curator may have edited it via the Save-as-template UI);
re-running is a no-op.

MANUAL DEPLOY STEP (CLAUDE.md): update_vN does NOT run from a git pull. Run it
by hand on each env after cutover and verify the template doc exists (the
script logs to /app/data/logs/system_<date>.log, not stdout):

    docker exec -w /app/python imports_container python3 \
        asset_management/updates/update_v22.py

    curl -s <es>/de_dashboard_templates/_doc/first_look_youtube | head -c 200
"""

import logging
import sys
import os
from datetime import datetime, timezone

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "../..")))

from lib.es_helpers import get_elasticsearch_client

current_date = datetime.now().strftime("%Y%m%d")
log_file = f"/app/data/logs/system_{current_date}.log"
logging.basicConfig(filename=log_file, level=logging.INFO, format="[update_v22] %(message)s")

TEMPLATES_INDEX = "de_dashboard_templates"
TEMPLATE_ID = "first_look_youtube"

# Same sentinel shape the migration wrote (template_service's
# ASSET_PLACEHOLDER tuple serialises to a JSON array; the substitution
# detector recognises both the array and bare-string forms).
ASSET_PLACEHOLDER = ["__FIRST_LOOK_ASSET_PLACEHOLDER__"]

DATA_VIEW = "*_youtube_analytics"
PLACEHOLDER_FIELD = "youtube_channel_id"


def _metric_chart(chart_id, title, field, col):
    return {
        "chart_id": chart_id,
        "chart_type": "metric",
        "title": title,
        "data_view": DATA_VIEW,
        "y_fields": [field],
        "aggregation": "sum",
        "filters": [
            {
                "field": PLACEHOLDER_FIELD,
                "op": "match_phrase_any",
                "value": ASSET_PLACEHOLDER,
            }
        ],
        "position": {"col": col, "row": 0, "w": 3, "h": 2},
        "hide_title": False,
    }


def _bar_chart(chart_id, title, field, col):
    return {
        "chart_id": chart_id,
        "chart_type": "bar",
        "title": title,
        "data_view": DATA_VIEW,
        "x_field": "kibana_date",
        "y_fields": [field],
        "aggregation": "sum",
        "filters": [
            {
                "field": PLACEHOLDER_FIELD,
                "op": "match_phrase_any",
                "value": ASSET_PLACEHOLDER,
            }
        ],
        "position": {"col": col, "row": 2, "w": 6, "h": 4},
        "hide_title": False,
    }


def build_template_doc():
    """The ``de_dashboard_templates`` doc, shaped exactly like the
    seeds (see tools/migrate_first_look_to_templates.py:_build_template_doc)."""
    now = datetime.now(timezone.utc).isoformat()
    return {
        "template_id": TEMPLATE_ID,
        "pack_id": "youtube",
        "connector_id": "youtube",
        "name": "YouTube — Views & engagement, last 30d",
        "description": "First-look YouTube dashboard.",
        "menu_title": "YouTube",
        "menu_position": 0,
        "required_sources": ["youtube"],
        "placeholder_field": PLACEHOLDER_FIELD,
        "echarts_config": {
            "global_date_range": None,
            "filters": [],
            "charts": [
                _metric_chart("fl_yt_views_total", "Views (last 30d)", "views", 0),
                _metric_chart("fl_yt_watch_minutes_total", "Watch time (minutes)", "estimated_minutes_watched", 3),
                _metric_chart("fl_yt_subs_gained_total", "Subscribers gained", "subscribers_gained", 6),
                _metric_chart("fl_yt_likes_total", "Likes", "likes", 9),
                _bar_chart("fl_yt_views_daily", "Daily views", "views", 0),
                _bar_chart("fl_yt_watch_minutes_daily", "Daily watch time (minutes)", "estimated_minutes_watched", 6),
            ],
        },
        "frozen_from_dashboard_id": None,
        "created_at": now,
        "updated_at": now,
    }


def seed_youtube_template(es):
    """Write first_look_youtube unless a doc with that id already exists."""
    try:
        if es.exists(index=TEMPLATES_INDEX, id=TEMPLATE_ID):
            logging.info(f"{TEMPLATE_ID} already exists in {TEMPLATES_INDEX}; leaving it untouched.")
            return "success"
    except Exception as e:
        logging.error(f"Error checking for existing {TEMPLATE_ID}: {e}")
        return "abort"

    try:
        es.index(
            index=TEMPLATES_INDEX,
            id=TEMPLATE_ID,
            document=build_template_doc(),
            refresh="wait_for",
        )
        logging.info(f"Seeded {TEMPLATE_ID} into {TEMPLATES_INDEX}.")
    except Exception as e:
        logging.error(f"Error seeding {TEMPLATE_ID}: {e}")
        return "abort"

    return "success"


def run_update():
    es = get_elasticsearch_client()
    if es is None:
        logging.error("Failed to connect to Elasticsearch")
        return "abort"

    logging.info("Starting update_v22:  seed first_look_youtube template")

    if seed_youtube_template(es) == "abort":
        logging.error("Update aborted (template seed)")
        return "abort"

    logging.info("Update_v22 completed successfully")
    return "success"


if __name__ == "__main__":
    run_update()
