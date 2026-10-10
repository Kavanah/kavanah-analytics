"""
Tests for the Google Ads pipeline scripts.

Covers:
  import_scripts.google_ads.send_json_to_elastic:
    read_json_files         — yields JSON from directory tree
    send_to_elasticsearch   — idempotent ES index with doc_id
    create_google_ads_index — ensures index with proper mapping

  import_scripts.google_ads.get_campaign_data:
    get_google_ads_customers     — ES lookup (mocked)
    load_google_ads_credentials — YAML developer-token base

SDK packages are shimmed in conftest.py.
"""

import io
import json
import logging
import pytest
from unittest import mock

import import_scripts.google_ads.send_json_to_elastic as gads_send
import import_scripts.google_ads.get_campaign_data as gads_get


# ---------------------------------------------------------------------------
# read_json_files
# ---------------------------------------------------------------------------


class TestReadJsonFiles:

    def test_yields_parsed_json(self):
        payload = {"campaign_id": "c1", "impressions": 200}
        fake_walk = [("/data/google_ads", [], ["c1.json"])]

        with (
            mock.patch("import_scripts.google_ads.send_json_to_elastic.os.walk", return_value=fake_walk),
            mock.patch("builtins.open", mock.mock_open(read_data=json.dumps(payload))),
        ):
            results = list(gads_send.read_json_files("/data/google_ads"))

        assert len(results) == 1
        assert results[0]["campaign_id"] == "c1"

    def test_skips_non_json_files(self):
        fake_walk = [("/data", [], ["notes.txt", "data.json"])]
        payload = {"campaign_id": "c2"}

        with (
            mock.patch("import_scripts.google_ads.send_json_to_elastic.os.walk", return_value=fake_walk),
            mock.patch("builtins.open", mock.mock_open(read_data=json.dumps(payload))),
        ):
            results = list(gads_send.read_json_files("/data"))

        assert len(results) == 1

    def test_empty_directory_yields_nothing(self):
        fake_walk = [("/data", [], [])]
        with mock.patch("import_scripts.google_ads.send_json_to_elastic.os.walk", return_value=fake_walk):
            assert list(gads_send.read_json_files("/data")) == []

    def test_skips_malformed_json_without_raising(self):
        fake_walk = [("/data", [], ["bad.json"])]
        with (
            mock.patch("import_scripts.google_ads.send_json_to_elastic.os.walk", return_value=fake_walk),
            mock.patch("builtins.open", mock.mock_open(read_data="not-valid-json")),
        ):
            # Should not raise — bad JSON is logged and skipped
            results = list(gads_send.read_json_files("/data"))
        assert results == []


# ---------------------------------------------------------------------------
# send_to_elasticsearch
# ---------------------------------------------------------------------------


class TestSendToElasticsearch:

    def test_indexes_document_with_doc_id(self):
        es = mock.MagicMock()
        data = {"campaign_id": "c1", "date": "2024-01-01"}
        gads_send.send_to_elasticsearch(es, "myteam_google_ads", data, "c1-2024-01-01")
        es.index.assert_called_once_with(
            index="myteam_google_ads",
            body=data,
            id="c1-2024-01-01",
        )

    def test_logs_error_on_exception(self):
        es = mock.MagicMock()
        es.index.side_effect = Exception("ES unavailable")
        # Should not raise
        gads_send.send_to_elasticsearch(es, "myteam_google_ads", {}, "doc-1")


# ---------------------------------------------------------------------------
# create_google_ads_index
# ---------------------------------------------------------------------------


class TestCreateGoogleAdsIndex:

    def test_calls_create_index_if_not_exists(self):
        es = mock.MagicMock()
        with mock.patch("import_scripts.google_ads.send_json_to_elastic.create_index_if_not_exists") as mock_create:
            gads_send.create_google_ads_index(es, "myteam_google_ads")
        mock_create.assert_called_once()
        # First arg is the ES client, second is the index name
        args = mock_create.call_args[0]
        assert args[0] is es
        assert args[1] == "myteam_google_ads"
        # the third arg is the mappings *body* — properties directly,
        # NOT a nested {"mappings": ...} (the helper adds that wrapper). Asserting
        # the un-wrapped shape is the regression guard for the double-wrap bug.
        assert "properties" in args[2]
        assert "mappings" not in args[2]

    def test_create_sends_single_mappings_layer(self):
        """create_google_ads_index built {"mappings": {"properties":...}}
        and create_index_if_not_exists wrapped it again, so ES received
        {"mappings": {"mappings": ...}} -> mapper_parsing_exception (swallowed,
        falling back to dynamic mapping). Exercise the REAL helper and assert ES
        gets exactly one mappings layer with the intended explicit field types.

        Mutation (verified): re-add the outer "mappings" wrapper in
        create_google_ads_index -> body["mappings"] nests another "mappings" and
        these asserts fail.
        """
        es = mock.MagicMock()
        es.indices.exists.return_value = False
        gads_send.create_google_ads_index(es, "myteam_google_ads")

        body = es.indices.create.call_args.kwargs["body"]
        assert set(body.keys()) == {"mappings"}
        props = body["mappings"]["properties"]
        # exactly one mappings layer — no nested re-wrap
        assert "mappings" not in body["mappings"]
        # intended explicit field types are enforced, not dynamically inferred
        assert props["ad_id"] == {"type": "keyword"}
        assert props["campaign_id"] == {"type": "keyword"}
        assert props["date"] == {"type": "date", "format": "yyyy-MM-dd"}


# ---------------------------------------------------------------------------
# get_google_ads_customers (ES-dependent)
# ---------------------------------------------------------------------------


class TestGetGoogleAdsCustomers:

    def test_returns_customer_list(self):
        mock_hits = [{"_id": "cust1", "_source": {"google_ads_customer_title": "Acme"}}]
        with mock.patch(
            "import_scripts.google_ads.get_campaign_data.get_all_documents_paginated",
            return_value=mock_hits,
        ):
            result = gads_get.get_google_ads_customers()

        assert len(result) == 1
        assert result[0]["_source"]["google_ads_customer_title"] == "Acme"

    def test_returns_empty_list_on_exception(self):
        with mock.patch(
            "import_scripts.google_ads.get_campaign_data.get_all_documents_paginated",
            side_effect=Exception("ES down"),
        ):
            result = gads_get.get_google_ads_customers()

        assert result == []

    def test_customer_ids_scope_the_es_query(self):
        # a scoped run queries ES for the requested ids (either
        # mapping shape) instead of post-filtering a capped global fetch.
        with mock.patch(
            "import_scripts.google_ads.get_campaign_data.get_all_documents_paginated",
            return_value=[],
        ) as m_fetch:
            gads_get.get_google_ads_customers(customer_ids=["111-222-3333"])
        query = m_fetch.call_args.args[1]
        shoulds = query["bool"]["should"]
        assert {"terms": {"google_ads_customer_id": ["111-222-3333"]}} in shoulds
        assert {"terms": {"google_ads_customer_id.keyword": ["111-222-3333"]}} in shoulds


# ---------------------------------------------------------------------------
# load_google_ads_credentials — YAML developer-token base
# ---------------------------------------------------------------------------


class TestLoadOauthCredentials:

    def test_raises_when_config_file_missing(self):
        with mock.patch("import_scripts.google_ads.get_campaign_data.os.path.exists", return_value=False):
            with pytest.raises(FileNotFoundError):
                gads_get.load_google_ads_credentials()

    def test_raises_when_developer_token_missing(self):
        incomplete_config = {"login_customer_id": "123"}  # no developer_token anywhere
        with (
            mock.patch("import_scripts.google_ads.get_campaign_data.os.path.exists", return_value=True),
            mock.patch("builtins.open", mock.mock_open(read_data="")),
            mock.patch("import_scripts.google_ads.get_campaign_data.yaml.safe_load", return_value=incomplete_config),
            mock.patch.dict("os.environ", {}, clear=True),
        ):
            with pytest.raises(ValueError, match="Developer token required"):
                gads_get.load_google_ads_credentials()


# ---------------------------------------------------------------------------
# main (google_ads send_json_to_elastic)
# ---------------------------------------------------------------------------


class TestGoogleAdsSendMain:

    def test_main_processes_regular_record(self):
        mock_es = mock.MagicMock()
        data = {
            "team_title": "Alpha",
            "date": "2024-01-01",
            "kibana_date": "2024-01-01",
            "record_type": "daily",
            "google_ads_customer_id": "cust1",
            "campaign_id": "camp1",
            "ad_id": "ad1",
        }

        # index name is now ESID-keyed. team_title "Alpha" sanitizes
        # to "alpha", which the cache resolves to a known team_esid.
        team_cache = {
            "by_esid": {"team-alpha": {"team_title": "Alpha", "org_esids": []}},
            "by_title": {"alpha": "team-alpha"},
        }

        with (
            mock.patch("import_scripts.google_ads.send_json_to_elastic.get_elasticsearch_client", return_value=mock_es),
            mock.patch("import_scripts.google_ads.send_json_to_elastic.build_team_lookup_cache", return_value=team_cache),
            mock.patch("import_scripts.google_ads.send_json_to_elastic.read_json_files", return_value=[data]),
            mock.patch("import_scripts.google_ads.send_json_to_elastic.create_google_ads_index"),
            mock.patch("import_scripts.google_ads.send_json_to_elastic.send_to_elasticsearch") as mock_send,
        ):
            gads_send.main({})

        mock_send.assert_called_once()
        # Index name is the ESID-keyed `{team_esid}__google_ads`.
        index_arg = mock_send.call_args[0][1]
        assert index_arg == "team-alpha__google_ads"

    def test_main_processes_geo_record_type(self):
        mock_es = mock.MagicMock()
        data = {
            "team_title": "Beta",
            "date": "2024-01-01",
            "record_type": "geo",
            "google_ads_customer_id": "cust1",
            "campaign_id": "camp1",
            "geo_criterion_id": "geo1",
        }

        # team_title "Beta" resolves to a known team_esid via the cache.
        team_cache = {
            "by_esid": {"team-beta": {"team_title": "Beta", "org_esids": []}},
            "by_title": {"beta": "team-beta"},
        }

        with (
            mock.patch("import_scripts.google_ads.send_json_to_elastic.get_elasticsearch_client", return_value=mock_es),
            mock.patch("import_scripts.google_ads.send_json_to_elastic.build_team_lookup_cache", return_value=team_cache),
            mock.patch("import_scripts.google_ads.send_json_to_elastic.read_json_files", return_value=[data]),
            mock.patch("import_scripts.google_ads.send_json_to_elastic.create_google_ads_index"),
            mock.patch("import_scripts.google_ads.send_json_to_elastic.send_to_elasticsearch") as mock_send,
        ):
            gads_send.main({})

        mock_send.assert_called_once()
        assert mock_send.call_args[0][1] == "team-beta__google_ads"

    def test_main_processes_campaign_metadata_record_type(self):
        mock_es = mock.MagicMock()
        data = {
            "team_title": "Gamma",
            "date": "2024-01-01",
            "record_type": "campaign_metadata",
            "google_ads_customer_id": "cust1",
            "campaign_id": "camp1",
        }

        # team_title "Gamma" resolves to a known team_esid via the cache.
        team_cache = {
            "by_esid": {"team-gamma": {"team_title": "Gamma", "org_esids": []}},
            "by_title": {"gamma": "team-gamma"},
        }

        with (
            mock.patch("import_scripts.google_ads.send_json_to_elastic.get_elasticsearch_client", return_value=mock_es),
            mock.patch("import_scripts.google_ads.send_json_to_elastic.build_team_lookup_cache", return_value=team_cache),
            mock.patch("import_scripts.google_ads.send_json_to_elastic.read_json_files", return_value=[data]),
            mock.patch("import_scripts.google_ads.send_json_to_elastic.create_google_ads_index"),
            mock.patch("import_scripts.google_ads.send_json_to_elastic.send_to_elasticsearch") as mock_send,
        ):
            gads_send.main({})

        mock_send.assert_called_once()
        assert mock_send.call_args[0][1] == "team-gamma__google_ads"

    def test_main_sets_kibana_date_when_missing(self):
        mock_es = mock.MagicMock()
        data = {
            "team_title": "Delta",
            "date": "2024-06-15",
            "record_type": "daily",
            "google_ads_customer_id": "c1",
            "campaign_id": "c2",
            "ad_id": "a1",
        }

        with (
            mock.patch("import_scripts.google_ads.send_json_to_elastic.get_elasticsearch_client", return_value=mock_es),
            mock.patch("import_scripts.google_ads.send_json_to_elastic.read_json_files", return_value=[data]),
            mock.patch("import_scripts.google_ads.send_json_to_elastic.create_google_ads_index"),
            mock.patch("import_scripts.google_ads.send_json_to_elastic.send_to_elasticsearch"),
        ):
            gads_send.main({})

        assert data["kibana_date"] == "2024-06-15"

    def test_main_handles_empty_directory(self):
        mock_es = mock.MagicMock()

        with (
            mock.patch("import_scripts.google_ads.send_json_to_elastic.get_elasticsearch_client", return_value=mock_es),
            mock.patch("import_scripts.google_ads.send_json_to_elastic.read_json_files", return_value=[]),
        ):
            gads_send.main({})  # Should complete without error

    def test_main_narrows_team_scan_to_google_ads_enabled(self):
        # the team-cache scan must be filtered to Google-Ads-enabled
        # teams, not an all-teams match_all (which fired the cross-workspace
        # 'test'-slug collision warning and scanned every tenant's teams).
        mock_es = mock.MagicMock()
        team_cache = {"by_esid": {}, "by_title": {}}

        with (
            mock.patch("import_scripts.google_ads.send_json_to_elastic.get_elasticsearch_client", return_value=mock_es),
            mock.patch(
                "import_scripts.google_ads.send_json_to_elastic.build_team_lookup_cache",
                return_value=team_cache,
            ) as mock_cache,
            mock.patch("import_scripts.google_ads.send_json_to_elastic.read_json_files", return_value=[]),
        ):
            gads_send.main({})

        mock_cache.assert_called_once()
        assert mock_cache.call_args.kwargs.get("team_filter") == {"ds_google_ads": "true"}


# ###########################################################################
# get_campaign_data.py — additional coverage
# ###########################################################################

_GADS = "import_scripts.google_ads.get_campaign_data"


# ---------------------------------------------------------------------------
# _is_quota_error
# ---------------------------------------------------------------------------


class TestIsQuotaError:

    def _make_exception(self, messages):
        ex = mock.MagicMock()
        errors = []
        for msg in messages:
            err = mock.MagicMock()
            err.message = msg
            errors.append(err)
        ex.failure.errors = errors
        return ex

    def test_detects_quota_keyword(self):
        assert gads_get._is_quota_error(self._make_exception(["Quota exceeded"])) is True

    def test_detects_rate_keyword(self):
        assert gads_get._is_quota_error(self._make_exception(["Rate limit hit"])) is True

    def test_detects_resource_exhausted(self):
        assert gads_get._is_quota_error(self._make_exception(["RESOURCE EXHAUSTED"])) is True

    def test_detects_too_many(self):
        assert gads_get._is_quota_error(self._make_exception(["Too many requests"])) is True

    def test_returns_false_for_other_errors(self):
        assert gads_get._is_quota_error(self._make_exception(["Permission denied"])) is False


# ---------------------------------------------------------------------------
# get_google_ads_customers (extended)
# ---------------------------------------------------------------------------


class TestGetGoogleAdsCustomersExtended:

    def test_returns_empty_on_no_hits(self):
        with mock.patch(f"{_GADS}.get_all_documents_paginated", return_value=[]):
            result = gads_get.get_google_ads_customers()
        assert result == []


# ---------------------------------------------------------------------------
# get_team_by_google_ads_customer_esid
# ---------------------------------------------------------------------------


class TestGetTeamByCustomerEsid:

    def test_returns_team_metadata(self):
        team_doc = {
            "_id": "t1",
            "_source": {
                "team_title": "Alpha",
                "team_url": "alpha.com",
                "team_country": "US",
                "team_city": "NYC",
                "organization_title": "Org1",
            },
        }
        with mock.patch(f"{_GADS}.search_index", return_value={"hits": {"hits": [team_doc]}}):
            result = gads_get.get_team_by_google_ads_customer_esid("cust-1")
        assert result["team_title"] == "Alpha"
        assert result["team_esid"] == "t1"

    def test_returns_none_when_not_found(self):
        with mock.patch(f"{_GADS}.search_index", return_value={"hits": {"hits": []}}):
            result = gads_get.get_team_by_google_ads_customer_esid("cust-1")
        assert result is None

    def test_returns_none_on_exception(self):
        with mock.patch(f"{_GADS}.search_index", side_effect=Exception("ES error")):
            result = gads_get.get_team_by_google_ads_customer_esid("cust-1")
        assert result is None

    def test_multi_team_pick_is_deterministic_by_esid(self):
        # a customer on >1 team must resolve to the same team
        # regardless of ES doc order (lowest team_esid wins).
        hits = [
            {"_id": "zeta", "_source": {"team_title": "Zeta"}},
            {"_id": "alpha", "_source": {"team_title": "Alpha"}},
            {"_id": "mid", "_source": {"team_title": "Mid"}},
        ]
        with mock.patch(f"{_GADS}.search_index", return_value={"hits": {"hits": hits}}):
            result = gads_get.get_team_by_google_ads_customer_esid("cust-multi")
        assert result["team_esid"] == "alpha"

    def test_multi_team_logs_warning_naming_all(self, caplog):
        # a >1-team match must WARN and name every candidate.
        hits = [
            {"_id": "t-a", "_source": {"team_title": "A", "workspace_esid": "w1"}},
            {"_id": "t-b", "_source": {"team_title": "B", "workspace_esid": "w2"}},
        ]
        with mock.patch(f"{_GADS}.search_index", return_value={"hits": {"hits": hits}}):
            with caplog.at_level(logging.WARNING):
                gads_get.get_team_by_google_ads_customer_esid("cust-2")
        msg = " ".join(r.getMessage() for r in caplog.records if r.levelno == logging.WARNING)
        assert "t-a" in msg and "t-b" in msg and "cust-2" in msg

    def test_single_team_does_not_warn(self, caplog):
        hits = [{"_id": "only", "_source": {"team_title": "Only"}}]
        with mock.patch(f"{_GADS}.search_index", return_value={"hits": {"hits": hits}}):
            with caplog.at_level(logging.WARNING):
                gads_get.get_team_by_google_ads_customer_esid("cust-1")
        assert not [r for r in caplog.records if r.levelno == logging.WARNING]


# ---------------------------------------------------------------------------
# load_google_ads_credentials
# ---------------------------------------------------------------------------


class TestLoadGoogleAdsCredentials:
    """the loader returns only the developer-token base (no service
    account, no OAuth identity). The per-customer OAuth identity is layered on
    by resolve_google_ads_oauth_config."""

    def test_returns_developer_token_base_from_yaml(self, monkeypatch):
        # inverted the precedence to env-first, so the env var must be
        # cleared for this to exercise the YAML fallback deterministically.
        monkeypatch.delenv("GOOGLE_ADS_DEVELOPER_TOKEN", raising=False)
        yaml_config = {"developer_token": "tok", "login_customer_id": "123"}
        with (
            mock.patch(f"{_GADS}.os.path.exists", return_value=True),
            mock.patch("builtins.open", mock.mock_open(read_data="")),
            mock.patch(f"{_GADS}.yaml.safe_load", return_value=yaml_config),
        ):
            result = gads_get.load_google_ads_credentials()
        assert result["developer_token"] == "tok"
        assert result["login_customer_id"] == "123"
        assert result["use_proto_plus"] is True
        # The retired service-account identity must NOT be present.
        assert "json_key_file_path" not in result

    def test_login_customer_id_optional(self, monkeypatch):
        monkeypatch.delenv("GOOGLE_ADS_DEVELOPER_TOKEN", raising=False)
        yaml_config = {"developer_token": "tok"}  # no login_customer_id
        with (
            mock.patch(f"{_GADS}.os.path.exists", return_value=True),
            mock.patch("builtins.open", mock.mock_open(read_data="")),
            mock.patch(f"{_GADS}.yaml.safe_load", return_value=yaml_config),
        ):
            result = gads_get.load_google_ads_credentials()
        assert "login_customer_id" not in result

    def test_developer_token_from_env_when_absent_in_yaml(self):
        with (
            mock.patch(f"{_GADS}.os.path.exists", return_value=True),
            mock.patch("builtins.open", mock.mock_open(read_data="")),
            mock.patch(f"{_GADS}.yaml.safe_load", return_value={}),
            mock.patch.dict("os.environ", {"GOOGLE_ADS_DEVELOPER_TOKEN": "env-tok"}),
        ):
            result = gads_get.load_google_ads_credentials()
        assert result["developer_token"] == "env-tok"


# ---------------------------------------------------------------------------
# fetch_video_campaign_performance
# ---------------------------------------------------------------------------


class TestFetchVideoCampaignPerformance:

    def _mock_row(self):
        row = mock.MagicMock()
        row.campaign.id = 123
        row.campaign.name = "App Campaign"
        row.campaign.advertising_channel_type.name = "MULTI_CHANNEL"
        row.campaign.status.name = "ENABLED"
        row.campaign.app_campaign_setting.app_id = "com.example"
        row.campaign.app_campaign_setting.app_store.name = "GOOGLE_APP_STORE"
        row.ad_group.id = 456
        row.ad_group.name = "Ad Group 1"
        row.ad_group.status.name = "ENABLED"
        row.ad_group_ad.ad.id = 789
        row.ad_group_ad.ad.name = "Ad 1"
        row.metrics.impressions = 1000
        row.metrics.clicks = 50
        row.metrics.cost_micros = 5000000
        row.metrics.conversions = 10
        row.metrics.all_conversions = 12
        row.metrics.ctr = 0.05
        row.segments.date = "2024-01-15"
        return row

    def test_returns_campaign_data(self):
        client = mock.MagicMock()
        ga_service = mock.MagicMock()
        client.get_service.return_value = ga_service
        ga_service.search.return_value = [self._mock_row()]

        result = gads_get.fetch_video_campaign_performance(client, "123-456-7890", "2024-01-01", "2024-01-31")
        assert len(result) == 1
        assert result[0]["campaign_name"] == "App Campaign"
        assert result[0]["cost"] == 5.0
        assert result[0]["cost_per_install"] == 0.5

    def test_returns_empty_on_general_exception(self):
        client = mock.MagicMock()
        ga_service = mock.MagicMock()
        client.get_service.return_value = ga_service
        ga_service.search.side_effect = RuntimeError("API error")

        result = gads_get.fetch_video_campaign_performance(client, "123", "2024-01-01", "2024-01-31")
        assert result == []

    def test_handles_zero_conversions(self):
        client = mock.MagicMock()
        ga_service = mock.MagicMock()
        row = self._mock_row()
        row.metrics.conversions = 0
        row.metrics.clicks = 0
        client.get_service.return_value = ga_service
        ga_service.search.return_value = [row]

        result = gads_get.fetch_video_campaign_performance(client, "123", "2024-01-01", "2024-01-31")
        assert result[0]["cost_per_install"] == 0
        assert result[0]["conversion_rate"] == 0


# ---------------------------------------------------------------------------
# fetch_geo_performance
# ---------------------------------------------------------------------------


class TestFetchGeoPerformance:

    def _mock_geo_row(self, criterion_id=2840, conversions=10):
        row = mock.MagicMock()
        row.campaign.id = 123
        row.campaign.name = "Campaign"
        row.geographic_view.country_criterion_id = criterion_id
        row.metrics.conversions = conversions
        row.metrics.impressions = 1000
        row.metrics.cost_micros = 5000000
        row.segments.date = "2024-01-15"
        return row

    def test_returns_geo_data(self):
        client = mock.MagicMock()
        ga_service = mock.MagicMock()
        client.get_service.return_value = ga_service
        ga_service.search.return_value = [self._mock_geo_row()]

        result = gads_get.fetch_geo_performance(client, "123", "2024-01-01", "2024-01-31")
        assert len(result) == 1
        assert result[0]["record_type"] == "geo"

    def test_skips_zero_install_rows(self):
        client = mock.MagicMock()
        ga_service = mock.MagicMock()
        client.get_service.return_value = ga_service
        ga_service.search.return_value = [self._mock_geo_row(conversions=0)]

        result = gads_get.fetch_geo_performance(client, "123", "2024-01-01", "2024-01-31")
        assert result == []

    def test_returns_empty_on_exception(self):
        client = mock.MagicMock()
        ga_service = mock.MagicMock()
        client.get_service.return_value = ga_service
        ga_service.search.side_effect = RuntimeError("fail")

        result = gads_get.fetch_geo_performance(client, "123", "2024-01-01", "2024-01-31")
        assert result == []


# ---------------------------------------------------------------------------
# fetch_campaign_metadata
# ---------------------------------------------------------------------------


class TestFetchCampaignMetadata:

    def test_returns_metadata(self):
        client = mock.MagicMock()
        ga_service = mock.MagicMock()
        client.get_service.return_value = ga_service
        row = mock.MagicMock()
        row.campaign.id = 123
        row.campaign.name = "Campaign"
        row.campaign.status.name = "ENABLED"
        row.campaign.start_date_time = "2024-01-01"
        row.campaign.end_date_time = "2024-12-31"
        row.campaign.advertising_channel_type.name = "MULTI_CHANNEL"
        ga_service.search.return_value = [row]

        result = gads_get.fetch_campaign_metadata(client, "123")
        assert len(result) == 1
        assert result[0]["record_type"] == "campaign_metadata"
        assert result[0]["campaign_status"] == "ENABLED"

    def test_returns_empty_on_exception(self):
        client = mock.MagicMock()
        ga_service = mock.MagicMock()
        client.get_service.return_value = ga_service
        ga_service.search.side_effect = RuntimeError("fail")

        result = gads_get.fetch_campaign_metadata(client, "123")
        assert result == []


# ---------------------------------------------------------------------------
# load_geo_target_countries
# ---------------------------------------------------------------------------


class TestLoadGeoTargetCountries:

    def setup_method(self):
        # Reset module-level cache before each test
        gads_get._GEO_TARGET_COUNTRIES = None

    def test_loads_countries(self):
        client = mock.MagicMock()
        ga_service = mock.MagicMock()
        client.get_service.return_value = ga_service
        row = mock.MagicMock()
        row.geo_target_constant.id = 2840
        row.geo_target_constant.name = "United States"
        row.geo_target_constant.country_code = "US"
        row.geo_target_constant.canonical_name = "United States"
        ga_service.search.return_value = [row]

        result = gads_get.load_geo_target_countries(client, "123")
        assert 2840 in result
        assert result[2840]["country_code"] == "US"

    def test_returns_cached_result(self):
        gads_get._GEO_TARGET_COUNTRIES = {1: {"name": "Cached"}}
        client = mock.MagicMock()
        result = gads_get.load_geo_target_countries(client, "123")
        assert result == {1: {"name": "Cached"}}
        client.get_service.assert_not_called()

    def test_returns_empty_on_exception(self):
        client = mock.MagicMock()
        ga_service = mock.MagicMock()
        client.get_service.return_value = ga_service
        ga_service.search.side_effect = Exception("fail")

        result = gads_get.load_geo_target_countries(client, "123")
        assert result == {}


# ---------------------------------------------------------------------------
# save functions
# ---------------------------------------------------------------------------


class TestSaveCampaignData:

    def test_saves_json_file(self):
        m_open = mock.mock_open()
        with (
            mock.patch(f"{_GADS}.os.makedirs"),
            mock.patch(f"{_GADS}.os.path.exists", return_value=False),
            mock.patch("builtins.open", m_open),
        ):
            gads_get.save_campaign_data({"campaign_id": "c1", "ad_id": "a1"}, "cust-1", "team-1", "2024-01-15")
        m_open.assert_called_once()

    def test_skips_already_imported(self):
        # archived AND indexed -> true skip (no write).
        with (
            mock.patch(f"{_GADS}.os.makedirs"),
            mock.patch(f"{_GADS}.os.path.exists", return_value=True),
            mock.patch(f"{_GADS}.indexed_doc_present", return_value=True),
            mock.patch("builtins.open") as m_open,
        ):
            gads_get.save_campaign_data({"campaign_id": "c1", "ad_id": "a1"}, "cust-1", "team-1", "2024-01-15")
        m_open.assert_not_called()

    def test_rewrites_when_archived_but_not_indexed(self):
        # archived but ES doc MISSING -> re-write for re-send
        # (the old archive-alone skip caused silent permanent loss).
        m_open = mock.mock_open()
        with (
            mock.patch(f"{_GADS}.os.makedirs"),
            mock.patch(f"{_GADS}.os.path.exists", return_value=True),
            mock.patch(f"{_GADS}.indexed_doc_present", return_value=False),
            mock.patch("builtins.open", m_open),
        ):
            gads_get.save_campaign_data({"campaign_id": "c1", "ad_id": "a1"}, "cust-1", "team-1", "2024-01-15")
        m_open.assert_called_once()


class TestSaveGeoData:

    def test_saves_json_file(self):
        m_open = mock.mock_open()
        with (
            mock.patch(f"{_GADS}.os.makedirs"),
            mock.patch(f"{_GADS}.os.path.exists", return_value=False),
            mock.patch("builtins.open", m_open),
        ):
            gads_get.save_geo_data({"campaign_id": "c1", "geo_criterion_id": "g1"}, "cust-1", "team-1", "2024-01-15")
        m_open.assert_called_once()

    def test_skips_already_imported(self):
        # archived AND indexed -> true skip (no write).
        with (
            mock.patch(f"{_GADS}.os.makedirs"),
            mock.patch(f"{_GADS}.os.path.exists", return_value=True),
            mock.patch(f"{_GADS}.indexed_doc_present", return_value=True),
            mock.patch("builtins.open") as m_open,
        ):
            gads_get.save_geo_data({"campaign_id": "c1", "geo_criterion_id": "g1"}, "cust-1", "team-1", "2024-01-15")
        m_open.assert_not_called()

    def test_rewrites_when_archived_but_not_indexed(self):
        # archived but ES geo doc MISSING -> re-write for re-send.
        m_open = mock.mock_open()
        with (
            mock.patch(f"{_GADS}.os.makedirs"),
            mock.patch(f"{_GADS}.os.path.exists", return_value=True),
            mock.patch(f"{_GADS}.indexed_doc_present", return_value=False),
            mock.patch("builtins.open", m_open),
        ):
            gads_get.save_geo_data({"campaign_id": "c1", "geo_criterion_id": "g1"}, "cust-1", "team-1", "2024-01-15")
        m_open.assert_called_once()


class TestSaveCampaignMetadata:

    def test_saves_metadata_json(self):
        m_open = mock.mock_open()
        with mock.patch(f"{_GADS}.os.makedirs"), mock.patch("builtins.open", m_open):
            gads_get.save_campaign_metadata({"campaign_id": "c1"}, "cust-1", "team-1")
        m_open.assert_called_once()


# ---------------------------------------------------------------------------
# main (get_campaign_data)
# ---------------------------------------------------------------------------


class TestGetCampaignDataMain:

    def _mock_customer(self, cid="1234567890"):
        return {
            "_id": "cust-esid-1",
            "_source": {
                "google_ads_customer_id": cid,
                "google_ads_customer_title": "Test Customer",
            },
        }

    def _mock_team(self):
        return {
            "team_esid": "t1",
            "team_title": "Alpha",
            "team_url": "alpha.com",
            "team_country": "US",
            "team_city": "NYC",
            "organization_title": "Org1",
        }

    # OAuth-only: a customer is imported only when
    # resolve_google_ads_oauth_config returns a usable config. The default
    # patches below return an OAuth config so the processing tests exercise the
    # import path; the skip tests exercise the no-OAuth branch explicitly.
    _OAUTH_CFG = {"developer_token": "tok", "refresh_token": "r", "client_id": "c", "client_secret": "s"}

    def test_raises_on_credential_error(self):
        """a credentials failure must propagate, not exit rc 0.

        Previously named test_returns_early_on_credential_error and asserted the
        swallow. That made a missing developer token look like a SUCCESSFUL import
        of zero rows to the Report Scheduler.
        """
        with mock.patch(f"{_GADS}.load_google_ads_credentials", side_effect=FileNotFoundError("no config")):
            with pytest.raises(FileNotFoundError):
                gads_get.main({"start_date": "2024-01-01", "end_date": "2024-01-31", "customer_ids": "111"})

    def test_raises_when_no_customers(self):
        """a scoped run resolving zero customers must FAIL, not return.

        Previously named test_returns_early_when_no_customers. Both halves now
        raise: an absent --customer_ids (require_scope) and a provided one that
        resolves to nothing (scope_resolution_error).
        """
        with (
            mock.patch(f"{_GADS}.load_google_ads_credentials", return_value={}),
            mock.patch(f"{_GADS}.get_google_ads_customers", return_value=[]),
        ):
            # Scope provided, resolves to zero.
            with pytest.raises(ValueError, match="customer_ids"):
                gads_get.main({"start_date": "2024-01-01", "end_date": "2024-01-31", "customer_ids": "111"})
            # Scope absent entirely.
            with pytest.raises(ValueError, match="customer_ids"):
                gads_get.main({"start_date": "2024-01-01", "end_date": "2024-01-31"})

    def test_skips_customer_without_id(self):
        bad_customer = {"_id": "cust-1", "_source": {"google_ads_customer_id": None}}
        with (
            mock.patch(f"{_GADS}.load_google_ads_credentials", return_value={}),
            mock.patch(f"{_GADS}.get_google_ads_customers", return_value=[bad_customer]),
            mock.patch(f"{_GADS}.get_linked_teams") as mock_team,
        ):
            # a customer with no id cannot match the requested scope, so
            # the exact-match safety net empties the list and the run now raises
            # instead of silently importing nothing. The point of this test is
            # unchanged: the team lookup must not be reached.
            with pytest.raises(ValueError, match="customer_ids"):
                gads_get.main({"start_date": "2024-01-01", "end_date": "2024-01-31", "customer_ids": "1234567890"})
        mock_team.assert_not_called()

    def test_skips_customer_without_team(self):
        with (
            mock.patch(f"{_GADS}.load_google_ads_credentials", return_value={}),
            mock.patch(f"{_GADS}.get_google_ads_customers", return_value=[self._mock_customer()]),
            mock.patch(f"{_GADS}.get_linked_teams", return_value=[]),
            mock.patch(f"{_GADS}.fetch_video_campaign_performance") as mock_fetch,
        ):
            gads_get.main({"start_date": "2024-01-01", "end_date": "2024-01-31", "customer_ids": "1234567890"})
        mock_fetch.assert_not_called()

    def test_skips_customer_without_oauth_connection(self):
        """a customer with no OAuth connection is skipped (no SA to
        fall back to) and is NOT fetched."""
        with (
            mock.patch(f"{_GADS}.load_google_ads_credentials", return_value={}),
            mock.patch(f"{_GADS}.get_google_ads_customers", return_value=[self._mock_customer()]),
            mock.patch(f"{_GADS}.get_linked_teams", return_value=[("t1", self._mock_team())]),
            mock.patch(f"{_GADS}.resolve_google_ads_oauth_config", return_value=None),
            mock.patch(f"{_GADS}.fetch_video_campaign_performance") as mock_fetch,
        ):
            gads_get.main({"start_date": "2024-01-01", "end_date": "2024-01-31", "customer_ids": "1234567890"})
        mock_fetch.assert_not_called()

    def test_processes_customer_with_campaigns(self):
        campaign = {"campaign_id": "c1", "ad_id": "a1", "date": "2024-01-15"}
        with (
            mock.patch(f"{_GADS}.load_google_ads_credentials", return_value={}),
            mock.patch(f"{_GADS}.GoogleAdsClient.load_from_dict"),
            mock.patch(f"{_GADS}.resolve_google_ads_oauth_config", return_value=self._OAUTH_CFG),
            mock.patch(f"{_GADS}.get_google_ads_customers", return_value=[self._mock_customer()]),
            mock.patch(f"{_GADS}.get_linked_teams", return_value=[("t1", self._mock_team())]),
            mock.patch(f"{_GADS}.fetch_video_campaign_performance", return_value=[campaign]),
            mock.patch(f"{_GADS}.save_campaign_data") as mock_save,
            mock.patch(f"{_GADS}.fetch_geo_performance", return_value=[]),
            mock.patch(f"{_GADS}.fetch_campaign_metadata", return_value=[]),
            mock.patch(f"{_GADS}.write_no_data_markers"),
            mock.patch("import_scripts.facebook.run_daily_imports.generate_date_range", return_value=["2024-01-15"]),
        ):
            gads_get.main({"start_date": "2024-01-15", "end_date": "2024-01-15", "customer_ids": "1234567890"})
        mock_save.assert_called_once()

    def test_defaults_dates_when_not_provided(self):
        """Omitted dates default without error before the scope guard is reached.

        the empty-customer fixture now raises (scope provided, resolves
        to nothing), so assert the date defaulting got past its own validation
        rather than asserting the whole run completes.
        """
        with (
            mock.patch(f"{_GADS}.load_google_ads_credentials", return_value={}),
            mock.patch(f"{_GADS}.get_google_ads_customers", return_value=[]) as mock_fetch,
        ):
            with pytest.raises(ValueError, match="customer_ids"):
                gads_get.main({"customer_ids": "1234567890"})
        # Reaching the ES fetch proves the default start/end dates were computed
        # without raising a date-validation error.
        mock_fetch.assert_called_once()

    def test_filters_by_customer_ids(self):
        c1 = self._mock_customer("111")
        c2 = self._mock_customer("222")
        with (
            mock.patch(f"{_GADS}.load_google_ads_credentials", return_value={}),
            mock.patch(f"{_GADS}.get_google_ads_customers", return_value=[c1, c2]),
            mock.patch(f"{_GADS}.get_linked_teams", return_value=[]),
        ):
            gads_get.main({"start_date": "2024-01-01", "end_date": "2024-01-31", "customer_ids": "111"})

    def test_processes_geo_records_with_country_info(self):
        campaign = {"campaign_id": "c1", "ad_id": "a1", "date": "2024-01-15"}
        geo_rec = {
            "record_type": "geo",
            "campaign_id": "c1",
            "geo_criterion_id": 2840,
            "app_installs": 10,
            "date": "2024-01-15",
        }
        geo_countries = {2840: {"name": "United States", "country_code": "US"}}

        with (
            mock.patch(f"{_GADS}.load_google_ads_credentials", return_value={}),
            mock.patch(f"{_GADS}.GoogleAdsClient.load_from_dict"),
            mock.patch(f"{_GADS}.resolve_google_ads_oauth_config", return_value=self._OAUTH_CFG),
            mock.patch(f"{_GADS}.get_google_ads_customers", return_value=[self._mock_customer()]),
            mock.patch(f"{_GADS}.get_linked_teams", return_value=[("t1", self._mock_team())]),
            mock.patch(f"{_GADS}.fetch_video_campaign_performance", return_value=[campaign]),
            mock.patch(f"{_GADS}.save_campaign_data"),
            mock.patch(f"{_GADS}.fetch_geo_performance", return_value=[geo_rec]),
            mock.patch(f"{_GADS}.load_geo_target_countries", return_value=geo_countries),
            mock.patch(f"{_GADS}.save_geo_data") as mock_geo_save,
            mock.patch(f"{_GADS}.fetch_campaign_metadata", return_value=[]),
            mock.patch(f"{_GADS}.write_no_data_markers"),
            mock.patch("import_scripts.facebook.run_daily_imports.generate_date_range", return_value=["2024-01-15"]),
        ):
            gads_get.main({"start_date": "2024-01-15", "end_date": "2024-01-15", "customer_ids": "1234567890"})
        mock_geo_save.assert_called_once()

    def test_processes_campaign_metadata(self):
        meta = {"record_type": "campaign_metadata", "campaign_id": "c1"}
        with (
            mock.patch(f"{_GADS}.load_google_ads_credentials", return_value={}),
            mock.patch(f"{_GADS}.GoogleAdsClient.load_from_dict"),
            mock.patch(f"{_GADS}.resolve_google_ads_oauth_config", return_value=self._OAUTH_CFG),
            mock.patch(f"{_GADS}.get_google_ads_customers", return_value=[self._mock_customer()]),
            mock.patch(f"{_GADS}.get_linked_teams", return_value=[("t1", self._mock_team())]),
            mock.patch(f"{_GADS}.fetch_video_campaign_performance", return_value=[]),
            mock.patch(f"{_GADS}.fetch_geo_performance", return_value=[]),
            mock.patch(f"{_GADS}.fetch_campaign_metadata", return_value=[meta]),
            mock.patch(f"{_GADS}.save_campaign_metadata") as mock_meta_save,
            mock.patch(f"{_GADS}.write_no_data_markers"),
            mock.patch("import_scripts.facebook.run_daily_imports.generate_date_range", return_value=["2024-01-15"]),
        ):
            gads_get.main({"start_date": "2024-01-15", "end_date": "2024-01-15", "customer_ids": "1234567890"})
        mock_meta_save.assert_called_once()


# ---------------------------------------------------------------------------
# fetch_video_campaign_performance — quota retry path
# ---------------------------------------------------------------------------


class TestFetchVideoCampaignQuotaRetry:

    def _mock_row(self):
        row = mock.MagicMock()
        row.campaign.id = 123
        row.campaign.name = "Campaign"
        row.campaign.advertising_channel_type.name = "MULTI_CHANNEL"
        row.campaign.status.name = "ENABLED"
        row.campaign.app_campaign_setting.app_id = "com.example"
        row.campaign.app_campaign_setting.app_store.name = "GOOGLE_APP_STORE"
        row.ad_group.id = 456
        row.ad_group.name = "Ad Group"
        row.ad_group.status.name = "ENABLED"
        row.ad_group_ad.ad.id = 789
        row.ad_group_ad.ad.name = "Ad"
        row.metrics.impressions = 100
        row.metrics.clicks = 10
        row.metrics.cost_micros = 1000000
        row.metrics.conversions = 5
        row.metrics.all_conversions = 5
        row.metrics.ctr = 0.1
        row.segments.date = "2024-01-15"
        return row

    def test_retries_on_quota_error_and_succeeds(self):
        from tests.pipelines.conftest import _GoogleAdsException

        quota_ex = _GoogleAdsException("quota")
        quota_ex.failure.errors = [mock.MagicMock(message="Quota exceeded")]

        client = mock.MagicMock()
        ga_service = mock.MagicMock()
        client.get_service.return_value = ga_service
        # First call raises quota, retry succeeds
        ga_service.search.side_effect = [quota_ex, [self._mock_row()]]

        with mock.patch(f"{_GADS}.time.sleep"):
            result = gads_get.fetch_video_campaign_performance(client, "123", "2024-01-01", "2024-01-31")
        assert len(result) == 1

    def test_non_quota_google_ads_error_logs_and_returns_empty(self):
        from tests.pipelines.conftest import _GoogleAdsException

        ex = _GoogleAdsException("permission")
        err = mock.MagicMock()
        err.message = "Permission denied"
        err.location = None
        ex.failure.errors = [err]

        client = mock.MagicMock()
        ga_service = mock.MagicMock()
        client.get_service.return_value = ga_service
        ga_service.search.side_effect = ex

        result = gads_get.fetch_video_campaign_performance(client, "123", "2024-01-01", "2024-01-31")
        assert result == []
