"""
Onboarding Service (, workspace-first refactor / F7)

Wizard write path: orchestrates the workspace + org + team + membership +
role-assignment writes for each of the five onboarding entry points
defined in the epic, restructured under the spike's
four-tier tenant model (workspace > org > team > data).

Stub PR. Downstream wizard tickets own the HTTP route and
the UI; this module is the service surface they call into. No route
is wired here. The companion new-onboarding feature flag
gated the callers during the rollout; it was retired once
the wizard became the unconditional signup path.

Storage substrate:

* Workspace documents live in ``de_asset_workspace`` (F4 /).
* Org documents live in ``de_asset_organization``; carry
  ``workspace_esid`` since F5 /.
* Team documents live in ``de_asset_teams`` (existing).
* Memberships and role assignments live in the ``de_authz_*`` indices
  bootstrapped by ``PermissionsIndexService`` — including the new
  workspace-tier indices from F8 /.

The five entry points produce the following writes under the workspace-
first model:

* ``individual``    -> workspace + transparent org + team + workspace_owner@workspace
* ``org_owner``     -> workspace + named org + "Team 1" team (default) + workspace_owner@workspace
* ``invited``       -> no creation; workspace + org/team memberships + roles at invite scope
* ``agency``        -> workspace(is_agency=true) + first-client ORG + first-client team + workspace_owner
* ``domain_join``   -> no creation; workspace + org memberships + workspace_member role

All five are idempotent on the natural keys (workspace slug / org title /
team title / username:scope_esid:role_esid), so a step-3 refresh that
re-fires ``provision_individual`` etc. does not double-create.

Acceptance criteria :

* All four entry points produce correct memberships.
* Idempotent (refresh during step 3 does not double-create).

The interface returns a structured ``OnboardingResult`` so the wizard
layer can surface the created ESIDs (workspace / org / team) for
subsequent steps without re-querying ES.

Pre-F7 history: every ``provision_*`` started with the org write. The
agency variant relied on ``is_multi_workspace=True`` on the org doc to
flag agency mode; the spike retired that pattern in favour of
``is_agency=True`` on the workspace doc. The org-level flag is left
writable during the transition window so legacy readers don't break,
but new code reads ``WorkspaceMembershipService.is_agency_for_user``
instead.
"""

import logging
import sys
from dataclasses import dataclass, field
from datetime import datetime, timezone

sys.path.insert(0, "/app/python")
from lib.es_helpers import (
    TeamSlugCollisionError,
    check_team_slug_unique,
    dual_term,
    generate_es_safe_doc_id,
    get_elasticsearch_client,
)

from asset_management.web.services.permissions_index_service import (
    USER_ORG_ROLE_INDEX,
    USER_TEAM_ROLE_INDEX,
    USER_WORKSPACE_ROLE_INDEX,
    WORKSPACE_INDEX,
)
from asset_management.web.services.permissions_seeder_service import (
    ROLE_OWNER,
    ROLE_VIEWER,
    ROLE_WORKSPACE_OWNER,
    ROLE_WORKSPACE_VIEWER,
)
from asset_management.web.services.workspace_settings_service import (
    DEFAULT_CURRENCY,
    DEFAULT_FISCAL_YEAR_START_MONTH,
    DEFAULT_TIMEZONE,
)
from asset_management.web.services.workspace_slug_service import (
    WorkspaceSlugService,
)
from asset_management.web.services.auto_join_domain_service import (
    AutoJoinDomainService,
)
from asset_management.web.services.wizard_session_service import (
    WizardSessionService,
)

# key under the onboarding ``wizard_session.state`` that holds a
# domain-match join the user accepted but whose email is not yet verified.
PENDING_DOMAIN_JOIN_STATE_KEY = "pending_domain_join"
ONBOARDING_WIZARD_TYPE = "onboarding"

# ─────────────────────────────────────────────────────────────────────────────
# Index names — orgs and teams live in the existing asset indices.
# Workspace lives in de_asset_workspace (F4 /). Doc id is the
# workspace_slug, so a step-3 refresh that re-fires _upsert_workspace
# resolves the same slug → same doc id → no-op write via op_type=create
# + 409-swallow.
# ─────────────────────────────────────────────────────────────────────────────

ORG_INDEX = "de_asset_organization"
TEAM_INDEX = "de_asset_teams"

# ─────────────────────────────────────────────────────────────────────────────
# Entry-point enum
# ─────────────────────────────────────────────────────────────────────────────

ENTRY_INDIVIDUAL = "individual"
ENTRY_ORG_OWNER = "org_owner"
ENTRY_INVITED = "invited"
ENTRY_AGENCY = "agency"
ENTRY_DOMAIN_JOIN = "domain_join"

VALID_ENTRY_POINTS = (
    ENTRY_INDIVIDUAL,
    ENTRY_ORG_OWNER,
    ENTRY_INVITED,
    ENTRY_AGENCY,
    ENTRY_DOMAIN_JOIN,
)


@dataclass
class OnboardingResult:
    """Outcome of an entry-point provisioning call.

    ``created`` reflects whether the workspace / org / team were
    freshly created on this call. On a step-3 refresh all three flags
    flip to ``False`` and the existing ESIDs are returned, so the
    wizard's confirm screen is stable across replays.

    ``workspace_esid`` is the F7 addition. Pre-F7 every
    provisioning call started with the org write; under the new tenant
    model the workspace is the top-level tier, so it's the ESID
    downstream wizard steps key off (membership lookup, navigation
    gate, etc.).
    """

    entry_point: str
    username: str
    workspace_esid: str | None = None
    workspace_created: bool = False
    org_esid: str | None = None
    org_created: bool = False
    team_esid: str | None = None
    team_created: bool = False
    role_esid: str | None = None
    errors: list[str] = field(default_factory=list)

    @property
    def success(self) -> bool:
        return not self.errors


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _email_domain(email: str | None) -> str | None:
    """Return the domain part of an email address, or None if unparseable.

    The org-owner entry point names the new org after the email
    domain ('example-org.org' → "Example-org"). Falls back to
    None so the caller can route to a manual-title prompt instead of
    creating a garbage-named org.
    """
    if not email or "@" not in email:
        return None
    domain = email.rsplit("@", 1)[1].strip().lower()
    if not domain or "." not in domain:
        return None
    return domain


def _title_from_domain(domain: str) -> str:
    """Turn 'kavanahmedia.com' into 'Kavanahmedia'."""
    return domain.split(".", 1)[0].capitalize()


class OnboardingService:
    """Wizard write path for the onboarding epic.

    All methods are best-effort write orchestrators that return an
    ``OnboardingResult``. Per-entry-point methods accept whatever the
    wizard step actually collects (email for ``org_owner``, target
    scope ESIDs for ``invited``, client name for ``agency``); the
    common pieces — username, normalisation, idempotent writes —
    live in private helpers.
    """

    # ─────────────────────────────────────────────────────────────────────
    # Public API — one method per entry point
    # ─────────────────────────────────────────────────────────────────────

    @staticmethod
    def provision_individual(
        username: str,
        *,
        workspace_title: str | None = None,
        org_title: str | None = None,
        team_title: str | None = None,
    ) -> OnboardingResult:
        """Individual entry point (workspace-first / F7).

        earlier one title fanned out across workspace + transparent
        org + team. split the workspace title from the org/team
        title:

        * ``workspace_title`` — the system-derived "{org}-workspace"
          string, invisible to non-superusers (Layer-1 scope).
        * ``org_title`` — the user-typed org name, visible in the
          org/team filter dropdowns. Defaults to ``workspace_title`` when
          None for backward compat with earlier callers that passed
          only one title.
        * ``team_title`` — the user-typed team name from the
          step-5 form. Defaults to ``org_title`` when None/empty, which
          reproduces the earlier behaviour (the individual team was
          always named after the org).

        Workspace role: ``workspace_owner`` on the new workspace.

        Both titles fall back to ``f"{username}'s Workspace"`` /
        ``f"{username}'s Organization"`` respectively when empty, so
        any caller that pre-dates the title plumbing keeps
        working without a code change.
        """
        result = OnboardingResult(entry_point=ENTRY_INDIVIDUAL, username=username)
        if not username:
            result.errors.append("username is required")
            return result

        es = get_elasticsearch_client()
        if es is None:
            result.errors.append("elasticsearch unavailable")
            return result

        ws_title = (workspace_title or "").strip() or f"{username}'s Workspace"
        # org_title defaults to ws_title when caller didn't
        # pass it (earlier callers). Once the route is updated, the
        # caller always passes both.
        o_title = (org_title or "").strip() or ws_title

        workspace_esid, workspace_created = OnboardingService._upsert_workspace(
            es, title=ws_title, is_agency=False, exclude_username=username
        )
        if workspace_esid is None:
            result.errors.append("workspace create failed")
            return result
        result.workspace_esid = workspace_esid
        result.workspace_created = workspace_created

        org_esid, org_created = OnboardingService._upsert_org(
            es, title=o_title, is_multi_workspace=False, workspace_esid=workspace_esid
        )
        if org_esid is None:
            result.errors.append("org create failed")
            return result
        result.org_esid = org_esid
        result.org_created = org_created

        # team title is the user-typed step-5 value when
        # provided; otherwise it falls back to the org title (the
        # earlier individual default).
        t_title = (team_title or "").strip() or o_title
        team_esid, team_created = OnboardingService._upsert_team(
            es, title=t_title, org_esid=org_esid, workspace_esid=workspace_esid
        )
        if team_esid is None:
            result.errors.append("team create failed")
            return result
        result.team_esid = team_esid
        result.team_created = team_created

        OnboardingService._upsert_workspace_membership(es, username, workspace_esid, is_primary=True)
        OnboardingService._upsert_workspace_role_assignment(es, username, workspace_esid, ROLE_WORKSPACE_OWNER)
        OnboardingService._upsert_org_membership(es, username, org_esid, is_primary=True)
        OnboardingService._upsert_team_membership(es, username, team_esid, is_primary=True)
        OnboardingService._upsert_org_role_assignment(es, username, org_esid, ROLE_OWNER)
        # Defect B — team-tier role assignment was the only missing
        # tier; workspace + org tiers already get owner-role rows above, but
        # de_authz_user_team_role stayed empty for every wizard signup. Mirrors
        # the org pattern: ROLE_OWNER on the team they just created.
        OnboardingService._upsert_team_role_assignment(es, username, team_esid, ROLE_OWNER)
        result.role_esid = ROLE_WORKSPACE_OWNER
        return result

    @staticmethod
    def provision_org_owner(
        username: str,
        email: str | None,
        *,
        workspace_title: str | None = None,
        org_title: str | None = None,
        team_title: str | None = None,
    ) -> OnboardingResult:
        """Org owner entry point (workspace-first / F7).

        earlier workspace and org shared the same title.
        split them:

        * ``workspace_title`` — system-derived "{org}-workspace", not
          user-visible to non-superusers.
        * ``org_title`` — user-typed org name, visible in dropdowns.
          Defaults to ``workspace_title`` (earlier callers) when
          None.
        * ``team_title`` — the user-typed team name from the
          step-5 form. Defaults to ``"Team 1"`` when None/empty (renamed
          from the historical "General" default per a teammate 2026-06-22).
          The step-5 field is required now, so the route always passes a
          non-empty value; this fallback only fires for direct/programmatic
          callers that omit ``team_title``.

        Both fall back to the email-domain capitalisation, then to
        ``f"{username}'s Organization"`` if email is unparseable. Team
        title defaults to "Team 1" under the org. Workspace role:
        ``workspace_owner``.
        """
        result = OnboardingResult(entry_point=ENTRY_ORG_OWNER, username=username)
        if not username:
            result.errors.append("username is required")
            return result

        es = get_elasticsearch_client()
        if es is None:
            result.errors.append("elasticsearch unavailable")
            return result

        explicit_ws = (workspace_title or "").strip()
        if explicit_ws:
            ws_title = explicit_ws
        else:
            domain = _email_domain(email)
            if domain:
                ws_title = _title_from_domain(domain)
            else:
                ws_title = f"{username}'s Organization"

        # org_title independent of ws_title. Falls back to the
        # same domain-derived default for earlier callers (matches
        # legacy "workspace and org share one title" behaviour when only
        # workspace_title was passed).
        explicit_org = (org_title or "").strip()
        if explicit_org:
            o_title = explicit_org
        else:
            o_title = ws_title

        workspace_esid, workspace_created = OnboardingService._upsert_workspace(
            es, title=ws_title, is_agency=False, exclude_username=username
        )
        if workspace_esid is None:
            result.errors.append("workspace create failed")
            return result
        result.workspace_esid = workspace_esid
        result.workspace_created = workspace_created

        org_esid, org_created = OnboardingService._upsert_org(
            es, title=o_title, is_multi_workspace=False, workspace_esid=workspace_esid
        )
        if org_esid is None:
            result.errors.append("org create failed")
            return result
        result.org_esid = org_esid
        result.org_created = org_created

        # team title is the user-typed step-5 value when
        # provided; otherwise it falls back to "Team 1" (renamed from the
        # historical "General" org_owner default per a teammate 2026-06-22).
        t_title = (team_title or "").strip() or "Team 1"
        team_esid, team_created = OnboardingService._upsert_team(
            es, title=t_title, org_esid=org_esid, workspace_esid=workspace_esid
        )
        if team_esid is None:
            result.errors.append("team create failed")
            return result
        result.team_esid = team_esid
        result.team_created = team_created

        OnboardingService._upsert_workspace_membership(es, username, workspace_esid, is_primary=True)
        OnboardingService._upsert_workspace_role_assignment(es, username, workspace_esid, ROLE_WORKSPACE_OWNER)
        OnboardingService._upsert_org_membership(es, username, org_esid, is_primary=True)
        OnboardingService._upsert_team_membership(es, username, team_esid, is_primary=True)
        OnboardingService._upsert_org_role_assignment(es, username, org_esid, ROLE_OWNER)
        # Defect B — see provision_individual; team-tier role row.
        OnboardingService._upsert_team_role_assignment(es, username, team_esid, ROLE_OWNER)
        result.role_esid = ROLE_WORKSPACE_OWNER
        return result

    @staticmethod
    def provision_invited(
        username: str,
        *,
        workspace_esid: str | None = None,
        org_esid: str | None = None,
        team_esid: str | None = None,
        role_esid: str = ROLE_VIEWER,
        workspace_role_esid: str | None = None,
        is_primary_workspace: bool = False,
    ) -> OnboardingResult:
        """Invited entry point (workspace-aware / F7).

        No workspace / org / team creation — the invite already names
        the scope. Writes memberships + role assignments at whichever
        scope (workspace / org / team) the caller passed in.

        Under the new tenant model, every invite carries a
        ``workspace_esid`` (the F8 invite-token format extension). The
        org/team scopes are sub-scopes of that workspace, but the
        caller passes them explicitly so this method doesn't have to
        look them up. ``role_esid`` defaults to Viewer (org/team tier).

        ``workspace_role_esid`` is the workspace-tier role to grant
        . Defaults to ``workspace_member`` when None so existing
        callers keep the previous "every invite is read-only at the
        workspace tier" behaviour. consumer passes the
        invite-token's ``role_esid`` through here too, because today's
        earlier invites scope to workspace only (no team_esid on
        the token), so the invite's role would otherwise
        be silently dropped.

        ``is_primary_workspace=True`` for users who don't already
        belong to any workspace (first-time signup via invite); False
        for users joining a second workspace (the switcher UI will
        be enabled by the F8 ``is_multi_workspace_user_supported``
        flag in a future ticket).
        """
        result = OnboardingResult(entry_point=ENTRY_INVITED, username=username)
        if not username:
            result.errors.append("username is required")
            return result
        if not workspace_esid and not org_esid and not team_esid:
            result.errors.append("invited entry point requires workspace_esid, org_esid, or team_esid")
            return result

        es = get_elasticsearch_client()
        if es is None:
            result.errors.append("elasticsearch unavailable")
            return result

        if workspace_esid:
            OnboardingService._upsert_workspace_membership(es, username, workspace_esid, is_primary=is_primary_workspace)
            OnboardingService._upsert_workspace_role_assignment(
                es,
                username,
                workspace_esid,
                workspace_role_esid or ROLE_WORKSPACE_VIEWER,  # renamed from member
                is_primary=is_primary_workspace,
            )
            result.workspace_esid = workspace_esid
        if org_esid:
            OnboardingService._upsert_org_membership(es, username, org_esid, is_primary=False)
            OnboardingService._upsert_org_role_assignment(es, username, org_esid, role_esid)
            result.org_esid = org_esid
        if team_esid:
            OnboardingService._upsert_team_membership(es, username, team_esid, is_primary=False)
            OnboardingService._upsert_team_role_assignment(es, username, team_esid, role_esid)
            result.team_esid = team_esid
        result.role_esid = role_esid
        return result

    @staticmethod
    def provision_domain_join(
        username: str,
        *,
        org_esid: str,
        workspace_esid: str | None = None,
        role_esid: str = ROLE_VIEWER,
        is_primary_workspace: bool = True,
    ) -> OnboardingResult:
        """Domain-match auto-join entry point.

        The signup email matched an org's ``auto_join_email_domains``
        allowlist and the user clicked "Join". No workspace / org /
        team creation — the matched org already exists, and (post-F5)
        carries a ``workspace_esid`` that links it to its parent
        workspace.

        Under the new tenant model the wizard's domain-match prompt
        looks up the workspace allowlist (per the spike's Q3
        answer): the user is joining a workspace, and the org is
        either auto-picked (if the workspace has exactly one org) or
        chosen from a sub-picker (if the workspace has multiple). The
        caller passes both ESIDs explicitly so this method doesn't
        have to look up the workspace from the org doc.

        ``workspace_esid`` is optional for backwards compatibility
        during the F7 transition — callers that haven't been updated
        pass only ``org_esid`` and skip the workspace-tier writes.
        Once every caller is updated, the field becomes required.

        Defaults to Viewer for the same reason ``provision_invited``
        does: a domain match is a weaker signal of intent than an
        explicit invite, so the floor role is read-only. Workspace
        role: ``workspace_member`` — the lowest-privilege workspace
        tier.
        """
        result = OnboardingResult(entry_point=ENTRY_DOMAIN_JOIN, username=username)
        if not username:
            result.errors.append("username is required")
            return result
        if not org_esid:
            result.errors.append("domain_join entry point requires org_esid")
            return result

        es = get_elasticsearch_client()
        if es is None:
            result.errors.append("elasticsearch unavailable")
            return result

        if workspace_esid:
            OnboardingService._upsert_workspace_membership(es, username, workspace_esid, is_primary=is_primary_workspace)
            OnboardingService._upsert_workspace_role_assignment(
                es, username, workspace_esid, ROLE_WORKSPACE_VIEWER, is_primary=is_primary_workspace
            )  # renamed from member
            result.workspace_esid = workspace_esid

        OnboardingService._upsert_org_membership(es, username, org_esid, is_primary=False)
        OnboardingService._upsert_org_role_assignment(es, username, org_esid, role_esid)
        result.org_esid = org_esid
        result.role_esid = role_esid
        return result

    # ─────────────────────────────────────────────────────────────────────
    # domain-match join waits for email verification
    # ─────────────────────────────────────────────────────────────────────

    @staticmethod
    def record_pending_domain_join(
        username: str,
        *,
        org_esid: str,
        org_title: str,
        domain: str,
        email: str,
    ) -> bool:
        """Stash an accepted domain-match join until the email is verified.

        The join prompt fires on the email the user TYPED, which nobody has
        verified yet, so writing the org role at that point let anyone who
        typed ``x@victim-domain`` into an opted-in org. The join is parked on
        the onboarding wizard session instead and applied by
        :meth:`apply_pending_domain_join` once the inbox link is clicked.

        Returns True iff the stash was saved.
        """
        if not username or not org_esid:
            return False
        active = WizardSessionService.get_active(username, ONBOARDING_WIZARD_TYPE) or {}
        state = dict(active.get("state") or {})
        state[PENDING_DOMAIN_JOIN_STATE_KEY] = {
            "org_esid": org_esid,
            "org_title": org_title,
            "domain": domain,
            "email": email,
            "requested_at": _now_iso(),
        }
        return WizardSessionService.save(
            user_id=username,
            wizard_type=ONBOARDING_WIZARD_TYPE,
            current_step=active.get("current_step") or "verify-email",
            state=state,
        )

    @staticmethod
    def _verified_email(username: str) -> str | None:
        """The user's current email iff ``metadata.email_verified`` is set.

        One ES Security read for both values so the address checked against
        the auto-join domain is the one that was verified. ``update_email``
        clears the flag when the address changes, so a verified flag always
        belongs to the current address. ``None`` on any failure (fail closed).
        """
        es = get_elasticsearch_client()
        if es is None:
            return None
        try:
            user = dict(es.security.get_user(username=username)).get(username)
        except Exception as e:  # noqa: BLE001 — fail closed: no verified email
            logging.warning("OnboardingService._verified_email(%s): %s", username, e)
            return None
        if not user or (user.get("metadata") or {}).get("email_verified") is not True:
            return None
        return (user.get("email") or "").strip().lower() or None

    @staticmethod
    def apply_pending_domain_join(username: str) -> OnboardingResult | None:
        """Apply a parked domain-match join once the email is verified.

        Called from the email-verification handlers. Returns ``None`` when
        there is nothing to apply yet: no pending join, the email is not
        verified, or the verified address no longer matches the same org
        (the user changed their email, or the org dropped the domain). In
        those cases no org role is written. On a successful provision the
        pending entry is cleared, so repeat calls are no-ops.
        """
        if not username:
            return None
        active = WizardSessionService.get_active(username, ONBOARDING_WIZARD_TYPE)
        if not active:
            return None
        state = dict(active.get("state") or {})
        pending = state.get(PENDING_DOMAIN_JOIN_STATE_KEY)
        if not isinstance(pending, dict) or not pending.get("org_esid"):
            return None

        verified_email = OnboardingService._verified_email(username)
        if not verified_email:
            return None

        # Re-run the matcher on the VERIFIED address rather than trusting the
        # stash: the join is only owed to someone who controls a mailbox at a
        # domain the org opted in, and that must still be the same org.
        match = AutoJoinDomainService.find_match(verified_email)
        if not match or match.get("org_esid") != pending["org_esid"]:
            logging.warning(
                "pending domain join for %s not applied; verified email no longer matches org %s",
                username,
                pending["org_esid"],
            )
            return None

        result = OnboardingService.provision_domain_join(username=username, org_esid=pending["org_esid"])
        if result.success:
            state.pop(PENDING_DOMAIN_JOIN_STATE_KEY, None)
            WizardSessionService.save(
                user_id=username,
                wizard_type=ONBOARDING_WIZARD_TYPE,
                current_step=active.get("current_step") or "verify-email",
                state=state,
            )
        return result

    @staticmethod
    def provision_agency(
        username: str,
        *,
        agency_title: str,
        first_client_title: str,
    ) -> OnboardingResult:
        """Agency entry point (workspace-first / F7).

        The biggest semantic shift in F7 lives here. Pre-F7 the agency
        wizard wrote a single org with ``is_multi_workspace=true`` plus
        a "first client workspace" as a TEAM under it. The
        spike retired that pattern:

        * The **workspace** is the agency container; ``is_agency=true``
          replaces the deprecated org-level ``is_multi_workspace`` flag.
        * The "first client" the user names is an **organization**
          under the agency workspace — not a team. (The spike's load-
          bearing terminology correction: clients are orgs, not
          workspaces.)
        * Each client org gets its own default team named after the
          client so the import path has somewhere to attach data.

        Workspace role: ``workspace_owner`` on the agency workspace.
        Future client orgs added from inside the workspace become new
        orgs alongside the first, all under the same workspace.
        """
        result = OnboardingResult(entry_point=ENTRY_AGENCY, username=username)
        if not username:
            result.errors.append("username is required")
            return result
        if not agency_title or not first_client_title:
            result.errors.append("agency entry point requires agency_title and first_client_title")
            return result

        es = get_elasticsearch_client()
        if es is None:
            result.errors.append("elasticsearch unavailable")
            return result

        # Agency workspace — is_agency=True is the canonical "this is
        # an agency" marker post-F7. Old code reading org.is_multi_workspace
        # is being rewritten ticket-by-ticket; (bundled in this
        # PR) is the navigation gate that consumes is_agency.
        workspace_esid, workspace_created = OnboardingService._upsert_workspace(
            es, title=agency_title, is_agency=True, exclude_username=username
        )
        if workspace_esid is None:
            result.errors.append("workspace create failed")
            return result
        result.workspace_esid = workspace_esid
        result.workspace_created = workspace_created

        # First client = ORG (not team). is_multi_workspace stays False
        # on this org — agency-mode lives on the workspace doc now.
        org_esid, org_created = OnboardingService._upsert_org(
            es, title=first_client_title, is_multi_workspace=False, workspace_esid=workspace_esid
        )
        if org_esid is None:
            result.errors.append("org create failed")
            return result
        result.org_esid = org_esid
        result.org_created = org_created

        team_esid, team_created = OnboardingService._upsert_team(
            es, title=first_client_title, org_esid=org_esid, workspace_esid=workspace_esid
        )
        if team_esid is None:
            result.errors.append("team create failed")
            return result
        result.team_esid = team_esid
        result.team_created = team_created

        OnboardingService._upsert_workspace_membership(es, username, workspace_esid, is_primary=True)
        OnboardingService._upsert_workspace_role_assignment(es, username, workspace_esid, ROLE_WORKSPACE_OWNER)
        OnboardingService._upsert_org_membership(es, username, org_esid, is_primary=True)
        OnboardingService._upsert_team_membership(es, username, team_esid, is_primary=True)
        OnboardingService._upsert_org_role_assignment(es, username, org_esid, ROLE_OWNER)
        # Defect B — see provision_individual; team-tier role row.
        OnboardingService._upsert_team_role_assignment(es, username, team_esid, ROLE_OWNER)
        result.role_esid = ROLE_WORKSPACE_OWNER
        return result

    @staticmethod
    def add_client_to_workspace(
        username: str,
        *,
        workspace_esid: str,
        client_title: str,
    ) -> OnboardingResult:
        """Add a new client org + team to an existing agency workspace.

        Post-onboarding shortcut for an agency owner who has already
        provisioned a workspace via ``provision_agency`` and now wants
        a 2nd / 3rd / Nth client without re-running the full wizard
        (do-order #44 — "Adding 2nd client doesn't require re-running
        the full wizard").

        Subset of ``provision_agency``:
        * NO workspace create (the workspace already exists).
        * NO workspace-membership / workspace-role writes (the caller
          already has ``workspace_owner`` on the workspace; the new
          client doesn't change that).
        * Creates ORG (under the existing workspace) + TEAM (under the
          new org).
        * Stamps org membership ``is_primary=False`` — the caller's
          primary org is whichever they provisioned first; this is
          additional scope.
        * Stamps team membership ``is_primary=False`` for the same
          reason.
        * Stamps ``org_role_assignment = ROLE_OWNER`` on the new client
          org so the caller can manage data under it.

        Idempotency: same natural-key reuse pattern as
        ``provision_agency``. A duplicate POST with the same title
        returns the existing org/team ESIDs with ``created=False`` on
        both flags.

        The currency picker collected in the modal is persisted on the
        org doc itself (a future ticket can promote per-org currency
        scoping); for now we store it as ``default_currency`` on the
        org doc so the import path can default-currency-tag new
        connections under this client.
        """
        result = OnboardingResult(entry_point=ENTRY_AGENCY, username=username)
        if not username:
            result.errors.append("username is required")
            return result
        if not workspace_esid:
            result.errors.append("workspace_esid is required")
            return result
        if not client_title:
            result.errors.append("client_title is required")
            return result

        es = get_elasticsearch_client()
        if es is None:
            result.errors.append("elasticsearch unavailable")
            return result

        result.workspace_esid = workspace_esid

        # Client = ORG (NOT a separate workspace). is_multi_workspace
        # stays False on the client org; agency-mode lives on the
        # parent workspace doc per the F7 invariant. The workspace_esid
        # link is the canonical multi-tenant scoping signal.
        org_esid, org_created = OnboardingService._upsert_org(
            es, title=client_title, is_multi_workspace=False, workspace_esid=workspace_esid
        )
        if org_esid is None:
            result.errors.append("org create failed")
            return result
        result.org_esid = org_esid
        result.org_created = org_created

        team_esid, team_created = OnboardingService._upsert_team(
            es, title=client_title, org_esid=org_esid, workspace_esid=workspace_esid
        )
        if team_esid is None:
            result.errors.append("team create failed")
            return result
        result.team_esid = team_esid
        result.team_created = team_created

        # is_primary=False — caller's primary scope was set during the
        # original provision_agency call. A user can be a member of
        # many client orgs/teams under the same agency workspace.
        OnboardingService._upsert_org_membership(es, username, org_esid, is_primary=False)
        OnboardingService._upsert_team_membership(es, username, team_esid, is_primary=False)
        OnboardingService._upsert_org_role_assignment(es, username, org_esid, ROLE_OWNER)
        # Defect B — team-tier role row, same as the three provision_*
        # variants. The agency owner gets ROLE_OWNER on each client team they
        # spawn so de_authz_user_team_role lookups (e.g. RoleFilterService)
        # see the assignment without falling back to org-tier inference.
        OnboardingService._upsert_team_role_assignment(es, username, team_esid, ROLE_OWNER)
        result.role_esid = ROLE_OWNER
        return result

    @staticmethod
    def rename_provisioned_workspace(
        username: str,
        *,
        workspace_esid: str,
        org_esid: str,
        team_esid: str | None = None,
        new_workspace_title: str,
        new_org_title: str,
        rename_team: bool = False,
        new_team_title: str | None = None,
    ) -> OnboardingResult:
        """Rename an already-provisioned workspace + org (+ optional team) in place.

         follow-up. The step-3 rename guard catches the duplicate-
        workspace risk; this method is the actual rename. Updates the
        human-readable titles WITHOUT touching the doc id / slug — the
        slug is the URL identifier and stays stable per the spike's
        slug-stability rule (Slack/Linear pattern: ``acme`` stays
        ``acme`` even if the workspace is renamed to "Acme Inc 2026").

        Updated fields:

        * ``de_asset_workspace.workspace_title`` — the human label;
          ``workspace_slug`` and the doc id are untouched.
        * ``de_asset_organization.organization_title`` — the org name
          that surfaces in filter dropdowns.
        * ``de_asset_teams.team_title`` — only when ``rename_team=True``.
          renamed to ``new_team_title`` when the caller supplies
          it (the step-5 team-name field for individual + org_owner), else
          falls back to ``new_org_title`` for back-compat with earlier
          callers (the individual variant where the team was named after
          the org). Agency still skips the team rename — its team belongs
          to a separate client org and isn't named in step 5.

        Idempotent: passing the same titles as the existing doc is a
        no-op ES update; the call still returns ``success=True`` so
        the caller doesn't need to compare before/after.

        Returns an :class:`OnboardingResult` whose ``workspace_created``
        / ``org_created`` / ``team_created`` flags are all False (the
        docs already existed; this is a rename, not a create). On
        partial failure (workspace updated but org failed) the result
        carries the error AND the ESIDs so the caller can report
        what happened.
        """
        result = OnboardingResult(entry_point=ENTRY_INDIVIDUAL, username=username)
        if not username:
            result.errors.append("username is required")
            return result
        if not workspace_esid or not org_esid:
            result.errors.append("workspace_esid and org_esid are required for rename")
            return result
        if not new_workspace_title or not new_org_title:
            result.errors.append("new_workspace_title and new_org_title are required")
            return result

        es = get_elasticsearch_client()
        if es is None:
            result.errors.append("elasticsearch unavailable")
            return result

        result.workspace_esid = workspace_esid
        result.org_esid = org_esid
        result.team_esid = team_esid

        try:
            es.update(
                index=WORKSPACE_INDEX,
                id=workspace_esid,
                doc={"workspace_title": new_workspace_title},
                refresh="wait_for",
            )
        except Exception as e:
            logging.error(
                "OnboardingService.rename: workspace_title update failed for %s: %s",
                workspace_esid,
                e,
            )
            result.errors.append(f"workspace rename failed: {e}")
            return result

        try:
            es.update(
                index=ORG_INDEX,
                id=org_esid,
                doc={"organization_title": new_org_title},
                refresh="wait_for",
            )
        except Exception as e:
            logging.error(
                "OnboardingService.rename: organization_title update failed for %s: %s",
                org_esid,
                e,
            )
            result.errors.append(f"org rename failed: {e}")
            return result

        if rename_team and team_esid:
            # rename the team to the user-typed team name when
            # supplied; fall back to the org title for earlier callers.
            team_rename_title = (new_team_title or "").strip() or new_org_title
            try:
                es.update(
                    index=TEAM_INDEX,
                    id=team_esid,
                    doc={"team_title": team_rename_title},
                    refresh="wait_for",
                )
            except Exception as e:
                logging.error(
                    "OnboardingService.rename: team_title update failed for %s: %s",
                    team_esid,
                    e,
                )
                result.errors.append(f"team rename failed: {e}")
                return result

        return result

    # ─────────────────────────────────────────────────────────────────────
    # Internal: idempotent org / team upserts keyed by title.
    #
    # Title is the natural key for both indices because that is what
    # the legacy Phase-14 dual-write path (``_provision_new_authz_assignments``
    # in app.py) uses to resolve ``team_<title>`` / ``org_<title>``
    # ES Security role strings back to ESIDs. Using the same key here
    # means a step-3 refresh that re-fires the entry-point method
    # finds the org/team already in ES and returns the existing ESID.
    # ─────────────────────────────────────────────────────────────────────

    @staticmethod
    def _upsert_workspace(
        es,
        *,
        title: str,
        is_agency: bool,
        exclude_username: str | None = None,
    ) -> tuple[str | None, bool]:
        """Return ``(workspace_esid, created)``.

        Resolves the title to a deterministic slug via
        ``WorkspaceSlugService.resolve_unique_slug`` (which walks the
        ``slug``, ``slug-2``, ``slug-3``… ladder until a free value is
        found) and uses the slug as the workspace doc id. This is the
        same doc-id convention F4's bootstrap script uses for the
        Kavanah singleton, so the wizard's first agency signup writing
        to ``de_asset_workspace`` lands a doc whose shape matches what
        was already there.

        Idempotency: deterministic doc id + ``op_type=create`` +
        409-as-success. A step-3 refresh that re-fires this method with
        the same title resolves the same slug → same doc id → 409
        swallow → returns ``(existing_esid, False)``.

        ``is_agency`` lives on the workspace doc post-F7 — the
        deprecated ``de_asset_organization.is_multi_workspace`` flag
        stays writable on the org tier for the transition window but
        is no longer the canonical "this is an agency" signal.
        navigation gate consumes ``workspace.is_agency`` via
        ``WorkspaceMembershipService.is_agency_for_user``.
        """
        slug = WorkspaceSlugService.resolve_unique_slug(title, exclude_username=exclude_username)
        if not slug:
            logging.error("OnboardingService: workspace slug resolution failed for %r", title)
            return None, False

        # Check for an existing doc at the resolved slug first — if
        # present, this is the no-op refresh case and we return the
        # existing ESID without attempting a write.
        try:
            existing = es.get(index=WORKSPACE_INDEX, id=slug)
            return existing["_id"], False
        except Exception as e:
            if "not_found" not in str(e).lower() and "404" not in str(e):
                # Real ES error, not just doc-missing.
                logging.warning("OnboardingService: workspace lookup failed for %s: %s", slug, e)
                # Fall through to create — the create path will surface
                # the same error if ES is truly broken.

        doc = {
            "workspace_title": title,
            "workspace_slug": slug,
            "is_agency": is_agency,
            # F4 / switcher UI hidden when N=1.
            # A future ticket flips this on a per-workspace basis.
            "is_multi_workspace_user_supported": False,
            "timezone": DEFAULT_TIMEZONE,
            "currency": DEFAULT_CURRENCY,
            "fiscal_year_start_month": DEFAULT_FISCAL_YEAR_START_MONTH,
            "created_at": _now_iso(),
            "created_by": "onboarding_wizard",
        }
        try:
            es.index(
                index=WORKSPACE_INDEX,
                id=slug,
                document=doc,
                op_type="create",
                refresh="wait_for",
            )
            return slug, True
        except Exception as e:
            if "version_conflict" in str(e).lower():
                # Lost a race to a parallel call — the other call's doc
                # is canonical. Return the slug as the ESID; the doc
                # contents may differ but the natural key is the same.
                return slug, False
            logging.error("OnboardingService: workspace create failed for %r: %s", title, e)
            return None, False

    @staticmethod
    def _upsert_org(
        es,
        *,
        title: str,
        is_multi_workspace: bool,
        workspace_esid: str | None = None,
    ) -> tuple[str | None, bool]:
        """Return ``(org_esid, created)``.

        Looks up an org by ``organization_title`` first; creates one
        only when no match exists. ``is_multi_workspace`` is set on
        creation and refreshed on the existing doc (cheap, single-doc
        update) so a wizard rerun that toggles agency-mode lands the
        right flag.

        ``workspace_esid`` (F7 addition): if provided, written onto
        the org doc on create so F5's per-workspace slug scoping has
        the link from day one. Optional during the F7 transition so
        legacy callers that haven't been updated still work.
        """
        try:
            hit = es.search(
                index=ORG_INDEX,
                body={"query": {"match": {"organization_title": title}}, "size": 1},
            )
        except Exception as e:
            logging.warning("OnboardingService: org lookup failed for %r: %s", title, e)
            return None, False

        hits = hit.get("hits", {}).get("hits", [])
        if hits:
            esid = hits[0]["_id"]
            update_doc = {"is_multi_workspace": is_multi_workspace}
            if workspace_esid:
                update_doc["workspace_esid"] = workspace_esid
            try:
                es.update(
                    index=ORG_INDEX,
                    id=esid,
                    doc=update_doc,
                    refresh="wait_for",
                )
            except Exception as e:
                logging.warning("OnboardingService: org flag refresh failed for %s: %s", esid, e)
            return esid, False

        doc = {
            "organization_title": title,
            "is_multi_workspace": is_multi_workspace,
            "@timestamp": _now_iso(),
        }
        if workspace_esid:
            doc["workspace_esid"] = workspace_esid
        try:
            response = es.index(index=ORG_INDEX, document=doc, refresh="wait_for")
            return response["_id"], True
        except Exception as e:
            logging.error("OnboardingService: org create failed for %r: %s", title, e)
            return None, False

    @staticmethod
    def _upsert_team(es, *, title: str, org_esid: str, workspace_esid: str | None = None) -> tuple[str | None, bool]:
        """Return ``(team_esid, created)``.

        Lookup key is ``team_title`` scoped by ``organization_esid``
        so two orgs may have a "General" team without collision.
        ``organization_esid`` is mapped as ``text`` with a ``.keyword``
        sub-field, so the ``term`` query must target the ``.keyword``
        leaf — a ``term`` against the raw text field will silently
        miss (the analyzer never produces a matching token) and the
        method would happily create a duplicate team on the rerun.

        ``workspace_esid`` is stamped on the new team doc so
        ``cascade_purge_empty_workspace`` (which enumerates child teams
        by ``term:workspace_esid``) can find them. Pre-fix the wizard's
        team-create path was the only team-create site that didn't
        stamp this field, leaving wizard-created teams orphaned across
        a workspace delete + resign cycle. Optional kwarg for backward
        compat with any caller that doesn't yet thread the value.
        """
        try:
            hit = es.search(
                index=TEAM_INDEX,
                body={
                    "query": {
                        "bool": {
                            "must": [
                                {"match": {"team_title": title}},
                                # dual-term — a.keyword-only match
                                # silently found no existing team on bare-keyword
                                # prod, so this dedup minted duplicate teams.
                                dual_term("organization_esid", org_esid),
                            ]
                        }
                    },
                    "size": 1,
                },
            )
        except Exception as e:
            logging.warning(
                "OnboardingService: team lookup failed for %r under %s: %s",
                title,
                org_esid,
                e,
            )
            return None, False

        hits = hit.get("hits", {}).get("hits", [])
        if hits:
            return hits[0]["_id"], False

        # before creating a new team, reject if the sanitized
        # alias slug would collide with another team in this org. Idempotency
        # is preserved because the exact-title `match` above already returned
        # for the no-op refresh case.
        try:
            check_team_slug_unique(es, team_title=title, org_esids=[org_esid])
        except TeamSlugCollisionError as exc:
            logging.warning(
                "OnboardingService: team create rejected for %r in org %s — " "slug %r collides with team %s",
                title,
                org_esid,
                exc.slug,
                exc.existing_team_esid,
            )
            return None, False

        doc = {
            "team_title": title,
            "organization_esid": [org_esid],
            "@timestamp": _now_iso(),
        }
        if workspace_esid:
            doc["workspace_esid"] = workspace_esid
        try:
            # explicit ES-safe _id (alphanumeric leading char) so the
            # team_esid never produces an invalid {team_esid}__{source} index name.
            safe_id = generate_es_safe_doc_id()
            response = es.index(index=TEAM_INDEX, id=safe_id, document=doc, refresh="wait_for")
            return response["_id"], True
        except Exception as e:
            logging.error("OnboardingService: team create failed for %r: %s", title, e)
            return None, False

    # ─────────────────────────────────────────────────────────────────────
    # Internal: idempotent authz writes.
    # Deterministic doc IDs make duplicate inserts no-ops via ``op_type=create``
    # 409s — same convention as the Phase 14 dual-write path.
    # ─────────────────────────────────────────────────────────────────────

    @staticmethod
    def _upsert_workspace_membership(es, username: str, workspace_esid: str, *, is_primary: bool) -> None:
        """Write a ``de_authz_workspace_membership`` doc (F7 /).

         (P3b): membership docs are RETIRED — this is now a no-op.
        "Belongs to a workspace" == "holds a workspace role" (the role doc
        carries ``is_primary``). The companion
        ``_upsert_workspace_role_assignment`` call at every site is what now
        records the relationship; this method is kept (no-op) so the 5
        provision paths don't all need re-wiring in this phase.
        """
        del es, username, workspace_esid, is_primary  # retired: no membership write

    @staticmethod
    def _upsert_workspace_role_assignment(
        es, username: str, workspace_esid: str, role_esid: str, *, is_primary: bool = True
    ) -> None:
        """Write a ``de_authz_user_workspace_role`` doc (F7 /).

        Mirrors ``_upsert_org_role_assignment`` / ``_upsert_team_role_assignment``
        — deterministic doc id ``{username}:{workspace_esid}:{role_esid}``
        + ``op_type=create`` 409-swallow. Workspace roles are seeded
        by ``PermissionsSeederService``: ``workspace_owner`` /
        ``workspace_admin`` / ``workspace_viewer`` (; legacy
        ``workspace_member`` still resolves).

         (P3a): the role doc now carries ``is_primary``. This is
        the signal the membership doc used to own; as membership is
        retired (P61), ``WorkspaceMembershipService.get_primary_workspace``
        resolves the primary workspace from the role docs instead.
        Defaults to True — the Phase-1 single-workspace-per-user case,
        and the safe default for any caller that doesn't track it.
        """
        OnboardingService._safe_create(
            es,
            index=USER_WORKSPACE_ROLE_INDEX,
            doc_id=f"{username}:{workspace_esid}:{role_esid}",
            body={
                "username": username,
                "workspace_esid": workspace_esid,
                "role_esid": role_esid,
                "is_primary": is_primary,
                "created_at": _now_iso(),
            },
        )

    @staticmethod
    def _upsert_org_membership(es, username: str, org_esid: str, *, is_primary: bool) -> None:
        # (P3b): membership docs retired — no-op. The org ROLE doc
        # (_upsert_org_role_assignment) records the relationship; nothing reads
        # de_authz_org_membership any more.
        del es, username, org_esid, is_primary

    @staticmethod
    def _upsert_team_membership(es, username: str, team_esid: str, *, is_primary: bool) -> None:
        # (P3b): membership docs retired — no-op (see _upsert_org_membership).
        del es, username, team_esid, is_primary

    @staticmethod
    def _upsert_org_role_assignment(es, username: str, org_esid: str, role_esid: str) -> None:
        OnboardingService._safe_create(
            es,
            index=USER_ORG_ROLE_INDEX,
            doc_id=f"{username}:{org_esid}:{role_esid}",
            body={
                "username": username,
                "org_esid": org_esid,
                "role_esid": role_esid,
                "created_at": _now_iso(),
            },
        )

    @staticmethod
    def _upsert_team_role_assignment(es, username: str, team_esid: str, role_esid: str) -> None:
        OnboardingService._safe_create(
            es,
            index=USER_TEAM_ROLE_INDEX,
            doc_id=f"{username}:{team_esid}:{role_esid}",
            body={
                "username": username,
                "team_esid": team_esid,
                "role_esid": role_esid,
                "created_at": _now_iso(),
            },
        )

    @staticmethod
    def _safe_create(es, *, index: str, doc_id: str, body: dict) -> None:
        """``op_type=create`` write that treats 409 as the happy path.

        Idempotency contract: deterministic doc ID + create-or-409
        means a step-3 refresh that re-fires the wizard's provision
        call is a no-op.
        """
        try:
            es.index(index=index, id=doc_id, document=body, op_type="create", refresh="wait_for")
        except Exception as e:
            if "version_conflict" in str(e).lower():
                return  # already exists — desired
            logging.warning("OnboardingService: write to %s id=%s failed: %s", index, doc_id, e)
