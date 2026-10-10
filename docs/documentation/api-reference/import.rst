.. _api-import:

Import Endpoints
================

These endpoints manage data import jobs. All require ``asset_admin`` or ``superuser``.

----

.. http:get:: /import-data

   Renders the Import Data page (HTML).

   :statuscode 200: Import Data page rendered.

----

.. http:post:: /api/import/run

   Starts one or more data import jobs asynchronously.

   **Request body (JSON):**

   .. code-block:: json

      {
        "imports": [
          { "source": "facebook_ads", "days": 7 },
          { "source": "google_ads", "start_date": "2026-01-01", "end_date": "2026-01-31" }
        ],
        "delete_log": false,
        "delete_data": false
      }

   :json array imports: One or more import job configs. Each must include ``source``
     and either ``days`` or both ``start_date`` + ``end_date``.
   :json string source: Platform to import. One of:
     ``facebook_ads``, ``facebook_pages``, ``echo``, ``google_ads``,
     ``google_analytics``, ``instagram``, ``youtube``.
   :json integer days: Rolling window in days (1–365). Imports from N days ago to today.
     Cannot be used together with ``start_date``/``end_date``.
   :json string start_date: Start date in ``YYYY-MM-DD`` format.
   :json string end_date: End date in ``YYYY-MM-DD`` format. Maximum range: 365 days.
   :json bool delete_log: If ``true``, today's log file is deleted before the run starts.
   :json bool delete_data: If ``true``, recently-cached JSON import files are deleted
     before fetching, forcing a clean re-fetch from the platform API.

   :statuscode 200:

   .. code-block:: json

      {
        "success": true,
        "import_id": "import-20260316-143022",
        "started": "2026-03-16T14:30:22Z",
        "message": "Import started for 2 source(s)"
      }

   :statuscode 400: Missing or invalid parameters (e.g. unknown source, days out of range,
     date range > 365 days, both ``days`` and date range provided).
   :statuscode 409: Another import is already running.

----

.. http:get:: /api/import/status/(import_id)

   Returns the current status of a running or completed import job.

   :param import_id: The import ID returned by ``POST /api/import/run``.

   :statuscode 200:

   .. code-block:: json

      {
        "import_id": "import-20260316-143022",
        "status": "running",
        "started": "2026-03-16T14:30:22Z",
        "completed": null,
        "sources_completed": ["facebook_ads"],
        "sources_pending": ["google_ads"],
        "error": null
      }

   ``status`` values: ``running``, ``complete``, ``error``, ``cancelled``.

   :statuscode 404: Import ID not found.

----

.. http:post:: /api/import/cancel/(import_id)

   Requests cancellation of a running import job. The import stops after the
   current document batch completes.

   :param import_id: The import ID to cancel.

   :statuscode 200: Returns ``{"success": true, "message": "Cancellation requested"}``.
   :statuscode 404: Import ID not found.
   :statuscode 409: Import is not currently running (already complete or cancelled).

----

.. http:post:: /api/import/delete-log

   Deletes today's import log file.

   :statuscode 200: Returns ``{"success": true}`` or ``{"success": false, "message": "No log for today"}``

----

.. http:get:: /api/import/log-exists

   Checks whether a log file exists for today's date.

   :statuscode 200: Returns ``{"exists": true}`` or ``{"exists": false}``.

----

.. http:get:: /api/import/log-content

   Returns the content of today's import log, starting from a byte offset.
   Used for live log tailing (polling from the last read position).

   :query offset: Byte offset to start reading from (default: ``0``). Pass the
     ``offset`` value returned by the previous call to retrieve only new content.

   :statuscode 200:

   .. code-block:: json

      {
        "exists": true,
        "content": "[2026-03-16 14:30:22] INFO Starting import...\n",
        "offset": 52,
        "size": 52
      }

   On the next poll, pass ``offset: 52`` to get only lines added since the last call.

   If no new content has been written, ``content`` will be an empty string.

   :statuscode 200: Returns ``{"exists": false}`` if no log file exists for today.

----

.. http:get:: /api/import/data-source-status

   Returns the connection and last-import status for all configured data sources.
   Used to populate the Data Source Status table on the Import Data page.

   :statuscode 200:

   .. code-block:: json

      {
        "sources": {
          "facebook_ads": {
            "configured": true,
            "last_import": "2026-03-15T22:00:00Z",
            "status": "success"
          },
          "google_ads": {
            "configured": true,
            "last_import": null,
            "status": "not_run"
          },
          "echo": {
            "configured": false,
            "last_import": null,
            "status": "not_configured"
          }
        }
      }

----

.. http:get:: /api/import/data-view-teams

   Returns the list of teams that have data in each Kibana data view.
   Used to populate the Data Views section of the Import Data page.

   :statuscode 200: Returns a mapping of data view ID to list of team names with data.

----

.. http:get:: /api/import/field-values

   Returns the distinct values of a specific field in an Elasticsearch
   index. Used to populate filter dropdowns in the import configuration
   UI.

   :query index: Elasticsearch index name.
   :query field: Field name to return distinct values for.

   :statuscode 200: Returns ``{"values": ["value1", "value2", ...]}``.

----

.. http:get:: /api/import/all-field-values

   Like ``field-values`` but unconstrained by the calling user's
   org/team scope — used by admins to populate the asset-import filter
   dropdowns. Same query parameters and response shape.

----

Permissions
-----------

The Import Data page (``/import-data``) and the heavy-lifting
endpoints (``run``, ``cancel``) are gated on the ``mc_import_data``
permission. The status / log / field-values endpoints accept
any authenticated session, since they're polled from background pages
that show import progress.
