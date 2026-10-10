.. _guide-management-console-data-connectors:

================
Data Connectors
================

The **Data Connectors** page is a guided wizard for linking a team to
the third-party platforms you import data from — Facebook, Echo, and
Chatwoot today. It writes the same kind of asset records you'd create
manually in the Asset Explorer, but walks you through the multi-step
chain in one flow without leaving the UI.

.. tip::

   For everything else (Google Ads, Google Analytics, YouTube,
   Instagram, Disciple Tools), there's no wizard yet — use the Asset
   Explorer directly. See
   :doc:`/getting-started/connecting-data-sources` for the manual
   recipes.

.. note::

   Access is gated on the ``mc_data_connectors`` permission. Granted
   to ``superuser`` by default; ``asset_admin`` and any custom role
   can be granted it from User Management → Roles.

----

Page layout
===========

The page opens with a **Team** dropdown — pick the team you're
configuring. Below that, one card per supported connector:

- Facebook Ads
- Instagram
- Google Analytics 4
- Google Ads
- YouTube
- Echo Messaging
- Chatwoot
- Disciple Tools

Each card shows a status pill (*Configured*, *Needs Attention*, or
*Not Configured*), the asset records currently linked to the team,
and one or more action buttons.

Tick **Show only configured** to hide the cards for platforms this
team isn't using.

The cards for **Google Ads / GA4 / YouTube / Instagram / Disciple
Tools** are read-only here — they can show the connection status but
the wizard doesn't cover them. Use the Asset Explorer to set them up.

----

Before you start
================

Each platform needs credentials you'll have collected ahead of time.

**Facebook (Ads or Pages)**

You'll need:

- A Facebook Business Manager **Account ID**.
- A Facebook developer **App ID** + **App Secret** (from
  developers.facebook.com → My Apps → your app → Settings → Basic).
- A long-lived **App Token** in the format ``<App ID>|<App Secret>``.
- Permission to grant OAuth scopes for the user account that owns the
  pages or ad accounts you want to import.

**Echo**

- Your Echo subdomain (e.g. ``myorg`` for ``myorg.echo.myapp.com``).
- A user **API key** from your Echo profile → API Access.

**Chatwoot**

- Your Chatwoot **Account ID** — the numeric value in your account URL
  at ``app.chatwoot.com/app/accounts/<id>/...``.
- A user **API access token** from Chatwoot → Profile Settings →
  Access Tokens.

----

Setting up Facebook
===================

Click **Setup Facebook Ads** (or **Refresh token** if the connector
already exists but needs re-authorising). The wizard has five steps,
walked through in a side panel.

Step 1 — Account
----------------

Choose **Use existing** if Kavanah already has a Facebook Account
record you want to reuse, or **Create new** to create one.

If creating new, fill in:

- **Account title** — a label like "Kavanah Facebook Business".
- **Account ID** — the Business Manager account ID.
- **Description** — optional.

Click **Next**.

Step 2 — App
------------

Same pattern. Pick an existing Facebook App or create a new one.

If creating new:

- **App title** — a label.
- **App ID** — from the Meta Developers dashboard.
- **App secret** — encrypted at rest.
- **App token** — usually ``<App ID>|<App Secret>``.
- **Description** — optional.

Click **Next**.

Step 3 — User and OAuth
-----------------------

Pick or create the **App User** that will hold the OAuth token.

If creating new:

- **User title** — a human-friendly name like "Pages OAuth User".
- **Role** — *Pages* (for Page / Instagram Insights) or *Ads* (for Ad
  Account Insights).

Click **Next**. The wizard creates the App User record and surfaces
two buttons:

- **Authorize via OAuth** — opens the Facebook OAuth dialog in a
  popup. Sign in as the user that has access to the pages or ad
  accounts you want to import, and grant the requested scopes.
- **I've authorized — re-check** — manually re-poll the token state.

While you're in the popup, the wizard polls every three seconds for
the token. When Facebook redirects back, the App User record is
updated with the encrypted token and the wizard's status flips to
*"Token received."*. **Next** stays disabled until that happens.

.. note::

   If the popup never returns, or you closed it before granting
   permissions, click **Authorize via OAuth** again to retry. The
   token is only persisted after a successful round-trip.

Step 4 — Pages
--------------

The wizard calls Facebook's Graph API with the new token and lists the
pages the user has access to. Tick the pages that belong to this team.

If the Graph call fails (most often because the OAuth scopes don't
include ``pages_read_engagement`` or ``pages_show_list``), click **FB
Graph not working? Paste page IDs manually** and enter one page per
line in ``page_id,Page Name`` format.

Click **Next**.

Step 5 — Confirm
----------------

The wizard summarises what it's about to link. Click **Confirm**.

Behind the scenes:

- Any new accounts, apps, app users, and pages selected as *"Create
  new"* are written to ``de_asset_facebook_accounts``, ``fb_apps``,
  ``fb_app_users``, and ``fb_pages``.
- The team record (``de_asset_teams``) is updated with
  ``ds_facebook = true`` and the list of page ESIDs.

The wizard finishes with *"Facebook is now configured for this team."*

----

Setting up Echo
===============

Click **Setup Echo Messaging** on the Echo card.

Step 1 — Pick or create a tenant
--------------------------------

Choose **Use existing** to reuse a tenant another team already uses,
or **Create new** with these fields:

- **Tenant title** — a label.
- **Subdomain** — your Echo subdomain.
- **API key** — encrypted at rest.
- **Description** — optional.

Click **Next**.

Step 2 — Confirm tenant
-----------------------

The wizard summarises whether it's reusing or creating the tenant.
Click **Confirm**.

Behind the scenes:

- A new tenant is written to ``de_asset_echo_tenants`` (if you chose
  *"Create new"*).
- The team record is updated with ``ds_echo = true`` and the tenant
  ESID.

You're done.

----

Setting up Chatwoot
===================

Click **Setup Chatwoot** on the Chatwoot card.

Step 1 — Pick or create an account
----------------------------------

If creating new:

- **Account title** — a label.
- **Account ID** — the numeric ID from your Chatwoot account URL.
- **API access token** — encrypted at rest.
- **Description** — optional.

Click **Next**.

Step 2 — Confirm account
------------------------

Click **Confirm** to link. Behind the scenes:

- A new account record is written to ``de_asset_chatwoot_accounts``
  (if creating).
- The team record is updated with ``ds_chatwoot = true`` and the
  account ESID.

----

Rotating credentials
====================

When tokens expire (Facebook user-access tokens lapse after about 60
days; Echo and Chatwoot keys live as long as you keep them) the same
wizard will let you replace just the credential without rebuilding
the whole chain.

- **Facebook** — click **Refresh token** on the connector card. The
  wizard skips you ahead to the OAuth step (Step 3) so you can
  re-authorise the existing App User without re-entering the App or
  Account.
- **Echo / Chatwoot** — click **Rotate** on the connector card. Edit
  the API key in the existing tenant / account record and confirm.

----

Limitations
===========

- **No validation on the credentials you paste.** The wizard doesn't
  call Facebook / Echo / Chatwoot to verify the App Secret or API
  token before saving — bad credentials surface only when the import
  pipeline runs. If a wizard succeeds but imports fail, double-check
  the credentials in the Asset Explorer.
- **Wizard is not idempotent.** Re-running the Echo or Chatwoot wizard
  on a team that's already configured *replaces* the linked
  tenant/account ESID without warning. Use **Rotate** when you just
  want to change credentials.
- **Google / YouTube / GA4 / Instagram / Disciple Tools have no
  wizard** today. Use the Asset Explorer for those.

----

Related pages
=============

- :doc:`/getting-started/connecting-data-sources` — manual recipes for
  every platform, including the ones the wizard doesn't cover yet.
- :doc:`/management-console/asset-explorer` — reference for the
  underlying asset records the wizard creates.
- :doc:`/management-console/oauth-connections` — what the OAuth
  callback service does, how to refresh Facebook tokens manually.
