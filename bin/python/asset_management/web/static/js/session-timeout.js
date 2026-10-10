/**
 * Session timeout handling.
 *
 * Two coordinated mechanisms so an inactive tab handles an expired session
 * consistently — previously the outcome depended on which kind of request
 * the next click happened to make (clean redirect on a full-page nav, a
 * cryptic JSON parse-error on an AJAX call, or nothing at all while idle).
 *
 *   1. Fetch guard — wraps window.fetch and, when the server says the
 *      session has expired (a 401 with {"error":"session_expired"}, or a
 *      redirect whose final URL is the login page), navigates the whole tab
 *      to the login URL instead of letting the caller choke on login HTML.
 *
 *   2. Idle timer — redirects to /login once the session's idle window
 *      (SESSION_LIFETIME, supplied by the server via a meta tag) elapses
 *      with no user activity, so a genuinely-idle tab logs itself out
 *      proactively rather than waiting for the next click.
 *
 * Load AFTER app.js so this composes on top of the CSRF fetch wrapper
 * rather than racing a second window.fetch monkey-patch.
 */
(function () {
    'use strict';

    function loginUrlWithNext() {
        var here = window.location.pathname + window.location.search;
        // Don't bounce-loop if we're already on the login surface.
        if (window.location.pathname === '/login') {
            return null;
        }
        return '/login?next=' + encodeURIComponent(here);
    }

    // The dashboard viewer (view.html / summary_report.html) renders inside
    // an iframe on the read-only dashboards page. A redirect there must move
    // the TOP-LEVEL tab, not just the iframe — otherwise the login page would
    // render embedded inside the dashboard frame.
    function isFramed() {
        try {
            return window.self !== window.top;
        } catch (e) {
            // Cross-origin access to window.top throws — treat as framed.
            return true;
        }
    }

    function goToLogin(explicitUrl) {
        var target = explicitUrl || loginUrlWithNext();
        if (!target) {
            return;
        }
        try {
            if (isFramed() && window.top) {
                window.top.location.href = target;
                return;
            }
        } catch (e) { /* cross-origin top — fall through to self */ }
        window.location.href = target;
    }

    // --- 1. Fetch guard ---------------------------------------------------
    // Compose on top of whatever window.fetch already is (app.js's CSRF
    // wrapper). We must not parse-consume the caller's body, so we clone the
    // response only when we need to inspect a 401's JSON.
    var _wrappedFetch = window.fetch;
    window.fetch = function (input, init) {
        return _wrappedFetch.call(this, input, init).then(function (response) {
            // A 302 -> /login is followed transparently by fetch; the only
            // observable trace is response.redirected + a final URL on the
            // login page. Surface that as a full-tab redirect.
            try {
                if (response.redirected && response.url &&
                    response.url.indexOf('/login') !== -1) {
                    goToLogin();
                    return response;
                }
            } catch (e) { /* response.url unavailable — fall through */ }

            // AJAX/API callers get a 401 + JSON
            // {"error":"session_expired","login_url":...} from verify_auth.
            if (response.status === 401) {
                var ct = response.headers.get('content-type') || '';
                if (ct.indexOf('application/json') !== -1) {
                    // Clone so the original response is still consumable by
                    // the caller if we turn out not to redirect.
                    return response.clone().json().then(function (body) {
                        if (body && body.error === 'session_expired') {
                            goToLogin(body.login_url);
                        }
                        return response;
                    }).catch(function () {
                        return response;
                    });
                }
            }
            return response;
        });
    };

    // --- 2. Idle timer ----------------------------------------------------
    // SESSION_LIFETIME (seconds) is the server's idle window. The server
    // refreshes last_activity on every authenticated request, so each fetch
    // the user triggers also resets THIS timer (activity listeners below).
    // Only the top-level window runs the idle timer. The dashboard viewer
    // iframe still gets the fetch guard above, but its parent page already
    // owns the idle countdown — running a second one in the frame is
    // redundant and its activity listeners wouldn't see clicks landing on
    // the parent chrome anyway.
    var meta = document.querySelector('meta[name="session-idle-timeout"]');
    var idleSeconds = meta ? parseInt(meta.getAttribute('content'), 10) : 0;

    if (idleSeconds && idleSeconds > 0 && !isFramed()) {
        var idleTimer = null;

        // Fire a few seconds AFTER the server's idle window so that, by the
        // time we navigate to /login, the server definitely agrees the
        // session has expired and renders the login form — rather than
        // seeing a still-valid session and bouncing us back to the dashboard
        // (the "reloads but never logs out" symptom). The server is
        // authoritative; this client timer is the trigger, with a grace
        // margin to absorb clock skew and the gap between the user's last
        // real request and now. Capped so a tiny configured window (e.g. a
        // 30s test value) still gets a meaningful margin without overshooting.
        var graceSeconds = Math.min(10, Math.max(3, Math.round(idleSeconds * 0.15)));
        var fireAfterMs = (idleSeconds + graceSeconds) * 1000;

        function onIdleExpiry() {
            goToLogin();
        }

        function resetIdleTimer() {
            if (idleTimer) {
                clearTimeout(idleTimer);
            }
            idleTimer = setTimeout(onIdleExpiry, fireAfterMs);
        }

        // Throttle activity resets to at most once per 5s — listeners on
        // mousemove/scroll fire constantly and we only need a coarse signal.
        var lastReset = 0;
        function onActivity() {
            var now = Date.now();
            if (now - lastReset > 5000) {
                lastReset = now;
                resetIdleTimer();
            }
        }

        ['mousedown', 'keydown', 'scroll', 'touchstart'].forEach(function (ev) {
            window.addEventListener(ev, onActivity, { passive: true });
        });

        // When the tab is hidden the timer keeps running on the original
        // schedule; on becoming visible again, if we're already past the
        // window, redirect immediately rather than waiting out a stale timer.
        document.addEventListener('visibilitychange', function () {
            if (document.visibilityState === 'visible') {
                onActivity();
            }
        });

        resetIdleTimer();
    }
})();
