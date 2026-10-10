"""
Asset Service - wrapper around lib/es_helpers for web interface.
"""

import logging
import sys
import os

# Add parent directories to path for imports
sys.path.insert(
    0,
    os.path.dirname(os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))),
)

from elasticsearch.exceptions import NotFoundError
from lib.es_helpers import (
    add_document,
    get_all_documents,
    get_document_by_id,
    delete_document_by_id,
    edit_document,
    get_elasticsearch_client,
)


class AssetService:
    """Service class for asset CRUD operations."""

    @staticmethod
    def get_all(index_name, query=None, size=1000):
        """Get all documents from an index."""
        return get_all_documents(index_name, query, size)

    @staticmethod
    def get_by_id(index_name, doc_id):
        """Get a single document by ID."""
        return get_document_by_id(index_name, doc_id)

    @staticmethod
    def create(index_name, document):
        """Create a new document."""
        return add_document(index_name, document)

    @staticmethod
    def create_with_error(index_name, document, doc_id=None):
        """Create a new document, returning (success, error_message, item_id) tuple.

        When ``doc_id`` is provided the write goes through ``op_type=create``
        so a duplicate id surfaces as a 409 (mapped to a user-visible
        error) rather than silently overwriting an existing doc.
        relies on this for the workspace branch — workspace docs MUST have
        ``_id == workspace_slug`` (the documented natural-key convention
        in ``permissions_index_service``), and the WorkspaceSlugService
        availability check is fail-soft, so the create itself is the
        ultimate dedup gate.
        """
        from datetime import datetime

        es = get_elasticsearch_client()
        if es is None:
            return (
                False,
                "Unable to connect to Elasticsearch. Please check that the database is running.",
                None,
            )

        try:
            # Add @timestamp field if not present (required for data streams)
            if "@timestamp" not in document:
                document["@timestamp"] = datetime.utcnow().isoformat() + "Z"

            # ensure the title field has its keyword subfield
            # mapped before the very first write to a de_asset_* index.
            # No-op for indices that already exist OR for indices that
            # aren't in the asset-title mapping registry.
            try:
                from asset_management.web.services.permissions_index_service import (
                    PermissionsIndexService,
                )

                PermissionsIndexService.ensure_asset_title_mapping(es, index_name)
            except Exception as e:
                # Mapping pre-flight is belt-and-suspenders; the bootstrap
                # path covers the post-restart case. Log and proceed.
                logging.warning("mapping pre-flight skipped: %s", e)

            index_kwargs = {
                "index": index_name,
                "document": document,
                "refresh": "wait_for",
            }
            if doc_id is not None:
                index_kwargs["id"] = doc_id
                index_kwargs["op_type"] = "create"

            response = es.index(**index_kwargs)
            return True, None, response["_id"]
        except Exception as e:
            err_text = str(e).lower()
            if "version_conflict" in err_text or "conflict" in err_text:
                return False, f"A record with id {doc_id!r} already exists.", None
            # callers return this message to the browser; ES error
            # text (index names, hosts, query detail) stays in the log.
            logging.warning("AssetService.create_with_error: index=%s failed: %s", index_name, e)
            return False, "Database error: could not save the record.", None

    @staticmethod
    def update(index_name, doc_id, document):
        """Update an existing document."""
        return edit_document(index_name, doc_id, document)

    @staticmethod
    def delete(index_name, doc_id):
        """Delete a document and clean up ESID references in child assets."""
        success = delete_document_by_id(index_name, doc_id)
        if success:
            try:
                from asset_management.web.services.reference_cleanup_service import (
                    ReferenceCleanupService,
                )

                result = ReferenceCleanupService.cleanup_references(index_name, doc_id)
                if result:
                    logging.info(f"Reference cleanup for {index_name}/{doc_id}: {result}")
            except Exception as e:
                logging.error(f"Reference cleanup failed for {index_name}/{doc_id}: {e}")
        return success

    @staticmethod
    def delete_only(index_name, doc_id):
        """Delete a document without reference cleanup (for bulk operations)."""
        return delete_document_by_id(index_name, doc_id)

    @staticmethod
    def check_unique_value(index_name, field_name, value):
        """Check if a value is unique in an index."""
        es = get_elasticsearch_client()
        if es is None:
            return False

        # Try without .keyword first (for keyword type fields)
        query = {"term": {field_name: value}}
        try:
            result = es.search(index=index_name, body={"query": query})
            count = result["hits"]["total"]["value"]

            # If we got results, value is not unique
            if count > 0:
                return False

            # If no results, also try with .keyword suffix (for text type fields)
            # This handles cases where the field is mapped as text with keyword sub-field
            query_with_keyword = {"term": {f"{field_name}.keyword": value}}
            result = es.search(index=index_name, body={"query": query_with_keyword})
            return result["hits"]["total"]["value"] == 0
        except NotFoundError:
            # Index doesn't exist yet — no documents means any value is unique
            return True
        except Exception:
            return False

    @staticmethod
    def count(index_name, query=None):
        """Get count of documents in an index."""
        es = get_elasticsearch_client()
        if es is None:
            return 0

        try:
            body = {}
            if query:
                body["query"] = query
            result = es.count(index=index_name, body=body)
            return result["count"]
        except Exception:
            return 0

    @staticmethod
    def get_elasticsearch_client():
        """Get raw Elasticsearch client for advanced operations."""
        return get_elasticsearch_client()
