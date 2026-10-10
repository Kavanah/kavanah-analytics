"""
YouTube Analytics Import Module

This module fetches organic YouTube channel metrics via YouTube Analytics API.

Modules:
- get_youtube_analytics: Fetch channel analytics data
- send_json_to_elastic: Index JSON files to Elasticsearch
- archive_imported_json: Move processed files to archive
- run_daily_imports: Orchestrate full import workflow

Authentication:
- Service account authentication (JSON key file)
- Requires YouTube channel owner to grant service account access

Data Flow:
1. Fetch analytics from YouTube Analytics API
2. Save as JSON files organized by channel/team
3. Index to Elasticsearch (team-specific indices)
4. Archive processed files

Configuration:
- Service account: /app/reporting_configs/youtube-analytics-sa.json
- Assets: de_asset_youtube_channels (Elasticsearch)
- Output: /app/data/youtube_analytics/
"""

__version__ = "1.0.0"
__author__ = "Digital Engagement Dashboard"
