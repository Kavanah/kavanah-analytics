"""Test support for the release-product export job.

Two things live here, both in-memory and deterministic:

* ``FakeES`` — an Elasticsearch double that evaluates the exact queries the
  job builds (bool / term / terms / range / exists / match_all) against
  per-index document lists, answers the terms -> date_histogram -> sum /
  filter aggregation shape the job uses, and serves ``helpers.scan`` (the
  scroll protocol). Nothing is mocked around the job: the queries it
  builds are the queries that run.
* ``riverside_media_workspace`` — a synthetic organisation ("Riverside
  Media", labelled test data) with two teams and 24 months of Echo,
  Google Ads, GA4, YouTube and Facebook documents whose shapes follow the
  importers and the ES mappings exactly, so the real exporter runs on them
  unchanged. Countries PK IN BD NP KZ are the ones the owner publishes;
  AF IR TM are present in the data and left out. Seeded (random.Random),
  so every run yields the same numbers. No real names, hostnames or
  ministries appear anywhere.
"""

from __future__ import annotations

import hmac
import random
from datetime import date, datetime, timedelta, timezone
from typing import Any, Iterable


class NotFound(Exception):
    pass


# ── Query evaluation ──────────────────────────────────────────────────────────


def _get(doc: dict, path: str) -> Any:
    cur: Any = doc
    for part in path.split("."):
        if not isinstance(cur, dict):
            return None
        cur = cur.get(part)
    return cur


def _values(doc: dict, field: str) -> set:
    """A field's values as Elasticsearch matches them: an array field matches
    on any element (``term`` / ``terms`` on ``organization_esid: [esid]``)."""
    v = _get(doc, field)
    if v is None:
        return set()
    if isinstance(v, list):
        return {x for x in v if x is not None}
    return {v}


def matches(doc: dict, query: dict | None) -> bool:
    if not query:
        return True
    (kind, spec), *rest = query.items()
    assert not rest, "one clause per query object"
    if kind == "match_all":
        return True
    if kind == "bool":
        for clause in spec.get("filter", []) + spec.get("must", []):
            if not matches(doc, clause):
                return False
        for clause in spec.get("must_not", []):
            if matches(doc, clause):
                return False
        should = spec.get("should", [])
        if should:
            need = int(spec.get("minimum_should_match", 1 if not spec.get("filter") and not spec.get("must") else 0))
            hits = sum(1 for clause in should if matches(doc, clause))
            if hits < need:
                return False
        return True
    if kind == "term":
        ((f, v),) = spec.items()
        v = v.get("value") if isinstance(v, dict) else v
        return v in _values(doc, f)
    if kind == "terms":
        ((f, vs),) = spec.items()
        return bool(_values(doc, f) & set(vs))
    if kind == "exists":
        return _get(doc, spec["field"]) is not None
    if kind == "range":
        ((f, bounds),) = spec.items()
        v = _get(doc, f)
        if v is None:
            return False
        for op, bound in bounds.items():
            if op == "format":
                continue
            v2, b2 = v, bound
            if isinstance(v, str) and isinstance(bound, str):
                v2, b2 = v[:10], bound[:10]  # ISO day comparison
            if op == "gte" and not v2 >= b2:
                return False
            if op == "gt" and not v2 > b2:
                return False
            if op == "lte" and not v2 <= b2:
                return False
            if op == "lt" and not v2 < b2:
                return False
        return True
    raise NotImplementedError(f"FakeES does not evaluate {kind!r}")


def _run_aggs(docs: list[dict], aggs: dict) -> dict:
    out: dict[str, Any] = {}
    for name, spec in aggs.items():
        sub = spec.get("aggs") or {}
        if "terms" in spec:
            field = spec["terms"]["field"]
            size = int(spec["terms"].get("size", 10))
            groups: dict[Any, list[dict]] = {}
            for d in docs:
                v = _get(d, field)
                if v is None:
                    continue
                groups.setdefault(v, []).append(d)
            ordered = sorted(groups.items(), key=lambda kv: (-len(kv[1]), str(kv[0])))
            buckets = []
            for key, group in ordered[:size]:
                b = {"key": key, "doc_count": len(group)}
                b.update(_run_aggs(group, sub))
                buckets.append(b)
            out[name] = {
                "buckets": buckets,
                "sum_other_doc_count": sum(len(g) for _, g in ordered[size:]),
                "doc_count_error_upper_bound": 0,
            }
        elif "date_histogram" in spec:
            field = spec["date_histogram"]["field"]
            assert spec["date_histogram"].get("calendar_interval") == "month"
            groups = {}
            for d in docs:
                v = _get(d, field)
                if not v:
                    continue
                groups.setdefault(str(v)[:7], []).append(d)
            buckets = []
            for month in sorted(groups):
                b = {"key_as_string": month, "key": month, "doc_count": len(groups[month])}
                b.update(_run_aggs(groups[month], sub))
                buckets.append(b)
            out[name] = {"buckets": buckets}
        elif "sum" in spec:
            out[name] = {"value": float(sum(float(_get(d, spec["sum"]["field"]) or 0) for d in docs))}
        elif "filter" in spec:
            kept = [d for d in docs if matches(d, spec["filter"])]
            b = {"doc_count": len(kept)}
            b.update(_run_aggs(kept, sub))
            out[name] = b
        else:
            raise NotImplementedError(f"FakeES does not run aggregation {list(spec)!r}")
    return out


def _project(doc: dict, source: Any) -> dict:
    if source in (None, True):
        return doc
    fields = source if isinstance(source, list) else [source]
    out: dict = {}
    for path in fields:
        parts = path.split(".")
        cur, dst = doc, out
        for i, part in enumerate(parts):
            if not isinstance(cur, dict) or part not in cur:
                break
            if i == len(parts) - 1:
                dst[part] = cur[part]
            else:
                dst = dst.setdefault(part, {})
                cur = cur[part]
    return out


class _Indices:
    def __init__(self, parent: "FakeES"):
        self._p = parent

    def exists(self, index=None, **_):
        return index in self._p.docs

    # the sharing service heals the organisation mapping before it
    # writes; the fake has no mapping, so every field is "missing" and the
    # put_mapping body is recorded for assertions.
    def get_mapping(self, index=None, **_):
        return {index: {"mappings": {"properties": {}}}}

    def put_mapping(self, index=None, body=None, **_):
        self._p.put_mappings.append((index, body))
        return {"acknowledged": True}


class FakeES:
    """Minimal Elasticsearch double over ``{index: [(id, doc), ...]}``."""

    def __init__(self, docs: dict[str, list[tuple[str, dict]]] | None = None):
        self.docs: dict[str, list[tuple[str, dict]]] = {k: list(v) for k, v in (docs or {}).items()}
        self.indices = _Indices(self)
        self.searches: list[tuple[str, dict]] = []
        self.put_mappings: list[tuple[str, dict]] = []
        self._client_meta = ()

    # elasticsearch-py 9: helpers.scan calls client.options(...) first.
    def options(self, **_):
        return self

    def add(self, index: str, doc_id: str, doc: dict) -> None:
        self.docs.setdefault(index, []).append((doc_id, doc))

    def get(self, index=None, id=None, **_):
        for doc_id, doc in self.docs.get(index, []):
            if doc_id == id:
                return {"_index": index, "_id": doc_id, "found": True, "_source": doc}
        raise NotFound(f"{index}/{id}")

    def search(self, index=None, body=None, **kwargs):
        if index not in self.docs:
            raise NotFound(index)
        body = dict(body or {})
        for k in ("query", "aggs", "_source", "size", "sort"):
            if k in kwargs:
                body[k] = kwargs[k]
        self.searches.append((index, body))
        hits = [(i, d) for i, d in self.docs[index] if matches(d, body.get("query"))]
        resp: dict[str, Any] = {
            "took": 1,
            "timed_out": False,
            "_shards": {"total": 1, "successful": 1, "skipped": 0, "failed": 0},
            "hits": {"total": {"value": len(hits), "relation": "eq"}, "hits": []},
        }
        if "aggs" in body:
            resp["aggregations"] = _run_aggs([d for _, d in hits], body["aggs"])
        size = body.get("size")
        size = len(hits) if size is None else int(size)
        resp["hits"]["hits"] = [
            {"_index": index, "_id": i, "_score": None, "_source": _project(d, body.get("_source"))} for i, d in hits[:size]
        ]
        if "scroll" in kwargs:
            resp["_scroll_id"] = "fake-scroll"
        return resp

    def scroll(self, scroll_id=None, **_):
        return {
            "_scroll_id": scroll_id,
            "_shards": {"total": 1, "successful": 1, "skipped": 0, "failed": 0},
            "hits": {"total": {"value": 0, "relation": "eq"}, "hits": []},
        }

    def clear_scroll(self, **_):
        return {"succeeded": True}


# ── Riverside Media synthetic workspace ───────────────────────────────────────

ORG_ESID = "riverside-media-org"
ORG_TITLE = "Riverside Media"
TEAM_PASHTO = "team-pashto-outreach"
TEAM_URDU = "team-urdu-outreach"
TEAMS = {TEAM_PASHTO: "Pashto outreach", TEAM_URDU: "Urdu outreach"}
PUBLISHED_COUNTRIES = ["PK", "IN", "BD", "NP", "KZ"]
WITHHELD_COUNTRIES = ["AF", "IR", "TM"]
ALL_COUNTRIES = PUBLISHED_COUNTRIES + WITHHELD_COUNTRIES
COUNTRY_NAMES = {
    "PK": "Pakistan",
    "IN": "India",
    "BD": "Bangladesh",
    "NP": "Nepal",
    "KZ": "Kazakhstan",
    "AF": "Afghanistan",
    "IR": "Iran",
    "TM": "Turkmenistan",
}
ISO3 = {"PK": "PAK", "IN": "IND", "BD": "BGD", "NP": "NPL", "KZ": "KAZ", "AF": "AFG", "IR": "IRN", "TM": "TKM"}

ORG_URI = "https://data.global.church/org/riverside-media"
DISPLAY_NAME = "Riverside Media (test data)"
AGREEMENT_ID = "digital-aggregates"
AGREEMENT_VERSION = "0.2"
AGREEMENT_SHA256 = "e1057ffdf5e2b1865ea64f0658ad8b39fcad804a5433045dc923bb11023de22b"
AGREEMENT_ACCEPTED_AT = "2026-10-19T00:00:00Z"
NOW = datetime(2026, 10, 19, 12, 0, 0, tzinfo=timezone.utc)
# the organisation's per-team label salt and the labels the rule
# yields for the two teams (HMAC-SHA256(salt, esid), 32 hex) — recomputed
# here, not imported from the job, so a change to the rule shows.
TEAM_LABEL_SALT = "riverside-test-salt-5c4b3a2f1e0d9c8b"


def team_label(team_esid: str, salt: str = TEAM_LABEL_SALT) -> str:
    return hmac.new(salt.encode("utf-8"), team_esid.encode("utf-8"), "sha256").hexdigest()[:32]


LABELS = {esid: team_label(esid) for esid in TEAMS}
LAST_MONTH = "2026-10"
FIRST_MONTH = "2024-11"


def months_back(last_month: str, n: int) -> list[str]:
    y, m = int(last_month[:4]), int(last_month[5:7])
    out = []
    for _ in range(n):
        out.append(f"{y:04d}-{m:02d}")
        m -= 1
        if m == 0:
            y, m = y - 1, 12
    return list(reversed(out))


def _days_in(month: str) -> int:
    y, m = int(month[:4]), int(month[5:7])
    nxt = date(y + (m == 12), 1 if m == 12 else m + 1, 1)
    return (nxt - date(y, m, 1)).days


def _day(rng: random.Random, month: str) -> str:
    return f"{month}-{rng.randint(1, _days_in(month)):02d}"


def _echo_created_at(day: str, rng: random.Random) -> str:
    dt = datetime.fromisoformat(day) + timedelta(hours=rng.randint(0, 23), minutes=rng.randint(0, 59))
    return dt.strftime("%B %d, %Y %H:%M:%S UTC")


def organization_doc(**overrides) -> dict:
    doc = {
        "organization_title": ORG_TITLE,
        "organization_description": "Synthetic test organisation for the release-product export.",
        "workspace_title": "Riverside Media workspace",
        "workspace_esid": "riverside-media-ws",
        "gc_org_uri": ORG_URI,
        "gc_display_named": True,
        "gc_display_name": DISPLAY_NAME,
        "gc_agreement_id": AGREEMENT_ID,
        "gc_agreement_version": AGREEMENT_VERSION,
        "gc_agreement_sha256": AGREEMENT_SHA256,
        "gc_agreement_accepted_at": AGREEMENT_ACCEPTED_AT,
        "gc_team_label_salt": TEAM_LABEL_SALT,
        # the fixture organisation has opted in
        "gc_sharing_enabled": True,
        "gc_gateway_url": "https://api.example.org",
    }
    doc.update(overrides)
    return doc


def team_doc(team_esid: str, org_esid: str = ORG_ESID) -> dict:
    return {
        "team_title": TEAMS[team_esid],
        "team_name": TEAMS[team_esid],
        "workspace_title": "Riverside Media workspace",
        "workspace_esid": "riverside-media-ws",
        "organization_title": ORG_TITLE,
        # the Teams form stores the organisation as a list (multi_select + store_esid)
        "organization_esid": [org_esid],
        "team_country_text": "Pakistan",
        "team_country_esids": ["PK"],
        "team_language": "pashto" if team_esid == TEAM_PASHTO else "urdu",
    }


def _echo_doc(rng: random.Random, team_esid: str, iso2: str, month: str, import_month: str) -> dict:
    day = _day(rng, month)
    spiritual = 1 if rng.random() < 0.3 else 0
    multiplier = 1 if (spiritual == 0 and rng.random() < 0.1) else 0
    outcome = "Spiritual conversation" if spiritual else ("Multiplier" if multiplier else "No response")
    return {
        "id": f"echo-{rng.getrandbits(40):010x}",
        "client_id": f"client-{rng.getrandbits(32):08x}",
        "type": "chat",
        "topic": "general",
        "referrer": "Example outreach page",
        "created_at": _echo_created_at(day, rng),
        "kibana_date": day,
        # Every importer stamps @timestamp with the import instant, not the activity date.
        "@timestamp": f"{import_month}-02T06:00:00+00:00",
        "status": "closed",
        "outcome": outcome,
        "spiritual_conversation": spiritual,
        "multiplier": multiplier,
        "no_response": 1 if outcome == "No response" else 0,
        "language_barrier": 0,
        "Other": 0,
        "incomingMessagesCount": rng.randint(1, 12),
        "outboundMessagesCount": rng.randint(1, 12),
        "client_country": COUNTRY_NAMES[iso2],
        "contact_country_iso2": iso2,
        # The TEAM's country — never the seeker's.
        "team_country": "Pakistan",
        "team_country_iso3_code": "PK",
        "team_city": "Example City",
        "team_site_url": "https://example.org",
        "team_title": TEAMS[team_esid],
        "team_esid": team_esid,
        "organization_title": ORG_TITLE,
        "echo_source": "https://echo.example.org",
    }


def _google_ads_geo_doc(rng: random.Random, team_esid: str, iso2: str, day: str) -> dict:
    return {
        "record_type": "geo",
        "campaign_id": f"c{rng.randint(1000, 9999)}",
        "campaign_name": "Example campaign",
        "campaign_channel_type": "MULTI_CHANNEL",
        "geo_criterion_id": 2000 + ALL_COUNTRIES.index(iso2),
        "geo_country_code": iso2,
        "geo_country_name": COUNTRY_NAMES[iso2],
        "conversions": rng.randint(1, 20),
        "impressions": rng.randint(100, 5000),
        "cost": round(rng.uniform(1, 50), 2),
        "date": day,
        "kibana_date": day,
        "@timestamp": f"{day}T07:00:00+00:00",
        "team_esid": team_esid,
        "team_title": TEAMS[team_esid],
        "team_country": "Pakistan",
    }


def _google_ads_campaign_doc(rng: random.Random, team_esid: str, day: str) -> dict:
    """Daily campaign doc: has clicks and impressions but NO country — must not be counted."""
    return {
        "campaign_id": f"c{rng.randint(1000, 9999)}",
        "campaign_name": "Example campaign",
        "campaign_channel_type": "SEARCH",
        "campaign_status": "ENABLED",
        "impressions": rng.randint(100, 5000),
        "clicks": rng.randint(1, 200),
        "cost": round(rng.uniform(1, 50), 2),
        "conversions": rng.randint(0, 10),
        "date": day,
        "kibana_date": day,
        "@timestamp": f"{day}T07:00:00+00:00",
        "team_esid": team_esid,
        "team_title": TEAMS[team_esid],
        "team_country": "Pakistan",
    }


def _ga_doc(rng: random.Random, team_esid: str, iso2: str, day: str, report_type: str = "geo_device") -> dict:
    return {
        "report_type": report_type,
        "property_id": "properties/000000",
        "property_name": "Example property",
        "country": COUNTRY_NAMES[iso2],
        "city": "(not set)",
        "deviceCategory": rng.choice(["mobile", "desktop"]),
        "platform": "web",
        "date": day.replace("-", ""),
        "activeUsers": rng.randint(1, 300),
        "sessions": rng.randint(1, 400),
        "screenPageViews": rng.randint(1, 900),
        "eventCount": rng.randint(1, 2000),
        "totalUsers": rng.randint(1, 300),
        "kibana_date": day,
        "@timestamp": f"{day}T08:00:00+00:00",
        "team_esid": team_esid,
        "team_title": TEAMS[team_esid],
        "team_country": "Pakistan",
    }


def _youtube_doc(rng: random.Random, team_esid: str, iso2: str, day: str, record_type: str = "country_breakdown") -> dict:
    return {
        "record_type": record_type,
        "channel_id": "UCexample000000000000000",
        "channel_title": "Example channel",
        "date": day,
        "country": iso2,
        "views": rng.randint(10, 3000),
        "estimated_minutes_watched": rng.randint(10, 9000),
        "subscribers_gained": rng.randint(0, 20),
        "subscribers_lost": rng.randint(0, 5),
        "likes": rng.randint(0, 100),
        "comments": rng.randint(0, 20),
        "shares": rng.randint(0, 20),
        "kibana_date": day,
        "@timestamp": f"{day}T09:00:00+00:00",
        "team_esid": team_esid,
        "team_title": TEAMS[team_esid],
        "team_country": "Pakistan",
    }


def fb_targeting(countries: list[str], *, regions_in: str | None = None, country_groups: list[str] | None = None) -> dict:
    geo: dict[str, Any] = {"countries": list(countries), "location_types": ["home"]}
    if regions_in:
        geo["regions"] = [{"key": "9999", "name": "Example region", "country": regions_in}]
    if country_groups:
        geo["country_groups"] = list(country_groups)
    return {"age_min": 18, "age_max": 65, "geo_locations": geo, "publisher_platforms": ["facebook"]}


def _fb_doc(rng: random.Random, team_esid: str, adset_id: str, targeting: dict, day: str) -> dict:
    return {
        "ad_account_id": "act_000000000",
        "ad_campaign_id": "cmp-000001",
        "ad_id": f"ad-{adset_id}-{rng.randint(1, 3)}",
        "ad_name": "Example ad",
        "adset_id": adset_id,
        "adset_name": f"Example ad set {adset_id}",
        "adset_optimization_goal": "LINK_CLICKS",
        "adset_targeting": targeting,
        "insight_impressions": rng.randint(50, 4000),
        "insight_clicks": rng.randint(1, 150),
        "insight_spend": round(rng.uniform(1, 30), 2),
        "insight_date_start": day,
        "insight_date_stop": day,
        "date": day,
        "kibana_date": day,
        "@timestamp": f"{day}T10:00:00+00:00",
        "team_esid": team_esid,
        "team_title": TEAMS[team_esid],
        "team_country": "Pakistan",
        "team_site_url": "https://example.org",
        "team_city": "Example City",
        "organization_title": ORG_TITLE,
    }


def riverside_media_workspace(seed: int = 20261019, last_month: str = LAST_MONTH, months: int = 24) -> FakeES:
    """Build the whole synthetic workspace into a fresh FakeES."""
    rng = random.Random(seed)
    es = FakeES()
    es.add("de_asset_organization", ORG_ESID, organization_doc())
    es.add(
        "de_asset_organization",
        "other-org",
        organization_doc(organization_title="Other org", gc_org_uri="https://data.global.church/org/other-org"),
    )
    for esid in TEAMS:
        es.add("de_asset_teams", esid, team_doc(esid))
    es.add("de_asset_teams", "team-elsewhere", {**team_doc(TEAM_URDU, org_esid="other-org"), "team_title": "Elsewhere"})
    # Seeded catalog quirk: country_iso3_code holds the alpha-2 code (updates/update_v16.py).
    for iso2, name in COUNTRY_NAMES.items():
        es.add("de_asset_countries", iso2, {"country_title": name, "country_iso3_code": iso2})
    # one catalog row that does carry a real alpha-3 in the other field, so the catalog path is exercised
    es.add("de_asset_countries", "KZ-full", {"country_title": "Kazakhstan", "country_iso": "KAZ", "country_iso3_code": "KZ"})

    month_list = months_back(last_month, months)
    for esid in TEAMS:
        echo_idx = f"{esid}__echo_conversations"
        ads_idx = f"{esid}__google_ads"
        ga_idx = f"{esid}__ga_metrics"
        yt_idx = f"{esid}__youtube_analytics"
        fb_idx = f"{esid}__fb_insights"
        # Facebook ad sets: one single-country (PK), one region-only inside PK, one multi-country, one worldwide.
        adsets = [
            ("as-pk", fb_targeting(["PK"])),
            ("as-pk-region", fb_targeting([], regions_in="PK")),
            ("as-multi", fb_targeting(["PK", "IN", "AF"])),
            ("as-world", fb_targeting([], country_groups=["worldwide"])),
        ]
        for mi, month in enumerate(month_list):
            # Echo: imported the following month (so @timestamp month != activity month).
            import_month = month_list[mi + 1] if mi + 1 < len(month_list) else last_month
            for iso2 in ALL_COUNTRIES:
                weight = 6 if iso2 in ("PK", "IN") else 2
                for _ in range(rng.randint(0, weight)):
                    d = _echo_doc(rng, esid, iso2, month, import_month)
                    es.add(echo_idx, d["id"], d)
                for _ in range(rng.randint(0, 2)):
                    d = _google_ads_geo_doc(rng, esid, iso2, _day(rng, month))
                    es.add(ads_idx, f"geo-{rng.getrandbits(40):010x}", d)
                for _ in range(rng.randint(0, 2)):
                    d = _ga_doc(rng, esid, iso2, _day(rng, month))
                    es.add(ga_idx, f"ga-{rng.getrandbits(40):010x}", d)
                for _ in range(rng.randint(0, 2)):
                    d = _youtube_doc(rng, esid, iso2, _day(rng, month))
                    es.add(yt_idx, f"yt-{rng.getrandbits(40):010x}", d)
            # Non-geo docs that must never be counted.
            es.add(ads_idx, f"cmp-{rng.getrandbits(40):010x}", _google_ads_campaign_doc(rng, esid, _day(rng, month)))
            es.add(
                ga_idx, f"ga-pages-{rng.getrandbits(40):010x}", _ga_doc(rng, esid, "PK", _day(rng, month), report_type="pages")
            )
            es.add(
                yt_idx,
                f"yt-ret-{rng.getrandbits(40):010x}",
                _youtube_doc(rng, esid, "PK", _day(rng, month), record_type="video_retention"),
            )
            for adset_id, targeting in adsets:
                for _ in range(rng.randint(1, 3)):
                    d = _fb_doc(rng, esid, adset_id, targeting, _day(rng, month))
                    es.add(fb_idx, f"fb-{rng.getrandbits(40):010x}", d)
    # An activity month just outside the window, so the window filter is exercised.
    before = months_back(month_list[0], 2)[0]
    d = _echo_doc(rng, TEAM_PASHTO, "PK", before, month_list[0])
    es.add(f"{TEAM_PASHTO}__echo_conversations", d["id"], d)
    return es


def docs_of(es: FakeES, index: str) -> Iterable[dict]:
    return (d for _, d in es.docs.get(index, []))
