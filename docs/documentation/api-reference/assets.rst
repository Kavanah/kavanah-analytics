.. _api-assets:

Asset Endpoints
===============

These endpoints manage assets (organisations, teams, Facebook apps, Google Ads
customers, etc.) and provide supporting data for asset-related UI components.

All asset endpoints require the ``asset_admin`` or ``superuser`` role.

Valid values for ``<asset_type>``:

.. code-block:: text

   organizations, teams, regions, countries, cities,
   facebook_accounts, facebook_apps, facebook_ad_accounts,
   facebook_app_users, facebook_pages, echo_tenants,
   ga_accounts, ga_properties, google_ads_customers,
   instagram_accounts, youtube_channels, dashboards

----

Asset CRUD
----------

.. http:get:: /asset/(asset_type)

   Returns the asset list page for the given type (HTML).

   :param asset_type: One of the valid asset type values.
   :statuscode 200: Asset list page rendered.

----

.. http:get:: /asset/(asset_type)/add

   Returns the add-new-asset form page (HTML).

   :statuscode 200: Add form rendered.

----

.. http:post:: /asset/(asset_type)/add

   Creates a new asset record in Elasticsearch.

   :form \*: All required fields for the asset type. Field names and requirements
     vary by asset type (see the Asset Explorer for the field list for each type).
   :form csrf_token: CSRF token.

   :statuscode 302: Redirect to the asset list on success.
   :statuscode 400: Validation error (missing required field, or unique field conflict).

----

.. http:get:: /asset/(asset_type)/edit/(item_id)

   Returns the edit form for an existing asset (HTML).

   :param item_id: Elasticsearch document ID of the asset.
   :statuscode 200: Edit form rendered.
   :statuscode 404: Asset not found.

----

.. http:post:: /asset/(asset_type)/edit/(item_id)

   Updates an existing asset record.

   :param item_id: Elasticsearch document ID of the asset.
   :form \*: Fields to update.
   :form csrf_token: CSRF token.

   :statuscode 302: Redirect to asset list on success.
   :statuscode 400: Validation error.
   :statuscode 404: Asset not found.

----

.. http:post:: /asset/(asset_type)/delete/(item_id)

   Deletes a single asset record.

   :param item_id: Elasticsearch document ID.
   :form csrf_token: CSRF token.

   :statuscode 302: Redirect to asset list on success.
   :statuscode 404: Asset not found.

----

.. http:post:: /asset/(asset_type)/bulk-delete

   Deletes multiple asset records.

   :form ids: Comma-separated list of Elasticsearch document IDs.
   :form csrf_token: CSRF token.

   :statuscode 200: Returns ``{"success": true, "deleted": N}``.
   :statuscode 400: No IDs provided.

----

.. http:get:: /asset/(asset_type)/view/(item_id)

   Returns the asset detail view page (HTML).

   :param item_id: Elasticsearch document ID.
   :statuscode 200: Detail page rendered.
   :statuscode 404: Asset not found.

----

.. http:get:: /asset/(asset_type)/check-references/(item_id)

   Reports whether the asset is referenced by any other asset (e.g. a
   team referenced by Facebook ad accounts), so the UI can warn before
   a delete that would orphan dependents.

   :statuscode 200:

   .. code-block:: json

      {
        "has_references": true,
        "references": [
          { "type": "fb_ad_accounts", "count": 3 }
        ]
      }

----

.. http:get:: /api/asset/(asset_type)/(item_id)/details

   Returns JSON details for an asset, used to populate the sidebar detail panel.

   :param asset_type: Asset type.
   :param item_id: Elasticsearch document ID.

   :statuscode 200: Returns the asset document as JSON, with encrypted fields masked.
   :statuscode 404: Asset not found.

----

Asset Import (CSV)
------------------

.. http:post:: /api/assets/import

   Imports assets from a CSV file upload.

   **Content-Type:** ``multipart/form-data``

   :form asset_type: The asset type to import (e.g. ``organizations``).
   :form file: The CSV file. Must be UTF-8 or UTF-8 BOM encoded. Maximum 1,000 rows.

   :statuscode 200: Returns ``{"success": true, "imported": N, "skipped": M, "errors": [...]}``.
   :statuscode 400: Invalid asset type, missing file, or CSV parse error.

   **CSV format notes:**

   - First row must be a header row with field names
   - Field names must match the asset type's field definitions exactly
   - Encrypted fields can be included in plaintext in the CSV — they are encrypted before storage
   - Rows with unique-field conflicts will be skipped (existing record is not overwritten)

----

Options and Tree
----------------

.. http:get:: /api/options/(index_name)

   Returns all values of a specific field from an index, for populating
   select dropdowns in asset forms.

   :param index_name: Elasticsearch index name (e.g. ``de_asset_organization``).
   :query field: The field name to return values for (default: ``title``).

   :statuscode 200: Returns ``{"options": [{"value": "...", "label": "..."}, ...]}``.

----

.. http:get:: /api/options/(index_name)/filtered

   Returns field values filtered by a related field value, for dependent dropdowns
   (e.g. teams filtered by organisation).

   :param index_name: Elasticsearch index name.
   :query field: The field to return values for.
   :query filter_field: The field to filter by.
   :query filter_value: The value to match on ``filter_field``.

   :statuscode 200: Filtered options list.

----

.. http:get:: /api/tree/structure

   Returns the full asset tree structure with document counts per type, used
   to render the left navigation tree in the Asset Explorer.

   :statuscode 200: Returns nested JSON tree with counts:

   .. code-block:: json

      {
        "categories": [
          {
            "label": "Company",
            "types": [
              { "type": "organizations", "label": "Organisations", "count": 5 },
              { "type": "teams", "label": "Teams", "count": 12 }
            ]
          }
        ]
      }

----

.. http:get:: /api/tree/assets/(asset_type)

   Returns the list of asset records for a single type, used to populate tree
   sub-nodes without loading the full structure.

   :param asset_type: Asset type.
   :statuscode 200: Returns ``{"assets": [{"id": "...", "title": "..."}, ...]}``.

----

Search
------

.. http:get:: /api/search

   Full-text search across all asset types.

   :query q: Search query string.

   :statuscode 200: Returns grouped results:

   .. code-block:: json

      {
        "results": {
          "organizations": [{"id": "...", "title": "Acme Corp"}],
          "teams": [],
          "facebook_apps": [{"id": "...", "title": "My App"}]
        }
      }

----

.. http:get:: /api/dashboard/summary

   Returns counts of assets and dashboards for display on a summary widget.

   :statuscode 200: Returns summary counts (document counts per major index).
