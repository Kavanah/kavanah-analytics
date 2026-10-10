.. _api-reference:

=============
API Reference
=============

This section documents the REST API that powers the Kavanah Analytics DEAM web
interface. The API is intended for advanced users, automation scripts, and
integration developers.

.. admonition:: Documentation status

   This reference is documented from **source code inspection** (last
   refreshed April 2026). A formal OpenAPI 3.0 specification is planned
   under **** (Workstream 5, Task T-22: *"Add docstrings to all
   Flask routes; generate Swagger/OpenAPI spec"*). Until that
   specification is published, this document is authoritative.

   All endpoints have been verified against:

   - ``bin/python/asset_management/web/app.py``
   - ``bin/python/asset_management/web/blueprints/dashboard_builder/routes.py``
   - ``bin/python/asset_management/web/blueprints/data_explorer/routes.py``
   - ``bin/python/asset_management/web/blueprints/public_report/routes.py``
   - ``bin/python/asset_management/web/blueprints/restore_backup/routes.py``
   - ``bin/python/asset_management/web/blueprints/data_connectors/routes.py``

.. toctree::
   :maxdepth: 2
   :caption: Conventions

   base-url
   auth-basics
   csrf-protection
   http-status-codes
   response-envelope
   rate-limiting

.. toctree::
   :maxdepth: 2
   :caption: Shared endpoints

   authentication

.. toctree::
   :maxdepth: 2
   :caption: Dashboard API

   dashboards
   dashboard-builder-api

.. toctree::
   :maxdepth: 2
   :caption: Management Console API

   assets
   import
   user-management-api
   backup-api
