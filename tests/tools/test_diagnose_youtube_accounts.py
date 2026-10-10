"""Unit tests for the YouTube account-doc diagnostic.

Covers the pure logic (``classify_token_state``, ``build_report``) with account
sources built inline — no ES, no Google HTTP.

The load-bearing assertions are the two that guard against destructive
mistakes: a doc with linked channels must never report delete-safe, and docs
sharing a Google email must be grouped WITHOUT being called duplicates.
"""

from __future__ import annotations

import os
import sys

_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
_PY_ROOT = os.path.join(_REPO_ROOT, "bin", "python")
if _PY_ROOT not in sys.path:
    sys.path.insert(0, _PY_ROOT)

from tools.audit_youtube_duplicate_accounts import UNRECOVERABLE  # noqa: E402
from tools.diagnose_youtube_accounts import (  # noqa: E402
    build_report,
    classify_token_state,
)


def _acc(esid, title="", identity=None, owned=(), token="enc", email="", ws=""):
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
    if ws:
        src["workspace_esid"] = ws
    return src


# ── classify_token_state ─────────────────────────────────────────────────────


def test_should_distinguish_a_tokenless_doc_from_a_tokened_one():
    """The ownership audit collapses 'never authorized' and 'refresh failed'
    into one `unknown` bucket; the whole point of this tool is to separate them,
    because the remedies differ (retire vs re-auth)."""
    assert classify_token_state(_acc("a", token="")) == "no_token"
    assert classify_token_state(_acc("b", token="enc")) == "tokened"


# ── delete safety ────────────────────────────────────────────────────────────


def test_should_never_report_delete_safe_when_channels_still_point_at_the_doc():
    """Deleting a doc with linked channels silently orphans them. This is the
    guard that makes a 'named removal candidate' actually safe to remove."""
    report = build_report([_acc("dead", title="Fixture App", token="")], linked_of=lambda e: {"UC_a": "Still Linked"})
    doc = report["docs"][0]
    assert doc["safe_to_delete"] is False
    assert doc["linked_count"] == 1


def test_should_report_delete_safe_only_when_nothing_links_to_the_doc():
    report = build_report([_acc("orphan", title="Orphan Dupe", token="")], linked_of=lambda e: {})
    assert report["docs"][0]["safe_to_delete"] is True


# ── shared-bucket detection (the Info Sample shape) ────────────────────────


def test_should_flag_a_doc_whose_linked_channels_exceed_its_owned_set():
    """The 12 linked, 1 owned. Neither existing audit names this
    as 'being used as a shared bucket'."""
    linked = {f"UC_{i}": f"Channel {i}" for i in range(12)}
    report = build_report(
        [_acc("bucket", title="Info Sample", owned=["UC_0"], identity=UNRECOVERABLE)],
        linked_of=lambda e: linked,
    )
    doc = report["docs"][0]
    assert len(doc["unowned_linked"]) == 11
    assert "UC_0" not in doc["unowned_linked"]


def test_should_not_claim_unowned_channels_when_the_owned_set_is_unstamped():
    """An empty owned set is absence of evidence, not evidence of absence —
    reporting all linked channels as unowned there would be a false alarm on
    every never-probed doc."""
    report = build_report([_acc("nostamp", title="No Stamp", owned=[])], linked_of=lambda e: {"UC_a": "A"})
    assert report["docs"][0]["unowned_linked"] == []


def test_should_not_flag_a_healthy_doc_whose_owned_set_covers_its_links():
    report = build_report(
        [_acc("ok", title="Healthy", owned=["UC_a", "UC_extra"])],
        linked_of=lambda e: {"UC_a": "A"},
    )
    assert report["docs"][0]["unowned_linked"] == []


# ── identity reporting ───────────────────────────────────────────────────────


def test_should_mark_the_unrecoverable_sentinel_as_an_unusable_identity():
    """writes UNRECOVERABLE when a legacy grant yields no sub. It must
    not read as a real identity."""
    report = build_report([_acc("legacy", identity=UNRECOVERABLE)], linked_of=lambda e: {})
    doc = report["docs"][0]
    assert doc["identity"] == UNRECOVERABLE
    assert doc["identity_usable"] is False


def test_should_mark_a_real_sub_as_usable():
    report = build_report([_acc("modern", identity="103401661513291815428")], linked_of=lambda e: {})
    assert report["docs"][0]["identity_usable"] is True


# ── email grouping (must NOT assert duplication) ─────────────────────────────


def test_should_group_docs_sharing_an_email_without_calling_them_duplicates():
    """The prod Info Sample pair shares one Gmail but may be two distinct
    Brand Account identities. Grouping is informational only — the report must
    expose the grouping and leave the judgment to the duplicate audit."""
    accounts = [
        _acc("infoOld", title="Info Sample", email="info@example.com", identity=UNRECOVERABLE),
        _acc("infoNew", title="Info Sample", email="info@example.com", identity="sub-new"),
    ]
    report = build_report(accounts, linked_of=lambda e: {})
    assert report["email_groups"] == {"info@example.com": ["infoOld", "infoNew"]}
    # No doc-level duplicate verdict is emitted anywhere in this report.
    assert all("duplicate" not in d for d in report["docs"][0])


def test_should_not_group_a_lone_doc_or_docs_with_distinct_emails():
    accounts = [
        _acc("a", email="one@example.com"),
        _acc("b", email="two@example.com"),
    ]
    assert build_report(accounts, linked_of=lambda e: {})["email_groups"] == {}


def test_should_group_emails_case_insensitively():
    accounts = [
        _acc("a", email="Same@Example.com"),
        _acc("b", email="same@example.com"),
    ]
    groups = build_report(accounts, linked_of=lambda e: {})["email_groups"]
    assert list(groups) == ["same@example.com"]


def test_should_not_group_docs_that_have_no_email():
    accounts = [_acc("a", email=""), _acc("b", email="")]
    assert build_report(accounts, linked_of=lambda e: {})["email_groups"] == {}


# ── ordering ─────────────────────────────────────────────────────────────────


def test_should_rank_shared_buckets_ahead_of_healthy_docs():
    """A human scanning the output should hit the actionable doc first."""
    accounts = [
        _acc("healthy", title="Healthy", owned=["UC_x"]),
        _acc("bucket", title="Bucket", owned=["UC_a"]),
    ]
    report = build_report(
        accounts,
        linked_of=lambda e: {"UC_a": "A", "UC_b": "B"} if e == "bucket" else {"UC_x": "X"},
    )
    assert report["docs"][0]["_id"] == "bucket"
