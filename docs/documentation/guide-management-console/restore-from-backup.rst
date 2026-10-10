.. _guide-management-console-restore-from-backup:

====================
Restore from Backup
====================

The **Restore from Backup** page is where you recover from accidental
deletions, bad config changes, and full-disaster scenarios. It bundles
four restore paths under one Management Console page, ordered roughly
from least to most destructive:

1. **ES Snapshots** — selectively restore one or more indices from a
   native Elasticsearch snapshot. No downtime.
2. **Local Asset Backups** — replace the ``de_asset_*`` indices from a
   tarball you can create on demand. Affects asset configs only.
3. **S3 Full Data Backups** — replace the entire Elasticsearch data
   directory from an S3-archived backup. Several minutes of downtime,
   but recovers from total data loss.
4. **S3 Config Backups** — preview and selectively apply files from a
   stored config snapshot (docker-compose, ES/Kibana settings,
   scripts) to ``/app/reporting_configs`` on disk.

.. note::

   Access is gated on the ``mc_restore_backup`` permission. Granted to
   ``superuser`` and ``asset_admin`` by default; superusers can grant
   it to any custom role from User Management → Roles.

.. warning::

   Every restore on this page is destructive in one way or another.
   Read the on-screen confirmation dialog carefully before clicking
   the orange / red button. Where possible, take a fresh **Local
   Asset Backup** before a Local or Snapshot restore as a safety net.

----

Page layout
===========

The page renders four cards stacked vertically, each with an icon, a
short description, and the controls for that restore path. A status
banner at the top shows progress and success / error messages while
operations are running.

----

ES Snapshots — restore individual indices
=========================================

Use this for the most common case: a single ``de_asset_*`` index got
into a bad state (mis-imported CSV, wrong bulk delete) and you want
to roll just that index back without touching anything else. No
downtime.

Browse and inspect a snapshot
-----------------------------

1. Pick a repository from the **Repository** dropdown. Repositories
   are populated automatically — typically you'll see ``daily_repo``
   (the SLM-managed daily snapshots) and any one-off repos like
   ``migration_repo``.
2. The snapshots in that repo show in a table: name, **State**
   (``SUCCESS`` / ``FAILED`` / ``IN_PROGRESS``), start time, and
   index count. Click **Inspect** on any successful snapshot.
3. The inspect panel lists every index in the snapshot and tells you,
   for each one, whether it already exists in the cluster (so a
   restore would *replace* it) or doesn't (so a restore would
   *create* it from scratch).

   Use the **Show:** dropdown to narrow the list — *All*, *Only
   existing*, or *Only new* — and the **Select visible** /
   **Deselect visible** buttons for bulk toggling.

.. note::

   Some repos accumulate broken snapshots over time. The UI shows a
   warning banner like *"3 broken snapshot(s) skipped"* with the
   names. Have a self-hosted admin run
   ``POST /_snapshot/<repo>/_cleanup`` on the server to clear them.

Restore the selected indices
----------------------------

1. Tick the indices you want to restore.
2. Click **Restore selected indices**.
3. The confirmation dialog spells out exactly what's about to
   happen — how many indices already exist (red, *will be deleted
   and recreated*) and how many are fresh (green). Click **Restore
   selected** to proceed.

Behind the scenes, the restore runs with
``include_global_state=false`` (cluster settings aren't touched) and
``include_aliases=true`` (any aliases on the snapshot indices are
restored).

----

Local Asset Backups — quick safety net
======================================

Local Asset Backups are tarballed JSON dumps of every ``de_asset_*``
index (teams, accounts, integrations, dashboards). They live in
``/app/data/backups/`` on the imports container and are **separate
from** the daily ES snapshots — handy as a quick fail-safe before a
risky bulk edit in the Asset Explorer.

The page keeps the **5 most recent** backups; older ones are
auto-pruned each time you create a new one.

Create a backup
---------------

Click **Create backup now** at the top of the section. The page
generates a fresh ``asset-backup-<timestamp>.tar.gz``, prunes old
files, and refreshes the list.

Restore a backup
----------------

1. Find the backup you want in the table (filename / date / size).
2. Click **Restore** on that row.
3. Confirm the dialog: *"This will delete and recreate all
   de_asset* indices from backup '...'. This cannot be undone.
   Continue?"*
4. The status banner shows *"Restoring asset indices from
   '...'..."* until the operation finishes. The result is a list of
   restored index names.

What's affected:

- All ``de_asset_*`` indices are dropped and rebuilt from the backup.
- **Not affected**: user accounts, MFA settings, dashboard data
  indices (``*_fb_insights``, etc.), Kibana saved objects.

Use this for: rolling back asset edits, recovering from a bad CSV
import, restoring a deleted org/team without rolling back actual
analytics data.

----

S3 Full Data Backups — disaster recovery
========================================

This is the *"the box is gone, we're rebuilding from the AWS backup
bucket"* path. It downloads a full Elasticsearch data-directory
tarball from cross-account S3, **stops Elasticsearch**, swaps out the
entire ``/es-data`` directory, and restarts.

.. danger::

   Several minutes of downtime. Every index in the cluster is
   replaced — assets, users, dashboard configs, analytics data, MFA
   records, audit log, everything. Use only when the local SLM
   snapshots are also gone.

How it works
------------

1. Pick a backup from the table. Filenames look like
   ``es-data-<timestamp>.tar.gz``; size and date are shown.
2. Click **Download & Restore**.
3. The confirmation dialog spells out the steps:
   download → stop ES → move ``/es-data`` to ``/es-data.bak`` →
   extract the tarball → restart ES → wait for cluster health.
4. Click **Restore from S3**. The status banner sticks to *"Full S3
   restore in progress... Do not close this page"* for the duration.
5. On success the result shows the backup file, the rollback path
   (``/es-data.bak``), and the cluster health.

If Elasticsearch doesn't reach a healthy state within 120 seconds,
the UI flags it ("started but health check timed out — verify
manually"). The restore is still considered complete; ask a
self-hosted admin to verify with ``GET /_cluster/health`` from the
container.

If anything goes wrong mid-restore, the script attempts to swap the
old ``/es-data.bak`` directory back into place automatically. If
that recovery also fails, the original data still lives at
``/es-data.bak`` — a self-hosted admin can put it back by hand.

----

S3 Config Backups — restore platform settings
=============================================

S3 Config Backups capture the ELK platform configuration:
``docker-compose.yml``, ``elk/elasticsearch.yml``,
``elk/kibana.yml``, the import scripts, crontabs, and system info.
They're for restoring the **plumbing**, not the data.

This is a two-step flow. You download a backup as a "config
extraction" first, browse it, then selectively apply files to the
live ``/app/reporting_configs`` directory on the host.

Step 1 — Download and extract
-----------------------------

1. Find the backup you want and click **Download & Extract**.
2. The page downloads the tarball from S3 and unpacks it into
   ``/app/data/s3-config-restore/<extraction-name>/``.
3. A file tree appears with one row per file: relative path, size,
   and a **Live status** badge:

   - **new on disk** *(green)* — the file doesn't exist in
     ``/app/reporting_configs``
   - **identical** *(grey)* — the file matches the live version
     byte-for-byte
   - **differs from live** *(red)* — the file exists but has
     different contents

Step 2 — Apply selected files
-----------------------------

1. Tick the files you want to apply. Use **Only differing** to skip
   unchanged files; **Select all** / **Deselect all** for bulk
   toggling.
2. Click **Apply selected files to disk**.
3. The confirmation dialog tells you how many files will overwrite
   existing versions. Click **Apply files**.

For every overwrite, the previous version is copied first to
``/app/data/s3-config-restore/.applied-backup/<extraction>_<timestamp>/``
so you can roll back manually if a config change goes wrong.

.. note::

   Most config changes don't take effect until the affected
   container or service is restarted. Restoring
   ``docker-compose.yml`` or ``elk/elasticsearch.yml`` typically
   requires a ``docker compose up -d`` or full container recreate;
   restoring import scripts or crontabs takes effect on the next
   scheduled run.

----

What each path restores at a glance
===================================

.. list-table::
   :header-rows: 1
   :widths: 22 38 40

   * - Path
     - What it touches
     - Downtime
   * - ES Snapshot (selective)
     - The exact indices you tick — assets, dashboards, analytics
       data, anything in the snapshot.
     - None
   * - Local Asset Backup
     - Every ``de_asset_*`` index (rebuilt from JSON dump).
     - None — operation is fast
   * - S3 Full Data Backup
     - The entire Elasticsearch data directory — every index in the
       cluster.
     - Several minutes
   * - S3 Config Backup
     - Files under ``/app/reporting_configs`` on the host disk.
       No ES indices touched.
     - None at restore time; service-specific restart later

----

Common workflows
================

"I just imported a bad CSV — undo it"
   Use **Local Asset Backups**. If you took one before the import,
   restore it. If you didn't, switch to **ES Snapshots** and restore
   the affected ``de_asset_*`` index from the most recent daily
   snapshot.

"A teammate deleted the wrong organisation"
   Same recipe — Local Asset Backup or ES Snapshot of the
   ``de_asset_organization`` and ``de_asset_teams`` indices.

"The VPS died and we're rebuilding on a new host"
   Restore from **S3 Full Data Backup** first to get every index back
   in one go, then **S3 Config Backup** if you also need to recover
   docker-compose / Kibana / ES settings.

"Someone changed elasticsearch.yml and broke ingest"
   Use **S3 Config Backups** to grab a known-good extraction, then
   apply just ``elk/elasticsearch.yml`` (the row will show *"differs
   from live"*) and ask a self-hosted admin to recreate the ES
   container.

----

Related pages
=============

- :doc:`/management-console/backup-restore` — reference for the
  legacy ``/api/backup/*`` tarball API the Local Asset Backups
  section uses.
- :doc:`/management-console/elasticsearch-ops` — index naming, the
  daily SLM snapshot policy, and how to verify cluster health after
  a restore.
