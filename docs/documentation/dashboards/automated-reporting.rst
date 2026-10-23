.. _automated-reporting:

====================
Automated Reporting
====================

The **Report Scheduler** runs your reports on a schedule. It can do two
things:

- **Scheduled imports** pull fresh data from a connector for the teams
  you choose, then save a snapshot of a dashboard for that period.
- **Scheduled emails** send one or more dashboards to a list of
  recipients as a summary with a link, inline tables, or an Excel or
  PDF attachment.

Scheduled reporting is included on every subscription tier. See
*Subscription Tiers* on the :doc:`/index` page.

----

Opening the Report Scheduler
----------------------------

Select **Report Scheduler** (the clock icon) in the sidebar, or go to
``/report-scheduler/``.

The Report Scheduler is available to organization owners and admins.
Other users see *"The Report Scheduler is available to organization
owners and admins."*

The page has two lists: **Scheduled imports** and **Scheduled emails**.

----

Scheduling an import
--------------------

1. Under **1. Choose organizations & teams**, check the teams to include
   (**Select all** / **Clear all** are there for convenience).
2. Under **2. Pick a connector to schedule**, click a connector tile.
   Each tile shows how many of the selected teams use it. If none show,
   set up a connector on the **Data Connectors** page first.
3. Fill in the form and click **Save schedule**:

   - **Report name** (required) and **Description**.
   - **Scope** — the teams this schedule covers.
   - **Dashboard to report on** — one of the connector's dashboards, or
     **No report (import only)** to refresh the data without a snapshot.
   - **Frequency** — *Daily*, *Weekly*, *Every 2 weeks*, or *Monthly*,
     with **Day of week** or **Day of month** (1–28) as needed. A daily
     schedule also has **Days to pull** (1–7, default 1).
   - **Time** and **Timezone** — defaults to 06:00 in your profile's
     timezone.
   - **Save a snapshot for dashboard viewing** and **Number to keep**
     (1–100, default 7). Older snapshots are removed as new ones are
     saved.

The period each run covers follows the frequency: a daily run covers
the previous *N* days, a weekly run the previous calendar week, an
every-2-weeks run the previous 14 days, and a monthly run the previous
calendar month.

When a scheduled import runs, it imports the connector's data for the
selected teams first and then builds the snapshot. On the dashboard,
the **Reports** dialog lists the saved snapshots and applies a
snapshot's date range when you pick it.

----

Scheduling an email
-------------------

Click **New email schedule** and fill in:

- **Report name** (required) and **Description**.
- **Dashboards to email** (required) — up to 10. The email includes a
  link to each.
- **Teams** — all your teams are included by default.
- **Frequency**, **Day**, **Time** and **Timezone** — as for imports.
- **Date range** — *Previous 7 days* (default), *Previous 14 days*,
  *Previous 30 days*, *Previous calendar week*, *Previous calendar
  month*, or *Month to date*.
- **Recipients (comma-separated)** (required).
- **Format**:

  - *Summary + link* (default) — a short summary with a **View the full
    report** link to a saved snapshot.
  - *Inline HTML* — the report's tables in the email body.
  - *Excel attachment* or *PDF attachment* — the tables as a file. A
    report that contains only charts cannot be attached; the email says
    so instead.

- **Subject template** — defaults to
  ``{{report_name}} — {{date_range}}``.

Scheduled emails send the data already in Kavanah Analytics; they do
not run an import first. Pair one with a scheduled import if you want
the data refreshed just before it goes out.

Recipients outside your workspace
~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~

People in your workspace always receive the email. An address outside
your workspace first gets a *"Confirm your address to receive scheduled
reports"* email with a **Confirm this address** link, and receives
reports only after confirming. The link expires in 7 days; saving the
schedule again sends a fresh one. When you save, the form lists any
outside recipients who have not confirmed yet.

----

Managing schedules
------------------

Each import card shows its **Cadence**, **Last run**, **Next run**,
**Delivery**, **Scope** and **Connector**, with these actions:

- **Run now** — runs the schedule immediately, even if it is paused.
  Progress shows on the card, with a **Cancel** link to stop the
  in-progress import.
- **History** — the **Report history** of past runs, filterable by
  date. **View** opens a snapshot; **CSV** and **Excel** export its
  tables.
- **Errors** — shown when the last run had errors.
- **Edit**, **Pause** / **Resume**, and **Delete**. Deleting a schedule
  keeps its past snapshots.
- **Backfill** — imports a historical date range once (up to 365 days,
  defaulting to the last 30). For Meta, **Overwrite existing data**
  replaces what is stored, and a backfill that hits Meta's rate limit
  stops gracefully, keeping what it imported; run it again to continue.

Email cards show **Cadence**, **Date range**, **Teams**, **Recipients**,
**Last run** and **Next run**, with **Edit**, **Pause** / **Resume**
and **Delete**.

If a run is skipped because the subscription is inactive, the
**Errors** list says *"Your subscription is inactive — re-subscribe
under Billing to resume imports."*

----

Other ways to share reports
---------------------------

- **Public Impact Report URLs** — publish a Digital Impact Report to a
  public link that always shows current data. See
  :doc:`/guide-dashboards/sharing-reports`.
- **Excel / PDF export** from the dashboard toolbar — see
  :doc:`/guide-dashboards/overview`.
