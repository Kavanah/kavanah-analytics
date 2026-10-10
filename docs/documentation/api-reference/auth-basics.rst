.. _api-reference-auth-basics:

=====================
Authentication Basics
=====================

All API endpoints (except the login, signup, public-report, and a small
set of static / health endpoints) require an authenticated session.

**Session cookie:** After a successful login (``POST /login``), the
server sets a ``session`` cookie. Include this cookie in all subsequent
requests.

**Basic Auth fallback:** Endpoints also accept HTTP Basic Auth
credentials as an alternative to session cookies:

.. code-block:: bash

   curl -X GET "https://<your-domain>/assets/api/import/data-source-status" \
     -u "<username>:<password>"

**MFA enforcement:** If your deployment has ``ENFORCE_MFA=true`` (the
default), API requests from a session that hasn't completed 2FA
enrolment will be redirected to the MFA setup or challenge page. Use a
session that has fully enrolled, or set ``enforce_mfa: false`` in
master-config.yaml on dev/staging deployments where 2FA is opt-in.

For the specific login / logout endpoints, see :doc:`authentication`.
