"""SMTP transport policy — one place that says how the app talks to a mail relay.

 (PWRESET-009): every SMTP call site in the codebase used to build its
channel as ``smtplib.SMTP(host, port); server.starttls()`` or
``smtplib.SMTP_SSL(host, port)`` with no ``context``. In both shapes smtplib
falls back to ``ssl._create_stdlib_context()`` — ``verify_mode=CERT_NONE``,
``check_hostname=False`` — so the channel was encrypted but unauthenticated:
an on-path attacker presenting any certificate could read the relay
credentials sent to ``login()`` and whatever the message carried (password
reset links, invite links, MFA recovery codes, security alerts). Neither shape
set a timeout either, so an unresponsive relay pinned a gunicorn worker.

The fix is deliberately NOT a ``open_smtp(settings)`` wrapper: the existing
tests patch ``<module>.smtplib.SMTP`` on each service module, and a wrapper
would move the construction out from under those patches (they would then
reach a real relay). Each site keeps its own ``smtplib.SMTP(...)`` line and
passes the two things this module owns::

    server = smtplib.SMTP(host, int(port), timeout=SMTP_TIMEOUT_SECONDS)
    server.starttls(context=smtp_ssl_context())
    # or
    server = smtplib.SMTP_SSL(host, int(port), timeout=SMTP_TIMEOUT_SECONDS, context=smtp_ssl_context())

``the release-plan tests`` greps the
tree so a new call site that forgets either argument fails CI.

Public relays (SES, Gmail, Postmark…) present publicly-trusted certificates,
so the default context needs no trust-store work. If a relay ever needs a
private CA, pin it HERE (``ctx.load_verify_locations``), the way
``lib.es_auth`` pins the Elasticsearch CA — never by loosening verification
at a call site.
"""

from __future__ import annotations

import ssl

# Seconds before connect / command / read gives up. Relays answer in well under
# a second; 30 s is generous for a slow handshake and short enough that a dead
# relay does not hold a web worker for the request's lifetime.
SMTP_TIMEOUT_SECONDS = 30


def smtp_ssl_context() -> ssl.SSLContext:
    """A context that VERIFIES the relay: CERT_REQUIRED + hostname check.

    ``ssl.create_default_context()`` gives exactly that (purpose SERVER_AUTH,
    system trust store). Returned fresh per call so a caller cannot mutate
    shared state; the cost is negligible next to the network round-trips.
    """
    return ssl.create_default_context()
