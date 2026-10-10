.. _user-management:

================
User Management
================

The **User Management** page (``/assets/user-management``) is where you
add, suspend, and audit Elasticsearch security users — and where you
manage two-factor authentication, role assignments, and self-service
registration approvals.

.. tip::

   For a screen-by-screen walkthrough of the page, see
   :doc:`/guide-management-console/user-management`. This page is the
   reference appendix: API examples, role mechanics, and self-hosted
   admin notes.

.. note::

   Access is gated by the ``mc_user_management`` permission, not by role
   membership. ``asset_admin`` and ``superuser`` have it by default; a
   superuser can grant it to any custom role from the **Roles** tab. A
   handful of actions (deleting users, disabling 2FA, editing email
   addresses, resetting/setting passwords, creating custom roles, the
   Settings tab) are still ``superuser``-only.

----

Page tabs
---------

The page is split across five tabs:

- **Users** — every account, grouped by role (Super Users, Asset
  Admins, Org Users, Team Users, Legacy/Other). Filter by status
  (Active/Suspended), MFA state (Enabled/Disabled), or free-text
  search.
- **Roles** — list, edit, and create roles. The Settings dialog
  controls which Management Console pages each role can access via
  ``mc_*`` permission toggles.
- **Registrations** — approve or reject self-service signups when
  registration is enabled.
- **Login Activity** — daily-uniques chart plus a filterable audit
  log.
- **Settings** *(superuser only)* — Feedback email recipients and the
  Maintenance Alert banner.

User row columns
~~~~~~~~~~~~~~~~

.. list-table::
   :header-rows: 1
   :widths: 22 78

   * - Column
     - Description
   * - Username
     - Elasticsearch security username (click to open the detail
       sidebar)
   * - Full Name
     - Display name set on the ES user record
   * - Status
     - *Active* or *Suspended*
   * - Roles
     - Role chips. Click **Roles** to open the Edit Roles dialog.
   * - 2FA Status
     - *Disabled*, *Pending*, *TOTP* / *YubiKey*, or *Bypassed*
   * - 2FA Method
     - The method the user actually enrolled in (blank when *Pending*)
   * - Last Used
     - Last successful 2FA challenge
   * - Actions
     - Enable / Change 2FA, Disable 2FA *(superuser only)*, Suspend /
       Reinstate

----

Creating user accounts
----------------------

Three ways to create a user, in order of preference:

1. **From the UI** — click **+ Create User** on the Users tab. Kavanah
   generates a 16-character temporary password, emails it to the user,
   and forces them to change it on first login. See
   :doc:`/guide-management-console/user-management` for the full flow.
2. **Self-service registration** *(if enabled)* — users sign up at
   ``/register``, verify their email, then wait for an admin to approve
   in the **Registrations** tab. The password they pick during signup
   is the one they'll use after approval; no temporary password is
   generated.
3. **Direct via the Elasticsearch Security API** — useful when you're
   bootstrapping the deployment, scripting bulk imports, or need to
   create a service account before the web app is reachable.

To create a user via the Elasticsearch API:

.. code-block:: bash

   curl -X POST "https://<es-host>:9200/_security/user/<username>" \
     -H "Content-Type: application/json" \
     -u elastic:<password> \
     --cacert /path/to/ca.crt \
     -d '{
       "password": "<initial_password>",
       "roles": ["asset_admin", "org_<org_esid>"],
       "full_name": "Jane Smith",
       "email": "jane@example.com"
     }'

The user can then log in at ``/login`` (the single canonical sign-in
URL). Set
``"metadata": {"force_password_change": true}`` if you want them
prompted to change the password on first login (the UI path does this
automatically).

Service / demo accounts
~~~~~~~~~~~~~~~~~~~~~~~

For accounts that shouldn't follow the temp-password-via-email flow
(e.g. shared demo accounts, automation users), a superuser can use
**Set password** in the user detail sidebar. This sets a known
password without sending email and without forcing a change on next
login. Audit-logged.

----

Assigning roles
---------------

Roles drive two things at once: which Management Console pages a user
can reach (via ``mc_*`` permissions on system roles) and which
organisation or team's data they can see in dashboards (via
``org_<esid>`` and ``team_<esid>`` roles).

The standard recipes:

- **Dashboard viewer** — assign one or more ``org_<esid>`` /
  ``team_<esid>`` roles. They log in via ``/dashboards/login`` and see
  data filtered to those orgs/teams.
- **Customer / org-level admin** — assign a custom system role with
  ``mc_user_management`` (and any other ``mc_*`` perms you want) plus
  the ``org_<esid>`` of their organisation. They can manage users in
  their org without seeing the rest of the deployment.
- **Asset administrator** — assign ``asset_admin`` plus any
  ``org_<esid>`` roles needed.
- **Superuser** — assign ``superuser``.

The simplest way to assign roles is the **Edit Roles** dialog in the
UI. The Elasticsearch API is also fine if you're scripting:

.. code-block:: bash

   curl -X PUT "https://<es-host>:9200/_security/user/<username>" \
     -H "Content-Type: application/json" \
     -u elastic:<password> \
     --cacert /path/to/ca.crt \
     -d '{ "roles": ["asset_admin", "org_abc123", "org_def456"] }'

.. warning::

   Setting ``roles`` via the API replaces the entire list. Always
   include every role you want the user to keep.

----

Syncing roles
-------------

When you create a new organisation or team in the Asset Explorer, the
corresponding Elasticsearch security roles (``org_<esid>`` and
``team_<esid>``) are not created automatically — they're needed for
dashboard data filtering.

**To sync all roles:**

1. On the **Users** tab, click **Sync All Roles** in the yellow banner
   that appears when one or more roles are missing. (No banner means
   all roles are already in sync.)
2. The system reads every organisation and team from the asset indices,
   checks which ``org_*`` / ``team_*`` roles already exist in ES
   Security, and creates the missing ones with Kibana ``dashboard.read``
   and ``discover.read`` access in the ``default`` space.
3. A toast shows how many were created.

**To create a single role manually:** click **Create Role** next to the
specific org or team in the Users-tab listing.

You can also check sync status at any time via the
``GET /api/user-management/role-sync-status`` endpoint (see
:doc:`../api-reference/user-management-api`).

Sync and per-section **Create Role** both require ``asset_admin`` or
``superuser``.

----

Enabling MFA for a user
-----------------------

1. Find the user in the User Management table.
2. Click **Enable 2FA** (or **Change 2FA** if they already have a
   method enrolled).
3. Confirm.

The user's 2FA Status flips to *Pending*. The **next time they sign
in**, they pick TOTP (an authenticator app) or YubiKey themselves and
walk through enrolment. The admin no longer chooses the method — the
old method-picker dropdown was removed in the redesign.

The user has **5 minutes** after entering their password to complete
the setup. If they run out of time, they can try again on the next
login.

If your deployment has MFA enforcement enabled (the deployment-wide
``ENFORCE_MFA`` config flag, on by default), every user is required to
enrol regardless of whether you flipped the per-user toggle.

See :doc:`two-factor-auth` for the full enrolment flow from the user's
side.

----

Disabling MFA for a user *(superuser only)*
-------------------------------------------

1. Find the user in the User Management table.
2. Click **Disable 2FA**.
3. Confirm.

MFA is removed immediately. The user's next login won't require a
second factor.

For temporary lockouts (e.g. a user lost their phone for the day),
prefer **Bypass MFA** instead of disabling outright — it lets the user
log in once without 2FA and re-enables enforcement automatically. Use
**Disable 2FA** when the device is gone for good and they need to
re-enrol from scratch.

----

Resetting passwords *(superuser only)*
--------------------------------------

The user detail sidebar (click any username) has two superuser-only
password actions added :

- **Reset password** — generate a fresh temporary password, email it
  to the user, and force them to change it on next login. Use this for
  users who've forgotten their password.
- **Set password** — set a known password without sending email or
  forcing a change. Intended for service / demo accounts.

Both are audit-logged.

----

Session Timeout
-----------------

Sessions use an **idle timeout** plus an **absolute cap**, mirroring Kibana's
``idleTimeout`` + ``lifespan`` model:

- ``SESSION_LIFETIME`` — idle timeout in seconds. Default 14,400 (4h).
  Refreshed on every authenticated request, so active users stay logged in.
- ``SESSION_MAX_LIFETIME`` — absolute lifetime cap in seconds. Default 86,400
  (24h). After this many seconds since login the user must re-authenticate
  even if they have been continuously active.

To adjust either, set the env vars on the ``imports_container`` and restart
the web app:

.. code-block:: bash

   # In the container environment, e.g. via Docker Compose .env file:
   SESSION_LIFETIME=3600        # 1 hour idle timeout
   SESSION_MAX_LIFETIME=86400   # 24 hour absolute cap

   # Restart the asset manager web app:
   sudo docker exec imports_container pkill -f "gunicorn.*8084"
   sudo docker exec -d imports_container bash -c \
     "cd /app/python/asset_management/web && gunicorn -b 0.0.0.0:8084 app:app \
      --workers 2 --timeout 120 \
      --access-logfile /tmp/asset_web_access.log \
      --error-logfile /tmp/asset_web_error.log"

.. note::

   Kibana has its own separate session, configured in ``kibana.yml``
   (``xpack.security.session.idleTimeout`` and ``xpack.security.session.lifespan``).
   The DEAM env vars above don't affect it. If you need Kibana's session to
   match DEAM, edit ``kibana.yml`` and restart the ``kib01`` container.
