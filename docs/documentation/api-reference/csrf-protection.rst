.. _api-reference-csrf-protection:

===============
CSRF Protection
===============

All state-changing requests (POST, DELETE) require a CSRF token. Include the
token in one of these ways:

**Header (preferred for AJAX/API clients):**

.. code-block:: text

   X-CSRFToken: <token_value>

**Form field:**

.. code-block:: text

   csrf_token=<token_value>

Obtain the CSRF token from the ``window.CSRF_TOKEN`` JavaScript variable on any
authenticated page, or by parsing the ``csrf_token`` hidden field from any form.

Requests missing a valid CSRF token receive a ``400 Bad Request`` response.
