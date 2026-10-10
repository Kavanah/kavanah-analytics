.. _guide-management-console:

==================
Management Console
==================

Step-by-step guides for using the Kavanah Analytics Management Console. Each
guide walks you through a feature with annotated screenshots.

.. note::

   What you see in the Management Console depends on the ``mc_*``
   permissions on your role. By default ``superuser`` and
   ``asset_admin`` see every page; custom roles can be granted any
   subset from User Management → Roles. See
   :doc:`/management-console/roles-and-permissions` for the full
   permission map.

.. toctree::
   :maxdepth: 2

   overview
   user-management
   dashboard-builder
   data-connectors
   restore-from-backup
   status-reports
   testing

----

**In this section:**

:doc:`overview`
   A high-level tour of the Management Console — how to access it,
   which menus are visible to your role, and what each section does.

:doc:`user-management`
   Step-by-step guide to the User Management page: creating users,
   editing roles, enabling 2FA, approving registrations, and reviewing
   login activity.

:doc:`dashboard-builder`
   Step-by-step guide to the Dashboard Builder: creating a dashboard,
   adding and configuring charts, saving, and publishing Impact
   Reports to a public URL.

:doc:`data-connectors`
   Guided wizards for linking a team to Facebook, Echo, and Chatwoot —
   the no-Asset-Explorer path through the multi-step setup chain.

:doc:`restore-from-backup`
   Listing local asset backups, restoring from S3 Elasticsearch
   snapshots, and applying individual config extractions.

:doc:`status-reports`
   Daily-health screen for the import pipeline — parsed errors and
   per-platform records / errors / runtime summary.

:doc:`testing`
   Internal QA tooling: pytest, Locust load tests, security
   regression scans, dashboard health checks, dependency audits, and
   CI pipeline status.
