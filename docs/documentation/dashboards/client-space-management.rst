.. _client-space-management:

=========================
Client Space Management
=========================

A **client space** in Kavanah Analytics is the combination of an **Organisation** and
optionally a **Team** that scopes the data you see across all dashboards and reports.
Understanding how client spaces work is essential for users who manage data for
multiple organisations or teams.

----

Organisations and Teams
------------------------

**Organisation** — The top-level grouping, typically a company or client (e.g. "Acme Corp").
All data imported into Kavanah Analytics is tagged with an ``organization_title`` field.

**Team** — A subdivision within an organisation (e.g. "Marketing Team", "Sales Team").
Teams are optional — not all organisations have teams. Data can be tagged with
a ``team_title`` field.

The hierarchy looks like this:

.. code-block:: text

   Organisation: Acme Corp
   ├── Team: Marketing Team
   ├── Team: Sales Team
   └── (direct, no team)

----

How Role-Based Access Works
-----------------------------

Your Kavanah Analytics account is assigned one or more **roles** by your administrator.
Roles determine which organisations and teams you can see data for:

.. list-table::
   :header-rows: 1
   :widths: 30 70

   * - Role
     - Data access
   * - ``org_<org_esid>``
     - All data for a specific organisation (all teams within it)
   * - ``team_<team_esid>``
     - Data for a specific team only
   * - ``superuser`` / ``asset_admin``
     - All organisations and teams (unrestricted)

Your administrator assigns these roles to your account. You cannot change your
own role assignments. Contact your administrator to request access to additional
organisations or teams.

----

Switching Between Client Spaces
---------------------------------

On the **Dashboards** page, use the **Organisation** and **Team** dropdowns in the
filter bar to switch between client spaces:

1. Select an organisation from the **Organisation** dropdown.
2. Optionally select a team from the **Team** dropdown.
3. All platform tabs will update their availability indicators.
4. The current dashboard will reload with data filtered to the selected space.

If you have access to only one organisation, the Organisation dropdown will show
only that organisation. If you have only team-level access, the team's parent
organisation is selected automatically.

----

Primary vs. Partnership Filter Modes
--------------------------------------

The **Filter Type** setting controls how teams within an organisation are displayed
in the filter dropdown. This is relevant when a team is associated with multiple
organisations (e.g. a shared services team that works across clients).

.. list-table::
   :header-rows: 1
   :widths: 20 80

   * - Filter mode
     - Description
   * - **All**
     - Show all teams for the selected organisation, regardless of relationship type
   * - **Primary**
     - Show only teams where this organisation is the primary (owning) organisation
   * - **Partnerships**
     - Show only teams where this organisation has a partnership/affiliate relationship

The filter mode is set in the Organisation dropdown. For most users, **All** is the
correct choice. Use Primary or Partnerships if your organisation shares teams with
partner organisations and you need to separate that data.

----

Data Isolation
--------------

Kavanah Analytics enforces data isolation automatically:

- When you open the Dashboards page, an invisible filter clause is
  injected into every chart query that restricts results to documents
  matching your organisation(s) and team(s).
- When you query data in the Dashboard Builder or Data Explorer,
  results are filtered to your authorised orgs/teams automatically;
  you can't override the scope from the UI.
- Import logs and status reports are visible to authenticated
  administrators with the matching ``mc_*`` permissions, regardless
  of org-level role.

A user with ``org_AcmeCorp`` access cannot see ``org_BetaCorp`` data,
even if they know the exact published-report URL or the dashboard ID
to load directly. Visibility checks run server-side on every query.

----

Managing Client Spaces (Administrators)
-----------------------------------------

If you are an administrator, you create and manage organisations and teams in the
**Asset Explorer**. See :doc:`../management-console/asset-explorer` for full details.

Key points:

- Every organisation and team gets an Elasticsearch document ID (``esid``)
- The corresponding ES security roles are named ``org_<esid>`` and ``team_<esid>``
- Roles must be synced after creating new organisations or teams (see :doc:`../management-console/user-management`)
- All imported data must include ``organization_title`` and ``team_title`` fields for
  filtering to work correctly
