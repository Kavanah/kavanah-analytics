.. _guide-management-console-status-reports:

==============
Status Reports
==============

The **Status Reports** page (``/assets/status-reports``) is your
daily-health screen for the import pipeline. It surfaces the parsed
errors from the most recent import run plus a per-platform summary
table (records imported, errors, runtime), so you can spot a broken
connector or a silent zero-records run at a glance.

.. note::

   Access is gated on the ``mc_import_status_reports`` permission.
   ``asset_admin`` and ``superuser`` have it by default; superusers
   can grant it to any custom role from User Management → Roles.

----

Page layout
===========

Two sections, top to bottom:

1. **Errors panel** — heading *"Status Reports"*, the help line
   *"View errors from the most recent import log."*, a metadata
   bar (``Log File: …  |  Date: YYYY-MM-DD  |  Errors: N``), and a
   dark scrollable container showing the parsed error lines.
2. **Import Summary** — a per-platform table with a date selector
   above it that lets you jump back through the last 7 days of
   import logs.

----

The errors panel
================

Each error line in the panel is the raw text from
``/app/data/logs/imports_<date>.log``, with three kinds of inline
highlighting to make scanning faster:

- **Error** keyword → red
- Numeric IDs of 6+ digits (account IDs, asset IDs) → orange
- Library / module tags like ``[fb-run-daily-imports]`` → blue

The **Errors:** counter at the top of the page is the count of red
lines. Zero errors renders a green *"No errors reported"* banner
inside the container.

A **"Save As…"** button in the top-right downloads the visible error
text as ``errors-<date>.txt`` — useful for sharing with Kavanah Media
support or attaching to a.

----

The Import Summary
==================

The summary table answers *"did each platform actually run, and
roughly how did it go?"*

Columns:

.. list-table::
   :header-rows: 1
   :widths: 22 22 18 38

   * - Column
     - What it shows
     - Empty value
     - When to investigate
   * - **Data Source**
     - Platform name
     - —
     - Greyed-out row means *did not run* — confirm the cron entry
       or scheduler is healthy if you expected this source to fire.
   * - **Records Imported**
     - Integer count for the day
     - ``—`` (didn't run) or italic *"No data"* (ran but zero rows)
     - Zero records on a platform you know is busy — credentials
       expired or scope is wrong.
   * - **Errors**
     - Error count, red & bold if > 0
     - ``—``
     - Anything > 0; click into the **errors panel** above to read
       the actual lines.
   * - **Run Time**
     - ``Xm YYs``
     - ``—``
     - Sudden jumps from minutes to hours suggest the API is rate-
       limiting or paginating differently than expected.

A **Totals** row at the foot of the table sums the day across all
sources.

Choosing a different day
------------------------

The date dropdown above the table lets you switch between the last
seven days of import runs. Logs older than 7 days are deleted
automatically each time the page loads, so you'll only see dates
that still have data on disk.

When you pick a different date the page reloads with that date as
the ``?date=YYYY-MM-DD`` query param.

----

Status indicators at a glance
=============================

.. list-table::
   :header-rows: 1
   :widths: 30 70

   * - You see…
     - It means…
   * - Greyed-out row in the summary
     - That source didn't run today — not necessarily an error, but
       expected sources should always run.
   * - Red, bold *"Errors"* count
     - One or more lines in the log were classified as errors. Open
       the panel above and read them — most are obvious (auth fail,
       rate limit, missing field).
   * - Italic *"No data"* in **Records Imported**
     - The import ran without crashing but indexed zero documents.
       Common causes: an empty date range, an account with no
       campaigns in the period, or a scope-stripped OAuth token.
   * - Green *"No errors reported"*
     - All-clear for the selected date.
   * - *"No log files found"*
     - The day's log was deleted (older than 7 days) or the import
       script never started. Check the imports container and cron.

----

Common false positives
======================

A few log patterns that *look* like errors aren't counted as such:

- *"Index does not exist, creating…"* — the importer is creating a
  fresh per-org index. Informational.
- *"Processed: N. Errors: 0"* — the import wrap-up summary line.
  Even though it contains the word *Errors*, the colon-zero pattern
  is recognised and skipped.

If you spot a pattern that's being miscounted, file a feedback ticket
so the parser can be tuned.

----

What this page is *not*
=======================

- **Not real-time.** The page renders from saved log files; there's
  no live tail. To watch a running import, use the Import Data page
  (``/assets/import-data``) where the **Live Log** panel updates in
  place.
- **Not a retry surface.** You can read the errors here but can't
  re-run a failed import from this page. Re-trigger from Import Data.
- **Not per-record.** Counts are aggregate; there's no row-level
  breakdown.
- **Not org/team-filtered.** The page shows the full import run for
  the deployment, regardless of which org or team you're scoped to.
- **Not tracked over time.** No trend chart of errors / runtime
  across days; pick a different date to compare manually.

----

Common workflows
================

"My morning health check"
   Open the page, glance at the summary table for the current date,
   confirm every expected source ran with zero errors. If anything's
   red or greyed-out, drill in.

"Investigating yesterday's missing reach data"
   Pick yesterday's date from the dropdown. Look at the row for
   the relevant platform — *"No data"* points at credentials /
   scope; greyed-out points at scheduling; an error count points at
   the log panel.

"Filing a support ticket"
   Pick the affected date, click **Save As…** to download the
   error text, attach the file to your along with the
   summary table screenshot.

----

Related pages
=============

- :doc:`/management-console/import-data` — re-trigger a failed
  import or backfill a missing range.
- :doc:`/management-console/import-logs` — full log files (not
  just the parsed errors), with download and retention notes.
- :doc:`/management-console/elasticsearch-ops` — verify cluster
  health if a platform's documents aren't appearing.
