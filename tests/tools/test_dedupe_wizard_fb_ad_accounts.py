"""Unit tests for the wizard fb_ad_account dedup planner.

Covers the pure grouping/keep-newest logic in ``plan_deletions``. The ES I/O
(_fetch_wizard_docs, delete loop, team back-link read) is integration-only and
not exercised here.
"""

from __future__ import annotations

import os
import sys

_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
_PY_ROOT = os.path.join(_REPO_ROOT, "bin", "python")
if _PY_ROOT not in sys.path:
    sys.path.insert(0, _PY_ROOT)

from tools.dedupe_wizard_fb_ad_accounts import plan_deletions  # noqa: E402


def _doc(esid, fid, ts, title=""):
    return {"_id": esid, "fb_ad_account_id": fid, "ts": ts, "title": title}


def test_keeps_newest_timestamp_per_group():
    """Within an fb_ad_account_id group, the latest @timestamp survives and the
    rest are slated for deletion."""
    docs = [
        _doc("old", "act_111", "2026-06-01T00:00:00Z"),
        _doc("new", "act_111", "2026-06-15T00:00:00Z"),
        _doc("mid", "act_111", "2026-06-10T00:00:00Z"),
    ]
    groups, doomed = plan_deletions(docs)

    assert len(groups) == 1
    assert groups[0]["keep"]["_id"] == "new"
    assert sorted(doomed) == ["mid", "old"]


def test_singletons_are_never_deleted():
    """A group of one is left intact — nothing to dedup."""
    docs = [
        _doc("a", "act_111", "2026-06-01T00:00:00Z"),
        _doc("b", "act_222", "2026-06-02T00:00:00Z"),
    ]
    groups, doomed = plan_deletions(docs)

    assert doomed == []
    assert all(not g["drop"] for g in groups)


def test_doc_without_natural_key_is_left_untouched():
    """A doc with no fb_ad_account_id can't be safely grouped, so it is never
    a deletion candidate (and doesn't merge into another group)."""
    docs = [
        _doc("keyed1", "act_111", "2026-06-01T00:00:00Z"),
        _doc("keyed2", "act_111", "2026-06-02T00:00:00Z"),
        _doc("nokey", None, "2026-06-03T00:00:00Z"),
    ]
    groups, doomed = plan_deletions(docs)

    assert "nokey" not in doomed
    assert "keyed1" in doomed  # older of the keyed pair
    assert all(g["keep"]["_id"] != "nokey" for g in groups)


def test_missing_timestamp_sorts_oldest():
    """A doc with no @timestamp (ts == '') must lose to any timestamped doc in
    the same group, never win the keep slot."""
    docs = [
        _doc("nots", "act_111", ""),
        _doc("hasts", "act_111", "2026-06-01T00:00:00Z"),
    ]
    groups, doomed = plan_deletions(docs)

    assert groups[0]["keep"]["_id"] == "hasts"
    assert doomed == ["nots"]


# ── prefer the team-referenced survivor over plain newest ──────────


def test_team_referenced_doc_survives_over_newer_unreferenced():
    """When a team back-link points at an OLDER dupe, that referenced doc must
    survive — deleting it would orphan the team's import. This is the staging
    FLKVT case (2026-06-25): newest was unreferenced, the team pointed at the
    older one. Pure newest-wins would delete the doc the team imports off.
    """
    docs = [
        _doc("newest_unref", "act_111", "2026-06-24T00:00:00Z"),
        _doc("older_referenced", "act_111", "2026-06-22T00:00:00Z"),
        _doc("oldest_unref", "act_111", "2026-06-17T00:00:00Z"),
    ]
    groups, doomed = plan_deletions(docs, referenced={"older_referenced"})

    assert groups[0]["keep"]["_id"] == "older_referenced", (
        "the team-referenced doc must be kept even though it is not the newest; "
        "keeping the newest would orphan the team's fb_ad_account_esid back-link"
    )
    assert sorted(doomed) == ["newest_unref", "oldest_unref"]


def test_falls_back_to_newest_when_no_member_referenced():
    """A group whose docs are all unreferenced keeps the newest (unchanged
    behavior — the referenced set simply doesn't apply)."""
    docs = [
        _doc("old", "act_111", "2026-06-01T00:00:00Z"),
        _doc("new", "act_111", "2026-06-15T00:00:00Z"),
    ]
    # referenced points at an esid in a DIFFERENT group → no effect here.
    groups, doomed = plan_deletions(docs, referenced={"some-other-esid"})

    assert groups[0]["keep"]["_id"] == "new"
    assert doomed == ["old"]


def test_two_referenced_members_keep_newest_referenced():
    """Pathological group where two members are each team-referenced (two teams
    linked the same ad account to their own dupe). We cannot collapse without
    orphaning a team, so keep the NEWEST referenced doc; the older referenced
    one is dropped (the tool warns the operator to re-point it first)."""
    docs = [
        _doc("ref_new", "act_111", "2026-06-20T00:00:00Z"),
        _doc("ref_old", "act_111", "2026-06-10T00:00:00Z"),
        _doc("unref_newest", "act_111", "2026-06-25T00:00:00Z"),
    ]
    groups, doomed = plan_deletions(docs, referenced={"ref_new", "ref_old"})

    assert groups[0]["keep"]["_id"] == "ref_new", "newest of the referenced docs wins"
    assert sorted(doomed) == ["ref_old", "unref_newest"]
