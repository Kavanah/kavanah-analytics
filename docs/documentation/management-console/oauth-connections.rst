.. _oauth-connections:

==================
OAuth2 Connections
==================

Several platforms in Kavanah Analytics use OAuth2 for authentication.
This page covers how OAuth tokens are obtained, stored, and refreshed.

.. tip::

   For Facebook specifically, the **Data Connectors** page in the
   Management Console (gated on ``mc_data_connectors``) provides a
   guided wizard that walks you through Account → App → App User →
   Pages without leaving the UI — it handles the OAuth callback and
   token persistence automatically. The manual flow described below
   still works and is useful for non-Facebook platforms (Google,
   YouTube) and for self-hosted admins who'd rather click through the
   Asset Explorer directly.

----

Facebook OAuth
--------------

Facebook Ads, Pages, and Instagram data is accessed using a **user access token**
obtained via the Facebook OAuth2 flow. This token is scoped to a specific Facebook
user account that has access to the relevant ad accounts and pages.

**Architecture:**

Kavanah Analytics runs a dedicated OAuth2 callback server as a separate Flask application
on port 8083 inside the Docker container. This server handles the OAuth redirect
from Facebook and stores the resulting access token encrypted in Elasticsearch.

**Step 1 — Confirm the OAuth server is running**

.. code-block:: bash

   sudo docker exec imports_container ps aux | grep "gunicorn.*8083"

If the process is not running, start it:

.. code-block:: bash

   sudo docker exec -d imports_container bash -c \
     "cd /app/python && gunicorn -b 0.0.0.0:8083 fb_oauth_server:app \
      --workers 2 --access-logfile /tmp/fb_oauth_access.log"

**Step 2 — Generate the OAuth URL**

Navigate to **Asset Explorer → Facebook → Facebook App Users** and open the
app user record you created during data source setup. The record will show a
computed **OAuth URL** field.

The URL format is:

.. code-block:: text

   https://www.facebook.com/dialog/oauth?
     client_id={app_id}
     &redirect_uri=https://{your-domain}/fb-oauth/callback
     &scope=ads_read,pages_read_engagement,instagram_basic,...
     &state={app_user_esid}

**Step 3 — Complete the OAuth flow**

Open the OAuth URL in a browser while logged in as the Facebook user who has
access to the target ad accounts and pages:

1. Facebook will show a permissions consent screen.
2. Click **Allow**.
3. You are redirected back to the Kavanah Analytics OAuth callback URL.
4. The access token is exchanged, encrypted with Fernet encryption, and stored
   in the ``de_asset_fb_app_users`` Elasticsearch index.
5. The browser displays a success confirmation page.

**Token storage:** Tokens are stored encrypted using the Fernet key configured
in ``ENCRYPTION_KEY`` environment variable. They are never logged in plaintext.

----

Checking Facebook Token Health
--------------------------------

Use the token health check scripts to verify that stored tokens are valid:

.. code-block:: bash

   # Check all Facebook app user tokens
   sudo docker exec -it imports_container python3 \
     /app/python/import_scripts/facebook/check_oauth_tokens.py

   # Check a single token
   sudo docker exec -it imports_container python3 \
     /app/python/import_scripts/facebook/check_fb_token.py --user-esid <esid>

The script will output whether each token is valid, its expiry date, and whether
it needs refreshing.

**Facebook user access tokens typically expire after 60 days** for long-lived tokens.
Refresh before expiry to avoid import failures.

----

Refreshing Facebook Tokens
----------------------------

When a token expires or is revoked:

1. Open the Facebook App User record in the Asset Explorer.
2. Copy the **OAuth URL** from the record.
3. Open the URL in a browser as the Facebook user.
4. Complete the OAuth flow again.

The new token will overwrite the old one automatically.

----

Google Analytics (GA4) OAuth
----------------------------

Google Analytics 4 supports **per-connection OAuth** in addition to the legacy
service-account path. A user self-service-connects their own GA4 properties
through the **Data Connectors** wizard — no operator has to grant a shared
service account access to each property.

**The connect flow (Data Connectors → Google Analytics tile):**

1. **Account** — pick or create a Google account row (a label + the Google
   account email, used as the OAuth ``login_hint``).
2. **Authorize** — a popup opens the Google consent screen for the
   ``analytics.readonly`` scope. Grant access; the wizard polls for completion.
   The consent screen must be shown for Google to issue a refresh token.
3. **Properties** — the wizard enumerates the GA4 properties that account can
   access (live, via the Admin API ``list_account_summaries``). Select the ones
   this team should track.
4. **Confirm** — the properties are linked to the team (``ds_google_analytics``
   is set), and an encrypted refresh token is stored on a ``de_connection``
   (provider ``ga4``). The connector card then shows a connected-state summary
   with **Manage** and **Reconnect**.

**How the importer chooses credentials (OAuth-first, SA-fallback):**

For each property, the daily import (``get_ga_metrics``) resolves the property
to its GA4 OAuth connection; if one exists with a token, it authenticates with
that per-connection OAuth credential. Properties with no OAuth connection
continue to use the shared service account — so operator-seeded properties keep
working with no change.

**Token lifecycle:** Google refresh tokens do not expire on a timer the way
Meta's 60-day access tokens do; the short-lived access token is auto-refreshed
on every API call. If a user revokes access at Google (or the token otherwise
dies), the importer detects the rejected token, flips the connection to
``revoked`` (so the next run falls back to the service account and the card
shows *needs attention*), and the user reconnects through the wizard. There is
no rotation cron for GA4.

.. note::

   Production rollout of GA4 OAuth to end users depends on the shared Google
   OAuth client being verified by Google (the consent screen otherwise shows an
   unverified-app warning). Until then the flow is fully usable on staging
   against the unverified client.

----

Google Service Account Authentication
---------------------------------------

Google Ads and YouTube authentication use **service account** JSON keys or
manual OAuth tokens rather than the self-service OAuth wizard. (Google Analytics
also retains a service-account fallback — see *Google Analytics (GA4) OAuth*
above.) Service account credentials do not expire (unless the key is deleted in
Google Cloud Console).

**Where credentials are stored:**

Service account JSON is stored encrypted in:

- Google Ads customers: ``de_asset_google_ads_customers`` index, ``service_account_json`` field
- GA4 properties: ``de_asset_ga_properties`` index, ``service_account_json`` field
- YouTube channels: ``de_asset_youtube_channels`` index, ``oauth_token_json`` field (OAuth user token)

**Rotating a service account key:**

1. Go to Google Cloud Console → IAM & Admin → Service Accounts.
2. Select the service account → Keys → Add Key → Create new key (JSON).
3. Download the new JSON file.
4. In the Asset Explorer, edit the relevant asset and paste the new JSON into the
   ``Service Account JSON`` field.
5. Save the asset.

**YouTube OAuth token rotation:**

YouTube uses a user OAuth token, not a service account. To refresh:

.. code-block:: bash

   sudo docker exec -it imports_container python3 \
     /app/python/import_scripts/youtube/obtain_oauth_token.py

Follow the prompts, then update the YouTube Channel asset with the new token JSON.

----

Token Security
--------------

All stored credentials (Facebook tokens, service account JSON, API keys, passwords)
are encrypted at rest using **Fernet symmetric encryption** before being written
to Elasticsearch. The encryption key is stored in the ``ENCRYPTION_KEY`` environment
variable (or ``master-config.yaml``).

.. warning::

   Protect the ``ENCRYPTION_KEY`` environment variable carefully. If it is lost,
   all stored encrypted credentials become unrecoverable and must be re-entered
   manually. Back up the key alongside your backup strategy.
