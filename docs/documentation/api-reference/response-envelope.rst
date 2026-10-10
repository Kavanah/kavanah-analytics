.. _api-reference-response-envelope:

========================
Common Response Envelope
========================

Most ``/api/*`` endpoints return JSON with a consistent structure:

.. code-block:: text

   {
     "success": true,
     "message": "Optional human-readable message",
     ...additional fields...
   }

On error:

.. code-block:: json

   {
     "success": false,
     "error": "Description of the error"
   }

Some endpoints return different shapes (e.g. the Dashboard Builder query endpoint
returns ECharts option objects directly). These are documented in the individual
endpoint pages.
