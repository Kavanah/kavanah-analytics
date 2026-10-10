.. _guide-management-console-user-management:

================
User Management
================

The **User Management** page is where you invite people onto your Kavanah
Analytics deployment, control what they can see, and keep an eye on who's
signing in. Open it from the Management Console sidebar — you'll only see
the link if your role grants the **User Management** permission
(``mc_user_management``).

What you can do depends on which role you're signed in as:

- **Anyone with the User Management permission** can create users, edit
  roles, turn 2FA on or off, suspend or reinstate accounts, approve
  pending registrations, and review login activity. ``asset_admin`` and
  ``superuser`` have it by default; you can also grant it to a custom
  role from the Roles tab.
- **Asset admins** additionally can sync org/team security roles across
  Elasticsearch and create org or team roles from the Users-tab banner.
- **Superusers** see everything above plus the ability to delete users,
  edit user email addresses, reset or set a user's password, create
  custom roles, and configure deployment-wide settings (Feedback email
  and the Maintenance Alert).

The page is split into tabs across the top: **Users**, **Roles**,
**Registrations**, **Login Activity**, and (for superusers only)
**Settings**. We'll walk through each one.

----

Users Tab
=========

The Users tab is the first thing you land on. It lists everyone with an
account on this deployment, grouped by role.

Filtering and searching
-----------------------

At the top of the tab you'll find three controls to narrow the list:

- **Search** — type a username, full name, or email address
- **Status** — *All Statuses*, *Active*, or *Suspended*
- **MFA** — *All MFA*, *MFA Enabled*, or *MFA Disabled*

The counter next to the filters updates as you narrow the list.

How the user list is grouped
-----------------------------

Users are organised into collapsible sections so you can find people
quickly:

- **Super Users** *(superuser view only)* — accounts with the ``superuser``
  role
- **Asset Admins** *(superuser view only)* — accounts with ``asset_admin``
- **Org Users** — grouped by the organisation they belong to
- **Team Users** — grouped by team
- **Legacy / Other** *(superuser view only)* — accounts whose ``org_*`` or
  ``team_*`` roles don't match any organisation or team in the Asset
  Explorer (typically roles for orgs/teams that have since been deleted).
  These users still log in, but the orphaned role won't filter dashboards
  to anything meaningful.

Each section shows a small badge next to the org/team name indicating
whether the matching Elasticsearch security role exists. If the role is
missing, you'll see a **Create Role** button right there — one click
creates it.

The sync banner
~~~~~~~~~~~~~~~

If several org or team roles are missing at once, a yellow banner appears
at the top of the tab:

    *"3 org role(s) and 2 team role(s) are missing from Elasticsearch
    Security."*

Click **Sync All Roles** to create them all in one pass. **Sync All
Roles** and the per-section **Create Role** button both require
``asset_admin`` or ``superuser``.

Reading a user row
------------------

Each user row shows:

- **Username**, **Full Name**, and **Status** (Active / Suspended)
- The list of **roles** assigned to them
- Current **2FA Status** — one of *Disabled*, *Pending* (an admin enabled
  2FA but the user hasn't completed enrolment yet), *TOTP* / *YubiKey*
  (active and confirmed), or *Bypassed* (a superuser temporarily
  exempted them, e.g. while recovering a lost device)
- **2FA Method** — the method the user actually enrolled in
- **Last Used** — the last date the account authenticated with 2FA
- A row of action controls on the right

Click a username to open a side panel with the user's profile and login
history. The sidebar lets you:

- **Edit the email address** inline (click **edit** next to the email,
  then **save**)
- **Suspend** or **Reinstate** the account
- **Edit Roles** — same dialog as the Roles button in the user row
- **Reset password** *(superuser only)* — generate a fresh temporary
  password, email it to the user, and force them to change it on next
  login. Use this when someone's locked out of their account.
- **Set password** *(superuser only)* — set a known password without
  emailing the user and without forcing a change on next login. Intended
  for service accounts and demo logins where a temporary password isn't
  appropriate.

The **Login History** panel below the action buttons shows the user's
last 50 authentication events.

Creating a user
---------------

1. Click **+ Create User** at the top right of the page.
2. Fill in the form:

   - **Username** — lowercase letters, numbers, and underscores only
   - **Full Name** *(optional)*
   - **Email Address** — the address the welcome email will be sent to
   - **Roles** — tick the boxes for the roles you want to assign. Use
     the search box at the top of the role list to find them quickly

3. Click **Create User & Send Email**.

Kavanah generates a strong temporary password, stores it hashed in
Elasticsearch, and emails the user a welcome message with the password.
They'll be forced to change it on first login.

.. note::

   The role list shows only roles you're allowed to grant. If you're not
   a superuser, you won't see the ``superuser`` role here, and you can
   only assign org/team roles for the organisations or teams you have
   access to.

.. tip::

   Superusers can tick **Skip email (testing)** to create a user without
   sending a welcome email. The page will display the temporary password
   once — copy it before closing the dialog. Use this only when you need
   to hand the credentials to a user by a secure channel yourself.

Editing someone's roles
-----------------------

1. Find the user in the table and click the **Roles** button in their row.
2. In the **Edit Roles** dialog, tick or untick roles. Use the filter box
   to narrow long lists, or tick **Show selected only** to review what's
   currently assigned.
3. Click **Save Roles**.

Role changes reach the user's live session within about 30 seconds —
they don't need to sign out. Their next page load after that shows the
new access. To make a change take effect immediately, suspend the user
and reinstate them; that ends their existing session and forces a fresh
sign-in with the new roles.

Enabling 2FA for a user
------------------------

Admins turn 2FA on for a user; the user picks the method and completes
enrolment the next time they log in.

1. In the user's row, click **Enable 2FA** (the button is labelled
   **Change 2FA** if the user already has a method enrolled).
2. Confirm.

The user's 2FA Status flips to *Pending*. The next time they sign in,
they'll be prompted to choose between **TOTP** (an authenticator app
like Google Authenticator or 1Password) and **YubiKey** (a hardware
key), and walk through enrolment. Until they finish, they can't access
the rest of the app — but the *Pending* state is reversible: a superuser
can disable 2FA again from the same row.

See :doc:`/management-console/two-factor-auth` for the user-side setup
walkthrough and for notes on YubiKey validation.

.. note::

   If your deployment has MFA enforcement turned on (a deployment-wide
   config flag, set by your administrator), 2FA is required for every
   user — anyone without it will be prompted to enrol on their next
   login regardless of whether you flipped the switch on this page.

Disabling 2FA *(superuser only)*
--------------------------------

If a user loses their authenticator or hardware key, a superuser can
remove 2FA from their account:

1. Click **Disable 2FA** in the user's row.
2. Confirm.

The user's next login won't prompt for a second factor. Re-enable 2FA
as soon as they've regained access to their device.

Suspending and reinstating
--------------------------

- **Suspend** disables the account but keeps the record. Suspended users
  can't log in; their session is invalidated on the next request. Click
  **Reinstate** to re-enable the account later.
- Suspension is almost always the right choice for accounts that no
  longer need access — it's instant, reversible, and preserves the audit
  trail.

Bulk actions
------------

Tick the checkbox at the start of several user rows to open the **Bulk
Action Bar** at the bottom of the page. From there you can **Suspend
Selected**, **Reinstate Selected**, or **Delete Selected**. A
confirmation dialog runs the action across every ticked row.
**Cancel Selection** clears the tick boxes and closes the bar without
changing anything.

.. warning::

   **Delete Selected** permanently removes the accounts from Elasticsearch
   Security — audit history is kept but the account itself is gone. The
   built-in ``elastic`` user and any account with the ``superuser`` role
   can't be deleted through the UI. Prefer **Suspend Selected** unless
   you're certain you want the account gone for good.

----

Roles Tab
=========

The Roles tab lists every role Kavanah knows about, alongside a count
of how many users each role has.

Kavanah uses three types of roles:

- **System** — built-in roles like ``superuser`` and ``asset_admin``,
  plus any custom system-level roles a superuser has created. The
  ``superuser`` role can't be edited or deleted; other system roles can
  be edited but are protected from accidental deletion.
- **Organisation** — auto-prefixed ``org_<esid>``. Created when you add
  an organisation in the Asset Explorer (or synced from the banner on
  the Users tab).
- **Team** — auto-prefixed ``team_<esid>``. Created when you add a team.

Built-in system roles
---------------------

- ``superuser`` — full access to every feature, including Elastic and
  Kibana links. Cannot be edited.
- ``asset_admin`` — every Management Console page except Elastic.
  This is the typical "admin" role for non-superusers.
- ``editor`` — legacy editor role; reserved.
- ``elastic`` — legacy Elastic-only role; reserved.
- ``report_publisher`` — narrow capability role that grants
  **only** the ability to share Impact Reports via public URLs.
  Members can open the *Publish* dialog on a Reports-menu Impact
  Report, mint or revoke a shareable link, and choose its expiry.
  They cannot edit dashboards, view the Asset Explorer, or access any
  other admin tooling. Pair with ``asset_admin`` (or any role that
  grants ``mc_dashboard_builder``) if the user also needs to build
  reports.

Filter the list by name with the **Search roles...** box, or by type
with the **All Types** dropdown.

Reviewing or editing a role
---------------------------

Click **Settings** on any row to open the **Role Settings** dialog. From
there you can control which menu items and page links are visible to
that role:

- **Management Console — Sidebar** — one toggle per page: Asset
  Explorer, Data Explorer, Import Logs, Import — Status Reports, Import
  Data, Restore from Backup, User Management, Dashboard Builder, and
  Testing. Each maps to an ``mc_*`` permission and gates both the
  sidebar entry and access to the page itself.
- **Header Links** — toggle whether the *Dashboards* link in the top
  bar is visible.
- **Dashboard Page Links** — toggle the **Management Console** and
  **Data Explorer** entry-point links that appear on the dashboards
  landing page.

Click **Save Settings** when you're done. Changes take effect the next
time a user with that role loads a page.

.. note::

   Tick at least one Management Console sidebar page and the
   **Management Console** dashboard-page link automatically locks on —
   you can't remove the entry-point while the role still has access to
   pages behind it. Untick every sidebar page to free the toggle again.

.. note::

   The ``superuser`` role's settings can't be modified — superusers
   always have access to everything.

Creating a custom role *(superuser only)*
-----------------------------------------

1. Click **+ Create Role** at the top of the tab.
2. Choose a **Role Type** (System, Organisation, or Team) and a **Role
   Name**. The hint text in the dialog shows the naming convention for
   the type you pick — organisation roles must be prefixed ``org_`` and
   team roles ``team_``.
3. Toggle the menu and link permissions you want this role to have.
4. Click **Create Role**.

Role names are restricted to letters, digits, underscores, and hyphens
— no spaces or other special characters. If you type something
disallowed, the dialog flags it inline and won't submit.

Deleting a custom role
~~~~~~~~~~~~~~~~~~~~~~

Click **Delete** in the row for any non-system role. System roles can't
be deleted.

----

Registrations Tab
=================

The Registrations tab shows self-service registration requests. If
you've turned self-service registration on, users can sign up at
``/register`` and their requests land here waiting for approval.

Filter by status with the **Status** dropdown:

- **Pending Approval** — email has been verified, waiting for an admin
  to approve
- **Pending Verification** — the user hasn't clicked the verification
  link in their email yet
- **Approved** or **Rejected** — historical record of past decisions

Approving a registration
------------------------

1. Click **Approve** in the pending-approval row.
2. In the dialog, select the **Roles** you want to assign to the new
   account (hold Ctrl or Cmd to tick multiple).
3. Click **Approve**.

The user's Elasticsearch account is created using the password they
submitted at registration, and they'll receive an approval email telling
them they can log in. No temporary password is generated — the password
they chose during registration is the one they'll use.

Rejecting a registration
------------------------

1. Click **Reject**.
2. Optionally fill in the **Reason** (this is saved in the audit log but
   not emailed to the user).
3. Click **Reject**.

The pending record is marked *Rejected* and the user can try again if
they need to.

----

Login Activity Tab
==================

The Login Activity tab gives you two complementary views: a high-level
trend chart and a searchable audit log.

Daily logins chart
------------------

At the top you'll see **Daily unique logins** — a 30-day line chart of
distinct users who logged in each day. A small live counter on the right
shows how many users have been active in the last 30 minutes.

Click any day on the chart to drop a list of the users who logged in on
that date into the table below. This is a quick way to check whether a
specific team logged in during a campaign window, or to spot a day with
unusually low activity.

Audit log
---------

Below the chart is the audit log — every significant security event on
the deployment. Use the filters to narrow it:

- **Username** — exact match on a username
- **Action** — Login, Logout, MFA Challenge, MFA Required, Password
  Change, Create User, Delete User, Suspend User, Reinstate User, Update
  Roles, Password Reset Request, Password Reset Complete
- **Outcome** — Success, Failure, or both
- **Show unknown users** — include events from automated scanners and
  unknown usernames (usually off, useful when investigating)
- **Logged in last <window> only** — restrict to users whose most recent
  successful login falls inside the configured session idle window
  (4 hours by default) with no logout afterwards. This is a heuristic
  for "session may still be valid", not real-time presence — the audit
  log only records discrete events (login, logout, MFA, etc.) and not
  per-request activity, so a continuously-active user can still drop out
  of this filter the moment their last login crosses the idle window
  even though their session is being refreshed by activity

Click **Filter** to run the query. The result table shows the timestamp,
username, action, result, authentication method, whether 2FA was used,
the originating IP address, and a details column. Events flagged
**Suspicious** (pulsing red) mean a previously-unknown username
succeeded in logging in — investigate these promptly.

Paginate with the **Previous** and **Next** controls. If your role only
grants access to specific organisations or teams, the audit log is
filtered to show only events for users in that scope.

----

Settings Tab *(superuser only)*
===============================

The Settings tab holds two deployment-wide controls: **Feedback
Settings** and **Maintenance Alert**.

Feedback Settings
-----------------

Enter one or more email addresses (comma-separated) in the **Feedback
Email Address** field. Feedback submitted through the in-product
**Share Feedback** form is emailed to every address in this list. Click
**Save Settings** to apply.

Maintenance Alert
-----------------

Use the Maintenance Alert to display a short banner across the top of
the dashboards page — handy for warning users about scheduled
maintenance, planned downtime, or known issues.

1. Type your message in the **Message** textarea (up to 500 characters,
   plain text — no markup).
2. Click **Publish** to show the banner. It appears in the header for
   every signed-in user.
3. Users can dismiss the banner from their own session, but publishing
   a fresh message re-shows it for everyone.
4. Click **Hide** to take the banner down for everyone while keeping
   the message stored — handy if you want to re-publish the same text
   later.

The current state (*Published* or *Hidden*) and the stored message are
shown in the panel so you can tell at a glance whether anything is
live.

----

Common workflows
================

Onboarding a new organisation lead
-----------------------------------

If you want someone to manage their own organisation's users (a
"customer admin"), give them a custom role with the User Management
permission scoped to their org:

1. On the **Roles** tab, click **+ Create Role** and create a System
   role like ``org_lead`` with the **Management Console — Sidebar →
   User Management** toggle enabled (and any other pages you want them
   to reach, e.g. **Dashboard Builder**). Skip this step if you already
   have a suitable role.
2. On the **Users** tab, click **+ Create User**.
3. Fill in their details and tick the new role plus the specific
   ``org_<name>`` (and ``team_<name>``, if relevant) role for their
   organisation.
4. Click **Create User & Send Email** — they'll get a welcome email
   with a temporary password.
5. Once they've logged in and changed their password, return to the
   **Users** tab and click **Enable 2FA** in their row so they're
   prompted to set up TOTP or YubiKey on their next login.

When someone leaves
-------------------

1. Find the person on the **Users** tab.
2. Click **Suspend**. Their session is invalidated immediately and
   they can no longer log in.
3. Review the **Login Activity** tab for any unusual activity around
   their departure — it's a good housekeeping habit.
4. To fully remove the account, tick its checkbox and click
   **Delete Selected** in the bulk action bar. Suspension is
   reversible; deletion isn't.

After adding a new organisation in the Asset Explorer
-----------------------------------------------------

When you create a new organisation or team, Kavanah stores the entity
but doesn't automatically create the matching Elasticsearch role. You
have two ways to create it:

- Click **Create Role** on the banner that appears on the Users tab, or
- Wait until multiple roles accumulate and click **Sync All Roles** on
  the banner to create them all at once.

Users assigned to the new org or team won't see their data filtered
correctly until the role exists, so do this before issuing their
accounts.

----

Related pages
=============

- :doc:`/management-console/roles-and-permissions` — the full role
  naming convention and how role-based filtering works
- :doc:`/management-console/two-factor-auth` — the user-side 2FA setup
  flow
- :doc:`/management-console/user-management` — reference-level details
  about user management, including the Elasticsearch API if you need to
  create an account without the UI
