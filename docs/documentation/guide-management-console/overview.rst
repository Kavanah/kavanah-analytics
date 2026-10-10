.. _guide-management-console-overview:

============================
Management Console Overview
============================

The **Management Console** is the admin half of Kavanah Analytics —
where you configure data sources, build dashboards, manage users,
trigger imports, and recover from problems. Each page in the console
is gated by a separate permission, so you can hand a teammate access
to (say) just **User Management** and **Dashboard Builder** without
opening the rest of the console to them.

For step-by-step walkthroughs of the individual pages, see the rest
of this section. This page is a high-level orientation.

----

Accessing the Management Console
================================

If your role has any ``mc_*`` permission set, you'll see a **gear
icon** (⚙) in the top-right of the dashboards page. Clicking it
opens a dropdown with links to every Management Console page your
role can reach.

If your role has no ``mc_*`` permissions, the gear icon doesn't
appear and direct URLs to the Management Console redirect away.

----

What's in the Management Console
================================

The console has nine pages, each with its own permission flag and a
guide page in this section:

.. list-table::
   :header-rows: 1
   :widths: 30 30 40

   * - Page
     - Permission
     - What you do there
   * - **Asset Explorer**
     - ``mc_asset_explorer``
     - Browse, add, edit, delete the company / team / account /
       integration records that drive the rest of the platform.
   * - **Data Connectors**
     - ``mc_data_connectors``
     - Guided wizard for setting up Facebook / Echo / Chatwoot
       integrations. See :doc:`data-connectors`.
   * - **Data Explorer**
     - ``mc_data_explorer``
     - Natural-language query assistant powered by Claude. Ask
       questions in plain English and get charts, tables, or the
       raw ES query back.
   * - **Dashboard Builder**
     - ``mc_dashboard_builder``
     - No-code drag-and-drop dashboard editor. See
       :doc:`dashboard-builder`.
   * - **User Management**
     - ``mc_user_management``
     - Add and manage users, edit roles, configure 2FA, approve
       sign-ups, audit logins, post a maintenance banner. See
       :doc:`user-management`.
   * - **Import Data**
     - ``mc_import_data``
     - Trigger on-demand data imports from any connected platform
       and watch progress in a live log.
   * - **Import — Status Reports**
     - ``mc_import_status_reports``
     - Daily-health screen for the import pipeline — parsed errors
       plus a per-platform records / errors / runtime summary. See
       :doc:`status-reports`.
   * - **Import Logs**
     - ``mc_import_logs``
     - Full daily import log files, with download and delete.
   * - **Restore from Backup**
     - ``mc_restore_backup``
     - List local asset backups, restore from S3 Elasticsearch
       snapshots, apply individual config extractions. See
       :doc:`restore-from-backup`.
   * - **Testing**
     - ``mc_testing``
     - Internal QA tooling — pytest, Locust load tests, security
       regression scans, dashboard health checks, dependency
       audits, CI pipeline status. See :doc:`testing`.

----

Two role conventions you'll see
===============================

Permissions can be granted to any role from User Management → Roles.
There's no built-in "Org Admin" role any more (retired) —
it's been replaced by the per-page ``mc_*`` permission system.

In practice you'll usually see two flavours of admin:

- **``asset_admin``** — Kavanah's built-in catch-all admin role.
  Carries every ``mc_*`` permission by default. Use for staff who
  need full Management Console access.
- **``superuser``** — Kavanah's built-in unrestricted role. Has every
  ``mc_*`` permission **plus** the privileged actions that aren't
  exposed via permissions (deleting users, disabling 2FA, resetting
  passwords, creating custom roles, editing the Maintenance Alert
  and Feedback email).

For more granular access, create a custom role from the Roles tab
in User Management, tick exactly the ``mc_*`` permissions that
teammate needs, and assign the role. See
:doc:`/management-console/roles-and-permissions` for the full
permission reference.

----

What's *not* in the Management Console
======================================

A few things look like they should be in the console but live
elsewhere:

- **Your own profile** — change your display name, email, password,
  or 2FA method from the user-circle dropdown in the top-right
  (``/profile``). Available to every signed-in user, not gated on
  any ``mc_*`` permission. See
  :doc:`/guide-dashboards/your-profile`.
- **Public Impact Reports** — published from the dashboard viewer's
  toolbar (``Publish`` button), not from the Management Console.
  See :doc:`/guide-dashboards/sharing-reports`.
- **Kibana** — retained for self-hosted admins who want raw
  Discover / Lens access; no built-in Kavanah dashboards live there
  any more. Linked from the header by ``hdr_elastic_link`` on
  superuser roles only.
