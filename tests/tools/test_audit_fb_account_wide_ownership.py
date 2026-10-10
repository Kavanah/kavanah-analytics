"""Unit tests for the FB account-wide ownership audit.

Covers the pure classification + report logic (``classify_account``,
``build_report``) plus the ``_registered_owners`` grouping, with owner/claimant
sets injected — no ES. The I/O helpers (_account_wide_scope,
_account_wide_claims) are integration-only and not exercised here.

The anchor case is the 2026-07-14 finding: TC_MO (700000000000007) is
registered to FLKXP only, yet FLKOO imports it account-wide.
"""

from __future__ import annotations

import os
import sys

_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
_PY_ROOT = os.path.join(_REPO_ROOT, "bin", "python")
if _PY_ROOT not in sys.path:
    sys.path.insert(0, _PY_ROOT)

from tools.audit_fb_account_wide_ownership import (  # noqa: E402
    _registered_owners,
    build_report,
    classify_account,
)

FLKXP = "0HsphpMBrlUVAhtejKhG"
FLKOO = "zU_525sBIm2AdJKu3Wpa"
TC_MO = "700000000000007"


# ── classify_account ─────────────────────────────────────────────────────────


def test_should_flag_contended_when_non_owner_team_imports_account_wide():
    """The TC_MO shape: registered to FLKXP, but FLKOO also claims it."""
    kind, contenders = classify_account(owners={FLKXP}, claimants={FLKXP, FLKOO})
    assert kind == "contended"
    assert contenders == {FLKOO}


def test_should_flag_contended_when_only_a_non_owner_claims():
    """Owner never runs but a foreign team does — still a contention."""
    kind, contenders = classify_account(owners={FLKXP}, claimants={FLKOO})
    assert kind == "contended"
    assert contenders == {FLKOO}


def test_should_be_healthy_when_only_the_registered_owner_claims():
    kind, contenders = classify_account(owners={FLKXP}, claimants={FLKXP})
    assert kind == "healthy"
    assert contenders == set()


def test_should_be_healthy_when_no_team_imports_account_wide():
    """A registered account that no account-wide team resolves to is fine."""
    kind, contenders = classify_account(owners={FLKXP}, claimants=set())
    assert kind == "healthy"
    assert contenders == set()


def test_should_be_unowned_when_claimed_but_no_registered_owner():
    """Legacy-only twin with no team_esid: informational, not a failure."""
    kind, contenders = classify_account(owners=set(), claimants={FLKOO})
    assert kind == "unowned"
    assert contenders == {FLKOO}


# ── _registered_owners ───────────────────────────────────────────────────────


def test_should_collect_owner_only_from_docs_carrying_team_esid():
    """The wizard doc carries team_esid; the legacy twin does not — only the
    wizard doc's owner counts (mirrors the live TC_MO two-doc result)."""
    docs = [
        {"_id": "wiz", "fb_ad_account_id": TC_MO, "team_esid": FLKXP},
        {"_id": "leg", "fb_ad_account_id": TC_MO},  # legacy twin, no team_esid
    ]
    owners = _registered_owners(docs)
    assert owners == {TC_MO: {FLKXP}}


def test_should_ignore_docs_without_an_account_id():
    docs = [{"_id": "x", "team_esid": FLKXP}]
    assert _registered_owners(docs) == {}


# ── build_report ─────────────────────────────────────────────────────────────


def test_should_count_kinds_and_carry_contenders_through_report():
    owners = {TC_MO: {FLKXP}, "111": {"teamA"}}
    claims = {TC_MO: {FLKXP, FLKOO}, "111": {"teamA"}}
    report = build_report(
        account_ids=[TC_MO, "111"],
        owners_of=lambda a: owners.get(a, set()),
        claimants_of=lambda a: claims.get(a, set()),
        title_of=lambda a: "TC_MO" if a == TC_MO else "",
    )
    assert report["counts"] == {"healthy": 1, "contended": 1, "unowned": 0}
    contended = next(r for r in report["accounts"] if r["kind"] == "contended")
    assert contended["fb_ad_account_id"] == TC_MO
    assert contended["owners"] == [FLKXP]
    assert contended["contenders"] == [FLKOO]
