"""
Shared fixtures for service-layer tests.
"""

from unittest import mock

import pytest


@pytest.fixture
def mock_es():
    """Yield a MagicMock Elasticsearch client, pre-patched into es_helpers."""
    with mock.patch("lib.es_helpers.get_elasticsearch_client") as factory:
        client = mock.MagicMock()
        factory.return_value = client
        yield client


@pytest.fixture
def mock_es_none():
    """Yield None from get_elasticsearch_client to simulate ES being unavailable."""
    with mock.patch("lib.es_helpers.get_elasticsearch_client", return_value=None):
        yield
