.. _elasticsearch-ops:

===================
Elasticsearch Ops
===================

This page covers the Elasticsearch index structure used by Kavanah Analytics, Kibana
data view configuration, field mapping requirements, and common operational tasks.

----

Index Naming Convention
------------------------

Kavanah Analytics uses two families of Elasticsearch indices:

**Asset management indices** — Store configuration and metadata:

.. list-table::
   :header-rows: 1
   :widths: 40 60

   * - Index
     - Contents
   * - ``de_asset_organization``
     - Organisation records
   * - ``de_asset_teams``
     - Team records
   * - ``de_asset_regions``
     - Geographic regions
   * - ``de_asset_countries``
     - Countries
   * - ``de_asset_cities``
     - Cities
   * - ``de_asset_fb_accounts``
     - Facebook account records
   * - ``de_asset_fb_apps``
     - Facebook app records (App ID, Secret, Token)
   * - ``de_asset_fb_ad_accounts``
     - Facebook ad account records
   * - ``de_asset_fb_app_users``
     - Facebook OAuth user records (encrypted tokens)
   * - ``de_asset_fb_pages``
     - Facebook Page records
   * - ``de_asset_google_ads_customers``
     - Google Ads customer records
   * - ``de_asset_ga_properties``
     - Google Analytics property records
   * - ``de_asset_echo_tenants``
     - Echo/Chatwoot tenant records
   * - ``de_asset_ig_accounts``
     - Instagram account records
   * - ``de_asset_youtube_channels``
     - YouTube channel records
   * - ``de_asset_dashboards``
     - Kibana dashboard references and visibility settings
   * - ``de_asset_echarts_dashboards``
     - ECharts Dashboard Builder configurations
   * - ``de_asset_user_mfa``
     - Per-user MFA settings (method, TOTP secret)

**Data indices** — Store imported analytics data (one index per organisation, prefixed by slug):

.. list-table::
   :header-rows: 1
   :widths: 40 60

   * - Index pattern
     - Platform
   * - ``*fb_insights``
     - Facebook Ads insights (e.g. ``acmecorp_fb_insights``)
   * - ``*echo_conversations``
     - Echo/Chatwoot conversations
   * - ``*google_ads``
     - Google Ads performance data
   * - ``*ga_metrics``
     - Google Analytics 4 metrics
   * - ``*youtube_analytics``
     - YouTube channel analytics
   * - ``*ig_insights``
     - Instagram insights
   * - ``disciple-tools``
     - Disciple Tools pipeline data

----

Data Views
----------

Both the Dashboard Builder and (where present) Kibana use these data
views, defined in ``deployment/create-data-views.sh``:

.. list-table::
   :header-rows: 1
   :widths: 35 30 35

   * - Data view name
     - Index pattern
     - Platform
   * - Facebook Ads Insights
     - ``*fb_insights``
     - Facebook Ads
   * - Facebook Page Insights
     - ``*fb_page_insights``
     - Facebook Pages
   * - Instagram Insights
     - ``*ig_insights``
     - Instagram
   * - Google Ads
     - ``*google_ads``
     - Google Ads
   * - Google Analytics
     - ``*ga_metrics``
     - GA4
   * - YouTube Analytics
     - ``*youtube_analytics``
     - YouTube
   * - Echo Conversations
     - ``*echo_conversations``
     - Echo
   * - Chatwoot Conversations
     - ``*chatwoot_conversations``
     - Chatwoot
   * - Chatwoot Insights
     - ``*chatwoot_insights``
     - Chatwoot
   * - Disciple Tools
     - ``disciple-tools``
     - Disciple Tools

.. note::

   Built-in customer dashboards no longer live in Kibana — since the
   April 2026 ECharts migration they're served by the Dashboard
   Builder runtime. The Kibana data views remain useful for
   self-hosted admins who want raw Discover / Lens access to the same
   indices.

   The correct pattern for Facebook Ads is ``*fb_insights``, **not**
   ``facebook_ad_insights*``. Verify this in Kibana → Stack Management
   → Data Views if you can't reach data through Kibana.

----

Required Fields for Filtering
-------------------------------

For role-based dashboard filtering to work, all data index documents **must** contain
the following fields:

.. list-table::
   :header-rows: 1
   :widths: 30 70

   * - Field
     - Description
   * - ``organization_title``
     - The display name of the organisation (e.g. ``"Acme Corp"``)
   * - ``team_title``
     - The display name of the team (e.g. ``"Marketing Team"``) — can be ``null``

These values are resolved from the asset records at import time. If an import
script does not populate these fields, filtering will not work for that platform's data.

----

Google Ads Field Mapping Note
-------------------------------

Google Ads text fields in Elasticsearch have ``fielddata`` disabled. To aggregate
on text fields in the Dashboard Builder or in Kibana, you must use the ``.keyword``
sub-field:

- **Correct:** ``campaign_name.keyword``
- **Incorrect:** ``campaign_name``

The Dashboard Builder automatically appends ``.keyword`` for aggregations on
keyword-type fields from the Google Ads data view.

----

Kibana Space Configuration
---------------------------

All built-in dashboards use the **``default``** Kibana space.

To verify the space exists:

.. code-block:: bash

   curl -X GET "https://<kibana-host>:5601/api/spaces/space/default" \
     -H "kbn-xsrf: true" \
     -u elastic:<password>

To create the space if it does not exist:

.. code-block:: bash

   curl -X POST "https://<kibana-host>:5601/api/spaces/space" \
     -H "kbn-xsrf: true" \
     -H "Content-Type: application/json" \
     -u elastic:<password> \
     -d '{"id": "default", "name": "Kavanah Analytics", "color": "#1ba9f5"}'

----

Index Lifecycle Management (ILM)
----------------------------------

For production deployments, configure ILM policies on data indices to manage
storage costs:

- **Hot phase:** Active indices receiving writes — use high-performance storage
- **Warm phase:** Recent data still being queried — can use standard storage
- **Cold/Delete phase:** Data older than your retention policy

ILM policies are created and managed in Kibana → Stack Management → Index Lifecycle Policies.

For beta deployments without a large data volume, ILM is optional.

----

Checking Index Health
----------------------

View index health in the Status Reports page (``/assets/status-reports``), or
check directly in Kibana → Stack Management → Index Management.

From the command line:

.. code-block:: bash

   # Check all de_asset_* indices
   sudo docker exec imports_container curl -s \
     "https://localhost:9200/_cat/indices/de_asset_*?v&h=index,health,docs.count,store.size" \
     -u elastic:<password> --cacert /app/data/certs/es01_http_ca.crt

An index health of ``green`` means all shards are allocated. ``yellow`` means
replica shards are unallocated (common on single-node deployments — not a problem
for beta). ``red`` means primary shards are missing — investigate immediately.
