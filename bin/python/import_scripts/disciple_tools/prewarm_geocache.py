"""
Pre-warm the DT geocode cache used by the map dashboard.

Queries all unique meta.location_grid and meta.contact_place values from
the disciple-tools index, resolves each one to (lat, lon, country) via
Nominatim, and writes the results to /tmp/dt_geocode_cache.json — the same
file the Flask map endpoint reads from.

only coarse strings are ever sent or cached here — DT location-grid
names and ``meta.contact_place`` ("City, Country"). This used to
aggregate ``meta.contact_address`` (the street), which removed from the
index; the map endpoint has looked up ``meta.contact_place`` keys since then,
so the place is what must be warmed.

Because this runs in the background (cron / promote_staging), the 1-req/sec
Nominatim rate limit is acceptable.  The map endpoint itself never calls
Nominatim — it only reads from this cache.

Can be run standalone:
    python3 /app/python/import.py disciple_tools prewarm_geocache

Or called programmatically from promote_staging.py:
    from import_scripts.disciple_tools.prewarm_geocache import prewarm
    prewarm(es, index_name="disciple-tools")
"""

import json
import logging
import os
import re
import time

import requests as http_requests

import lib.logging

GEOCODE_CACHE_PATH = "/tmp/dt_geocode_cache.json"
DT_GRID_NAMES_CACHE = "/tmp/dt_grid_names.json"
NOMINATIM_URL = "https://nominatim.openstreetmap.org/search"
NOMINATIM_HEADERS = {"User-Agent": "DEAM-Dashboard/1.0"}


def _load_geocode_cache():
    """Load the existing geocode cache from disk."""
    if os.path.exists(GEOCODE_CACHE_PATH):
        try:
            with open(GEOCODE_CACHE_PATH) as f:
                return json.load(f)
        except Exception:
            pass
    return {}


def _save_geocode_cache(cache):
    """Persist the geocode cache to disk."""
    try:
        with open(GEOCODE_CACHE_PATH, "w") as f:
            json.dump(cache, f)
    except Exception as e:
        logging.warning(f"Failed to save geocode cache: {e}")


def _load_dt_grid_names():
    """Load the DT location grid ID → place-name mapping.

    Downloads from GitHub on first call, caches to disk.
    """
    if os.path.exists(DT_GRID_NAMES_CACHE):
        try:
            with open(DT_GRID_NAMES_CACHE) as f:
                return json.load(f)
        except Exception:
            pass

    grid_names = {}
    try:
        resp = http_requests.get(
            "https://raw.githubusercontent.com/DiscipleTools/location-grid-project/master/v1/hierarchy.txt",
            timeout=60,
        )
        pattern = re.compile(r"[-\s]*(.*?) \((\d+)\)")
        for line in resp.text.split("\n"):
            m = pattern.search(line)
            if m:
                grid_names[m.group(2)] = m.group(1).strip()
        with open(DT_GRID_NAMES_CACHE, "w") as f:
            json.dump(grid_names, f)
        logging.info(f"DT grid names loaded: {len(grid_names)} entries")
    except Exception as e:
        logging.warning(f"DT grid name lookup load failed: {e}")
    return grid_names


def _geocode_one(address, cache, last_call_time):
    """Geocode a single address via Nominatim, respecting rate limits.

    Returns updated last_call_time.  Result is stored directly in cache.
    """
    if not address or address in cache:
        return last_call_time

    elapsed = time.time() - last_call_time
    if elapsed < 1.0:
        time.sleep(1.0 - elapsed)

    try:
        resp = http_requests.get(
            NOMINATIM_URL,
            params={"q": address, "format": "json", "limit": 1, "addressdetails": "1"},
            headers=NOMINATIM_HEADERS,
            timeout=5,
        )
        last_call_time = time.time()
        results = resp.json()
        if results:
            lat = float(results[0]["lat"])
            lon = float(results[0]["lon"])
            country = results[0].get("address", {}).get("country", "")
            cache[address] = [lat, lon, country]
        else:
            cache[address] = None
    except Exception as e:
        logging.warning(f'Geocode failed for "{address}": {e}')
        cache[address] = None
        last_call_time = time.time()

    return last_call_time


def _get_unique_addresses(es, index_name):
    """Query the DT index for all unique location_grid and contact_place values.

    the coarse place, never a street field (see module docstring)."""
    body = {
        "size": 0,
        "aggs": {
            "by_grid": {"terms": {"field": "meta.location_grid.keyword", "size": 5000}},
            "by_address": {"terms": {"field": "meta.contact_place.keyword", "size": 5000}},
        },
    }
    try:
        resp = es.search(index=index_name, body=body)
        resp_dict = resp.body if hasattr(resp, "body") else dict(resp)
    except Exception as e:
        logging.warning(f"Failed to query DT addresses from {index_name}: {e}")
        return [], []

    grid_ids = [b["key"] for b in resp_dict.get("aggregations", {}).get("by_grid", {}).get("buckets", [])]
    addresses = [b["key"] for b in resp_dict.get("aggregations", {}).get("by_address", {}).get("buckets", [])]
    return grid_ids, addresses


def prewarm(es, index_name="disciple-tools"):
    """Pre-warm the geocode cache for all DT contacts in the given index.

    Returns a dict with stats: total, already_cached, newly_geocoded, failed.
    """
    logging.info(f"Pre-warming geocode cache from {index_name}...")

    cache = _load_geocode_cache()
    grid_names = _load_dt_grid_names()

    grid_ids, raw_addresses = _get_unique_addresses(es, index_name)

    # Resolve grid IDs to place names
    grid_addresses = []
    for gid in grid_ids:
        name = grid_names.get(gid)
        if name:
            grid_addresses.append(name)
        else:
            logging.debug(f"No grid name for ID {gid}")

    # Combine and deduplicate
    all_addresses = list(set(grid_addresses + raw_addresses))
    total = len(all_addresses)

    already_cached = sum(1 for a in all_addresses if a in cache)
    to_geocode = [a for a in all_addresses if a not in cache]

    logging.info(f"Geocache: {total} unique addresses, {already_cached} already cached, " f"{len(to_geocode)} to geocode")

    if not to_geocode:
        logging.info("Geocode cache is fully warm — nothing to do")
        return {
            "total": total,
            "already_cached": already_cached,
            "newly_geocoded": 0,
            "failed": 0,
        }

    last_call = 0.0
    newly_geocoded = 0
    failed = 0

    for i, address in enumerate(to_geocode, 1):
        last_call = _geocode_one(address, cache, last_call)
        if cache.get(address) is not None:
            newly_geocoded += 1
        else:
            failed += 1

        if i % 50 == 0:
            logging.info(f"  Geocoded {i}/{len(to_geocode)}...")
            _save_geocode_cache(cache)

    _save_geocode_cache(cache)

    logging.info(
        f"Geocache pre-warm complete: {newly_geocoded} new, {failed} failed, " f"{already_cached} were already cached"
    )
    return {
        "total": total,
        "already_cached": already_cached,
        "newly_geocoded": newly_geocoded,
        "failed": failed,
    }


def main(params):
    """Entry point for import.py: python3 import.py disciple_tools prewarm_geocache"""
    lib.logging.define("dt-prewarm-geocache")

    from lib.es_helpers import get_elasticsearch_client

    es = get_elasticsearch_client()
    if not es:
        logging.error("Failed to connect to Elasticsearch")
        return "abort"

    index_name = params.get("index", "disciple-tools")
    stats = prewarm(es, index_name)

    if stats["newly_geocoded"] == 0 and stats["failed"] == 0:
        return "success"

    logging.info(f"Pre-warm stats: {stats}")
    return "success"
