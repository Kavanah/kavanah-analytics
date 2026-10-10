"""Public (session-less) responses must carry Cache-Control.

After surfaced the marketing-page staleness issue (users seeing
stale HTML for hours after a deploy unless they hard-refreshed or used
Incognito), we extended the same pattern to the Flask app: every public
response that doesn't already set a Cache-Control header now picks up
``no-cache, must-revalidate`` via the ``set_security_headers``
after_request hook.

The pre-existing ``prevent_bfcache_for_session_bearing_responses`` hook
keeps applying the stricter ``no-store`` directive to authenticated /
mfa_pending / fpc_pending responses (its own test_id is in
``test_login_text.py``).

This file pins the *public* half:

- redirects (which don't normally set Cache-Control) get the new directive
- 4xx error responses on public endpoints also get it
"""

from __future__ import annotations


def _assert_carries_revalidation(headers, route: str) -> None:
    """Either ``no-cache, must-revalidate`` (the new default) or
    ``no-store`` (handler-set, also valid) is acceptable — the only
    failure mode is no Cache-Control at all, which lets browsers
    heuristic-cache the response and serve stale content after a deploy.
    """
    cache = headers.get("Cache-Control", "")
    lower = cache.lower()
    assert "no-cache" in lower or "no-store" in lower, (
        f"Public response for {route!r} must carry a cache-prevention "
        f"directive so browsers don't heuristic-cache it across deploys. "
        f"Got Cache-Control={cache!r}."
    )


def test_legacy_dashboards_login_redirect_carries_cache_control(client):
    """301 redirect from /dashboards/login -> /login is a public response
    that doesn't set its own Cache-Control. Without the after_request
    setdefault Chrome heuristic-caches the redirect itself, so a stale
    redirect target survives across deploys."""
    resp = client.get("/dashboards/login", follow_redirects=False)
    assert resp.status_code in (301, 302, 308)
    _assert_carries_revalidation(resp.headers, "/dashboards/login")


def test_public_report_invalid_token_carries_cache_control(client):
    """Public Impact Report URLs are session-less. Whether
    the token resolves to a valid report or 4xxs, the response shell
    is what the browser will cache — and the same staleness window
    applies."""
    resp = client.get("/public/report/this-token-does-not-exist", follow_redirects=False)
    # Either 404 (token unknown) or 200 (placeholder) — both are public
    # responses and both must carry the directive.
    _assert_carries_revalidation(resp.headers, "/public/report/<token>")
