.. _api-backup:

Backup API
==========

There are two backup-related surfaces today:

1. The legacy ``/api/backup/*`` endpoints (documented here) create,
   list, and restore tarballed dumps of every ``de_asset_*`` index in
   one shot. These remain available for scripts and disaster recovery
   but are no longer the primary operator interface — they require
   ``asset_admin`` or ``superuser``.
2. The newer ``/restore-backup`` blueprint, gated on the
   ``mc_restore_backup`` permission, drives the *Restore from Backup*
   page in the Management Console. It handles local asset backups,
   S3-based Elasticsearch snapshots, and config extractions
   individually rather than as one tarball. See the route inventory in
   ``bin/python/asset_management/web/blueprints/restore_backup/routes.py``
   for the full list — endpoints follow the
   ``/restore-backup/api/<action>`` convention.

.. warning::

   The restore operation is **destructive and irreversible**. It drops and recreates
   all ``de_asset_*`` Elasticsearch indices from the backup archive. Only use restore
   for disaster recovery. Always create a fresh backup before restoring.

----

.. http:post:: /api/backup/create

   Creates a new backup of all ``de_asset_*`` Elasticsearch indices.

   The backup is saved to ``/app/data/backups/asset-backup-YYYYMMDD.tar.gz``.
   If a backup for today already exists, a timestamp suffix is added.

   No request body is required.

   :statuscode 200:

   .. code-block:: json

      {
        "ok": true,
        "filename": "asset-backup-20260316.tar.gz",
        "size_kb": 142,
        "indices_included": 18,
        "documents_included": 452
      }

   :statuscode 500: Elasticsearch error or disk write failure.
   :statuscode 403: Insufficient role.

----

.. http:get:: /api/backup/list

   Returns a list of all available backup files.

   :statuscode 200:

   .. code-block:: json

      {
        "backups": [
          {
            "filename": "asset-backup-20260316.tar.gz",
            "size_kb": 142,
            "created": "2026-03-16",
            "path": "/app/data/backups/asset-backup-20260316.tar.gz"
          },
          {
            "filename": "asset-backup-20260315.tar.gz",
            "size_kb": 138,
            "created": "2026-03-15",
            "path": "/app/data/backups/asset-backup-20260315.tar.gz"
          }
        ]
      }

   Returns an empty list if no backups exist.

   :statuscode 403: Insufficient role.

----

.. http:post:: /api/backup/restore

   Restores all ``de_asset_*`` Elasticsearch indices from a backup archive.

   .. danger::

      This operation **drops and recreates all asset indices**. All current asset
      data, MFA settings, and dashboard configurations will be replaced with the
      backup's contents. This cannot be undone.

   **Request body (JSON):**

   .. code-block:: json

      { "filename": "asset-backup-20260316.tar.gz" }

   :json string filename: The backup filename (base name only, not the full path).
     Must be a file that exists in ``/app/data/backups/``.

   :statuscode 200:

   .. code-block:: json

      {
        "ok": true,
        "filename": "asset-backup-20260316.tar.gz",
        "indices_restored": 18,
        "documents_restored": 452
      }

   :statuscode 400: Filename not provided or file not found in the backup directory.
   :statuscode 500: Elasticsearch error during restore.
   :statuscode 403: Insufficient role.

----

What Is Included in a Backup
------------------------------

A backup includes all documents and mappings from these indices:

.. list-table::
   :header-rows: 1
   :widths: 50 50

   * - Index
     - Contents
   * - ``de_asset_organization``
     - All organisations
   * - ``de_asset_teams``
     - All teams
   * - ``de_asset_regions`` / ``countries`` / ``cities``
     - Geographic assets
   * - ``de_asset_fb_accounts`` / ``fb_apps`` / ``fb_ad_accounts`` / ``fb_app_users`` / ``fb_pages``
     - All Facebook assets (with encrypted credentials)
   * - ``de_asset_google_ads_customers``
     - Google Ads customers (with encrypted credentials)
   * - ``de_asset_ga_properties``
     - GA4 properties
   * - ``de_asset_echo_tenants``
     - Echo tenants
   * - ``de_asset_ig_accounts``
     - Instagram accounts
   * - ``de_asset_youtube_channels``
     - YouTube channels
   * - ``de_asset_dashboards``
     - Kibana dashboard references and visibility settings
   * - ``de_asset_echarts_dashboards``
     - Dashboard Builder configurations
   * - ``de_asset_user_mfa``
     - Per-user MFA settings

**Not included:** Analytics data indices (``*fb_insights``, etc.), Elasticsearch
security users and roles, Kibana saved objects.
