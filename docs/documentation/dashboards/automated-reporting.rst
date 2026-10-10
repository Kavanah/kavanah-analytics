.. _automated-reporting:

====================
Automated Reporting
====================

.. note::

   Automated email / PDF reporting on a schedule is **not in v1.0**.
   It's tracked for **Phase 2** (target Q3 2026) per
   :doc:`/release-notes/release-roadmap`. This page is a placeholder
   that documents the planned shape of the feature so admins can plan
   accordingly.

----

Planned feature outline
-----------------------

When the feature ships, you'll be able to schedule a recurring email
report from the Management Console with these knobs:

- **Recipients** — one or more email addresses (typically a list of
  internal stakeholders or supporters).
- **Cadence** — weekly (day of week + time) or monthly (day of month
  + time).
- **Date range covered** — a rolling window: prior 7 days, prior 30
  days, prior month, or a custom rolling expression.
- **Charts to include** — pick from your saved Dashboard Builder
  dashboards (in particular Digital Impact Reports).
- **Format** — inline HTML email digest, PDF attachment, or a public
  URL to a published Impact Report.

Subscription tiers as advertised on the marketing pages:

.. list-table::
   :header-rows: 1
   :widths: 30 70

   * - Tier
     - Automated reporting
   * - Free
     - Not included
   * - Pro
     - Included — weekly email digests
   * - Enterprise
     - Included — custom schedule, PDF, priority delivery

----

What you can do today instead
-----------------------------

Until the scheduled-delivery feature ships, the closest alternatives:

1. **Public Impact Report URLs** *(shipped)* — publish a
   Digital Impact Report and include the link in your own newsletter
   tooling. The report data updates automatically every time someone
   opens it. See :doc:`/guide-dashboards/sharing-reports`.
2. **Manual Excel / PDF export** from the dashboard toolbar — see
   :doc:`/guide-dashboards/overview`.
3. **Browser-driven PDF** — use *Print* on a published Impact Report
   URL for a clean, paginated PDF.

For high-volume recurring reports during the interim, contact your
Kavanah Media account manager.
