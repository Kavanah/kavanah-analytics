"""Unit tests for the YouTube account-ownership audit.

Covers the pure classification + report logic (``classify_account``,
``build_report``) with linked/owned sets injected — no ES, no Google HTTP.
The I/O helpers (_fetch_all_accounts, _linked_channels, and the reused
_owned_channel_ids probe) are integration-only and not exercised here.
"""

from __future__ import annotations

import os
import sys

import pytest

_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
_PY_ROOT = os.path.join(_REPO_ROOT, "bin", "python")
if _PY_ROOT not in sys.path:
    sys.path.insert(0, _PY_ROOT)

from tools.audit_youtube_account_ownership import (  # noqa: E402
    build_report,
    classify_account,
)


# ── classify_account ─────────────────────────────────────────────────────────


def test_should_flag_orphaned_when_token_owns_only_some_linked_channels():
    """The Info Sample shape: one linked channel owned, the rest orphaned."""
    linked = {"UC_owned": "FLKDF", "UC_orphan1": "Radio Sample", "UC_orphan2": "Bada Jeevan"}
    kind, orphaned = classify_account(linked, owned={"UC_owned"})
    assert kind == "orphaned"
    assert orphaned == {"UC_orphan1": "Radio Sample", "UC_orphan2": "Bada Jeevan"}


def test_should_flag_orphaned_when_token_owns_none_of_the_linked_channels():
    linked = {"UC_a": "A", "UC_b": "B"}
    kind, orphaned = classify_account(linked, owned=set())
    assert kind == "orphaned"
    assert orphaned == linked


def test_should_be_healthy_when_token_owns_every_linked_channel():
    """A correctly-scoped doc (incl. a post-split single-channel doc like FLKDF,
    or a legacy sub-less doc whose owned set covers its linked set)
    must NOT flag."""
    linked = {"UC_owned": "FLKDF"}
    kind, orphaned = classify_account(linked, owned={"UC_owned", "UC_other_owned_but_unlinked"})
    assert kind == "healthy"
    assert orphaned == {}


def test_should_be_unknown_when_ownership_is_unknowable():
    """owned=None is the _owned_channel_ids contract for no-token / refresh
    failed / probe failed — a distinct bucket, NEVER a false orphan flag."""
    linked = {"UC_a": "A"}
    kind, orphaned = classify_account(linked, owned=None)
    assert kind == "unknown"
    assert orphaned == {}


def test_should_be_unlinked_when_tokened_doc_has_no_channels():
    kind, orphaned = classify_account(linked={}, owned={"UC_x"})
    assert kind == "unlinked"
    assert orphaned == {}


# ── build_report ─────────────────────────────────────────────────────────────


def _accounts():
    return [
        {"_id": "acc-info", "youtube_account_title": "Info Sample"},
        {"_id": "acc-clean", "youtube_account_title": "Clean"},
        {"_id": "acc-notoken", "youtube_account_title": "No Token"},
        {"_id": "acc-empty", "youtube_account_title": "Unlinked"},
    ]


def _fixtures():
    linked_by = {
        "acc-info": {"UC_owned": "FLKDF", "UC_orphan": "Radio Sample"},
        "acc-clean": {"UC_clean": "Clean Channel"},
        "acc-notoken": {"UC_x": "Stranded"},
        "acc-empty": {},
    }
    owned_by = {
        "acc-info": {"UC_owned"},
        "acc-clean": {"UC_clean"},
        "acc-notoken": None,  # unknowable
        "acc-empty": {"UC_something"},
    }
    return linked_by, owned_by


def test_report_counts_each_kind_and_lists_orphans():
    linked_by, owned_by = _fixtures()
    report = build_report(
        _accounts(),
        linked_of=lambda esid: linked_by[esid],
        owned_of=lambda acc: owned_by[acc["_id"]],
    )
    assert report["counts"] == {"healthy": 1, "orphaned": 1, "unknown": 1, "unlinked": 1}

    by_id = {r["_id"]: r for r in report["accounts"]}
    assert by_id["acc-info"]["kind"] == "orphaned"
    assert by_id["acc-info"]["orphaned"] == {"UC_orphan": "Radio Sample"}
    assert by_id["acc-info"]["linked_count"] == 2
    assert by_id["acc-clean"]["kind"] == "healthy"
    assert by_id["acc-notoken"]["kind"] == "unknown"
    assert by_id["acc-empty"]["kind"] == "unlinked"


def test_report_on_empty_index_is_all_zero():
    report = build_report([], linked_of=lambda esid: {}, owned_of=lambda acc: set())
    assert report["accounts"] == []
    assert report["counts"] == {"healthy": 0, "orphaned": 0, "unknown": 0, "unlinked": 0}


def test_run_returns_nonzero_only_when_an_orphan_exists():
    """The exit-code contract: unknown/unlinked are not failures; only an
    orphaned doc makes the tool exit 1. Exercised through build_report so the
    counts that drive run()'s return value are pinned."""
    linked_by, owned_by = _fixtures()

    # Full fixture set has one orphan -> would exit 1.
    report = build_report(
        _accounts(),
        linked_of=lambda esid: linked_by[esid],
        owned_of=lambda acc: owned_by[acc["_id"]],
    )
    assert report["counts"]["orphaned"] == 1

    # Drop the orphan doc: only healthy/unknown/unlinked remain -> would exit 0.
    no_orphan = [a for a in _accounts() if a["_id"] != "acc-info"]
    report2 = build_report(
        no_orphan,
        linked_of=lambda esid: linked_by[esid],
        owned_of=lambda acc: owned_by[acc["_id"]],
    )
    assert report2["counts"]["orphaned"] == 0
