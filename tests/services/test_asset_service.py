"""
Tests for the Asset Service.

Covers:
  get_all, get_by_id, create, update, delete — wrapper methods
  create_with_error    — success, ES unavailable, ES error, timestamp injection
  check_unique_value   — unique, not unique, keyword fallback, index not found, ES unavailable
  count                — success, with query, ES unavailable, exception
  get_elasticsearch_client — returns client
"""

from unittest import mock

import pytest

from asset_management.web.services.asset_service import AssetService


class TestWrapperMethods:

    def test_get_all(self):
        with mock.patch("asset_management.web.services.asset_service.get_all_documents", return_value=[]) as m:
            result = AssetService.get_all("test_index", {"match_all": {}}, size=50)
        m.assert_called_once_with("test_index", {"match_all": {}}, 50)
        assert result == []

    def test_get_by_id(self):
        with mock.patch("asset_management.web.services.asset_service.get_document_by_id", return_value={"title": "x"}) as m:
            result = AssetService.get_by_id("test_index", "abc123")
        m.assert_called_once_with("test_index", "abc123")

    def test_create(self):
        with mock.patch("asset_management.web.services.asset_service.add_document", return_value="new_id") as m:
            result = AssetService.create("test_index", {"title": "x"})
        m.assert_called_once()

    def test_update(self):
        with mock.patch("asset_management.web.services.asset_service.edit_document") as m:
            AssetService.update("test_index", "abc", {"title": "y"})
        m.assert_called_once_with("test_index", "abc", {"title": "y"})

    def test_delete(self):
        with mock.patch("asset_management.web.services.asset_service.delete_document_by_id") as m:
            AssetService.delete("test_index", "abc")
        m.assert_called_once_with("test_index", "abc")


class TestCreateWithError:

    def test_success(self):
        mock_es = mock.MagicMock()
        mock_es.index.return_value = {"_id": "new_id"}
        with mock.patch("asset_management.web.services.asset_service.get_elasticsearch_client", return_value=mock_es):
            success, error, item_id = AssetService.create_with_error("test_index", {"title": "x"})
        assert success is True
        assert error is None
        assert item_id == "new_id"

    def test_adds_timestamp_if_missing(self):
        mock_es = mock.MagicMock()
        mock_es.index.return_value = {"_id": "id"}
        doc = {"title": "x"}
        with mock.patch("asset_management.web.services.asset_service.get_elasticsearch_client", return_value=mock_es):
            AssetService.create_with_error("test_index", doc)
        indexed_doc = mock_es.index.call_args[1]["document"]
        assert "@timestamp" in indexed_doc

    def test_preserves_existing_timestamp(self):
        mock_es = mock.MagicMock()
        mock_es.index.return_value = {"_id": "id"}
        doc = {"title": "x", "@timestamp": "2024-01-01T00:00:00Z"}
        with mock.patch("asset_management.web.services.asset_service.get_elasticsearch_client", return_value=mock_es):
            AssetService.create_with_error("test_index", doc)
        indexed_doc = mock_es.index.call_args[1]["document"]
        assert indexed_doc["@timestamp"] == "2024-01-01T00:00:00Z"

    def test_es_unavailable(self):
        with mock.patch("asset_management.web.services.asset_service.get_elasticsearch_client", return_value=None):
            success, error, item_id = AssetService.create_with_error("test_index", {"title": "x"})
        assert success is False
        assert "Elasticsearch" in error
        assert item_id is None

    def test_es_exception(self):
        mock_es = mock.MagicMock()
        mock_es.index.side_effect = Exception("mapping error")
        with mock.patch("asset_management.web.services.asset_service.get_elasticsearch_client", return_value=mock_es):
            success, error, item_id = AssetService.create_with_error("test_index", {"title": "x"})
        assert success is False
        # the ES text stays in the log, never in the returned message.
        assert error == "Database error: could not save the record."
        assert "mapping error" not in error


class TestCheckUniqueValue:

    def test_unique_value(self):
        mock_es = mock.MagicMock()
        mock_es.search.return_value = {"hits": {"total": {"value": 0}}}
        with mock.patch("asset_management.web.services.asset_service.get_elasticsearch_client", return_value=mock_es):
            result = AssetService.check_unique_value("test_index", "title", "unique_val")
        assert result is True

    def test_not_unique(self):
        mock_es = mock.MagicMock()
        mock_es.search.return_value = {"hits": {"total": {"value": 1}}}
        with mock.patch("asset_management.web.services.asset_service.get_elasticsearch_client", return_value=mock_es):
            result = AssetService.check_unique_value("test_index", "title", "duplicate")
        assert result is False

    def test_keyword_fallback(self):
        mock_es = mock.MagicMock()
        # First call (term) returns 0, second call (.keyword) also returns 0
        mock_es.search.side_effect = [
            {"hits": {"total": {"value": 0}}},
            {"hits": {"total": {"value": 0}}},
        ]
        with mock.patch("asset_management.web.services.asset_service.get_elasticsearch_client", return_value=mock_es):
            result = AssetService.check_unique_value("test_index", "title", "val")
        assert result is True
        assert mock_es.search.call_count == 2

    def test_index_not_found(self):
        from elasticsearch.exceptions import NotFoundError
        mock_es = mock.MagicMock()
        mock_meta = mock.MagicMock()
        mock_meta.status = 404
        mock_es.search.side_effect = NotFoundError("index_not_found_exception", mock_meta, "not found")
        with mock.patch("asset_management.web.services.asset_service.get_elasticsearch_client", return_value=mock_es):
            result = AssetService.check_unique_value("test_index", "title", "val")
        assert result is True

    def test_es_unavailable(self):
        with mock.patch("asset_management.web.services.asset_service.get_elasticsearch_client", return_value=None):
            result = AssetService.check_unique_value("test_index", "title", "val")
        assert result is False

    def test_generic_exception(self):
        mock_es = mock.MagicMock()
        mock_es.search.side_effect = Exception("error")
        with mock.patch("asset_management.web.services.asset_service.get_elasticsearch_client", return_value=mock_es):
            result = AssetService.check_unique_value("test_index", "title", "val")
        assert result is False


class TestCount:

    def test_count_no_query(self):
        mock_es = mock.MagicMock()
        mock_es.count.return_value = {"count": 42}
        with mock.patch("asset_management.web.services.asset_service.get_elasticsearch_client", return_value=mock_es):
            result = AssetService.count("test_index")
        assert result == 42

    def test_count_with_query(self):
        mock_es = mock.MagicMock()
        mock_es.count.return_value = {"count": 5}
        with mock.patch("asset_management.web.services.asset_service.get_elasticsearch_client", return_value=mock_es):
            result = AssetService.count("test_index", {"term": {"status": "active"}})
        body = mock_es.count.call_args[1]["body"]
        assert "query" in body

    def test_es_unavailable(self):
        with mock.patch("asset_management.web.services.asset_service.get_elasticsearch_client", return_value=None):
            result = AssetService.count("test_index")
        assert result == 0

    def test_exception(self):
        mock_es = mock.MagicMock()
        mock_es.count.side_effect = Exception("error")
        with mock.patch("asset_management.web.services.asset_service.get_elasticsearch_client", return_value=mock_es):
            result = AssetService.count("test_index")
        assert result == 0


class TestGetElasticsearchClient:

    def test_returns_client(self):
        mock_es = mock.MagicMock()
        with mock.patch("asset_management.web.services.asset_service.get_elasticsearch_client", return_value=mock_es):
            result = AssetService.get_elasticsearch_client()
        assert result is mock_es
