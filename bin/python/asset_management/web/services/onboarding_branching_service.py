"""
Onboarding Branching Service

Maps an ``account_type`` (the enum persisted step 2)
to the downstream defaults the wizard renders on steps 3, 4, and 6:

* **Step 3 — workspace**: title template, copy strings, and whether the
  agency client field is rendered.
* **Step 4 — connect a source**: which connector tile is highlighted
  out of the box (Meta Ads for most flows; deferred for invited users
  because the inviting org likely already has sources hooked up).
* **Step 6 — pick assets**: the default for the "share this connection
  across the whole organization" toggle (advanced). Agencies want
  org-shared by default so their client workspaces can reuse a single
  Meta login; everyone else wants per-workspace.

The service is pure: no ES calls, no Flask request context. The step
2 POST writes ``account_type`` to user.metadata + wizard_session.state;
this service reads that single value back out and computes the rest.
That keeps step 3/4/6 routes (when they ship) thin — they call
``OnboardingBranchingService.for_account_type(account_type, ...)`` and
read ``defaults.step3_workspace_title``, ``defaults.step6_org_shared_default``,
etc., without re-deriving the branching rules.

The "Reload at step 3 still shows correct defaults" acceptance bullet
on the ticket is satisfied by this design: defaults are deterministic
from the stored ``account_type``, so a refresh resolves the same
defaults the user saw before the refresh.

Storage decision is recorded in ``the private docs``
(entry: ``2026-05-13 — ``).
"""

from __future__ import annotations

from dataclasses import dataclass

from asset_management.web.services.user_profile_service import (
    ACCOUNT_TYPE_AGENCY,
    ACCOUNT_TYPE_INDIVIDUAL,
    ACCOUNT_TYPE_INVITED,
    ACCOUNT_TYPE_ORG_OWNER,
    DEFAULT_ACCOUNT_TYPE,
    VALID_ACCOUNT_TYPES,
)

# ─────────────────────────────────────────────────────────────────────────────
# Step 3 copy keys
#
# The wizard renders copy server-side, so the keys here are the final
# strings, not i18n placeholders. If a future ticket adds translation we
# swap these for keys into a strings catalogue; until then the service
# owns the copy so a step-3 template change does not have to touch
# branching logic.
# ─────────────────────────────────────────────────────────────────────────────

# step-3 input is now the ORGANIZATION name (the thing the
# user actually sees in their org/team filter dropdowns), not the
# workspace name. The workspace title is server-derived from the
# org name via ``derive_workspace_title`` (see OnboardingDefaults below).
#
# Defaults pre-fill the input with the user's email_local so a user can
# Continue without typing — but the field is editable and the hint
# nudges them to use a real org name. Org_owner used to pre-fill from
# the email DOMAIN via the route's ``_title_from_domain`` helper; that
# remains the GET default for org_owner specifically (e.g. "example-org.org"
# -> "Example Org") because it's the better-quality default for that
# variant. Individual + agency stay on email_local.
#
# Historical: earlier the templates carried "'s Workspace" / "'s
# Team" / "'s Agency" suffixes because the input WAS the workspace
# name. earlier the input is the org name so those suffixes are
# dropped; the workspace title gets a system-generated "-workspace"
# suffix in derive_workspace_title.
WORKSPACE_TITLE_INDIVIDUAL = "{email_local}"
WORKSPACE_TITLE_ORG_OWNER = "{email_local}"
WORKSPACE_TITLE_AGENCY = "{email_local}"
WORKSPACE_TITLE_INVITED = ""  # invited users never see step 3 — the invite scope is set

# heading is "Workspace Settings" because the input is no
# longer the workspace name itself (it's the org name; the workspace
# title is system-generated). The card holds Org Name + timezone +
# currency + fiscal-year — all the per-workspace settings the user
# might want to set at signup.
STEP3_TITLE_INDIVIDUAL = "Workspace Settings"
STEP3_TITLE_ORG_OWNER = "Workspace Settings"
# F9 / agency wizard still names two things —
# the agency itself (drives both the workspace name AND the agency org
# name) and the first client org. keeps the dual-input shape
# but renames the headline from "Name your agency workspace and your
# first client" to a settings-style heading.
STEP3_TITLE_AGENCY = "Workspace Settings"
STEP3_TITLE_INVITED = ""

# subtitle copy speaks to what the org-name input drives
# (the visible filter dropdown), not what the workspace title drives
# (nothing the user sees). follow-up (2026-05-21): the
# org_owner pre-fill is derived from email_local (the part before
# `@`, e.g. "jane_doe" for "jane_doe@example.com"), NOT from
# the email domain. The copy said "from your email" which was
# technically accurate (the username IS the email local-part) but
# user-confusing — the user read it as "we guessed from your
# @example.com domain" and was looking for a Teamexpansion-style
# default. "Username" is closer to what the user actually sees.
STEP3_SUB_INDIVIDUAL = "Name the organization you'll see in filters and dropdowns. You can change this later."
STEP3_SUB_ORG_OWNER = "We've made a guess from your username — change it if you want."
# F9: load-bearing terminology fix. Pre-F9 said "Each client
# lives in its own workspace under your agency" — wrong under the new
# tenant model. Clients are ORGANIZATIONS inside the agency workspace.
# The org-vs-workspace distinction is the load-bearing piece: a future
# copy refresh that drifts back to "own workspace" silently undoes the
# whole four-tier model in the user's head.
STEP3_SUB_AGENCY = (
    "Each client lives in its own organization inside your agency workspace. " "You can add more clients after setup."
)
STEP3_SUB_INVITED = ""

# labels reflect the new semantics. The input drives the
# ORG title (visible in dropdowns); the workspace title is derived
# server-side as "{org}-workspace" and the user never sees it.
STEP3_LABEL_INDIVIDUAL = "Organization name"
STEP3_LABEL_ORG_OWNER = "Organization name"
# F9 / agency users see "Agency name" because the
# input drives the agency workspace title AND the workspace
# (workspace_title = "{agency_name}-workspace"). Distinct from the
# first-client input below.
STEP3_LABEL_AGENCY = "Agency name"
STEP3_LABEL_INVITED = ""

# the suffix appended to the org-name input to produce the
# workspace title. Workspace titles are never user-visible to non-
# superusers (Layer-1 scope hides them; only admin User Management
# surfaces show them), so a fixed system suffix is fine. Kept as a
# module constant so the test can assert against the exact format and
# a future copy change doesn't have to dig through the service.
WORKSPACE_TITLE_SUFFIX = "-workspace"

# Step 4 default connector. The agency / individual / org_owner flows
# all default to Meta as the primary funnel path. Google Ads, GA4, and
# YouTube ARE wired through the wizard (tokenless
# finalize; Data Connectors modal hand-off) — Meta stays
# the default because it is the native in-wizard OAuth flow, not because
# the others are unreachable. (An earlier note here cited the
# cohort gate — superseded;.)
# ``None`` means "do not preselect" and is reserved for invited users:
# their inviting org has already connected sources, so step 4 should
# offer "Skip — use the team's sources" instead of forcing a fresh
# OAuth round-trip.
STEP4_CONNECTOR_DEFAULT = "meta_ads"
STEP4_CONNECTOR_INVITED = None

# Step 6 "share this connection across the workspace" toggle default.
# Agency mode is True because a single Meta login typically holds the
# ad accounts for every client; per-workspace duplication would force
# the agency owner through OAuth once per client. Everyone else
# defaults to per-workspace (False) so a personal account doesn't
# leak into a future shared org.
STEP6_ORG_SHARED_AGENCY = True
STEP6_ORG_SHARED_DEFAULT = False


@dataclass(frozen=True)
class OnboardingDefaults:
    """Step 3/4/6 defaults for a given ``account_type``.

    Frozen so a consumer that captures a reference can't accidentally
    mutate the branching result mid-render and confuse the next step.
    The wizard layer reads these fields and feeds them into its templates;
    nothing in this dataclass knows about Flask, ES, or i18n.
    """

    account_type: str

    # Step 3 — workspace step
    step3_title: str
    step3_subtitle: str
    step3_label: str
    step3_workspace_title_template: str
    step3_show_agency_client_field: bool

    # Step 4 — connect a source step
    step4_default_connector: str | None  # None = "do not preselect"
    step4_offer_skip_use_team_sources: bool

    # Step 6 — pick assets step
    step6_org_shared_default: bool
    step6_show_advanced_org_shared_toggle: bool

    def step3_workspace_title(self, *, email_local: str = "") -> str:
        """Render the org-name input default against the given email local-part.

        Kept as a method (rather than the route formatting the template
        itself) so the template-with-placeholder convention is owned by
        the branching service. semantic note: this method now
        returns the ORG-NAME default the user sees in the step-3 input,
        not the workspace title. The workspace title is derived from
        that input via ``derive_workspace_title`` after the user
        confirms.
        """
        try:
            return self.step3_workspace_title_template.format(
                email_local=email_local or "",
            )
        except (KeyError, IndexError):
            # Defensive: an unknown placeholder in the template would
            # otherwise crash the wizard mid-flow. Fall back to the
            # raw template so the user at least sees something usable.
            return self.step3_workspace_title_template

    @staticmethod
    def derive_workspace_title(org_title: str) -> str:
        """Produce the workspace title from the user-typed org name.

        Workspace titles are not user-visible to non-superusers (Layer-1
        scope), so this is a system-generated string with a
        deterministic suffix. The shape is "{org_title}-workspace".

        Returns an empty string when ``org_title`` is empty — callers
        should validate the org-name input before reaching this method;
        this is a defensive fallback that lets the route's "title
        required" branch surface its existing error rather than landing
        a bare "-workspace" doc.

        No uniqueness guarantee on the output: WorkspaceSlugService
        carries the dedup load via slug collision-resolution. Two
        users typing the same org name produce the same workspace
        title, distinct workspace_slug values (e.g. "acme-workspace"
        and "acme-workspace-2"), and two separate workspace docs.
        That's by design — see DECISIONS.md "2026-05-21 — "
        for the trade-off rationale.
        """
        cleaned = (org_title or "").strip()
        if not cleaned:
            return ""
        return f"{cleaned}{WORKSPACE_TITLE_SUFFIX}"


class OnboardingBranchingService:
    """Server-side branching for steps 3, 4, and 6.

    Consumed by the (future) step-3/4/6 routes. The wizard layer calls
    ``for_account_type(account_type)`` once per render and reads the
    relevant fields off the returned ``OnboardingDefaults``.

    The service deliberately accepts the raw enum string (not the user
    record) so the caller can compute defaults from whichever copy of
    ``account_type`` is closest at hand — the user's metadata, the
    wizard_session state blob, or a Settings-page override that hasn't
    been written yet.
    """

    @staticmethod
    def for_account_type(account_type: str | None) -> OnboardingDefaults:
        """Return the defaults for a single ``account_type``.

        An unknown / missing value normalises to the
        ``DEFAULT_ACCOUNT_TYPE`` defaults (currently ``org_owner``) —
        same fallback policy as ``UserProfileService.get_account_type``,
        so the wizard never dead-ends on a corrupted record.
        """
        if account_type not in VALID_ACCOUNT_TYPES:
            account_type = DEFAULT_ACCOUNT_TYPE

        if account_type == ACCOUNT_TYPE_INDIVIDUAL:
            return OnboardingDefaults(
                account_type=account_type,
                step3_title=STEP3_TITLE_INDIVIDUAL,
                step3_subtitle=STEP3_SUB_INDIVIDUAL,
                step3_label=STEP3_LABEL_INDIVIDUAL,
                step3_workspace_title_template=WORKSPACE_TITLE_INDIVIDUAL,
                step3_show_agency_client_field=False,
                step4_default_connector=STEP4_CONNECTOR_DEFAULT,
                step4_offer_skip_use_team_sources=False,
                step6_org_shared_default=STEP6_ORG_SHARED_DEFAULT,
                step6_show_advanced_org_shared_toggle=False,
            )

        if account_type == ACCOUNT_TYPE_AGENCY:
            return OnboardingDefaults(
                account_type=account_type,
                step3_title=STEP3_TITLE_AGENCY,
                step3_subtitle=STEP3_SUB_AGENCY,
                step3_label=STEP3_LABEL_AGENCY,
                step3_workspace_title_template=WORKSPACE_TITLE_AGENCY,
                step3_show_agency_client_field=True,
                step4_default_connector=STEP4_CONNECTOR_DEFAULT,
                step4_offer_skip_use_team_sources=False,
                step6_org_shared_default=STEP6_ORG_SHARED_AGENCY,
                step6_show_advanced_org_shared_toggle=True,
            )

        if account_type == ACCOUNT_TYPE_INVITED:
            return OnboardingDefaults(
                account_type=account_type,
                step3_title=STEP3_TITLE_INVITED,
                step3_subtitle=STEP3_SUB_INVITED,
                step3_label=STEP3_LABEL_INVITED,
                step3_workspace_title_template=WORKSPACE_TITLE_INVITED,
                step3_show_agency_client_field=False,
                step4_default_connector=STEP4_CONNECTOR_INVITED,
                step4_offer_skip_use_team_sources=True,
                step6_org_shared_default=STEP6_ORG_SHARED_DEFAULT,
                step6_show_advanced_org_shared_toggle=False,
            )

        # ACCOUNT_TYPE_ORG_OWNER (and any normalised-to-default value)
        return OnboardingDefaults(
            account_type=ACCOUNT_TYPE_ORG_OWNER,
            step3_title=STEP3_TITLE_ORG_OWNER,
            step3_subtitle=STEP3_SUB_ORG_OWNER,
            step3_label=STEP3_LABEL_ORG_OWNER,
            step3_workspace_title_template=WORKSPACE_TITLE_ORG_OWNER,
            step3_show_agency_client_field=False,
            step4_default_connector=STEP4_CONNECTOR_DEFAULT,
            step4_offer_skip_use_team_sources=False,
            step6_org_shared_default=STEP6_ORG_SHARED_DEFAULT,
            step6_show_advanced_org_shared_toggle=False,
        )
