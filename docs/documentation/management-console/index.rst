.. _management-console:

==================
Management Console
==================

The Management Console section covers asset configuration, data imports, user
management, and operations tasks.

.. note::

   Some pages in this section require the ``asset_admin`` or ``superuser`` role.
   Role requirements are noted on each page.

.. toctree::
   :maxdepth: 2
   :hidden:

   asset-explorer
   import-data
   import-logs
   user-management
   roles-and-permissions
   two-factor-auth
   oauth-connections
   elasticsearch-ops
   backup-restore

----

**In this section:**

:doc:`asset-explorer`
   Create, edit, and delete assets for all supported platforms (organisations,
   teams, Facebook apps, Google Ads customers, Echo tenants, and more).

:doc:`import-data`
   Run on-demand data imports, configure date ranges, monitor progress with live
   log tailing, and manage data refresh.

:doc:`import-logs`
   View, download, and delete import log files. Check the Status Reports page
   for a high-level view of system health.

:doc:`user-management`
   View all users, sync Elasticsearch security roles, and enable or disable
   two-factor authentication per user.

:doc:`roles-and-permissions`
   Understand the role naming convention, what each role grants, and how
   role-based filtering works across the platform.

:doc:`two-factor-auth`
   Configure TOTP (authenticator apps) and YubiKey hardware key 2FA for
   admin accounts. Step-by-step setup flows for both methods.

:doc:`oauth-connections`
   Manage Facebook and Google OAuth2 connections: generate OAuth URLs,
   store and refresh access tokens, and check token health.

:doc:`elasticsearch-ops`
   Understand the Elasticsearch index structure, Kibana data views, index health,
   and field mapping requirements for correct filtering.

:doc:`backup-restore`
   Create and restore backups of all asset data. Understand what is included
   in a backup and the implications of a restore operation.
