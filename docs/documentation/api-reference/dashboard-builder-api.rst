.. _api-dashboard-builder:

Dashboard Builder API
======================

The Dashboard Builder is implemented as a Flask blueprint with the URL
prefix ``/dashboard-builder/``.

**Permission gating:**

- The builder editor (``GET /``, plus all save / delete / properties
  endpoints) requires the ``mc_dashboard_builder`` permission.
  ``asset_admin`` and ``superuser`` have it by default; a superuser
  can grant it to any custom role.
- The publish endpoints (``POST/DELETE /publish``, ``POST
  /publish/regenerate``) are gated by the dedicated ``publish_reports``
  capability. Admin roles imply it; the dedicated
  ``report_publisher`` system role grants it without granting any
  ``mc_*`` admin sidebar items.
- The viewer (``GET /view/<id>``) and read-only endpoints (load
  dashboard, list dashboards, run query) are accessible to any
  authenticated user — visibility is enforced server-side per
  dashboard.

----

Pages
-----

.. http:get:: /dashboard-builder/

   Renders the Dashboard Builder page (HTML). Requires the
   ``mc_dashboard_builder`` permission.

   :statuscode 200: Builder page rendered.
   :statuscode 403: Missing ``mc_dashboard_builder`` permission.

----

.. http:get:: /dashboard-builder/view/(dashboard_id)

   Renders the read-only dashboard viewer for a saved ECharts dashboard.
   Accessible to all authenticated users. Role-restricted users see data
   filtered to their organisation automatically.

   :param dashboard_id: ECharts dashboard document ID.

   :statuscode 200: Viewer page rendered.
   :statuscode 404: Dashboard not found.

----

Data View Endpoints
-------------------

.. http:get:: /dashboard-builder/api/data-views

   Returns the list of available Elasticsearch indices (data views) for use
   in the data view selector.

   :statuscode 200:

   .. code-block:: json

      {
        "data_views": [
          { "id": "fb_insights", "label": "Facebook Ads Insights" },
          { "id": "google_ads", "label": "Google Ads Performance" },
          { "id": "ga_metrics", "label": "Google Analytics" },
          { "id": "echo_conversations", "label": "Echo Conversations" },
          { "id": "youtube_analytics", "label": "YouTube Analytics" }
        ]
      }

   System indices (those starting with ``.``) are excluded.

----

.. http:get:: /dashboard-builder/api/data-views/(data_view_id)/fields

   Returns the field list and types for a specific data view (index).
   Used to populate the field pills in the left panel.

   :param data_view_id: The Elasticsearch index name.

   :statuscode 200:

   .. code-block:: json

      {
        "fields": [
          { "name": "date", "type": "date" },
          { "name": "campaign_name", "type": "keyword" },
          { "name": "spend", "type": "numeric" },
          { "name": "impressions", "type": "numeric" },
          { "name": "clicks", "type": "numeric" }
        ]
      }

   Field types are simplified from ES types:

   - ``date`` → ``date``
   - ``keyword``, ``text`` → ``keyword``
   - ``long``, ``integer``, ``float``, ``double`` → ``numeric``

----

Query Endpoint
--------------

.. http:post:: /dashboard-builder/api/query

   Executes an Elasticsearch query for a chart configuration and returns
   the result in a format suitable for Apache ECharts rendering.

   **Request body (JSON):**

   .. code-block:: json

      {
        "data_view": "fb_insights",
        "chart_type": "bar",
        "x_field": "campaign_name",
        "y_field": "spend",
        "aggregation": "sum",
        "breakdown": null,
        "filters": [
          { "field": "spend", "op": "gt", "value": 100 }
        ],
        "date_range": {
          "field": "date",
          "from": "2026-01-01",
          "to": "2026-01-31"
        }
      }

   :json string data_view: Elasticsearch index name.
   :json string chart_type: One of ``bar``, ``line``, ``pie``, ``table``, ``metric``.
   :json string x_field: Field name for the X axis / category. Not required for ``metric``.
   :json string y_field: Field name for the Y axis / metric value.
   :json string aggregation: One of ``sum``, ``avg``, ``min``, ``max``, ``count``, ``cardinality``.
   :json string breakdown: Optional breakdown field (produces multi-series charts). ``null`` to omit.
   :json array filters: Optional list of filter objects (see filter format below).
   :json object date_range: Optional date range filter with ``field``, ``from``, ``to``.

   **Filter format:**

   .. code-block:: text

      { "field": "<field_name>", "op": "<operator>", "value": <value> }

   Filters can be passed in the request body and the server will honour
   them, but the per-chart Add Filter UI in the builder right panel is
   not currently wired in (the ``filters.js`` module exists in the code
   but isn't exposed). Filters are still useful for dashboard-wide org
   / team scoping which is applied automatically.

   Valid operators:

   .. list-table::
      :header-rows: 1
      :widths: 15 40 45

      * - Operator
        - Applies to
        - Behaviour
      * - ``eq``
        - Any
        - Exact match (``term`` query)
      * - ``neq``
        - Any
        - Must not match (``bool.must_not.term``)
      * - ``gt``
        - Numeric, date
        - Greater than (``range.gt``)
      * - ``gte``
        - Numeric, date
        - Greater than or equal (``range.gte``)
      * - ``lt``
        - Numeric, date
        - Less than (``range.lt``)
      * - ``lte``
        - Numeric, date
        - Less than or equal (``range.lte``)
      * - ``between``
        - Numeric, date
        - ``value`` must be a 2-element array ``[min, max]``; maps to ``range.gte/lte``
      * - ``in``
        - Keyword
        - Matches any of the values in the list (``terms`` query)

   **Response — bar / line / pie chart:**

   Returns an Apache ECharts option object:

   .. code-block:: json

      {
        "title": { "text": "" },
        "tooltip": { "trigger": "axis" },
        "legend": { "data": ["spend"] },
        "xAxis": { "type": "category", "data": ["Campaign A", "Campaign B"] },
        "yAxis": { "type": "value" },
        "series": [
          { "name": "spend", "type": "bar", "data": [4200.50, 3100.75] }
        ]
      }

   **Response — table chart:**

   .. code-block:: json

      {
        "chart_type": "table",
        "columns": ["campaign_name", "spend", "impressions"],
        "rows": [
          ["Campaign Alpha", 4200.50, 18000],
          ["Campaign Beta", 3100.75, 12500]
        ]
      }

   **Response — metric / KPI chart:**

   .. code-block:: json

      {
        "chart_type": "metric",
        "label": "spend",
        "value": 48203.25,
        "field": "spend",
        "aggregation": "sum"
      }

   :statuscode 200: Chart data returned.
   :statuscode 400: Missing required fields or invalid chart type.
   :statuscode 503: Elasticsearch is unreachable.

----

Trend Line Endpoint
-------------------

.. http:post:: /dashboard-builder/api/trend-line-data

   Returns a multi-source date histogram for rendering a Trend Line chart.
   Each source can come from a different data view.

   **Request body (JSON):**

   .. code-block:: json

      {
        "sources": [
          {
            "label": "Facebook Reach",
            "data_view": "fb_insights",
            "field": "reach",
            "color": "#4e79a7",
            "date_field": "date"
          },
          {
            "label": "Google Clicks",
            "data_view": "google_ads",
            "field": "clicks",
            "color": "#f28e2b",
            "date_field": "segments.date"
          }
        ],
        "filters": [],
        "date_range": { "from": "2026-01-01", "to": "2026-01-31" },
        "date_interval": "day"
      }

   :json array sources: List of source configs (``label``, ``data_view``, ``field``, ``color``, ``date_field``).
   :json array filters: Optional global filters applied to all sources.
   :json object date_range: ``from`` and ``to`` in ``YYYY-MM-DD`` format.
   :json string date_interval: Histogram bucket size: ``day``, ``week``, or ``month``.

   **Response:**

   .. code-block:: json

      {
        "x_data": ["2026-01-01", "2026-01-02", "..."],
        "series": [
          { "name": "Facebook Reach", "data": [12000, 15000, "..."], "color": "#4e79a7" },
          { "name": "Google Clicks", "data": [800, 950, "..."], "color": "#f28e2b" }
        ]
      }

   :statuscode 200: Trend data returned.
   :statuscode 400: Invalid request body.

----

.. http:get:: /dashboard-builder/api/map-data

   Returns geographic data for rendering Leaflet map charts.

   :query org: Organisation name to filter (optional).
   :query data_view: Data view to query.
   :query field: The location field name.

   :statuscode 200: Returns GeoJSON-compatible feature collection.

----

Dashboard CRUD
--------------

.. http:get:: /dashboard-builder/api/dashboards

   Returns the list of all saved ECharts dashboards.

   :statuscode 200:

   .. code-block:: json

      {
        "dashboards": [
          { "id": "uuid-123", "title": "Weekly Facebook", "updated_at": "2026-03-15" }
        ]
      }

----

.. http:post:: /dashboard-builder/api/dashboards

   Saves a dashboard configuration (create or overwrite).

   **Request body:** Full dashboard config JSON (see schema below).

   :statuscode 200: Returns ``{"success": true, "id": "<dashboard_id>"}``

----

.. http:get:: /dashboard-builder/api/dashboards/(dashboard_id)

   Loads a saved dashboard configuration.

   :param dashboard_id: Dashboard document ID.
   :statuscode 200: Full dashboard config JSON.
   :statuscode 404: Dashboard not found.

----

.. http:delete:: /dashboard-builder/api/dashboards/(dashboard_id)

   Deletes a saved dashboard.

   :param dashboard_id: Dashboard document ID.
   :statuscode 200: Returns ``{"success": true}``.
   :statuscode 404: Dashboard not found.

----

.. http:patch:: /dashboard-builder/api/dashboards/(dashboard_id)/properties

   Updates non-config properties of a dashboard (title, menu, menu
   position) without rewriting the entire chart layout. Used by the
   left-panel Dashboard Properties form.

   :param dashboard_id: Dashboard document ID.
   :json string title: New title (optional).
   :json string menu: ``Reports`` or ``Maps`` (optional).
   :json integer menu_position: Order in the sidebar (optional).
   :json string visibility_mode: ``show_all`` / ``show_teams`` /
     ``hide`` (optional).
   :json array allowed_org_esids: Org ESIDs for ``show_teams`` mode.
   :json array allowed_team_esids: Team ESIDs for ``show_teams`` mode.

   :statuscode 200: Returns ``{"success": true}``.
   :statuscode 404: Dashboard not found.

----

.. http:post:: /dashboard-builder/api/dashboards/(dashboard_id)/export

   Exports a rendered dashboard to Excel, CSV, or PDF.

   **Request body (JSON):**

   .. code-block:: json

      {
        "format": "xlsx",
        "title": "Report Title",
        "time_from": "2026-03-01",
        "time_to": "2026-03-31",
        "org": "Acme",
        "team": "Marketing"
      }

   :json string format: ``xlsx``, ``csv``, or ``pdf``.
   :json string title: Filename (without extension).
   :json string time_from: ISO date.
   :json string time_to: ISO date.
   :json string org: Optional organisation filter.
   :json string team: Optional team filter.

   :statuscode 200: Streams the rendered file in the response body.

----

.. http:get:: /dashboard-builder/api/dashboards/org-team-options

   Returns the list of organisations and teams the current user can
   apply as a dashboard scope. Used to populate the multi-selects on
   the Dashboard Properties panel.

----

Publish (Impact Reports)
----------------------------------

Publishing turns an Impact Report dashboard into a public URL anyone
can open without logging in. Eligible dashboards must:

- be of ``dashboard_type: echarts``
- live in the **Reports** menu
- contain only Impact Report charts (``report``, ``google_report``,
  ``combined_report``)
- have at least one ``allowed_org_esids`` or ``allowed_team_esids``
  assigned

.. http:get:: /dashboard-builder/api/dashboards/(dashboard_id)/publish

   Returns the publish state of a dashboard.

   :statuscode 200:

   .. code-block:: json

      {
        "success": true,
        "id": "uuid-123",
        "published": true,
        "token": "rN8s2V…",
        "path": "/public/report/rN8s2V…",
        "published_at": "2026-04-21T16:00:00Z",
        "expires_at": "2026-05-21T16:00:00Z",
        "expired": false,
        "published_org_title": "Kavanah",
        "published_team_title": null,
        "eligible": true,
        "eligibility_reason": null,
        "can_publish": true
      }

   ``can_publish`` reflects whether the **current user** holds the
   ``publish_reports`` capability — the toolbar UI hides the Publish
   button when this is ``false``. ``expired`` is ``true`` only when an
   active token's ``expires_at`` is in the past; the public route
   treats expired tokens as 404.

.. http:post:: /dashboard-builder/api/dashboards/(dashboard_id)/publish

   Publishes the dashboard. Token rotation is rotation-free when one
   already exists: changing ``expires_in_days`` / ``never_expires`` /
   scope updates the same record so already-shared URLs stay valid.
   Use ``/publish/regenerate`` to rotate the token.

   Requires ``publish_reports``.

   :requestbody (optional):

   .. code-block:: json

      {
        "expires_in_days": 30,
        "never_expires": false,
        "published_org_title": "Kavanah",
        "published_team_title": "Kavanah Team"
      }

   - ``expires_in_days`` — integer between 1 and 3650; defaults to 30.
   - ``never_expires`` — when ``true``, sets ``publish_expires_at`` to
     ``null`` and ``expires_in_days`` is ignored.
   - ``published_org_title`` / ``published_team_title`` — snapshot the
     publisher's currently selected org/team. The public route's
     forced filters use these instead of the dashboard's general
     ``allowed_*_esids`` so the URL stays scoped to what the publisher
     intended, regardless of later membership changes.

   :statuscode 200: Returns the publish state object above.
   :statuscode 400: Dashboard is not eligible, or the request body
     contains an invalid expiry value.
   :statuscode 403: Caller lacks the ``publish_reports`` capability.

.. http:post:: /dashboard-builder/api/dashboards/(dashboard_id)/publish/regenerate

   Rotates the publish token. Any URL that referenced the previous
   token immediately stops working. Optional request body fields
   (same shape as ``POST /publish``) update expiry / scope at the
   same time; omitted keys preserve the prior values.

   Requires ``publish_reports``.

   :statuscode 200: Returns the new publish state.
   :statuscode 403: Caller lacks the ``publish_reports`` capability.

.. http:delete:: /dashboard-builder/api/dashboards/(dashboard_id)/publish

   Revokes the publish token and clears the expiry / scope snapshot
   fields. The public URL returns *"This report is not available"* to
   anyone who tries to open it.

   Requires ``publish_reports``.

   :statuscode 200: Returns ``{"success": true}``.
   :statuscode 403: Caller lacks the ``publish_reports`` capability.

----

Dashboard Config Schema
-----------------------

This is the canonical JSON structure for a saved ECharts dashboard:

.. code-block:: json

   {
     "id": "uuid-123",
     "title": "Weekly Facebook Report",
     "created_at": "2026-01-20T10:00:00Z",
     "updated_at": "2026-01-20T12:00:00Z",
     "visibility_mode": "show_all",
     "allowed_org_esids": [],
     "allowed_team_esids": [],
     "global_date_range": {
       "field": "date",
       "from": "2026-01-01",
       "to": "2026-01-31"
     },
     "charts": [
       {
         "chart_id": "c1",
         "title": "Spend by Campaign",
         "position": { "col": 0, "row": 0, "w": 6, "h": 4 },
         "data_view": "fb_insights",
         "chart_type": "bar",
         "x_field": "campaign_name",
         "y_field": "spend",
         "aggregation": "sum",
         "breakdown": null,
         "filters": [
           { "field": "spend", "op": "gt", "value": 100 }
         ],
         "display_toggles": {
           "Campaign Alpha": true,
           "Campaign Beta": false
         }
       }
     ]
   }

**Visibility modes:**

- ``show_all`` — Visible to all authenticated users
- ``show_teams`` — Visible only to users with roles whose ESIDs match
  ``allowed_org_esids`` / ``allowed_team_esids`` (the server
  resolves the user's role list to ESIDs once per request)
- ``hide`` — Hidden (only administrators can see it)

**Publish fields** (added, present on Impact Reports only):

- ``publish_token`` — URL-safe random secret. Absent when unpublished.
- ``publish_enabled`` — Boolean mirror of token presence.
- ``published_at`` — ISO timestamp of first publish or last
  regenerate.
