"""Import-error classifier.

Maps a raw importer error string — an API error code, an exception's
``str()``, or a known log message — to a concise, **user-readable**
classification the Report Scheduler (and, later, the Status Reports page)
can show instead of raw log text or stack traces.

Why a standalone module
-----------------------

The same raw errors surface in two places that must not drift:

* the Report Scheduler's per-run "Errors" button (this ticket), fed from
  the ``de_import_progress`` doc's terminal ``error`` field; and
* the Status Reports / Import Data pages (out of scope here, follow-up),
  fed from the dated ``imports_YYYYMMDD.log`` classifier in
  ``log_service.py``.

Keeping the category table here, pure and table-driven, lets both reuse
one source of truth.

What reaches this classifier today
-----------------------------------

For the scheduler path, only the importers' **top-level** failure strings
reach ``de_import_progress`` (``complete_run(status="failed", error=...)``):

* lock contention — ``"another <X> importer holds the lock; skipping"``
* orphaned/killed — ``"orphaned: importer died before completing"`` /
  ``"process exited without recording completion..."``
* the outermost ``str(e)`` from each importer, which can carry a
  ``RefreshError`` / ``invalid_grant`` (revoked OAuth), a
  ``GoogleAdsException``, ``Missing required OAuth credentials`` /
  ``Credentials file not found`` (no creds), a ``DTAuthError`` (Site-Link
  mismatch), or a ``429`` / ``quota`` (rate limit).

The **granular, per-asset** errors (FB/IG code 10 permission, FB code 100
not-found, GA4 per-property invalid_grant, YouTube 403, strict-mode zero
teams, revoked connection) are caught *inside* the importer's per-asset loop
and would otherwise never bubble to ``complete_run``. The follow-up
wires those catch sites to :meth:`ImportProgressService.record_import_error`
(via the ambient :mod:`lib.import_run_context`), which classifies them HERE
and appends them to the run's ``import_errors`` array — so a run that
"succeeded" overall still surfaces the one page/property it had to skip.
Internal, non-user-correctable conditions (FB code 17 throttle is transient
and self-retries; IG code 100 invalid-params and keyword/field-mapping errors
are our bugs, not the user's) are deliberately NOT recorded — see the user's
scope on ("don't include internal issues the user can't control").

Severity
--------

* ``error``     — a real failure the user should act on (red badge).
* ``transient`` — the platform rate-limited us; the import retries on its
  own. Surfaced, but not framed as the user's problem to fix.
* ``warning`` — a standing configuration state that degrades
  imports (e.g. a team still on its own legacy Meta app) but is not a run
  failure. Amber badge; must stay visible without competing with hard run
  failures for attention, so it never counts toward the red error badge —
  and it is NOT ``info``, because info entries are dropped from the import
  errors report and this state is silently losing data.
* ``info``      — not a failure: "no new data this period", or a connector
  (chatwoot) that has no team-scope import step and reports on existing
  data. Grey note, never counted toward the red error badge.

Purity
------

``classify`` does no IO and is deterministic — it can be unit-tested
directly and called from any layer. Unknown input never raises; it falls
back to a generic ``error`` classification so an unrecognized string still
shows the user *something* actionable ("the import didn't finish").
"""

from __future__ import annotations

import re

# Severity tiers (string constants so callers and tests can reference them
# without importing an enum).
SEVERITY_ERROR = "error"
SEVERITY_TRANSIENT = "transient"
SEVERITY_WARNING = "warning"
SEVERITY_INFO = "info"

# Category → user-facing presentation. ``title`` is the short label; ``message``
# is the action-oriented sentence. ``severity`` drives badge styling + whether
# the category counts toward the red error count.
_CATEGORY_TABLE: dict[str, dict[str, str]] = {
    "permission_denied": {
        "title": "Permission denied",
        "message": "Reconnect this account and grant the required permissions.",
        "severity": SEVERITY_ERROR,
    },
    "connection_revoked": {
        "title": "Connection disconnected",
        "message": "This connection was disconnected or its login expired — reconnect it under Data Connectors.",
        "severity": SEVERITY_ERROR,
    },
    # a LEGACY Facebook app token (de_asset_fb_apps) whose 60-day
    # session expired (OAuth code 190). Distinct from connection_revoked: the
    # legacy fb_app is NOT a Data Connectors wizard connection, so the copy must
    # point at the Facebook app/login itself, not the wizard. It cannot
    # auto-refresh (the token being exchanged is itself dead) — a manual
    # reconnect is required, so this is an error-severity, user-actionable row.
    "connection_expired": {
        "title": "Facebook login expired",
        "message": "The Facebook login for this account expired — reconnect Facebook to resume imports.",
        "severity": SEVERITY_ERROR,
    },
    "missing_credentials": {
        "title": "Missing credentials",
        "message": "This connector isn't fully configured — reconnect it under Data Connectors.",
        "severity": SEVERITY_ERROR,
    },
    "rate_limited": {
        "title": "Rate-limited",
        "message": "The platform rate-limited us; the import will retry automatically.",
        "severity": SEVERITY_TRANSIENT,
    },
    "no_data_source": {
        "title": "No data source linked",
        "message": "No data source is linked to this team for this connector — check the team's Data Connectors.",
        "severity": SEVERITY_ERROR,
    },
    # billing-inactive sources. Both are user-correctable (red badge),
    # but the action differs, so they are distinct categories with distinct copy:
    # over-cap → free a slot / upgrade; no subscription → re-subscribe.
    "billing_inactive_over_cap": {
        "title": "Over your connection cap",
        "message": (
            "This data source is over your connection cap and isn't imported — "
            "free a slot or upgrade under Billing to resume imports."
        ),
        "severity": SEVERITY_ERROR,
    },
    "billing_inactive_no_subscription": {
        "title": "Subscription inactive",
        "message": "Your subscription is inactive — re-subscribe under Billing to resume imports.",
        "severity": SEVERITY_ERROR,
    },
    # the team's connection pointer resolved to a DIFFERENT
    # connector's login (e.g. the shared connection_esid was last stamped by
    # the YouTube wizard, so the Meta importer found a Google token). Distinct
    # from connection_revoked: the named connection is healthy — it's just the
    # wrong provider's — and the fix is a reconnect of THIS connector, which
    # re-stamps the pointer.
    "wrong_connection": {
        "title": "Wrong connection linked",
        "message": (
            "This team's connection points at a different connector's login — "
            "reconnect this connector under Data Connectors."
        ),
        "severity": SEVERITY_ERROR,
    },
    # YouTube Analytics 403 with bare reason "forbidden": the
    # AUTHORIZING GOOGLE ACCOUNT lost analytics access on the channel (a
    # YouTube-side permissions change). Distinct from permission_denied: the
    # OAuth token is healthy and there are no app permissions left to grant —
    # "reconnect and grant permissions" is wrong advice. The fix lives in
    # YouTube Studio (a channel owner restores access) or in reconnecting AS
    # a currently-authorized Google identity.
    "youtube_channel_access_denied": {
        "title": "Channel access lost",
        "message": (
            "This account can no longer read analytics for this channel — a channel owner "
            "must grant it access in YouTube Studio (Settings → Permissions), or reconnect "
            "signed in as a Google account that manages the channel."
        ),
        "severity": SEVERITY_ERROR,
    },
    "asset_not_found": {
        "title": "Account no longer accessible",
        "message": (
            "A linked account or page couldn't be found or is no longer " "accessible — re-link it under Data Connectors."
        ),
        "severity": SEVERITY_ERROR,
    },
    "dt_auth": {
        "title": "Disciple.Tools Site-Link invalid",
        "message": "The Disciple.Tools Site-Link token is invalid or the domains don't match — check it under Data Connectors.",
        "severity": SEVERITY_ERROR,
    },
    "lock_contention": {
        "title": "Import skipped",
        "message": "Another import for this connector was already running; this run was skipped and will retry next cycle.",
        "severity": SEVERITY_TRANSIENT,
    },
    "import_incomplete": {
        "title": "Import didn't finish",
        "message": "The import didn't finish — try running the report again.",
        "severity": SEVERITY_ERROR,
    },
    "storage_error": {
        "title": "Storage error",
        "message": "We couldn't save the imported data — try again, and contact support if it persists.",
        "severity": SEVERITY_ERROR,
    },
    # a connected GA4 property Google answers with empty reports day
    # after day: the website stopped sending to it (one team's web property,
    # dark for two months after a Tag Manager change). Nothing is broken on our
    # side and the fix is on the site, so WARNING (amber), not a red error.
    "property_dark": {
        "title": "Google Analytics property stopped receiving data",
        "message": (
            "Google has returned no data for this Google Analytics property for several days — check that the "
            "website's Google Analytics tag (or Tag Manager) still sends to this property."
        ),
        "severity": SEVERITY_WARNING,
    },
    "no_data": {
        "title": "No new data",
        "message": "No new data was available for this period.",
        "severity": SEVERITY_INFO,
    },
    "import_skipped": {
        "title": "Import step skipped",
        "message": "This connector reports on existing data; no fresh import was run for this report.",
        "severity": SEVERITY_INFO,
    },
    # an Echo conversation's referrer matched a known FB page / YouTube
    # channel, but no active team in the tenant owns that asset, so the conversation
    # can't be attributed to a team. The import itself succeeded; this is an
    # attribution gap, not a connector failure — INFO (grey note), never a red
    # badge. The fix is to put the asset on the right team (or accept it's
    # intentionally unowned). detail names the specific page/channel.
    # Google Ads campaigns no team's campaign rules claim (or several
    # do) on a shared account: data left out, user-correctable, nothing broken.
    "unattributed_campaign": {
        "title": "Campaigns not assigned to a team",
        "message": (
            "Some Google Ads campaigns on a shared account match no team's campaign labels or name prefixes "
            "(or match several) and weren't imported — set each team's campaign rules under Data Connectors."
        ),
        "severity": SEVERITY_WARNING,
    },
    "unattributed_referrer": {
        "title": "Conversation couldn't be attributed",
        "message": (
            "Some conversations referred by this page/channel couldn't be attributed to a team — "
            "add it to the owning team's Data Connectors if it should be tracked."
        ),
        "severity": SEVERITY_INFO,
    },
    # a team whose Facebook pages still authenticate under their own
    # legacy Meta app (or partly do — "mixed") instead of the unified wizard
    # app. NOT run-derived: this is static configuration detected from asset
    # state (lib.fb_app_attribution), injected synthetically by the import
    # errors report builder rather than recorded by an importer, so no _RULES
    # regex maps to it — it is reachable only by direct category lookup.
    # warning severity by design: legacy apps request fewer scopes and the
    # importer skips denied assets silently, so the team IS losing data, but
    # the finding must not compete with hard run failures for the red badge.
    "legacy_meta_app": {
        "title": "Legacy Meta app",
        "message": (
            "This team is still authenticated under its own legacy Meta app — "
            "reconnect Facebook via Data Connectors to migrate it to the unified app."
        ),
        "severity": SEVERITY_WARNING,
    },
    "unknown": {
        "title": "Import error",
        "message": "The import didn't finish — try running the report again.",
        "severity": SEVERITY_ERROR,
    },
}

# Ordered (regex, category) rules — FIRST match wins. Order matters: the
# specific lock / orphaned / credential strings must be tested before the broad
# permission / revoked / rate-limit patterns, which in turn precede any generic
# fallthrough. Each pattern is case-insensitive.
#
# Patterns are matched against the raw error string with ``re.search`` (substring
# semantics), so they target the distinctive token, not the whole message.
_RULES: list[tuple[re.Pattern[str], str]] = [
    # ── Lock contention. The legacy phrase ("<X> importer holds the lock;
    # skipping") plus the phrase the lock-loser resolve now writes
    # ("another import is already in progress; this run was skipped"). Without the
    # second alternative the new message fell through to ``unknown`` and showed the
    # misleading "import didn't finish — try again" (a lock skip is benign and
    # self-resolving, not a failure to retry). ──
    (re.compile(r"holds the lock; skipping|another import is already in progress", re.I), "lock_contention"),
    # ── Orphaned / killed mid-flight ──
    (re.compile(r"orphaned:|exited without recording completion|killed mid-flight", re.I), "import_incomplete"),
    # ── Billing-inactive source. The importer billing pre-check
    # (lib.billing_entitlement.source_billing_block) skips a source that is over
    # the paid connection cap, or whose subscription is inactive, and records the
    # distinctive raw string here via record_billing_skip. Distinctive tokens
    # ("connection cap" / "subscription is inactive") so they can't grab unrelated
    # errors. Over-cap is tested first; both precede the broad fallthroughs. ──
    (re.compile(r"over the connection cap|over your connection cap", re.I), "billing_inactive_over_cap"),
    (re.compile(r"subscription is inactive", re.I), "billing_inactive_no_subscription"),
    # ── Facebook legacy-app token expired ──────────
    # MUST precede connection_revoked: a code-190 "session has expired" is an
    # expiry (reconnect Facebook), NOT a user-initiated disconnect, and points at
    # the legacy fb_app login rather than the Data Connectors wizard. Distinctive
    # token "facebook login expired" is what get_insights records; "session has
    # expired" + "code 190" also match the raw FB API string defensively.
    (
        re.compile(r"facebook login expired|session has expired|\bcode 190\b|error_subcode.{0,4}46[37]", re.I),
        "connection_expired",
    ),
    # ── Team pointer stamped by another connector's wizard ────────
    # MUST precede connection_revoked/connection_expired: without this guard the
    # Google-token-vs-FB-API failure surfaced as a misleading code-190
    # "disconnected", naming a healthy connection. The distinctive "overwritten
    # by another connector" token is what get_insights' provider guard records.
    (re.compile(r"pointer overwritten by another connector", re.I), "wrong_connection"),
    # ── Connection revoked / OAuth refresh rejected (GA4, YouTube) ──
    (re.compile(r"invalid_grant|refresherror|revoked|disconnected", re.I), "connection_revoked"),
    # ── Missing credentials (google_ads ValueError, GA4 service-account path) ──
    # "no oauth connection": the GA4 importer records this exact phrase
    # when a property's team hasn't authorized a GA4 OAuth connection (no SA
    # fallback). Without it the string fell through to ``unknown``
    # and showed the misleading "import didn't finish — try again" instead of the
    # actionable "reconnect this connector" (same gap class as the lock
    # skip). Use the distinctive "no oauth connection" token, NOT the broad "not
    # authorized", to avoid grabbing unrelated permission errors.
    (
        re.compile(
            r"missing required oauth credentials|credentials file not found|developer.?token|"
            r"no oauth token|no oauth connection|failed to load credentials",
            re.I,
        ),
        "missing_credentials",
    ),
    # ── Disciple.Tools Site-Link auth ──
    (re.compile(r"dtautherror|site.?link", re.I), "dt_auth"),
    # ── Rate limit / quota / throttle (FB code 17, GA4 429/quota, HTTP 429) ──
    (
        re.compile(
            r"\bcode 17\b|2446079|rate.?limit|too many api calls|user request limit|"
            r"\b429\b|resource_exhausted|\bquota\b|hit quota",
            re.I,
        ),
        "rate_limited",
    ),
    # ── YouTube channel-access 403, reason "forbidden" ────────────
    # MUST precede permission_denied: this is the 403 where "grant the required
    # permissions" is wrong advice (nothing left to grant; a channel owner must
    # restore access in YouTube Studio). The distinctive "channel analytics
    # forbidden" token is what get_youtube_analytics records for a bare
    # reason=forbidden body; a genuine insufficientPermissions 403 records the
    # "access denied" wording and still lands on permission_denied below.
    (re.compile(r"channel analytics forbidden", re.I), "youtube_channel_access_denied"),
    # ── Permission denied (FB/IG code 10, instagram_manage_insights) ──
    (
        re.compile(
            r"\bcode 10\b|error code 10\b|instagram_manage_insights|access denied|"
            r"insufficientpermissions|insight permissions|lacks insights",
            re.I,
        ),
        "permission_denied",
    ),
    # ── Linked account/page gone or inaccessible (FB page code 100 "not found
    # or not accessible"). MUST follow permission_denied (a page can be both
    # "access denied" + a 100); the distinctive "not found or not accessible"
    # token keeps this from grabbing IG's unrelated "invalid params (code 100)",
    # which we don't surface at all. ──
    (re.compile(r"not found or not accessible|not accessible \(code 100\)", re.I), "asset_not_found"),
    # ── Connector has no team-scope import step (chatwoot) ──────────────────
    # MUST precede no_data_source: "not team-scopable" / "no scope values
    # resolved" both contain a "team"/"scope" token the broader no_data_source
    # pattern would otherwise grab. import_skipped is the more specific category.
    (re.compile(r"not team-scopable|import step skipped|no scope values resolved", re.I), "import_skipped"),
    # ── Echo referrer matched an asset no team owns ───────────────
    # MUST precede no_data_source: the echo catch site's context contains "no
    # active team" (the no_data_source token). The distinctive "referrer
    # unattributed" token keeps this info-severity attribution note off the
    # error-severity no_data_source rule. ──
    (re.compile(r"referrer unattributed", re.I), "unattributed_referrer"),
    # ── Google Ads campaign matched no team's rules ───────────────
    # MUST precede no_data_source: the message names teams and rules.
    (re.compile(r"campaign unattributed", re.I), "unattributed_campaign"),
    # ── No data source linked to the team (strict-mode zero teams / no chain) ──
    (
        re.compile(
            r"zero teams|no active team|no asset chain|no traversable|" r"not linked to a|no .* linked to (?:this )?team",
            re.I,
        ),
        "no_data_source",
    ),
    # ── ES write / storage failures ──
    (
        re.compile(r"bulk.{0,20}error|failed to (?:index|write)|elasticsearch.*(?:unavailable|error)", re.I),
        "storage_error",
    ),
    # ── A GA4 property that went dark. MUST precede no_data: the
    # message contains "no data". ──
    (re.compile(r"property went dark", re.I), "property_dark"),
    # ── No new data for the window (informational) ──
    (re.compile(r"no (?:new )?data|0 documents|no records", re.I), "no_data"),
]


def classify(raw: str, *, connector_id: str | None = None, detail: str | None = None) -> dict[str, str]:
    """Classify a raw importer error string into a user-readable result.

    Args:
        raw: The raw error text — an exception ``str()``, an API error
            message, or a known log line. Empty / ``None`` is treated as an
            unknown error (the caller shouldn't pass empty, but we never raise).
        connector_id: Optional connector hint (``meta_ads``, ``echo``, ...).
            Reserved for future connector-specific disambiguation; unused today
            but accepted so callers can pass it without a signature change.
        detail: Optional name of the SPECIFIC asset the error is about — the
            Google Ads customer, the Facebook page/team, the GA4 property, the
            YouTube channel. When given it is prepended to the message ("Kavanah
            Media: This connector isn't fully configured …") so the user knows
            *which* account to fix, and returned as a ``detail`` field so callers
            can dedup per-asset (two unconfigured accounts → two lines).

    Returns:
        ``{"category", "title", "message", "severity", "detail"}``. Always a
        valid dict — unrecognized input falls back to the ``unknown`` category
        (an ``error``-severity generic message), never an exception. ``detail``
        is "" when none was supplied.
    """
    text = (raw or "").strip()
    detail = (detail or "").strip()

    def _result(category: str) -> dict[str, str]:
        base = _CATEGORY_TABLE[category]
        message = base["message"]
        if detail:
            # Prefix the specific asset name so the user sees which one to fix.
            message = f"{detail}: {message}"
        return {
            "category": category,
            "title": base["title"],
            "message": message,
            "severity": base["severity"],
            "detail": detail,
        }

    if not text:
        return _result("unknown")

    for pattern, category in _RULES:
        if pattern.search(text):
            return _result(category)

    return _result("unknown")


def category_presentation(category: str) -> dict[str, str]:
    """The user-facing ``{title, message, severity}`` for a known category
    (a copy; the ``unknown`` entry when unrecognized).

    For callers that already know the category and have no raw error string
    to :func:`classify` — synthetic ``legacy_meta_app`` rows are
    computed from asset state, not from an importer error, but their copy and
    severity must still come from this one table so surfaces can't drift.
    """
    return dict(_CATEGORY_TABLE.get(category) or _CATEGORY_TABLE["unknown"])


def is_error_severity(category_or_result: str | dict) -> bool:
    """True if the category/result counts toward the red error badge.

    ``transient`` and ``info`` do NOT count — a rate-limit retries on its own
    and "no new data" / "import skipped" are configuration states, mirroring
    ``summarize_snapshot_errors``' benign-skip handling.
    """
    if isinstance(category_or_result, dict):
        severity = category_or_result.get("severity", SEVERITY_ERROR)
    else:
        severity = _CATEGORY_TABLE.get(category_or_result, {}).get("severity", SEVERITY_ERROR)
    return severity == SEVERITY_ERROR
