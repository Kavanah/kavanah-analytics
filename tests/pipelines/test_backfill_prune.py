"""follow-up — backfill history records respect delivery.snapshot_keep.

Before this, backfill RUN records (record_type="backfill") were written to the
snapshot index on every backfill but NEVER pruned (prune_snapshots only ran on a
report-snapshot write, and a backfill builds no snapshot), so History piled up
unbounded. Now:

  * prune_snapshots is record_type-aware — report snapshots and backfill records
    prune on INDEPENDENT keep-counts, so neither kind evicts the other.
  * backfill_now prunes its own kind after writing the history record.
"""

from __future__ import annotations

from unittest import mock

import pytest

_MOD = "asset_management.web.services.report_schedule_service"


class _FilteringEs:
    """ES double that honors the record_type filter, generated_at sort, and the
    ``from`` offset prune_snapshots relies on. Docs: {id: {generated_at,
    record_type}}."""

    def __init__(self, docs):
        self.docs = dict(docs)
        self.deleted = []

    def _is_backfill(self, doc):
        return doc.get("record_type") == "backfill"

    def _matches(self, doc, filters):
        """Model the actual ES bool-filter predicates prune_snapshots builds:
        a term on record_type=backfill, or a must_not term on it, or (bug) no
        record_type predicate at all → matches every doc."""
        for f in filters:
            term = f.get("term", {})
            if term.get("record_type") == "backfill":
                if not self._is_backfill(doc):
                    return False
            must_not = f.get("bool", {}).get("must_not", [])
            for mn in must_not:
                if mn.get("term", {}).get("record_type") == "backfill" and self._is_backfill(doc):
                    return False
        return True

    def search(self, index=None, body=None, **kwargs):
        filters = body["query"]["bool"]["filter"]
        matched = [(sid, d) for sid, d in self.docs.items() if self._matches(d, filters)]
        matched.sort(key=lambda kv: kv[1]["generated_at"], reverse=True)  # newest first
        frm = body.get("from", 0)
        size = body.get("size", 10000)
        page = matched[frm : frm + size]
        return {"hits": {"hits": [{"_id": sid} for sid, _ in page]}}

    def delete(self, index=None, id=None, **kwargs):
        del self.docs[id]
        self.deleted.append(id)


def _svc():
    from asset_management.web.services.report_schedule_service import ReportScheduleService

    return ReportScheduleService


def _docs(snapshots, backfills):
    """snapshots / backfills: lists of ISO generated_at strings, newest last."""
    out = {}
    for i, ts in enumerate(snapshots):
        out[f"snap{i}"] = {"generated_at": ts, "record_type": "snapshot"}
    for i, ts in enumerate(backfills):
        out[f"bf{i}"] = {"generated_at": ts, "record_type": "backfill"}
    return out


def test_prune_backfill_deletes_oldest_backfills_beyond_keep():
    es = _FilteringEs(_docs(snapshots=[], backfills=[f"2026-07-{d:02d}" for d in range(1, 11)]))
    deleted = _svc().prune_snapshots(es, "s1", keep=3, record_type="backfill")
    assert deleted == 7
    # The 3 newest backfills survive; snapshots are untouched (none here).
    assert set(es.docs) == {"bf9", "bf8", "bf7"}


def test_prune_backfill_never_touches_report_snapshots():
    es = _FilteringEs(
        _docs(
            snapshots=[f"2026-07-{d:02d}" for d in range(1, 6)],  # 5 snapshots
            backfills=[f"2026-07-{d:02d}" for d in range(1, 11)],  # 10 backfills
        )
    )
    deleted = _svc().prune_snapshots(es, "s1", keep=2, record_type="backfill")
    # Only backfills pruned (10 -> 2); all 5 report snapshots survive.
    assert deleted == 8
    assert sum(1 for d in es.docs.values() if d["record_type"] == "snapshot") == 5
    assert sum(1 for d in es.docs.values() if d["record_type"] == "backfill") == 2


def test_prune_snapshot_default_excludes_backfill_records():
    # A snapshot prune must not count backfill records toward its keep window, and
    # must never delete one. 3 snapshots + 3 backfills, keep=2 -> delete 1 snapshot.
    es = _FilteringEs(
        _docs(
            snapshots=["2026-07-01", "2026-07-02", "2026-07-03"],
            backfills=["2026-07-04", "2026-07-05", "2026-07-06"],  # newer, but a different kind
        )
    )
    deleted = _svc().prune_snapshots(es, "s1", keep=2)  # record_type defaults to "snapshot"
    assert deleted == 1
    assert es.deleted == ["snap0"]  # the oldest SNAPSHOT, not any backfill
    assert sum(1 for d in es.docs.values() if d["record_type"] == "backfill") == 3


def test_prune_snapshot_treats_missing_record_type_as_snapshot():
    # earlier rows have no record_type — they must still prune as snapshots.
    es = _FilteringEs({f"old{i}": {"generated_at": f"2026-07-0{i+1}"} for i in range(4)})
    deleted = _svc().prune_snapshots(es, "s1", keep=2)
    assert deleted == 2


def test_backfill_now_prunes_backfill_records():
    R = _svc()
    schedule = {
        "schedule_id": "im1",
        "connector_id": "google_ads",
        "team_esids": ["team_a"],
        "org_id": "org_1",
        "team_id": "__all__",
        "delivery": {"save_snapshot": True, "snapshot_keep": 5},
        "schedule": {"frequency": "daily", "time": "06:00", "timezone": "UTC", "date_range_mode": "rolling_7d"},
        "query": {},
        "status": "active",
    }
    es = object()
    with mock.patch(f"{_MOD}.get_elasticsearch_client", return_value=es), mock.patch.object(
        R, "get_schedule", return_value=schedule
    ), mock.patch.object(
        R, "_schedule_team_esids", return_value=["team_a"]
    ), mock.patch.object(
        R, "_set_run_state"
    ), mock.patch.object(
        R, "_start_progress_run", return_value=None
    ), mock.patch.object(
        R, "_trigger_scoped_import", return_value={"dispatched": True, "pids": [], "lock_scope": ""}
    ), mock.patch.object(
        R, "write_backfill_history_record", return_value="bf_new"
    ), mock.patch.object(
        R, "prune_snapshots", return_value=0
    ) as mock_prune, mock.patch(
        f"{_MOD}._now"
    ) as now:
        from datetime import datetime, timezone

        now.return_value = datetime(2026, 7, 16, 12, 0, tzinfo=timezone.utc)
        res = R.backfill_now("im1", {"from": "2026-07-01", "to": "2026-07-10"}, requested_by="alice")

    assert res["success"]
    # The backfill pruned its OWN kind with the schedule's clamped keep.
    mock_prune.assert_called_once()
    assert mock_prune.call_args.kwargs.get("record_type") == "backfill"
    assert mock_prune.call_args.kwargs.get("keep") == 5
