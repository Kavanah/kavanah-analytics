.. _guide-dashboards-sharing-reports:

=====================================
Sharing Digital Impact Reports
=====================================

You can share a Digital Impact Report with supporters, partners, or donors
who don't have a Kavanah Analytics login. Kavanah gives each report a
unique, randomly-generated URL that anyone can open — but only shows data
for the organization or team the report was built for.

This is useful for:

- Including a live report link in a newsletter or fundraising appeal
- Sending a board member a link they can open on their phone
- Embedding a report in a supporter update email

Who can publish
---------------

Publishing is available on any dashboard that:

- Lives in the **Reports** menu
- Contains a **Digital Impact Report** chart (Facebook, Google, or
  Combined)
- Has an organization or team assigned in its properties

If any of those conditions isn't met, the Publish button won't appear on
the dashboard's toolbar.

Publishing a report
-------------------

1. Open the dashboards page and click the report you want to share.
2. On the toolbar at the top-right, click **Publish** (next to the
   Export button).
3. In the dialog, click **Generate Link**. Kavanah creates a unique URL
   you can copy.
4. Click **Copy** to copy the URL to your clipboard, then paste it
   wherever you're sharing it (email, newsletter, messaging app).

Anyone who opens the URL will see the report with a time-range selector
at the top. They do not need a Kavanah account.

Changing the time range on a shared report
------------------------------------------

People who open a shared report URL can change the time range at the top
of the page — Last 7 days, Last 30 days, Last 90 days, Last 6 months,
Last 12 months, or a custom date range. The charts update to reflect the new
range.

The report always shows data for the organization or team it was built
for — changing the time range doesn't change the scope of the data.

Regenerating the link
---------------------

If you ever need to invalidate a link you've shared (for example,
you realize you sent it to the wrong group), you can rotate the URL:

1. Open the dashboard again
2. Click **Publish** — the dialog shows the current URL
3. Click **Regenerate**, then confirm

Kavanah issues a brand-new URL. The previous URL stops working
immediately — anyone who had it will see a "This report is not
available" page.

Removing a shared link
----------------------

When you no longer want the report to be publicly accessible:

1. Open the dashboard
2. Click **Publish**
3. Click **Remove Link**, then confirm

The URL is revoked. Anyone who had the link will see the "This report is
not available" page when they try to open it.

You can always publish the report again later — it'll get a fresh URL.

What gets shared
----------------

The shared report contains:

- The same charts as the authenticated dashboard (KPIs, funnel, geo
  map, trend lines)
- A time-range selector at the top
- A minimal header with the report title

It does **not** include:

- Any login prompt or Kavanah navigation chrome
- Any way to navigate to other reports or dashboards
- Data from organizations or teams other than the one the report is
  scoped to

Frequently asked questions
--------------------------

**Is the URL guessable?**
   No. Each URL contains a 32-byte random token (over 256 bits of
   entropy) — there are vastly more possible URLs than atoms in the
   observable universe, so brute-forcing one is not feasible.

**Can I password-protect a shared link?**
   Not at this time. If you need access control, you currently have two
   options: (1) keep the report unpublished and share screenshots
   instead, or (2) publish the link to a small known audience and
   regenerate/remove it when you're done.

**Is the link indexed by search engines?**
   No. The public viewer sends a ``noindex`` directive so Google and
   other search engines don't crawl it. That said, treat these links as
   unlisted rather than truly secret — don't post them on public
   websites.

**How do I know who has opened a shared report?**
   Access tracking isn't enabled by default. If you need it, ask your
   administrator.

**Do regenerated links share data with the old link?**
   Yes — they show the exact same data. Regenerating only changes the
   URL itself, not the underlying report or its scope.
