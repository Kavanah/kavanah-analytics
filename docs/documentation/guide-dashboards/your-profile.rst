.. _guide-dashboards-your-profile:

================
Your Profile
================

The **Profile** page (``/profile``) is where every Kavanah Analytics
user manages their own account — display name, email, password, and
two-factor authentication. Unlike the Management Console pages, you
don't need any special permission to reach it; just being signed in
is enough.

.. tip::

   Open Profile by clicking your **user-circle icon** in the top
   right of the dashboards page. The icon shows your full name (or
   username if no name is set).

----

Page layout
===========

Four cards stacked under the heading **"My Profile"** with the
subtitle *"Update your account details, password, and two-factor
authentication."*:

1. **Display name** — change the name shown on the dashboards page
   and across the deployment.
2. **Email address** — update where notification emails are sent.
3. **Password** — change your password.
4. **Two-factor authentication** — enrol or change a 2FA method,
   and disable 2FA where the deployment allows.

Each card has its own **Save…** button that's greyed out until you've
made a change.

----

Display name
============

1. Type a new name in the field.
2. Click **Save name**.

The button stays disabled until you've actually changed the value
(blank or unchanged input). Display names can be up to 200 characters.

The change shows up immediately in the user-circle dropdown and
anywhere else your name is rendered.

----

Email address
=============

1. Type a valid email address.
2. Click **Save email**.

Standard email validation runs in the field — local-part, ``@``, and
a domain with TLD. Up to 254 characters.

Changing your email here does not send a verification message — the
new address is trusted as-is. If you've genuinely lost access to the
old account, ask an admin to update it from the User Management page
instead, so the change is audit-logged under their account.

----

Change password
===============

1. Enter your **current password** in the first field.
2. Enter your **new password** (at least 8 characters).
3. Enter the same value in **Confirm password**.
4. Click **Change password**.

A live strength meter under the *new password* field colour-codes the
strength as **Weak** (red) → **Fair** (orange) → **Good** (blue) →
**Strong** (green). The button only enables when:

- All three fields are populated
- The new password has 8+ characters
- The new password is different from the current password
- *Confirm* matches *New password*

The current password is re-verified server-side on submit, so
someone with a stolen session cookie can't change your password
without knowing it. After a successful change you stay signed in;
embedded Kibana panels keep working too (the SSO cookie is refreshed
automatically).

----

Two-factor authentication
=========================

The 2FA card shows a status badge at the top:

- **Active — Authenticator app** *(green)* — TOTP enrolled and
  confirmed.
- **Active — YubiKey** *(green)* — YubiKey enrolled.
- **Pending — choose a method below** *(yellow)* — an admin enabled
  2FA for you but you haven't completed enrolment yet.
- **Not set up** *(grey)* — no 2FA on the account.

Below the badge are the two enrolment cards.

Set up an authenticator app
---------------------------

1. Click **Set up** under *Authenticator app*.
2. Scan the QR code in any TOTP app (Google Authenticator, Authy,
   1Password, etc.). The plaintext secret is also shown if you'd
   rather type it in.
3. Enter the 6-digit code your app shows.
4. Click **Confirm**.

The TOTP secret is only persisted to Elasticsearch after the
confirm step succeeds — if you abandon the page mid-setup, nothing is
saved.

Set up a YubiKey
----------------

1. Click **Set up** under *YubiKey*.
2. Insert your YubiKey, click in the OTP input, and tap the gold
   button.
3. The 44-character OTP autotypes into the field.
4. Click **Register**.

The OTP is validated against the Yubico API — if your deployment
doesn't have outbound internet access, validation will fail and the
admin should fall back to TOTP.

Switching methods
-----------------

You only have one active 2FA method at a time. Click **Set up** (or
**Re-enrol** if the button reads that) on whichever method you want
and complete the enrolment — the new method replaces the old.

Disable two-factor *(when the deployment allows)*
-------------------------------------------------

Below the enrolment cards a **Disable two-factor** section appears
when you have an active method. To disable:

1. Type your current password.
2. Click **Disable two-factor** *(red)*.
3. Confirm the prompt: *"Disable two-factor authentication for your
   account?"*

.. note::

   Two-factor authentication is usually required for every user. When
   it's required, the disable form is replaced with a locked note: *"Two-factor
   authentication is required for all accounts on this system and
   cannot be disabled."* If you've genuinely lost your device, ask a
   superuser to grant you a one-time **Bypass MFA** from the User
   Management page so you can log in once and re-enrol.

----

What this page does *not* do
============================

- **Account deletion.** You can't delete your own account.
  Suspension or deletion is admin-only — see
  :doc:`/guide-management-console/user-management`.
- **Session management.** No "log me out of every other device"
  button; sessions expire after the idle timeout (4 hours by default)
  or the absolute cap (24 hours by default), whichever comes first.
- **Recovery codes.** Kavanah doesn't issue printed backup codes
  today. The recovery path is *"ask a superuser to grant a Bypass
  MFA"*.
- **Role / permission editing.** You can't grant yourself extra
  permissions; ask an admin.
- **API tokens.** No personal access tokens — Kavanah uses session
  cookies and HTTP Basic Auth only.

----

Related pages
=============

- :doc:`/management-console/two-factor-auth` — full reference for
  TOTP / YubiKey including the admin-side flows.
- :doc:`/guide-management-console/user-management` — how admins
  enable / disable 2FA and reset passwords on your behalf.
