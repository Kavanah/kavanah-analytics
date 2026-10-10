"""
Dashboard Checklist Service (, extended)

The persistent yellow checklist card on the dashboard right rail. The
new discovery surface that replaces Asset Explorer's first-run role for
ongoing setup discovery: a user who finished the onboarding wizard but
hasn't connected a second source, customised KPIs, scheduled a weekly
summary, etc. still needs a "what's next" affordance on every
/dashboard hit.

 shipped **the card shell + two wired items**. appended
the remaining five standard items (add another data source, invite a
teammate, customize KPIs, schedule weekly summary, set conversion
goals) — for a total of seven. adds **conditional items**:
items that only appear when their user/org-state predicate is true.
Four of them ship: verify_email (only if the user's email is not yet
verified), add_client (agency-only — the AC says it replaces the
"Invite a teammate" headline for agencies, so this module also hides
invite_teammate when add_client is visible), enable_2fa (deferred —
stub gated False until new-device tracking lands) and connect_billing
(deferred — stub gated False until trial-end timestamps ship).

 wires **per-item Skip**. A user can mark any individual row as
"done" without performing the action — useful for items that don't
apply (e.g. a solo user skipping ``invite_teammate``). A skipped row
renders as checked-off so the visual matches the AC's "moves to the
done state visually" bullet, but ``ChecklistItem.skipped`` distinguishes
it from a genuinely-completed item for analytics. The same contract
applies to standard AND conditional rows: a skipped conditional renders
done+skipped in place rather than dropping out, so the UX is uniform
across both tiers (the AC's "moves to the done state visually" reads
literally regardless of which tuple the item lives in). Each skip
persists to ``de_dashboard_checklist_skip`` keyed
``{username}:{workspace_esid}:{item_id}`` and is undone via the
``unskip`` surface — every skip is reversible.

 wires the **individual variant**: a solo user (``account_type
== "individual"``) sees ``invite_teammate`` hidden entirely (no
teammate to invite when there is no team) and the two "build out
your own dashboard" rows — ``add_another_source`` and
``customize_kpis`` — promoted to the top of the standard block as the
headline items. Agency-only conditional rows (``add_client``) are
already gated by ``is_agency_for_user`` which is False for individuals,
so the "hide agency-only items" AC bullet falls out for free; the
``_INDIVIDUAL_HEADLINE_IDS`` reorder is the new behaviour. The
``account_type`` lives on the user (canonical reader
``UserProfileService.get_account_type``), NOT on the workspace —
two users in the same workspace with different ``account_type``
values each see their own variant, which is the AC's "Variations by
entry point" intent.

 wires **auto-collapse-to-header**. Once the user crosses 80%
progress the card collapses to a slim header bar showing
"Finish setting up — N% done" plus an Expand toggle. The collapse
state is tristate-persisted on the dismiss doc (``collapse_state``
column: ``"collapsed"``, ``"expanded"``, or absent). Absent means
"follow the auto rule" (collapsed iff progress >= 80%); an explicit
value pins the user's choice through page refreshes so the AC's
"Expand state survives page refresh" bullet is exercised verbatim.
The dismiss doc is shared (not a new index) so the existing per-user
tenant boundary (``{username}:{workspace_esid}`` doc id) ports over
without a new mapping or per-user sweep entry. The
``_STANDARD_ITEMS`` / ``_CONDITIONAL_ITEMS`` tuples are shaped so
future additions land as a pure append.

Org scope per the acceptance criteria: progress is per-workspace, not
per-user. Two users in the same workspace see the same "X of N done"
counter because both predicates evaluate against the workspace's data
(connections live under ``de_connection.principal == workspace_esid``;
first-look dashboards live under ``de_asset_echarts_dashboards`` with
``allowed_team_esids`` intersecting the workspace's teams). Dismiss
state IS per-user (per the AC's "per-user dismissibility") and is keyed
``{username}:{workspace_esid}`` so a teammate's dismiss doesn't hide
the card for everyone else.

Storage:
  ``de_dashboard_checklist_dismiss`` — one doc per (user, workspace)
  recording when the user dismissed the card. Bootstrap lazily on first
  write (no migration; matches every other new-index in this codebase).
  Doc-id is ``{username}:{workspace_esid}`` so dismiss-state lookups
  are a single ``es.get`` not an ``es.search``.

Dismissibility:
  Card is dismissible at any progress (; was ``>= 80`` under
  ). The template shows the X whenever the card has rows AND the
  ``dismiss`` server method
  returns ``False`` under threshold — defense in depth, so a forged
  POST can't dismiss a barely-started checklist.
"""

from __future__ import annotations

import logging
import sys
from dataclasses import dataclass, field
from datetime import datetime, timezone

sys.path.insert(0, "/app/python")

from lib.es_helpers import (  # noqa: E402
    create_index_if_not_exists,
    get_elasticsearch_client,
    put_regular_index_template,
)

try:
    from web.url_prefix import assets_path
except ImportError:
    from asset_management.web.url_prefix import assets_path  # noqa: E402

try:
    from web.services.workspace_membership_service import WorkspaceMembershipService
except ImportError:
    from asset_management.web.services.workspace_membership_service import (  # noqa: E402
        WorkspaceMembershipService,
    )

try:
    from web.services.onboarding_email_verification_service import (
        OnboardingEmailVerificationService,
    )
except ImportError:
    from asset_management.web.services.onboarding_email_verification_service import (  # noqa: E402
        OnboardingEmailVerificationService,
    )

try:
    from web.services.user_profile_service import (
        ACCOUNT_TYPE_INDIVIDUAL,
        UserProfileService,
    )
except ImportError:
    from asset_management.web.services.user_profile_service import (  # noqa: E402
        ACCOUNT_TYPE_INDIVIDUAL,
        UserProfileService,
    )


INDEX_DISMISS = "de_dashboard_checklist_dismiss"

INDEX_DISMISS_MAPPING = {
    "properties": {
        "username": {"type": "keyword"},
        "workspace_esid": {"type": "keyword"},
        "dismissed_at": {"type": "date"},
        # tristate collapse preference on the same doc.
        # ``"collapsed"`` / ``"expanded"`` pin the user's explicit
        # choice; an absent field means "follow the auto rule"
        # (collapsed when ``progress_pct >= COLLAPSE_THRESHOLD_PCT``).
        # Stored as a keyword so the column is queryable in Kibana
        # ad-hoc for analytics ("what % of users explicitly expand?").
        "collapse_state": {"type": "keyword"},
        "collapse_state_updated_at": {"type": "date"},
    }
}

# per-item "Skip this" affordance. One doc per
# (user, workspace, item) records that the user chose to skip that
# checklist row. Schema is intentionally a superset of the dismiss
# index so analytics ("Skipped vs completed distinguishable
# in analytics") can join the two indices on (username, workspace_esid)
# and filter on item_id. The doc id is deterministic
# ``{username}:{workspace_esid}:{item_id}`` so per-item lookups are
# a single ``es.get``; bulk reads for the checklist render use a
# single ``es.search`` with a term-filter on (username, workspace_esid).
INDEX_SKIP = "de_dashboard_checklist_skip"

INDEX_SKIP_MAPPING = {
    "properties": {
        "username": {"type": "keyword"},
        "workspace_esid": {"type": "keyword"},
        "item_id": {"type": "keyword"},
        "skipped_at": {"type": "date"},
    }
}

# Once a user has knocked out 80% of the items the card has done its
# job. The remaining 20% can be re-opened on demand via the
# undismiss surface.
DISMISS_THRESHOLD_PCT = 80

# once a user has crossed this threshold the card auto-
# collapses to a header bar (title + progress %, with an Expand
# toggle). The ticket's AC reads "4 of 5 standard items done" — a
# literal 80% ratio that aligns with DISMISS_THRESHOLD_PCT. We
# deliberately share the threshold value so the two affordances stay
# coherent: collapse hides the noise at 80%; dismiss removes the
# card entirely at the same point. Future redesign that decouples
# them should override this constant rather than rewriting the
# auto-rule.
COLLAPSE_THRESHOLD_PCT = 80

# the two persisted values for the tristate. Anything else
# is treated as "no preference" (auto-rule applies). Defined as
# constants so the route handlers, the service writers, and the
# test stay in lock-step on the wire format.
COLLAPSE_STATE_COLLAPSED = "collapsed"
COLLAPSE_STATE_EXPANDED = "expanded"
_VALID_COLLAPSE_STATES = (COLLAPSE_STATE_COLLAPSED, COLLAPSE_STATE_EXPANDED)

INDEX_CONNECTION = "de_connection"
# Issue 3 — the predicate was pointing at the wrong index. User-
# created and first-look-cloned dashboards live under
# ``de_asset_echarts_dashboards`` (per ``DashboardService.get_visible_echarts_dashboards``
# and ``template_service.DASHBOARDS_INDEX``); ``de_asset_dashboards`` is the
# legacy Kibana visibility-config index that pre-dates the ECharts cutover
# and is empty for wizard-signup users. The mismatch meant
# ``first_dashboard_created`` never auto-checked even though the user's
# dashboard rendered fine on /dashboard. Fixed to the ECharts index.
INDEX_DASHBOARDS = "de_asset_echarts_dashboards"
INDEX_WORKSPACE_MEMBERSHIP = "de_authz_workspace_membership"

# follow-up — sentinel used when the user has no primary
# workspace. Before this constant the dismiss/collapse/skip write
# methods all 400'd on no_workspace; from the user's POV every checklist
# button silently failed. The sentinel lets the doc-id construction
# succeed (so persistence works) without colliding with any real
# workspace_esid (workspace esids are random ES auto-ids, never the
# string ``__no_workspace__``). If the user later joins a workspace,
# the sentinel-keyed row is harmlessly orphaned — the new workspace
# gets a fresh row and the read path keys off the user's current
# workspace, not the sentinel.
_NO_WORKSPACE_SENTINEL = "__no_workspace__"

# number of distinct ``de_connection`` rows scoped to the
# workspace required for ``add_another_source`` to flip done. A user
# with one connection has satisfied ``connect_first_source`` but not
# this item; the threshold is 2 so the AC's "Add another data source"
# reads literally.
_MIN_CONNECTIONS_FOR_ADD_ANOTHER = 2

# number of workspace members required for ``invite_teammate``
# to flip done. Threshold is 2 because the wizard auto-creates a
# membership row for the workspace owner; a second row means at least
# one invite went out and was accepted.
_MIN_MEMBERS_FOR_INVITE = 2
# a SENT invite also completes ``invite_teammate``. Index and
# status values mirror ``invite_service.INDEX`` / its ``status`` field
# (pending → accepted | expired | cancelled); only the first two count.
INDEX_INVITES = "de_authz_invite"
_INVITE_STATUSES_THAT_COUNT = ("pending", "accepted")

# individual variant headline. When the user's
# ``account_type == "individual"`` these two standard items move to
# slots 0 + 1 of the rendered standard block (above the rest of the
# standard items but below any visible conditional rows). The intent
# (per the ticket's "Variations by entry point" table) is to put the
# two "build out your own dashboard" affordances at the top because a
# solo user's setup journey is dominated by source coverage + KPI
# tuning rather than team growth. Order within the tuple is the render
# order: ``add_another_source`` then ``customize_kpis``.
_INDIVIDUAL_HEADLINE_IDS: tuple[str, ...] = (
    "add_another_source",
    "customize_kpis",
)


@dataclass(frozen=True)
class ChecklistItem:
    """A single row in the checklist card.

    Frozen so the items tuple stays a stable identity; the template
    iterates a list of new ChecklistItem replicas with ``done`` filled
    in (the predicate output) rather than mutating the originals.
    """

    id: str
    title: str
    description: str
    action_url: str
    action_label: str
    done: bool = False
    # True iff the row is "done" because the user skipped it
    # via the per-item Skip affordance, NOT because they actually
    # completed the underlying action. ``done`` stays True (the row
    # renders as checked-off per the AC's "Skipping moves the item to
    # the done state visually" bullet); ``skipped`` is the analytics
    # discriminator so completion-rate metrics don't count skips. A
    # genuinely completed item has ``done=True, skipped=False``.
    skipped: bool = False


@dataclass(frozen=True)
class ChecklistState:
    """The view object the template consumes.

    ``items`` is the rendered list (each with ``done`` evaluated against
    the user's workspace). ``progress_pct`` is rounded int so the CSS
    ``width: {pct}%`` rule lands on a whole percent.
    """

    items: tuple[ChecklistItem, ...]
    completed_count: int
    total_count: int
    progress_pct: int
    is_dismissed: bool
    is_dismissible: bool
    # derived collapse state the template renders against.
    # True ⇒ render the slim header-bar variant; False ⇒ render the
    # full card. Derivation is in ``get_state``: an explicit user
    # choice (``collapse_state`` field on the dismiss doc) overrides;
    # absence falls back to the auto-rule ``progress_pct >=
    # COLLAPSE_THRESHOLD_PCT``.
    is_collapsed: bool = False
    # follow-up — True when every checklist item is done. The
    # template uses this to auto-hide the entire surface (card AND the
    # 'Re-open setup checklist' button) without requiring a manual
    # dismiss. Distinct from ``is_dismissed`` (which is a user choice
    # at >=80% progress that leaves the re-open chip visible).
    is_complete: bool = False
    workspace_esid: str | None = None
    item_dicts: list[dict] = field(default_factory=list)


# shipped the first two items; appends the remaining
# five. Order is rendered order (top-to-bottom on the card). Each
# item's ``done`` field is the default False here; ``get_state`` fills
# it in from the predicate output. action_url destinations:
#   - connect_first_source / add_another_source → wizard step-4
#     connector tile grid (the canonical "add a source" surface).
#   - first_dashboard_created → /dashboard (the page the user is
#     already on; clicking confirms the item).
# invite_teammate → #invite-modal sentinel. The modal's
#     ``document.body`` click listener intercepts navigation and opens
#     the in-page modal — the row never leaves /dashboard.
#   - customize_kpis → /assets/dashboard-builder/ (the KPI/widget
#     editor the AC alludes to).
#   - schedule_weekly_summary / set_conversion_goals → checklist
#     coming-soon stub (real surfaces ship in separate epics; the AC
#     forbids 404s so we land a friendly placeholder route instead).
# Trimmed to a fixed 5-row block (2026-05-22). The previous 7-row
# variant carried two stub-only rows (``schedule_weekly_summary``,
# ``set_conversion_goals``) whose action_urls pointed at the
# ``/assets/onboarding/checklist/coming-soon`` placeholder page —
# functionally the buttons "didn't go to anything". Removing those rows
# AND disabling all conditional-tier rows below pins the rendered count
# at exactly 5 for every cohort (individual / org_owner / agency /
# invited), so the "X of N done" counter stops jumping between 7, 8, 9.
_STANDARD_ITEMS: tuple[ChecklistItem, ...] = (
    ChecklistItem(
        id="connect_first_source",
        title="Connect a data source",
        description="Meta Ads, Google Ads, GA4 or 30+ others",
        # follow-up (2026-05-21): retargeted from
        # /assets/onboarding/step-6 (wizard connector picker) to
        # /assets/data-connectors/ (the post-wizard connector
        # management surface). The wizard is for first-time setup
        # only; once the user has finished or skipped it, the wizard
        # URLs are blocked (see the wizard-URL guard) and adding a
        # source happens on the data-connectors page.
        action_url=assets_path("/data-connectors/"),
        action_label="Connect",
    ),
    ChecklistItem(
        id="first_dashboard_created",
        title="First dashboard created",
        description="A starter dashboard pinned to your team",
        action_url="/dashboard",
        action_label="View",
    ),
    ChecklistItem(
        id="add_another_source",
        title="Add another data source",
        description="Blend channels for a fuller picture",
        # follow-up — same retarget as connect_first_source.
        action_url=assets_path("/data-connectors/"),
        action_label="Add",
    ),
    ChecklistItem(
        id="invite_teammate",
        title="Invite a teammate",
        description="Share access with a collaborator",
        # sentinel anchor consumed by the invite modal's
        # ``document.body.addEventListener('click', ...)`` in
        # ``_invite_modal.html``. Clicking the row opens the modal in
        # place rather than navigating; replaces the
        # placeholder ``/assets/profile`` URL.
        action_url="#invite-modal",
        action_label="Invite",
    ),
    ChecklistItem(
        id="customize_kpis",
        title="Customize KPIs",
        description="Pick the metrics that matter for you",
        action_url=assets_path("/dashboard-builder/"),
        action_label="Customize",
    ),
)


# conditional items. Each row is paired with a visibility
# predicate in ``_visibility_by_id`` below. An item is rendered only
# when its predicate returns True; otherwise it is omitted from the
# final list AND from completed_count / total_count entirely. The "done"
# flag on a conditional item is semantically different from a standard
# item: a conditional item is visible *because* there is something to
# do, so its done flag is always False while it's visible. Once the
# user finishes the action, the predicate flips False and the row
# disappears (rather than rendering as a checked-off row). This matches
# the AC's "No item ever shown to a user who can't act on it" bullet —
# a verified user does not see "Verify your email — done"; they see
# nothing at all in that slot.
#
# action_url destinations:
#   - verify_email → /assets/profile (the profile page surfaces the
# "resend verification" affordance wired up).
#   - add_client → /assets/onboarding/step-5 (the agency-specific
#     workspace step is where the client-org form lives today; the
#     dedicated "add another client" surface is a separate ticket).
#   - enable_2fa / connect_billing → coming-soon stub. Their real
#     destinations ship in separate epics; the visibility predicate
#     stays gated False until then so the row never renders, but the
#     coming-soon route is whitelisted defensively in case a forced
#     visible-flip ever surfaces a click.
_CONDITIONAL_ITEMS: tuple[ChecklistItem, ...] = (
    ChecklistItem(
        id="verify_email",
        title="Verify your email",
        description="Confirm your email address to keep your account secure",
        action_url=assets_path("/profile"),
        action_label="Verify",
    ),
    ChecklistItem(
        id="add_client",
        title="Add another client",
        description="Bring a new client organization into your agency workspace",
        # sentinel anchor consumed by ``_add_client_modal.html``.
        # The modal's ``document.body`` click listener intercepts navigation
        # and opens a stripped-down step-3-style form (client name + currency
        # only) in place; the row no longer leaves /dashboard. The full
        # /assets/onboarding/step-5 page is still the wizard's step-3 surface
        # for first-time agency setup — this anchor is the post-onboarding
        # "add another client" shortcut so adding the 2nd, 3rd, Nth client
        # doesn't require re-running the wizard.
        action_url="#add-client-modal",
        action_label="Add",
    ),
    ChecklistItem(
        id="enable_2fa",
        title="Enable two-factor authentication",
        description="Add a second layer of security to your sign-in",
        action_url=assets_path("/onboarding/checklist/coming-soon?item=enable_2fa"),
        action_label="Enable",
    ),
    ChecklistItem(
        id="connect_billing",
        title="Connect billing to keep going",
        description="Your trial is wrapping up — add billing to stay connected",
        action_url=assets_path("/onboarding/checklist/coming-soon?item=connect_billing"),
        action_label="Add billing",
    ),
)


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


class DashboardChecklistService:
    """Read + dismiss surface for the dashboard discovery checklist."""

    # ─────────────────────────────────────────────────────────────────
    # Index bootstrap (lazy — called on first dismiss write)
    # ─────────────────────────────────────────────────────────────────

    @staticmethod
    def ensure_index_exists() -> None:
        """Create the dismiss + skip indices if they do not exist.

        Idempotent. Safe to call on every dismiss; the underlying
        helpers short-circuit when the index/template is already
        present. added the skip index here so the same
        lazy-bootstrap path covers both.
        """
        es = get_elasticsearch_client()
        if es is None:
            logging.warning("DashboardChecklistService: could not connect to ES")
            return
        put_regular_index_template(es, INDEX_DISMISS, INDEX_DISMISS_MAPPING, priority=500)
        create_index_if_not_exists(es, INDEX_DISMISS, INDEX_DISMISS_MAPPING)
        put_regular_index_template(es, INDEX_SKIP, INDEX_SKIP_MAPPING, priority=500)
        create_index_if_not_exists(es, INDEX_SKIP, INDEX_SKIP_MAPPING)

    # ─────────────────────────────────────────────────────────────────
    # Read
    # ─────────────────────────────────────────────────────────────────

    @staticmethod
    def _is_wizard_signup_user(username: str) -> bool:
        """only wizard-signup users see the checklist.

        True iff the ES Security user record carries
        ``metadata.wizard_signup=true`` — the PERMANENT flag the wizard's
        ``OnboardingSignupService.signup_step1`` stamps at account
        creation. All other users — pre-wizard prod accounts,
        accounts provisioned via the admin user-management UI, ES
        built-ins — never went through the wizard and have nothing to
        "finish setting up", so the checklist is suppressed for them.

         Issue 2 split this cohort flag away from the transient
        ``onboarding_required`` flag the login gate clears once both
        gates pass. earlier this method read ``onboarding_required``,
        which meant a wizard-completed user landed on /dashboard with
        the checklist already hidden — the gate had just cleared the
        very flag the checklist used to identify the cohort. The new
        ``wizard_signup`` flag is never cleared, so a wizard-completed
        user continues to see "Finish setting up" until they complete or
        dismiss it.

        Going forward this means the cohort is structural rather than a
        per-user opt-out: a future admin-created account inherits "no
        checklist" by virtue of not carrying the wizard stamp, no bulk
        migration required. The earlier ``checklist_hidden`` opt-out
        flag (and its bulk-flip script) was removed when this logic
        landed.

        Returns False on any error path (no username, ES outage,
        missing user). The checklist is a discovery affordance, not a
        security gate, and fail-closed here is the right direction:
        hiding the surface on a transient blip is preferable to
        surprising every user with a setup card during an outage.
        """
        if not username:
            return False
        es = get_elasticsearch_client()
        if es is None:
            return False
        try:
            current = dict(es.security.get_user(username=username)).get(username)
        except Exception as e:  # noqa: BLE001 — defensive boundary
            logging.warning(
                "DashboardChecklistService._is_wizard_signup_user(%s): %s",
                username,
                e,
            )
            return False
        if not current:
            return False
        return bool((current.get("metadata") or {}).get("wizard_signup"))

    @staticmethod
    def _resolve_workspace_esid(username: str) -> str | None:
        """The workspace the predicates evaluate against.

        Falls back to ``None`` for users without a workspace membership
        (pre-F7 legacy accounts, ES outage). ``None`` propagates through
        the predicates as "no workspace context" — both items render
        not-done, the card stays visible, and the workspace_esid-keyed
        dismiss row is suppressed (we can't write a doc without a
        workspace component to the doc id).
        """
        if not username:
            return None
        try:
            return WorkspaceMembershipService.get_primary_workspace(username)
        except Exception as e:
            logging.warning(
                "DashboardChecklistService._resolve_workspace_esid(%s): %s",
                username,
                e,
            )
            return None

    @staticmethod
    def _has_any_connection(workspace_esid: str | None, username: str) -> bool:
        """Predicate for ``connect_first_source``.

        True iff at least one ``de_connection`` row exists with
        ``principal`` matching either the workspace ESID (the F7
        convention — the wizard stamps the workspace on the principal)
        OR the username (legacy pre-F7 connections where the user
        themselves was the principal). The OR is what lets pre-existing
        users avoid a "you haven't connected anything" prompt the moment
        the F7 cutover lands.
        """
        es = get_elasticsearch_client()
        if es is None:
            return False
        principals: list[str] = []
        if workspace_esid:
            principals.append(workspace_esid)
        if username:
            principals.append(username)
        if not principals:
            return False
        try:
            resp = es.count(
                index=INDEX_CONNECTION,
                body={"query": {"terms": {"principal": principals}}},
            )
        except Exception as e:
            logging.warning(
                "DashboardChecklistService._has_any_connection(%s, %s): %s",
                workspace_esid,
                username,
                e,
            )
            return False
        return int(resp.get("count", 0)) > 0

    @staticmethod
    def _has_multiple_connections(workspace_esid: str | None, username: str) -> bool:
        """Predicate for ``add_another_source``.

        True iff at least ``_MIN_CONNECTIONS_FOR_ADD_ANOTHER`` (i.e. 2)
        ``de_connection`` rows exist whose principal matches the
        workspace ESID or the username (the same OR clause as
        ``_has_any_connection`` so legacy pre-F7 principals carry).
        Sharing the principal filter is deliberate: a tenant-isolation
        mutation that drops the filter trips both predicates the same
        way, so the (a2) sub-assert keeps catching it.
        """
        es = get_elasticsearch_client()
        if es is None:
            return False
        principals: list[str] = []
        if workspace_esid:
            principals.append(workspace_esid)
        if username:
            principals.append(username)
        if not principals:
            return False
        try:
            resp = es.count(
                index=INDEX_CONNECTION,
                body={"query": {"terms": {"principal": principals}}},
            )
        except Exception as e:
            logging.warning(
                "DashboardChecklistService._has_multiple_connections(%s, %s): %s",
                workspace_esid,
                username,
                e,
            )
            return False
        return int(resp.get("count", 0)) >= _MIN_CONNECTIONS_FOR_ADD_ANOTHER

    @staticmethod
    def _has_invited_teammate(workspace_esid: str | None) -> bool:
        """Predicate for ``invite_teammate``.

        True iff ``de_authz_workspace_membership`` shows at least
        ``_MIN_MEMBERS_FOR_INVITE`` rows for the workspace. The wizard
        creates one membership row for the owner at provision time;
        a count of 2 means at least one invite was accepted. Scopes
        strictly by ``workspace_esid`` (no username OR clause) so a
        leak across tenants flips this predicate False, not True —
        the conservative direction.
        """
        if not workspace_esid:
            return False
        es = get_elasticsearch_client()
        if es is None:
            return False
        try:
            resp = es.count(
                index=INDEX_WORKSPACE_MEMBERSHIP,
                body={"query": {"term": {"workspace_esid": workspace_esid}}},
            )
        except Exception as e:
            logging.warning(
                "DashboardChecklistService._has_invited_teammate(%s): %s",
                workspace_esid,
                e,
            )
            return False
        if int(resp.get("count", 0)) >= _MIN_MEMBERS_FOR_INVITE:
            return True
        # SENDING the invite completes the row. The membership
        # count above only flips once the invitee accepts, which left the
        # row undone for the whole time the invite sat in their inbox (a teammate,
        # real new-user run 2026-09-18: two invites sent, row still open).
        # A pending or accepted invite doc for THIS workspace counts;
        # expired/cancelled ones do not. Fail-soft: a lookup error keeps
        # the membership verdict (False here) rather than raising.
        try:
            inv = es.count(
                index=INDEX_INVITES,
                body={
                    "query": {
                        "bool": {
                            "filter": [
                                {"term": {"workspace_esid": workspace_esid}},
                                {"terms": {"status": list(_INVITE_STATUSES_THAT_COUNT)}},
                            ]
                        }
                    }
                },
                ignore_unavailable=True,
            )
        except Exception as e:
            logging.warning(
                "DashboardChecklistService._has_invited_teammate(%s) invite lookup: %s",
                workspace_esid,
                e,
            )
            return False
        return int((inv or {}).get("count", 0)) >= 1

    @staticmethod
    def _has_customized_kpis(workspace_esid: str | None, username: str) -> bool:
        """Predicate for ``customize_kpis`` — stub.

        The KPI-preference storage doc the predicate eventually reads
        is a separate ticket; until that lands, the predicate is
        wired to always-False so the item renders as "not done". The
        action_url routes the user to the dashboard builder where the
        real customization UX lives.
        """
        del workspace_esid, username  # unused until the storage lands
        return False

    @staticmethod
    def _has_scheduled_weekly_summary(workspace_esid: str | None, username: str) -> bool:
        """Predicate for ``schedule_weekly_summary`` — stub.

        Always False until the weekly-summary scheduler ships. The
        action_url routes to the coming-soon stub page.
        """
        del workspace_esid, username
        return False

    @staticmethod
    def _has_set_conversion_goals(workspace_esid: str | None, username: str) -> bool:
        """Predicate for ``set_conversion_goals`` — stub.

        Always False until the conversion-goals storage ships. The
        action_url routes to the coming-soon stub page.
        """
        del workspace_esid, username
        return False

    # ─────────────────────────────────────────────────────────────────
    # conditional-item visibility predicates
    # Each returns True iff the item should appear on the checklist.
    # Returning False omits the row entirely (not "renders as done").
    # ─────────────────────────────────────────────────────────────────

    @staticmethod
    def _should_show_verify_email(workspace_esid: str | None, username: str) -> bool:
        """Visibility for ``verify_email``.

        True iff the user's ES security ``metadata.email_verified`` is
        NOT set. ``OnboardingEmailVerificationService.is_email_verified``
        is the canonical reader and already fails closed on
        any error path; we invert it for the visibility predicate.
        A user with no username (anonymous render path) gets False so
        the card never offers a verify CTA without a user to act on.
        """
        del workspace_esid
        if not username:
            return False
        return not OnboardingEmailVerificationService.is_email_verified(username)

    @staticmethod
    def _should_show_add_client(workspace_esid: str | None, username: str) -> bool:
        """Visibility for ``add_client``.

        True iff the user's primary workspace operates as an agency.
        Reads the canonical ``WorkspaceMembershipService.is_agency_for_user``
         — same source as the Organizations nav-menu gate, so
        the checklist and the nav stay in sync. The
        ``invite_teammate`` row is hidden when this predicate is True
        (see ``get_state``'s agency-mode filter) so the agency variant
        doesn't carry two competing "grow your account" rows; the AC
        literally says add_client "replaces" the invite_teammate
        headline for agencies.
        """
        del workspace_esid
        if not username:
            return False
        try:
            return WorkspaceMembershipService.is_agency_for_user(username)
        except Exception as e:  # noqa: BLE001 — defensive boundary
            logging.warning(
                "DashboardChecklistService._should_show_add_client(%s): %s",
                username,
                e,
            )
            return False

    @staticmethod
    def _should_show_enable_2fa(workspace_esid: str | None, username: str) -> bool:
        """Visibility for ``enable_2fa`` — stub.

        The AC's trigger is "after first login from a new device" — that
        requires a per-user device-fingerprint store that doesn't exist
        yet. Return False until that ticket lands so the row never
        renders prematurely. When wired, the predicate becomes "user
        signed in from a new device AND has not enabled 2FA yet".
        """
        del workspace_esid, username
        return False

    @staticmethod
    def _should_show_connect_billing(workspace_esid: str | None, username: str) -> bool:
        """Visibility for ``connect_billing`` — stub.

        The AC's trigger is "near end of trial" — that requires a
        per-workspace trial-end timestamp + a paid-plan flag, neither
        of which exists in storage today. Return False until those
        fields ship. When wired, the predicate becomes "trial_ends_at
        within N days AND no active paid subscription".
        """
        del workspace_esid, username
        return False

    # ─────────────────────────────────────────────────────────────────
    # variant gate
    # ─────────────────────────────────────────────────────────────────

    @staticmethod
    def _is_individual(username: str) -> bool:
        """Is the user on the individual (solo) variant?

        Reads the canonical ``UserProfileService.get_account_type`` —
        the same source the onboarding-branching service uses, so the
        checklist variant and the wizard branching stay in lock-step.
        Returns False on no-username + on any exception (defensive
        boundary; mirrors ``_should_show_add_client``'s posture).
        Falling back to "not individual" is the safe direction because
        it surfaces ``invite_teammate`` rather than hiding it, matching
        the principle that discovery surfaces should err toward being
        seen.
        """
        if not username:
            return False
        try:
            return UserProfileService.get_account_type(username) == ACCOUNT_TYPE_INDIVIDUAL
        except Exception as e:  # noqa: BLE001 — defensive boundary
            logging.warning(
                "DashboardChecklistService._is_individual(%s): %s",
                username,
                e,
            )
            return False

    @staticmethod
    def _has_any_dashboard(workspace_esid: str | None, username: str) -> bool:
        """Predicate for ``first_dashboard_created``.

        True iff at least one ``de_asset_echarts_dashboards`` row has the
        workspace's primary team in its ``allowed_team_esids`` list.
         first-look clone is the canonical first
        writer here; user-created dashboards land
        the same way.

        Falls back to a username-keyed lookup for legacy / pre-F7
        accounts whose dashboards may have been stamped against a
        ``created_by`` field rather than a team scope.
        """
        es = get_elasticsearch_client()
        if es is None:
            return False

        team_esids = DashboardChecklistService._teams_for_workspace(workspace_esid)
        if team_esids:
            try:
                resp = es.count(
                    index=INDEX_DASHBOARDS,
                    body={"query": {"terms": {"allowed_team_esids": team_esids}}},
                )
            except Exception as e:
                logging.warning(
                    "DashboardChecklistService._has_any_dashboard(%s teams): %s",
                    workspace_esid,
                    e,
                )
                return False
            if int(resp.get("count", 0)) > 0:
                return True

        if username:
            try:
                resp = es.count(
                    index=INDEX_DASHBOARDS,
                    body={"query": {"term": {"created_by": username}}},
                )
            except Exception as e:
                logging.warning(
                    "DashboardChecklistService._has_any_dashboard(%s username): %s",
                    username,
                    e,
                )
                return False
            return int(resp.get("count", 0)) > 0

        return False

    @staticmethod
    def _teams_for_workspace(workspace_esid: str | None) -> list[str]:
        """Resolve a workspace's team ESIDs.

        Teams live under ``de_asset_teams`` with a ``workspace_esid``
        field (F7's workspace-first contract). Returns ``[]`` for an
        unknown workspace, ES outage, or empty workspace — every
        consumer treats empty as "no team scope to check against".
        """
        if not workspace_esid:
            return []
        es = get_elasticsearch_client()
        if es is None:
            return []
        try:
            resp = es.search(
                index="de_asset_teams",
                body={
                    # ``de_asset_teams.workspace_esid`` was reindexed
                    # to keyword, so the bare term lookup now matches hyphenated
                    # slugs (the ``.keyword`` workaround is reverted).
                    "query": {"term": {"workspace_esid": workspace_esid}},
                    "size": 100,
                    "_source": False,
                },
            )
        except Exception as e:
            logging.warning(
                "DashboardChecklistService._teams_for_workspace(%s): %s",
                workspace_esid,
                e,
            )
            return []
        return [hit["_id"] for hit in resp.get("hits", {}).get("hits", [])]

    @staticmethod
    def _doc_id(username: str, workspace_esid: str) -> str:
        return f"{username}:{workspace_esid}"

    @staticmethod
    def _skip_doc_id(username: str, workspace_esid: str, item_id: str) -> str:
        """Deterministic doc id for a (user, workspace, item) skip row.

        keyed identically to the dismiss row's
        ``{user}:{workspace}`` prefix with an item suffix so the
        rows sort sensibly when inspecting in Kibana.
        """
        return f"{username}:{workspace_esid}:{item_id}"

    @staticmethod
    def _all_known_item_ids() -> set[str]:
        """The set of item ids the service recognises.

        Used by ``skip`` to reject a forged POST with an unknown
        item_id before it lands a stray row in ES. Includes both
        standard and conditional items — a future ticket that adds
        a new tuple needs to extend this set so the validation stays
        in sync.
        """
        return {item.id for item in _STANDARD_ITEMS} | {item.id for item in _CONDITIONAL_ITEMS}

    @staticmethod
    def _get_skipped_ids(username: str, workspace_esid: str | None) -> set[str]:
        """Return the set of item ids the user has skipped in this workspace.

        One ES round-trip per ``get_state`` call (a single ``es.search``
        with a term filter on username+workspace_esid). Returns an empty
        set on no-workspace, ES outage, ``index_not_found``, or any
        other failure — same fail-open posture as ``is_dismissed``,
        so a transient ES blip never erases a user's skip state in a
        way that surprises them.
        """
        if not username or not workspace_esid:
            return set()
        es = get_elasticsearch_client()
        if es is None:
            return set()
        try:
            resp = es.search(
                index=INDEX_SKIP,
                body={
                    "query": {
                        "bool": {
                            "filter": [
                                {"term": {"username": username}},
                                {"term": {"workspace_esid": workspace_esid}},
                            ]
                        }
                    },
                    "size": 100,
                    "_source": ["item_id"],
                },
            )
        except Exception as e:
            msg = str(e).lower()
            if "not_found" in msg or "index_not_found" in msg or "404" in msg:
                return set()
            logging.warning(
                "DashboardChecklistService._get_skipped_ids(%s, %s): %s",
                username,
                workspace_esid,
                e,
            )
            return set()
        hits = resp.get("hits", {}).get("hits", [])
        return {(hit.get("_source") or {}).get("item_id", "") for hit in hits if (hit.get("_source") or {}).get("item_id")}

    @staticmethod
    def _get_dismiss_doc(username: str, workspace_esid: str | None) -> dict | None:
        """Single ``es.get`` for the (user, workspace) dismiss row.

        Returns the ``_source`` dict on hit, ``None`` on miss /
        no-workspace / ES outage / index_not_found. introduces
        this helper because both ``is_dismissed`` and the new collapse
        derivation need the same row; calling ``es.get`` twice per
        ``get_state`` would be wasteful. The "no row = not dismissed
        AND no explicit collapse preference" default keeps existing
        ``is_dismissed`` callers behaviour-compatible.
        """
        if not username or not workspace_esid:
            return None
        es = get_elasticsearch_client()
        if es is None:
            return None
        doc_id = DashboardChecklistService._doc_id(username, workspace_esid)
        try:
            resp = es.get(index=INDEX_DISMISS, id=doc_id)
        except Exception as e:
            msg = str(e).lower()
            if "not_found" in msg or "index_not_found" in msg or "404" in msg:
                return None
            logging.warning(
                "DashboardChecklistService._get_dismiss_doc(%s): %s",
                doc_id,
                e,
            )
            return None
        # es.get returns an ObjectApiResponse (duck-type on .get, not isinstance
        # dict) — the old isinstance(dict) guard returned {} for every real
        # dismiss doc, silently losing dashboard collapse/dismiss state
        # (same class as).
        source = resp.get("_source") if hasattr(resp, "get") else None
        return source if isinstance(source, dict) else {}

    @staticmethod
    def is_dismissed(username: str, workspace_esid: str | None) -> bool:
        """Point-lookup of the dismiss row via deterministic doc id.

        Returns False for missing-workspace, ES outage, or
        ``index_not_found`` (i.e. before the first dismiss has ever
        been written). The "no row = not dismissed" default is the safe
        one: a transient ES outage shows the card rather than hiding
        it, matching the principle that discovery surfaces should
        err toward being seen.

        Note: moved the doc fetch into
        ``_get_dismiss_doc``. The dismiss-bit is True iff the row
        exists AND carries a ``dismissed_at`` value — a doc created
        solely by an explicit collapse/expand action (writes
        the row to persist ``collapse_state`` before the user has
        crossed the dismiss threshold) does NOT count as dismissed.
        """
        doc = DashboardChecklistService._get_dismiss_doc(username, workspace_esid)
        if doc is None:
            return False
        return bool(doc.get("dismissed_at"))

    @staticmethod
    def _get_collapse_state(
        username: str,
        workspace_esid: str | None,
        dismiss_doc: dict | None = None,
    ) -> str | None:
        """Read the persisted ``collapse_state`` field, if any.

        ``dismiss_doc`` is an optional cache so callers that already
        fetched the row (``get_state``) don't pay a second ``es.get``.
        Returns one of ``"collapsed"`` / ``"expanded"`` (the valid
        persisted values) or ``None`` (no doc / field absent / invalid).
        Treating unknown values as ``None`` is deliberate: a future
        ticket that adds a third state shouldn't break older code
        paths that haven't been updated.
        """
        doc = dismiss_doc if dismiss_doc is not None else DashboardChecklistService._get_dismiss_doc(username, workspace_esid)
        if not isinstance(doc, dict):
            return None
        value = doc.get("collapse_state")
        if value in _VALID_COLLAPSE_STATES:
            return value
        return None

    @staticmethod
    def get_state(username: str) -> ChecklistState:
        """The single readable view consumed by ``dashboard_page``.

        Always returns a fully-formed ``ChecklistState`` — even on ES
        outage or unknown user the card renders (with both items
        not-done) rather than vanishing. The reasoning: AC is
        "card renders on dashboard for all entry points", and a silent
        empty state would regress that.

        conditional items (verify_email, add_client,
        enable_2fa, connect_billing) are interleaved at runtime based
        on their visibility predicates. A conditional item that's
        visible is always not-done (the AC: "No item ever shown to a
        user who can't act on it" — once they act, the predicate flips
        False and the row disappears entirely). For agency users,
        add_client visible AND invite_teammate hidden — they cover the
        same "grow your account" UX slot.

        individual users (``account_type == "individual"``)
        also hide ``invite_teammate`` (no team to invite to) AND the
        standard block is reordered so ``add_another_source`` +
        ``customize_kpis`` sit at the top — the AC's headline pair for
        the solo variant.

        only users who went through the new
        onboarding wizard (``metadata.wizard_signup=true``, stamped by
        ``OnboardingSignupService.signup_step1``) see the checklist.
        Pre-wizard prod users, admin-created accounts, and any other
        cohort that doesn't carry the wizard stamp short-circuit here
        to a fully-empty hidden state. The state reuses the
        ``is_complete=True`` semantic — the template guard
        ``{% if checklist and not checklist.is_complete %}`` already
        gates the entire surface (card + collapsed-header + re-open
        chip) on that flag, so the hidden cohort renders identically
        to a 100%-done cohort: no checklist UI of any kind. Setting
        ``is_dismissed=True`` alongside (despite is_complete already
        hiding the chip) is belt-and-braces — if a future template
        change separates the chip's gate from is_complete, the flag
        still suppresses the re-open affordance for this cohort.

        This is structural rather than a per-user opt-out: the cohort
        is correctly identified by the absence of the wizard stamp,
        so no migration is required for existing prod users and any
        future admin-created account is hidden by default.
        """
        if not DashboardChecklistService._is_wizard_signup_user(username):
            return ChecklistState(
                items=(),
                completed_count=0,
                total_count=0,
                progress_pct=0,
                is_dismissed=True,
                is_dismissible=False,
                is_collapsed=False,
                is_complete=True,
                workspace_esid=None,
                item_dicts=[],
            )

        workspace_esid = DashboardChecklistService._resolve_workspace_esid(username)
        # follow-up — the dismiss-state and skip-state rows are
        # keyed by ``{username}:{workspace_esid}``. For users without a
        # primary workspace, the write methods use _NO_WORKSPACE_SENTINEL
        # so the doc-id is still constructable; the read path here must
        # use the same sentinel so collapse/dismiss/skip persistence
        # survives a page refresh. The PREDICATES (connection / dashboard
        # / etc.) still take the original ``workspace_esid`` since they
        # query other indices keyed by the real ESID, not the sentinel.
        effective_workspace_esid = workspace_esid or _NO_WORKSPACE_SENTINEL

        has_connection = DashboardChecklistService._has_any_connection(workspace_esid, username)
        has_dashboard = DashboardChecklistService._has_any_dashboard(workspace_esid, username)
        has_multi_connection = DashboardChecklistService._has_multiple_connections(workspace_esid, username)
        has_teammate = DashboardChecklistService._has_invited_teammate(workspace_esid)
        has_kpis = DashboardChecklistService._has_customized_kpis(workspace_esid, username)

        _done_by_id = {
            "connect_first_source": has_connection,
            "first_dashboard_created": has_dashboard,
            "add_another_source": has_multi_connection,
            "invite_teammate": has_teammate,
            "customize_kpis": has_kpis,
        }

        # Conditional rows disabled (2026-05-22). Locking visibility to
        # False for every conditional id pins the rendered checklist
        # length at exactly len(_STANDARD_ITEMS) for every cohort.
        # The conditional-tier surface is kept in code so a future
        # ticket can re-enable specific rows by flipping the predicate
        # here; the per-tile copy + action_urls stay where they are.
        _visible_by_id = {
            "verify_email": False,
            "add_client": False,
            "enable_2fa": False,
            "connect_billing": False,
        }

        # agency variant: ``add_client`` is the headline
        # conditional row (renders above the standard block by virtue of
        # the conditional-tier render order). ``invite_teammate`` stays
        # visible but is de-emphasized by position alone — it falls into
        # its tuple-declared slot 3 below the promoted ``add_client``
        # row rather than being hidden. Agencies grow by adding clients,
        # not teammates, but the team-invite affordance is still useful;
        # the headline-vs-standard separation is enough visual hierarchy.
        # previously hid ``invite_teammate`` entirely when
        # ``add_client`` was visible — reverses that since the
        # ticket explicitly says "Invite a teammate still available but
        # de-emphasized" (do-order #44).
        hidden_standard_ids: set[str] = set()

        # Individual-variant invite_teammate hide disabled
        # (2026-05-22) to keep the rendered row count fixed at
        # ``len(_STANDARD_ITEMS)`` for every cohort. The headline
        # reorder for individuals still runs below — invite_teammate
        # is just kept on the list.
        is_individual = DashboardChecklistService._is_individual(username)

        # skipped item ids for this (user, workspace). A
        # skipped standard row is rendered done=True, skipped=True so
        # the visual checks off but analytics can tell skips from real
        # completions. A skipped conditional row drops out entirely —
        # same outcome as the user acting on it, keeping the AC's "no
        # item shown to a user who can't act on it" invariant for the
        # conditional tier.
        skipped_ids = DashboardChecklistService._get_skipped_ids(username, effective_workspace_esid)

        rendered: list[ChecklistItem] = []
        completed = 0

        # Conditional rows that are visible render above the standard
        # block. Urgency-ordered: verify_email and connect_billing are
        # the time-sensitive ones; add_client and enable_2fa are
        # promotional. The tuple order in _CONDITIONAL_ITEMS is the
        # render order, same convention as _STANDARD_ITEMS.
        for item in _CONDITIONAL_ITEMS:
            if not _visible_by_id[item.id]:
                continue
            # a skipped conditional row renders done=True,
            # skipped=True (same visual contract as a skipped standard
            # row). The AC reads "Skipping moves the item to the done
            # state visually" — that's a visibility-preserving
            # semantic, so we keep the row in place rather than
            # dropping it out. Counts toward completed_count for
            # consistency with skipped standard rows.
            is_skipped = item.id in skipped_ids
            done = is_skipped
            if done:
                completed += 1
            rendered.append(
                ChecklistItem(
                    id=item.id,
                    title=item.title,
                    description=item.description,
                    action_url=item.action_url,
                    action_label=item.action_label,
                    done=done,
                    skipped=is_skipped,
                )
            )

        # individual variant promotes
        # ``_INDIVIDUAL_HEADLINE_IDS`` to slots 0+1 of the standard
        # block; the remaining items keep their tuple-declared order
        # underneath. The non-individual path renders the tuple in its
        # declared order unchanged so the invariants are
        # untouched for org_owner / agency / invited users.
        _standard_by_id = {item.id: item for item in _STANDARD_ITEMS}
        if is_individual:
            ordered_ids = list(_INDIVIDUAL_HEADLINE_IDS)
            ordered_ids.extend(item.id for item in _STANDARD_ITEMS if item.id not in _INDIVIDUAL_HEADLINE_IDS)
        else:
            ordered_ids = [item.id for item in _STANDARD_ITEMS]

        for item_id in ordered_ids:
            if item_id in hidden_standard_ids:
                continue
            item = _standard_by_id[item_id]
            real_done = _done_by_id[item.id]
            is_skipped = item.id in skipped_ids and not real_done
            done = real_done or is_skipped
            if done:
                completed += 1
            rendered.append(
                ChecklistItem(
                    id=item.id,
                    title=item.title,
                    description=item.description,
                    action_url=item.action_url,
                    action_label=item.action_label,
                    done=done,
                    skipped=is_skipped,
                )
            )

        total = len(rendered)
        progress_pct = int(round((completed / total) * 100)) if total else 0
        # follow-up (2026-05-20): suppress the whole-card
        # dismiss X when only one item is left. The progress threshold
        # alone (>= 80%) would let a user with 7-of-8 dismiss the card
        # with one task still on their plate. With one row remaining
        # we want the user to finish it (or per-item skip it), not
        # hide the surface that's nudging them toward it.
        remaining = max(0, total - completed)
        # (user decision, a teammate 2026-09-18): the card is dismissible
        # at ANY progress. The 80% gate and the "not with one row
        # left" guard left a new user with no visible way
        # to clear the card — the per-item skip was hover-only and the X was
        # hidden. Dismiss stays reversible through the "Re-open setup
        # checklist" chip, and skipped-vs-completed analytics are untouched.
        # ``DISMISS_THRESHOLD_PCT`` still drives the auto-COLLAPSE rule only.
        is_dismissible = total > 0

        # one ``es.get`` for the (user, workspace) row covers
        # both ``is_dismissed`` AND the persisted collapse preference.
        # The dismiss-flag is gated on a non-empty ``dismissed_at`` so
        # a row written solely to persist a pre-threshold collapse
        # choice doesn't accidentally hide the whole card.
        dismiss_doc = DashboardChecklistService._get_dismiss_doc(username, effective_workspace_esid)
        is_dismissed = bool(dismiss_doc and dismiss_doc.get("dismissed_at"))
        explicit_collapse = DashboardChecklistService._get_collapse_state(
            username, effective_workspace_esid, dismiss_doc=dismiss_doc
        )
        if explicit_collapse == COLLAPSE_STATE_COLLAPSED:
            is_collapsed = True
        elif explicit_collapse == COLLAPSE_STATE_EXPANDED:
            is_collapsed = False
        else:
            # Auto rule: collapse once the user has done enough of the
            # setup that the full card is more noise than value. The
            # AC's "4 of 5 standard items done" reads as a literal 80%
            # ratio, which matches DISMISS_THRESHOLD_PCT; sharing the
            # constant keeps the two affordances aligned.
            is_collapsed = progress_pct >= COLLAPSE_THRESHOLD_PCT

        # Plain-dict mirror of items — Jinja's safe-mode template
        # consumes a list of dicts more cleanly than a list of frozen
        # dataclasses (some renderers throw on attribute access against
        # ``__slots__``-style objects). Keep both: tests prefer the
        # typed list, the template prefers item_dicts.
        item_dicts = [
            {
                "id": item.id,
                "title": item.title,
                "description": item.description,
                "action_url": item.action_url,
                "action_label": item.action_label,
                "done": item.done,
                "skipped": item.skipped,
            }
            for item in rendered
        ]

        # follow-up — surface "100% done" as a distinct state so
        # the template can auto-hide the card AND the re-open chip
        # without requiring a manual dismiss click. ``total > 0`` guard
        # protects against the empty-checklist edge case (no items
        # configured) where progress_pct math is 0/0 → 0 and we don't
        # want to flip is_complete=True on an empty card.
        is_complete = total > 0 and completed >= total

        return ChecklistState(
            items=tuple(rendered),
            completed_count=completed,
            total_count=total,
            progress_pct=progress_pct,
            is_dismissed=is_dismissed,
            is_dismissible=is_dismissible,
            is_collapsed=is_collapsed,
            is_complete=is_complete,
            workspace_esid=workspace_esid,
            item_dicts=item_dicts,
        )

    # ─────────────────────────────────────────────────────────────────
    # Write
    # ─────────────────────────────────────────────────────────────────

    @staticmethod
    def dismiss(username: str) -> dict:
        """Persist a dismissal row for (user, primary-workspace).

        Returns ``{"success": bool, "error": <str> | None}``. Refuses
        with ``error="below_threshold"`` only when the state is not
        dismissible at all — that means "the card has no
        rows" (an empty checklist has nothing to dismiss); the former 80%
        gate is gone. Re-evaluated server-side so the contract does not
        depend on the template.
        """
        if not username:
            return {"success": False, "error": "username_required"}
        state = DashboardChecklistService.get_state(username)
        if not state.is_dismissible:
            return {"success": False, "error": "below_threshold"}
        # follow-up — use a sentinel doc-id key when the user
        # has no primary workspace so the write still succeeds.
        workspace_esid = state.workspace_esid or _NO_WORKSPACE_SENTINEL

        DashboardChecklistService.ensure_index_exists()
        es = get_elasticsearch_client()
        if es is None:
            return {"success": False, "error": "es_unavailable"}
        doc_id = DashboardChecklistService._doc_id(username, workspace_esid)
        # upsert (don't overwrite) so an existing
        # ``collapse_state`` field survives a dismiss. The row's
        # tenant-keyed identity ({user}:{workspace}) is unchanged; we
        # are only setting ``dismissed_at``.
        try:
            es.update(
                index=INDEX_DISMISS,
                id=doc_id,
                body={
                    "doc": {
                        "username": username,
                        "workspace_esid": workspace_esid,
                        "dismissed_at": _now_iso(),
                    },
                    "doc_as_upsert": True,
                },
                refresh="wait_for",
            )
        except Exception as e:
            logging.error(
                "DashboardChecklistService.dismiss(%s): %s",
                doc_id,
                e,
            )
            return {"success": False, "error": "dismiss_failed"}
        return {"success": True, "error": None}

    @staticmethod
    def undismiss(username: str) -> dict:
        """Restore the checklist card after a prior dismissal.

        Idempotent: undismissing a card that wasn't dismissed returns
        success.

        undismiss preserves any persisted ``collapse_state``
        so a user who dismissed, then collapsed (or vice versa), then
        undismissed, doesn't lose their layout choice. Implemented as
        a partial update that nulls ``dismissed_at``; the row stays
        if a non-default ``collapse_state`` exists. When neither
        field is set anymore the doc carries no behavioural state, so
        we delete it (keeps the index small + makes Kibana spelunking
        cleaner).
        """
        if not username:
            return {"success": False, "error": "username_required"}
        # follow-up — sentinel doc-id key when no workspace.
        workspace_esid = DashboardChecklistService._resolve_workspace_esid(username) or _NO_WORKSPACE_SENTINEL
        es = get_elasticsearch_client()
        if es is None:
            return {"success": False, "error": "es_unavailable"}
        doc_id = DashboardChecklistService._doc_id(username, workspace_esid)

        # Cheap read first — if the row carries an explicit
        # collapse_state we keep the row (nulling only dismissed_at).
        # If it doesn't, we delete the row entirely so the index
        # stays small.
        existing = DashboardChecklistService._get_dismiss_doc(username, workspace_esid)
        has_collapse_state = isinstance(existing, dict) and existing.get("collapse_state") in _VALID_COLLAPSE_STATES

        if has_collapse_state:
            try:
                es.update(
                    index=INDEX_DISMISS,
                    id=doc_id,
                    body={"doc": {"dismissed_at": None}},
                    refresh="wait_for",
                )
            except Exception as e:
                logging.warning(
                    "DashboardChecklistService.undismiss(%s, preserve collapse): %s",
                    doc_id,
                    e,
                )
                return {"success": False, "error": "undismiss_failed"}
            return {"success": True, "error": None}

        try:
            es.delete(index=INDEX_DISMISS, id=doc_id, refresh="wait_for")
        except Exception as e:
            msg = str(e).lower()
            if "not_found" in msg or "index_not_found" in msg or "404" in msg:
                return {"success": True, "error": None}
            logging.warning(
                "DashboardChecklistService.undismiss(%s): %s",
                doc_id,
                e,
            )
            return {"success": False, "error": "undismiss_failed"}
        return {"success": True, "error": None}

    # ─────────────────────────────────────────────────────────────────
    # Collapse / Expand
    # ─────────────────────────────────────────────────────────────────

    @staticmethod
    def _set_collapse_state(username: str, state: str) -> dict:
        """Upsert the persisted ``collapse_state`` for (user, workspace).

        Common code path for both ``collapse`` (state="collapsed") and
        ``expand`` (state="expanded"). Uses ``es.update`` with
        ``doc_as_upsert`` so any existing ``dismissed_at`` on the row
        survives the write — the two surfaces (dismiss / collapse)
        share a doc and must not stomp on each other's columns.
        """
        if not username:
            return {"success": False, "error": "username_required"}
        if state not in _VALID_COLLAPSE_STATES:
            return {"success": False, "error": "invalid_state"}
        # follow-up — sentinel doc-id key when no workspace.
        workspace_esid = DashboardChecklistService._resolve_workspace_esid(username) or _NO_WORKSPACE_SENTINEL

        DashboardChecklistService.ensure_index_exists()
        es = get_elasticsearch_client()
        if es is None:
            return {"success": False, "error": "es_unavailable"}
        doc_id = DashboardChecklistService._doc_id(username, workspace_esid)
        try:
            es.update(
                index=INDEX_DISMISS,
                id=doc_id,
                body={
                    "doc": {
                        "username": username,
                        "workspace_esid": workspace_esid,
                        "collapse_state": state,
                        "collapse_state_updated_at": _now_iso(),
                    },
                    "doc_as_upsert": True,
                },
                refresh="wait_for",
            )
        except Exception as e:
            logging.error(
                "DashboardChecklistService._set_collapse_state(%s, %s): %s",
                doc_id,
                state,
                e,
            )
            return {"success": False, "error": "collapse_failed" if state == COLLAPSE_STATE_COLLAPSED else "expand_failed"}
        return {"success": True, "error": None}

    @staticmethod
    def collapse(username: str) -> dict:
        """Persist an explicit "I want this collapsed" choice.

        Writes ``collapse_state="collapsed"`` to the shared dismiss
        doc. The choice survives page refreshes because the next
        ``get_state`` reads the field and overrides the auto-rule.

        Unlike ``dismiss`` there is no threshold guard — a user can
        collapse the card at any progress level (the AC's "Expand
        state survives page refresh" is symmetric; the user choice
        wins over the auto-rule regardless of progress).
        """
        return DashboardChecklistService._set_collapse_state(username, COLLAPSE_STATE_COLLAPSED)

    @staticmethod
    def expand(username: str) -> dict:
        """Persist an explicit "I want this expanded" choice.

        Writes ``collapse_state="expanded"`` to the shared dismiss
        doc. The user-choice override is what makes the AC's "Expand
        state survives page refresh" bullet land — once the user
        clicks expand on the post-threshold auto-collapsed header,
        the choice is durable until they change it.
        """
        return DashboardChecklistService._set_collapse_state(username, COLLAPSE_STATE_EXPANDED)

    # ─────────────────────────────────────────────────────────────────
    # per-item Skip / Unskip
    # ─────────────────────────────────────────────────────────────────

    @staticmethod
    def skip(username: str, item_id: str) -> dict:
        """Persist a per-item skip row.

        Validates ``item_id`` against the union of known standard and
        conditional ids — a forged POST with an unknown id is rejected
        with ``error="unknown_item"`` so the skip index never collects
        stray rows. Otherwise writes the (user, workspace, item) row
        with refresh=wait_for so the next ``get_state`` call sees it
        immediately (the dashboard re-renders right after skip).
        """
        if not username:
            return {"success": False, "error": "username_required"}
        if not item_id:
            return {"success": False, "error": "item_id_required"}
        if item_id not in DashboardChecklistService._all_known_item_ids():
            return {"success": False, "error": "unknown_item"}

        # follow-up — sentinel doc-id key when no workspace.
        workspace_esid = DashboardChecklistService._resolve_workspace_esid(username) or _NO_WORKSPACE_SENTINEL

        DashboardChecklistService.ensure_index_exists()
        es = get_elasticsearch_client()
        if es is None:
            return {"success": False, "error": "es_unavailable"}
        doc_id = DashboardChecklistService._skip_doc_id(username, workspace_esid, item_id)
        try:
            es.index(
                index=INDEX_SKIP,
                id=doc_id,
                body={
                    "username": username,
                    "workspace_esid": workspace_esid,
                    "item_id": item_id,
                    "skipped_at": _now_iso(),
                },
                refresh="wait_for",
            )
        except Exception as e:
            logging.error(
                "DashboardChecklistService.skip(%s): %s",
                doc_id,
                e,
            )
            return {"success": False, "error": "skip_failed"}
        return {"success": True, "error": None}

    @staticmethod
    def unskip(username: str, item_id: str) -> dict:
        """Delete a per-item skip row, restoring the row's real done state.

        Idempotent — re-opening an item the user hasn't skipped is a
        no-op (200 with no ES touch beyond a single 404'd delete). The
        ``item_id`` validation matches ``skip``: unknown ids are
        rejected rather than silently no-op'd, so a typo on the wire
        surfaces in the JSON instead of being masked.
        """
        if not username:
            return {"success": False, "error": "username_required"}
        if not item_id:
            return {"success": False, "error": "item_id_required"}
        if item_id not in DashboardChecklistService._all_known_item_ids():
            return {"success": False, "error": "unknown_item"}

        # follow-up — sentinel doc-id key when no workspace.
        workspace_esid = DashboardChecklistService._resolve_workspace_esid(username) or _NO_WORKSPACE_SENTINEL
        es = get_elasticsearch_client()
        if es is None:
            return {"success": False, "error": "es_unavailable"}
        doc_id = DashboardChecklistService._skip_doc_id(username, workspace_esid, item_id)
        try:
            es.delete(index=INDEX_SKIP, id=doc_id, refresh="wait_for")
        except Exception as e:
            msg = str(e).lower()
            if "not_found" in msg or "index_not_found" in msg or "404" in msg:
                return {"success": True, "error": None}
            logging.warning(
                "DashboardChecklistService.unskip(%s): %s",
                doc_id,
                e,
            )
            return {"success": False, "error": "unskip_failed"}
        return {"success": True, "error": None}
