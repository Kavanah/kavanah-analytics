"""
Connections Blueprint Routes

Per-(principal, provider, provider_account_id) connection-detail pages
plus the two lifecycle actions — reauthorize (Meta-side OAuth roundtrip
against an existing connection) and disconnect (soft-delete via
``status=revoked``).

The Data Connectors card's "Manage connection" button — renamed from
the legacy "Manage OAuth tokens" — deep-links here at
``/connections/<connection_esid>`` instead of the legacy asset-explorer
list view at ``/assets/facebook_app_users``. The legacy list view stays
functional for operator / superuser legacy-asset CRUD; the new page is
the user-facing manage surface for wizard-cohort connections whose
token lives on ``de_connection``.

Routes:

* ``GET  /connections/<esid>``                — read-only detail page
* ``GET  /connections/<esid>/start-reauth``   — kick off OAuth roundtrip
                                                (Meta only)
* ``POST /connections/<esid>/disconnect``     — flip status to revoked

The OAuth callback at ``analytics.onboarding_step7_oauth_callback``
detects a reauth flow via ``session["oauth_reauth_active_esid"]`` and
branches into ``_handle_reauth_callback`` (defined in the analytics
blueprint), which upserts the refreshed token against the existing
connection's natural key and redirects back to the detail page.
"""

import re
import logging
import secrets
from datetime import datetime, timezone

from flask import abort, jsonify, redirect, render_template, request, session, url_for

from . import connections_bp

try:
    from web.services.connection_service import ConnectionService
    from web.services.role_filter_service import RoleFilterService
    from web.services.workspace_membership_service import WorkspaceMembershipService
except ImportError:
    from ...services.connection_service import ConnectionService
    from ...services.role_filter_service import RoleFilterService
    from ...services.workspace_membership_service import WorkspaceMembershipService


# / providers whose OAuth roundtrip the wizard owns.
# Meta-side connections (facebook + instagram, which share a Meta app) go
# through the wizard's Meta dialog; youtube goes through the Google
# leg on fb_oauth_server. GA4 / Google Ads / Echo / Chatwoot are tokenless or
# service-account so "reauthorize" has no meaning for them.
# The template gates the Reauthorize button on membership of this set.
# tiktok: the Login Kit leg on fb_oauth_server (_start_tiktok_reauthorize).
_REAUTH_SUPPORTED_PROVIDERS = frozenset({"facebook", "instagram", "youtube", "tiktok"})

# the connection page's amber badge on "Meta data access expires"
# uses the SAME window as the probe's renewal_required verdict, so the page and
# the banner never disagree about whether a connection is "expiring".
try:
    from lib.credential_health import RENEWAL_THRESHOLD_DAYS as _DATA_ACCESS_WARN_DAYS
except ImportError:  # pragma: no cover - import boundary
    _DATA_ACCESS_WARN_DAYS = 21

# providers whose reauth leg is Google/YouTube rather than Meta.
# Split out because the two legs diverge completely: Meta mints state on
# flask.session and returns to the wizard's registered callback, while YouTube
# mints state ON THE ACCOUNT DOC and returns to fb_oauth_server's already-
# registered /youtube/auth_callback (see _start_youtube_reauthorize).
_GOOGLE_REAUTH_PROVIDERS = frozenset({"youtube"})

# providers whose connection holds an OAuth token (vs
# api_key / service_account / tokenless). Used to gate the Disconnect
# button on the detail page. Echo + Chatwoot connections do not hold
# user-scoped tokens (cluster-wide service token), so "Disconnect"
# would not change importer behaviour for them — the team-side asset
# row would still drive the import. We surface the button anyway when
# the connection has a real principal, because flipping status=revoked
# is still a useful audit signal; if that turns out to be confusing
# we can narrow this set later. ships it broad.
_DISCONNECT_SUPPORTED_PROVIDERS = frozenset(
    {
        "facebook",
        "instagram",
        "youtube",
        "google_ads",
        "ga4",
        "echo",
        # Phase 3 — the Bitly connection holds a real per-team API
        # token (encrypted); revoking it stops the importer for that team
        # (the connection enumeration skips non-active rows). Not in the
        # reauth sets: there is no OAuth roundtrip — re-entering the token
        # via the Data Connectors wizard is the reconnect path.
        "bitly",
        # revoking a TikTok connection stops its imports (the
        # importer skips non-active rows). Reconnect = run the Data Connectors
        # wizard again (the callback merges into this row), so not in the
        # reauth sets.
        "tiktok",
    }
)


# Map ``de_connection.provider`` values to human-readable labels for the
# page title. The provider enum lives on ConnectionService (PROVIDER_*).
_PROVIDER_LABELS: dict[str, str] = {
    "facebook": "Facebook",
    "instagram": "Instagram",
    "youtube": "YouTube",
    "google_ads": "Google Ads",
    "ga4": "Google Analytics 4",
    "echo": "Echo Messaging",
    "bitly": "Bitly",
    "tiktok": "TikTok",
}


# Friendly names for FB / IG OAuth scopes. Unknown scopes render as their
# raw string. Kept short — the full scope catalogue is documented in the
# Meta app review notes; this map only needs the scopes we actually
# request via WizardOAuthService.default_scopes_for("meta_ads").
_SCOPE_FRIENDLY: dict[str, str] = {
    "ads_read": "Read ad insights",
    "ads_management": "Manage ads",
    "pages_show_list": "List Pages you manage",
    "pages_read_engagement": "Read Page engagement",
    "instagram_basic": "Basic Instagram profile",
    "instagram_manage_insights": "Read Instagram insights",
    "business_management": "Read your Business Manager",
    "read_insights": "Read Page insights",
    "public_profile": "Public profile",
    "email": "Email address",
}


def _format_expires_in_days(token_expires_at) -> tuple[str | None, int | None]:
    """Return ``(human_string, days_remaining)`` from a token_expires_at
    field. The field can be an ISO-8601 string (the wizard's write
    path) or an int unix timestamp (legacy fb_app_users.fb_app_user_token_exp).
    Returns ``(None, None)`` when the field is missing or unparseable.
    """
    if token_expires_at in (None, "", 0):
        return None, None
    try:
        if isinstance(token_expires_at, (int, float)):
            exp = datetime.fromtimestamp(int(token_expires_at), tz=timezone.utc)
        else:
            iso = str(token_expires_at).replace("Z", "+00:00")
            exp = datetime.fromisoformat(iso)
            if exp.tzinfo is None:
                exp = exp.replace(tzinfo=timezone.utc)
    except (TypeError, ValueError):
        return None, None
    now = datetime.now(timezone.utc)
    delta = exp - now
    days = int(delta.total_seconds() // 86400)
    if days <= 0:
        return "expired", days
    if days == 1:
        return "expires tomorrow", days
    return f"expires in {days} days", days


def _format_iso(value) -> str:
    """Render a date-ish field as ``YYYY-MM-DD HH:MM UTC`` or empty
    string when not parseable. Same input shape as
    ``_format_expires_in_days`` — the connection service writes ISO-8601
    via ``datetime.now(timezone.utc).isoformat()`` (see
    ``connection_service._now_iso``).
    """
    if not value:
        return ""
    try:
        if isinstance(value, (int, float)):
            dt = datetime.fromtimestamp(int(value), tz=timezone.utc)
        else:
            iso = str(value).replace("Z", "+00:00")
            dt = datetime.fromisoformat(iso)
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=timezone.utc)
    except (TypeError, ValueError):
        return ""
    return dt.strftime("%Y-%m-%d %H:%M UTC")


def _viewer_can_see_connection(
    connection_source: dict,
    viewer_username: str | None = None,
    connection_esid: str | None = None,
    viewer_roles: list | None = None,
) -> bool:
    """authorisation gate for the detail page.

    A viewer can see a connection if any of:

    1. They are the connection's ``principal`` (most common — the user
       who OAuth'd is reading their own connection).
    2. ``RoleFilterService.get_allowed_workspaces()`` returns ``None``
       — the legacy superuser / asset_admin bypass that already gates
       every Asset Explorer page.
    3. The connection's principal belongs to a workspace the viewer
       can see (via ``WorkspaceMembershipService.get_workspaces_for_user``)
       AND ``org_shared=True``. Workspace admins can read connections
       held by other members of their workspace only when the
       connection has been explicitly opted into org-sharing via the
       defaults to False, so the safe answer is
       "private to the principal" unless the principal opted in.

    ``viewer_username`` is passed in by the route so the helper itself
    has no Flask-session dependency — that keeps the four authz
    branches unit-testable without a request context.

    Plus: a workspace OWNER — System 2 grant, empty
    ``session["roles"]`` — of a workspace this connection serves, resolved
    via :func:`_owner_grant_workspaces` when ``connection_esid`` is given.

    Note: this is a READ gate. Disconnect stays principal-only.
    Reauthorize is gated by the stricter
    :func:`_viewer_can_reauthorize_connection` — no longer
    principal-only; the token-overwrite risk that rule guarded against is
    handled by the coverage check + esid-addressed adopt instead.
    """
    if not connection_source:
        return False

    viewer = viewer_username or ""
    if not viewer:
        return False

    principal = connection_source.get("principal") or ""
    if viewer == principal:
        return True

    # the explicit privileged roles see every connection, whatever the
    # resolver returns. Since F1 a real superuser resolves to a LIST (their
    # grants, possibly []), never None, so the branch below never admitted one: the
    # banner and the re-auth gate (explicit "superuser" role) offered Reconnect, and
    # the page hosting it 404'd for any connection outside the superuser's own
    # workspaces — including every connection no team links (prod, 2026-10-07).
    if {"superuser", "asset_admin"}.intersection(viewer_roles or []):
        return True

    allowed_workspaces = RoleFilterService.get_allowed_workspaces()
    if allowed_workspaces is None:
        # Tier 1 — ``None`` is overloaded: "privileged see-all"
        # (superuser / asset_admin), but ALSO "roleless user with no scope
        # to resolve". While this page sat behind the blueprint's
        # asset_admin route gate the ambiguity was harmless; with
        # ``connections.connection_detail`` exempted in auth.py so
        # workspace owners (System 2 grant, empty session["roles"]) can
        # reach it, a roleless session resolves ``None`` here too. See-all is
        # admitted only on the explicit privileged roles, checked above
        # (passed by the route from session — this helper stays
        # Flask-session-free so the authz branches remain unit-testable
        # without a request context); a None here falls through to the
        # owner-grant and org-shared clauses.
        allowed_workspaces = []

    # Tier 1 — a workspace OWNER sees the connections their
    # workspace's teams ride, org_shared or not: the credential banner's
    # Reauthorize CTA lands here, and anyone the re-auth gate admits must be
    # able to read the page hosting the button. Grant-index lookup,
    # fail-closed (empty set on error → clause skipped).
    esid = (connection_esid or "").strip()
    if esid:
        linked_workspaces = _workspaces_for_connection(esid)
        if linked_workspaces:
            owner_workspaces = _owner_grant_workspaces(viewer)
            if owner_workspaces and owner_workspaces.intersection(linked_workspaces):
                return True
            # anyone the re-auth gate admits must be able to READ the
            # page that hosts the Reauthorize button. The banner
            # offers Reconnect to holders of connection.reauthorize in the row's
            # workspace, and _viewer_can_reauthorize_connection admits an admin
            # whose resolved scope covers the connection (its clauses 3 and 3.5),
            # but this gate only knew OWNER grants — so a workspace_admin clicking
            # Reconnect landed on the 404 the route collapses denials into (prod,
            # 2026-09-07). Mirror both clauses here, by INTERSECTION with the
            # connection's linked workspaces — never see-all.
            if allowed_workspaces and linked_workspaces.intersection(allowed_workspaces):
                return True
            if _viewer_holds_reauthorize_permission(viewer, linked_workspaces):
                return True

    if not connection_source.get("org_shared"):
        return False

    # Org-shared path: the connection is visible to workspace members
    # the principal shares a workspace with. Intersect the viewer's
    # allowed workspaces with the principal's memberships.
    if not allowed_workspaces:
        return False
    principal_workspaces = set(WorkspaceMembershipService.get_workspaces_for_user(principal) or [])
    if not principal_workspaces:
        return False
    return bool(principal_workspaces.intersection(allowed_workspaces))


def _viewer_can_admin_connection(
    connection_source: dict,
    viewer_username: str | None = None,
    connection_esid: str | None = None,
) -> bool:
    """a WEAKER, admin-only capability than
    :func:`_viewer_can_see_connection`, granting an in-scope workspace/org
    admin the right to SEE which account a connection belongs to and to UNLINK
    it from a team — WITHOUT owning the principal's token.

    Returns True when any of:
      1. The viewer IS the principal (they already have full access).
      2. ``RoleFilterService.get_allowed_workspaces()`` is ``None`` (superuser /
         asset_admin see-all — already passes the read gate too).
      3. The connection's principal shares a workspace with the viewer's allowed
         set — i.e. the viewer is an admin over a workspace this connection lives
         in. This is ``_viewer_can_see_connection``'s org-shared branch MINUS the
         ``org_shared`` requirement: an admin can administer a connection in
         their workspace even if the principal never opted into org-sharing.

    This is deliberately NOT the read gate. It authorises exactly two low-risk
    actions that don't need the principal's Google/Meta token:
      * seeing the connected account identity (which account/email), and
      * team-unlink / disconnect (``disconnect_connector`` — clears the team's
        pointer; the ``de_connection`` token is left intact).

    It does NOT authorise the token-holder-only actions, which stay gated on
    principal identity per listing/selecting channels
    (``channels.list(mine=True)`` needs the owner's token) and REVOKING the
    connection token (``disconnect_connection`` — would kill another user's live
    credential; user decision 2026-07-01 keeps that principal-only).

    ``viewer_username`` and ``connection_esid`` are passed in by the route (no
    Flask-session dependency; the esid is required because get_connection
    returns only _source), matching ``_viewer_can_see_connection`` so the gate
    stays unit-testable.
    """
    if not connection_source:
        return False
    viewer = viewer_username or ""
    if not viewer:
        return False

    principal = connection_source.get("principal") or ""
    if viewer == principal:
        return True

    allowed_workspaces = RoleFilterService.get_allowed_workspaces()
    if allowed_workspaces is None:
        return True  # superuser / asset_admin see-all
    if not allowed_workspaces:
        return False  # deny-all floor

    # follow-up (2026-07-01) — the admin gate authorises over the
    # workspace the CONNECTION LIVES IN, i.e. the workspace(s) of the team(s)
    # that link this connection — NOT the principal's own workspace
    # memberships. The original code intersected the principal's memberships,
    # which broke the load-bearing case: on staging one team (kavanah-media) is
    # connected via an operator principal who is a member of workspace-a/b/c +
    # control-workspace — NOT kavanah-media — so a kavanah-media owner was
    # wrongly denied admin over a connection sitting in their own workspace.
    # A connection carries no workspace_esid of its own (0/27 on staging), so
    # we resolve it through the linking team doc. The esid must be passed by the
    # route: ConnectionService.get_connection returns only _source (no doc id),
    # so it can't be recovered from connection_source. Absent esid -> fail closed.
    esid = (connection_esid or "").strip()
    if not esid:
        return False
    linked_workspaces = _workspaces_for_connection(esid)
    if not linked_workspaces:
        return False
    return bool(linked_workspaces.intersection(allowed_workspaces))


def _owner_grant_workspaces(viewer_username: str) -> set[str]:
    """Tier 1 — workspace_esids where the viewer holds the
    ``workspace_owner`` grant.

    Wizard-signup owners hold the role as a System 2 grant
    (``de_authz_user_workspace_role``) with EMPTY ``session["roles"]``, so
    ``RoleFilterService.get_allowed_workspaces()`` resolves them to ``None``
    and no session-role clause can ever admit them. This reads the grant
    index directly — the same source ``auth.user_is_workspace_owner`` checks
    existence against — returning the workspace ids instead of a
    bool so callers can INTERSECT rather than see-all.

    Empty set on missing user, lookup error, or no grants — fail closed.
    """
    viewer = (viewer_username or "").strip()
    if not viewer:
        return set()
    from lib.es_helpers import get_elasticsearch_client

    es = get_elasticsearch_client()
    if es is None:
        return set()
    try:
        resp = es.search(
            index="de_authz_user_workspace_role",
            body={
                "query": {
                    "bool": {
                        "must": [
                            {"term": {"username": viewer}},
                            {"term": {"role_esid": "workspace_owner"}},
                        ]
                    }
                },
                "size": 100,
                "_source": ["workspace_esid"],
            },
        )
    except Exception as e:  # noqa: BLE001 — authz lookup, fail closed
        logging.warning("_owner_grant_workspaces lookup failed for %r: %s", viewer, e)
        return set()
    hits = (resp.get("hits") or {}).get("hits") or []
    out: set[str] = set()
    for h in hits:
        ws = ((h.get("_source") or {}).get("workspace_esid") or "").strip()
        if ws:
            out.add(ws)
    return out


def _viewer_holds_reauthorize_permission(viewer_username: str, linked_workspaces: set[str]) -> bool:
    """does the viewer hold ``Permission.CONNECTION_REAUTHORIZE``
    via a workspace-tier role on ANY of the workspaces this connection serves?

    Checked per linked workspace, never "anywhere": a workspace_admin of
    workspace A must not reach workspace B's credential. Shared by the
    re-auth gate (clause 3.5) and the read gate so the two cannot drift apart
    again — the page hosting the button and the button's POST admit the same
    people.
    """
    from asset_management.web.services.permission_resolver import PermissionResolver
    from asset_management.web.services.permissions_enum import Permission

    for ws in linked_workspaces:
        if PermissionResolver.has_permission_in_workspace(viewer_username, ws, Permission.CONNECTION_REAUTHORIZE):
            return True
    return False


def _viewer_can_reauthorize_connection(
    connection_source: dict,
    viewer_username: str | None = None,
    connection_esid: str | None = None,
) -> bool:
    """Tier 1 — who may re-authorise a connection.

    Deliberately STRICTER than :func:`_viewer_can_admin_connection`, which this
    does not reuse despite the overlap.

    ``_viewer_can_admin_connection`` treats ``get_allowed_workspaces() is None``
    as "superuser / asset_admin see-all". But ``None`` is overloaded: per its own
    docstring it is also returned for "non-privileged users with no scope to
    resolve", because ``should_apply_filter()`` returns False for "a user with no
    org/team roles at all who is also NOT privileged" — a misconfigured account.

    That ambiguity is tolerable two low-risk verbs (see which
    account a connection belongs to; unlink a team). It is NOT tolerable here:
    re-auth OVERWRITES a live credential, restricted it to the
    principal precisely to stop one user clobbering another's token. Widening the
    gate onto a helper that fails open on ambiguity would turn a misconfigured
    account into "can re-authorise anything".

    So this gate admits only unambiguous authority:

      1. the connection's principal (they own the credential already);
      2. an EXPLICIT superuser — the role, not the ``None`` sentinel;
      3. an admin whose resolved workspace list is a NON-EMPTY set that
         intersects the workspaces this connection lives in;
      4. a workspace OWNER — held as a System 2
         grant with empty ``session["roles"]``, read from the grant index
         via :func:`_owner_grant_workspaces` — of a workspace this
         connection serves. This is the clause that reaches customers.

    ``None`` denies. Fail closed.
    """
    if not connection_source:
        return False
    viewer = (viewer_username or "").strip()
    if not viewer:
        return False

    if viewer == (connection_source.get("principal") or ""):
        return True

    if "superuser" in (session.get("roles") or []):
        return True

    esid = (connection_esid or "").strip()
    if not esid:
        return False
    linked_workspaces = _workspaces_for_connection(esid)
    if not linked_workspaces:
        return False

    allowed_workspaces = RoleFilterService.get_allowed_workspaces()
    if allowed_workspaces and linked_workspaces.intersection(allowed_workspaces):
        # Clause 3 — admin whose RESOLVED scope covers this connection's
        # workspace. None / [] deliberately fall through: never see-all here.
        return True

    # Clause 3.5 — the viewer holds Permission.CONNECTION_REAUTHORIZE
    # via a workspace-tier role (new authz model) on a workspace this
    # connection serves. Checked PER LINKED WORKSPACE, never "anywhere" —
    # a workspace_admin of workspace A must not re-auth workspace B's
    # credential. This is the clause role changes flow through from now on;
    # clauses 3 and 4 remain for legacy-role admins and System-2 owners.
    if _viewer_holds_reauthorize_permission(viewer, linked_workspaces):
        return True

    # Clause 4 — a workspace OWNER of a workspace
    # this connection serves. Owners resolve to None above (System 2 grant,
    # empty session roles), so the grant index is the only place they exist.
    owner_workspaces = _owner_grant_workspaces(viewer)
    return bool(owner_workspaces.intersection(linked_workspaces))


# follow-up — connection-field names a team doc can use to point at its
# de_connection. The generic ``connection_esid`` plus the provider-specific
# fields ``_resolve_connection_esid_fallback`` reads, so the reverse
# lookup finds the linking team regardless of which field the wizard stamped.
_TEAM_CONNECTION_FIELDS = (
    "connection_esid",
    "google_ads_connection_esid",
    "ga4_connection_esid",
    "youtube_connection_esid",
    # Phase 3 — stamped by the Bitly wizard (configure_bitly).
    "bitly_connection_esid",
    # a LIST of TikTok connection esids; a term query matches any
    # element, so the reverse lookup works unchanged.
    "team_tiktok_connection_esids",
)


def _workspaces_for_connection(connection_esid: str) -> set[str]:
    """Return the set of ``workspace_esid``s of every team that links this
    connection.

    This is the connection's "home workspace(s)" — the basis the admin
    gate intersects against a viewer's allowed set. Two resolution paths,
    mirroring the two ways the connector card itself resolves a connection:

    A. Teams pointing DIRECTLY at the connection via any of
       ``_TEAM_CONNECTION_FIELDS`` (the wizard-stamped team-doc pointer).
    B. Teams linking a legacy asset this connection OWNS via
       ``de_connection_asset.legacy_asset_esid`` — the same backlink
       ``find_connection_esid_by_legacy_assets`` resolves the card with. A team
       resolved that way often carries NO team-doc pointer at all (staging
       2026-07-01: all five configured GA4 teams), so path A alone returned
       empty and the gate denied every non-superuser admin — landing a
       workspace owner back on the earlier "you don't manage this
       account" wall the gate exists to remove.

    Empty set when no team references the connection (orphaned / never-linked)
    or on any lookup error (fail-closed: the gate then denies, never a false
    grant).
    """
    if not connection_esid:
        return set()
    try:
        from lib.es_helpers import search_index

        # Query BOTH the bare field and its ``.keyword`` subfield for each
        # connection field. These fields are mapped inconsistently across
        # environments — on staging ``google_ads_connection_esid`` is
        # ``text``+keyword, so a bare ``term`` matches NOTHING (the /
        # trap) and the gads connection's home workspace was silently
        # lost -> the admin gate wrongly denied. A ``term`` on a non-existent
        # ``.keyword`` subfield simply returns no hits, so querying both is safe
        # whether the field is bare keyword (prod) or text+keyword (staging).
        should: list[dict] = []
        for f in _TEAM_CONNECTION_FIELDS:
            should.append({"term": {f: connection_esid}})
            should.append({"term": {f + ".keyword": connection_esid}})

        # Path B — the connection's legacy assets, matched against every
        # connector's team link field (single ``terms`` clause per field so the
        # clause count stays flat however many assets the connection owns).
        asset_esids = sorted(
            {
                esid
                for row in ConnectionService.list_assets_for_connection(connection_esid)
                if (esid := ((row.get("_source") or {}).get("legacy_asset_esid") or "").strip())
            }
        )
        if asset_esids:
            try:
                from web.services.connector_status_service import CONNECTORS
            except ImportError:
                from ...services.connector_status_service import CONNECTORS
            link_fields = sorted(
                {
                    field
                    for c in CONNECTORS
                    for k in ("team_esid_field", "fallback_team_esid_field", "ig_team_esid_field")
                    if (field := c.get(k))
                }
            )
            for f in link_fields:
                should.append({"terms": {f: asset_esids}})
                should.append({"terms": {f + ".keyword": asset_esids}})

        clause = {"bool": {"should": should, "minimum_should_match": 1}}
        resp = search_index("de_asset_teams", clause, size=100)
        hits = (resp.get("hits") or {}).get("hits") or []
        return {ws for h in hits if (ws := ((h.get("_source") or {}).get("workspace_esid") or "").strip())}
    except Exception as e:  # noqa: BLE001 — an authz-resolution error must fail closed, never falsely grant
        logging.warning("_workspaces_for_connection lookup failed for %s: %s", connection_esid, e)
        return set()


_HEALTH_PROVIDERS = ("facebook", "instagram")
_HEALTH_SKIP_STATUSES = frozenset({"superseded", "revoked"})


def _meta_connections_health() -> list[dict]:
    """every live Meta connection with the dates that govern it.

    Joins ``de_connection`` (token + data-access clocks) with the health
    row (the probe's verdict) so the November data-access batch can be
    SCHEDULED — soonest window first — instead of discovered when imports stop.
    Read-only; costs zero Meta calls (the probe already did them). Best-effort:
    a missing health row leaves the verdict blank rather than failing the page.
    """
    try:
        from lib import credential_health as ch
        from lib.es_helpers import search_index
    except ImportError:  # pragma: no cover - import boundary
        return []

    resp = search_index("de_connection", {"terms": {"provider": list(_HEALTH_PROVIDERS)}}, size=500)
    rows: list[dict] = []
    for hit in (resp.get("hits") or {}).get("hits") or []:
        esid = str(hit.get("_id") or "")
        src = hit.get("_source") or {}
        if (src.get("status") or "") in _HEALTH_SKIP_STATUSES:
            continue
        try:
            health = ch.get_health("de_connection", esid) or {}
        except Exception as e:  # noqa: BLE001 — the verdict is decoration; the dates are the point
            logging.warning("health lookup failed for %s: %s", esid, e)
            health = {}
        access_label, access_days = _format_expires_in_days(src.get("data_access_expires_at"))
        token_label, token_days = _format_expires_in_days(src.get("token_expires_at"))
        rows.append(
            {
                "esid": esid,
                "label": src.get("label") or esid,
                "principal": src.get("principal") or "",
                "provider": src.get("provider") or "",
                "status": src.get("status") or "",
                "token_kind": src.get("token_kind") or "user",
                "token_expires_at": _format_iso(src.get("token_expires_at")),
                "token_label": token_label,
                "data_access_expires_at": _format_iso(src.get("data_access_expires_at")),
                "data_access_label": access_label,
                "data_access_days": access_days,
                "health_status": health.get("status") or "",
                "health_reason": health.get("reason") or "",
                "last_probed_at": _format_iso(health.get("last_probed_at")),
            }
        )
    # Soonest data-access expiry first; rows with no recorded clock last (they
    # are the ones the backfill / next re-auth still has to populate).
    rows.sort(key=lambda r: (r["data_access_days"] is None, r["data_access_days"] or 0, r["label"]))
    return rows


@connections_bp.route("/health")
def connections_health() -> str:
    """operator view: Meta connections and their data-access dates.

    Superuser / asset_admin only; everyone else gets 404 so the route does not
    advertise itself. Registered before the ``/<connection_esid>`` catch-all by
    Werkzeug's static-first rule.

    the gate is the explicit session-role check
    ``_viewer_can_see_connection`` uses. It used to be
    ``get_allowed_workspaces() is None``, an overloaded signal: real superusers
    resolve to a list (so they got 404) while a roleless session resolves to
    ``None`` (so it saw every tenant's Meta connections and principals).
    """
    viewer = session.get("username") or ""
    if not viewer:
        abort(401)
    if not {"superuser", "asset_admin"} & set(session.get("roles") or []):
        abort(404)
    rows = _meta_connections_health()
    expiring = [r for r in rows if r["data_access_days"] is not None and r["data_access_days"] <= _DATA_ACCESS_WARN_DAYS]
    unrecorded = [r for r in rows if r["data_access_days"] is None]
    return render_template(
        "connections_health.html",
        rows=rows,
        expiring_count=len(expiring),
        unrecorded_count=len(unrecorded),
        warn_days=_DATA_ACCESS_WARN_DAYS,
    )


@connections_bp.route("/<connection_esid>")
def connection_detail(connection_esid: str) -> str:
    """read-only Manage Connection page.

    Renders the metadata stored on a single ``de_connection`` doc plus
    the list of ``de_connection_asset`` rows under it. The page is
    reachable only when the viewer passes ``_viewer_can_see_connection``;
    on missing connections or denied workspace scope, returns 404 so
    enumeration of connection ESIDs across tenants is collapsed into
    "not found" (mirrors the get_team_connectors 404 contract).
    """
    viewer = session.get("username") or ""
    if not viewer:
        abort(401)
    source = ConnectionService.get_connection(connection_esid)
    if not source:
        abort(404)
    if not _viewer_can_see_connection(
        source,
        viewer_username=viewer,
        connection_esid=connection_esid,
        viewer_roles=session.get("roles") or [],
    ):
        # Collapse "you don't have access" into "not found" so the page
        # can't be used to probe whether a given connection_esid exists.
        abort(404)

    provider = source.get("provider") or ""
    expires_label, expires_days = _format_expires_in_days(source.get("token_expires_at"))
    # Meta's 90-day data-access clock, the one the credential-health
    # probe counts down to. Shown beside the token expiry so the person who can
    # re-authorise sees the date that actually matters (a re-auth resets it).
    data_access_label, data_access_days = _format_expires_in_days(source.get("data_access_expires_at"))

    raw_scopes = source.get("scopes") or []
    scopes = [{"raw": s, "friendly": _SCOPE_FRIENDLY.get(s, s)} for s in raw_scopes if s]

    # de_connection_asset rows under this connection — used by the
    # "Assets connected" section so a user can see which ad accounts /
    # pages / properties this token grants the importer access to.
    asset_hits = ConnectionService.list_assets_for_connection(connection_esid)
    assets: list[dict] = []
    for hit in asset_hits:
        s = hit.get("_source") or {}
        assets.append(
            {
                "esid": hit.get("_id") or "",
                "asset_type": s.get("asset_type") or "",
                "provider_asset_id": s.get("provider_asset_id") or "",
                "label": s.get("label") or "",
            }
        )

    # Status badge — connection_service writes "active" / "expired" /
    # "revoked" / "pending_token"; the template renders a coloured
    # chip per value.
    status = source.get("status") or "active"

    viewer_is_principal = viewer == (source.get("principal") or "")
    # Action gating:
    # * Reauthorize: principal, superuser, scoped admin, or
    #   workspace owner — the same _viewer_can_reauthorize_connection gate
    #   start_reauthorize enforces, so the button renders exactly where the
    # POST would succeed. Supersedes principal-only rule; the
    #   token-overwrite risk it guarded against is handled by the coverage
    #   check + esid-addressed adopt (see start_reauthorize). Still only for
    #   providers in _REAUTH_SUPPORTED_PROVIDERS and never on a revoked
    #   connection (re-auth there would be confusing — user should re-run
    #   the wizard's set-up flow instead).
    # * Disconnect: only the principal, only for
    #   OAuth-token-bearing providers, not for an already-revoked connection
    #   (idempotent second click would no-op anyway, but the button is
    #   misleading).
    can_reauthorize = (
        _viewer_can_reauthorize_connection(source, viewer, connection_esid)
        and provider in _REAUTH_SUPPORTED_PROVIDERS
        and status != "revoked"
    )
    can_disconnect = viewer_is_principal and provider in _DISCONNECT_SUPPORTED_PROVIDERS and status != "revoked"

    # a partial-coverage re-auth is STAGED by the callback (
    # Tier 2) and the browser is sent back here with ?reauth=partial. Until now
    # the page rendered nothing for that state and had no accept / discard
    # controls (the only UI calling resolve_reauth_coverage was the Data
    # Connectors system-user paste flow), so the staged token sat unresolved
    # and the click read as "nothing happened" (prod, 2026-09-07). Hand the
    # template what a human needs to decide — who, when, which assets the new
    # token cannot reach and why — and NEVER the token itself.
    pending_reauth = _pending_reauth_context(source, connection_esid, can_resolve=can_reauthorize)

    context = {
        "connection_esid": connection_esid,
        "provider": provider,
        "provider_label": _PROVIDER_LABELS.get(provider, provider or "Connection"),
        "provider_account_id": source.get("provider_account_id") or "",
        "label": source.get("label") or "",
        "status": status,
        "expires_label": expires_label,
        "expires_days": expires_days,
        "token_expires_at": _format_iso(source.get("token_expires_at")),
        "data_access_expires_at": _format_iso(source.get("data_access_expires_at")),
        "data_access_label": data_access_label,
        "data_access_days": data_access_days,
        "data_access_warn_days": _DATA_ACCESS_WARN_DAYS,
        "scopes": scopes,
        "org_shared": bool(source.get("org_shared")),
        "principal": source.get("principal") or "",
        "created_at": _format_iso(source.get("created_at")),
        "updated_at": _format_iso(source.get("updated_at")),
        "assets": assets,
        # Viewer == principal gates the management actions. Workspace
        # admins viewing an org-shared connection see metadata only.
        "viewer_is_principal": viewer_is_principal,
        "can_reauthorize": can_reauthorize,
        "can_disconnect": can_disconnect,
        # The URL the JS POSTs to for disconnect — passed through so the
        # template doesn't have to know the blueprint name.
        "disconnect_url": url_for("connections.disconnect_connection", connection_esid=connection_esid),
        # Reauthorize button is a plain <a href>; the route mints a state
        # token in flask.session and redirects to Meta's authorize URL.
        "reauthorize_url": url_for("connections.start_reauthorize", connection_esid=connection_esid),
        "pending_reauth": pending_reauth,
    }
    return render_template("connection_detail.html", **context)


def _pending_reauth_context(source: dict, connection_esid: str, *, can_resolve: bool) -> dict | None:
    """what the detail page shows for a staged partial-coverage re-auth.

    ``None`` when nothing is staged. Otherwise who staged it and when, the
    coverage tally, the assets the new token could NOT reach (name, type, Meta
    error), whether THIS viewer may resolve it (the same gate as the POST, so
    the buttons render exactly where the POST would succeed), and the resolve
    URL. The staged token is deliberately not part of the result.
    """
    if not source.get("pending_reauth_token_enc"):
        return None
    coverage = source.get("pending_reauth_coverage") or {}
    failed = [
        {
            "asset_type": a.get("asset_type") or "",
            "asset_id": a.get("asset_id") or "",
            "asset_name": a.get("asset_name") or a.get("asset_id") or "",
            # Meta appends a "Refer to https://developers.facebook.com/..." tail; drop it, keep the reason.
            "error": re.split(r"\s+Refer to https?://", a.get("error") or "", maxsplit=1)[0][:200],
            "error_code": a.get("error_code"),
        }
        for a in (coverage.get("assets") or [])
        if not a.get("ok")
    ]
    return {
        "by": source.get("pending_reauth_by") or "",
        "at": _format_iso(source.get("pending_reauth_at")),
        "covered": coverage.get("covered", 0),
        "total": coverage.get("total", 0),
        "failed": failed,
        "can_resolve": bool(can_resolve),
        "resolve_url": url_for("connections.resolve_reauth_coverage", connection_esid=connection_esid),
    }


# ── Reauthorize ───────────────────────────────────────────────


def _start_youtube_reauthorize(connection_esid: str, source: dict):
    """in-place Google/YouTube reauthorize for an existing connection.

    Deliberately thin: rather than building a second Google OAuth roundtrip, it
    mints a fresh ``youtube_account_auth_state`` on the existing account doc and
    hands off to fb_oauth_server's ``/youtube/<auth_url>`` initiate leg. That
    reuses the ALREADY-REGISTERED Google redirect URI (no OAuth-app admin
    change) and, more importantly, inherits every safety check the setup
    callback grew:

    * the ping-pong guard — refuses to swap this doc's token to an
      identity that does not own the channels already linked to it. This is the
      exact failure that produced the sprawl, so a reauth leg that
      bypassed it would reintroduce the bug it exists to prevent.
    * identity dedup and owned-channel-set stamping.

    Token destination: ``de_asset_youtube_accounts.youtube_account_token`` only
    — that is what the YouTube importer reads, so it is canonical. The
    ``de_connection`` row is intentionally NOT mirrored here; its YouTube
    natural key is currently the Google *email*, which is not unique per
    account doc (two prod docs for one partner share one email, verified
    2026-07-31), so mirroring would misattribute the token between them.
    Re-keying to the account esid + dual-write is tracked separately.

    Resolves the account doc via the connection's ``legacy_asset_esid`` pointer
    (written by ``connection_backfill``) rather than by email, for the same
    non-uniqueness reason.
    """
    account_esid = str(source.get("legacy_asset_esid") or "").strip()
    if not account_esid:
        # A YouTube connection with no legacy pointer predates or bypassed the
        # backfill; there is no doc to re-auth against. Surface it as a real
        # error rather than silently starting a roundtrip that can't land.
        logging.warning(
            "reauth: youtube connection %s has no legacy_asset_esid",
            connection_esid,
        )
        abort(409, description="This YouTube connection is not linked to an account asset.")

    try:
        from web.services.asset_service import AssetService
        from web.services.form_service import FormService
    except ImportError:
        from ...services.asset_service import AssetService
        from ...services.form_service import FormService

    account = AssetService.get_by_id("de_asset_youtube_accounts", account_esid)
    if not account:
        logging.warning(
            "reauth: youtube account %s (from connection %s) not found",
            account_esid,
            connection_esid,
        )
        abort(409, description="The YouTube account asset for this connection no longer exists.")

    auth_url_path = str(account.get("youtube_account_auth_url") or "").strip()
    if len(auth_url_path) != 15:
        # The initiate leg hard-404s on any other length, so fail here with a
        # readable reason instead of bouncing the user to a bare 404.
        logging.warning(
            "reauth: youtube account %s has unusable auth_url (len=%d)",
            account_esid,
            len(auth_url_path),
        )
        abort(409, description="This YouTube account is missing its authorization link.")

    # Mint a FRESH state and persist it on the doc. The callback looks the
    # account up BY state, so rotating it here means (a) the stale state from
    # the original setup can't be replayed, and (b) this reauth is the only
    # in-flight authorization that can land on this doc.
    account["youtube_account_auth_state"] = FormService.generate_random_string(25)
    # Mark this roundtrip as a REAUTH so the callback can tailor its success
    # copy. The shared success page tells the user to "return to the wizard to
    # finish setup" — correct for a first-time setup, wrong here:
    # a reauth re-uses the existing channel selection, so there is nothing left
    # to confirm and that copy would send the user hunting for a step that
    # doesn't exist. The callback pops this flag when it consumes the state.
    account["youtube_account_reauth_pending"] = True
    AssetService.update("de_asset_youtube_accounts", account_esid, account)

    # Reuse the existing config reader (same source the wizard's YouTube popup
    # URL uses) rather than a second copy of the try/except import dance.
    try:
        from web.services.connector_status_service import _get_fboauth_hostname
    except ImportError:
        from ...services.connector_status_service import _get_fboauth_hostname

    hostname = _get_fboauth_hostname()
    if not hostname:
        logging.error("reauth: fboauth hostname not configured")
        abort(503, description="OAuth provider is temporarily unavailable.")

    logging.info(
        "reauth: starting youtube roundtrip connection=%s account=%s",
        connection_esid,
        account_esid,
    )
    # nosemgrep: tainted-url-host - hostname from server config, not request
    return redirect(f"https://{hostname}/youtube/{auth_url_path}")


def _start_tiktok_reauthorize(connection_esid: str):
    """in-place TikTok reauthorize for an existing connection.

    Seeds a fresh single-use OAuth state ON THIS ROW and hands off to
    fb_oauth_server's ``/tiktok/<auth_url>`` page, the same leg the wizard uses.
    The callback finds the row by that state and refuses a token for any TikTok
    account other than the one this row already names, so a reauth can refresh
    the grant but never move linked teams onto someone else's account.
    """
    seeded = ConnectionService.seed_oauth_state(connection_esid)
    if not seeded.get("success"):
        logging.warning("reauth: seed_oauth_state failed for %s: %s", connection_esid, seeded.get("error"))
        abort(503, description="Could not start the TikTok authorization. Try again.")

    try:
        from web.services.connector_status_service import _get_fboauth_hostname
    except ImportError:
        from ...services.connector_status_service import _get_fboauth_hostname

    hostname = _get_fboauth_hostname()
    if not hostname:
        logging.error("reauth: fboauth hostname not configured")
        abort(503, description="OAuth provider is temporarily unavailable.")
    logging.info("reauth: starting tiktok roundtrip connection=%s", connection_esid)
    # nosemgrep: tainted-url-host - hostname from server config, not request
    return redirect(f"https://{hostname}/tiktok/{seeded['auth_url']}")


@connections_bp.route("/<connection_esid>/start-reauth")
def start_reauthorize(connection_esid: str):
    """kick off a Meta OAuth roundtrip against an existing
    connection.

    Mirrors ``analytics.onboarding_step7_start_oauth`` but:

    * Operates on an existing ``de_connection`` row (not a wizard
      session). The connection's natural key (principal, provider,
      provider_account_id) is what the callback's ``upsert_connection``
      keys on, so a successful re-OAuth refreshes the token in place.

    * Stores the OAuth state token in the Flask session keyed by
      ``connection_esid`` rather than on a ``de_wizard_session`` blob —
      the user isn't running a wizard, and bloating
      ``de_wizard_session`` with non-wizard rows would muddy the
      analytics that key off that index. The state-mismatch defence
      is the same (state is single-use; popped on callback).

    * Records a ``return_to=connections/<esid>`` hint on the Flask
      session so the shared callback at
      ``analytics.onboarding_step7_oauth_callback`` knows to redirect
      back to the connection detail page rather than to /onboarding/step-7.

    Authorisation: the connection's principal, a superuser,
    a workspace admin over the workspace the connection lives in, or a
    workspace OWNER of a workspace it serves — resolved by
    ``_viewer_can_reauthorize_connection`` (strict, fail-closed).

    This deliberately widens principal-only gate. That gate made the
    button a dead end whenever the principal was unavailable, which in practice
    is most of the time: one person set up most connections, so nobody else could
    ever fix them. ``_viewer_can_admin_connection``'s docstring still describes
    reauthorise as principal-only under that description is superseded
    here.

    The original objection to widening ("it would silently overwrite their stored
    token") is answered other half rather than by refusing: the
    callback now writes onto THIS row by esid via ``adopt_reauth_token`` and
    records ``last_reauth_by``, and a partial-coverage result is staged pending
    explicit acknowledgment instead of being committed. Overwriting a working
    token with a worse one is the risk that mattered, and the coverage check —
    not the identity of the clicker — is what actually prevents it.

    Provider gating (``_REAUTH_SUPPORTED_PROVIDERS``) makes sure we don't try to
    run a Meta dialog for, say, Google Ads.
    """
    viewer = session.get("username") or ""
    if not viewer:
        abort(401)

    source = ConnectionService.get_connection(connection_esid)
    if not source:
        abort(404)
    if not _viewer_can_reauthorize_connection(source, viewer, connection_esid):
        # Match the read-side 404 pattern — don't leak that the connection
        # exists to someone with no admin rights over it.
        abort(404)

    # refuse to run an OAuth round-trip on a system-user connection.
    #
    # This is a SILENT DOWNGRADE guard, not a tidiness check. The OAuth callback
    # would succeed, the user would see the green "Reauthorized" page, and the
    # row's non-expiring SYSTEM_USER token would have been replaced by a USER
    # token on Meta's 90-day data-access clock. Nothing would look wrong until
    # imports stopped ~3 months later — and the connection would by then be back
    # on the exact treadmill this path exists to escape.
    #
    # A new system-user token is pasted, not OAuth'd (Meta has no API to mint one
    # on a user's behalf), so the correct action is Business Manager, not this
    # button. 409 rather than 400: the request is well-formed, it conflicts with
    # the resource's current state.
    if (source.get("token_kind") or "") == "system_user":
        logging.info(
            "refused OAuth reauth on system-user connection %s (viewer=%s)",
            connection_esid,
            viewer,
        )
        abort(
            409,
            description=(
                "This connection uses a Business Manager system user token, which "
                "does not expire. Reconnecting through Facebook would replace it "
                "with a token that expires every 90 days. To rotate it, generate a "
                "new system user token in Meta Business Manager and paste it in."
            ),
        )

    provider = source.get("provider") or ""
    if provider not in _REAUTH_SUPPORTED_PROVIDERS:
        abort(400, description="Reauthorize is not available for this provider.")

    # YouTube reauth is a Google roundtrip, not a Meta one. Diverge
    # before any Meta-app lookup happens (get_meta_app would 503 for a
    # YouTube connection, which reads as an outage rather than a wrong turn).
    if provider in _GOOGLE_REAUTH_PROVIDERS:
        return _start_youtube_reauthorize(connection_esid, source)
    if provider == "tiktok":
        return _start_tiktok_reauthorize(connection_esid)

    # Wizard's OAuth service knows the Meta app + scopes + state-token
    # generator. We use the FB-side default scopes (Meta covers both
    # facebook and instagram providers — they share the Meta app).
    try:
        from asset_management.web.services.wizard_oauth_service import WizardOAuthService
        from asset_management.web.services.connector_scope_manifest_service import (
            ConnectorScopeManifestService,  # noqa: F401  (scope source-of-truth, kept for parity)
        )
    except ImportError:
        from ...services.wizard_oauth_service import WizardOAuthService
        from ...services.connector_scope_manifest_service import (  # noqa: F401
            ConnectorScopeManifestService,
        )

    meta_app = WizardOAuthService.get_meta_app()
    if meta_app is None:
        logging.warning(
            "reauth: WizardOAuthService.get_meta_app returned None for %s",
            connection_esid,
        )
        abort(503, description="OAuth provider is temporarily unavailable.")

    # Mint state token and stash on flask.session keyed by connection
    # so a multi-tab user reauthorising different connections doesn't
    # clobber each other's state. The callback pops by the key it gets
    # from session["oauth_reauth_active_esid"].
    oauth_state = secrets.token_urlsafe(32)
    session[f"oauth_reauth_state:{connection_esid}"] = oauth_state
    session[f"oauth_reauth_app_esid:{connection_esid}"] = meta_app.esid
    session["oauth_reauth_active_esid"] = connection_esid

    # Re-request the full default scope set. We could narrow to the
    # currently-granted scopes on the connection doc, but the user is
    # explicitly reauthorising so the right default is "ask for
    # everything the wizard would ask for on a fresh sign-up" — that
    # also closes the gap if Meta has added a scope to the manifest
    # since the original grant.
    scopes = tuple(sorted(set(WizardOAuthService.default_scopes_for("meta_ads"))))

    # OAuth 2.0 redirect_uri MUST be absolute and match the value sent
    # at authorize. The wizard's callback is already a pre-registered
    # Meta redirect URI; reusing it (rather than registering a second
    # URI for /connections/<esid>/oauth-callback) keeps the Meta-app
    # admin surface unchanged. The callback distinguishes wizard vs
    # reauth flows by looking for the reauth keys in flask.session.
    # nosemgrep: flask-url-for-external-true - OAuth redirect_uri must be absolute; host fixed by nginx
    redirect_uri = url_for("analytics.onboarding_step7_oauth_callback", _external=True)
    # the registered Meta redirect URI is the BARE
    # https://<host>/onboarding/step-7/oauth-callback. This
    # page is deep-linked from Data Connectors under /assets/connections/…,
    # so SCRIPT_NAME=/assets and url_for prepends the prefix; Meta then
    # refuses the dialog with "URL Blocked" and the callback runs with no
    # code. Same canonicalisation the wizard and data-connectors starts do.
    redirect_uri = redirect_uri.replace("/assets/onboarding/", "/onboarding/", 1)
    authorize_url = WizardOAuthService.build_authorize_url(
        client_id=meta_app.client_id,
        redirect_uri=redirect_uri,
        state=oauth_state,
        scopes=scopes,
        # Force account-picker so the user can switch the underlying
        # Meta account if they want to. Reauthorise from the
        # connection page implies "I want to redo this" — the default
        # Meta behaviour of silently honouring the existing FB session
        # would be the wrong UX.
        reauthenticate=True,
    )
    return redirect(authorize_url)


# ── connect Meta with a Business Manager system-user token ────────


@connections_bp.route("/system-user", methods=["POST"])
def connect_system_user_token():
    """Accept a pasted Meta system-user token for a team.

    The advanced alternative to the wizard's OAuth path. A system-user token has
    no expiry and no data-access clock, so a team on this route leaves the 90-day
    re-auth treadmill entirely. Meta has no API to mint one on a user's behalf,
    so paste is the only possible shape.

    Sequence, and the order matters — nothing is persisted until the token has
    been proven:

      1. validate  — reject a USER token outright (accepting one would silently
                     defeat the whole feature: works for 90 days, then dies,
                     while the operator believes it never expires) and reject a
                     token missing required scopes (three of six real
                     system users lack ``read_insights``, and without it Meta
                     returns 200 with an empty body — no error to alarm on);
      2. coverage  — probe the token against the assets THIS TEAM declares,
                     reusing prober;
      3. commit or stage — full coverage commits; partial coverage is STAGED and
                     needs explicit acknowledgment, exactly as does for
                     re-auth. Acknowledgment reuses the existing
                     ``/connections/<esid>/reauth-coverage`` route rather than a
                     parallel one, so there is a single accept/discard path.

    Returns JSON. Never echoes the token, in any branch, including errors.

     (security-scan CONN-003): authorization runs FIRST, before the
    token is even looked at. This route used to check only for a session, so a
    caller holding ``mc_data_connectors`` in workspace A could name a team in
    workspace B and, because the team's existing connection is reused, replace
    that tenant's live Meta credential with their own token. Three gates now
    sit ahead of step 1, in this order:

      0a. ``_require_data_connectors``  — the same permission every wizard
          configure endpoint requires;
      0b. ``_check_team_for_wizard``     — workspace narrowing + team-admin
          scope; a deny collapses to 404 exactly as the
          wizard routes do, so team esids cannot be probed across tenants;
      0c. ``_viewer_can_reauthorize_connection`` on the team's EXISTING
          connection, if it has one — reusing that row overwrites its
          credential, so it needs the same authority as the Reauthorize
          button. This one is a 403 with a plain message
          rather than a 404: the caller has already proven they may see the
          team, so the connection's existence is not a secret from them.

    Nothing past the gates — not even Meta's ``/debug_token`` — runs for a
    caller who fails any of them.
    """
    viewer = session.get("username") or ""
    if not viewer:
        return jsonify({"success": False, "error": "Not authenticated"}), 401

    # -- 0. authorize before anything else -----------------------
    # Imported lazily from the wizard blueprint, the same direction that module
    # already imports helpers from this one, so there is no module-level cycle.
    try:
        from web.blueprints.data_connectors.routes import (
            _check_team_for_wizard,
            _require_data_connectors,
        )
    except ImportError:
        from ..data_connectors.routes import (  # type: ignore[no-redef]
            _check_team_for_wizard,
            _require_data_connectors,
        )

    _require_data_connectors()

    payload = request.get_json(silent=True) or request.form or {}
    team_esid = (payload.get("team_esid") or "").strip()
    token = (payload.get("token") or "").strip()

    if not team_esid:
        return jsonify({"success": False, "error": "team_esid is required"}), 400

    authz_err = _check_team_for_wizard(team_esid)
    if authz_err is not None:
        return authz_err

    if not token:
        return jsonify({"success": False, "error": "No token was provided."}), 400

    from lib.es_helpers import get_document_by_id

    team = get_document_by_id("de_asset_teams", team_esid) or {}
    existing_esid, existing_source = _existing_team_connection(team)
    if existing_esid and not _viewer_can_reauthorize_connection(existing_source, viewer, existing_esid):
        logging.warning(
            "%s may not re-authorise connection %s already held by team %s; refused",
            viewer,
            existing_esid,
            team_esid,
        )
        return (
            jsonify(
                {
                    "success": False,
                    "error": (
                        "This team's Meta connection belongs to another user. Only that user, "
                        "a workspace owner or an administrator can replace its token."
                    ),
                }
            ),
            403,
        )

    from asset_management.web.services.system_user_token_service import (
        TOKEN_KIND_SYSTEM_USER,
        team_assets_for_coverage,
        validate_system_user_token,
    )
    from asset_management.web.services.wizard_oauth_service import WizardOAuthService

    meta_app = WizardOAuthService.get_meta_app()
    if meta_app is None:
        return jsonify({"success": False, "error": "OAuth provider is temporarily unavailable."}), 503

    # --- 1. validate before anything is written -----------------------------
    result = validate_system_user_token(token, meta_app.client_id, meta_app.client_secret)
    if not result.ok:
        # 422 for "the token is wrong" vs 400 for "the request is wrong" — the
        # UI branches on this to decide whether to keep the paste box populated.
        return (
            jsonify(
                {
                    "success": False,
                    "code": result.code,
                    "error": result.message,
                    "token_type": result.token_type,
                    "missing_scopes": result.missing_scopes,
                    "lost_capabilities": result.lost_capabilities,
                }
            ),
            503 if result.code == "unreachable" else 422,
        )

    # --- 2. resolve or create the connection row ----------------------------
    esid = _resolve_system_user_connection(team_esid, viewer, result.system_user_id, team=team, existing_esid=existing_esid)
    if not esid:
        return jsonify({"success": False, "error": "Could not create the connection."}), 500

    # --- 3. coverage against what the TEAM needs ----------------------------
    assets = team_assets_for_coverage(team_esid)
    coverage = _system_user_coverage(token, assets)

    from asset_management.web.blueprints.analytics.routes import _reauth_decision

    if _reauth_decision(coverage) == "commit":
        ok = ConnectionService.adopt_reauth_token(
            esid,
            actor=viewer,
            oauth_token=token,
            token_expires_at=None,  # a system-user token has no expiry, by design
            scopes=list(result.granted_scopes),
            coverage=coverage,
            token_kind=TOKEN_KIND_SYSTEM_USER,
        )
        outcome = "committed"
    else:
        ok = ConnectionService.stage_reauth_token(
            esid,
            actor=viewer,
            oauth_token=token,
            token_expires_at=None,
            scopes=list(result.granted_scopes),
            coverage=coverage,
            token_kind=TOKEN_KIND_SYSTEM_USER,
        )
        outcome = "staged"

    if not ok:
        return jsonify({"success": False, "error": "Could not save the token."}), 500

    try:
        from asset_management.web.services.audit_log_service import AuditLogService
    except ImportError:  # pragma: no cover
        from web.services.audit_log_service import AuditLogService

    AuditLogService.log_event(
        viewer,
        "connection_system_user_token_set",
        True,
        ip_address=request.remote_addr,
        user_agent=request.headers.get("User-Agent", ""),
        detail=(
            f"analytics_event=connection.system_user.{outcome} "
            f"connection_esid={esid} team_esid={team_esid} "
            f"system_user_id={result.system_user_id} "
            f"coverage={coverage.get('covered', 0)}/{coverage.get('total', 0)}"
        ),
    )

    try:
        from asset_management.web.services.credential_banner_service import invalidate_cache

        invalidate_cache()
    except Exception as exc:  # noqa: BLE001
        logging.warning("banner cache invalidation failed: %s", exc)

    return jsonify(
        {
            "success": True,
            "outcome": outcome,
            "connection_esid": esid,
            "token_type": result.token_type,
            "system_user_id": result.system_user_id,
            "granted_scopes": list(result.granted_scopes),
            "coverage": coverage,
            # The UI shows accept/discard only when staged; acknowledgment goes
            # to the existing route.
            "needs_acknowledgment": outcome == "staged",
        }
    )


def _system_user_coverage(token: str, assets: list) -> dict:
    """Probe a candidate token against a team's assets.

    Mirrors ``_reauth_coverage`` but takes the asset list directly,
    because a brand-new connection has no ``de_connection_asset`` rows to read.
    Same prober, same ``summarize`` shape, same error semantics — an internal
    failure sets ``error`` so the caller STAGES rather than commits ("could not
    check" must never be mistaken for "nothing to check").
    """
    try:
        from lib.credential_prober import TokenProbe, probe_assets, summarize

        probes = probe_assets(token, assets)
        out = summarize(TokenProbe(reachable=True, is_valid=True), probes)
        out["error"] = False
        return out
    except Exception as exc:  # noqa: BLE001
        logging.warning("coverage probe failed: %s", exc)
        return {"token": {}, "assets": [], "covered": 0, "total": 0, "fully_covered": False, "error": True}


def _existing_team_connection(team: dict) -> tuple[str | None, dict | None]:
    """Return ``(esid, source)`` of the connection this team already points at,
    or ``(None, None)`` when it has none that still resolves.

    Split out of :func:`_resolve_system_user_connection` so the
    route can run the re-authorise gate on the existing row BEFORE it spends a
    Meta call validating the pasted token.
    """
    for field in ("connection_esid", "wizard_connection_esid"):
        existing = (team or {}).get(field)
        if not existing:
            continue
        source = ConnectionService.get_connection(existing)
        if source:
            return str(existing), source
    return None, None


def _resolve_system_user_connection(
    team_esid: str,
    viewer: str,
    system_user_id: str,
    *,
    team: dict | None = None,
    existing_esid: str | None = None,
) -> str:
    """Return the de_connection esid this team's Meta token should live on.

    Reuses the team's existing connection when it has one, so switching an
    established team onto a system-user token UPGRADES the row in place — its
    asset links, org_shared flag and history survive, and every team pointer
    keeps resolving. Creating a second row instead is precisely the duplication
     exist to stop.

    the caller passes the team doc and the already-gated
    ``existing_esid`` it resolved up front; both are re-derived here only when
    omitted, so the function still stands alone.
    """
    if team is None:
        from lib.es_helpers import get_document_by_id

        team = get_document_by_id("de_asset_teams", team_esid) or {}
    if existing_esid is None:
        existing_esid, _ = _existing_team_connection(team)
    if existing_esid:
        return existing_esid

    created = ConnectionService.create_pending_connection(
        principal=viewer,
        provider="facebook",
        provider_account_id=system_user_id or f"system-user-{team_esid}",
        label=f"meta_ads — system user ({team.get('team_title') or team_esid})",
    )
    # create_pending_connection returns {"success": bool, "esid": ..., "error": ...}.
    # Check success rather than reading esid blindly — a failed create would
    # otherwise return "" and surface as a generic 500 with no reason logged.
    if not isinstance(created, dict) or not created.get("success"):
        logging.warning(
            "create_pending_connection failed for team %s: %s",
            team_esid,
            (created or {}).get("error") if isinstance(created, dict) else created,
        )
        return ""
    return str(created.get("esid") or "")


# ── Tier 2: acknowledge a partial-coverage re-auth ─────────────────


@connections_bp.route("/<connection_esid>/reauth-coverage", methods=["POST"])
def resolve_reauth_coverage(connection_esid: str):
    """Accept or discard a staged partial-coverage re-auth token.

    The callback stages rather than commits when the new token reaches only some
    of the connection's assets, so this is the "explicit acknowledgment before
    commit" requires. Two outcomes:

    * ``action=accept``  — promote the staged token. The user has seen exactly
      which assets it cannot reach and chosen to proceed anyway.
    * ``action=discard`` — drop it. The PREVIOUS token stays live and untouched,
      which is the whole point of staging: a rushed re-auth must not be able to
      downgrade a working connection by accident.

    Same widened gate as start_reauthorize — whoever may re-authorise may resolve
    the result of their own re-authorisation.
    """
    viewer = session.get("username") or ""
    if not viewer:
        return jsonify({"success": False, "error": "Not authenticated"}), 401

    source = ConnectionService.get_connection(connection_esid)
    if not source:
        return jsonify({"success": False, "error": "Connection not found"}), 404
    if not _viewer_can_reauthorize_connection(source, viewer, connection_esid):
        return jsonify({"success": False, "error": "Connection not found"}), 404

    if not source.get("pending_reauth_token_enc"):
        return jsonify({"success": False, "error": "No pending re-authorisation to resolve"}), 409

    action = (request.form.get("action") or (request.json or {}).get("action") or "").strip()
    if action not in ("accept", "discard"):
        return jsonify({"success": False, "error": "action must be accept or discard"}), 400

    # Imported locally to match this blueprint's existing pattern of not pulling
    # service modules in at import time (the module-level imports here are Flask
    # + ConnectionService only).
    try:
        from asset_management.web.services.audit_log_service import AuditLogService
    except ImportError:  # pragma: no cover — alternate package root, as in analytics/routes.py
        from web.services.audit_log_service import AuditLogService

    coverage = source.get("pending_reauth_coverage") or {}
    if action == "accept":
        ok = ConnectionService.promote_staged_reauth(connection_esid, actor=viewer)
    else:
        ok = ConnectionService.discard_staged_reauth(connection_esid)

    AuditLogService.log_event(
        viewer,
        "connection_reauth_coverage_resolved",
        ok,
        ip_address=request.remote_addr,
        user_agent=request.headers.get("User-Agent", ""),
        detail=(
            f"analytics_event=connection.reauth.coverage_{action} "
            f"connection_esid={connection_esid} "
            f"coverage={coverage.get('covered', 0)}/{coverage.get('total', 0)}"
        ),
    )
    if not ok:
        return jsonify({"success": False, "error": "Could not resolve the pending re-authorisation"}), 500

    # the banner reads cached health; a resolved re-auth should not
    # keep advertising a problem the user just acted on.
    try:
        from asset_management.web.services.credential_banner_service import invalidate_cache

        invalidate_cache()
    except Exception as exc:  # noqa: BLE001 — cache hygiene must not fail the action
        logging.warning("banner cache invalidation failed: %s", exc)

    return jsonify({"success": True, "action": action})


# ── Disconnect ────────────────────────────────────────────────


@connections_bp.route("/<connection_esid>/disconnect", methods=["POST"])
def disconnect_connection(connection_esid: str):
    """soft-delete a connection by flipping status to
    ``revoked``. Pauses imports because the importer-side resolvers in
    ``import_scripts/facebook/get_insights.py``,
    ``import_scripts/facebook/get_page_insights.py``,
    ``import_scripts/instagram/get_instagram_insights.py``, plus the
    four on-demand kickoff resolvers in
    ``import_scripts/{echo,google_ads,google_analytics,facebook}/run_daily_imports.py``,
    short-circuit when they see ``status == "revoked"`` on the
    connection doc.

    Returns JSON. Only the principal may disconnect — workspace admins
    on an org-shared connection cannot revoke someone else's auth.

    Soft-delete (status flip) rather than hard delete because:

    * The legacy-asset back-pointers and audit history on
      ``de_connection_asset`` are worth preserving.
    * The importer needs to *see* the revoked row so it can log "skipped
      because revoked" rather than fall back to legacy resolvers and
      potentially re-import with a different (legacy) credential.
    * A future "undo disconnect" affordance (out of scope this PR) can
      flip status back to active.
    """
    viewer = session.get("username") or ""
    if not viewer:
        return jsonify({"success": False, "error": "Not authenticated"}), 401

    source = ConnectionService.get_connection(connection_esid)
    if not source:
        return jsonify({"success": False, "error": "Connection not found"}), 404
    if (source.get("principal") or "") != viewer:
        # Same enumeration defence as the read path — collapse "wrong
        # principal" to "not found" rather than leaking existence.
        return jsonify({"success": False, "error": "Connection not found"}), 404

    if (source.get("status") or "") == "revoked":
        # Idempotent: a second click after a refresh shouldn't error.
        return jsonify({"success": True, "already_revoked": True})

    # read the TikTok refresh token BEFORE the status flip: the
    # grant is ended at TikTok too (below), and that needs the token.
    tiktok_token = ConnectionService.get_decrypted_token(connection_esid) if source.get("provider") == "tiktok" else None

    ok = ConnectionService.set_status(connection_esid, "revoked")
    if not ok:
        logging.warning("disconnect: set_status failed for %s", connection_esid)
        return jsonify({"success": False, "error": "Disconnect failed"}), 500

    if tiktok_token:
        # Best-effort: DEAM has stopped using the grant either way (the importer
        # skips revoked rows); this also withdraws it on TikTok's side so the
        # account owner's TikTok settings no longer list DEAM.
        try:
            from lib.tiktok_client import TikTokError, revoke as tiktok_revoke

            try:
                tiktok_revoke(tiktok_token)
                logging.info("disconnect: revoked the TikTok grant for %s", connection_esid)
            except TikTokError as e:
                logging.warning("disconnect: TikTok revoke failed for %s: %s", connection_esid, e)
        except ImportError as e:  # pragma: no cover - import boundary
            logging.warning("disconnect: TikTok client unavailable: %s", e)
    if source.get("provider") == "tiktok":
        try:
            from web.services import tiktok_account_projection
        except ImportError:
            from ...services import tiktok_account_projection
        tiktok_account_projection.sync(connection_esid)

    logging.info(
        "disconnect: principal=%s connection_esid=%s provider=%s",
        viewer,
        connection_esid,
        source.get("provider") or "",
    )
    return jsonify({"success": True})
