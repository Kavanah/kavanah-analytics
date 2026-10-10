.. _api-reference-http-status-codes:

=================
HTTP Status Codes
=================

.. list-table::
   :header-rows: 1
   :widths: 15 85

   * - Code
     - Meaning
   * - ``200``
     - Success
   * - ``400``
     - Bad request — missing or invalid parameters
   * - ``401``
     - Unauthenticated — no valid session or Basic Auth credentials
   * - ``403``
     - Forbidden — authenticated but insufficient role
   * - ``404``
     - Not found — resource does not exist
   * - ``429``
     - Rate limited — too many failed authentication attempts (5 per 15 minutes)
   * - ``500``
     - Internal server error — check the application error log
   * - ``503``
     - Service unavailable — Elasticsearch is unreachable
