.. _roles-and-permissions:

======================
Roles and Permissions
======================

Kavanah Analytics uses Elasticsearch Security roles for two distinct
purposes:

1. **Access to Management Console pages** is gated by ``mc_*``
   permissions attached to roles. Any role can be granted
   any subset of these permissions from the **Roles** tab in User
   Management.
2. **Dashboard data filtering** is driven by ``org_<esid>`` and
   ``team_<esid>`` roles, which restrict the user to a specific
   organisation or team's data.

A user's effective access is the **union** of all the permissions on
all the roles assigned to them.

----

Role types
----------

.. list-table::
   :header-rows: 1
   :widths: 30 70

   * - Role pattern
     - Purpose
   * - ``superuser``
     - Full unrestricted access. Locked — settings can't be changed.
   * - ``asset_admin``
     - Built-in admin role. Has every ``mc_*`` permission by default.
   * - Custom system role
     - Any role you create with the **Create Role** modal. Permissions
       are assigned by ticking ``mc_*`` toggles in Role Settings.
   * - ``org_<org_esid>``
     - Restricts dashboard data to one organisation.
   * - ``team_<team_esid>``
     - Restricts dashboard data to one team.

The ``<org_esid>`` / ``<team_esid>`` values are the Elasticsearch
document IDs of the corresponding ``de_asset_organization`` /
``de_asset_teams`` records. The Asset Explorer shows them next to each
asset.

----

Permission gates (``mc_*``)
---------------------------

Each Management Console page is gated by a single ``mc_*`` permission.
A role with the permission can reach the page; without it, the page is
hidden from the sidebar and 403s on direct navigation.

.. list-table::
   :header-rows: 1
   :widths: 32 68

   * - Permission
     - What it controls
   * - ``mc_asset_explorer``
     - Asset Explorer page and ``/api/assets/*`` endpoints
   * - ``mc_data_connectors``
     - Data Connectors wizard (Facebook / Echo / Chatwoot guided
       setup) and the ``/data-connectors/api/wizard/*`` endpoints
   * - ``mc_data_explorer``
     - Data Explorer page (NLQ assistant)
   * - ``mc_user_management``
     - User Management page and most ``/api/user-management/*``
       endpoints
   * - ``mc_dashboard_builder``
     - Dashboard Builder editor (``/dashboard-builder/``) and
       dashboard write APIs
   * - ``mc_import_data``
     - Import Data page (run on-demand imports)
   * - ``mc_import_logs``
     - Import Logs page
   * - ``mc_import_status_reports``
     - Import — Status Reports page
   * - ``mc_restore_backup``
     - Restore from Backup page (``/restore-backup/`` blueprint
       endpoints) plus the legacy ``/api/backup/*`` endpoints
   * - ``mc_testing``
     - Testing page (internal QA tooling — pytest, Locust,
       security scans, dashboard health checks)

Three additional toggles affect non-MC navigation:

- ``hdr_dashboards_link`` — show *Dashboards* in the top header.
- ``db_management_console`` — show the *Management Console* entry-point
  link on the dashboards landing page. Auto-locked on while any
  ``mc_*`` permission is set.
- ``db_data_explorer`` — show the *Data Explorer* entry-point link on
  the dashboards landing page.

Public pages and routes
~~~~~~~~~~~~~~~~~~~~~~~

- ``/login`` (single sign-in surface; ``/assets/login`` and
  ``/dashboards/login`` are permanent redirects), ``/mfa/*``
  (challenge + setup; ``/assets/mfa/*`` is a permanent redirect
  to the bare path), ``/register``, password reset routes — public.
- ``/dashboards`` and ``/dashboard-builder/view/<id>`` — any
  authenticated user. Data is filtered by org/team roles.
- Public Impact Report URLs (``/public/report/<token>``) — no auth
  required; viewer is scoped server-side.

Superuser-only operations
~~~~~~~~~~~~~~~~~~~~~~~~~

Some operations are gated on the ``superuser`` role specifically (not
on an ``mc_*`` permission):

- Disabling a user's 2FA
- Resetting or setting another user's password
- Editing another user's email
- Creating, editing, or deleting custom roles
- Editing the deployment-wide Feedback email and Maintenance Alert

----

How Data Filtering Works
-------------------------

When an authenticated user accesses the Dashboards page or the Dashboard Builder
viewer, Kavanah Analytics inspects their session roles and injects an invisible data filter.

**Single-organisation user** (has ``org_abc123``):

The system looks up the organisation title for ``abc123`` in the ``de_asset_organization``
index, then injects:

.. code-block:: json

   { "term": { "organization_title": "Acme Corp" } }

**Multi-organisation user** (has ``org_abc123`` and ``org_def456``):

.. code-block:: json

   { "terms": { "organization_title": ["Acme Corp", "Beta Corp"] } }

**Unrestricted users** (``superuser``, ``asset_admin``):
No filter is injected. All organisations' data is visible.

This filter is applied to every Kibana visualisation in the ``default`` space,
so users cannot bypass it by modifying the Kibana dashboard URL.

----

Kibana Space
------------

All built-in dashboards use the ``default`` Kibana space. Roles created
by the sync process are granted the following Kibana privileges in
this space:

- ``dashboard.read``
- ``discover.read``

``asset_admin`` and ``superuser`` roles have full access to all Kibana
spaces.

.. note::

   Day-to-day, almost no end user touches Kibana directly any more —
   the dashboards, builder, and Data Explorer all run inside the DEAM
   web app. Kibana is retained as a backstop for self-hosted admins who
   want raw query access. If you're rolling out Kavanah for the first
   time, you typically don't need to think about Kibana spaces at all.

----

Adding Roles to a User
-----------------------

Roles are assigned directly in Elasticsearch Security. To add a role to an existing user:

.. code-block:: bash

   curl -X POST "https://<es-host>:9200/_security/user/<username>" \
     -H "Content-Type: application/json" \
     -u elastic:<password> \
     --cacert /path/to/ca.crt \
     -d '{
       "roles": ["asset_admin", "org_abc123", "org_def456"]
     }'

.. warning::

   When you set ``roles`` via the API, it **replaces** the user's existing role list.
   Always include all desired roles in the update, not just the new ones.

----

Checking Role Sync Status
--------------------------

You can check how many organisation and team roles are missing (have assets in the
database but no corresponding ES security role) via:

- **User Management page:** The **Role Sync Status** indicator shows missing count
- **API:** ``GET /api/user-management/role-sync-status``
  (returns ``{ "missing_count": N, "missing_roles": [...] }``)
