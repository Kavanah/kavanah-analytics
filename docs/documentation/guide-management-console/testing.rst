.. _guide-management-console-testing:

============================
Testing & QA Console
============================

The **Testing** page (``/testing``) is internal QA tooling for the
Kavanah Analytics deployment itself — pytest, Locust load tests,
security regression scans, dashboard health checks, dependency
audits, and CI status. It's not part of the customer-facing platform;
end users won't typically see it.

.. note::

   Access is gated on the ``mc_testing`` permission. By default no
   role has this enabled; a superuser can grant it from User
   Management → Roles. **Triggering** any test (start pytest, run
   Locust, etc.) additionally requires ``asset_admin`` or
   ``superuser`` — viewing the page with only ``mc_testing`` will
   show the UI but POSTs will 403.

.. warning::

   The Locust load test target defaults to
   ``https://<your-staging-host>``. The target is
   configurable through the ``LOAD_TEST_TARGET_URL`` env var, but
   there's no UI selector — make sure the env var doesn't point at
   production before you click **Run Load Test**.

----

Page layout
===========

Five tabs across the top:

- **Regression Tests** — per-dashboard health checks and the pytest
  runner.
- **Dashboard Comparison** — cross-dashboard metric comparison
  (Summary vs Trends vs Maps) with date / org / team filters.
- **Load Testing** — Locust headless runs with live stats and log
  tail.
- **Visual Regression** — sanity-check pre-built dashboard
  iframes inline.
- **Security** — three sub-sections: CI Pipeline Status, Security
  Regression Tests (release-plan filter), and Dependency Audit
  (pip-audit).

----

Regression Tests
================

- **Run All Tests** — runs ``python3 -m pytest /app/tests/ -v
  --tb=short`` with a 180-second timeout. Output streams to a log file
  under ``/app/data/logs/pytest_*.log`` and the latest is shown in
  the panel.
- **Per-dashboard checks** — the table lists each dashboard
  with a **Re-run** button that loads the dashboard in a hidden
  iframe and captures up to five console errors. Stats: pass / fail /
  pending pills + overall progress bar.

----

Load Testing
============

Headless Locust run, no web UI exposed.

- **Users** (1–100, default 10), **Duration** (30 s – 15 min,
  default 60 s), **Spawn rate** (1–20 / sec, default 2).
- **Run Load Test** — kicks off Locust, writes the PID to
  ``/app/data/logs/locust.pid``, streams stdout to
  ``/app/data/logs/locust_*.log``.
- **Stop** — sends SIGTERM and polls until the process exits.
- The stats table shows req count, failures, and median / avg /
  p95 / p99 latencies per endpoint, with a searchable log tail
  underneath.

----

Security
========

Three blocks under the Security tab.

CI Pipeline Status
------------------

Polls the GitHub API for the most recent runs of OWASP ZAP, Trivy,
Semgrep, Gitleaks, and the Pytest workflow. Each row links to the
GitHub Actions page for that run.

Security Regression Tests
-------------------------

Runs ``pytest tests/flask/test_security.py`` (180-second timeout).

The **Release plan** dropdown filters by the
``@pytest.mark.release_plan(name)`` decorator on individual tests —
discovered automatically by scanning the test tree. Picking a plan
runs only the matching tests and merges in the most recent Playwright
JSON report from ``/app/data/logs/e2e/`` for end-to-end coverage.

If the Playwright report is more than 24 hours old, the UI flags it
as stale.

Dependency Audit
----------------

Runs ``python3 -m pip_audit --desc --format json`` (120-second
timeout) and renders the results — package, version, vulnerability
ID, fix versions, description.

----

What this page does *not* do
============================

- **Doesn't run live Playwright tests.** The Node toolchain isn't
  installed in the imports container. Playwright results are read
  from the most recent JSON report on disk.
- **Doesn't manage release plans.** Plans are discovered from the
  test code (``@pytest.mark.release_plan``) — there's no UI to
  create or edit them.
- **Doesn't gate releases.** This is a staging / inspection tool,
  not a CI gate. Production releases are governed by the GitHub
  Actions workflows the *CI Pipeline Status* block links to.
- **Doesn't run against production.** Or rather — it shouldn't.
  Verify ``LOAD_TEST_TARGET_URL`` before running Locust.

----

Related pages
=============

- :doc:`/management-console/elasticsearch-ops` — checking cluster
  health if a dashboard test fails because ES is unavailable.
- :doc:`/management-console/import-logs` — the Status Reports page
  for import-pipeline issues, distinct from this internal QA tool.
