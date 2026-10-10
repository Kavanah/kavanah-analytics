"""TikTok organic importer — Login Kit + Display API.

    python3 /app/python/import.py tiktok run_daily_imports --team_ids=<esid>[,<esid>]

Each run stores a dated snapshot per linked TikTok account (``account_daily``)
and per video (``video_daily``) in ``{team_esid}__tiktok_organic``. The Display
API only exposes lifetime counts, so daily change is computed against the
previous snapshot (``*_gained`` fields); history starts at the first import.
"""
