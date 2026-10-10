.. _asset-explorer:

===============
Asset Explorer
===============

The **Asset Explorer** (``/assets/``) is the central management
interface for all data source configurations in Kavanah Analytics. It
uses a tree navigation on the left to browse asset categories, and a
content area on the right to view, add, edit, and delete assets.

Access is gated on the ``mc_asset_explorer`` permission (``asset_admin``
and ``superuser`` have it by default).

.. tip::

   For Facebook, Echo, and Chatwoot, the **Data Connectors** page
   (``mc_data_connectors``) provides a guided wizard that creates the
   underlying account / app / OAuth user chain step-by-step. Use it
   instead of clicking through the Asset Explorer when bootstrapping a
   new tenant. The Asset Explorer is still where you go to *edit*
   existing assets afterwards.

----

Asset Categories and Types
---------------------------

Assets are organised into the following categories:

**Company**

.. list-table::
   :header-rows: 1
   :widths: 30 70

   * - Asset type
     - Description
   * - Organisations
     - Top-level client groups. Every imported data record is tagged with an organisation.
   * - Teams
     - Subdivisions within an organisation.
   * - Regions
     - Geographic regions (for reporting segmentation).
   * - Countries
     - Countries (for geographic filtering).
   * - Cities
     - Cities (for geographic filtering).

**Facebook**

.. list-table::
   :header-rows: 1
   :widths: 30 70

   * - Asset type
     - Description
   * - Facebook Accounts
     - A Facebook Business Manager account.
   * - Facebook Apps
     - A Facebook developer app (stores App ID, App Secret, App Token).
   * - Facebook Ad Accounts
     - An ad account linked to an App User token (format: ``act_XXXXXXXXXX``).
   * - Facebook App Users
     - An OAuth user that grants the app access to ad accounts and pages.
   * - Facebook Pages
     - A Facebook Page to import organic reach data from.

**Messaging**

.. list-table::
   :header-rows: 1
   :widths: 30 70

   * - Asset type
     - Description
   * - Echo Tenants
     - A Chatwoot/Echo tenant (stores subdomain and API key).

**Google Analytics**

.. list-table::
   :header-rows: 1
   :widths: 30 70

   * - Asset type
     - Description
   * - Google Analytics Accounts
     - A Google Analytics account.
   * - Google Analytics Properties
     - A GA4 property (stores Property ID and service account JSON).

**Google Ads**

.. list-table::
   :header-rows: 1
   :widths: 30 70

   * - Asset type
     - Description
   * - Google Ads Customers
     - A Google Ads customer account (stores Customer ID, Developer Token, service account JSON).

**Instagram**

.. list-table::
   :header-rows: 1
   :widths: 30 70

   * - Asset type
     - Description
   * - Instagram Accounts
     - An Instagram Business Account linked to a Facebook App.

**YouTube**

.. list-table::
   :header-rows: 1
   :widths: 30 70

   * - Asset type
     - Description
   * - YouTube Channels
     - A YouTube channel (stores Channel ID and OAuth token JSON).

----

Adding an Asset
----------------

1. Navigate to the asset type in the tree (e.g. *Facebook → Facebook Apps*).
2. Click **Add New** in the top right.
3. Fill in the required fields (marked with a red asterisk).
4. Click **Save**.

**Field types:**

- **Text** — Free-text input
- **Select** — Dropdown populated from another asset type (relational)
- **Multi-select** — Multiple selections from another asset type
- **Password / Encrypted** — Stored encrypted using Fernet; displayed masked in the UI
- **Textarea** — Multi-line text (used for JSON keys)
- **Computed** — Read-only fields calculated from other fields (e.g. OAuth URL)

**Encrypted fields** include: Facebook App Secret, App Token, Google service account JSON,
Echo API keys, and YouTube OAuth tokens.

----

Editing an Asset
-----------------

1. Navigate to the asset record (via the tree or search).
2. Click **Edit**.
3. Make changes and click **Save**.

Changes to credentials take effect on the next import run — there is no need to
restart any service.

.. note::

   Changing an Organisation or Team name after data has been imported will cause
   a mismatch between the new name and existing Elasticsearch documents. The
   ``organization_title`` field in existing documents will not be updated automatically.
   Only new imports will use the updated name.

----

Deleting an Asset
------------------

**Single delete:** Navigate to the asset, click **Delete**, and confirm.

**Bulk delete:** From the asset list page, check the records you want to delete
and click **Bulk Delete**.

.. warning::

   Deleting an asset does **not** delete the imported data in Elasticsearch.
   To remove data, you must delete the relevant documents from Elasticsearch directly.
   Deleting a parent asset (e.g. a Facebook App) does not cascade-delete child assets
   (e.g. Facebook Ad Accounts linked to that app).

----

CSV Import
----------

You can bulk-import assets from a CSV file.

1. Navigate to the asset list for the type you want to import.
2. Click **Import CSV**.
3. Select your CSV file (UTF-8 or UTF-8 BOM encoding).
4. The system will validate the file and show a preview.
5. Click **Confirm Import**.

CSV files must have a header row. Required columns vary by asset type but always
include the asset's required fields. Download the template from the import dialog
to see the correct column names.

**Notes:**
- Existing records with matching unique fields will be updated, not duplicated.
- Encrypted fields (API keys, secrets) can be included in CSV but will be stored encrypted.
- Maximum 1,000 rows per CSV import.

----

Relational Links
-----------------

Many asset types reference other assets via a ``store_esid`` link. For example:

- A **Facebook Ad Account** links to a **Facebook App User** via ``app_user_esid``
- A **Team** links to an **Organisation** via ``organization_esid``
- A **Facebook App** links to a **Facebook Account** via ``account_esid``

When creating relational assets, create the parent first, then the child.
Relational select fields are populated from the parent asset index automatically.

----

Dashboard Reordering
---------------------

ECharts dashboards appear in a menu on the Dashboards page. To change the order:

1. Navigate to **Dashboards → ECharts Dashboards** in the tree.
2. Drag the dashboard records to your preferred order using the drag handle.
3. The ``menu_position`` field is updated automatically.

The new order takes effect immediately on the Dashboards page.

----

Global Search
--------------

Use the search bar at the top of the Asset Explorer to find assets across all types.
Results are grouped by asset type. Click a result to navigate directly to that asset.
