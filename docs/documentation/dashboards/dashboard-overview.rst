.. _dashboard-overview:

==================
Dashboard Overview
==================

The **Dashboards** page (``/dashboards``) is the primary analytics
view in Kavanah Analytics. It displays dashboards for each
connected data platform, scoped automatically to the data your account
is authorised to see.

.. tip::

   For a screen-by-screen walkthrough — menus, filters, time ranges,
   the maintenance banner, and the account menu — see
   :doc:`/guide-dashboards/overview`. This page is the reference
   appendix: the platform-tab → data-view map and a short comparison
   between built-in dashboards and the Dashboard Builder.

----

Platform tabs
-------------

Each platform tab corresponds to one data view in Elasticsearch:

.. list-table::
   :header-rows: 1
   :widths: 25 35 40

   * - Tab
     - Data view
     - Index pattern
   * - Facebook Ads
     - Facebook Ads Insights
     - ``*fb_insights``
   * - Facebook Pages
     - Facebook Page Insights
     - ``*fb_page_insights``
   * - Instagram
     - Instagram Insights
     - ``*ig_insights``
   * - Google Ads
     - Google Ads
     - ``*google_ads``
   * - Google Analytics
     - Google Analytics
     - ``*ga_metrics``
   * - YouTube
     - YouTube Analytics
     - ``*youtube_analytics``
   * - Echo
     - Echo Conversations
     - ``*echo_conversations``
   * - Chatwoot
     - Chatwoot Conversations
     - ``*chatwoot_conversations``
   * - Disciple Tools
     - Disciple Tools
     - ``disciple-tools``

A tab is hidden if the data view doesn't exist in your deployment, or
shown in grey if no data matches the current org/team filter.

----

Org / team filter scope
-----------------------

Dashboards are filtered server-side by the user's role assignments:

- ``superuser`` / ``asset_admin`` see all organisations and teams.
- ``org_<esid>`` / ``team_<esid>`` users see only their assigned
  organisation(s) or team(s).

The injection is invisible to the dashboard — the filter is added at
query time as a ``term`` / ``terms`` clause on ``organization_title``
or ``team_title``. Users can't see or remove it. See
:doc:`client-space-management` for the full org / team model.

----

Dashboards page vs. Dashboard Builder
-------------------------------------

.. list-table::
   :header-rows: 1
   :widths: 30 35 35

   * -
     - Dashboards page
     - Dashboard Builder
   * - **URL**
     - ``/dashboards``
     - ``/dashboard-builder/``
   * - **Audience**
     - Every authenticated user
     - Roles with ``mc_dashboard_builder``
   * - **What it does**
     - Reads + filters dashboards
     - Creates and edits them
   * - **Permission**
     - none specific
     - ``mc_dashboard_builder``

Built-in dashboards (every platform tab) and Digital Impact Reports
are ready to view out of the box.
The Dashboard Builder is what you use to *make* a new dashboard;
the Dashboards page is where everyone *views* it.

See :doc:`dashboard-builder` for full details on the Dashboard
Builder and :doc:`/guide-management-console/dashboard-builder` for
the step-by-step guide.
