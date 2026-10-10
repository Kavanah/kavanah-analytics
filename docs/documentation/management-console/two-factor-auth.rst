.. _two-factor-auth:

=======================
Two-Factor Auth (2FA)
=======================

Kavanah Analytics supports two-factor authentication (2FA) for all user
accounts. Two methods are supported:

- **TOTP** (Time-based One-Time Password) — works with any authenticator
  app (Google Authenticator, Authy, 1Password, etc.)
- **YubiKey OTP** — hardware security key manufactured by Yubico

An administrator enables 2FA for the account; the user picks the method
(TOTP or YubiKey) and enrols on their next login.

.. note::

   By default Kavanah enforces 2FA for every web UI login — the
   ``ENFORCE_MFA`` deployment config setting (in master-config.yaml or
   the env var of the same name) defaults to ``true``. When enforcement
   is on, every user is required to enrol whether or not an admin has
   flipped the per-user toggle. Set ``enforce_mfa: false`` to make 2FA
   per-user and opt-in instead.

----

Prerequisites (Administrator)
------------------------------

Before enabling YubiKey 2FA, configure the Yubico API credentials in your
environment:

.. code-block:: bash

   YUBICO_CLIENT_ID=<your_client_id>
   YUBICO_SECRET_KEY=<your_secret_key>

Obtain these from the `Yubico developer portal <https://upgrade.yubico.com/getapikey/>`_.
Without these credentials, YubiKey validation will fail.

TOTP requires no additional configuration.

----

Enabling 2FA for a user (admin)
-------------------------------

1. Navigate to **User Management** at ``/assets/user-management``.
2. Find the user in the table.
3. Click **Enable 2FA** (or **Change 2FA** if they already have a method
   enrolled).
4. Confirm.

The user's 2FA Status flips to *Pending*. The next time they sign in,
they pick TOTP or YubiKey themselves and walk through enrolment — the
admin no longer chooses the method (this changed).

----

TOTP Setup Flow (User)
-----------------------

After an administrator enables 2FA for your account (or if your
deployment enforces 2FA for every user):

1. Log in with your username and password.
2. You're redirected to the **MFA Setup** page. Click **TOTP** to start
   the authenticator-app flow.
3. A QR code is displayed. Open your authenticator app and scan it.
   The issuer name shown in the app is **Kavanah Analytics**.
4. The app generates a 6-digit code. Enter the code in the confirmation
   field.
5. Click **Confirm**.
6. TOTP setup is complete and you're logged in.

.. note::

   You have **5 minutes** from the time you enter your password to complete the
   MFA setup. If you do not complete setup within 5 minutes, you will be
   redirected to the login page and must start again.

From this point on, every login will require both your password and the current
TOTP code from your authenticator app.

----

TOTP Login Flow (User)
-----------------------

1. Enter your username and password on the login page.
2. You are redirected to the **MFA Challenge** page.
3. Open your authenticator app and find the Kavanah Analytics / Digital Engagement
   Dashboard entry.
4. Enter the current 6-digit code.
5. Click **Verify**.

TOTP codes are valid for 30 seconds. If the code expires while you are typing,
wait for the next code to appear in your app.

----

YubiKey Setup Flow (User)
--------------------------

After an administrator enables 2FA for your account (or if your
deployment enforces 2FA for every user):

1. Log in with your username and password.
2. You're redirected to the **MFA Setup** page. Click **YubiKey** to
   start the hardware-key flow.
3. Insert your YubiKey into a USB port.
4. Click in the **OTP** input field to focus it.
5. Tap the gold button on your YubiKey. A long OTP string is typed in
   automatically.
6. Click **Register**.
7. YubiKey setup is complete and you're logged in.

.. note::

   You have **5 minutes** to complete YubiKey registration after entering your
   password.

----

YubiKey Login Flow (User)
--------------------------

1. Enter your username and password on the login page.
2. You are redirected to the **MFA Challenge** page.
3. Insert your YubiKey.
4. Click in the OTP field and tap your YubiKey.
5. The OTP is submitted and validated against the Yubico API.

.. warning::

   YubiKey OTP validation requires an internet connection to the Yubico validation
   API (``api.yubico.com``). If your server does not have outbound internet access,
   YubiKey OTP validation will fail. In that case, use TOTP instead.

----

Disabling 2FA for a user *(superuser only)*
-------------------------------------------

1. Navigate to **User Management**.
2. Find the user (any 2FA status other than *Disabled*).
3. Click **Disable 2FA**.
4. Confirm.

The user's next login won't require a second factor.

Use this when the device or key is gone for good and the user needs to
re-enrol from scratch. For temporary lockouts (lost phone for the day,
travelling without the YubiKey), prefer **Bypass MFA** — it lets the
user log in once without 2FA and re-enables enforcement automatically
on subsequent logins.

----

2FA status states
-----------------

The 2FA Status column on the User Management page can show four
states:

- **Disabled** — no 2FA enrolled. If MFA enforcement is on, the user
  will be required to enrol on next login regardless.
- **Pending** — admin (or enforcement) has enabled 2FA, but the user
  hasn't completed enrolment yet. They'll be prompted on next sign-in.
- **TOTP** / **YubiKey** — fully enrolled with that method.
- **Bypassed** — superuser temporarily exempted the user. The bypass
  is consumed on the next successful login.

----

MFA Session State
-----------------

The MFA flow uses a two-phase session:

1. After a successful password entry, ``mfa_pending = True`` is set in the session.
2. While ``mfa_pending`` is True, all requests are redirected to the MFA challenge page,
   except for the MFA setup and challenge endpoints themselves.
3. After successful MFA verification, ``mfa_pending`` is cleared and the full session is established.
4. If the user does not complete MFA within **5 minutes**, the pending session expires.

This means a user cannot bypass MFA by navigating directly to another URL after
entering their password — they will always be redirected back to the MFA challenge.
