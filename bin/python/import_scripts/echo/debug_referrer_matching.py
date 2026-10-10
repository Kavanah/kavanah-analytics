"""
Debug script for referrer-to-team matching.

Loads FB pages and YouTube channels from Elasticsearch, then runs every
matching strategy against the specified referrers without fetching any
conversations from the Echo API.

matching is tenant-scoped in production (per-tenant asset pools +
team dicts), so this tool requires --tenant_esid and debugs against the same
tenant-scoped pools the importer uses. The [DIAG] fallback queries remain
cluster-wide (read-only) so a miss can still be explained by "the page IS on
a team, but outside this tenant".

Usage:
    python3 import.py echo debug_referrer_matching --referrers "Example Page" --tenant_esid abc123

    # Multiple referrers:
    python3 import.py echo debug_referrer_matching --referrers "Example Page,Another Page" --tenant_esid abc123
"""

import re
import unicodedata
import logging

import lib.logging
from lib.es_helpers import get_elasticsearch_client
from import_scripts.echo.get_conversations import (
    _ZW_CHAR_RE,
    _normalize_for_matching,
    _clean_referrer,
    normalize_referrer,
    find_fb_page_with_fuzzy_matching,
    fetch_all_facebook_pages,
    fetch_all_youtube_channels,
    build_fb_pages_maps,
    build_yt_channels_map,
    get_tenant_team_maps,
)


def _find_close_keys(referrer, asset_map, top_n=10):
    """
    Show the closest keys in asset_map to the referrer so we can spot
    near-misses (e.g. extra space, different casing, Unicode difference).
    """
    norm_ref = _normalize_for_matching(referrer)
    cleaned_ref = _clean_referrer(referrer)
    norm_cleaned = _normalize_for_matching(cleaned_ref) if cleaned_ref else norm_ref

    candidates = []
    for key in asset_map:
        norm_key = _normalize_for_matching(key)
        # Score: length of longest common substring divided by max length
        ref_set = set(norm_cleaned or "")
        key_set = set(norm_key or "")
        if not ref_set or not key_set:
            continue
        overlap = len(ref_set & key_set) / max(len(ref_set), len(key_set))
        candidates.append((overlap, key))

    candidates.sort(reverse=True)
    return candidates[:top_n]


def _show_bytes(text):
    """Return hex codepoints for debugging Unicode surprises."""
    if not text:
        return "(empty)"
    return " ".join(f"U+{ord(c):04X}({c!r})" for c in text)


def debug_one_referrer(
    referrer, fb_pages_map, page_esid_to_fb_page_id, yt_channels_map, fb_page_id_to_team, yt_esid_to_team, es
):
    logging.info("=" * 60)
    logging.info(f"DEBUGGING REFERRER: {referrer!r}")
    logging.info(f"  Raw bytes: {_show_bytes(referrer)}")

    cleaned = _clean_referrer(referrer)
    logging.info(f"  After _clean_referrer: {cleaned!r}")
    logging.info(f"  After _normalize_for_matching: {_normalize_for_matching(cleaned)!r}")

    # --- Facebook pages ---
    logging.info("-" * 40)
    logging.info("STEP 1: Facebook page matching")
    fb_esid, fb_match_type, fb_matched_key = find_fb_page_with_fuzzy_matching(referrer, fb_pages_map)
    if fb_esid:
        logging.info(f"  [HIT]  ESID={fb_esid}, match_type={fb_match_type}, matched_key={fb_matched_key!r}")
        fb_page_id = page_esid_to_fb_page_id.get(fb_esid)
        logging.info(f"         fb_page_id={fb_page_id}")
        if fb_page_id and es:
            team, team_esid = fb_page_id_to_team.get(str(fb_page_id), (None, None))
            if team:
                logging.info(f"         Team: '{team.get('team_title')}' (ESID={team_esid})")
            else:
                logging.warning(f"         No team in this tenant for fb_page_id={fb_page_id}")
                # Diagnostic: search WITHOUT the Active status filter to see what's actually there
                try:
                    raw = es.search(
                        index="de_asset_teams",
                        body={"query": {"terms": {"team_facebook_page_id": [fb_page_id]}}},
                        size=5,
                    )
                    raw_hits = raw.get("hits", {}).get("hits", [])
                    if raw_hits:
                        logging.info(f"         [DIAG] Found {len(raw_hits)} team(s) ignoring status filter:")
                        for h in raw_hits:
                            src = h["_source"]
                            logging.info(
                                f"           ESID={h['_id']} "
                                f"title={src.get('team_title')!r} "
                                f"fb_page_ids={src.get('team_facebook_page_id')!r}"
                            )
                    else:
                        logging.warning(
                            f"         [DIAG] No team found even without status filter — fb_page_id type/value mismatch?"
                        )
                        # Try as integer in case it's stored as a number
                        try:
                            raw2 = es.search(
                                index="de_asset_teams",
                                body={"query": {"terms": {"team_facebook_page_id": [int(fb_page_id)]}}},
                                size=5,
                            )
                            raw2_hits = raw2.get("hits", {}).get("hits", [])
                            if raw2_hits:
                                logging.info(f"         [DIAG] Found {len(raw2_hits)} team(s) when querying as integer:")
                                for h in raw2_hits:
                                    src = h["_source"]
                                    logging.info(f"           ESID={h['_id']} title={src.get('team_title')!r}")
                            else:
                                logging.warning(
                                    f"         [DIAG] Also no match as integer. Check field name and value in de_asset_teams."
                                )
                        except (ValueError, Exception) as e2:
                            logging.error(f"         [DIAG] Integer query failed: {e2}")
                except Exception as e:
                    logging.error(f"         [DIAG] Diagnostic query failed: {e}")
    else:
        logging.warning(f"  [MISS] No FB page matched.")
        logging.info("  Closest keys in FB pages map:")
        for score, key in _find_close_keys(referrer, fb_pages_map):
            norm = _normalize_for_matching(key)
            logging.info(f"    score={score:.2f}  key={key!r}  (normalized: {norm!r})")
            logging.info(f"           bytes: {_show_bytes(key)}")

    # --- YouTube channels ---
    logging.info("-" * 40)
    logging.info("STEP 2: YouTube channel matching")
    yt_esid, yt_match_type, yt_matched_key = find_fb_page_with_fuzzy_matching(referrer, yt_channels_map)
    if yt_esid:
        logging.info(f"  [HIT]  ESID={yt_esid}, match_type={yt_match_type}, matched_key={yt_matched_key!r}")
        if es:
            team, team_esid = yt_esid_to_team.get(yt_esid, (None, None))
            if team:
                logging.info(f"         Team: '{team.get('team_title')}' (ESID={team_esid})")
            else:
                logging.warning(f"         No team in this tenant for YT channel ESID={yt_esid}")
    else:
        logging.warning(f"  [MISS] No YouTube channel matched.")
        logging.info("  Closest keys in YouTube channels map:")
        for score, key in _find_close_keys(referrer, yt_channels_map):
            norm = _normalize_for_matching(key)
            logging.info(f"    score={score:.2f}  key={key!r}  (normalized: {norm!r})")
            logging.info(f"           bytes: {_show_bytes(key)}")

    logging.info("=" * 60)


def main(params):
    lib.logging.define("echo-debug-matching")

    referrers_raw = params.get("referrers", "")
    if not referrers_raw:
        logging.error('No referrers specified. Use --referrers "Name 1,Name 2"')
        return

    referrers = [r.strip() for r in referrers_raw.split(",") if r.strip()]

    # production matching is tenant-scoped; debug against the same
    # per-tenant pools or the results are meaningless.
    tenant_esid = params.get("tenant_esid")
    if not tenant_esid or tenant_esid is True:
        logging.error("No tenant specified. Use --tenant_esid <esid> (matching is tenant-scoped).")
        return

    logging.info(f"Debugging {len(referrers)} referrer(s) in tenant {tenant_esid}: {referrers}")

    es = get_elasticsearch_client()
    if es is None:
        logging.error("Failed to connect to Elasticsearch.")
        return

    logging.info("Loading tenant team maps...")
    fb_page_id_to_team, yt_esid_to_team = get_tenant_team_maps(es, tenant_esid)

    logging.info("Loading Facebook pages...")
    fb_pages_map, page_esid_to_fb_page_id = build_fb_pages_maps(fetch_all_facebook_pages(es), set(fb_page_id_to_team))
    logging.info(f"  Loaded {len(fb_pages_map)} FB page keys")

    logging.info("Loading YouTube channels...")
    yt_channels_map = build_yt_channels_map(fetch_all_youtube_channels(es), set(yt_esid_to_team))
    logging.info(f"  Loaded {len(yt_channels_map)} YouTube channel keys")

    for referrer in referrers:
        debug_one_referrer(
            referrer,
            fb_pages_map,
            page_esid_to_fb_page_id,
            yt_channels_map,
            fb_page_id_to_team,
            yt_esid_to_team,
            es,
        )

    logging.info("Debug complete.")
