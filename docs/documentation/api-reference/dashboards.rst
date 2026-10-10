.. _api-dashboards:

Dashboard Endpoints
===================

These endpoints support the Dashboards page (``/dashboards``) — the
read-only consumption surface that points at saved ECharts dashboards
from the Dashboard Builder. All ``/api/dashboards/*`` endpoints listed
here are accessible to any authenticated user; the response is filtered
server-side to the user's org/team scope.

For *creating* dashboards, see :doc:`dashboard-builder-api`. The Kibana
"sync" and "convert-to-echarts" endpoints documented in earlier
versions of this page were retired with the April 2026 ECharts
migration — built-in Kavanah dashboards no longer live in Kibana.

----

.. http:get:: /dashboards

   Renders the Dashboards page (HTML). Displays Kibana dashboards organised by
   platform tab, with organisation/team filter controls.

   :statuscode 200: Dashboards page rendered.
   :statuscode 302: Redirect to login if unauthenticated.

----

.. http:get:: /api/dashboards/(dashboard_key)

   Returns configuration and the Kibana iframe URL for a specific dashboard.

   :param dashboard_key: The dashboard's key value stored in ``de_asset_dashboards``.
   :query org: Organisation filter value (optional).
   :query team: Team filter value (optional).

   :statuscode 200: Returns dashboard config and URL:

   .. code-block:: json

      {
        "success": true,
        "dashboard_id": "abc123",
        "title": "Facebook Ads Overview",
        "url": "https://<kibana>/s/default/app/dashboards#/view/...",
        "visibility_mode": "show_all"
      }

   :statuscode 404: Dashboard not found.

----

.. http:get:: /api/dashboards/space/(space_id)

   Returns all dashboards in a given Kibana space. Retained for
   self-hosted admin tooling; built-in dashboards no longer live in
   Kibana.

   :param space_id: Kibana space ID (e.g. ``default``).

   :statuscode 200: Returns list of saved-object dashboards.
   :statuscode 503: Kibana is unreachable.

----

.. http:get:: /api/dashboards/filter-options

   Returns the list of organisations and teams available for the filter dropdowns
   on the Dashboards page, scoped to the current user's roles.

   :query platform: Platform name (optional; used to filter to orgs with data on that platform).
   :query org: Selected organisation value (optional; used to populate team dropdown).
   :query filter_type: ``all``, ``primary``, or ``partnerships`` (default: ``all``).

   :statuscode 200:

   .. code-block:: json

      {
        "orgs": [
          { "value": "Acme Corp", "label": "Acme Corp" }
        ],
        "teams": [
          { "value": "Marketing Team", "label": "Marketing Team" }
        ]
      }

   Users with ``org_*`` or ``team_*`` restricted roles see only their authorised
   organisations and teams. Unrestricted roles (``superuser``, ``asset_admin``)
   see all organisations.

----

.. http:get:: /api/dashboards/platform-availability

   Checks which platforms have data in Elasticsearch for the given organisation/team filter.
   Used to show the green/grey availability dots on the platform tabs.

   :query org: Organisation name to filter by.
   :query team: Team name to filter by (optional).
   :query filter_type: ``all``, ``primary``, or ``partnerships``.

   :statuscode 200:

   .. code-block:: json

      {
        "platforms": {
          "Facebook Ads": { "has_data": true },
          "Google Ads": { "has_data": false },
          "Echo Conversations": { "has_data": true }
        }
      }

   The check runs a multi-search (``_msearch``) against all data indices with a
   ``term`` filter on ``organization_title``. A platform is ``has_data: true`` if
   at least one document matches.

----

.. http:get:: /api/dashboards/data-pulled-as-of

   Returns the timestamp of the most recent document in each data index, representing
   the last time data was successfully imported for each platform.

   :query org: Organisation name (optional; if provided, filters to that org's data).

   :statuscode 200:

   .. code-block:: json

      {
        "facebook_ads": "2026-03-15T22:00:00Z",
        "google_ads": "2026-03-14T10:00:00Z",
        "echo": null
      }

   A ``null`` value means no documents have been imported for that platform.

----

.. http:get:: /api/dashboards/data-pulled-as-of-echarts

   Like ``/api/dashboards/data-pulled-as-of`` but returns timestamps
   keyed by ECharts dashboard ID rather than by data source — used by
   the per-dashboard *"Data pulled as of …"* badge in the toolbar.

   :statuscode 200: Returns a mapping of dashboard ID to ISO timestamp.
