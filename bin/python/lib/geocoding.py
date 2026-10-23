import json
import os
import re
import time
import logging

CACHE_FILE = "/app/data/geocoding_cache.json"
_cache = None
_cache_mtime = None


def _load_cache():
    global _cache, _cache_mtime
    if _cache is None:
        if os.path.exists(CACHE_FILE):
            with open(CACHE_FILE) as f:
                _cache = json.load(f)
            try:
                _cache_mtime = os.path.getmtime(CACHE_FILE)
            except OSError:
                _cache_mtime = None
        else:
            _cache = {}
            _cache_mtime = None
    return _cache


def _save_cache():
    global _cache_mtime
    os.makedirs(os.path.dirname(CACHE_FILE), exist_ok=True)
    with open(CACHE_FILE, "w") as f:
        json.dump(_cache, f)
    try:
        _cache_mtime = os.path.getmtime(CACHE_FILE)
    except OSError:
        _cache_mtime = None


def reload_cache_if_changed():
    """Re-read the cache file if its mtime has changed since the last load.

    The geocode cache is per-Python-process. In a multi-worker gunicorn
    setup the worker handling a request may be holding a stale snapshot
    if the prewarm script (or another worker) just wrote new entries.
    Calling this once at the start of a batch of cache-only lookups
    keeps the in-memory cache aligned with disk without paying the cost
    on every individual lookup.
    """
    global _cache, _cache_mtime
    if not os.path.exists(CACHE_FILE):
        return
    try:
        disk_mtime = os.path.getmtime(CACHE_FILE)
    except OSError:
        return
    if _cache is None or _cache_mtime is None or disk_mtime > _cache_mtime:
        try:
            with open(CACHE_FILE) as f:
                _cache = json.load(f)
            _cache_mtime = disk_mtime
        except (OSError, json.JSONDecodeError) as e:
            logging.warning(f"[geocoding] Failed to reload cache: {e}")


def lookup_cached_city(city_name, region="", country_code=""):
    """Return cached coords for a city or None — never calls Nominatim.

    This is the request-path counterpart to ``geocode_city``: it consults
    only the on-disk cache populated by the import / prewarm path. Used by
    the progressive map render so a cold cache or a slow Nominatim never
    blocks the dashboard response.
    """
    cache = _load_cache()
    key = f"city|{city_name}|{region}|{country_code}"
    return cache.get(key)


_NOMINATIM_RETRY_DELAYS = [5, 10, 20]  # backoff schedule for HTTP 429s


# FB Graph's targeting cities API returns verbose admin1 labels
# ("Ankara Province", "Punjab region", "Rakhine State") that Nominatim does
# not match. Strip those suffix/prefix tokens before sending to Nominatim.
#
# KEEP IN SYNC with `_stripAdminSuffix` in
# bin/python/asset_management/web/static/dashboard_builder/charts.js — the
# JS side strips the same noise from FB region names before matching against
# admin1.geojson on the dashboard map. Diverging lists produce inconsistent
# matches between the import path (here) and the render path (charts.js).
_REGION_SUFFIX_TOKENS = (
    "Autonomous Region",
    "County",
    "Counties",
    "Department",
    "Departments",
    "District",
    "Districts",
    "Division",
    "Divisions",
    "Emirate",
    "Governorate",
    "Governorates",
    "Metropolitan City",
    "Municipality",
    "Oblast",
    "Prefecture",
    "Prefectures",
    "Province",
    "Provinces",
    "Region",
    "Regions",
    "Republic",
    "Special Region",
    "State",
    "States",
    "Territory",
    "Territories",
    "Voivodeship",
)
_REGION_PREFIX_TOKENS = (
    "District of",
    "Emirate of",
    "Governorate of",
    "Kingdom of",
    "Prefecture of",
    "Province of",
    "Region of",
    "Republic of",
    "State of",
    "Territory of",
)


def _normalize_region(region):
    """Strip noise tokens FB Graph adds to admin1 names so Nominatim can match.

    "Ankara Province" → "Ankara"; "Punjab region" → "Punjab";
    "Province of Ontario" → "Ontario". Case-insensitive on the token, but
    preserves the surviving region's original casing.
    """
    if not region:
        return ""
    cleaned = region.strip()
    if not cleaned:
        return ""

    for prefix in _REGION_PREFIX_TOKENS:
        if cleaned.lower().startswith(prefix.lower() + " "):
            cleaned = cleaned[len(prefix) + 1 :].strip()
            break

    for suffix in _REGION_SUFFIX_TOKENS:
        lowered = cleaned.lower()
        suf_lower = " " + suffix.lower()
        if lowered.endswith(suf_lower):
            cleaned = cleaned[: -len(suf_lower)].strip()
            break

    return cleaned


def geocode_city(city_name, region="", country_code=""):
    """
    Resolve a city name to {"lat": float, "lon": float} using Nominatim.
    Cached in CACHE_FILE with key prefix 'city|'.
    Returns None if the city cannot be geocoded.

    retries with exponential backoff (5s → 10s → 20s) on
    Nominatim Varnish 429 / GeocoderUnavailable. After the final retry,
    falls through to the broader exception handler and returns None.

    the region string is normalized via :func:`_normalize_region`
    to drop verbose admin1 suffixes ("Punjab region" → "Punjab"). When the
    normalized region still produces no Nominatim match, fall back once to
    a city-only lookup scoped by ``country_codes``. The cache key keeps
    the original (city, region, country_code) so callers reusing the same
    triplet hit the cache even though the wire query differs.
    """
    cache = _load_cache()
    key = f"city|{city_name}|{region}|{country_code}"
    if key in cache:
        return cache[key]

    from geopy.geocoders import Nominatim

    try:
        from geopy.exc import GeocoderUnavailable
    except ImportError:  # very old geopy fallback
        GeocoderUnavailable = Exception  # type: ignore[assignment]

    geocoder = Nominatim(user_agent="digital-engagement-dashboard")

    # Use country_codes as a filter rather than embedding the ISO2 in the
    # query string — Nominatim doesn't reliably match "Chicago, US" but
    # does match "Chicago" with country_codes=us.
    kwargs = {"timeout": 10}
    if country_code:
        kwargs["country_codes"] = country_code.lower()

    normalized_region = _normalize_region(region)
    primary_query = ", ".join(filter(None, [city_name, normalized_region]))
    fallback_query = city_name if normalized_region else None

    def _attempt(query):
        """Run the Nominatim call with the retry budget. Returns
        (coords_or_None, gave_up_due_to_429: bool)."""
        coords = None
        gave_up = False
        last_error = None
        for attempt, delay in enumerate([0] + _NOMINATIM_RETRY_DELAYS):
            if delay:
                logging.info(
                    f"[geocoding] Retry {attempt}/{len(_NOMINATIM_RETRY_DELAYS)} for "
                    f"'{city_name}' after {delay}s backoff (last error: {last_error})"
                )
                time.sleep(delay)
            else:
                time.sleep(1)  # Nominatim rate limit: 1 req/sec on first attempt

            try:
                loc = geocoder.geocode(query, **kwargs)
                if loc:
                    coords = {"lat": loc.latitude, "lon": loc.longitude}
                break
            except GeocoderUnavailable as e:
                last_error = e
                # Retry — Nominatim's Varnish layer 429s aggressively under load.
                continue
            except Exception as e:
                logging.warning(f"[geocoding] Error geocoding city '{city_name}': {e}")
                break
        else:
            gave_up = True
            logging.warning(
                f"[geocoding] Gave up on '{city_name}' after "
                f"{len(_NOMINATIM_RETRY_DELAYS)} retries (last error: {last_error})"
            )
        return coords, gave_up

    result, gave_up = _attempt(primary_query)
    if result is None and not gave_up and fallback_query and fallback_query != primary_query:
        # try (city, country_codes) without the region qualifier.
        logging.info(
            f"[geocoding] Region-qualified lookup empty for '{primary_query}'; "
            f"retrying as '{fallback_query}' with country_codes={country_code!r}"
        )
        result, _ = _attempt(fallback_query)

    if result is None:
        logging.warning(f"[geocoding] Could not geocode city: {city_name}, {region}, {country_code}")

    # Only cache successful results — failed lookups should be retried.
    if result is not None:
        cache[key] = result
        _save_cache()
    return result


# ISO2 → centroid coordinates, loaded from country_centroids.json.
#
# this table used to be a hand-maintained dict literal here while
# the team-form Countries dropdown was seeded independently from
# iso_countries.json (update_v16) — two sources of truth that drifted
#. The centroids now live in country_centroids.json, a
# sibling of iso_countries.json, and the loader below asserts at import time
# that every ISO code in iso_countries.json has a centroid. Adding a country
# to the dialog without adding its centroid fails loudly here instead of the
# geocoder silently dropping it from the Meta map.
#
# The centroids file may carry extra non-ISO codes (currently XK/Kosovo — a
# valid Facebook targeting code intentionally absent from the ISO list).
_DATA_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "asset_management", "updates")
_ISO_COUNTRIES_PATH = os.path.join(_DATA_DIR, "iso_countries.json")
_COUNTRY_CENTROIDS_PATH = os.path.join(_DATA_DIR, "country_centroids.json")


def _load_country_centroids(
    centroids_path: str = _COUNTRY_CENTROIDS_PATH,
    iso_path: str = _ISO_COUNTRIES_PATH,
) -> dict[str, dict[str, float]]:
    """Build the ISO2 → {lat, lon} centroid table from country_centroids.json.

    Raises RuntimeError when any code in iso_countries.json lacks a centroid,
    so drift between the two files is caught at import time in every
    environment rather than surfacing as countries silently missing from the
    Meta map (the failure mode).
    """
    with open(centroids_path, encoding="utf-8") as fh:
        raw = json.load(fh)
    centroids = {code: {"lat": float(point["lat"]), "lon": float(point["lon"])} for code, point in raw.items()}

    with open(iso_path, encoding="utf-8") as fh:
        iso_entries = json.load(fh)
    missing = sorted({entry["country_iso3_code"] for entry in iso_entries} - set(centroids))
    if missing:
        raise RuntimeError(
            f"country_centroids.json is missing {len(missing)} ISO code(s) that "
            f"iso_countries.json makes selectable in the team dialog: {missing!r}. "
            f"Add a centroid for each to {centroids_path} — without one the "
            f"geocoder silently drops that country from the Meta map."
        )
    return centroids


_COUNTRY_CENTROIDS = _load_country_centroids()


def country_from_coords(lat, lon):
    """Return the ISO2 country code nearest to the given lat/lon.

    Uses a simple squared-distance comparison against country centroids.
    Accurate enough for city-level points (not border cases).
    Returns empty string if no centroids are available.
    """
    best_code = ""
    best_dist = float("inf")
    for code, centroid in _COUNTRY_CENTROIDS.items():
        dlat = lat - centroid["lat"]
        dlon = lon - centroid["lon"]
        dist = dlat * dlat + dlon * dlon
        if dist < best_dist:
            best_dist = dist
            best_code = code
    return best_code


def geocode_all_targeting_levels(targeting, cache_only=False):
    """Geocode adset targeting geo_locations at city, region, and country levels.

    when ``cache_only=True`` the city- and region-level lookups
    consult the on-disk cache ONLY (via :func:`lookup_cached_city`) and never
    invoke Nominatim. A cold miss yields no entry — silently skipped, not an
    error. This is the mode the Facebook importer's hot path uses so a cold
    cache or a new team never blocks on Nominatim's 1 req/sec wall. The
    offline-centroid levels (custom_locations, geo_markets, countries) are
    unaffected by this flag — they never touch the network. The async
    out-of-band population path re-enables live lookups through
    its own channel without reverting this switch.

    Facebook returns several different shapes inside
    ``geo_locations`` depending on how the advertiser scoped the targeting:

      - ``cities`` — explicit city picker; entries carry ``country`` (ISO2)
      - ``regions`` — state/province; entries carry ``country`` (ISO2)
      - ``countries`` — country picker; entries carry ``country_code``
      - ``custom_locations`` — radius around a lat/lon; entries carry
        ``country`` (ISO2) and ``latitude`` / ``longitude``
      - ``geo_markets`` — DMA-style market (e.g. "Houston, TX"); entries
        carry ``country`` (ISO2) and ``name``

    Pre-fix the geocoder only read the first three shapes, and even when
    it read ``cities`` it discarded the per-entry ``country`` code. Teams
    targeting via ``custom_locations`` or ``geo_markets`` (one team is)
    therefore had ``location_countries`` empty, and the Impact Report
    Geographic Reach section showed nothing.

    Post-fix: every shape contributes (a) its name/coordinates to the
    appropriate level (city/region/country) AND (b) its ISO2 country to
    a derived-country set. The final ``country`` list is the union of
    explicit ``geo_locations.countries`` and derived countries — so
    even an adset that only targets two cities in Turkey ends up with
    ``location_countries=[{iso2: TR, ...}]``.
    """
    if not targeting or not isinstance(targeting, dict):
        return {}

    geo = targeting.get("geo_locations", {})
    result = {}

    # in cache-only mode, resolve cities/regions from the on-disk
    # cache only — never call Nominatim. lookup_cached_city uses the same
    # cache key as geocode_city, so warmed entries still hit.
    _lookup = lookup_cached_city if cache_only else geocode_city

    # Track every ISO2 we've seen across all shapes so we can fill in
    # ``location_countries`` even when the advertiser didn't pick at the
    # country level. Order is preserved by insertion (Python dict).
    derived_countries = {}

    def _record_country(code):
        """Add an ISO2 to derived_countries if it has a known centroid."""
        if not code:
            return
        code_upper = str(code).upper()
        if code_upper in derived_countries:
            return
        centroid = _COUNTRY_CENTROIDS.get(code_upper)
        if centroid:
            derived_countries[code_upper] = centroid

    # Cities
    city_locs = []
    for city in geo.get("cities", []):
        name = city.get("name", "")
        country = city.get("country", "")
        _record_country(country)
        if name:
            point = _lookup(name, city.get("region", ""), country)
            if point:
                city_locs.append({"name": name, "coordinates": point})
    if city_locs:
        result["city"] = city_locs

    # Regions (state/province level) — geocode via Nominatim
    region_locs = []
    for region in geo.get("regions", []):
        name = region.get("name", "")
        country = region.get("country", "")
        _record_country(country)
        if name:
            point = _lookup(name, "", country)
            if point:
                region_locs.append({"name": name, "coordinates": point, "country_code": country})
    if region_locs:
        result["region"] = region_locs

    # Custom locations (radius around lat/lon) — every adset for one team uses
    # these. The lat/lon is authoritative; the country field is a hint.
    # Emit them as cities so the city-level map dots still render, and
    # contribute the country to derived_countries.
    for cl in geo.get("custom_locations", []):
        if not isinstance(cl, dict):
            continue
        country = cl.get("country", "")
        _record_country(country)
        lat = cl.get("latitude")
        lon = cl.get("longitude")
        if lat is None or lon is None:
            continue
        # Custom locations don't carry a city name — synthesise one from
        # the country code so the city list still has a label, and so
        # multiple custom-location dots in the same country don't all
        # collapse onto a single map marker.
        name = cl.get("name")
        if not name:
            name = f"Custom location ({country})" if country else "Custom location"
        city_locs.append({"name": name, "coordinates": {"lat": lat, "lon": lon}})
    if city_locs and "city" not in result:
        result["city"] = city_locs

    # Geo markets (DMA-style: "Houston, TX") — name-only, no lat/lon.
    # Don't try to geocode the market name (it's not a real city); just
    # contribute the country.
    for gm in geo.get("geo_markets", []):
        if not isinstance(gm, dict):
            continue
        _record_country(gm.get("country", ""))

    # Countries — use centroid lookup (no Nominatim calls needed). Start
    # with explicit picks, then union in everything we derived above.
    country_locs = []
    seen = set()
    for country in geo.get("countries", []):
        code = country.get("country_code", "") if isinstance(country, dict) else str(country)
        code_upper = code.upper()
        centroid = _COUNTRY_CENTROIDS.get(code_upper)
        if centroid and code_upper not in seen:
            country_locs.append({"name": code_upper, "iso2": code_upper, "coordinates": centroid})
            seen.add(code_upper)
    for code_upper, centroid in derived_countries.items():
        if code_upper in seen:
            continue
        country_locs.append({"name": code_upper, "iso2": code_upper, "coordinates": centroid})
        seen.add(code_upper)
    if country_locs:
        result["country"] = country_locs

    return result


# words that mark an address component as street-level (a street,
# a building, a unit, a neighbourhood block) rather than a town, district,
# region or country. Matched as whole lower-cased tokens after stripping
# trailing punctuation. Deliberately broad: a dropped component only costs map
# precision, a kept one sends part of a convert's street to a third party.
# fmt: off
_STREET_TOKENS = frozenset(
    {
        # English
        "street", "st", "str", "road", "rd", "avenue", "ave", "av", "lane", "ln", "drive", "dr",
        "boulevard", "blvd", "way", "close", "court", "ct", "place", "pl", "terrace", "crescent",
        "highway", "hwy", "alley", "apt", "apartment", "apartments", "suite", "unit", "flat",
        "floor", "building", "bldg", "block", "house", "box", "po", "park", "compound", "villa",
        # Turkish
        "cad", "cd", "cadde", "caddesi", "sok", "sk", "sokak", "sokağı", "sokagi", "mah", "mh",
        "mahalle", "mahallesi", "bulvar", "bulvarı", "bulvari", "apartmanı", "apartmani",
        "daire", "kat", "blok", "no", "site", "sitesi", "parkı", "parki", "çıkmazı", "cikmazi",
        # French / Spanish / Portuguese / Italian / German
        "rue", "chemin", "impasse", "allée", "allee", "calle", "avenida", "carrera", "barrio",
        "rua", "travessa", "via", "viale", "piazza", "vicolo", "straße", "strasse", "weg",
        "gasse", "platz",
        # Indonesian / Malay
        "jalan", "jl", "jln", "gang", "gg", "rt", "rw", "lorong",
        # Russian
        "улица", "ул", "проспект", "пр", "переулок", "пер", "дом", "д", "кв", "квартира",
        # Arabic / Persian / Urdu / Hebrew
        "شارع", "زقاق", "عمارة", "شقة", "خیابان", "خيابان", "کوچه", "كوچه", "پلاک", "گلی", "רחוב",
    }
)
# fmt: on
_COMPONENT_SPLIT = re.compile(r"[,;\n\r|،，、]+")  # incl. Arabic / CJK commas
_TOKEN_SPLIT = re.compile(r"[\s/\\\-–—()]+")
# A component of more words than this reads as a description, not a place name.
_MAX_PLACE_WORDS = 4
# Keep at most this many trailing components: "City, Country" grain.
_MAX_PLACE_COMPONENTS = 2


def _is_coarse_component(component):
    """True when an address component can be sent to a geocoder: no digit
    (house / unit numbers, postcodes, phone numbers), no ``@``, no street-level
    word, and short enough to be a place name."""
    if not component or any(ch.isdigit() for ch in component) or "@" in component or "#" in component:
        return False
    words = [w.strip(".:'\"`").lower() for w in _TOKEN_SPLIT.split(component) if w.strip(".:'\"`")]
    if not words or len(words) > _MAX_PLACE_WORDS:
        return False
    return not any(w in _STREET_TOKENS for w in words)


def coarse_query_for_address(address):
    """reduce a free-text address to a coarse query LOCALLY, or "".

    Splits on commas / semicolons / newlines, drops every street-level
    component (see :func:`_is_coarse_component`) and keeps at most the last
    two survivors — the ``City, Country`` end of an address. Nothing here
    touches the network; this is what makes "the street never leaves the box"
    true for :func:`coarse_location_for_address`.

    "Mevlana Sk. 12, Karatay, Konya" → "Karatay, Konya";
    "Atatürk Caddesi 5" → "" (nothing coarse left, so nothing is geocoded).
    """
    parts = [p.strip() for p in _COMPONENT_SPLIT.split(address or "")]
    kept = [p for p in parts if _is_coarse_component(p)]
    return ", ".join(kept[-_MAX_PLACE_COMPONENTS:])


def coarse_location_for_address(address):
    """reduce a free-text street address to ``{"place": "City, Country",
    "country": "Country", "lat": float, "lon": float}`` via Nominatim, or None.

    the street never leaves this host. The address is first reduced
    locally by :func:`coarse_query_for_address`; only that coarse string is sent
    to Nominatim and used as the cache key (``place|<coarse>``), so neither the
    geocoder nor ``geocoding_cache.json`` ever sees the street, and lat/lon are
    the coarse place's, not the street's. Rate-limited to Nominatim's 1 req/s;
    only successes cached.
    """
    query = coarse_query_for_address(address)
    if not query:
        return None
    cache = _load_cache()
    key = f"place|{query}"
    if key in cache:
        return cache[key]
    from geopy.geocoders import Nominatim

    try:
        from geopy.exc import GeocoderUnavailable
    except ImportError:  # very old geopy fallback
        GeocoderUnavailable = Exception  # type: ignore[assignment]
    geocoder = Nominatim(user_agent="digital-engagement-dashboard")
    result = None
    last_error = None
    for attempt, delay in enumerate([0] + _NOMINATIM_RETRY_DELAYS):
        time.sleep(delay or 1)
        try:
            loc = geocoder.geocode(query, addressdetails=True, timeout=10)
        except GeocoderUnavailable as e:
            last_error = e
            continue
        except Exception as e:
            logging.warning(f"[geocoding] Error geocoding a coarse place (len={len(query)}): {type(e).__name__}")
            break
        if loc:
            addr = (getattr(loc, "raw", {}) or {}).get("address", {}) or {}
            city = (
                addr.get("city")
                or addr.get("town")
                or addr.get("village")
                or addr.get("municipality")
                or addr.get("county")
                or addr.get("state")
                or ""
            )
            country = addr.get("country") or ""
            if country:
                result = {
                    "place": ", ".join(x for x in (city, country) if x),
                    "country": country,
                    "lat": loc.latitude,
                    "lon": loc.longitude,
                }
        break
    else:
        logging.warning(
            f"[geocoding] Gave up on an address after {len(_NOMINATIM_RETRY_DELAYS)} retries (last error: {last_error})"
        )
    if result is not None:
        cache[key] = result
        _save_cache()
    return result
