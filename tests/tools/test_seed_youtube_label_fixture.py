"""Unit tests for the label fixture seeder.

Covers ``build_fixture`` (pure) plus the safety guards. No ES.

The point of the fixture is to reproduce the prod bug's SHAPE on staging, where
the real FLKKP.J data does not exist — so the tests assert that shape precisely:
same title and email across two docs (the precondition for the bug) with
different owned-channel stamps (the thing that fixes it), and the owned-vs-linked
divergence that makes the "authorized as" wording load-bearing.
"""

from __future__ import annotations

import os
import sys

_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
_PY_ROOT = os.path.join(_REPO_ROOT, "bin", "python")
if _PY_ROOT not in sys.path:
    sys.path.insert(0, _PY_ROOT)

from tools.seed_youtube_label_fixture import (  # noqa: E402
    EMAIL,
    FIXTURE_FLAG,
    TITLE,
    build_fixture,
    merge_team_channels,
    prune_team_links,
)


def test_should_give_both_accounts_the_same_title_and_email():
    """That collision IS the bug's precondition — without it the fixture proves
    nothing, because the rows would differ on title alone."""
    accounts, _ = build_fixture(None)
    assert len(accounts) == 2
    assert accounts[0]["youtube_account_title"] == accounts[1]["youtube_account_title"] == TITLE
    assert accounts[0]["youtube_account_email"] == accounts[1]["youtube_account_email"] == EMAIL


def test_should_stamp_different_owned_channels_on_each_account():
    """The only field that distinguishes them, hence the only thing the fix can
    key on."""
    accounts, _ = build_fixture(None)
    a, b = (acc["youtube_account_owned_channel_titles"] for acc in accounts)
    assert a != b
    assert a and b


def test_should_never_write_a_token():
    """A tokenless doc cannot import and cannot be mistaken for a live OAuth
    grant — the fixture must not create one."""
    accounts, _ = build_fixture(None)
    assert all("youtube_account_token" not in acc for acc in accounts)


def test_should_mark_every_doc_with_the_fixture_flag():
    """--remove keys on this flag, never on title, so it can't reap real data."""
    accounts, channels = build_fixture(None)
    assert all(acc.get(FIXTURE_FLAG) is True for acc in accounts)
    assert all(ch.get(FIXTURE_FLAG) is True for ch in channels)


def test_should_prefix_titles_so_fixture_docs_are_obvious_in_the_ui():
    accounts, channels = build_fixture(None)
    assert TITLE.startswith("[TEST]")
    assert all(ch["youtube_channel_title"].startswith("[TEST]") for ch in channels)


def test_should_use_an_unroutable_email_domain():
    """.invalid is reserved and can never be a real Google login, so the fixture
    can't collide with or shadow a genuine account."""
    assert EMAIL.endswith(".invalid")


def test_should_reproduce_the_owned_versus_linked_divergence():
    """Account B owns Bravo but is linked to Charlie and Delta — the prod shape
    (jVj4LpwB owns only FLKDF while 12 channels point at it). This is what makes
    the heading read 'authorized as: Bravo' above a list of other channels, and
    is precisely the case where a bare channel-name suffix would look like a
    mislabel."""
    accounts, channels = build_fixture(None)
    b_owned = set(accounts[1]["youtube_account_owned_channel_titles"])
    b_linked = {ch["youtube_channel_title"] for ch in channels if ch["_fixture_account"] == "B"}
    assert b_linked, "account B must have linked channels"
    assert not (b_linked & b_owned), "B's linked channels must NOT be in its owned set"


def test_should_link_the_healthy_account_to_the_channel_it_owns():
    """Account A is the control: owned == linked, so its heading should read
    consistently and prove the label isn't just always mismatched."""
    accounts, channels = build_fixture(None)
    a_owned = set(accounts[0]["youtube_account_owned_channel_titles"])
    a_linked = {ch["youtube_channel_title"] for ch in channels if ch["_fixture_account"] == "A"}
    assert a_linked == a_owned


def test_should_omit_workspace_esid_when_not_requested():
    accounts, channels = build_fixture(None)
    assert all("workspace_esid" not in d for d in accounts + channels)


def test_should_stamp_workspace_esid_on_every_doc_when_requested():
    """A doc with no workspace_esid is invisible to workspace-scoped users
    , which would make the fixture unviewable for the very tester
    who needs to see it."""
    accounts, channels = build_fixture("ws-staging")
    assert all(d.get("workspace_esid") == "ws-staging" for d in accounts + channels)


# ── team attachment (the connector-dialog surface) ───────────────────────────
# The dialog's group headings read team_youtube_channel_handle_esids, so viewing
# that surface needs the fixture channels linked to a real team. Clobbering that
# field would silently unlink a live team's channels, so the merge is additive.


def test_should_preserve_a_teams_existing_channel_links():
    """The load-bearing guarantee: attaching the fixture must never drop links a
    real team already has."""
    out = merge_team_channels(["real1", "real2"], ["fx1", "fx2"])
    assert out == ["real1", "real2", "fx1", "fx2"]


def test_should_not_duplicate_a_link_that_is_already_present():
    out = merge_team_channels(["fx1"], ["fx1", "fx2"])
    assert out == ["fx1", "fx2"]


def test_should_handle_a_team_with_no_existing_links():
    assert merge_team_channels(None, ["fx1"]) == ["fx1"]
    assert merge_team_channels([], ["fx1"]) == ["fx1"]


def test_should_handle_the_legacy_scalar_string_shape():
    """The read path tolerates a bare string, so the writer must too — treating
    it as a character list would corrupt the field."""
    assert merge_team_channels("real1", ["fx1"]) == ["real1", "fx1"]
    assert merge_team_channels("", ["fx1"]) == ["fx1"]


def test_should_drop_empty_entries_from_the_existing_list():
    assert merge_team_channels(["real1", "", None], ["fx1"]) == ["real1", "fx1"]


def test_should_prune_only_the_fixture_links_on_cleanup():
    """--remove unpicks the fixture from real teams. It must drop ONLY the
    fixture esids — pruning a real link would silently unlink a live channel."""
    out = prune_team_links(["real1", "fx1", "real2", "fx2"], {"fx1", "fx2"})
    assert out == ["real1", "real2"]


def test_should_leave_a_team_untouched_when_it_has_no_fixture_links():
    links = ["real1", "real2"]
    assert prune_team_links(links, {"fx1"}) == links


def test_should_prune_the_legacy_scalar_string_shape():
    assert prune_team_links("fx1", {"fx1"}) == []
    assert prune_team_links("real1", {"fx1"}) == ["real1"]


def test_should_survive_a_round_trip_of_attach_then_prune():
    """Seeding then cleaning up must return the team to its original links —
    otherwise the fixture leaves a permanent mark on real data."""
    original = ["real1", "real2"]
    fixture = ["fx1", "fx2", "fx3"]
    attached = merge_team_channels(original, fixture)
    assert prune_team_links(attached, set(fixture)) == original


def test_should_name_the_connector_gating_flag_the_status_service_reads():
    """The connector card computes `enabled` from ds_youtube, and everything
    downstream (linked assets, card state, the dialog) is gated on it. Linking
    channels without setting it leaves the team reading "not configured" — the
    reason a fixture team showed no connections at all. Pin the field name so a
    rename in the status service surfaces here rather than as a silent no-op."""
    from tools.seed_youtube_label_fixture import TEAM_DS_FLAG
    from asset_management.web.services.connector_status_service import CONNECTORS

    youtube = next(c for c in CONNECTORS if c["key"] == "youtube")
    assert TEAM_DS_FLAG == youtube["team_flag"]


def test_should_pin_the_team_link_field_the_status_service_reads():
    from tools.seed_youtube_label_fixture import TEAM_CHANNELS_FIELD
    from asset_management.web.services.connector_status_service import CONNECTORS

    youtube = next(c for c in CONNECTORS if c["key"] == "youtube")
    assert TEAM_CHANNELS_FIELD == youtube["team_esid_field"]


def test_should_generate_distinct_auth_slugs_per_account():
    """youtube_account_auth_url is unique-constrained in the real config; two
    fixture docs sharing one would misrepresent the shape."""
    accounts, _ = build_fixture(None)
    assert accounts[0]["youtube_account_auth_url"] != accounts[1]["youtube_account_auth_url"]
    assert len(accounts[0]["youtube_account_auth_url"]) == 15
