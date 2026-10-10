"""Facebook Graph API version constants — single source of truth.

Bumping the version is a one-line change here, not a sweep across the codebase.
"""

FB_GRAPH_VERSION: str = "v25.0"

FB_GRAPH_URL: str = f"https://graph.facebook.com/{FB_GRAPH_VERSION}"

FB_OAUTH_DIALOG_URL: str = f"https://www.facebook.com/{FB_GRAPH_VERSION}/dialog/oauth"

# bound every Graph HTTP call so a wedged socket can't sleep
# indefinitely. A worker blocked in a timeout-less recv() never returns to
# the importer's cancellation check, so the dashboard Cancel button is a
# silent no-op (confirmed on staging). The tuple is (connect, read):
#   * connect (10s): generous for TLS + DNS under load; a connect that
#     stalls past this is a dead route, not a slow one.
#   * read (120s): wider than any single Graph insights page response in
#     practice (typical < 10s) but bounded so the per-date fetch can't
#     wedge for hours. On timeout, requests raises requests.Timeout, which
#     the existing per-call retry/backoff paths already treat as a failed
#     attempt — no new error handling required.
FB_GRAPH_HTTP_TIMEOUT: tuple[int, int] = (10, 120)

# scalar variant for the facebook_business SDK. FacebookAdsApi.init
# accepts a single ``timeout=`` that it threads into api.call(timeout=...),
# which requests applies to BOTH connect and read (a scalar requests timeout
# bounds each phase). The SDK does not accept a (connect, read) tuple, so this
# mirrors the read bound of FB_GRAPH_HTTP_TIMEOUT. Same purpose: a wedged SDK
# socket can't sleep past this and block the importer's cancellation check.
FB_GRAPH_SDK_TIMEOUT: int = 120
