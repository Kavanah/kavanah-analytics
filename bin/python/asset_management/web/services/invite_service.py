"""Invite Service.

Sender-side surface for the in-app invite-teammates modal. Closes the
honest-caveat gap left open (checklist row with placeholder
``action_url="/assets/profile"``) (org-owner end-of-wizard
``?invite=open`` flag with nothing to consume it).

What lives here:

* The ``de_authz_invite`` ES index — one doc per pending invite, keyed
  ``{workspace_esid}:{email}`` so the dedupe + Resend paths are point
  lookups. Lazy bootstrap on first write, same pattern as
  dismiss index.
* ``create_or_resend`` — the route's load-bearing entry point.
  Validates the email, checks for an existing workspace membership
  (already-member branch), checks for an existing pending doc
  (resend branch), otherwise upserts a fresh invite and fires the
  email. Returns a discriminated-union dict so the route can map each
  branch to the matching inline state in the modal.
* ``get_pending`` — read surface for ``GET /api/invites/pending``;
  surfaces the created_at + role so the modal can render an informed
  "Resend?" prompt.
* ``send_invite_email`` — synchronous SMTP send following the same
  ``smtplib`` pattern as ``OnboardingEmailVerificationService`` (no SES,
  no async dispatch — the modal POST blocks on the send so the inline
  success/failure states are accurate to what actually happened).

What lives elsewhere:

* Token issuance — ``InviteTokenService.generate_token``. We
  call it inside ``send_invite_email`` to mint the link; this service
  never decodes tokens.
* Permission gate — wires ``Permission.USER_INVITE``
  enforcement, the route uses ``_require_admin`` as a defense-in-depth
  backstop. The swap site is source-pinned in the route handler.
   added the per-workspace half here: ``inviter_grant_refusal``
  checks ``USER_INVITE`` in the workspace the invite lands in and caps the
  grantable role below the inviter's own.
* Accept / consume — (invited-user wizard variant) reads the
  invite payload off ``wizard_session.state['invite']``. This service
  marks the doc ``accepted`` lazily on first consume; the missing-team
  edge is out of scope here.

 (expired-invite edge) added ``mark_expired`` +
``notify_inviter_of_expiry`` + ``stamp_expiry_notified``. The route
detects expiry on landing (via ``InviteTokenService.decode_token``'s
``expired=True`` branch), flips the doc, debounces, and emails the
inviter — no scheduled sweep, expiry is detected when an invitee
actually clicks an expired link.

Status transitions:
    pending -> accepted (consume path; not written by this service)
    pending -> expired (landing-time detect via ``mark_expired``)
    pending -> cancelled  (future ticket — no UI to cancel today)

The dedupe doc-id contract (``{workspace}:{email}``) means a Resend
overwrites the existing doc rather than creating a duplicate; a fresh
``created_at`` would erase the original send timestamp so we preserve
the first ``created_at`` and stamp ``last_sent_at`` on each resend.
That split is what lets expire on ``created_at`` (the user's
choice was made at first send) without resends winding back the clock.
"""

from __future__ import annotations

import json
import logging
import os
import re
import smtplib

from lib.smtp_transport import SMTP_TIMEOUT_SECONDS, smtp_ssl_context
import sys
import uuid
from datetime import datetime, timedelta, timezone
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from typing import Any

sys.path.insert(0, "/app/python")

from lib.email_format import is_plausible_email  # noqa: E402
from lib.es_helpers import (  # noqa: E402
    create_index_if_not_exists,
    generate_es_safe_doc_id,
    get_elasticsearch_client,
    put_regular_index_template,
)

try:
    from web.services.invite_token_service import (
        INVITE_TOKEN_EXPIRY_SECONDS,
        InviteTokenService,
    )
except ImportError:
    from asset_management.web.services.invite_token_service import (  # noqa: E402
        INVITE_TOKEN_EXPIRY_SECONDS,
        InviteTokenService,
    )

try:
    from web.services.workspace_membership_service import WorkspaceMembershipService
except ImportError:
    from asset_management.web.services.workspace_membership_service import (  # noqa: E402
        WorkspaceMembershipService,
    )

try:
    from web.services.permissions_index_service import (
        USER_WORKSPACE_ROLE_INDEX,
        WORKSPACE_MEMBERSHIP_INDEX,
    )
except ImportError:
    from asset_management.web.services.permissions_index_service import (  # noqa: E402
        USER_WORKSPACE_ROLE_INDEX,
        WORKSPACE_MEMBERSHIP_INDEX,
    )

try:
    from web.services.permission_resolver import Permission, PermissionResolver
except ImportError:
    from asset_management.web.services.permission_resolver import (  # noqa: E402
        Permission,
        PermissionResolver,
    )

try:
    from web.services.permissions_seeder_service import (
        ROLE_EDITOR,
        ROLE_ORG_ADMIN,
        ROLE_ORG_VIEWER,
        ROLE_OWNER,
        ROLE_TEAM_ADMIN,
        ROLE_VIEWER,
        ROLE_WORKSPACE_ADMIN,
        ROLE_WORKSPACE_OWNER,
        ROLE_WORKSPACE_VIEWER,
    )
except ImportError:
    from asset_management.web.services.permissions_seeder_service import (  # noqa: E402
        ROLE_EDITOR,
        ROLE_ORG_ADMIN,
        ROLE_ORG_VIEWER,
        ROLE_OWNER,
        ROLE_TEAM_ADMIN,
        ROLE_VIEWER,
        ROLE_WORKSPACE_ADMIN,
        ROLE_WORKSPACE_OWNER,
        ROLE_WORKSPACE_VIEWER,
    )


INDEX = "de_authz_invite"

INDEX_MAPPING = {
    "properties": {
        # Lowercased per ``_normalize_email`` before write so the
        # {workspace}:{email} doc id collapses ``Alice@Example.com`` and
        # ``alice@example.com`` into the same pending row. Without this
        # a resend would mint a second doc and the modal would render
        # two "pending" hits.
        "email": {"type": "keyword"},
        "workspace_esid": {"type": "keyword"},
        "role_esid": {"type": "keyword"},
        "inviter": {"type": "keyword"},
        # Free-text personal note (280-char cap enforced at the API
        # boundary). text-mapped rather than keyword so future analytics
        # ("which org's notes mention 'agency'?") can full-text search.
        "note": {"type": "text"},
        # ``created_at`` is the FIRST-send timestamp and is preserved on
        # resend so "X days since the invite went out" sweep
        # measures from the user's original choice, not their last click.
        "created_at": {"type": "date"},
        # ``last_sent_at`` is the most recent send. Differs from
        # created_at iff the user clicked Resend at least once.
        "last_sent_at": {"type": "date"},
        # Mirror of ``created_at + INVITE_TOKEN_EXPIRY_SECONDS`` (7d).
        # Stored explicitly so a range query on this field finds
        # candidates without re-doing the arithmetic at query time.
        "expires_at": {"type": "date"},
        # pending|accepted|expired|cancelled. Stamped pending on create.
        # (accept) (expire), and a future cancel ticket
        # transition it; this service never reads the field outside
        # ``get_pending``'s pending-filter.
        "status": {"type": "keyword"},
        # Stamped by ``accept_invite`` when the wizard's
        # invited variant consumes the row. ES allows dynamic dates so
        # this is technically optional in the mapping, but pinning the
        # type avoids the "first write sets the type" surprise on a
        # fresh cluster.
        "accepted_at": {"type": "date"},
        # JWT identifier mirror — the wizard stamps ``jti`` onto
        # ``wizard_session.state['invite']`` for one-time-use enforcement
        #. Keeping it on the doc means we can correlate a
        # consume event against the pending row without round-tripping
        # the JWT through ES.
        "jti": {"type": "keyword"},
        # Stamped by ``mark_expired`` when an expired token
        # is detected on landing. Distinct from ``expires_at`` — that
        # is the projected end of the 7-day window; ``expired_at`` is
        # the wall-clock moment the system observed the expiry.
        "expired_at": {"type": "date"},
        # Stamped by ``notify_inviter_of_expiry`` when the
        # inviter has been emailed about an expired link. Used as a
        # debounce: a second landing on the same expired token
        # short-circuits the notify so the inviter isn't spammed.
        "expiry_notified_at": {"type": "date"},
        # optional team scope picked by the inviter at send
        # time. Empty string when the inviter left "Any team"; the
        # receiver picks or auto-creates on consume. Mirrored onto the
        # JWT so the receive-site read is a token decode, not an ES
        # round trip.
        "team_esid": {"type": "keyword"},
    }
}

# 280-char limit on the personal note matches the modal's textarea
# maxlength and Twitter's intuition for "short personal context". Longer
# notes belong in a follow-up email, not the invite itself.
NOTE_MAX_LENGTH = 280

# follow-up — RFC-5321 total-address ceiling. The format regex
# below deliberately stays loose (mailservers are the real validator),
# but an unbounded string is index bloat + a doc-id-length hazard, so
# cap the length server-side to match the modal's maxlength.
EMAIL_MAX_LENGTH = 254

# the only roles an invite may grant: exactly the options of the
# invite modal's role <select> (_invite_modal.html, ``invite-modal-role``:
# editor / viewer / org_admin / workspace_admin). ``role_esid`` arrives from
# the request body and is written verbatim onto the invite and, on accept,
# onto the invitee's workspace + team role docs, so anything outside this set
# (``workspace_owner``, ``owner``, ``superuser``, typos) is refused.
INVITABLE_ROLE_ESIDS: frozenset[str] = frozenset({ROLE_EDITOR, ROLE_VIEWER, ROLE_ORG_ADMIN, ROLE_WORKSPACE_ADMIN})

# privilege rank used for the "an inviter may only grant a role
# strictly below their own" ceiling. Covers every role that can sit on a
# workspace-tier role doc (seeded workspace roles, plus the invite roles that
# ``provision_invited`` writes there verbatim). A role missing from this map
# ranks 0, so an unrecognised inviter role can never clear the ceiling.
_ROLE_RANK: dict[str, int] = {
    ROLE_WORKSPACE_OWNER: 40,
    ROLE_OWNER: 40,
    ROLE_WORKSPACE_ADMIN: 30,
    ROLE_ORG_ADMIN: 20,
    ROLE_TEAM_ADMIN: 20,
    ROLE_EDITOR: 10,
    ROLE_VIEWER: 0,
    ROLE_ORG_VIEWER: 0,
    ROLE_WORKSPACE_VIEWER: 0,
}

# RFC-5322 is a thousand-line ABNF — we don't need to validate it on
# the server (mailservers reject malformed addresses on send and the
# user retries). The client-side regex is for early-fail UX; the server
# only checks "has an @ with something on each side and no whitespace"
# so a downstream send catches the real malformedness.

try:
    from web.services.smtp_settings import load_smtp_settings
except ImportError:
    from ..services.smtp_settings import load_smtp_settings

_PASSWORD_RESET_SETTINGS = "/app/python/password_reset/config/settings.json"


def _normalize_email(email: str) -> str:
    """Trim + lowercase so doc-id collisions collapse case variants.

    Email addresses are case-insensitive in the local-part per RFC-5321
    in practice (every mailserver normalises). Lowercasing here is the
    cheap way to make ``Alice@Example.com`` and ``alice@example.com``
    map to the same pending invite. Trim guards against a copy-paste
    that drags a trailing space into the form.
    """
    if not isinstance(email, str):
        return ""
    return email.strip().lower()


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _iso(dt: datetime) -> str:
    return dt.isoformat()


def _get_smtp_settings() -> dict:
    """Env-aware shared SMTP settings: PW_RESET_SMTP_* env
    credentials win over settings.json, which no longer stores them."""
    return load_smtp_settings("InviteService")


def _get_app_base_url(settings: dict | None = None) -> str:
    """Derive the application base URL (scheme://host) from SMTP settings.

    Same shape as ``OnboardingEmailVerificationService._get_app_base_url``
    so a deployment with a single ``settings.json`` produces the same
    link host across all the email surfaces.
    """
    settings = settings if settings is not None else _get_smtp_settings()
    app_url = (settings.get("app_url") or "").rstrip("/")
    if not app_url:
        return ""
    from urllib.parse import urlparse

    parsed = urlparse(app_url)
    return f"{parsed.scheme}://{parsed.netloc}"


class InviteService:
    """Sender-side invite surface for the modal."""

    # ------------------------------------------------------------------
    # Index bootstrap (lazy — called on first create_or_resend write)
    # ------------------------------------------------------------------

    @staticmethod
    def ensure_index_exists() -> None:
        """Create the invite index if it does not exist. Idempotent."""
        es = get_elasticsearch_client()
        if es is None:
            logging.warning("InviteService: could not connect to ES")
            return
        put_regular_index_template(es, INDEX, INDEX_MAPPING, priority=500)
        create_index_if_not_exists(es, INDEX, INDEX_MAPPING)

    # ------------------------------------------------------------------
    # Doc-id + helpers
    # ------------------------------------------------------------------

    @staticmethod
    def _doc_id(workspace_esid: str, email: str) -> str:
        """Deterministic doc id for the (workspace, email) pair.

        Email is lowercased + trimmed before the join so case variants
        collapse to one pending row. Workspace scoping is the OUTER
        component so a ``terms`` query on a workspace prefix finds every
        invite in that workspace via doc-id prefix routing.
        """
        return f"{workspace_esid}:{_normalize_email(email)}"

    @staticmethod
    def _validate_email_format(email: str) -> bool:
        # Length first: the loose pattern is quadratic on long
        # '@'/'.'-heavy input, and two of the three callers had no cap at all.
        email = email or ""
        return len(email) <= EMAIL_MAX_LENGTH and is_plausible_email(email)

    @staticmethod
    def _get_inviter_display_name(username: str) -> str:
        """Return the ES Security ``full_name`` for a username, or the
        username itself when no friendlier name is on record.

        Used by every invite-email surface so the greeting reads "Hi
        Alice Smith" when we know it, "Hi alice" when we don't. The
        lookup is O(1) point read against ES Security (no scan), so
        calling it once per email send is cheap.

        Soft-fails the same way ``_find_username_by_email`` does: on a
        missing user / ES outage / lookup exception we fall back to the
        username rather than raising — the email greeting is cosmetic,
        not load-bearing.
        """
        if not username:
            return ""
        es = get_elasticsearch_client()
        if es is None:
            return username
        try:
            response = dict(es.security.get_user(username=username))
        except Exception as e:  # noqa: BLE001 — top-level boundary
            logging.warning(
                "InviteService._get_inviter_display_name(%s): %s",
                username,
                e,
            )
            return username
        record = response.get(username)
        if not isinstance(record, dict):
            return username
        full_name = (record.get("full_name") or "").strip()
        return full_name or username

    @staticmethod
    def _find_username_by_email(email: str) -> str | None:
        """Reverse-lookup the ES Security username for a given email.

        ``de_authz_workspace_membership`` keys on ``username``, but the
        invite form takes an email. ES Security stores email on the
        user record, not on the membership doc, so we iterate
        ``es.security.get_user()`` once and match. Same pattern the
        MFA + AuditLog services use to surface "users by attribute"
        — the call is O(users-in-cluster) but the user count is small
        and the workspace-membership check is the actual hot path.

        Email match is case-insensitive (we lowercase both sides) to
        align with ``_normalize_email``'s doc-id contract.
        """
        if not email:
            return None
        es = get_elasticsearch_client()
        if es is None:
            return None
        normalised = _normalize_email(email)
        try:
            response = dict(es.security.get_user())
        except Exception as e:  # noqa: BLE001 — top-level boundary
            logging.warning("InviteService._find_username_by_email(%s): %s", email, e)
            return None
        for username, user_doc in response.items():
            user_email = (user_doc.get("email") or "").strip().lower()
            if user_email == normalised:
                return username
        return None

    @staticmethod
    def _is_existing_member(workspace_esid: str, email: str) -> bool:
        """True iff someone with ``email`` already holds membership in
        the workspace.

        Two-step: ES Security email → username, then membership-index
        ``term`` filter on (username, workspace_esid). Returns False on
        ES outage, unknown email, or zero memberships — the conservative
        direction: we'd rather let an invite go to an existing member
        (the recipient sees "you're already in!") than block a
        legitimate invite because of a transient lookup blip.
        """
        if not workspace_esid or not email:
            return False
        username = InviteService._find_username_by_email(email)
        if not username:
            return False
        es = get_elasticsearch_client()
        if es is None:
            return False
        try:
            resp = es.count(
                index=WORKSPACE_MEMBERSHIP_INDEX,
                body={
                    "query": {
                        "bool": {
                            "filter": [
                                {"term": {"workspace_esid": workspace_esid}},
                                {"term": {"username": username}},
                            ]
                        }
                    }
                },
            )
        except Exception as e:
            msg = str(e).lower()
            if "not_found" in msg or "index_not_found" in msg or "404" in msg:
                return False
            logging.warning(
                "InviteService._is_existing_member(%s, %s): %s",
                workspace_esid,
                email,
                e,
            )
            return False
        return int(resp.get("count", 0)) > 0

    # ------------------------------------------------------------------
    # Workspace teams
    # ------------------------------------------------------------------

    @staticmethod
    def list_workspace_teams(workspace_esid: str) -> list[dict]:
        """Return ``[{"team_esid": ..., "team_title": ...}]`` for a workspace.

        Lookup goes workspace → orgs → teams. The team doc carries
        ``organization_esid`` (an array under F5+); the org doc carries
        ``workspace_esid``. Joining via the org index is the reliable
        path — querying ``de_asset_teams.workspace_esid`` directly would
        miss teams whose docs haven't been backfilled with the field.

        Returns ``[]`` on ES outage, unknown workspace, or empty
        workspace. Caller treats empty as "no team scope" and routes
        the invitee to the auto-create-General branch.
        """
        if not workspace_esid:
            return []
        es = get_elasticsearch_client()
        if es is None:
            return []
        try:
            orgs_resp = es.search(
                index="de_asset_organization",
                body={
                    "query": {"term": {"workspace_esid": workspace_esid}},
                    "_source": False,
                    "size": 1000,
                },
            )
        except Exception as e:  # noqa: BLE001 — top-level boundary
            logging.warning("InviteService.list_workspace_teams orgs lookup %s: %s", workspace_esid, e)
            return []
        org_esids = [hit["_id"] for hit in orgs_resp.get("hits", {}).get("hits", [])]
        if not org_esids:
            return []
        try:
            teams_resp = es.search(
                index="de_asset_teams",
                body={
                    # bare organization_esid — explicit keyword
                    # post-reindex; the .keyword subfield is gone (a term on
                    # it silently matches nothing, which emptied the Data
                    # Inventory data-view dropdown via
                    # workspace_scoped_index_pattern).
                    "query": {"terms": {"organization_esid": org_esids}},
                    "_source": ["team_title", "organization_esid"],
                    "size": 1000,
                },
            )
        except Exception as e:  # noqa: BLE001 — top-level boundary
            logging.warning("InviteService.list_workspace_teams teams lookup %s: %s", workspace_esid, e)
            return []
        result: list[dict] = []
        for hit in teams_resp.get("hits", {}).get("hits", []):
            src = hit.get("_source") or {}
            result.append(
                {
                    "team_esid": hit["_id"],
                    "team_title": (src.get("team_title") or "").strip() or hit["_id"],
                }
            )
        result.sort(key=lambda r: r["team_title"].lower())
        return result

    @staticmethod
    def team_belongs_to_workspace(team_esid: str, workspace_esid: str) -> bool:
        """True iff ``team_esid`` resolves to an org under ``workspace_esid``.

        Used as a defense-in-depth check on POST /api/invites + on the
        receiver picker submit so a forged team_esid in a request body
        can't grant a stranger membership in someone else's team.
        """
        if not team_esid or not workspace_esid:
            return False
        es = get_elasticsearch_client()
        if es is None:
            return False
        try:
            team_doc = es.get(index="de_asset_teams", id=team_esid)
        except Exception:  # noqa: BLE001 — not_found / outage both fall closed
            return False
        org_esids = (team_doc.get("_source") or {}).get("organization_esid") or []
        if isinstance(org_esids, str):
            org_esids = [org_esids]
        if not org_esids:
            return False
        try:
            orgs_resp = es.search(
                index="de_asset_organization",
                body={
                    "query": {
                        "bool": {
                            "filter": [
                                {"ids": {"values": list(org_esids)}},
                                {"term": {"workspace_esid": workspace_esid}},
                            ]
                        }
                    },
                    "_source": False,
                    "size": 1,
                },
            )
        except Exception:  # noqa: BLE001 — defensive read
            return False
        return bool(orgs_resp.get("hits", {}).get("hits"))

    @staticmethod
    def ensure_general_team(workspace_esid: str) -> str | None:
        """Return the ``team_esid`` of a "General" team in the workspace.

        If a team named "General" already exists in any of the
        workspace's orgs, return its esid. Otherwise create one under
        the workspace's first (lowest-id) org. Returns ``None`` on ES
        outage or when the workspace has no orgs — caller falls through
        to the show-4-6 branch in that case (the AC's "never blocked"
        contract: the user advances; the team-tier write is best-effort).
        """
        if not workspace_esid:
            return None
        es = get_elasticsearch_client()
        if es is None:
            return None
        try:
            orgs_resp = es.search(
                index="de_asset_organization",
                body={
                    "query": {"term": {"workspace_esid": workspace_esid}},
                    "sort": [{"_id": "asc"}],
                    "_source": False,
                    "size": 1000,
                },
            )
        except Exception as e:  # noqa: BLE001 — top-level boundary
            logging.warning("InviteService.ensure_general_team orgs lookup %s: %s", workspace_esid, e)
            return None
        org_esids = [hit["_id"] for hit in orgs_resp.get("hits", {}).get("hits", [])]
        if not org_esids:
            return None
        # Look for an existing "General" team under any of the workspace's orgs.
        try:
            existing = es.search(
                index="de_asset_teams",
                body={
                    "query": {
                        "bool": {
                            "must": [
                                {"match": {"team_title": "General"}},
                                {"terms": {"organization_esid": org_esids}},
                            ]
                        }
                    },
                    "_source": False,
                    "size": 1,
                },
            )
        except Exception as e:  # noqa: BLE001 — top-level boundary
            logging.warning("InviteService.ensure_general_team existing-team lookup %s: %s", workspace_esid, e)
            return None
        hits = existing.get("hits", {}).get("hits", [])
        if hits:
            return hits[0]["_id"]
        # No existing General — create one under the workspace's first
        # org so the team has somewhere structural to live.
        primary_org = org_esids[0]
        doc = {
            "team_title": "General",
            "organization_esid": [primary_org],
            "@timestamp": _iso(_now()),
        }
        try:
            # pass an explicit ES-safe _id (alphanumeric leading char)
            # instead of relying on ES auto-id, which can produce an _id starting
            # with _/-/+ that later yields an invalid {team_esid}__{source} index
            # name and a cryptic 400 on dashboard query.
            safe_id = generate_es_safe_doc_id()
            response = es.index(index="de_asset_teams", id=safe_id, document=doc, refresh="wait_for")
        except Exception as e:  # noqa: BLE001 — top-level boundary
            logging.error("InviteService.ensure_general_team create failed %s: %s", workspace_esid, e)
            return None
        return response.get("_id")

    @staticmethod
    def get_pending(workspace_esid: str, email: str) -> dict | None:
        """Return the pending invite doc for (workspace, email), or None.

        Single ``es.get`` via deterministic doc id. ``None`` covers all
        of: no row, ES outage, index_not_found, or a row that exists but
        is no longer ``status="pending"`` (accepted / expired / cancelled
        — those don't surface in the Resend path).
        """
        if not workspace_esid or not email:
            return None
        es = get_elasticsearch_client()
        if es is None:
            return None
        doc_id = InviteService._doc_id(workspace_esid, email)
        try:
            resp = es.get(index=INDEX, id=doc_id)
        except Exception as e:
            msg = str(e).lower()
            if "not_found" in msg or "index_not_found" in msg or "404" in msg:
                return None
            logging.warning("InviteService.get_pending(%s): %s", doc_id, e)
            return None
        # es.get returns an ObjectApiResponse (mapping-like via .get, NOT a dict
        # subclass) — duck-type, don't isinstance(dict). The old isinstance(dict)
        # guard made this return None for EVERY real doc (same class as).
        source = resp.get("_source") if hasattr(resp, "get") else None
        if not isinstance(source, dict):
            return None
        if source.get("status") != "pending":
            return None
        return source

    # ------------------------------------------------------------------
    # Accept
    # ------------------------------------------------------------------

    @staticmethod
    def accept_invite(workspace_esid: str, email: str) -> dict:
        """Flip the pending invite doc to ``status="accepted"``.

        Called by the wizard variant on first arrival at step-2
        (the consume site) — after ``OnboardingService.provision_invited``
        has written memberships. Idempotent: a second call on an
        already-accepted doc is a no-op success; the failure modes are
        the same shape as ``get_pending`` (no row / no ES / index_not_found)
        so the caller treats "couldn't find a row to flip" as a soft
        warning rather than a hard error — the membership writes already
        landed, so the user is in even if the invite-doc bookkeeping
        slipped.

        Returns ``{"status": "accepted" | "not_found" | "es_unavailable", ...}``.
        Never raises — the route swallows the result and logs on
        non-accepted statuses; nothing else depends on the doc-flip.
        """
        if not workspace_esid or not email:
            return {"status": "not_found", "reason": "missing_inputs"}
        es = get_elasticsearch_client()
        if es is None:
            return {"status": "es_unavailable"}
        doc_id = InviteService._doc_id(workspace_esid, email)
        try:
            es.update(
                index=INDEX,
                id=doc_id,
                body={"doc": {"status": "accepted", "accepted_at": _iso(_now())}},
                refresh="wait_for",
            )
        except Exception as e:
            msg = str(e).lower()
            if "not_found" in msg or "index_not_found" in msg or "404" in msg:
                return {"status": "not_found", "doc_id": doc_id}
            logging.warning("InviteService.accept_invite(%s): %s", doc_id, e)
            return {"status": "es_unavailable", "error": "Could not read the invite."}
        return {"status": "accepted", "doc_id": doc_id}

    # ------------------------------------------------------------------
    # Expiry
    # ------------------------------------------------------------------

    @staticmethod
    def mark_expired(workspace_esid: str, email: str) -> dict:
        """Flip the pending invite doc to ``status="expired"``.

        Called by the landing handler when ``decode_token``
        reports ``expired=True``. Stamps both ``status="expired"`` and
        an ``expired_at`` wall-clock timestamp; the latter distinguishes
        "observed expired at" from the projected ``expires_at`` so a
        future audit can tell whether a doc expired silently (never
        landed on) or was hit by an invitee.

        Idempotent: a second call on an already-expired doc just
        overwrites the same fields. Soft-fails on missing rows / ES
        outages the same way ``accept_invite`` does — the doc-flip is
        bookkeeping; the user-facing screen is independent of whether
        the row got flipped.

        Returns ``{"status": "expired" | "not_found" | "es_unavailable", ...}``.
        Never raises.
        """
        if not workspace_esid or not email:
            return {"status": "not_found", "reason": "missing_inputs"}
        es = get_elasticsearch_client()
        if es is None:
            return {"status": "es_unavailable"}
        doc_id = InviteService._doc_id(workspace_esid, email)
        try:
            es.update(
                index=INDEX,
                id=doc_id,
                body={"doc": {"status": "expired", "expired_at": _iso(_now())}},
                refresh="wait_for",
            )
        except Exception as e:
            msg = str(e).lower()
            if "not_found" in msg or "index_not_found" in msg or "404" in msg:
                return {"status": "not_found", "doc_id": doc_id}
            logging.warning("InviteService.mark_expired(%s): %s", doc_id, e)
            return {"status": "es_unavailable", "error": "Could not update the invite."}
        return {"status": "expired", "doc_id": doc_id}

    @staticmethod
    def notify_inviter_of_expiry(
        *,
        inviter_username: str,
        invitee_email: str,
        workspace_esid: str,
    ) -> dict:
        """Email the inviter that an invitee landed on an expired link.

        AC: 'Inviter gets notified within 1 minute.' Synchronous SMTP
        send (same posture as ``send_invite_email``) so the route
        surfaces the success/failure state inline; the modal-style
        ``analytics_event=invite.expiry.notified`` event fires whenever
        a send is attempted.

        Debounce contract — the caller is responsible for stamping
        ``expiry_notified_at`` on the doc BEFORE calling this. We don't
        do it here because the debounce decision belongs to the route
        (it has the request context to decide whether a refresh-spam
        is in flight); this method just sends.

        Returns ``{"success": bool, "error": str, "resend_link": str}``.
        Never raises.
        """
        if not InviteService._validate_email_format(invitee_email):
            return {"success": False, "error": "Invalid invitee email"}
        if not inviter_username:
            return {"success": False, "error": "Missing inviter"}

        # Reverse-lookup the inviter's email + display name from ES
        # Security. The invite doc stores ``inviter`` as a username
        # so we have to round-trip to ES to find the
        # mailbox to send to. Soft-fail on miss — the screen still
        # renders even if we can't email anyone.
        es = get_elasticsearch_client()
        inviter_email = ""
        inviter_display = inviter_username
        if es is not None:
            try:
                user_doc = dict(es.security.get_user(username=inviter_username))
                if inviter_username in user_doc:
                    record = user_doc[inviter_username]
                    inviter_email = (record.get("email") or "").strip()
            except Exception as e:  # noqa: BLE001 — top-level boundary
                logging.warning(
                    "InviteService.notify_inviter_of_expiry(%s): inviter lookup failed: %s",
                    inviter_username,
                    e,
                )

        # Resolve the display name via the shared helper so the
        # greeting matches what ``send_invite_email`` uses for the
        # same username. Falls back to the username on miss.
        inviter_display = InviteService._get_inviter_display_name(inviter_username)

        if not inviter_email:
            return {"success": False, "error": "Inviter has no email on record"}

        settings = _get_smtp_settings()
        required = ["smtp_host", "smtp_port", "smtp_username", "smtp_password", "from_email"]
        missing = [k for k in required if not settings.get(k)]
        if missing:
            return {
                "success": False,
                "error": f"SMTP not configured (missing: {', '.join(missing)})",
            }

        base_url = _get_app_base_url(settings)
        # The resend link drops the inviter back into the dashboard
        # with the invite modal pre-opened on the original email so
        # one click rehydrates the form. The modal's create_or_resend
        # branch overwrites the existing (now-expired) doc with a
        # fresh JWT + new 7-day window — no manual cleanup required.
        from urllib.parse import quote as _urlquote

        resend_link = f"{base_url}/dashboard?invite=open&prefill_email=" f"{_urlquote(_normalize_email(invitee_email))}"
        logo_url = f"{base_url}/static/kavanah-analytics-logo.svg" if base_url else ""

        subject = f"Your invite to {invitee_email} expired"

        # HTML-escape the email values — they're user-supplied free
        # text that's about to land in markup, and a malicious invitee
        # who controlled the address could otherwise inject markup.
        from html import escape as _html_escape

        safe_invitee = _html_escape(invitee_email)
        safe_display = _html_escape(inviter_display)

        html_body = f"""<!DOCTYPE html>
<html>
<head>
<meta charset="UTF-8">
<title>{subject}</title>
</head>
<body style="margin:0;padding:0;background-color:#F1F3F3;font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',Roboto,Helvetica,Arial,sans-serif;color:#000716;">
<table role="presentation" width="100%" cellpadding="0" cellspacing="0" border="0" style="background-color:#F1F3F3;">
  <tr><td align="center" style="padding:24px 12px;">
    <table role="presentation" width="600" cellpadding="0" cellspacing="0" border="0" style="max-width:600px;width:100%;background-color:#ffffff;border-radius:8px;border:1px solid #e9ebed;">
      <tr><td style="background-color:#232F3E;padding:16px 24px;border-radius:8px 8px 0 0;">
        <table role="presentation" cellpadding="0" cellspacing="0" border="0"><tr>
          <td style="vertical-align:middle;width:32px;"><img src="{logo_url}" width="28" height="28" alt="" style="display:block;border:0;"></td>
          <td style="vertical-align:middle;padding-left:10px;color:#ffffff;font-size:17px;font-weight:700;">Kavanah Analytics</td>
        </tr></table>
      </td></tr>
      <tr><td style="padding:32px 28px;background-color:#ffffff;color:#000716;font-size:15px;line-height:1.6;">
        <h2 style="margin:0 0 16px;color:#000716;font-size:20px;font-weight:700;">Your invite expired</h2>
        <p style="margin:0 0 12px;">Hi {safe_display},</p>
        <p style="margin:0 0 12px;">The invite you sent to <strong>{safe_invitee}</strong> expired before they signed up. They just tried the link and we showed them an expiry notice.</p>
        <p style="margin:0 0 12px;">Click below to resend the invite with a fresh 7-day window.</p>
        <table role="presentation" cellpadding="0" cellspacing="0" border="0" style="margin:20px 0;"><tr>
          <td style="border-radius:6px;background-color:#0972D3;">
            <a href="{resend_link}" style="display:inline-block;padding:14px 32px;color:#ffffff;text-decoration:none;font-weight:600;font-size:15px;border-radius:6px;">Resend invite</a>
          </td>
        </tr></table>
        <p style="margin:16px 0 4px;font-size:13px;color:#5F6B7A;">If the button doesn't work, copy and paste this URL into your browser:</p>
        <p style="margin:0 0 16px;"><a href="{resend_link}" style="color:#0972D3;word-break:break-all;">{resend_link}</a></p>
        <p style="margin:0;font-size:13px;color:#5F6B7A;">If you no longer want to invite this person, you can ignore this email.</p>
      </td></tr>
      <tr><td style="padding:16px 28px 24px;border-top:1px solid #e9ebed;background-color:#ffffff;color:#5F6B7A;font-size:12px;line-height:1.5;border-radius:0 0 8px 8px;">
        This is an automated message — please do not reply.
      </td></tr>
    </table>
  </td></tr>
</table>
</body>
</html>"""

        text_body = (
            f"Your invite expired\n\n"
            f"Hi {inviter_display},\n\n"
            f"The invite you sent to {invitee_email} expired before they signed up. "
            f"They just tried the link and we showed them an expiry notice.\n\n"
            f"Resend the invite with a fresh 7-day window:\n\n"
            f"{resend_link}\n\n"
            f"If you no longer want to invite this person, you can ignore this email."
        )

        msg = MIMEMultipart("alternative")
        msg["Subject"] = subject
        msg["From"] = settings["from_email"]
        msg["To"] = inviter_email
        msg.attach(MIMEText(text_body, "plain"))
        msg.attach(MIMEText(html_body, "html"))

        try:
            if settings.get("smtp_use_tls", True):
                server = smtplib.SMTP(settings["smtp_host"], int(settings["smtp_port"]), timeout=SMTP_TIMEOUT_SECONDS)
                server.starttls(context=smtp_ssl_context())
            else:
                server = smtplib.SMTP_SSL(
                    settings["smtp_host"], int(settings["smtp_port"]), timeout=SMTP_TIMEOUT_SECONDS, context=smtp_ssl_context()
                )
            server.login(settings["smtp_username"], settings["smtp_password"])
            server.sendmail(settings["from_email"], inviter_email, msg.as_string())
            server.quit()
            logging.info(
                "InviteService: expiry-notify email sent to inviter=%s (workspace=%s, invitee=%s)",
                inviter_username,
                workspace_esid,
                invitee_email,
            )
            return {"success": True, "resend_link": resend_link}
        except Exception as e:  # noqa: BLE001 — top-level boundary
            logging.error(
                "InviteService.notify_inviter_of_expiry: SMTP failed for inviter=%s: %s",
                inviter_username,
                e,
            )
            return {"success": False, "error": "Could not send the expiry notice."}

    @staticmethod
    def stamp_expiry_notified(workspace_esid: str, email: str) -> bool:
        """Stamp ``expiry_notified_at`` on the invite doc.

        Debounce primitive — the route writes this BEFORE firing the
        notify email so a concurrent second landing on the same expired
        token short-circuits the notify. Returns True on write success,
        False on missing doc / ES outage / index_not_found.

        Idempotency: we overwrite the timestamp on each write so a
        rare retry after a transient ES blip surfaces the most recent
        attempt; the route's debounce decision is "was the field set
        BEFORE we landed?" rather than "what's its value now."
        """
        if not workspace_esid or not email:
            return False
        es = get_elasticsearch_client()
        if es is None:
            return False
        doc_id = InviteService._doc_id(workspace_esid, email)
        try:
            es.update(
                index=INDEX,
                id=doc_id,
                body={"doc": {"expiry_notified_at": _iso(_now())}},
                refresh="wait_for",
            )
        except Exception as e:
            msg = str(e).lower()
            if "not_found" in msg or "index_not_found" in msg or "404" in msg:
                return False
            logging.warning("InviteService.stamp_expiry_notified(%s): %s", doc_id, e)
            return False
        return True

    @staticmethod
    def get_invite_doc(workspace_esid: str, email: str) -> dict | None:
        """Return the invite doc for (workspace, email) regardless of status.

        Distinct from ``get_pending`` (which only returns ``status="pending"``)
        because the expiry path needs to read ``expiry_notified_at``
        on a doc that has already been flipped to ``status="expired"`` to
        decide whether the debounce gate has fired. ``None`` on no row /
        ES outage / index_not_found.
        """
        if not workspace_esid or not email:
            return None
        es = get_elasticsearch_client()
        if es is None:
            return None
        doc_id = InviteService._doc_id(workspace_esid, email)
        try:
            resp = es.get(index=INDEX, id=doc_id)
        except Exception as e:
            msg = str(e).lower()
            if "not_found" in msg or "index_not_found" in msg or "404" in msg:
                return None
            logging.warning("InviteService.get_invite_doc(%s): %s", doc_id, e)
            return None
        # es.get returns an ObjectApiResponse (duck-type on .get, not isinstance
        # dict) — the old isinstance(dict) guard returned None for every real
        # invite doc (same class as).
        source = resp.get("_source") if hasattr(resp, "get") else None
        if not isinstance(source, dict):
            return None
        return source

    # ------------------------------------------------------------------
    # Inviter authorization
    # ------------------------------------------------------------------

    @staticmethod
    def _inviter_workspace_role_esids(username: str, workspace_esid: str) -> set[str]:
        """Every workspace-tier role ``username`` holds on ``workspace_esid``.

        ``WorkspaceMembershipService.get_role`` returns only the
        alphabetically-first role, which would understate a multi-role
        inviter, so the ceiling reads the full set. Empty on ES outage or
        lookup failure, which fails the ceiling closed.
        """
        if not username or not workspace_esid:
            return set()
        es = get_elasticsearch_client()
        if es is None:
            return set()
        try:
            resp = es.search(
                index=USER_WORKSPACE_ROLE_INDEX,
                body={
                    "query": {
                        "bool": {
                            "filter": [
                                {"term": {"username": username}},
                                {"term": {"workspace_esid": workspace_esid}},
                            ]
                        }
                    },
                    "_source": ["role_esid"],
                    "size": 100,
                },
            )
        except Exception as e:  # noqa: BLE001 — deny-by-default boundary
            logging.warning("InviteService._inviter_workspace_role_esids(%s, %s): %s", username, workspace_esid, e)
            return set()
        return {r for hit in resp.get("hits", {}).get("hits", []) if (r := (hit.get("_source") or {}).get("role_esid"))}

    @staticmethod
    def inviter_grant_refusal(inviter_username: str, workspace_esid: str, role_esid: str) -> str | None:
        """why this (non-platform-admin) inviter may NOT send this
        invite into ``workspace_esid``, or ``None`` when they may.

        Three checks, all against the workspace the invite will land in:

        * ``bad_role`` — ``role_esid`` is not one the invite modal offers.
        * ``forbidden`` — the inviter does not hold ``USER_INVITE`` through a
          workspace-tier role on THIS workspace. A scoped admin role held in
          some other workspace no longer carries over.
        * ``role_not_permitted`` — the role is at or above the inviter's own
          highest role here: an owner may invite admins, an admin may not
          invite an owner or a peer admin.

        Platform admins (superuser / asset_admin) are not routed through
        here; the caller skips this check for them.
        """
        if not isinstance(role_esid, str) or role_esid not in INVITABLE_ROLE_ESIDS:
            return "bad_role"
        if not PermissionResolver.has_permission_in_workspace(inviter_username, workspace_esid, Permission.USER_INVITE):
            return "forbidden"
        held = InviteService._inviter_workspace_role_esids(inviter_username, workspace_esid)
        inviter_rank = max((_ROLE_RANK.get(r, 0) for r in held), default=0)
        if _ROLE_RANK.get(role_esid, 0) >= inviter_rank:
            return "role_not_permitted"
        return None

    @staticmethod
    def inviter_may_invite_in(inviter_username: str, workspace_esid: str) -> bool:
        """does this (non-platform-admin) caller hold
        ``USER_INVITE`` through a workspace-tier role on ``workspace_esid``?

        The read side of the invite modal (pending lookup, team picker) uses
        this so it answers only for a workspace the caller may invite into —
        the same per-workspace check ``inviter_grant_refusal`` applies to the
        send. Deny-by-default on empty inputs / ES outage (resolver contract).
        """
        return PermissionResolver.has_permission_in_workspace(inviter_username, workspace_esid, Permission.USER_INVITE)

    # ------------------------------------------------------------------
    # Create-or-resend (the load-bearing entry point)
    # ------------------------------------------------------------------

    @staticmethod
    def create_or_resend(
        *,
        inviter_username: str,
        workspace_esid: str,
        email: str,
        role_esid: str,
        note: str = "",
        secret_key: str = "",
        team_esid: str | None = None,
    ) -> dict:
        """Create a fresh invite or resend an existing pending one.

        Returns a discriminated-union dict the route maps to inline
        states in the modal:

        * ``{"status": "sent", "email": <normalised>, ...}``
            A brand-new invite was written and the email fired.
        * ``{"status": "resent", "email": ..., "existing": <pending>}``
            An existing pending invite was found; we updated
            ``last_sent_at`` (preserving ``created_at``) and re-fired
            the email. No duplicate doc was written.
        * ``{"status": "already_member"}``
            The email is already a workspace member; no doc write, no
            email fired.
        * ``{"status": "invalid", "reason": <slug>}``
            Email format failed validation, the inputs are missing, or
            the note exceeded 280 chars. No write.
        * ``{"status": "es_unavailable"}`` / ``{"status": "send_failed"}``
            Infra failure. ``es_unavailable`` is the WRITE leg failing
            (no doc was written); ``send_failed`` means the doc IS
            written but SMTP threw — the user can retry which hits the
            Resend path. Detail in the ``error`` field.

        The two failure-mode posture is deliberate: a logged invite
        with no email is recoverable; a sent email with no doc would
        leak duplicate accepts on the receive side, so we write first
        and then send.
        """
        normalised_email = _normalize_email(email)
        note = (note or "").strip()

        if not inviter_username or not workspace_esid:
            return {"status": "invalid", "reason": "missing_session"}
        if not normalised_email or not InviteService._validate_email_format(normalised_email):
            return {"status": "invalid", "reason": "bad_email"}
        # follow-up — the format regex is deliberately loose, so
        # bound the length separately (the modal's maxlength mirrors this).
        if len(normalised_email) > EMAIL_MAX_LENGTH:
            return {"status": "invalid", "reason": "bad_email"}
        # allow-list, not merely non-empty: the role is written
        # verbatim onto the invitee, so a forged body must not name one the
        # modal never offers (workspace_owner, owner, superuser, ...).
        if not isinstance(role_esid, str) or role_esid not in INVITABLE_ROLE_ESIDS:
            return {"status": "invalid", "reason": "bad_role"}
        if len(note) > NOTE_MAX_LENGTH:
            return {"status": "invalid", "reason": "note_too_long"}

        # if the inviter picked a team, defense-in-depth check
        # that it actually belongs to this workspace. A forged team_esid
        # in the request body must not grant membership to a stranger's
        # team. We treat a failed check as a soft drop (set team_esid
        # back to None) rather than failing the whole invite — the AC's
        # "never blocked" contract; the invitee will see the team picker
        # on the receive side and pick a real team.
        if team_esid and not InviteService.team_belongs_to_workspace(team_esid, workspace_esid):
            logging.warning(
                "InviteService.create_or_resend: team_esid %s rejected for workspace %s (cross-workspace forge)",
                team_esid,
                workspace_esid,
            )
            team_esid = None

        # Already-a-member is the first branch the AC names. Run it
        # BEFORE the get_pending lookup so the cheaper count survives
        # an ES outage on the pending-doc lookup — we don't want a
        # transient blip to invite an existing member.
        if InviteService._is_existing_member(workspace_esid, normalised_email):
            return {"status": "already_member"}

        existing = InviteService.get_pending(workspace_esid, normalised_email)

        InviteService.ensure_index_exists()
        es = get_elasticsearch_client()
        if es is None:
            return {"status": "es_unavailable", "error": "Elasticsearch unavailable"}

        now = _now()
        expires = now + timedelta(seconds=INVITE_TOKEN_EXPIRY_SECONDS)
        doc_id = InviteService._doc_id(workspace_esid, normalised_email)

        if existing is not None:
            # Resend path — preserve ``created_at`` + ``jti`` so the
            # expiry sweep measures from the FIRST send and the
            # receive-side one-time-use enforcement still matches the
            # JWT we hand back to the user.
            update_doc = {
                "last_sent_at": _iso(now),
                # Bump expiry on resend so the user actually gets the
                # 7-day window from the LATEST send. created_at stays
                # frozen for the expiry-sweep semantics above; expires_at
                # is the user-facing TTL the JWT carries.
                "expires_at": _iso(expires),
                "note": note,
                "role_esid": role_esid,
                # record team selection on resend too so a
                # later landing reflects whatever the inviter picked
                # most recently (sender may swap teams between sends).
                "team_esid": team_esid or "",
            }
            try:
                es.update(
                    index=INDEX,
                    id=doc_id,
                    body={"doc": update_doc},
                    refresh="wait_for",
                )
            except Exception as e:  # noqa: BLE001 — top-level boundary
                logging.error("InviteService.create_or_resend resend %s: %s", doc_id, e)
                return {"status": "es_unavailable", "error": "Could not save the invite."}
            jti = existing.get("jti") or uuid.uuid4().hex
        else:
            # Fresh-create path — generate a new jti and stamp the
            # first ``created_at``. ``status="pending"`` so
            # consume path knows the row is still actionable.
            jti = uuid.uuid4().hex
            doc = {
                "email": normalised_email,
                "workspace_esid": workspace_esid,
                "role_esid": role_esid,
                "inviter": inviter_username,
                "note": note,
                "created_at": _iso(now),
                "last_sent_at": _iso(now),
                "expires_at": _iso(expires),
                "status": "pending",
                "jti": jti,
                # sender's team selection (optional). Empty
                # string means "no team scope" and the receiver will
                # pick or auto-create on consume.
                "team_esid": team_esid or "",
            }
            try:
                es.update(
                    index=INDEX,
                    id=doc_id,
                    body={"doc": doc, "doc_as_upsert": True},
                    refresh="wait_for",
                )
            except Exception as e:  # noqa: BLE001 — top-level boundary
                logging.error("InviteService.create_or_resend create %s: %s", doc_id, e)
                return {"status": "es_unavailable", "error": "Could not save the invite."}

        # Send the email AFTER the write. If SMTP throws we surface a
        # send_failed status so the route can render a "couldn't send,
        # try again" inline error — the user's retry hits the Resend
        # branch (no duplicate doc).
        send_result = InviteService.send_invite_email(
            email=normalised_email,
            inviter_username=inviter_username,
            workspace_esid=workspace_esid,
            role_esid=role_esid,
            note=note,
            jti=jti,
            secret_key=secret_key,
            team_esid=team_esid,
        )
        if not send_result.get("success"):
            return {
                "status": "send_failed",
                "error": send_result.get("error") or "SMTP send failed",
                "email": normalised_email,
            }

        if existing is not None:
            return {
                "status": "resent",
                "email": normalised_email,
                "existing": existing,
            }
        return {"status": "sent", "email": normalised_email}

    # ------------------------------------------------------------------
    # SMTP send
    # ------------------------------------------------------------------

    @staticmethod
    def send_invite_email(
        *,
        email: str,
        inviter_username: str,
        workspace_esid: str,
        role_esid: str,
        note: str = "",
        jti: str = "",
        secret_key: str = "",
        team_esid: str | None = None,
    ) -> dict:
        """Send the invite email synchronously via smtplib.

        Returns ``{"success": bool, "error": str}``. The
        ``InviteTokenService.generate_token`` mints the JWT that the
        recipient clicks; we embed it in a link to
        ``/onboarding/start?invite=<jwt>`` so the receiving
        path picks it up automatically.

        Synchronous (no thread dispatch) because the modal POST surfaces
        the result inline — the user wants to know "did the email
        actually go?" while looking at the form. The verification-email
        service uses async dispatch because the wizard POST has its own
        next-step redirect and can't wait; the invite modal has no such
        constraint.
        """
        if not InviteService._validate_email_format(email):
            return {"success": False, "error": "Invalid recipient email"}

        settings = _get_smtp_settings()
        required = ["smtp_host", "smtp_port", "smtp_username", "smtp_password", "from_email"]
        missing = [k for k in required if not settings.get(k)]
        if missing:
            return {
                "success": False,
                "error": f"SMTP not configured (missing: {', '.join(missing)})",
            }

        # Mint the JWT the recipient will click. Empty secret_key would
        # produce a token nobody can verify, so refuse early — the route
        # always passes ``current_app.config['SECRET_KEY']``.
        if not secret_key:
            return {"success": False, "error": "Missing signing key"}
        try:
            token = InviteTokenService.generate_token(
                invitee_email=email,
                # F8 / the workspace is the authoritative scope
                # the invite lands in. We still pass workspace_esid in
                # the JWT's ``org_esid`` slot — that's the F8 token
                # shape; didn't expand the JWT to a separate
                # workspace claim. ``team_esid`` is now an opt-in slice
                #: when the inviter picked a team on the
                # modal, it rides through and the receiver skips the
                # missing-team picker entirely.
                org_esid=workspace_esid,
                team_esid=team_esid or None,
                role_esid=role_esid,
                inviter=inviter_username,
                secret_key=secret_key,
            )
        except Exception as e:  # noqa: BLE001 — top-level boundary
            logging.error("InviteService.send_invite_email: token mint failed: %s", e)
            return {"success": False, "error": "Could not mint invite token"}

        # If jti is supplied we reuse it on the token — but the
        # generator always assigns a fresh jti, so resends mint a new
        # JWT per send. The ES doc keeps the original jti for the
        # consume correlation; the LATEST JWT's jti also lands
        # in wizard_session.state on receive — both are accepted at the
        # consume site as a defense-in-depth fall-through. Logged so a
        # follow-up can audit the resend-correlation behaviour.
        if jti:
            logging.info(
                "InviteService: resend jti correlation — doc_jti=%s new_token_minted (jti rotates per token)",
                jti,
            )

        base_url = _get_app_base_url(settings)
        invite_url = f"{base_url}/onboarding/start?invite={token}"
        logo_url = f"{base_url}/static/kavanah-analytics-logo.svg" if base_url else ""

        subject = "You've been invited to Kavanah Analytics"
        # follow-up: resolve full_name via the shared helper so
        # the greeting reads "Hi Alice Smith" instead of "Hi alice"
        # when the inviter's ES Security record carries a display name.
        # Falls back to the username on miss / ES outage.
        inviter_display = InviteService._get_inviter_display_name(inviter_username)
        note_block_html = ""
        note_block_text = ""
        if note:
            # HTML-escape the note before embedding. The note is user-
            # supplied free text; without escaping a malicious inviter
            # could inject markup into the recipient's email. Use the
            # standard library so we don't pull in a templating engine
            # just for this.
            from html import escape as _html_escape

            safe_note = _html_escape(note)
            note_block_html = (
                '<table role="presentation" width="100%" cellpadding="0" cellspacing="0" border="0" '
                'style="background-color:#FAFAFA;border:1px solid #D5DBDB;border-radius:6px;margin:20px 0;">'
                '<tr><td style="padding:14px 18px;font-size:14px;color:#000716;">'
                f'<p style="margin:0 0 6px;"><strong>Personal note from {_html_escape(inviter_display)}:</strong></p>'
                f'<p style="margin:0;font-style:italic;">"{safe_note}"</p>'
                "</td></tr></table>"
            )
            note_block_text = f'\n\nPersonal note from {inviter_display}:\n"{note}"\n'

        html_body = f"""<!DOCTYPE html>
<html>
<head>
<meta charset="UTF-8">
<title>{subject}</title>
</head>
<body style="margin:0;padding:0;background-color:#F1F3F3;font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',Roboto,Helvetica,Arial,sans-serif;color:#000716;">
<table role="presentation" width="100%" cellpadding="0" cellspacing="0" border="0" style="background-color:#F1F3F3;">
  <tr><td align="center" style="padding:24px 12px;">
    <table role="presentation" width="600" cellpadding="0" cellspacing="0" border="0" style="max-width:600px;width:100%;background-color:#ffffff;border-radius:8px;border:1px solid #e9ebed;">
      <tr><td style="background-color:#232F3E;padding:16px 24px;border-radius:8px 8px 0 0;">
        <table role="presentation" cellpadding="0" cellspacing="0" border="0"><tr>
          <td style="vertical-align:middle;width:32px;"><img src="{logo_url}" width="28" height="28" alt="" style="display:block;border:0;"></td>
          <td style="vertical-align:middle;padding-left:10px;color:#ffffff;font-size:17px;font-weight:700;">Kavanah Analytics</td>
        </tr></table>
      </td></tr>
      <tr><td style="padding:32px 28px;background-color:#ffffff;color:#000716;font-size:15px;line-height:1.6;">
        <h2 style="margin:0 0 16px;color:#000716;font-size:20px;font-weight:700;">You've been invited</h2>
        <p style="margin:0 0 12px;">Hello,</p>
        <p style="margin:0 0 12px;"><strong>{inviter_display}</strong> has invited you to collaborate on Kavanah Analytics. Click below to accept the invitation and set up your account.</p>
        {note_block_html}
        <table role="presentation" cellpadding="0" cellspacing="0" border="0" style="margin:20px 0;"><tr>
          <td style="border-radius:6px;background-color:#0972D3;">
            <a href="{invite_url}" style="display:inline-block;padding:14px 32px;color:#ffffff;text-decoration:none;font-weight:600;font-size:15px;border-radius:6px;">Accept invitation</a>
          </td>
        </tr></table>
        <p style="margin:16px 0 4px;font-size:13px;color:#5F6B7A;">This invitation expires in 7 days. If the button doesn't work, copy and paste this URL into your browser:</p>
        <p style="margin:0 0 16px;"><a href="{invite_url}" style="color:#0972D3;word-break:break-all;">{invite_url}</a></p>
        <p style="margin:0;font-size:13px;color:#5F6B7A;">If you didn't expect this invitation, you can safely ignore this email.</p>
      </td></tr>
      <tr><td style="padding:16px 28px 24px;border-top:1px solid #e9ebed;background-color:#ffffff;color:#5F6B7A;font-size:12px;line-height:1.5;border-radius:0 0 8px 8px;">
        This is an automated message — please do not reply.
      </td></tr>
    </table>
  </td></tr>
</table>
</body>
</html>"""

        text_body = (
            f"You've been invited to Kavanah Analytics\n\n"
            f"Hello,\n\n"
            f"{inviter_display} has invited you to collaborate on Kavanah Analytics. "
            f"Accept the invitation by visiting:\n\n"
            f"{invite_url}\n"
            f"{note_block_text}"
            f"\nThis invitation expires in 7 days.\n\n"
            f"If you didn't expect this invitation, you can safely ignore this email."
        )

        msg = MIMEMultipart("alternative")
        msg["Subject"] = subject
        msg["From"] = settings["from_email"]
        msg["To"] = email
        msg.attach(MIMEText(text_body, "plain"))
        msg.attach(MIMEText(html_body, "html"))

        try:
            if settings.get("smtp_use_tls", True):
                server = smtplib.SMTP(settings["smtp_host"], int(settings["smtp_port"]), timeout=SMTP_TIMEOUT_SECONDS)
                server.starttls(context=smtp_ssl_context())
            else:
                server = smtplib.SMTP_SSL(
                    settings["smtp_host"], int(settings["smtp_port"]), timeout=SMTP_TIMEOUT_SECONDS, context=smtp_ssl_context()
                )
            server.login(settings["smtp_username"], settings["smtp_password"])
            server.sendmail(settings["from_email"], email, msg.as_string())
            server.quit()
            logging.info(
                "InviteService: invite email sent to %s by inviter=%s workspace=%s",
                email,
                inviter_username,
                workspace_esid,
            )
            return {"success": True}
        except Exception as e:  # noqa: BLE001 — top-level boundary
            logging.error(
                "InviteService: failed to send invite to %s: %s",
                email,
                e,
            )
            return {"success": False, "error": "Could not send the invite email."}
