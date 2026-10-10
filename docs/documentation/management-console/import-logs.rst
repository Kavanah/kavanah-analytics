.. _import-logs:

============
Import Logs
============

The **Import Logs** page (``/assets/logs``) provides access to all
import log files. Each log file covers one calendar day and records
every import operation run on that day.

Access is gated on the ``mc_import_logs`` permission. ``asset_admin``
and ``superuser`` have it by default.

----

Viewing Logs
------------

Navigate to **Import Logs** at ``/assets/logs``. The page lists all available log files,
sorted by date (most recent first).

Each entry shows:

- **Date** — The calendar date the log covers (format: ``imports_YYYYMMDD.log``)
- **Size** — File size in kilobytes
- **Actions** — View, Download, Delete

Click **View** to open the log file inline. The full log content is displayed in a
scrollable, monospace text area.

Log lines follow this format::

   [YYYY-MM-DD HH:MM:SS] LEVEL  message

Log levels:

- ``INFO`` — Normal progress messages
- ``WARNING`` — Non-fatal issues (e.g. a single document failed to index)
- ``ERROR`` — A step failed; the import may have stopped early
- ``EXCEPTION`` — An unhandled exception occurred; full stack trace is included

----

Downloading Logs
----------------

Click **Download** next to a log entry to download the file as a ``.log`` text file.
This is useful for sharing with Kavanah Media support or archiving for compliance.

----

Deleting Logs
-------------

Click **Delete** next to a log entry to permanently delete that day's log file.
You will be asked to confirm before deletion.

.. warning::

   Deleted log files cannot be recovered. Only delete logs you are certain you
   no longer need. Kavanah Media recommends retaining logs for at least 30 days.

----

Status Reports
--------------

The **Status Reports** page (``/assets/status-reports``) provides a high-level
summary of system health without requiring you to read raw log files.

The status report shows:

- **Data source connectivity** — Whether each platform's credentials are valid and reachable
- **Last successful import** — Date and time of the most recent import for each platform
- **Document counts** — Number of documents indexed per platform index
- **Index health** — Elasticsearch index status (green / yellow / red)

Use Status Reports as a daily health check. Drill down to Import Logs when
a status indicator shows a problem.

----

Searching Within a Log
----------------------

Because log files are displayed as plain text in the browser, use your browser's
built-in search (``Ctrl+F`` / ``Cmd+F``) to search for specific terms within a log,
such as an error message, a campaign name, or a specific timestamp.

----

Log Retention Policy
---------------------

Log files accumulate indefinitely. As a best practice:

- Delete logs older than 90 days on a regular basis, or
- Download logs you want to archive, then delete them from the server to free space

There is no automatic log rotation or expiry in the current beta release.
Automated log retention policies are planned for a future release.
