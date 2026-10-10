.. _dashboard-builder:

==================
Dashboard Builder
==================

The **Dashboard Builder** (``/dashboard-builder/``) is a no-code, drag-and-drop tool
for creating custom analytics charts. It stores
dashboards so they are available to your whole team.

.. tip::

   For a step-by-step walkthrough, see
   :doc:`/guide-management-console/dashboard-builder`. This page is the
   reference appendix: every chart type, aggregation, and config field.

.. note::

   The Dashboard Builder is gated by the ``mc_dashboard_builder``
   permission. ``asset_admin`` and ``superuser`` have it by
   default; a superuser can grant it to any custom role from the
   Roles tab in User Management. Saved dashboards can be *viewed* by
   all authenticated users at ``/dashboard-builder/view/<dashboard_id>``,
   subject to each dashboard's own *Visibility* setting.

----

Page Layout
-----------

The builder has a three-panel layout:

.. code-block:: text

   ┌─────────────────┬──────────────────────────────┬──────────────────┐
   │  LEFT PANEL     │  CANVAS                      │  RIGHT PANEL     │
   │  (280 px)       │  (flex-grow)                 │  (300 px)        │
   │                 │                              │                  │
   │  Data View      │  ┌──────────┐ ┌──────────┐   │  Chart config    │
   │  [select ▾]     │  │ Chart 1  │ │ Chart 2  │   │  (shown when a   │
   │                 │  └──────────┘ └──────────┘   │  chart is        │
   │  Fields list    │                              │  selected)       │
   │  (draggable)    │  [ + Add Chart ]             │                  │
   │                 │                              │                  │
   │  Chart types    │                              │                  │
   │  (draggable)    │                              │                  │
   └─────────────────┴──────────────────────────────┴──────────────────┘

**Left panel** — Data view selector, draggable field pills, and chart type tiles.

**Canvas** — The area where charts are placed. Charts can be reordered by dragging.

**Right panel** — Appears when a chart is selected. Shows chart configuration,
display toggles, and data filters.

----

Data Views
----------

A **data view** maps to an Elasticsearch index. Select your data view from the
dropdown at the top of the left panel. The field list updates automatically to show
the fields available in that index.

Available data views and their Elasticsearch indices:

.. list-table::
   :header-rows: 1
   :widths: 40 60

   * - Data view label
     - ES index pattern
   * - Facebook Ads Insights
     - ``*fb_insights``
   * - Facebook Page Insights
     - ``*fb_page_insights``
   * - Instagram Insights
     - ``*ig_insights``
   * - Google Ads Performance
     - ``*google_ads``
   * - Google Analytics
     - ``*ga_metrics``
   * - YouTube Analytics
     - ``*youtube_analytics``
   * - Echo Conversations
     - ``*echo_conversations``
   * - Chatwoot Conversations
     - ``*chatwoot_conversations``
   * - Chatwoot Insights
     - ``*chatwoot_insights``
   * - Disciple Tools
     - ``disciple-tools``

Field types are shown with icons in the field list:

- **D** — Date field
- **K** — Keyword (text / category) field
- **123** — Numeric field (integer, float, long)

----

Chart Types
-----------

Charts available from the **Add Chart** dropdown / modal:

.. list-table::
   :header-rows: 1
   :widths: 20 20 60

   * - Chart type
     - Required fields
     - Description
   * - Bar Chart
     - X field (keyword), Y field (numeric)
     - Grouped bar chart. Use ``breakdown`` for multi-series bars.
   * - Line Chart
     - X field (date or keyword), Y field (numeric)
     - Trend over time when X is a date; category comparison otherwise.
   * - Pie Chart
     - X field (keyword), Y field (numeric)
     - Proportional breakdown by category.
   * - Table
     - Row labels field, columns
     - Tabular data with configurable columns and aggregations. Display
       toggles control visible columns.
   * - Metric / KPI
     - Y field (numeric)
     - Single aggregated value with a large display number.
   * - Grouped Table
     - Row labels, group columns
     - Styled summary table with coloured group/column headers — used
       for Impact Report summary cards.
   * - Geographic Map
     - Statistic + one or more fields per data view
     - Three-layer map: country dots, region (admin-1) polygons shaded
       by value, and city dots, drawn over a base tile layer.

Templates-only chart types — added by clicking **Templates** in the
left panel (these aren't in the chart-type dropdown):

.. list-table::
   :header-rows: 1
   :widths: 30 70

   * - Chart type
     - Description
   * - Facebook Digital Impact Report (``report``)
     - Full-page report: KPI cards + funnel + geographic reach +
       monthly trends, scoped to Facebook Ads.
   * - Combined Digital Impact Report (``combined_report``)
     - Same layout, combining Facebook and Google Ads totals.
   * - Google Digital Impact Report (``google_report``)
     - Google Ads-only version of the same layout.
   * - Summary Report (``canva``)
     - Four-section visual (Reach / Engagement / Interaction / Jesus
       Journey) suitable for printing or embedding in Canva.
   * - Trend Line (``trend_line``)
     - Multi-source date histogram for comparing the same metric
       across different platforms on one chart.

----

Adding a Chart
--------------

**Method 1 — Drag and drop:**

1. Drag a chart type tile from the left panel onto the canvas. A new chart slot appears.
2. Drag a field pill from the field list onto the **X Field** drop zone.
3. Drag another field pill onto the **Y Field** drop zone.
4. The chart renders automatically once both required fields are assigned.

**Method 2 — Click to add:**

1. Click **+ Add Chart** on the canvas.
2. Select a chart type from the modal.
3. Assign fields by dragging from the left panel or selecting them in the right panel.

----

Aggregations
------------

The **Aggregation** setting in the right panel controls how the Y field values
are combined across multiple documents that share the same X value:

.. list-table::
   :header-rows: 1
   :widths: 20 80

   * - Aggregation
     - Behaviour
   * - Sum
     - Total of all values (default for metrics like spend, impressions)
   * - Average
     - Mean value across documents
   * - Min / Max
     - Smallest or largest single value
   * - Count
     - Number of documents (ignores Y field value)
   * - Unique
     - Count of distinct values (unique campaigns, unique users, etc.).
       Maps to an Elasticsearch ``cardinality`` aggregation under the
       hood — the dropdown label is "Unique".

----

Breakdown Field
---------------

An optional **Breakdown** field splits each X-axis bucket into multiple series —
one series per distinct value of the breakdown field. For example:

- X: ``date``, Y: ``spend``, Breakdown: ``campaign_name``
  → A multi-line chart showing spend over time for each campaign separately.

Breakdown is available for Bar and Line chart types.

----

Date Ranges
-----------

**Global date range** — Set once for the whole dashboard using the date range picker
in the toolbar. Applies to all charts that have a date field as their X axis.

**Per-chart date range** — Override the global range for a specific chart by setting
a date range in the right panel config section for that chart.

Date range format: ``YYYY-MM-DD``.

----

Data Filters
------------

The per-chart Data Filters UI described in earlier versions of this
doc isn't currently wired into the builder right panel — the
``filters.js`` module exists in the codebase but no **Add Filter**
button surfaces it. To narrow a chart's data today:

- Use the dashboard-wide **Org** and **Team** filters in the toolbar to
  scope every chart at once.
- For Impact Report charts and Tables, use the **Metrics editor** /
  **Columns editor** in the right panel — each column or metric has
  its own data view, field, and aggregation.

If you have a use case that needs per-chart inequality / range filters
(e.g. "spend > 100"), let the team know via the in-product Share
Feedback form so we can prioritise re-enabling the filter UI.

----

Display Toggles
---------------

After a chart renders, the **Display Toggles** section in the right panel shows
a checkbox for each series or column. Uncheck a series to hide it from the chart
without removing it from the data. This is useful for focusing on specific campaigns
or categories.

For **table charts**, display toggles control which columns are visible.

Toggle state is saved as part of the dashboard config.

----

Saving and Loading Dashboards
------------------------------

**Create:** Click **+ New** at the top of the left panel and enter a
title.

**Save:** Click **Save & Refresh** in the toolbar. Kavanah stores the
dashboard in the ``de_asset_echarts_dashboards`` index and re-queries
every chart with the current toolbar filters. Save & Refresh is
disabled until you make a change.

**Visibility (set in the Dashboard Properties panel, not at save
time):**

- **Show All** — visible to every authenticated user
- **Show Teams** — visible only to users assigned to the organisations
  or teams selected in the multi-select lists below this option
- **Hidden** — visible only to administrators

**Load:** Click a dashboard in the **Dashboards** explorer in the left
panel.

**Delete:** Load the dashboard, then use the explorer's per-row delete
control. (If you have unsaved changes when switching dashboards,
Kavanah pops up a *Save first / Discard / Cancel* dialog.)

**Publish (Impact Reports only):** open the dashboard in the viewer
and click **Publish** in the toolbar. See
:doc:`/guide-dashboards/sharing-reports` for the full lifecycle —
generate, copy, regenerate, and revoke.

----

Dashboard Viewer
----------------

Saved dashboards can be shared via their viewer URL:
``/dashboard-builder/view/<dashboard_id>``

The viewer is accessible to all authenticated users (no ``asset_admin`` role required).
Users who are role-restricted to a specific organisation will see data filtered to
their organisation automatically.

----

Dashboard Builder vs. Kibana Dashboards
-----------------------------------------

As of April 2026 all customer-facing dashboards live in the Dashboard
Builder. Kibana is retained for self-hosted admins who want raw
Discover / Lens access to the underlying indices, but no built-in
Kavanah dashboards run there any more.

.. list-table::
   :header-rows: 1
   :widths: 30 35 35

   * -
     - Dashboard Builder
     - Kibana
   * - **Use when**
     - Anything user-facing — every dashboard, report, and map
     - Ad-hoc queries against ES indices, Discover, raw Lens
   * - **Chart library**
     - Apache ECharts + Leaflet
     - Kibana / Vega / Lens
   * - **Access to create**
     - ``mc_dashboard_builder`` permission
     - Self-hosted admin (``superuser`` or Kibana role)
   * - **Data filtering**
     - Org/Team toolbar filters + per-chart fields
     - Manual KQL or filter pills
   * - **Saved where**
     - ``de_asset_echarts_dashboards`` index
     - Kibana saved objects (``default`` space)
