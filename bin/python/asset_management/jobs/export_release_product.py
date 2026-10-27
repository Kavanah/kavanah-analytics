"""Export a digital-engagement release product for one organisation.

A *release product* is a single JSON file of aggregate counts at
**country x month** grain — one row per team x seeker country x month over
the live 24-month window — plus a manifest saying what it is, which
sources it was computed from, which countries the workspace owner chose to
publish, and which data-sharing agreement they accepted. Its contract is
``schemas/gc_release_product.schema.json`` (JSON Schema 2020-12); the
receiving side validates the same file.

What leaves: country x month counts and the manifest. What stays: every
conversation, contact, responder, campaign, ad, spend figure and anything
below country grain. Nothing in the file names a person.

Counting rules (each metric names the only documents it reads):

* ``touchpoints`` — Echo conversations (``{team}__echo_conversations``)
  counted by the seeker's ``contact_country_iso2`` and the month of
  ``kibana_date``. ``team_country`` is the team's own country and is never
  used. Chatwoot conversations carry no seeker country and are excluded.
* ``active`` — the same Echo conversations flagged ``spiritual_conversation``
  or ``multiplier``. Always <= touchpoints.
* ``exposed`` — Google Ads ``impressions`` from ``record_type: geo`` docs by
  ``geo_country_code`` + YouTube ``views`` from ``record_type:
  country_breakdown`` docs by ``country`` + Facebook ``insight_impressions``
  from ad sets whose ``adset_targeting.geo_locations`` names exactly ONE
  country.
* ``interactions`` — Google Ads ``clicks`` on the same geo docs (the
  importer's geographic_view query does not request ``metrics.clicks``
  today, so this term is 0 until it does) + GA4 ``sessions`` from
  ``report_type: geo_device`` docs by ``country`` (a name; resolved with the
  shared ``lib.country_codes`` resolver) + Facebook ``insight_clicks`` from
  the same single-country ad sets.

Facebook placement: where the importer has stored Meta's
per-country delivery breakdown (``{team}__fb_insights_country``, one doc per
ad x day x country), those ad-days are placed by the country the ads actually
delivered in — multi-country and worldwide ad sets included — and the
platform docs for the same ad-days are skipped so nothing is counted twice.
Only ad-days with NO breakdown rows (data imported before the breakdown
existed, or a run with it turned off) fall back to the targeting rule above:
a single-country ad set is placed, anything else is reported once in
``manifest.unattributed`` so the owner and the receiver know what the rows
do not contain.

Why ``kibana_date`` and not ``@timestamp``: every importer stamps
``@timestamp`` with the import instant (``datetime.now`` at send time), so
its month is the month of the import, not of the activity; a backfill would
land two years of conversations in one month. ``kibana_date`` is the ISO
``yyyy-MM-dd`` activity date every sender normalises (Echo derives it from
``created_at``, Google Ads / YouTube / GA4 from ``date``, Facebook from
``insight_date_start``).

Only the teams and countries the caller selected are read: the country
filter is in every query, so a country present in the data but not
selected never leaves Elasticsearch and is simply absent from the rows and
from ``countriesIncluded``. A team that does not belong to the organisation
is refused, not silently included.

A row never names a team. Its ``team`` value is an opaque label:
HMAC-SHA256 of the team esid keyed with the organisation's
``gc_team_label_salt``, truncated to 32 hex characters. The same team gets
the same label in every release of the same organisation, so the receiver
can replace earlier cells, and nothing in the file resolves to a title or
to the esid. The salt is minted when the owner opts in; without it the job
refuses.

The whole feature sits behind the ``GLOBAL_CHURCH_PUBLISH`` feature flag
(``FeatureFlagService``, OFF by default on every environment). While it is
off the job refuses before reading anything.

ISO 3166-1 alpha-3 is written beside alpha-2 because the receiver's
country registry resolves on alpha-3. It is resolved from
``de_asset_countries`` (whichever of ``country_iso`` / ``country_iso3_code``
holds a three-letter value — the seeded catalog's ``country_iso3_code``
actually holds the alpha-2 code, see ``updates/update_v16.py``), then from
the static table in ``lib.country_codes``, then from ``pycountry``. A row
whose alpha-3 cannot be resolved makes the run refuse rather than guess.

Dry-run by default: prints row count, country count, month range, the
unattributed totals and the path it WOULD write. ``--apply`` writes the
file. Never prints a document value or a team's figures — only counts.

Usage (inside the imports container)::

    python3 /app/python/scripts/export_release_product.py --org <org esid> --teams a,b --countries PK,IN
    python3 /app/python/scripts/export_release_product.py --org <org esid> --teams a,b --countries PK,IN --apply
"""

from __future__ import annotations

import hmac
import json
import logging
import os
import re
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Iterable

from asset_management.web.services.feature_flag_service import FLAG_GLOBAL_CHURCH_PUBLISH, FeatureFlagService
from lib.country_codes import _ISO3_TO_ISO2, resolve_country_to_iso2
from lib.es_helpers import build_data_index_name

LOG_PREFIX = "export-release-product"

EXPORTER_VERSION = "0.1.0"
SCHEMA_VERSION = "0.1.0"
GENERATED_BY = f"kavanah-analytics export_release_product {EXPORTER_VERSION}"
GRAIN = "country x month"
SUPPRESSION = "none"
ACCEPTED_BY_ROLE = "workspace-owner"

WINDOW_MONTHS = 24
DATE_FIELD = "kibana_date"
# ISO-2 cardinality is < 260; a terms agg this size never overflows.
TERMS_SIZE = 300
SCAN_PAGE = 1000

ORG_INDEX = "de_asset_organization"
TEAMS_INDEX = "de_asset_teams"
COUNTRIES_INDEX = "de_asset_countries"

DEFAULT_OUT_DIR = "/app/data/exports/release_product"

# Per-organisation settings read from ``de_asset_organization``
# (``schemas/asset_schema_organization.json``). The gateway key and URL
# are for the publish step, which is not part of this job.
SETTING_ORG_URI = "gc_org_uri"
SETTING_DISPLAY_NAMED = "gc_display_named"
SETTING_DISPLAY_NAME = "gc_display_name"
SETTING_AGREEMENT_ID = "gc_agreement_id"
SETTING_AGREEMENT_VERSION = "gc_agreement_version"
SETTING_AGREEMENT_SHA256 = "gc_agreement_sha256"
SETTING_AGREEMENT_ACCEPTED_AT = "gc_agreement_accepted_at"
# per-organisation secret that keys the opaque per-team label.
SETTING_TEAM_LABEL_SALT = "gc_team_label_salt"
# the owner's opt-in. Nothing is read, let alone released, while it is off.
SETTING_SHARING_ENABLED = "gc_sharing_enabled"
TEAM_LABEL_SALT_MIN_CHARS = 16
TEAM_LABEL_HEX_CHARS = 32
REQUIRED_SETTINGS = (
    SETTING_ORG_URI,
    SETTING_AGREEMENT_ID,
    SETTING_AGREEMENT_VERSION,
    SETTING_AGREEMENT_SHA256,
    SETTING_AGREEMENT_ACCEPTED_AT,
)

_ISO2_RE = re.compile(r"^[A-Z]{2}$")
_ISO3_RE = re.compile(r"^[A-Z]{3}$")
_MONTH_RE = re.compile(r"^[0-9]{4}-(0[1-9]|1[0-2])$")
_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
_ORG_URI_RE = re.compile(r"^https://data\.global\.church/org/[a-z0-9][a-z0-9-]*$")
# The receiver's contract for the opaque label (Global.Church, 2026-10-26).
_TEAM_LABEL_RE = re.compile(r"^[A-Za-z0-9_-]{8,64}$")

ISO2_TO_ISO3_STATIC: dict[str, str] = {iso2: iso3 for iso3, iso2 in _ISO3_TO_ISO2.items()}


class ExportRefused(Exception):
    """The run cannot produce a valid release; nothing was written."""


# ── Feature flag ──────────────────────────────────────────────────────────────


def publish_enabled() -> bool:
    """True only while the ``GLOBAL_CHURCH_PUBLISH`` flag is on (registry default: off)."""
    return FeatureFlagService.is_enabled(FLAG_GLOBAL_CHURCH_PUBLISH)


def _require_enabled() -> None:
    if not publish_enabled():
        raise ExportRefused(
            f"Global.Church publishing is off (feature flag {FLAG_GLOBAL_CHURCH_PUBLISH}); nothing was read or written"
        )


# ── Opaque team label ─────────────────────────────────────────────────────────


def team_label(salt: str, team_esid: str) -> str:
    """The opaque per-team value written in each row: HMAC-SHA256(salt, esid), 32 hex chars.

    Keyed per organisation so the same team cannot be matched across two
    organisations' releases, and stable within one so a later release
    replaces the earlier cells.
    """
    digest = hmac.new(salt.encode("utf-8"), team_esid.encode("utf-8"), "sha256").hexdigest()
    return digest[:TEAM_LABEL_HEX_CHARS]


def team_label_salt(org: dict) -> str:
    salt = str(org.get(SETTING_TEAM_LABEL_SALT) or "").strip()
    if len(salt) < TEAM_LABEL_SALT_MIN_CHARS:
        raise ExportRefused(
            f"organisation setting {SETTING_TEAM_LABEL_SALT} is missing or shorter than {TEAM_LABEL_SALT_MIN_CHARS} "
            "characters (it is minted when the owner opts in to Global.Church sharing)"
        )
    return salt


# ── Time window ───────────────────────────────────────────────────────────────


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _iso(dt: datetime) -> str:
    return dt.strftime("%Y-%m-%dT%H:%M:%SZ")


def _body(resp: Any) -> Any:
    """Unwrap an elasticsearch-py ObjectApiResponse (or pass a dict through)."""
    return resp.body if hasattr(resp, "body") else resp


def month_window(now: datetime | None = None, months: int = WINDOW_MONTHS) -> tuple[str, str]:
    """``(firstMonth, lastMonth)`` — the ``months`` calendar months ending with the current one (UTC)."""
    now = now or _now()
    last = f"{now.year:04d}-{now.month:02d}"
    y, m = now.year, now.month - (months - 1)
    while m <= 0:
        y -= 1
        m += 12
    return f"{y:04d}-{m:02d}", last


def _month_after(month: str) -> str:
    y, m = int(month[:4]), int(month[5:7])
    m += 1
    if m > 12:
        y, m = y + 1, 1
    return f"{y:04d}-{m:02d}"


def window_range(first_month: str, last_month: str) -> dict:
    """The one date filter every source query carries (ISO days, UTC)."""
    return {
        "range": {
            DATE_FIELD: {
                "gte": f"{first_month}-01",
                "lt": f"{_month_after(last_month)}-01",
                "format": "strict_date_optional_time",
            }
        }
    }


# ── Queries (pure, so tests can pin them) ─────────────────────────────────────


def _country_month_aggs(country_field: str, sums: dict[str, str], extra: dict | None = None) -> dict:
    per_month: dict[str, Any] = {name: {"sum": {"field": src}} for name, src in sums.items()}
    if extra:
        per_month.update(extra)
    return {
        "by_country": {
            "terms": {"field": country_field, "size": TERMS_SIZE},
            "aggs": {
                "by_month": {
                    "date_histogram": {
                        "field": DATE_FIELD,
                        "calendar_interval": "month",
                        "format": "yyyy-MM",
                        "min_doc_count": 1,
                        "time_zone": "UTC",
                    },
                    "aggs": per_month,
                }
            },
        }
    }


ACTIVE_FILTER = {
    "bool": {
        "should": [
            {"range": {"spiritual_conversation": {"gte": 1}}},
            {"range": {"multiplier": {"gte": 1}}},
        ],
        "minimum_should_match": 1,
    }
}


def echo_query(countries: list[str], first_month: str, last_month: str) -> dict:
    return {
        "size": 0,
        "query": {
            "bool": {
                "filter": [
                    {"terms": {"contact_country_iso2": countries}},
                    window_range(first_month, last_month),
                ]
            }
        },
        "aggs": _country_month_aggs("contact_country_iso2", {}, {"active": {"filter": ACTIVE_FILTER}}),
    }


def google_ads_query(countries: list[str], first_month: str, last_month: str) -> dict:
    return {
        "size": 0,
        "query": {
            "bool": {
                "filter": [
                    {"term": {"record_type": "geo"}},
                    {"terms": {"geo_country_code": countries}},
                    window_range(first_month, last_month),
                ]
            }
        },
        "aggs": _country_month_aggs("geo_country_code", {"impressions": "impressions", "clicks": "clicks"}),
    }


def youtube_query(countries: list[str], first_month: str, last_month: str) -> dict:
    return {
        "size": 0,
        "query": {
            "bool": {
                "filter": [
                    {"term": {"record_type": "country_breakdown"}},
                    {"terms": {"country": countries}},
                    window_range(first_month, last_month),
                ]
            }
        },
        "aggs": _country_month_aggs("country", {"views": "views"}),
    }


def ga_query(first_month: str, last_month: str) -> dict:
    """GA4 stores the country NAME, so the country filter is applied after resolution."""
    return {
        "size": 0,
        "query": {
            "bool": {
                "filter": [
                    {"term": {"report_type": "geo_device"}},
                    {"exists": {"field": "country"}},
                    window_range(first_month, last_month),
                ]
            }
        },
        "aggs": _country_month_aggs("country", {"sessions": "sessions"}),
    }


def facebook_query(first_month: str, last_month: str) -> dict:
    return {
        "query": {
            "bool": {
                "filter": [
                    {"exists": {"field": "adset_targeting"}},
                    window_range(first_month, last_month),
                ]
            }
        },
        "_source": [DATE_FIELD, "ad_id", "insight_impressions", "insight_clicks", "adset_targeting.geo_locations"],
    }


def facebook_country_query(first_month: str, last_month: str) -> dict:
    """every per-country delivery row in the window (NOT filtered by
    the selected countries — the ad-days it covers must be known in full so
    the platform scan can skip them)."""
    return {
        "query": {
            "bool": {
                "filter": [
                    {"term": {"record_type": "country_breakdown"}},
                    window_range(first_month, last_month),
                ]
            }
        },
        "_source": [DATE_FIELD, "ad_id", "country", "insight_impressions", "insight_clicks"],
    }


# ── Facebook targeting ────────────────────────────────────────────────────────


def targeted_countries(targeting: Any) -> set[str] | None:
    """The ISO-2 set an ad set's ``geo_locations`` names, or None when it cannot be reduced to countries.

    Every shape Meta uses contributes its country (``countries`` entries
    are bare codes or ``{country_code}``; ``regions`` / ``cities`` /
    ``custom_locations`` / ``geo_markets`` entries carry ``country``).
    ``country_groups`` ("worldwide", "asia" …) and an empty
    ``geo_locations`` cannot be placed and yield None.
    """
    if not isinstance(targeting, dict):
        return None
    geo = targeting.get("geo_locations")
    if not isinstance(geo, dict):
        return None
    if geo.get("country_groups"):
        return None
    found: set[str] = set()
    for entry in geo.get("countries") or []:
        code = entry.get("country_code") if isinstance(entry, dict) else entry
        if code:
            found.add(str(code).upper())
    for level in ("regions", "cities", "custom_locations", "geo_markets"):
        for entry in geo.get(level) or []:
            if isinstance(entry, dict) and entry.get("country"):
                found.add(str(entry["country"]).upper())
    return found or None


def single_target_country(targeting: Any) -> str | None:
    found = targeted_countries(targeting)
    if found and len(found) == 1:
        return next(iter(found))
    return None


# ── Aggregation ───────────────────────────────────────────────────────────────

Cell = tuple[str, str, str]  # (team label, iso2, month)


@dataclass
class Counts:
    rows: dict[Cell, dict[str, int]] = field(default_factory=dict)
    unattributed_exposed: int = 0
    unattributed_interactions: int = 0
    sources_used: set[str] = field(default_factory=set)
    ga_unresolved_names: int = 0
    overflow: bool = False

    def cell(self, team: str, iso2: str, month: str) -> dict[str, int]:
        return self.rows.setdefault((team, iso2, month), {"exposed": 0, "interactions": 0, "touchpoints": 0, "active": 0})


def _int(v: Any) -> int:
    return int(round(float(v or 0)))


def _walk_country_months(aggregations: dict) -> Iterable[tuple[str, str, dict]]:
    by_country = (aggregations or {}).get("by_country") or {}
    for cb in by_country.get("buckets") or []:
        for mb in ((cb.get("by_month") or {}).get("buckets")) or []:
            month = str(mb.get("key_as_string") or "")
            if _MONTH_RE.match(month):
                yield str(cb.get("key")), month, mb


def _overflowed(aggregations: dict) -> bool:
    return int((((aggregations or {}).get("by_country") or {}).get("sum_other_doc_count")) or 0) > 0


def _search_aggs(es, index: str, body: dict) -> dict | None:
    """Aggregations of one search, or None when the index does not exist."""
    if not es.indices.exists(index=index):
        return None
    return (_body(es.search(index=index, body=body)) or {}).get("aggregations") or {}


def _scan(es, index: str, body: dict) -> Iterable[dict]:
    from elasticsearch.helpers import scan

    yield from scan(es, index=index, query=body, size=SCAN_PAGE)


def count_team(es, counts: Counts, team_esid: str, team_label: str, countries: list[str], first: str, last: str) -> None:
    selected = set(countries)

    aggs = _search_aggs(es, build_data_index_name(team_esid, "echo_conversations"), echo_query(countries, first, last))
    if aggs is not None:
        counts.sources_used.add("echo")
        counts.overflow |= _overflowed(aggs)
        for iso2, month, mb in _walk_country_months(aggs):
            c = counts.cell(team_label, iso2, month)
            c["touchpoints"] += _int(mb.get("doc_count"))
            c["active"] += _int((mb.get("active") or {}).get("doc_count"))

    aggs = _search_aggs(es, build_data_index_name(team_esid, "google_ads"), google_ads_query(countries, first, last))
    if aggs is not None:
        counts.sources_used.add("google_ads")
        counts.overflow |= _overflowed(aggs)
        for iso2, month, mb in _walk_country_months(aggs):
            c = counts.cell(team_label, iso2, month)
            c["exposed"] += _int((mb.get("impressions") or {}).get("value"))
            c["interactions"] += _int((mb.get("clicks") or {}).get("value"))

    aggs = _search_aggs(es, build_data_index_name(team_esid, "youtube_analytics"), youtube_query(countries, first, last))
    if aggs is not None:
        counts.sources_used.add("youtube")
        counts.overflow |= _overflowed(aggs)
        for iso2, month, mb in _walk_country_months(aggs):
            counts.cell(team_label, iso2, month)["exposed"] += _int((mb.get("views") or {}).get("value"))

    aggs = _search_aggs(es, build_data_index_name(team_esid, "ga_metrics"), ga_query(first, last))
    if aggs is not None:
        counts.sources_used.add("ga4")
        counts.overflow |= _overflowed(aggs)
        for name, month, mb in _walk_country_months(aggs):
            iso2 = resolve_country_to_iso2(name)
            if iso2 is None:
                counts.ga_unresolved_names += 1
                continue
            if iso2 not in selected:
                continue
            counts.cell(team_label, iso2, month)["interactions"] += _int((mb.get("sessions") or {}).get("value"))

    # per-country delivery rows first: they place every ad-day they
    # cover (multi-country ad sets included) and mark those ad-days so the
    # platform scan below never counts them a second time.
    covered: set[tuple[str, str]] = set()
    fb_country_index = build_data_index_name(team_esid, "fb_insights_country")
    if es.indices.exists(index=fb_country_index):
        counts.sources_used.add("facebook_ads")
        for hit in _scan(es, fb_country_index, facebook_country_query(first, last)):
            src = hit.get("_source") or {}
            day = str(src.get(DATE_FIELD) or "")
            month = day[:7]
            if not _MONTH_RE.match(month):
                continue
            covered.add((str(src.get("ad_id") or ""), day))
            iso2 = str(src.get("country") or "").upper()
            if iso2 not in selected:
                continue
            c = counts.cell(team_label, iso2, month)
            c["exposed"] += _int(src.get("insight_impressions"))
            c["interactions"] += _int(src.get("insight_clicks"))

    fb_index = build_data_index_name(team_esid, "fb_insights")
    if es.indices.exists(index=fb_index):
        counts.sources_used.add("facebook_ads")
        for hit in _scan(es, fb_index, facebook_query(first, last)):
            src = hit.get("_source") or {}
            day = str(src.get(DATE_FIELD) or "")
            month = day[:7]
            if not _MONTH_RE.match(month):
                continue
            if (str(src.get("ad_id") or ""), day) in covered:
                continue
            impressions = _int(src.get("insight_impressions"))
            clicks = _int(src.get("insight_clicks"))
            iso2 = single_target_country(src.get("adset_targeting"))
            if iso2 is None:
                counts.unattributed_exposed += impressions
                counts.unattributed_interactions += clicks
                continue
            if iso2 not in selected:
                continue
            c = counts.cell(team_label, iso2, month)
            c["exposed"] += impressions
            c["interactions"] += clicks


# ── Reference data ────────────────────────────────────────────────────────────


def load_catalog_iso3(es) -> dict[str, str]:
    """ISO-2 -> ISO-3 from ``de_asset_countries``; only pairs where one field is 2 letters and the other 3."""
    out: dict[str, str] = {}
    if not es.indices.exists(index=COUNTRIES_INDEX):
        return out
    resp = _body(es.search(index=COUNTRIES_INDEX, body={"size": 1000, "query": {"match_all": {}}}))
    for hit in ((resp or {}).get("hits") or {}).get("hits") or []:
        src = hit.get("_source") or {}
        values = [str(src.get(k) or "").strip().upper() for k in ("country_iso", "country_iso3_code")]
        two = [v for v in values if _ISO2_RE.match(v)]
        three = [v for v in values if _ISO3_RE.match(v)]
        if two and three:
            out[two[0]] = three[0]
    return out


def resolve_iso3(iso2: str, catalog: dict[str, str]) -> str:
    iso3 = catalog.get(iso2) or ISO2_TO_ISO3_STATIC.get(iso2)
    if iso3 is None:
        try:
            import pycountry

            rec = pycountry.countries.get(alpha_2=iso2)
            iso3 = rec.alpha_3 if rec else None
        except Exception:  # noqa: BLE001 — the fallback is optional; refusal below is the contract
            iso3 = None
    if not iso3 or not _ISO3_RE.match(iso3):
        raise ExportRefused(f"ISO-3 code for {iso2} cannot be resolved (catalog, static table, pycountry); refusing to guess")
    return iso3


def load_organization(es, org_esid: str) -> dict:
    try:
        doc = _body(es.get(index=ORG_INDEX, id=org_esid))
    except Exception as exc:  # noqa: BLE001 — a missing org is a refusal, not a crash
        raise ExportRefused(f"organisation {org_esid!r} not found in {ORG_INDEX} ({type(exc).__name__})") from exc
    return (doc or {}).get("_source") or {}


def organization_refs(raw) -> list[str]:
    """The organisation esids a team document names. The Teams form stores
    ``organization_esid`` as a list (a multi-select with ``store_esid``); older
    and hand-written documents carry a bare string. Both shapes are read."""
    if isinstance(raw, str):
        return [raw] if raw else []
    if isinstance(raw, list):
        return [str(v) for v in raw if v]
    return []


def load_teams(es, org_esid: str, team_esids: list[str]) -> list[str]:
    """The selected team esids, each confirmed to exist and to belong to the organisation.

    The team title is deliberately not read: nothing in the release names a
    team, so the job never holds the title at all.
    """
    teams: list[str] = []
    for esid in team_esids:
        try:
            doc = _body(es.get(index=TEAMS_INDEX, id=esid))
        except Exception as exc:  # noqa: BLE001
            raise ExportRefused(f"team {esid!r} not found in {TEAMS_INDEX} ({type(exc).__name__})") from exc
        src = (doc or {}).get("_source") or {}
        if org_esid not in organization_refs(src.get("organization_esid")):
            raise ExportRefused(f"team {esid!r} does not belong to organisation {org_esid!r}; refusing")
        teams.append(esid)
    return teams


# ── Manifest ──────────────────────────────────────────────────────────────────


def _bool(v: Any) -> bool:
    if isinstance(v, bool):
        return v
    return str(v or "").strip().lower() in ("true", "1", "yes")


def build_manifest(
    org: dict,
    org_esid: str,
    *,
    sources: set[str],
    countries: list[str],
    first_month: str,
    last_month: str,
    unattributed_exposed: int,
    unattributed_interactions: int,
    now: datetime,
    test_data: bool,
) -> dict:
    missing = [k for k in REQUIRED_SETTINGS if not str(org.get(k) or "").strip()]
    if missing:
        raise ExportRefused(
            "organisation settings missing: " + ", ".join(missing) + " (set them in the organisation's settings)"
        )
    org_uri = str(org[SETTING_ORG_URI]).strip()
    if not _ORG_URI_RE.match(org_uri):
        raise ExportRefused(f"{SETTING_ORG_URI} is not a directory URI of the form https://data.global.church/org/<slug>")
    sha = str(org[SETTING_AGREEMENT_SHA256]).strip().lower()
    if not _SHA256_RE.match(sha):
        raise ExportRefused(f"{SETTING_AGREEMENT_SHA256} is not a 64-hex SHA-256")
    named = _bool(org.get(SETTING_DISPLAY_NAMED))
    display: dict[str, Any] = {"named": named}
    if named:
        name = str(org.get(SETTING_DISPLAY_NAME) or "").strip()
        if not name:
            raise ExportRefused(f"{SETTING_DISPLAY_NAMED} is true but {SETTING_DISPLAY_NAME} is empty")
        display["name"] = name
    manifest: dict[str, Any] = {
        "schemaVersion": SCHEMA_VERSION,
        "grain": GRAIN,
        "suppression": SUPPRESSION,
        "sources": sorted(sources),
        "countriesIncluded": sorted(countries),
        "organization": {"orgUri": org_uri, "partnerOrgId": org_esid},
        "displayName": display,
        "period": {"firstMonth": first_month, "lastMonth": last_month},
        "agreement": {
            "id": str(org[SETTING_AGREEMENT_ID]).strip(),
            "version": str(org[SETTING_AGREEMENT_VERSION]).strip(),
            "sha256": sha,
            "acceptedAt": str(org[SETTING_AGREEMENT_ACCEPTED_AT]).strip(),
            "acceptedByRole": ACCEPTED_BY_ROLE,
        },
        "unattributed": {"exposed": unattributed_exposed, "interactions": unattributed_interactions},
        "generatedAt": _iso(now),
        "generatedBy": GENERATED_BY,
    }
    if test_data:
        manifest["testData"] = True
    return manifest


def build_rows(counts: Counts, catalog: dict[str, str]) -> list[dict]:
    rows = []
    for (team, iso2, month), c in sorted(counts.rows.items()):
        rows.append(
            {
                "team": team,
                "countryIso2": iso2,
                "countryIso3": resolve_iso3(iso2, catalog),
                "month": month,
                "exposed": c["exposed"],
                "interactions": c["interactions"],
                "touchpoints": c["touchpoints"],
                "active": c["active"],
            }
        )
    return rows


# ── Validation ────────────────────────────────────────────────────────────────


def schema_path() -> str:
    return os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..", "schemas", "gc_release_product.schema.json"))


def validate_release(release: dict) -> list[str]:
    """Problems found in ``release`` against the contract; empty when valid.

    Uses ``jsonschema`` when it is installed; otherwise a built-in check of
    the same constraints (required keys, enums, patterns, ``active <=
    touchpoints`` and the displayName rule) so the job never writes a file
    the receiver would refuse.
    """
    try:
        import jsonschema  # type: ignore

        with open(schema_path(), encoding="utf-8") as fh:
            schema = json.load(fh)
        validator = jsonschema.Draft202012Validator(schema, format_checker=jsonschema.FormatChecker())
        return [f"{'/'.join(str(p) for p in e.path) or '$'}: {e.message}" for e in validator.iter_errors(release)]
    except ImportError:
        return _builtin_validate(release)


def _builtin_validate(release: dict) -> list[str]:
    problems: list[str] = []
    m = release.get("manifest") or {}
    for key in (
        "schemaVersion",
        "grain",
        "suppression",
        "sources",
        "countriesIncluded",
        "organization",
        "displayName",
        "period",
        "agreement",
        "generatedAt",
        "generatedBy",
    ):
        if key not in m:
            problems.append(f"manifest/{key}: required")
    if m.get("schemaVersion") != SCHEMA_VERSION:
        problems.append("manifest/schemaVersion: must be " + SCHEMA_VERSION)
    if m.get("grain") != GRAIN:
        problems.append("manifest/grain: must be " + GRAIN)
    if m.get("suppression") != SUPPRESSION:
        problems.append("manifest/suppression: must be none")
    allowed = {"echo", "google_ads", "ga4", "youtube", "facebook_ads"}
    if not m.get("sources") or not set(m["sources"]) <= allowed:
        problems.append("manifest/sources: non-empty subset of " + ",".join(sorted(allowed)))
    included = m.get("countriesIncluded") or []
    if not included or any(not _ISO2_RE.match(str(c)) for c in included):
        problems.append("manifest/countriesIncluded: non-empty ISO-2 list")
    org = m.get("organization") or {}
    if not _ORG_URI_RE.match(str(org.get("orgUri") or "")):
        problems.append("manifest/organization/orgUri: pattern")
    if not str(org.get("partnerOrgId") or ""):
        problems.append("manifest/organization/partnerOrgId: required")
    dn = m.get("displayName") or {}
    if not isinstance(dn.get("named"), bool):
        problems.append("manifest/displayName/named: boolean")
    elif dn["named"] and not dn.get("name"):
        problems.append("manifest/displayName/name: required when named")
    elif not dn["named"] and "name" in dn:
        problems.append("manifest/displayName/name: must be absent when not named")
    period = m.get("period") or {}
    for k in ("firstMonth", "lastMonth"):
        if not _MONTH_RE.match(str(period.get(k) or "")):
            problems.append(f"manifest/period/{k}: YYYY-MM")
    ag = m.get("agreement") or {}
    if not re.match(r"^[a-z0-9][a-z0-9-]*$", str(ag.get("id") or "")):
        problems.append("manifest/agreement/id: pattern")
    if not re.match(r"^[0-9]+\.[0-9]+(\.[0-9]+)?$", str(ag.get("version") or "")):
        problems.append("manifest/agreement/version: pattern")
    if not _SHA256_RE.match(str(ag.get("sha256") or "")):
        problems.append("manifest/agreement/sha256: 64 hex")
    if not str(ag.get("acceptedAt") or ""):
        problems.append("manifest/agreement/acceptedAt: required")
    if ag.get("acceptedByRole") != ACCEPTED_BY_ROLE:
        problems.append("manifest/agreement/acceptedByRole: must be workspace-owner")
    un = m.get("unattributed")
    if un is not None and (not isinstance(un.get("exposed"), int) or not isinstance(un.get("interactions"), int)):
        problems.append("manifest/unattributed: integer exposed + interactions")
    seen: set[Cell] = set()
    for i, row in enumerate(release.get("rows") or []):
        for k in ("team", "countryIso2", "countryIso3", "month", "exposed", "interactions", "touchpoints", "active"):
            if k not in row:
                problems.append(f"rows/{i}/{k}: required")
        if not _TEAM_LABEL_RE.match(str(row.get("team") or "")):
            problems.append(f"rows/{i}/team: opaque label pattern")
        if not _ISO2_RE.match(str(row.get("countryIso2") or "")):
            problems.append(f"rows/{i}/countryIso2: pattern")
        if not _ISO3_RE.match(str(row.get("countryIso3") or "")):
            problems.append(f"rows/{i}/countryIso3: pattern")
        if not _MONTH_RE.match(str(row.get("month") or "")):
            problems.append(f"rows/{i}/month: YYYY-MM")
        if str(row.get("countryIso2")) not in included:
            problems.append(f"rows/{i}/countryIso2: not in countriesIncluded")
        for k in ("exposed", "interactions", "touchpoints", "active"):
            v = row.get(k)
            if not isinstance(v, int) or isinstance(v, bool) or v < 0:
                problems.append(f"rows/{i}/{k}: non-negative integer")
        key = (str(row.get("team")), str(row.get("countryIso2")), str(row.get("month")))
        if key in seen:
            problems.append(f"rows/{i}: duplicate team/country/month")
        seen.add(key)
    return problems


def _semantic_check(release: dict) -> list[str]:
    """Rules the schema states in prose: active <= touchpoints, period covers the rows."""
    problems = []
    m = release["manifest"]
    for i, row in enumerate(release["rows"]):
        if row["active"] > row["touchpoints"]:
            problems.append(f"rows/{i}: active > touchpoints")
        if not (m["period"]["firstMonth"] <= row["month"] <= m["period"]["lastMonth"]):
            problems.append(f"rows/{i}: month outside period")
    return problems


# ── Run ───────────────────────────────────────────────────────────────────────


def _parse_countries(countries: Iterable[str]) -> list[str]:
    out = sorted({str(c).strip().upper() for c in countries if str(c).strip()})
    bad = [c for c in out if not _ISO2_RE.match(c)]
    if bad:
        raise ExportRefused("countries must be ISO 3166-1 alpha-2 codes: " + ", ".join(bad))
    if not out:
        raise ExportRefused("at least one country must be selected")
    return out


def default_out_path(org_esid: str, last_month: str, out_dir: str = DEFAULT_OUT_DIR) -> str:
    return os.path.join(out_dir, f"release-product_{org_esid}_{last_month}.json")


def build_release(
    es,
    *,
    org_esid: str,
    team_esids: list[str],
    countries: list[str],
    now: datetime | None = None,
    test_data: bool = False,
    org: dict | None = None,
) -> tuple[dict, Counts]:
    """Compute the release for one organisation; refuses rather than writing an invalid file.

    ``org`` lets a caller supply the organisation document instead of
    reading it: the publish dialog builds the release from the
    record *as it will be* once the owner's acceptance is written, and
    writes that acceptance only after the gateway has accepted the file.
    """
    _require_enabled()
    now = now or _now()
    countries = _parse_countries(countries)
    if not team_esids:
        raise ExportRefused("at least one team must be selected")
    first, last = month_window(now)
    if org is None:
        org = load_organization(es, org_esid)
    if not _bool(org.get(SETTING_SHARING_ENABLED)):
        raise ExportRefused(
            f"organisation {org_esid!r} has not turned on Global.Church sharing ({SETTING_SHARING_ENABLED}); nothing was read"
        )
    salt = team_label_salt(org)
    teams = load_teams(es, org_esid, team_esids)
    counts = Counts()
    for esid in teams:
        count_team(es, counts, esid, team_label(salt, esid), countries, first, last)
    if counts.overflow:
        raise ExportRefused("a country terms aggregation overflowed; refusing a partial release")
    if not counts.sources_used:
        raise ExportRefused("none of the selected teams has a connected source index; nothing to release")
    catalog = load_catalog_iso3(es)
    release = {
        "manifest": build_manifest(
            org,
            org_esid,
            sources=counts.sources_used,
            countries=countries,
            first_month=first,
            last_month=last,
            unattributed_exposed=counts.unattributed_exposed,
            unattributed_interactions=counts.unattributed_interactions,
            now=now,
            test_data=test_data,
        ),
        "rows": build_rows(counts, catalog),
    }
    problems = validate_release(release) + _semantic_check(release)
    if problems:
        raise ExportRefused("release fails the schema: " + "; ".join(problems[:10]))
    return release, counts


def summarize(release: dict, counts: Counts) -> dict:
    """Counts only — safe to print."""
    rows = release["rows"]
    m = release["manifest"]
    return {
        "rows": len(rows),
        "teams": len({r["team"] for r in rows}),
        "countries": len(m["countriesIncluded"]),
        "countries_with_rows": len({r["countryIso2"] for r in rows}),
        "first_month": m["period"]["firstMonth"],
        "last_month": m["period"]["lastMonth"],
        "sources": m["sources"],
        "unattributed_exposed": m["unattributed"]["exposed"],
        "unattributed_interactions": m["unattributed"]["interactions"],
        "ga_unresolved_country_names": counts.ga_unresolved_names,
    }


def write_release(release: dict, out_path: str) -> None:
    os.makedirs(os.path.dirname(os.path.abspath(out_path)), exist_ok=True)
    tmp = out_path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(release, fh, indent=2, sort_keys=True)
        fh.write("\n")
    os.replace(tmp, out_path)


def run(
    es,
    *,
    org_esid: str,
    team_esids: list[str],
    countries: list[str],
    apply: bool = False,
    out_path: str | None = None,
    now: datetime | None = None,
    test_data: bool = False,
    out=print,
) -> int:
    """Dry-run (default) prints what would be written; ``apply`` writes it. Returns a process exit code."""
    try:
        release, counts = build_release(
            es, org_esid=org_esid, team_esids=team_esids, countries=countries, now=now, test_data=test_data
        )
    except ExportRefused as exc:
        out(f"REFUSED: {exc}")
        return 2
    path = out_path or default_out_path(org_esid, release["manifest"]["period"]["lastMonth"])
    summary = summarize(release, counts)
    mode = "APPLY" if apply else "DRY-RUN"
    out(f"{LOG_PREFIX} [{mode}] organisation {org_esid}: {json.dumps(summary, sort_keys=True)}")
    if not apply:
        out(f"{LOG_PREFIX} [DRY-RUN] would write {path} (re-run with --apply to write it)")
        return 0
    write_release(release, path)
    out(f"{LOG_PREFIX} [APPLY] wrote {path} ({summary['rows']} rows)")
    logging.info("%s: wrote %s rows=%s", LOG_PREFIX, path, summary["rows"])
    return 0
