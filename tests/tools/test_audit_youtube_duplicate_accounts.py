"""Unit tests for the cross-doc YouTube duplicate-account audit.

Covers the pure pair-classification + report logic (``classify_pair``,
``build_report``) with account sources built inline — no ES, no Google HTTP.
The reused I/O helpers (_fetch_all_accounts, _linked_channels) are
integration-only and not exercised here.

The load-bearing case is the prod-verified "Info Sample Jesus Initiative"
pair: two docs, SAME title, DIFFERENT Google identities. That must classify as
distinct_identity, never as a duplicate — merging it recreates the
token ping-pong.
"""

from __future__ import annotations

import os
import sys

_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
_PY_ROOT = os.path.join(_REPO_ROOT, "bin", "python")
if _PY_ROOT not in sys.path:
    sys.path.insert(0, _PY_ROOT)

from tools.audit_youtube_duplicate_accounts import (  # noqa: E402
    UNRECOVERABLE,
    build_report,
    classify_pair,
)

INFO_DISCOVER = "Info Sample Jesus Initiative"


def _acc(esid, title="", identity=None, owned=(), token="enc", email=""):
    """An account doc shaped like a de_asset_youtube_accounts _source + _id."""
    src = {
        "_id": esid,
        "youtube_account_title": title,
        "youtube_account_email": email,
        "youtube_account_owned_channel_ids": list(owned),
    }
    if identity is not None:
        src["youtube_account_identity"] = identity
    if token:
        src["youtube_account_token"] = token
    return src


# ── classify_pair: the real duplicate ────────────────────────────────────────


def test_should_flag_duplicate_identity_when_two_docs_share_a_google_sub():
    """One identity can hold only one token, so one identity in two docs is a
    genuine duplicate — the only safe-to-merge verdict."""
    left = _acc("docA", title="Trellis Partners", identity="103401661513291815428")
    right = _acc("docB", title="Trellis Partners (old)", identity="103401661513291815428")
    assert classify_pair(left, right) == "duplicate_identity"


def test_should_flag_duplicate_identity_even_when_titles_differ_entirely():
    """Identity is the signal; the display title is cosmetic and must not gate it."""
    left = _acc("docA", title="Completely Different Name", identity="sub-999")
    right = _acc("docB", title="Nothing Alike", identity="sub-999")
    assert classify_pair(left, right) == "duplicate_identity"


# ── classify_pair: the must-not-merge case ───────────────────────────────────


def test_should_report_distinct_identity_for_the_info_discover_pair():
    """Prod-verified 2026-07-13: two docs, same title, two distinct Brand Account
    identities. NOT a duplicate — one doc per identity is the model."""
    left = _acc("jVj4LpwBIm2AdJKusRc6", title=INFO_DISCOVER, identity="sub-old")
    right = _acc("Bq8RXJ8BTJnYq0K40LDs", title=INFO_DISCOVER, identity="103401661513291815428")
    assert classify_pair(left, right) == "distinct_identity"


def test_should_not_flag_different_identities_even_when_owned_sets_overlap():
    """Different identities are definitively not duplicates. Overlap must never
    upgrade them to a merge candidate — that is the ping-pong bug shape."""
    left = _acc("docA", title="A", identity="sub-1", owned=["UC_shared"])
    right = _acc("docB", title="B", identity="sub-2", owned=["UC_shared"])
    assert classify_pair(left, right) is None


def test_should_ignore_unrecoverable_sentinel_rather_than_treat_it_as_an_identity():
    """stamps UNRECOVERABLE when a legacy grant yields no sub. Treating
    it as a real identity would collapse every unrecoverable doc into one
    bogus duplicate_identity pair."""
    left = _acc("docA", title="A", identity=UNRECOVERABLE, owned=["UC_a"])
    right = _acc("docB", title="B", identity=UNRECOVERABLE, owned=["UC_b"])
    # Non-overlapping owned sets and different titles => unrelated, not duplicate.
    assert classify_pair(left, right) is None


# ── classify_pair: the sub-miss shape ───────────────────────────────


def test_should_flag_duplicate_owned_set_when_subless_docs_overlap():
    """Legacy pre-openid grants yield no sub, so overlap is the best signal."""
    left = _acc("docA", title="Samplehouse", owned=["UC_x", "UC_y"])
    right = _acc("docB", title="Samplehouse dup", owned=["UC_y"])
    assert classify_pair(left, right) == "duplicate_owned_set"


def test_should_not_flag_subless_docs_with_empty_owned_sets():
    """An empty owned set can't overlap — the guard. Two freshly created
    tokenless docs must not read as duplicates of each other."""
    left = _acc("docA", title="A", owned=[])
    right = _acc("docB", title="B", owned=[])
    assert classify_pair(left, right) is None


def test_should_not_flag_subless_docs_with_disjoint_owned_sets():
    left = _acc("docA", title="A", owned=["UC_a"])
    right = _acc("docB", title="B", owned=["UC_b"])
    assert classify_pair(left, right) is None


def test_should_report_title_collision_when_identity_evidence_is_missing():
    """Same title, no identity on one side, no overlap: unknowable from stored
    state. Surfaced as its own bucket, never as a duplicate."""
    left = _acc("docA", title=INFO_DISCOVER, identity="sub-1", owned=["UC_a"])
    right = _acc("docB", title=INFO_DISCOVER, owned=["UC_b"])
    assert classify_pair(left, right) == "title_collision_unknown"


def test_should_not_flag_distinct_titles_with_no_identity_evidence():
    left = _acc("docA", title="Alpha", owned=[])
    right = _acc("docB", title="Beta", owned=[])
    assert classify_pair(left, right) is None


def test_should_treat_titles_as_equal_across_whitespace_and_case():
    left = _acc("docA", title="  info   SAMPLE Jesus Initiative ", owned=["UC_a"])
    right = _acc("docB", title="Info Sample Jesus Initiative", owned=["UC_b"])
    assert classify_pair(left, right) == "title_collision_unknown"


def test_should_not_pair_two_untitled_docs_on_empty_title_match():
    """Two docs with blank titles must not read as a title collision."""
    left = _acc("docA", title="", owned=["UC_a"])
    right = _acc("docB", title="", owned=["UC_b"])
    assert classify_pair(left, right) is None


# ── build_report ─────────────────────────────────────────────────────────────


def test_should_count_verdicts_and_carry_linked_counts_per_side():
    accounts = [
        _acc("docA", title="Dup", identity="sub-1"),
        _acc("docB", title="Dup other", identity="sub-1"),
    ]
    report = build_report(accounts, linked_of=lambda esid: {"UC_1": "One"} if esid == "docA" else {})

    assert report["counts"]["duplicate_identity"] == 1
    pair = report["pairs"][0]
    assert pair["verdict"] == "duplicate_identity"
    by_id = {s["_id"]: s for s in pair["sides"]}
    assert by_id["docA"]["linked_count"] == 1
    assert by_id["docB"]["linked_count"] == 0


def test_should_rank_duplicates_ahead_of_informational_verdicts():
    """Actionable pairs must lead the output — a human scanning the log should
    not have to read past cleared pairs to find the real duplicate."""
    accounts = [
        _acc("infoOld", title=INFO_DISCOVER, identity="sub-old"),
        _acc("infoNew", title=INFO_DISCOVER, identity="sub-new"),
        _acc("dupA", title="Real Dup", identity="sub-dup"),
        _acc("dupB", title="Real Dup 2", identity="sub-dup"),
    ]
    report = build_report(accounts, linked_of=lambda esid: {})

    assert report["pairs"][0]["verdict"] == "duplicate_identity"
    assert report["counts"]["duplicate_identity"] == 1
    assert report["counts"]["distinct_identity"] == 1


def test_should_not_compare_a_doc_against_itself():
    """A single doc can never be its own duplicate."""
    report = build_report([_acc("docA", title="Solo", identity="sub-1")], linked_of=lambda esid: {})
    assert report["pairs"] == []


def test_should_report_shared_channel_ids_for_a_flagged_overlap_pair():
    accounts = [
        _acc("docA", title="A", owned=["UC_x", "UC_shared"]),
        _acc("docB", title="B", owned=["UC_shared", "UC_z"]),
    ]
    report = build_report(accounts, linked_of=lambda esid: {})
    assert report["pairs"][0]["shared_channel_ids"] == ["UC_shared"]
