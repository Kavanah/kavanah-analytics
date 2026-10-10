.. _api-reference-base-url:

========
Base URL
========

The DEAM web application is served behind an Nginx reverse proxy with a URL prefix.
In the default configuration:

.. code-block:: text

   https://<your-domain>/assets/

All API endpoints documented in this reference are relative to this base URL.
For example, the full URL for ``POST /api/import/run`` is:

.. code-block:: text

   https://<your-domain>/assets/api/import/run

The Dashboard Builder blueprint uses the sub-prefix ``/dashboard-builder/``:

.. code-block:: text

   https://<your-domain>/assets/dashboard-builder/api/query
