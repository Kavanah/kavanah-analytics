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

Every tier is the whole platform. The tiers differ only in who
qualifies and what each team costs.

.. list-table::
   :header-rows: 1
   :widths: 19 27 27 27

   * -
     - Missionary teams
     - Christian ministries
     - Businesses & other groups
   * - **Who qualifies**
     - Any Christian missionary team or organization doing
       evangelism, discipleship, and church planting
     - Any other Christian ministry
     - Everyone else, including agencies and commercial media
       teams that serve mission clients
   * - **Price**
     - $0 for every team (sponsored license, funded by donors)
     - $20 per team / month (covers our server costs)
     - $49.95 per team / month
   * - **How to start**
     - Apply for a sponsored license
     - Apply for sponsorship (the same application; the review
       sets the rate)
     - Sign up and start a 14-day free trial
   * - **Users**
     - Unlimited, no per-user charges
     - Unlimited, no per-user charges
     - Unlimited, no per-user charges
   * - **Data sources**
     - Every connector
     - Every connector
     - Every connector
   * - **Features**
     - The whole platform
     - The whole platform
     - The whole platform

**Every connector:** Meta (Facebook and Instagram), YouTube, Google Ads,
GA4, Chatwoot, Echo, and Disciple.Tools.

**The whole platform:** role-scoped access, so people see their team and
nothing else; custom dashboards; geographic views; and scheduled
reports.

A *team* is one reporting group, usually one campaign, one region, or
one initiative. Every new account starts with a 14-day trial, and
sponsorship applications are reviewed by a person, usually within two
business days. A sponsored license has no trial clock, never converts
to a paid plan, and is not metered as you add teams. Apply or start a
trial at `kavanahanalytics.com/pricing
<https://kavanahanalytics.com/pricing>`_.

Kavanah Analytics is also open source. You can run it on your own
servers for free (you then pass Meta App Review and Google OAuth
verification yourself): `github.com/Kavanah/kavanah-analytics
<https://github.com/Kavanah/kavanah-analytics>`_.

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
