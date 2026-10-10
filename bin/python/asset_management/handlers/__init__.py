"""Shared utilities for CLI asset handlers."""

import logging

from lib.es_helpers import delete_document_by_id


def delete_asset_with_cleanup(index_name, doc_id):
    """Delete a document and clean up ESID references in child assets.

    Wraps delete_document_by_id with automatic reference cleanup so that
    CLI handlers get the same cascade behaviour as the web API.
    """
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
