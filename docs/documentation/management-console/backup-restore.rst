.. _backup-restore:

================
Backup & Restore
================

Kavanah Analytics has two backup-and-restore surfaces today:

1. The **legacy Asset Explorer backup buttons** described on this page
   create and restore a single tarball containing every
   ``de_asset_*`` index. Useful for scripts and disaster recovery, gated
   on ``asset_admin`` / ``superuser``.
2. The **Restore from Backup** Management Console page (gated on the
   ``mc_restore_backup`` permission) provides a richer UI: list local
   backups, list and restore S3-based Elasticsearch snapshots, browse
   config extractions, and apply individual config files. It's the
   primary operator interface today; the legacy tarball flow described
   here is what powers it under the hood.

Both let you recover from accidental deletions, data corruption, or
container rebuild.

----

What Is Backed Up
------------------

A backup includes **all ``de_asset_*`` Elasticsearch indices**, specifically:

- All organisations, teams, regions, countries, cities
- All Facebook, Google, Echo, Instagram, YouTube asset records (including encrypted credentials)
- All Kibana dashboard references and visibility settings
- All ECharts Dashboard Builder configurations
- All per-user MFA settings

**What is NOT backed up:**

- Imported analytics data (``*fb_insights``, ``*google_ads``, etc.) — this data can be
  re-imported from the source platforms
- Elasticsearch security users and roles — manage these separately in Elasticsearch
- Kibana saved objects — manage these in Kibana → Stack Management → Saved Objects

----

Creating a Backup
------------------

**Via the UI:**

Navigate to the Asset Explorer (``/assets/``) and click **Create Backup** in the
top navigation.

The backup will be created immediately and saved to:

.. code-block:: text

   /app/data/backups/asset-backup-YYYYMMDD.tar.gz

If multiple backups are created on the same day, a timestamp suffix is added.

**Via the API:**

.. code-block:: bash

   curl -X POST "https://<your-domain>/assets/api/backup/create" \
     -H "Cookie: session=<your_session_cookie>" \
     -H "X-CSRFToken: <csrf_token>"

Response:

.. code-block:: json

   { "ok": true, "filename": "asset-backup-20260316.tar.gz" }

----

Listing Backups
----------------

**Via the UI:**

Navigate to the Asset Explorer and click **Backups**. The list shows all available
backup files with their date, size, and a Restore button.

**Via the API:**

.. code-block:: bash

   curl -X GET "https://<your-domain>/assets/api/backup/list" \
     -H "Cookie: session=<your_session_cookie>"

Response:

.. code-block:: json

   {
     "backups": [
       { "filename": "asset-backup-20260316.tar.gz", "size_kb": 142, "created": "2026-03-16" },
       { "filename": "asset-backup-20260315.tar.gz", "size_kb": 138, "created": "2026-03-15" }
     ]
   }

----

Restoring a Backup
-------------------

.. warning::

   Restoring a backup **drops and recreates all ``de_asset_*`` indices** from the
   archive. All current asset data will be replaced with the backup's contents.
   This operation **cannot be undone**. Only use restore for disaster recovery.

   Before restoring, consider creating a new backup first to preserve the current state.

**Via the UI:**

1. Navigate to the Asset Explorer and click **Backups**.
2. Find the backup you want to restore.
3. Click **Restore**.
4. Confirm the warning dialog.
5. The restore will run in the background. The page will refresh when complete.

**Via the API:**

.. code-block:: bash

   curl -X POST "https://<your-domain>/assets/api/backup/restore" \
     -H "Content-Type: application/json" \
     -H "Cookie: session=<your_session_cookie>" \
     -H "X-CSRFToken: <csrf_token>" \
     -d '{ "filename": "asset-backup-20260316.tar.gz" }'

Response:

.. code-block:: json

   {
     "ok": true,
     "indices_restored": 18,
     "documents_restored": 452
   }

----

Backup Best Practices
----------------------

1. **Create a backup before any major change** — Before bulk-deleting assets,
   importing a large CSV, or running a restore, create a backup first.

2. **Automate daily backups** — Add a cron job inside the container:

   .. code-block:: bash

      # Inside the container crontab (crontab -e):
      0 2 * * * curl -s -X POST "http://localhost:8084/api/backup/create" \
        -H "Cookie: session=<session>" >> /tmp/backup_cron.log 2>&1

3. **Copy backups off-server** — The ``/app/data/backups/`` directory is inside
   the Docker volume. Copy backup files to S3 or another location:

   .. code-block:: bash

      # Copy latest backup to S3
      sudo docker exec imports_container sh -c \
        "aws s3 cp /app/data/backups/asset-backup-$(date +%Y%m%d).tar.gz \
        s3://your-backup-bucket/kavanahanalytics/"

4. **Retain at least 7 daily backups** — Keep the last 7 days of backups before
   deleting older files.

----

Access Control
--------------

The legacy ``/api/backup/*`` endpoints described above require the
``asset_admin`` or ``superuser`` role. The newer **Restore from
Backup** Management Console page is gated on the ``mc_restore_backup``
permission — a superuser can grant that to any custom role from the
User Management → Roles tab.

Unauthenticated or unauthorised requests receive a ``403 Forbidden``
response.
