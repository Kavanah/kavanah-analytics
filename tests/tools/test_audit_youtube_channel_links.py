"""Unit tests for the channel-side YouTube link audit.

Covers ``build_report`` with plain lists/sets injected — no ES, no Google HTTP.

Each case is a defect actually found on prod 2026-07-31 that the three
account-side audits could not see, because they all enumerate accounts first and
therefore never visit a channel row that names no (or a missing) account.
"""

from __future__ import annotations

import os
import sys

_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
_PY_ROOT = os.path.join(_REPO_ROOT, "bin", "python")
if _PY_ROOT not in sys.path:
    sys.path.insert(0, _PY_ROOT)

from tools.audit_youtube_channel_links import build_report  # noqa: E402


def _ch(doc_id, channel_id="UC_x", title="Chan", account="acct1", ws=""):
    src = {
        "_id": doc_id,
        "youtube_channel_id": channel_id,
        "youtube_channel_title": title,
        "youtube_account_esid": account,
    }
    if ws:
        src["workspace_esid"] = ws
    return src


# ── unlinked (no account at all) ─────────────────────────────────────────────


def test_should_flag_a_channel_with_no_account_esid():
    """Prod 2026-07-31: a Prabhu Yeshu Ke Saath channel doc had no
    youtube_account_esid. No account means no token means it can never import,
    and no account-side audit enumerates it."""
    report = build_report([_ch("c1", account="")], account_ids={"acct1"})
    assert report["counts"]["unlinked"] == 1
    assert report["unlinked"][0]["_id"] == "c1"
    # An unlinked row is NOT also counted as dangling — distinct remedies.
    assert report["counts"]["dangling"] == 0


def test_should_not_place_an_unlinked_channel_in_the_account_map():
    report = build_report([_ch("c1", account="")], account_ids={"acct1"})
    assert report["by_account"] == {}


# ── dangling (account named but missing) ─────────────────────────────────────


def test_should_flag_a_channel_pointing_at_a_nonexistent_account():
    """The failure mode created by deleting an account doc that still had
    channels — the exact thing the delete-safety check exists to prevent."""
    report = build_report([_ch("c1", account="ghost")], account_ids={"acct1"})
    assert report["counts"]["dangling"] == 1
    assert report["dangling"][0]["account_esid"] == "ghost"


def test_should_not_flag_a_channel_whose_account_exists():
    report = build_report([_ch("c1", account="acct1")], account_ids={"acct1"})
    assert report["counts"]["dangling"] == 0
    assert report["counts"]["unlinked"] == 0


# ── duplicate channel ids ────────────────────────────────────────────────────


def test_should_flag_the_same_channel_id_in_two_docs():
    """Prod 2026-07-31: Fixture App UCV7PTXjMdn8A6le-wu3xcvw existed on both the
    dead doc and its successor. Whichever the importer honors is scan-order
    dependent, and a surviving copy makes the dead doc look delete-unsafe."""
    rows = [
        _ch("c1", channel_id="UC_dup", title="Fixture App", account="dead"),
        _ch("c2", channel_id="UC_dup", title="Fixture App", account="successor"),
    ]
    report = build_report(rows, account_ids={"dead", "successor"})
    assert report["counts"]["duplicate_channel_ids"] == 1
    assert len(report["duplicates"]["UC_dup"]) == 2


def test_should_flag_a_duplicate_even_when_one_copy_is_unlinked():
    """The Prabhu shape: one copy correctly linked, one copy with no account."""
    rows = [
        _ch("c1", channel_id="UC_dup", account=""),
        _ch("c2", channel_id="UC_dup", account="acct1"),
    ]
    report = build_report(rows, account_ids={"acct1"})
    assert report["counts"]["duplicate_channel_ids"] == 1
    assert report["counts"]["unlinked"] == 1


def test_should_not_flag_distinct_channel_ids_on_one_account():
    """The normal multi-channel account — must stay quiet."""
    rows = [
        _ch("c1", channel_id="UC_a", account="acct1"),
        _ch("c2", channel_id="UC_b", account="acct1"),
    ]
    report = build_report(rows, account_ids={"acct1"})
    assert report["counts"]["duplicate_channel_ids"] == 0
    assert len(report["by_account"]["acct1"]) == 2


def test_should_ignore_channel_docs_with_no_channel_id_when_grouping():
    """A row with no youtube_channel_id can't be compared for duplication; two
    of them must not read as duplicates of each other."""
    rows = [_ch("c1", channel_id="", account="acct1"), _ch("c2", channel_id="", account="acct1")]
    report = build_report(rows, account_ids={"acct1"})
    assert report["counts"]["duplicate_channel_ids"] == 0


# ── the account map ──────────────────────────────────────────────────────────


def test_should_group_channels_under_their_account_for_the_mapping():
    """The 'which account goes with which channel' answer Asset Explorer does
    not surface."""
    rows = [
        _ch("c1", channel_id="UC_a", title="A", account="bucket"),
        _ch("c2", channel_id="UC_b", title="B", account="bucket"),
        _ch("c3", channel_id="UC_c", title="C", account="solo"),
    ]
    report = build_report(rows, account_ids={"bucket", "solo"})
    assert len(report["by_account"]["bucket"]) == 2
    assert len(report["by_account"]["solo"]) == 1


def test_should_report_a_clean_graph_as_having_no_defects():
    rows = [_ch("c1", channel_id="UC_a", account="acct1")]
    report = build_report(rows, account_ids={"acct1"})
    c = report["counts"]
    assert (c["unlinked"], c["dangling"], c["duplicate_channel_ids"]) == (0, 0, 0)
