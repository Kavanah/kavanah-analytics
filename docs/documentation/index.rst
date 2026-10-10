.. Kavanah Analytics Documentation master file

==================================
Kavanah Analytics Documentation
==================================

**Kavanah Analytics** is Kavanah Media's digital engagement analytics
platform. It aggregates data from your advertising and messaging
channels — Facebook Ads, Facebook Pages, Instagram, Google Ads, YouTube,
GA4, Echo, Chatwoot, and Disciple Tools — and surfaces it through
interactive dashboards and a Claude-powered
natural-language Data Explorer.

----

What do you want to do?
-----------------------

.. rst-class:: doc-landing-grid

- **View my data** — open the *Dashboards* page, filter by your
  organisation or team, and explore the built-in reports.
  → :doc:`guide-dashboards/overview`

- **Build a custom dashboard** — drag-and-drop charts on top of any
  data source, save and share with your team.
  → :doc:`guide-management-console/dashboard-builder`

- **Ask a question in plain English** — let the Data Explorer
  translate "How much did we spend on ads last month?" into a chart.
  → :doc:`guide-data-explorer/overview`

- **Manage my team** — create users, assign roles, approve sign-ups,
  audit logins, configure 2FA.
  → :doc:`guide-management-console/user-management`

- **Publish an Impact Report** — generate a public URL for a Digital
  Impact Report so supporters can view it without a Kavanah login.
  → :doc:`guide-dashboards/sharing-reports`

- **Set up the platform from scratch** — connect a new data source,
  run your first import, build your first dashboard.
  → :doc:`getting-started/index`

- **Find an API endpoint** — REST reference for every shipped Flask
  route across the asset manager and the Dashboard Builder /
  Data Explorer / public report blueprints.
  → :doc:`api-reference/index`

.. note::

   Kavanah Analytics v1.0 reached general availability in **May 2026**.
   Browse the :doc:`release-notes/v1.0-beta` for the full feature list.

----

Browse by section
-----------------

.. toctree::
   :maxdepth: 2
   :caption: Getting Started

   getting-started/index

.. toctree::
   :maxdepth: 2
   :caption: Dashboards

   guide-dashboards/index

.. toctree::
   :maxdepth: 2
   :caption: Data Explorer

   guide-data-explorer/index

.. toctree::
   :maxdepth: 2
   :caption: Management Console

   guide-management-console/index

.. toctree::
   :maxdepth: 1
   :caption: Reference

   glossary
   api-reference/index
   release-notes/index

----

Subscription Tiers
------------------

.. list-table::
   :header-rows: 1
   :widths: 20 15 15 50

   * - Feature
     - Free
     - Pro
     - Enterprise
   * - **Price**
     - $0
     - $49 / mo or $499 / yr
     - $249 / mo
   * - **Users**
     - Up to 10
     - Unlimited
     - Unlimited
   * - **Data sources**
     - Up to 5
     - All platforms
     - All platforms + custom
   * - **Core dashboards**
     - Read-only
     - Read-only
     - Read-only
   * - **Dashboard Builder**
     - —
     - Full access
     - Full access
   * - **Automated weekly reports**
     - —
     - Included
     - Included
   * - **Client space management**
     - —
     - Included
     - Included
   * - **Custom ingestion pipelines**
     - —
     - —
     - Included
   * - **White-label branding**
     - —
     - —
     - Included *(GA)*
   * - **SLA uptime**
     - —
     - —
     - Included
   * - **Support**
     - Community
     - Email (48 hr SLA)
     - Dedicated account manager

----

Frequently-asked references
---------------------------

* :doc:`getting-started/first-dashboard` — Build your first dashboard
  in minutes
* :doc:`guide-management-console/user-management` — Step-by-step user
  management walkthrough
* :doc:`guide-management-console/dashboard-builder` — Step-by-step
  dashboard builder walkthrough
* :doc:`management-console/two-factor-auth` — Setting up 2FA for your
  team
* :doc:`management-console/roles-and-permissions` — How the ``mc_*``
  permission system works
* :doc:`glossary` — Terminology reference (Asset, Data View, ESID,
  Impact Report, etc.)
* :doc:`api-reference/index` — REST API reference for every endpoint
