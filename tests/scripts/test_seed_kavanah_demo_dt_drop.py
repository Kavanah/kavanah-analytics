"""seed_kavanah_demo_dt `--reseed` doubled the index on prod.

Background
----------
`seed_kavanah_demo_dt.py --reseed` deletes-then-reseeds so the curated totals
stay exact (2625 docs). On **prod** the target name
``kavanah_team_disciple_tools`` is an **ALIAS** (→ the ESID-keyed
concrete index), not a regular index. The old `_drop` did
``indices.exists(name)`` (true for an alias) then ``indices.delete(name)`` —
which ES rejects on an alias with a 400 ``illegal_argument_exception``. The
``except`` swallowed it, the delete never happened, and the subsequent bulk
APPENDED a second copy → 2625 → 5250 (every `--verify` total exactly 2×).

On dev/staging the name is a regular index the seeder created, so delete
succeeded and the bug never showed — the env-divergence trap.

The fix: `_drop` detects the alias via ``is_alias`` and clears it with
``delete_by_query`` match_all (preserving the index, mapping, and alias
binding), instead of ``indices.delete``. Regular-index and data-stream paths
are unchanged.

These are shape/behaviour pins on the extracted module-level `_drop`. The
end-to-end "prod reseed lands 2625, not 5250" was verified on prod 2026-06-18.

Mutation-verified locally:
  - Revert `_drop` to call ``indices.delete`` on the alias branch → the alias
    test fails (delete on an alias is the doubling bug).
  - Drop the ``is_alias`` short-circuit so the alias falls through to the
    regular path → ``indices.delete`` is called on the alias → test fails.
"""

from __future__ import annotations

import pytest

from scripts import seed_kavanah_demo_dt as seeder


class _FakeIndices:
    def __init__(self, parent):
        self._p = parent

    def exists_alias(self, name=None):
        return name in self._p.aliases

    def get_alias(self, name=None):
        # mirror es.indices.get_alias(name=...) → {concrete_index: {"aliases": {...}}}
        return {self._p.aliases[name]: {"aliases": {name: {}}}}

    def exists(self, index=None):
        # ES `exists` is true for an alias OR a concrete index.
        return index in self._p.aliases or index in self._p.regular

    def delete(self, index=None):
        # Real ES 400s when `index` is an alias. Model that: recording a delete
        # against an alias is the bug, so blow up the way ES would.
        if index in self._p.aliases:
            raise RuntimeError(
                "illegal_argument_exception: [" + index + "] matches an alias, "
                "specify the corresponding concrete indices instead"
            )
        self._p.deleted_indices.append(index)
        self._p.regular.discard(index)

    def delete_data_stream(self, name=None):
        self._p.deleted_data_streams.append(name)


class _FakeES:
    """Minimal ES double that knows aliases vs regular indices and records the
    drop path taken."""

    def __init__(self, aliases=None, regular=None):
        self.aliases = dict(aliases or {})  # alias_name -> concrete_index
        self.regular = set(regular or [])
        self.deleted_indices = []
        self.deleted_data_streams = []
        self.delete_by_query_calls = []
        self.indices = _FakeIndices(self)

    def delete_by_query(self, index=None, body=None, refresh=None, conflicts=None):
        self.delete_by_query_calls.append({"index": index, "body": body})
        return {"deleted": 2625}


def test_drop_clears_alias_via_delete_by_query_not_delete():
    """Against an ALIAS (the prod shape), `_drop` must purge via
    delete_by_query and must NOT call indices.delete — calling delete on an
    alias 400s and silently no-ops, which is the doubling bug."""
    es = _FakeES(aliases={"kavanah_team_disciple_tools": "vjhmt50bfzy0o-sz8qzx__disciple_tools"})

    removed = seeder._drop(es, "kavanah_team_disciple_tools")

    assert removed is True
    # the load-bearing fix: docs cleared via delete_by_query match_all
    assert len(es.delete_by_query_calls) == 1, "alias must be cleared via delete_by_query"
    assert es.delete_by_query_calls[0]["index"] == "kavanah_team_disciple_tools"
    assert es.delete_by_query_calls[0]["body"] == {"query": {"match_all": {}}}
    # and crucially NOT via indices.delete (which 400s on an alias → no-op → double)
    assert es.deleted_indices == [], "must NOT indices.delete an alias (that is the doubling bug)"


def test_drop_still_deletes_a_regular_index():
    """On dev/staging the name is a regular index — `_drop` must still delete
    it outright (no delete_by_query), preserving the original behaviour."""
    es = _FakeES(regular={"kavanah_team_disciple_tools"})

    removed = seeder._drop(es, "kavanah_team_disciple_tools")

    assert removed is True
    assert es.deleted_indices == ["kavanah_team_disciple_tools"]
    assert es.delete_by_query_calls == [], "regular index must be deleted, not delete_by_query'd"


def test_drop_handles_nonexistent_name_without_raising():
    """Neither alias nor regular index present (fresh env / already-clean):
    `_drop` is a no-op that does not raise (the data-stream delete is swallowed)."""
    es = _FakeES()

    # must not raise; nothing to clear
    seeder._drop(es, "kavanah_team_disciple_tools")

    assert es.delete_by_query_calls == []
    assert es.deleted_indices == []
