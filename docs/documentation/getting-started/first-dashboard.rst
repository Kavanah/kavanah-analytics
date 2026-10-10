.. _first-dashboard:

======================
Your First Dashboard
======================

This quickstart walks you through running your first data import, viewing a
built-in platform dashboard, and creating a custom chart in the Dashboard Builder.

**Prerequisites:**

- Your role grants the ``mc_import_data`` and ``mc_dashboard_builder``
  permissions (or an admin has set up data sources and built a starter
  dashboard for you). ``asset_admin`` and ``superuser`` have both by
  default.
- At least one data source is configured (see
  :doc:`connecting-data-sources`).

----

Step 1 — Run Your First Import
-------------------------------

1. Navigate to **Import Data** at ``/assets/import-data``.

2. In the **Select Data Sources** panel, check the platforms you want to import.
   For your first run, select one platform (e.g. *Facebook Ads*).

3. Set the **Date Range**: choose *Rolling — Last 7 days* to pull the most recent week.

4. Leave *Delete cached data* and *Delete today's log* unchecked.

5. Click **Run Import**.

The import log will appear below and update in real time. A successful import ends with
a line like::

   [INFO] Import complete. X documents indexed.

.. tip::

   If the import fails, check the log for authentication errors. The most common
   cause is an expired OAuth token or an incorrect API key. See
   :doc:`../management-console/oauth-connections` for token refresh instructions.

----

Step 2 — View a Built-In Dashboard
------------------------------------

1. Navigate to **Dashboards** at ``/dashboards`` (the platform's main
   landing page after login).

2. Pick a platform from the left sidebar (e.g. **Meta** for Facebook
   Ads).

3. If you imported data for multiple organisations, use the
   **Organisation** dropdown at the top to filter to your organisation.

4. The dashboard renders inline as interactive charts.

.. note::

   If the dashboard shows "No data", confirm that:

   - The import completed successfully (check Import Logs)
   - Your account has the correct organisation role (``org_<your_org>``)
   - The correct organisation is selected in the Organisation dropdown

----

Step 3 — Build Your First Custom Chart
----------------------------------------

The Dashboard Builder lets you create custom visualisations on top of your
imported data.

1. Navigate to **Dashboard Builder** at ``/dashboard-builder/``.

2. Click **+ New** at the top of the left panel and give the
   dashboard a name (e.g. *"My First Dashboard"*).

3. Add a chart: click **+ Add Chart** on the canvas (or use the chart
   type dropdown in the left panel) and pick **Bar Chart**.

4. Click the new chart to select it. The right panel shows the chart's
   config.

5. In **Data & Fields**, pick **Facebook Ads Insights** as the **Data
   View**, ``campaign_name`` as **X Field**, and ``spend`` as **Y
   Field**. The chart renders automatically.

6. In **Chart Info**, set the **Title** to something meaningful (e.g.
   "Spend by Campaign").

7. Optionally adjust the **Aggregation** dropdown (default: Sum).

8. Click **Save & Refresh** in the toolbar to persist the dashboard.

Your dashboard is now saved and can be reopened from the **Dashboards**
explorer in the left panel any time.

----

Next Steps
----------

- :doc:`../dashboards/dashboard-builder` — Full Dashboard Builder reference (all chart types, filters, date ranges)
- :doc:`../dashboards/reach-metrics` — Understanding paid vs. organic reach data
- :doc:`../management-console/import-data` — Import options in detail (date ranges, delete flags, cancellation)
- :doc:`../management-console/user-management` — Add team members and assign roles
