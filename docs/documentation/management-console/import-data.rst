.. _import-data:

===========
Import Data
===========

The **Import Data** page (``/assets/import-data``) lets you trigger on-demand data
imports from any of your connected platforms. You can monitor progress in real time
and manage import history.

.. note::

   Import Data is gated on the ``mc_import_data`` permission.
   ``asset_admin`` and ``superuser`` have it by default; a superuser
   can grant it to any custom role from User Management → Roles. Polling
   endpoints for the live log accept any authenticated session, so a
   user can watch a running import even if they can't trigger one
   themselves.

----

Running an Import
-----------------

1. Navigate to **Import Data** at ``/assets/import-data``.

2. In the **Select Data Sources** panel, check one or more platforms to import:
   - Facebook Ads
   - Facebook Pages
   - Echo
   - Chatwoot
   - Google Ads
   - Google Analytics
   - Instagram
   - YouTube

3. Set the **Date Range**:

   **Rolling (days):** Select a number of days from today (1–365).
   For example, "Last 7 days" imports data from 7 days ago to today.

   **Custom range:** Enter a start date and end date in ``YYYY-MM-DD`` format.
   The maximum custom range is **365 days**.

4. **Options:**

   - *Delete cached data before import* — Removes recently-imported JSON files
     before fetching. Use this to force a clean re-fetch if you suspect cached
     data is incomplete or corrupted. Does not delete data already indexed in Elasticsearch.
   - *Delete today's log before import* — Clears the log file for today's date
     so the new run starts with a clean log.

5. Click **Run Import**.

----

Monitoring Progress
-------------------

Once an import starts, the **Live Log** panel appears below the form. The log
updates every few seconds, showing lines like::

   [2026-03-16 10:23:01] INFO Starting facebook_ads import for 7 days
   [2026-03-16 10:23:02] INFO Fetching ad insights for account act_123456789
   [2026-03-16 10:23:05] INFO Indexed 147 documents into fb_insights_acmecorp
   [2026-03-16 10:23:05] INFO facebook_ads import complete

The log tails from the last read position, so refreshing the page does not
cause duplicate log entries.

A successful import ends with a line confirming the number of documents indexed.
An error will appear as an ``ERROR`` or ``EXCEPTION`` line.

----

Import Statuses
---------------

The **Data Source Status** table at the bottom of the page shows the state of
each platform's last import attempt:

.. list-table::
   :header-rows: 1
   :widths: 20 80

   * - Status
     - Meaning
   * - Configured
     - Asset records exist and credentials are present. Ready to import.
   * - Running
     - An import is currently in progress for this platform.
   * - Success
     - Last import completed without errors.
   * - Error
     - Last import encountered an error. Check the import log for details.
   * - Not configured
     - No asset records found for this platform. See :doc:`../getting-started/connecting-data-sources`.

----

Cancelling an Import
--------------------

To cancel a running import, click the **Cancel** button that appears next to the
live log while an import is in progress. The import will stop after the current
batch of documents finishes indexing.

.. note::

   Cancelling an import does not roll back documents that have already been indexed.
   If you need to revert, re-run the import with *Delete cached data* checked, or
   contact your administrator to delete the affected index documents.

----

Import Frequency Recommendations
----------------------------------

.. list-table::
   :header-rows: 1
   :widths: 30 70

   * - Platform
     - Recommended frequency
   * - Facebook Ads
     - Daily (data is finalised ~24 hours after the ad run date)
   * - Google Ads
     - Daily
   * - Google Analytics
     - Weekly (session data is stable within 48 hours)
   * - Facebook Pages / Instagram
     - Weekly
   * - YouTube
     - Weekly
   * - Echo / Chatwoot
     - Daily or real-time via webhook *(webhook support coming in GA)*

Running an import more frequently than needed does not cause data loss — existing
documents are matched by date and updated, not duplicated.

----

Import Date Range Tips
-----------------------

- **New setup:** Run a 90–180 day import on first setup to populate historical data.
- **Ongoing:** Run daily with "Last 1 day" or "Last 3 days" to catch any updates.
- **Backfill:** Use a custom date range to fill in gaps. Maximum range is 365 days per run.
- **Reprocessing:** Check *Delete cached data* when re-importing a range you have already
  imported, to ensure fresh data is fetched from the platform API.

----

Viewing Past Imports
--------------------

All import activity is recorded in daily log files. Navigate to **Import Logs**
(``/assets/logs``) to view, download, or delete log files. See :doc:`import-logs`
for details.
