.. _api-reference-rate-limiting:

=============
Rate Limiting
=============

Failed authentication attempts are rate-limited to **5 attempts per 15 minutes**
per username. After 5 failures, the account is temporarily locked and requests
return ``429 Too Many Requests``.

Successful requests are not rate-limited.
