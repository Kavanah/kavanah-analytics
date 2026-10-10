.. _api-user-management:

User Management API
====================

These endpoints back the User Management page (Users, Roles,
Registrations, Login Activity, and Settings tabs).

**Permission gating:**

- The page itself (``GET /user-management``) is gated on the
  ``mc_user_management`` permission.
- Most ``/api/user-management/*`` endpoints require ``asset_admin`` or
  ``superuser`` (``_require_admin``).
- A small set of operations (disable 2FA, set/reset password, edit
  email, set MFA bypass, create custom roles) require ``superuser``.

----

.. http:get:: /user-management

   Renders the User Management page (HTML). Lists all Elasticsearch
   security users with their roles and MFA status, plus the Roles,
   Registrations, Login Activity, and Settings tabs.

   Gated on ``mc_user_management``.

   :statuscode 200: User Management page rendered.
   :statuscode 403: Missing ``mc_user_management`` permission.

----

.. http:post:: /api/user-management/sync-roles

   Creates all missing ``org_*`` and ``team_*`` Elasticsearch security
   roles based on the current contents of ``de_asset_organization`` and
   ``de_asset_teams``.

   Existing roles are not modified. Only missing roles are created.

   Each created role is granted the following Kibana privileges in the
   ``default`` space:

   - ``dashboard.read``
   - ``discover.read``

   Requires ``asset_admin`` or ``superuser``.

   :statuscode 200:

   .. code-block:: json

      {
        "success": true,
        "created": ["org_abc123", "team_def456"],
        "already_present": ["org_xyz789"],
        "total_created": 2,
        "total_present": 1
      }

   :statuscode 403: Insufficient role.

----

.. http:post:: /api/user-management/create-role

   Creates a single ``org_*`` or ``team_*`` Elasticsearch security role.

   **Request body (JSON):**

   .. code-block:: json

      { "role_type": "org", "title": "Acme Corp" }

   :json string role_type: ``"org"`` or ``"team"``.
   :json string title: The organisation or team title as stored in Elasticsearch.
     This is used to look up the ``esid`` and generate the role name.

   :statuscode 200:

   .. code-block:: json

      {
        "success": true,
        "role_name": "org_abc123",
        "created": true,
        "skipped": false
      }

   ``created: false, skipped: true`` is returned if the role already exists.

   :statuscode 400: Unknown title or invalid role type.
   :statuscode 403: Insufficient role.

----

.. http:get:: /api/user-management/role-sync-status

   Returns the count of organisation and team records that do not have a
   corresponding Elasticsearch security role yet.

   :statuscode 200:

   .. code-block:: json

      {
        "missing_count": 3,
        "missing_roles": ["org_abc123", "team_def456", "team_ghi789"]
      }

   A ``missing_count`` of 0 means all roles are in sync.

----

.. http:post:: /api/user-management/enable-mfa

   Marks a specific user as 2FA-required. The user picks the method
   (TOTP or YubiKey) and completes enrolment themselves on next login
   .

   **Request body (JSON):**

   .. code-block:: json

      { "username": "jane.smith" }

   :json string username: The Elasticsearch security username.

   :statuscode 200: Returns ``{"success": true}``.
   :statuscode 400: Unknown username.
   :statuscode 403: Missing admin role.

----

.. http:post:: /api/user-management/disable-mfa

   Disables and removes MFA for a specific user. **Superuser only.**

   **Request body (JSON):**

   .. code-block:: json

      { "username": "jane.smith" }

   :json string username: The Elasticsearch security username.

   :statuscode 200: Returns ``{"success": true}``.
   :statuscode 400: Unknown username or MFA not currently enabled.
   :statuscode 403: Not a superuser.

----

.. http:post:: /api/user-management/set-mfa-bypass

   Temporarily exempts a user from MFA enforcement so they can log in
   once without 2FA — used to recover users who've lost their device.
   The bypass is consumed on the next successful login.
   **Superuser only.**

   :json string username: The Elasticsearch security username.
   :json bool enabled: ``true`` to grant bypass, ``false`` to revoke.

   :statuscode 200: Returns ``{"success": true}``.

----

User CRUD endpoints
-------------------

Most user-CRUD endpoints accept a JSON body, return the standard
``{"success": true|false, ...}`` envelope, and require
``_require_admin`` (``asset_admin`` or ``superuser``) unless noted.

.. list-table::
   :header-rows: 1
   :widths: 14 38 12 36

   * - Method
     - Path
     - Gate
     - Purpose
   * - POST
     - ``/api/user-management/create-user``
     - admin
     - Create a new ES security user with a generated temp password and
       welcome email. Body: ``username``, ``email``, ``full_name``,
       ``roles[]``, optional ``skip_email`` (superuser only).
   * - POST
     - ``/api/user-management/delete-user``
     - admin
     - Permanently delete a user. Cannot delete ``elastic``, the
       calling user, or any account with the ``superuser`` role.
   * - POST
     - ``/api/user-management/suspend-user``
     - admin
     - Disable an account without deleting it.
   * - POST
     - ``/api/user-management/reinstate-user``
     - admin
     - Re-enable a suspended account.
   * - POST
     - ``/api/user-management/bulk-action``
     - admin
     - Apply suspend / reinstate / delete to multiple usernames in one
       request. Body: ``action``, ``usernames[]``.
   * - POST
     - ``/api/user-management/update-roles``
     - admin
     - Replace a user's role list. Body: ``username``, ``roles[]``.
   * - POST
     - ``/api/user-management/update-email``
     - admin
     - Change a user's email address. Body: ``username``, ``email``.
   * - POST
     - ``/api/user-management/reset-password``
     - admin
     - Generate a new temporary password and email it to the user;
       forces password change on next login.
   * - POST
     - ``/api/user-management/admin-set-password``
     - admin
     - Set a known password for a user without sending email and
       without forcing a change on next login. Used for service /
       demo accounts. Body: ``username``, ``new_password``,
       optional ``reason``.
   * - GET
     - ``/api/user-management/user-detail/<username>``
     - admin
     - Return profile + recent login activity for the user, used by
       the right-panel sidebar.

----

Roles, permissions, and registrations
-------------------------------------

.. list-table::
   :header-rows: 1
   :widths: 14 38 12 36

   * - Method
     - Path
     - Gate
     - Purpose
   * - GET
     - ``/api/user-management/roles``
     - admin
     - List every role with type (system / org / team) and user count.
   * - POST
     - ``/api/user-management/roles``
     - superuser
     - Create a custom role with ``mc_*`` permissions. Body:
       ``role_name``, ``role_type`` (``system`` / ``org`` / ``team``),
       ``permissions{}``.
   * - GET
     - ``/api/user-management/role-permissions/<role_name>``
     - admin
     - Return the boolean ``mc_*`` and ``db_*`` flags for a role.
   * - POST
     - ``/api/user-management/role-permissions/<role_name>``
     - superuser
     - Update a role's permission flags. Body: any subset of the 13
       permission keys (``mc_*``, ``db_*``, ``hdr_*``).
   * - DELETE
     - ``/api/user-management/delete-role/<role_name>``
     - superuser
     - Delete a custom role. System roles can't be deleted.
   * - GET
     - ``/api/admin/registrations``
     - admin
     - List pending self-service registrations. Query: ``status``
       (``all``, ``pending_approval``, ``pending_verification``,
       ``approved``, ``rejected``).
   * - POST
     - ``/api/admin/registrations/<id>/approve``
     - admin
     - Approve a pending registration; creates the ES user with the
       password they chose at signup. Body: ``roles[]``.
   * - POST
     - ``/api/admin/registrations/<id>/reject``
     - admin
     - Reject a pending registration. Body: optional ``reason``.

----

Login Activity & audit
----------------------

.. list-table::
   :header-rows: 1
   :widths: 14 38 12 36

   * - Method
     - Path
     - Gate
     - Purpose
   * - GET
     - ``/api/user-management/audit-log``
     - admin
     - Paginated audit-log query. Query params: ``username``,
       ``action``, ``outcome``, ``include_unknown_users``,
       ``active_only``, ``limit``, ``offset``.
   * - GET
     - ``/api/user-management/login-trend``
     - admin
     - Daily unique-login counts for the last 30 days, used to draw
       the chart on the Login Activity tab.
   * - GET
     - ``/api/user-management/login-users``
     - admin
     - Users who logged in on a specific date. Query: ``date``.
   * - GET
     - ``/api/user-management/active-users``
     - admin
     - Users with successful logins in the last N minutes. Query:
       ``minutes`` (default 30).

----

Settings (deployment-wide)
--------------------------

.. http:get:: /api/admin/settings

   Returns deployment-wide settings (currently feedback-email
   recipients).

.. http:post:: /api/admin/settings

   Updates deployment-wide settings. Body: ``feedback_email``
   (comma-separated). **Superuser only.**

.. http:get:: /api/maintenance-alert

   Returns the current Maintenance Alert status — whether one is
   published, the message text, and the timestamp it was last
   updated. Any authenticated user can call this so the dashboards
   page can show the banner.

.. http:post:: /api/maintenance-alert/publish

   Publishes (or replaces) the Maintenance Alert. Body: ``message``
   (max 500 chars, plain text). **Superuser only.**

.. http:post:: /api/maintenance-alert/hide

   Hides the alert for all users while keeping the message stored.
   **Superuser only.**
