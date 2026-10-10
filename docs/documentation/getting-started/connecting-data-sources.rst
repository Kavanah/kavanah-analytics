.. _connecting-data-sources:

=========================
Connecting Data Sources
=========================

Before you can import data, you need to create **asset records** in the Asset Explorer
for each platform you want to connect. Each asset stores the credentials and
configuration that the import pipeline uses to authenticate with that platform.

.. note::

   Asset management requires the ``mc_asset_explorer`` permission. By
   default ``asset_admin`` and ``superuser`` have it; a superuser can
   grant it to any custom role from the User Management → Roles tab.

Navigate to the asset manager at ``https://kavanahanalytics.com/assets/`` and
use the tree navigation on the left to find the relevant asset type.

.. tip::

   For Facebook, Echo, and Chatwoot, you can also use the **Data
   Connectors** wizard under the Management Console (gated on the
   ``mc_data_connectors`` permission) — it walks you through the
   account / app / OAuth chain step-by-step instead of asking you to
   create each asset record by hand. The manual flow described below
   still works for everything the wizard supports.

----

Facebook Ads
------------

Facebook Ads data requires four linked asset records: **Account → App → App User (OAuth) → Ad Account**.

**Step 1 — Create a Facebook Account asset**

Navigate to *Facebook → Facebook Accounts* and click **Add New**.

Required fields:

- **Account Name** — A human-readable label (e.g. "Kavanah Media Facebook")
- **Account ID** — Your Facebook Business Manager account ID

**Step 2 — Create a Facebook App asset**

Navigate to *Facebook → Facebook Apps* and click **Add New**.

Required fields:

- **App Name** — The name of your Facebook developer app
- **App ID** — Found in the Facebook Developer portal under your app settings
- **App Secret** — Found in the Facebook Developer portal (*treat this as a password — it is stored encrypted*)
- **App Token** — A long-lived app access token (``{App ID}|{App Secret}`` format, or generated from the token tool)
- **Facebook Account** — Select the account you created in Step 1

**Step 3 — Create a Facebook App User (OAuth)**

Navigate to *Facebook → Facebook App Users* and click **Add New**.

Required fields:

- **Display Name** — A label for this OAuth user (e.g. "Admin User Token")
- **Facebook App** — Select the app from Step 2

After saving, the asset record will show an **OAuth URL**. Open that URL in a browser
and log in with the Facebook account that has access to the ad accounts you want to
import. This grants a user access token that is stored encrypted in Kavanah Analytics.

.. note::

   The Facebook OAuth flow is handled by a separate service running on port 8083.
   If the OAuth URL does not load, check with your administrator that the OAuth
   server is running.

**Step 4 — Create a Facebook Ad Account asset**

Navigate to *Facebook → Facebook Ad Accounts* and click **Add New**.

Required fields:

- **Ad Account Name** — A human-readable label
- **Ad Account ID** — Your ad account ID (format: ``act_XXXXXXXXXX``)
- **App User** — Select the OAuth user from Step 3

Once all four assets are linked, the Facebook Ads import pipeline can authenticate
and pull ad insights data.

----

Facebook Pages & Instagram
---------------------------

Facebook Pages and Instagram data are pulled using the same app and OAuth user
credentials as Facebook Ads.

**Facebook Pages:** Navigate to *Facebook → Facebook Pages* and click **Add New**.

Required fields:

- **Page Name** — Human-readable label
- **Page ID** — Your Facebook Page ID
- **Facebook App** — Select the app from the Facebook Ads setup

**Instagram Accounts:** Navigate to *Instagram → Instagram Accounts* and click **Add New**.

Required fields:

- **Account Name** — Human-readable label
- **Instagram Business Account ID** — Found in the Facebook Developer portal
- **Facebook App** — Select the linked Facebook App

----

Google Ads
-----------

Google Ads authentication uses a **service account** or **OAuth2 credentials** plus
a **customer ID**.

Navigate to *Google → Google Ads Customers* and click **Add New**.

Required fields:

- **Customer Name** — Human-readable label (e.g. "Kavanah Media Google Ads")
- **Customer ID** — Your Google Ads customer ID (format: ``XXX-XXX-XXXX``)
- **Developer Token** — Your Google Ads API developer token
- **Service Account JSON** — The contents of your Google service account key file
  (*stored encrypted*)

To obtain a service account key:

1. Go to the `Google Cloud Console <https://console.cloud.google.com/>`_ → IAM & Admin → Service Accounts.
2. Create a new service account or select an existing one.
3. Create a JSON key and download it.
4. Paste the full JSON contents into the **Service Account JSON** field.

----

Google Analytics 4
-------------------

Navigate to *Google → Google Analytics Properties* and click **Add New**.

Required fields:

- **Property Name** — Human-readable label
- **Property ID** — Your GA4 property ID (format: ``XXXXXXXXX``)
- **Service Account JSON** — Same service account used for Google Ads, or a separate one
  with ``Viewer`` access granted to the GA4 property

----

YouTube
--------

YouTube authentication uses an OAuth2 user token obtained via the
``obtain_oauth_token.py`` script.

Navigate to *YouTube → YouTube Channels* and click **Add New**.

Required fields:

- **Channel Name** — Human-readable label
- **Channel ID** — Your YouTube channel ID (format: ``UCxxxxxxxxxxxxxxxxxxxx``)
- **OAuth Token (JSON)** — The contents of the token file generated by the OAuth script

To generate the OAuth token:

.. code-block:: bash

   sudo docker exec -it imports_container python3 /app/python/import_scripts/youtube/obtain_oauth_token.py

Follow the on-screen prompts. The script will output a token JSON string — paste
that into the **OAuth Token (JSON)** field.

----

Echo / Chatwoot
----------------

Navigate to *Echo → Echo Tenants* and click **Add New**.

Required fields:

- **Tenant Name** — Human-readable label (e.g. "Support Team Echo")
- **Subdomain** — Your Chatwoot/Echo subdomain (e.g. ``myorg`` for ``myorg.chatwoot.com``)
- **API Key** — A Chatwoot user API access token (*stored encrypted*)

To find your API key: log in to Chatwoot → Profile → Access Token.

----

Disciple Tools
---------------

Disciple Tools data is ingested directly into Elasticsearch and does not use
the standard import pipeline. No asset configuration is required in the Asset Explorer.

Contact your Kavanah Media account manager for setup instructions specific to
your Disciple Tools instance.

----

Verifying Connections
---------------------

After creating your asset records, navigate to **Import Data** (``/assets/import-data``)
and check the **Data Source Status** table at the bottom of the page. Each configured
data source will show a status indicator:

- **Green / Configured** — The asset record is complete and credentials are present
- **Yellow / Partial** — Some required fields are missing
- **Grey / Not configured** — No asset record found for this platform

.. note::

   The status check confirms that credentials are *present*, not that they are *valid*.
   The first import will confirm whether authentication succeeds. Check the import log
   for authentication errors.
