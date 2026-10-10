"""
Tests for dashboard builder map data query functions.

Routes / functions covered:
  GET  /dashboard-builder/api/map-data          get_map_data (route dispatch)
  _query_fb_map                                  Facebook city-level map
  _query_fb_region_map                           Facebook region-level map
  _query_fb_country_map                          Facebook country-level map
  _query_echo_country_map                        Echo country map
  _query_echo_map                                Echo city-level map
  _query_google_ads_country_map                  Google Ads country map
  _query_google_ads_installs_geo_map             Google Ads installs geo map
  _query_ga4_country_map                         GA4 country map
  _query_youtube_country_map                     YouTube country map
  _query_team_country_map_generic                Generic team country map
  _query_team_title_country_map                  Team title country map
  _query_dt_map                                  DT address-level map
  _query_dt_country_map                          DT country map
  _query_dt_team_country_map                     DT team country map

All external service calls (Elasticsearch, geocoding) are mocked.
"""

from unittest import mock

import pytest

from asset_management.web.blueprints.dashboard_builder.routes import (
    _query_fb_map,
    _query_fb_map_cached_only,
    _query_fb_region_map,
    _query_fb_country_map,
    _query_echo_country_map,
    _query_echo_map,
    _query_google_ads_country_map,
    _query_google_ads_installs_geo_map,
    _query_ga4_country_map,
    _query_youtube_country_map,
    _query_team_country_map_generic,
    _query_team_title_country_map,
    _query_dt_map,
    _query_dt_country_map,
    _query_dt_team_country_map,
)

_R = "asset_management.web.blueprints.dashboard_builder.routes"


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _mock_es(return_value):
    """Create a mock ES client whose .search() returns the given dict."""
    es = mock.MagicMock()
    es.search.return_value = return_value
    return es


def _mock_es_team_title(buckets):
    """mock ES for the dual-field team_title agg.

    ``_terms_agg_by_team_title`` queries BOTH ``team_title.keyword`` and bare
    ``team_title`` and merges. A real cluster returns the buckets under exactly
    ONE mapping (a team lives in one index mapping) and an empty bucket set for
    the other. This mock mirrors that: buckets for ``.keyword``, empty for bare
    — so the merge isn't a double-count. (The merge itself is covered by
    test_team_title_agg_dual_field.)
    """
    es = mock.MagicMock()

    def _search(index=None, body=None, ignore_unavailable=False):
        field = body["aggs"]["by_team"]["terms"]["field"]
        out = buckets if field == "team_title.keyword" else []
        return _es_resp({"aggregations": {"by_team": {"buckets": out}}})

    es.search.side_effect = _search
    return es


def _es_resp(body):
    """Wrap a dict so it behaves like an ES response (no .body attribute)."""
    return body


# ---------------------------------------------------------------------------
# Sample stat configs (mirror real entries from _MAP_STATISTICS)
# ---------------------------------------------------------------------------

STAT_FB_REACH = {
    "data_source": "fb_insights",
    "field": "insight_reach",
    "type": "range",
    "gte": 1,
}

STAT_FB_APP_INSTALLS = {
    "data_source": "fb_insights",
    "field": "insight_actions_mobile_app_install",
    "type": "range",
    "gte": 1,
}

STAT_ECHO_MESSAGES = {
    "data_source": "echo",
    "field": "incomingMessagesCount",
    "type": "range",
    "gte": 1,
}

STAT_ECHO_MULTIPLIER = {
    "data_source": "echo",
    "field": "multiplier",
    "type": "range",
    "gte": 1,
    "min_country_value": 2,
}

STAT_ECHO_CONTACTS = {
    "data_source": "echo",
    "field": None,
    "cardinality_field": "client_contact",
}

STAT_GOOGLE_ADS_INSTALLS = {
    "data_source": "google_ads",
    "field": "app_installs",
    "type": "range",
    "gte": 1,
}

STAT_GA4_WEB = {
    "data_source": "ga_metrics",
    "field": "totalUsers",
    "type": "range",
    "gte": 1,
}

STAT_YOUTUBE_SUBS = {
    "data_source": "youtube",
    "field": "subscribers_gained",
    "type": "range",
    "gte": 1,
}

STAT_DT_CONTACTS = {
    "data_source": "dt",
    "field": None,
    "dt_date_field": "meta.first_contact_date.keyword",
}

STAT_DT_BAPTISMS = {
    "data_source": "dt",
    "field": "meta.baptism_date",
    "type": "exists",
    "dt_date_field": "date_created.keyword",
}

STAT_DT_SALVATION = {
    "data_source": "dt",
    "field": "meta.faith_status.keyword",
    "type": "term",
    "value": "believer",
    "address_only": True,
    "dt_date_field": "date_created.keyword",
}

STAT_ORGANIC_FB = {
    "data_source": "fb_page_insights",
    "field": "page_impressions_unique",
    "type": "range",
    "gte": 1,
}


# ===========================================================================
# _query_fb_map
# ===========================================================================


class TestQueryFbMap:
    """Facebook city-level map aggregation."""

    @mock.patch("lib.geocoding.geocode_city", return_value=None)
    def test_normal_buckets(self, mock_geocode):
        es = _mock_es(_es_resp({
            "aggregations": {
                "cities": {
                    "buckets": [
                        {
                            "key": "Cairo",
                            "doc_count": 10,
                            "sample": {
                                "hits": {
                                    "hits": [{
                                        "_source": {
                                            "location_cities": [
                                                {"name": "Cairo", "coordinates": {"lat": 30.0, "lon": 31.2}},
                                            ]
                                        }
                                    }]
                                }
                            },
                            "top_country": {"buckets": [{"key": "EG"}]},
                            "stat": {"value": 500},
                        }
                    ]
                }
            }
        }))
        result = _query_fb_map(es, STAT_FB_REACH, "VOGZ", ["team1"], "2024-01-01", "2024-12-31")
        assert len(result) == 1
        assert result[0]["name"] == "Cairo"
        assert result[0]["lat"] == 30.0
        assert result[0]["lon"] == 31.2
        assert result[0]["value"] == 500

    def test_empty_buckets(self):
        es = _mock_es(_es_resp({"aggregations": {"cities": {"buckets": []}}}))
        result = _query_fb_map(es, STAT_FB_REACH, "", [], "", "")
        assert result == []

    @mock.patch("lib.geocoding.geocode_city", return_value=None)
    def test_missing_coordinates_drops_point(self, mock_geocode):
        """Cities with no resolvable coords are dropped — no centroid fallback."""
        es = _mock_es(_es_resp({
            "aggregations": {
                "cities": {
                    "buckets": [{
                        "key": "Unknown City",
                        "doc_count": 5,
                        "sample": {
                            "hits": {
                                "hits": [{
                                    "_source": {
                                        "location_cities": [
                                            {"name": "Unknown City", "coordinates": {}}
                                        ]
                                    }
                                }]
                            }
                        },
                        "top_country": {"buckets": []},
                        "stat": {"value": 100},
                    }]
                }
            }
        }))
        result = _query_fb_map(es, STAT_FB_REACH, "", [], "", "")
        assert result == []

    @mock.patch("lib.geocoding.geocode_city", return_value=None)
    def test_city_not_in_location_cities_dropped(self, mock_geocode):
        """Bucket key with no matching location_cities entry and no country is dropped."""
        es = _mock_es(_es_resp({
            "aggregations": {
                "cities": {
                    "buckets": [{
                        "key": "Atlantis",
                        "doc_count": 3,
                        "sample": {
                            "hits": {
                                "hits": [{
                                    "_source": {
                                        "location_cities": [
                                            {"name": "Cairo", "coordinates": {"lat": 30.0, "lon": 31.2}},
                                        ]
                                    }
                                }]
                            }
                        },
                        "top_country": {"buckets": []},
                        "stat": {"value": 50},
                    }]
                }
            }
        }))
        result = _query_fb_map(es, STAT_FB_REACH, "", [], "", "")
        assert result == []

    def test_stat_value_zero_uses_doc_count(self):
        es = _mock_es(_es_resp({
            "aggregations": {
                "cities": {
                    "buckets": [{
                        "key": "Nairobi",
                        "doc_count": 7,
                        "sample": {
                            "hits": {
                                "hits": [{
                                    "_source": {
                                        "location_cities": [
                                            {"name": "Nairobi", "coordinates": {"lat": -1.3, "lon": 36.8}},
                                        ]
                                    }
                                }]
                            }
                        },
                        "stat": {"value": 0},
                    }]
                }
            }
        }))
        result = _query_fb_map(es, STAT_FB_REACH, "", [], "", "")
        assert result[0]["value"] == 7


# ===========================================================================
# _query_fb_map_cached_only — cache-only city resolver
# ===========================================================================


class TestQueryFbMapCachedOnly:
    """Cache-only city resolver: Nominatim must never be called from this
    path. Verified with mocks that explode if reached."""

    @mock.patch("lib.geocoding.geocode_city", side_effect=AssertionError("geocode_city must not be called from the cache-only resolver"))
    @mock.patch("lib.geocoding.lookup_cached_city")
    @mock.patch("lib.geocoding.reload_cache_if_changed")
    def test_resolves_from_cache_only(self, mock_reload, mock_lookup, mock_geocode):
        mock_lookup.return_value = {"lat": 30.0, "lon": 31.2}
        es = _mock_es(_es_resp({
            "aggregations": {
                "cities": {
                    "buckets": [{
                        "key": "Cairo",
                        "doc_count": 10,
                        "sample": {
                            "hits": {
                                "hits": [{
                                    "_source": {
                                        "location_cities": [],
                                        "adset_targeting": {"geo_locations": {"cities": [
                                            {"name": "Cairo", "country": "EG", "region": "Cairo Governorate"},
                                        ]}},
                                    }
                                }]
                            }
                        },
                        "stat": {"value": 500},
                    }]
                }
            }
        }))
        points, total, resolved = _query_fb_map_cached_only(
            es, STAT_FB_REACH, "VOGZ", ["team1"], "2024-01-01", "2024-12-31"
        )
        assert total == 1
        assert resolved == 1
        assert len(points) == 1
        assert points[0]["name"] == "Cairo"
        assert points[0]["lat"] == 30.0
        # mtime-aware reload runs once at the start of the batch
        mock_reload.assert_called_once()
        # Live Nominatim path is never invoked
        mock_geocode.assert_not_called()

    @mock.patch("lib.geocoding.geocode_city", side_effect=AssertionError("geocode_city must not be called from the cache-only resolver"))
    @mock.patch("lib.geocoding.lookup_cached_city", return_value=None)
    @mock.patch("lib.geocoding.reload_cache_if_changed")
    def test_falls_back_to_location_cities_then_drops(self, mock_reload, mock_lookup, mock_geocode):
        """When the cache has no hit, fall back to the location_cities
        source field. If that's also missing, drop the bucket — never
        call Nominatim."""
        es = _mock_es(_es_resp({
            "aggregations": {
                "cities": {
                    "buckets": [
                        {
                            "key": "Nairobi",
                            "doc_count": 7,
                            "sample": {
                                "hits": {
                                    "hits": [{
                                        "_source": {
                                            "location_cities": [
                                                {"name": "Nairobi", "coordinates": {"lat": -1.3, "lon": 36.8}},
                                            ],
                                            "adset_targeting": {"geo_locations": {"cities": [
                                                {"name": "Nairobi", "country": "KE"},
                                            ]}},
                                        }
                                    }]
                                }
                            },
                            "stat": {"value": 200},
                        },
                        {
                            "key": "Atlantis",
                            "doc_count": 3,
                            "sample": {
                                "hits": {
                                    "hits": [{
                                        "_source": {
                                            "location_cities": [],
                                            "adset_targeting": {"geo_locations": {"cities": [
                                                {"name": "Atlantis", "country": "XX"},
                                            ]}},
                                        }
                                    }]
                                }
                            },
                            "stat": {"value": 50},
                        },
                    ]
                }
            }
        }))
        points, total, resolved = _query_fb_map_cached_only(
            es, STAT_FB_REACH, "", [], "", ""
        )
        assert total == 2
        assert resolved == 1
        assert len(points) == 1
        assert points[0]["name"] == "Nairobi"
        mock_geocode.assert_not_called()

    @mock.patch("lib.geocoding.reload_cache_if_changed")
    def test_empty_buckets(self, mock_reload):
        es = _mock_es(_es_resp({"aggregations": {"cities": {"buckets": []}}}))
        points, total, resolved = _query_fb_map_cached_only(
            es, STAT_FB_REACH, "", [], "", ""
        )
        assert points == []
        assert total == 0
        assert resolved == 0


# ===========================================================================
# _query_fb_region_map
# ===========================================================================


class TestQueryFbRegionMap:
    """Facebook region-level map — aggregates directly on location_regions.name."""

    def test_normal_region_aggregation(self):
        es = _mock_es(_es_resp({
            "aggregations": {
                "regions": {
                    "buckets": [
                        {
                            "key": "California",
                            "doc_count": 10,
                            "sample": {
                                "hits": {"hits": [{"_source": {
                                    "location_regions": [
                                        {"name": "California", "coordinates": {"lat": 36.78, "lon": -119.42}, "country_code": "US"},
                                    ]
                                }}]}
                            },
                            "stat": {"value": 200},
                        },
                    ]
                }
            }
        }))
        result = _query_fb_region_map(es, STAT_FB_REACH, "VOGZ", [], "2024-01-01", "2024-12-31")
        assert len(result) == 1
        assert result[0]["name"] == "California"
        assert result[0]["value"] == 200
        assert result[0]["lat"] == 36.78
        assert result[0]["lon"] == -119.42

    def test_empty_buckets(self):
        es = _mock_es(_es_resp({"aggregations": {"regions": {"buckets": []}}}))
        result = _query_fb_region_map(es, STAT_FB_REACH, "", [], "", "")
        assert result == []

    def test_es_exception_returns_empty(self):
        es = mock.MagicMock()
        es.search.side_effect = Exception("ES down")
        result = _query_fb_region_map(es, STAT_FB_REACH, "", [], "", "")
        assert result == []

    def test_missing_coordinates_yields_null_lat_lon(self):
        """When the sample hit's matching region has no coordinates, lat/lon come back null."""
        es = _mock_es(_es_resp({
            "aggregations": {
                "regions": {
                    "buckets": [{
                        "key": "Nowhere",
                        "doc_count": 3,
                        "sample": {
                            "hits": {"hits": [{"_source": {
                                "location_regions": [{"name": "Nowhere"}],
                            }}]}
                        },
                        "stat": {"value": 50},
                    }]
                }
            }
        }))
        result = _query_fb_region_map(es, STAT_FB_REACH, "", [], "", "")
        assert len(result) == 1
        assert result[0]["name"] == "Nowhere"
        assert result[0]["value"] == 50
        assert result[0]["lat"] is None
        assert result[0]["lon"] is None


# ===========================================================================
# _query_fb_country_map
# ===========================================================================


class TestQueryFbCountryMap:
    """Facebook country-level map with fallback to team_country."""

    def test_primary_location_countries(self):
        es = _mock_es(_es_resp({
            "aggregations": {
                "countries": {
                    "buckets": [{
                        "key": "US",
                        "doc_count": 50,
                        "stat": {"value": 1000},
                    }]
                }
            }
        }))
        result = _query_fb_country_map(es, STAT_FB_REACH, "VOGZ", [], "2024-01-01", "2024-12-31")
        assert len(result) == 1
        # lat/lon resolved from canonical _COUNTRY_CENTROIDS table
        assert result[0] == {"name": "US", "iso2": "US", "lat": 37.09, "lon": -95.71, "value": 1000}

    def test_fallback_to_team_country(self):
        """When primary query returns empty, falls back to team_country."""
        es = mock.MagicMock()
        # First call (location_countries.iso2) returns empty
        # Second call (team_country) returns data
        es.search.side_effect = [
            _es_resp({"aggregations": {"countries": {"buckets": []}}}),
            _es_resp({
                "aggregations": {
                    "countries": {
                        "buckets": [{
                            "key": "IN",
                            "doc_count": 20,
                            "stat": {"value": 500},
                        }]
                    }
                }
            }),
        ]
        result = _query_fb_country_map(es, STAT_FB_REACH, "", [], "", "")
        assert len(result) == 1
        assert result[0]["name"] == "IN"
        assert result[0]["iso2"] == "IN"
        assert result[0]["value"] == 500

    def test_fallback_skips_unknown_and_full_country_names(self):
        es = mock.MagicMock()
        # Primary loop iterates two iso2 fields; both must be empty before fallback runs
        empty = _es_resp({"aggregations": {"countries": {"buckets": []}}})
        es.search.side_effect = [
            empty,
            empty,
            _es_resp({
                "aggregations": {
                    "countries": {
                        "buckets": [
                            {"key": "unknown", "doc_count": 5, "stat": {"value": 100}},
                            {"key": "Brazil", "doc_count": 10, "stat": {"value": 200}},
                            {"key": "BR", "doc_count": 8, "stat": {"value": 150}},
                        ]
                    }
                }
            }),
        ]
        result = _query_fb_country_map(es, STAT_FB_REACH, "", [], "", "")
        # "unknown" and "Brazil" (full name) skipped; only "BR" (ISO2) survives.
        assert len(result) == 1
        assert result[0]["iso2"] == "BR"

    def test_primary_exception_falls_back(self):
        es = mock.MagicMock()
        es.search.side_effect = [
            Exception("Primary query failed"),
            _es_resp({
                "aggregations": {
                    "countries": {
                        "buckets": [{
                            "key": "MX",
                            "doc_count": 8,
                            "stat": {"value": 300},
                        }]
                    }
                }
            }),
        ]
        result = _query_fb_country_map(es, STAT_FB_REACH, "", [], "", "")
        assert len(result) == 1
        assert result[0]["iso2"] == "MX"

    def test_both_queries_fail_returns_empty(self):
        es = mock.MagicMock()
        es.search.side_effect = Exception("All queries fail")
        result = _query_fb_country_map(es, STAT_FB_REACH, "", [], "", "")
        assert result == []


# ===========================================================================
# _query_echo_country_map
# ===========================================================================


class TestQueryEchoCountryMap:
    """Echo country map aggregation — prefers contact_country_iso2, falls back to team_country."""

    def test_contact_iso2_primary_path(self):
        """Primary path: aggregate by contact_country_iso2."""
        es = _mock_es(_es_resp({
            "aggregations": {
                "by_country": {
                    "buckets": [{
                        "key": "IN",
                        "doc_count": 30,
                        "total": {"value": 150},
                    }]
                }
            }
        }))
        result = _query_echo_country_map(es, STAT_ECHO_MESSAGES, "", [], "", "")
        assert len(result) == 1
        assert result[0]["name"] == "IN"
        assert result[0]["iso2"] == "IN"
        assert result[0]["iso3"] is None
        assert result[0]["value"] == 150

    def test_fallback_to_team_country(self):
        """When contact_country_iso2 is empty, falls back to team_country."""
        empty_resp = _es_resp({"aggregations": {"by_country": {"buckets": []}}})
        team_resp = _es_resp({
            "aggregations": {
                "by_country": {
                    "buckets": [{
                        "key": "India",
                        "doc_count": 30,
                        "total": {"value": 150},
                        "iso3_hit": {
                            "hits": {
                                "hits": [{"_source": {"team_country_iso3_code": "IND"}}]
                            }
                        },
                    }]
                }
            }
        })
        es = mock.MagicMock()
        # First two calls (contact_country_iso2.keyword, contact_country_iso2) return empty,
        # third call (team_country.keyword) returns data
        es.search.side_effect = [empty_resp, empty_resp, team_resp]
        result = _query_echo_country_map(es, STAT_ECHO_MESSAGES, "", [], "", "")
        assert len(result) == 1
        assert result[0]["name"] == "India"
        assert result[0]["iso3"] == "IND"
        assert result[0]["value"] == 150

    def test_empty_buckets(self):
        es = _mock_es(_es_resp({"aggregations": {"by_country": {"buckets": []}}}))
        result = _query_echo_country_map(es, STAT_ECHO_MESSAGES, "", [], "", "")
        assert result == []

    def test_unknown_country_skipped(self):
        es = _mock_es(_es_resp({
            "aggregations": {
                "by_country": {
                    "buckets": [{
                        "key": "Unknown",
                        "doc_count": 2,
                        "total": {"value": 10},
                    }]
                }
            }
        }))
        result = _query_echo_country_map(es, STAT_ECHO_MESSAGES, "", [], "", "")
        assert result == []

    def test_min_country_value_filter(self):
        """stat_cfg with min_country_value filters out low-value countries."""
        es = _mock_es(_es_resp({
            "aggregations": {
                "by_country": {
                    "buckets": [
                        {"key": "IN", "doc_count": 1, "total": {"value": 1}},
                        {"key": "EG", "doc_count": 5, "total": {"value": 10}},
                    ]
                }
            }
        }))
        result = _query_echo_country_map(es, STAT_ECHO_MULTIPLIER, "", [], "", "")
        assert len(result) == 1
        assert result[0]["name"] == "EG"

    def test_cardinality_contact_iso2_path(self):
        """Cardinality path: aggregate by contact_country_iso2 directly."""
        es = _mock_es(_es_resp({
            "aggregations": {
                "by_country": {
                    "buckets": [
                        {"key": "IN", "doc_count": 10, "total": {"value": 8}},
                        {"key": "EG", "doc_count": 5, "total": {"value": 3}},
                    ]
                }
            }
        }))
        result = _query_echo_country_map(es, STAT_ECHO_CONTACTS, "", [], "", "")
        assert len(result) == 2
        country_map = {p["name"]: p["value"] for p in result}
        assert country_map["IN"] == 8
        assert country_map["EG"] == 3

    @mock.patch(f"{_R}._load_dt_team_country_map")
    def test_cardinality_fallback_to_team_mapping(self, mock_team_map):
        """Cardinality: falls back to team -> country mapping when iso2 is empty."""
        mock_team_map.return_value = {"Team Alpha": "IN", "Team Beta": "EG"}
        empty_resp = _es_resp({"aggregations": {"by_country": {"buckets": []}}})
        team_resp = _es_resp({
            "aggregations": {
                "by_team": {
                    "buckets": [
                        {"key": "Team Alpha", "doc_count": 10, "total": {"value": 8}},
                        {"key": "Team Beta", "doc_count": 5, "total": {"value": 3}},
                    ]
                }
            }
        })
        es = mock.MagicMock()
        # First two calls (contact_country_iso2.keyword, contact_country_iso2) return empty,
        # third call (team_title.keyword) returns team data
        es.search.side_effect = [empty_resp, empty_resp, team_resp]
        result = _query_echo_country_map(es, STAT_ECHO_CONTACTS, "", [], "", "")
        assert len(result) == 2
        country_map = {p["name"]: p["value"] for p in result}
        assert country_map["IN"] == 8
        assert country_map["EG"] == 3

    @mock.patch(f"{_R}._load_dt_team_country_map")
    def test_cardinality_empty_team_map_returns_empty(self, mock_team_map):
        mock_team_map.return_value = {}
        empty_resp = _es_resp({"aggregations": {"by_country": {"buckets": []}}})
        es = mock.MagicMock()
        es.search.side_effect = [empty_resp, empty_resp]
        result = _query_echo_country_map(es, STAT_ECHO_CONTACTS, "", [], "", "")
        assert result == []

    def test_es_exception_returns_empty(self):
        es = mock.MagicMock()
        es.search.side_effect = Exception("ES unavailable")
        result = _query_echo_country_map(es, STAT_ECHO_MESSAGES, "", [], "", "")
        assert result == []


# ===========================================================================
# _query_google_ads_country_map
# ===========================================================================


class TestQueryGoogleAdsCountryMap:
    """Google Ads country map aggregation (geo documents)."""

    def test_normal_buckets(self):
        es = _mock_es(_es_resp({
            "aggregations": {
                "by_country": {
                    "buckets": [{
                        "key": "United States",
                        "doc_count": 100,
                        "total": {"value": 5000},
                        "iso2_hit": {
                            "hits": {
                                "hits": [{"_source": {"geo_country_code": "us"}}]
                            }
                        },
                    }]
                }
            }
        }))
        result = _query_google_ads_country_map(es, STAT_GOOGLE_ADS_INSTALLS, "", [], "", "")
        assert len(result) == 1
        assert result[0]["name"] == "United States"
        assert result[0]["iso2"] == "US"
        assert result[0]["value"] == 5000

    def test_empty_buckets(self):
        es = _mock_es(_es_resp({"aggregations": {"by_country": {"buckets": []}}}))
        result = _query_google_ads_country_map(es, STAT_GOOGLE_ADS_INSTALLS, "", [], "", "")
        assert result == []

    def test_zero_value_skipped(self):
        es = _mock_es(_es_resp({
            "aggregations": {
                "by_country": {
                    "buckets": [{
                        "key": "Brazil",
                        "doc_count": 2,
                        "total": {"value": 0},
                        "iso2_hit": {"hits": {"hits": []}},
                    }]
                }
            }
        }))
        result = _query_google_ads_country_map(es, STAT_GOOGLE_ADS_INSTALLS, "", [], "", "")
        assert result == []

    def test_empty_country_name_skipped(self):
        es = _mock_es(_es_resp({
            "aggregations": {
                "by_country": {
                    "buckets": [{
                        "key": "",
                        "doc_count": 1,
                        "total": {"value": 10},
                        "iso2_hit": {"hits": {"hits": []}},
                    }]
                }
            }
        }))
        result = _query_google_ads_country_map(es, STAT_GOOGLE_ADS_INSTALLS, "", [], "", "")
        assert result == []

    def test_es_exception_returns_empty(self):
        es = mock.MagicMock()
        es.search.side_effect = Exception("timeout")
        result = _query_google_ads_country_map(es, STAT_GOOGLE_ADS_INSTALLS, "", [], "", "")
        assert result == []


# ===========================================================================
# _query_google_ads_installs_geo_map
# ===========================================================================


class TestQueryGoogleAdsInstallsGeoMap:
    """Google Ads installs geo map (exists-based geo documents)."""

    def test_normal_buckets(self):
        es = _mock_es(_es_resp({
            "aggregations": {
                "by_country": {
                    "buckets": [{
                        "key": "India",
                        "doc_count": 50,
                        "total": {"value": 2000},
                        "country_code": {"buckets": [{"key": "IN", "doc_count": 50}]},
                    }]
                }
            }
        }))
        result = _query_google_ads_installs_geo_map(es, STAT_GOOGLE_ADS_INSTALLS, "", [], "", "")
        assert len(result) == 1
        assert result[0] == {"name": "India", "iso2": "IN", "lat": None, "lon": None, "value": 2000}

    def test_empty_buckets(self):
        es = _mock_es(_es_resp({"aggregations": {"by_country": {"buckets": []}}}))
        result = _query_google_ads_installs_geo_map(es, STAT_GOOGLE_ADS_INSTALLS, "", [], "", "")
        assert result == []

    def test_zero_value_skipped(self):
        es = _mock_es(_es_resp({
            "aggregations": {
                "by_country": {
                    "buckets": [{
                        "key": "France",
                        "doc_count": 1,
                        "total": {"value": 0},
                        "country_code": {"buckets": [{"key": "FR", "doc_count": 1}]},
                    }]
                }
            }
        }))
        result = _query_google_ads_installs_geo_map(es, STAT_GOOGLE_ADS_INSTALLS, "", [], "", "")
        assert result == []

    def test_unknown_country_skipped(self):
        es = _mock_es(_es_resp({
            "aggregations": {
                "by_country": {
                    "buckets": [{
                        "key": "unknown",
                        "doc_count": 3,
                        "total": {"value": 100},
                        "country_code": {"buckets": []},
                    }]
                }
            }
        }))
        result = _query_google_ads_installs_geo_map(es, STAT_GOOGLE_ADS_INSTALLS, "", [], "", "")
        assert result == []

    def test_no_country_code_bucket(self):
        """Country code is None when no sub-buckets exist."""
        es = _mock_es(_es_resp({
            "aggregations": {
                "by_country": {
                    "buckets": [{
                        "key": "Mexico",
                        "doc_count": 10,
                        "total": {"value": 500},
                        "country_code": {"buckets": []},
                    }]
                }
            }
        }))
        result = _query_google_ads_installs_geo_map(es, STAT_GOOGLE_ADS_INSTALLS, "", [], "", "")
        assert len(result) == 1
        assert result[0]["iso2"] is None
        assert result[0]["value"] == 500

    def test_es_exception_returns_empty(self):
        es = mock.MagicMock()
        es.search.side_effect = Exception("connection refused")
        result = _query_google_ads_installs_geo_map(es, STAT_GOOGLE_ADS_INSTALLS, "", [], "", "")
        assert result == []


# ===========================================================================
# _query_ga4_country_map
# ===========================================================================


class TestQueryGa4CountryMap:
    """GA4 country map aggregation."""

    def test_normal_buckets(self):
        es = _mock_es(_es_resp({
            "aggregations": {
                "by_country": {
                    "buckets": [
                        {"key": "United States", "doc_count": 100, "total": {"value": 5000}},
                        {"key": "India", "doc_count": 80, "total": {"value": 3000}},
                    ]
                }
            }
        }))
        result = _query_ga4_country_map(es, STAT_GA4_WEB, "", [], "", "")
        assert len(result) == 2
        assert result[0] == {"name": "United States", "iso2": None, "lat": None, "lon": None, "value": 5000}

    def test_empty_buckets(self):
        es = _mock_es(_es_resp({"aggregations": {"by_country": {"buckets": []}}}))
        result = _query_ga4_country_map(es, STAT_GA4_WEB, "", [], "", "")
        assert result == []

    def test_not_set_country_skipped(self):
        es = _mock_es(_es_resp({
            "aggregations": {
                "by_country": {
                    "buckets": [
                        {"key": "(not set)", "doc_count": 50, "total": {"value": 200}},
                        {"key": "Brazil", "doc_count": 30, "total": {"value": 100}},
                    ]
                }
            }
        }))
        result = _query_ga4_country_map(es, STAT_GA4_WEB, "", [], "", "")
        assert len(result) == 1
        assert result[0]["name"] == "Brazil"

    def test_zero_value_skipped(self):
        es = _mock_es(_es_resp({
            "aggregations": {
                "by_country": {
                    "buckets": [{"key": "Japan", "doc_count": 0, "total": {"value": 0}}]
                }
            }
        }))
        result = _query_ga4_country_map(es, STAT_GA4_WEB, "", [], "", "")
        assert result == []

    def test_es_exception_returns_empty(self):
        es = mock.MagicMock()
        es.search.side_effect = Exception("timeout")
        result = _query_ga4_country_map(es, STAT_GA4_WEB, "", [], "", "")
        assert result == []


# ===========================================================================
# _query_youtube_country_map
# ===========================================================================


class TestQueryYoutubeCountryMap:
    """YouTube country map aggregation."""

    def test_normal_buckets(self):
        es = _mock_es(_es_resp({
            "aggregations": {
                "by_country": {
                    "buckets": [
                        {"key": "US", "doc_count": 200, "total": {"value": 1500}},
                        {"key": "GB", "doc_count": 50, "total": {"value": 300}},
                    ]
                }
            }
        }))
        result = _query_youtube_country_map(es, STAT_YOUTUBE_SUBS, "", [], "", "")
        assert len(result) == 2
        assert result[0] == {"name": "US", "iso2": "US", "lat": None, "lon": None, "value": 1500}
        assert result[1]["iso2"] == "GB"

    def test_empty_buckets(self):
        es = _mock_es(_es_resp({"aggregations": {"by_country": {"buckets": []}}}))
        result = _query_youtube_country_map(es, STAT_YOUTUBE_SUBS, "", [], "", "")
        assert result == []

    def test_zero_value_skipped(self):
        es = _mock_es(_es_resp({
            "aggregations": {
                "by_country": {
                    "buckets": [{"key": "DE", "doc_count": 0, "total": {"value": 0}}]
                }
            }
        }))
        result = _query_youtube_country_map(es, STAT_YOUTUBE_SUBS, "", [], "", "")
        assert result == []

    def test_unknown_country_skipped(self):
        es = _mock_es(_es_resp({
            "aggregations": {
                "by_country": {
                    "buckets": [{"key": "unknown", "doc_count": 3, "total": {"value": 10}}]
                }
            }
        }))
        result = _query_youtube_country_map(es, STAT_YOUTUBE_SUBS, "", [], "", "")
        assert result == []

    def test_es_exception_returns_empty(self):
        es = mock.MagicMock()
        es.search.side_effect = Exception("cluster error")
        result = _query_youtube_country_map(es, STAT_YOUTUBE_SUBS, "", [], "", "")
        assert result == []


# ===========================================================================
# _query_echo_map
# ===========================================================================


class TestQueryEchoMap:
    """Echo map — aggregates by contact_country_iso2, plots at country centroids."""

    def test_normal_buckets(self):
        es = _mock_es(_es_resp({
            "aggregations": {
                "by_country": {
                    "buckets": [{
                        "key": "IN",
                        "doc_count": 15,
                        "total": {"value": 80},
                    }]
                }
            }
        }))
        result = _query_echo_map(es, STAT_ECHO_MESSAGES, "", [], "", "")
        assert len(result) == 1
        assert result[0]["name"] == "IN"
        assert result[0]["value"] == 80
        # Lat/lon come from _COUNTRY_CENTROIDS for India
        assert result[0]["lat"] == pytest.approx(20.59, abs=0.1)
        assert result[0]["lon"] == pytest.approx(78.96, abs=0.1)

    def test_unknown_iso2_without_centroid_skipped(self):
        """ISO2 codes not in _COUNTRY_CENTROIDS are skipped."""
        es = _mock_es(_es_resp({
            "aggregations": {
                "by_country": {
                    "buckets": [{"key": "XX", "doc_count": 2, "total": {"value": 5}}]
                }
            }
        }))
        result = _query_echo_map(es, STAT_ECHO_MESSAGES, "", [], "", "")
        assert result == []

    def test_empty_buckets(self):
        es = _mock_es(_es_resp({"aggregations": {"by_country": {"buckets": []}}}))
        result = _query_echo_map(es, STAT_ECHO_MESSAGES, "", [], "", "")
        assert result == []

    def test_es_exception_returns_empty(self):
        es = mock.MagicMock()
        es.search.side_effect = Exception("network error")
        result = _query_echo_map(es, STAT_ECHO_MESSAGES, "", [], "", "")
        assert result == []

    def test_no_field_uses_doc_count(self):
        """When stat_cfg has no field, value falls back to doc_count."""
        stat_no_field = {"data_source": "echo", "field": None}
        es = _mock_es(_es_resp({
            "aggregations": {
                "by_country": {
                    "buckets": [{"key": "US", "doc_count": 12}]
                }
            }
        }))
        result = _query_echo_map(es, stat_no_field, "", [], "", "")
        assert len(result) == 1
        assert result[0]["value"] == 12


# ===========================================================================
# _query_team_country_map_generic
# ===========================================================================


class TestQueryTeamCountryMapGeneric:
    """Generic team country map (fb_page_insights, ig_insights, etc.)."""

    def test_normal_buckets(self):
        es = _mock_es(_es_resp({
            "aggregations": {
                "by_country": {
                    "buckets": [
                        {"key": "IN", "doc_count": 40, "total": {"value": 2000}},
                        {"key": "Egypt", "doc_count": 20, "total": {"value": 500}},
                    ]
                }
            }
        }))
        result = _query_team_country_map_generic(es, "*fb_page_insights", STAT_ORGANIC_FB, "", [], "", "")
        assert len(result) == 2
        assert result[0]["name"] == "IN"
        assert result[0]["iso2"] == "IN"  # 2-char is treated as ISO2
        assert result[1]["name"] == "Egypt"
        assert result[1]["iso2"] is None  # longer name is not iso2

    def test_empty_buckets_tries_fallback(self):
        """First field returns empty, second returns empty -> empty result."""
        es = mock.MagicMock()
        es.search.side_effect = [
            _es_resp({"aggregations": {"by_country": {"buckets": []}}}),
            _es_resp({"aggregations": {"by_country": {"buckets": []}}}),
        ]
        result = _query_team_country_map_generic(es, "*ig_insights", STAT_ORGANIC_FB, "", [], "", "")
        assert result == []

    def test_keyword_field_preferred_over_plain(self):
        """When .keyword returns data, plain field is not tried."""
        es = _mock_es(_es_resp({
            "aggregations": {
                "by_country": {
                    "buckets": [{"key": "TR", "doc_count": 10, "total": {"value": 300}}]
                }
            }
        }))
        result = _query_team_country_map_generic(es, "*ig_insights", STAT_ORGANIC_FB, "", [], "", "")
        assert len(result) == 1
        # Only one search call (keyword worked)
        assert es.search.call_count == 1

    def test_unknown_country_skipped(self):
        es = _mock_es(_es_resp({
            "aggregations": {
                "by_country": {
                    "buckets": [{"key": "Unknown", "doc_count": 5, "total": {"value": 100}}]
                }
            }
        }))
        result = _query_team_country_map_generic(es, "*fb_page_insights", STAT_ORGANIC_FB, "", [], "", "")
        # Unknown is skipped -> empty from keyword, tries plain field
        # But same mock returns same unknown -> both empty
        assert result == []

    def test_es_exception_returns_empty(self):
        es = mock.MagicMock()
        es.search.side_effect = Exception("mapping error")
        result = _query_team_country_map_generic(es, "*fb_page_insights", STAT_ORGANIC_FB, "", [], "", "")
        assert result == []


# ===========================================================================
# _query_team_title_country_map
# ===========================================================================


class TestQueryTeamTitleCountryMap:
    """Team title -> country map via de_asset_teams."""

    @mock.patch(f"{_R}._load_dt_team_country_map")
    def test_normal_aggregation(self, mock_team_map):
        mock_team_map.return_value = {"Alpha Team": "US", "Beta Team": "IN"}
        es = _mock_es_team_title([
            {"key": "Alpha Team", "doc_count": 50, "total": {"value": 1000}},
            {"key": "Beta Team", "doc_count": 30, "total": {"value": 500}},
        ])
        result = _query_team_title_country_map(es, "*youtube_analytics", STAT_YOUTUBE_SUBS, "", [], "", "")
        assert len(result) == 2
        country_map = {p["name"]: p["value"] for p in result}
        assert country_map["US"] == 1000
        assert country_map["IN"] == 500

    @mock.patch(f"{_R}._load_dt_team_country_map")
    def test_same_country_teams_summed(self, mock_team_map):
        mock_team_map.return_value = {"Team A": "EG", "Team B": "EG"}
        es = _mock_es_team_title([
            {"key": "Team A", "doc_count": 10, "total": {"value": 200}},
            {"key": "Team B", "doc_count": 5, "total": {"value": 100}},
        ])
        result = _query_team_title_country_map(es, "*youtube_analytics", STAT_YOUTUBE_SUBS, "", [], "", "")
        assert len(result) == 1
        assert result[0]["name"] == "EG"
        assert result[0]["value"] == 300

    @mock.patch(f"{_R}._load_dt_team_country_map")
    def test_empty_team_map_returns_empty(self, mock_team_map):
        mock_team_map.return_value = {}
        es = mock.MagicMock()
        result = _query_team_title_country_map(es, "*youtube_analytics", STAT_YOUTUBE_SUBS, "", [], "", "")
        assert result == []

    @mock.patch(f"{_R}._load_dt_team_country_map")
    def test_unknown_team_skipped(self, mock_team_map):
        mock_team_map.return_value = {"Known Team": "US"}
        es = _mock_es_team_title([
            {"key": "Known Team", "doc_count": 10, "total": {"value": 100}},
            {"key": "Mystery Team", "doc_count": 5, "total": {"value": 50}},
        ])
        result = _query_team_title_country_map(es, "*youtube_analytics", STAT_YOUTUBE_SUBS, "", [], "", "")
        assert len(result) == 1
        assert result[0]["value"] == 100

    @mock.patch(f"{_R}._load_dt_team_country_map")
    def test_es_exception_returns_empty(self, mock_team_map):
        mock_team_map.return_value = {"Team": "US"}
        es = mock.MagicMock()
        es.search.side_effect = Exception("index_not_found")
        result = _query_team_title_country_map(es, "*youtube_analytics", STAT_YOUTUBE_SUBS, "", [], "", "")
        assert result == []


# ===========================================================================
# _query_dt_map
# ===========================================================================


class TestQueryDtMap:
    """Disciple.Tools address-level map with geocoding."""

    @mock.patch(f"{_R}._geocode_address")
    @mock.patch(f"{_R}._load_dt_grid_names", return_value={"12345": "Istanbul"})
    def test_grid_and_address_buckets(self, mock_grid, mock_geocode):
        mock_geocode.side_effect = lambda name, *a: (41.0, 29.0) if name == "Istanbul" else (30.0, 31.2) if name == "Cairo" else None
        es = _mock_es(_es_resp({
            "aggregations": {
                "by_grid": {
                    "buckets": [{"key": "12345", "doc_count": 20}]
                },
                "by_address": {
                    "buckets": [{"key": "Cairo", "doc_count": 5}]
                },
            }
        }))
        result = _query_dt_map(es, STAT_DT_CONTACTS, "", [], "", "")
        assert len(result) == 2
        names = {p["name"] for p in result}
        assert "Istanbul" in names
        assert "Cairo" in names

    @mock.patch(f"{_R}._geocode_address", return_value=None)
    @mock.patch(f"{_R}._load_dt_grid_names", return_value={})
    def test_empty_grid_names(self, mock_grid, mock_geocode):
        es = _mock_es(_es_resp({
            "aggregations": {
                "by_grid": {"buckets": []},
                "by_address": {"buckets": []},
            }
        }))
        result = _query_dt_map(es, STAT_DT_CONTACTS, "", [], "", "")
        assert result == []

    @mock.patch(f"{_R}._geocode_address", return_value=(40.0, 29.0))
    @mock.patch(f"{_R}._load_dt_grid_names", return_value={"111": "CityA", "222": "CityB"})
    def test_duplicate_coordinates_deduplicated(self, mock_grid, mock_geocode):
        """Two grid entries geocoding to same coords should produce one point."""
        es = _mock_es(_es_resp({
            "aggregations": {
                "by_grid": {
                    "buckets": [
                        {"key": "111", "doc_count": 10},
                        {"key": "222", "doc_count": 5},
                    ]
                },
                "by_address": {"buckets": []},
            }
        }))
        result = _query_dt_map(es, STAT_DT_CONTACTS, "", [], "", "")
        assert len(result) == 1

    @mock.patch(f"{_R}._geocode_address", return_value=(35.0, 33.0))
    @mock.patch(f"{_R}._load_dt_grid_names", return_value={})
    def test_address_only_mode(self, mock_grid, mock_geocode):
        """With address_only=True, by_grid is not used."""
        es = _mock_es(_es_resp({
            "aggregations": {
                "by_address": {
                    "buckets": [{"key": "Nicosia", "doc_count": 3}]
                }
            }
        }))
        result = _query_dt_map(es, STAT_DT_SALVATION, "", [], "", "")
        assert len(result) == 1
        assert result[0]["name"] == "Nicosia"


# ===========================================================================
# _query_dt_country_map
# ===========================================================================


class TestQueryDtCountryMap:
    """Disciple.Tools country-level map via geocoding."""

    @mock.patch(f"{_R}._geocode_cache", {"Istanbul": (41.0, 29.0, "Turkey"), "Cairo": (30.0, 31.2, "Egypt")})
    @mock.patch("lib.geocoding.country_from_coords", side_effect=lambda lat, lon: "TR" if lat == 41.0 else "EG")
    @mock.patch(f"{_R}._load_geocode_cache")
    @mock.patch(f"{_R}._geocode_address")
    @mock.patch(f"{_R}._load_dt_grid_names", return_value={"12345": "Istanbul"})
    def test_normal_country_aggregation(self, mock_grid, mock_geocode, mock_load_cache, mock_country):
        mock_geocode.return_value = (41.0, 29.0)
        es = _mock_es(_es_resp({
            "aggregations": {
                "by_grid": {"buckets": [{"key": "12345", "doc_count": 20}]},
                "by_address": {"buckets": [{"key": "Cairo", "doc_count": 5}]},
            }
        }))
        result = _query_dt_country_map(es, STAT_DT_CONTACTS, "", [], "", "")
        # Result is keyed by ISO2 (resolved via country_from_coords), not country name
        country_map = {p["name"]: p["value"] for p in result}
        assert country_map.get("TR") == 20
        assert country_map.get("EG") == 5

    def test_es_exception_returns_empty(self):
        es = mock.MagicMock()
        es.search.side_effect = Exception("index missing")
        result = _query_dt_country_map(es, STAT_DT_CONTACTS, "", [], "", "")
        assert result == []

    @mock.patch(f"{_R}._load_geocode_cache")
    @mock.patch(f"{_R}._geocode_address", return_value=None)
    @mock.patch(f"{_R}._load_dt_grid_names", return_value={})
    def test_empty_aggregations(self, mock_grid, mock_geocode, mock_load_cache):
        es = _mock_es(_es_resp({
            "aggregations": {
                "by_grid": {"buckets": []},
                "by_address": {"buckets": []},
            }
        }))
        result = _query_dt_country_map(es, STAT_DT_CONTACTS, "", [], "", "")
        assert result == []


# ===========================================================================
# _query_dt_team_country_map
# ===========================================================================


class TestQueryDtTeamCountryMap:
    """DT contacts aggregated by team -> country mapping."""

    @mock.patch(f"{_R}._load_dt_team_country_map")
    def test_normal_aggregation(self, mock_team_map):
        mock_team_map.return_value = {"Alpha": "TR", "Beta": "EG"}
        es = _mock_es_team_title([
            {"key": "Alpha", "doc_count": 30},
            {"key": "Beta", "doc_count": 15},
        ])
        result = _query_dt_team_country_map(es, STAT_DT_CONTACTS, "", [], "", "")
        assert len(result) == 2
        country_map = {p["name"]: p["value"] for p in result}
        assert country_map["TR"] == 30
        assert country_map["EG"] == 15

    @mock.patch(f"{_R}._load_dt_team_country_map")
    def test_same_country_summed(self, mock_team_map):
        mock_team_map.return_value = {"Team A": "IN", "Team B": "IN"}
        es = _mock_es_team_title([
            {"key": "Team A", "doc_count": 10},
            {"key": "Team B", "doc_count": 20},
        ])
        result = _query_dt_team_country_map(es, STAT_DT_CONTACTS, "", [], "", "")
        assert len(result) == 1
        assert result[0]["value"] == 30

    @mock.patch(f"{_R}._load_dt_team_country_map")
    def test_empty_team_map_returns_empty(self, mock_team_map):
        mock_team_map.return_value = {}
        es = mock.MagicMock()
        result = _query_dt_team_country_map(es, STAT_DT_CONTACTS, "", [], "", "")
        assert result == []

    @mock.patch(f"{_R}._load_dt_team_country_map")
    def test_keyword_fallback_on_exception(self, mock_team_map):
        """First search (team_title.keyword) fails, falls back to plain team_title."""
        mock_team_map.return_value = {"Alpha": "US"}
        es = mock.MagicMock()
        es.search.side_effect = [
            Exception("field not found"),
            _es_resp({
                "aggregations": {
                    "by_team": {
                        "buckets": [{"key": "Alpha", "doc_count": 10}]
                    }
                }
            }),
        ]
        result = _query_dt_team_country_map(es, STAT_DT_CONTACTS, "", [], "", "")
        assert len(result) == 1
        assert result[0]["name"] == "US"

    @mock.patch(f"{_R}._load_dt_team_country_map")
    def test_both_searches_fail_returns_empty(self, mock_team_map):
        mock_team_map.return_value = {"Alpha": "US"}
        es = mock.MagicMock()
        es.search.side_effect = Exception("all fail")
        result = _query_dt_team_country_map(es, STAT_DT_CONTACTS, "", [], "", "")
        assert result == []


# ===========================================================================
# GET /dashboard-builder/api/map-data (route-level integration tests)
# ===========================================================================


class TestMapDataRoute:
    """Route-level tests for the map-data endpoint."""

    def test_unauthenticated_redirects(self, client):
        resp = client.get("/dashboard-builder/api/map-data")
        assert resp.status_code == 401
        assert "login" in (resp.get_json() or {}).get("login_url", "").lower()

    @mock.patch(f"{_R}.get_elasticsearch_client", return_value=None)
    def test_es_unavailable_returns_503(self, mock_es, auth_client):
        resp = auth_client.get("/dashboard-builder/api/map-data?statistic=reach")
        assert resp.status_code == 503
        data = resp.get_json()
        assert data["success"] is False
        assert "unavailable" in data["error"].lower()

    def test_unknown_statistic_returns_400(self, auth_client):
        resp = auth_client.get("/dashboard-builder/api/map-data?statistic=nonexistent_stat_xyz")
        assert resp.status_code == 400
        data = resp.get_json()
        assert data["success"] is False

    @mock.patch(f"{_R}._query_fb_country_map", return_value=[{"name": "Egypt", "iso2": "EG", "value": 100}])
    @mock.patch(f"{_R}._query_fb_region_map", return_value=[])
    @mock.patch(f"{_R}._query_fb_map", return_value=[])
    @mock.patch(f"{_R}.get_elasticsearch_client")
    def test_fb_insights_dispatches_correctly(self, mock_get_es, mock_fb, mock_region, mock_country, auth_client):
        """FB country path no longer loads cities; cities live on
        /api/map-data/cities. _query_fb_map (the live-geocoding helper) must
        not be invoked from the country render."""
        mock_get_es.return_value = mock.MagicMock()
        resp = auth_client.get("/dashboard-builder/api/map-data?statistic=reach&org=VOGZ")
        assert resp.status_code == 200
        data = resp.get_json()
        assert data["success"] is True
        assert data["cities"] == []
        assert data["countries"] == [{"name": "Egypt", "iso2": "EG", "value": 100}]
        assert "points" not in data
        mock_country.assert_called_once()
        mock_region.assert_called_once()
        mock_fb.assert_not_called()

    @mock.patch(f"{_R}._query_echo_country_map", return_value=[{"name": "India", "iso2": None, "lat": None, "lon": None, "value": 50}])
    @mock.patch(f"{_R}._query_echo_map", return_value=[])
    @mock.patch(f"{_R}.get_elasticsearch_client")
    def test_echo_dispatches_correctly(self, mock_get_es, mock_echo, mock_echo_country, auth_client):
        mock_get_es.return_value = mock.MagicMock()
        resp = auth_client.get("/dashboard-builder/api/map-data?statistic=messages")
        assert resp.status_code == 200
        data = resp.get_json()
        assert data["success"] is True
        assert len(data["countries"]) == 1

    @mock.patch(f"{_R}._query_google_ads_installs_geo_map", return_value=[{"name": "US", "iso2": "US", "lat": None, "lon": None, "value": 1000}])
    @mock.patch(f"{_R}.get_elasticsearch_client")
    def test_google_ads_installs_dispatches(self, mock_get_es, mock_geo, auth_client):
        mock_get_es.return_value = mock.MagicMock()
        resp = auth_client.get("/dashboard-builder/api/map-data?statistic=google_ads_app_installs")
        assert resp.status_code == 200
        data = resp.get_json()
        assert data["success"] is True
        assert len(data["countries"]) == 1

    @mock.patch(f"{_R}._query_ga4_country_map", return_value=[])
    @mock.patch(f"{_R}.get_elasticsearch_client")
    def test_ga4_dispatches_correctly(self, mock_get_es, mock_ga4, auth_client):
        mock_get_es.return_value = mock.MagicMock()
        resp = auth_client.get("/dashboard-builder/api/map-data?statistic=organic_web")
        assert resp.status_code == 200
        data = resp.get_json()
        assert data["success"] is True

    @mock.patch(f"{_R}._query_team_title_country_map", return_value=[])
    @mock.patch(f"{_R}._query_youtube_country_map", return_value=[])
    @mock.patch(f"{_R}.get_elasticsearch_client")
    def test_youtube_fallback_to_team_title(self, mock_get_es, mock_yt, mock_tt, auth_client):
        """When youtube country returns empty, falls back to team_title_country_map."""
        mock_get_es.return_value = mock.MagicMock()
        resp = auth_client.get("/dashboard-builder/api/map-data?statistic=follows_youtube")
        assert resp.status_code == 200
        # Both youtube and fallback were called
        mock_yt.assert_called_once()
        mock_tt.assert_called_once()

    @mock.patch(f"{_R}._query_team_country_map_generic", return_value=[])
    @mock.patch(f"{_R}.get_elasticsearch_client")
    def test_fb_page_insights_uses_generic(self, mock_get_es, mock_generic, auth_client):
        mock_get_es.return_value = mock.MagicMock()
        resp = auth_client.get("/dashboard-builder/api/map-data?statistic=organic_facebook")
        assert resp.status_code == 200
        mock_generic.assert_called_once()

    @mock.patch(f"{_R}._query_dt_country_map", return_value=[])
    @mock.patch(f"{_R}._query_dt_map", return_value=[])
    @mock.patch(f"{_R}.get_elasticsearch_client")
    def test_dt_dispatches_correctly(self, mock_get_es, mock_dt, mock_dt_country, auth_client):
        mock_get_es.return_value = mock.MagicMock()
        resp = auth_client.get("/dashboard-builder/api/map-data?statistic=dt_contacts")
        assert resp.status_code == 200
        data = resp.get_json()
        assert data["success"] is True
        mock_dt.assert_called_once()
        mock_dt_country.assert_called_once()

    def test_default_statistic_is_reach(self, auth_client):
        """When no statistic param, defaults to 'reach' (fb_insights). After
         the FB country render goes through region+country only —
        cities are served separately on /api/map-data/cities."""
        with mock.patch(f"{_R}.get_elasticsearch_client") as mock_get_es, \
             mock.patch(f"{_R}._query_fb_map") as mock_fb, \
             mock.patch(f"{_R}._query_fb_region_map", return_value=[]) as mock_region, \
             mock.patch(f"{_R}._query_fb_country_map", return_value=[]) as mock_country:
            mock_get_es.return_value = mock.MagicMock()
            resp = auth_client.get("/dashboard-builder/api/map-data")
            assert resp.status_code == 200
            mock_country.assert_called_once()
            mock_region.assert_called_once()
            mock_fb.assert_not_called()


# ===========================================================================
# /api/map-data/cities — second-paint endpoint
# ===========================================================================


class TestMapCityDataRoute:
    """Route dispatch for the cache-only city endpoint."""

    @mock.patch(f"{_R}.get_elasticsearch_client", return_value=None)
    def test_es_unavailable_returns_503(self, mock_es, auth_client):
        resp = auth_client.get("/dashboard-builder/api/map-data/cities?statistic=reach")
        assert resp.status_code == 503
        data = resp.get_json()
        assert data["success"] is False

    def test_unknown_statistic_returns_400(self, auth_client):
        resp = auth_client.get("/dashboard-builder/api/map-data/cities?statistic=nonexistent_stat_xyz")
        assert resp.status_code == 400

    @mock.patch(f"{_R}._query_fb_map_cached_only", return_value=([{"name": "Cairo", "lat": 30.0, "lon": 31.2, "value": 100, "country": "EG"}], 5, 3))
    @mock.patch(f"{_R}.get_elasticsearch_client")
    def test_fb_returns_resolved_cities_and_counts(self, mock_get_es, mock_fb, auth_client):
        """Counts come back as total_cities / resolved_cities so the UI can
        show 'N of M cities shown'. _query_fb_map_cached_only returns a
        3-tuple; we don't unwrap a fake 1-element here so the test exercises
        the real shape."""
        mock_get_es.return_value = mock.MagicMock()
        resp = auth_client.get("/dashboard-builder/api/map-data/cities?statistic=reach&org=VOGZ")
        assert resp.status_code == 200
        data = resp.get_json()
        assert data["success"] is True
        # Returned tuple: (points, total, resolved). Note the helper returned
        # 1 point but reported total=5, resolved=3 — endpoint forwards both
        # counts unchanged so partial-coverage UX works.
        assert data["total_cities"] == 5
        assert data["resolved_cities"] == 3
        assert data["cities"][0]["name"] == "Cairo"
        mock_fb.assert_called_once()

    @mock.patch(f"{_R}._query_fb_map_cached_only", return_value=([], 0, 0))
    @mock.patch(f"{_R}.get_elasticsearch_client")
    def test_fb_empty_cache_returns_empty_payload(self, mock_get_es, mock_fb, auth_client):
        mock_get_es.return_value = mock.MagicMock()
        resp = auth_client.get("/dashboard-builder/api/map-data/cities?statistic=reach")
        assert resp.status_code == 200
        data = resp.get_json()
        assert data == {"success": True, "cities": [], "total_cities": 0, "resolved_cities": 0}

    @mock.patch(f"{_R}._query_fb_map_cached_only")
    @mock.patch(f"{_R}._query_echo_map")
    @mock.patch(f"{_R}._query_dt_map")
    @mock.patch(f"{_R}.get_elasticsearch_client")
    def test_non_fb_short_circuits_to_empty(self, mock_get_es, mock_dt, mock_echo, mock_fb, auth_client):
        """Non-FB data sources keep their inline cities on /api/map-data;
        this endpoint short-circuits for them so the frontend can call it
        unconditionally without branching."""
        mock_get_es.return_value = mock.MagicMock()
        resp = auth_client.get("/dashboard-builder/api/map-data/cities?statistic=messages")
        assert resp.status_code == 200
        data = resp.get_json()
        assert data == {"success": True, "cities": [], "total_cities": 0, "resolved_cities": 0}
        # None of the per-source helpers should have been touched
        mock_fb.assert_not_called()
        mock_echo.assert_not_called()
        mock_dt.assert_not_called()
