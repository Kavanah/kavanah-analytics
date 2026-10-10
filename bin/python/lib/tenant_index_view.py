"""reverse-lookup join for tenant data indices.

Turns an opaque data-index name (``{team_esid}__{source}``, or
the legacy ``{slug}_{source}`` during the migration window) back into its
human-readable team / org / workspace. Extracted from
``bin/scripts/list-tenant-indices.sh`` so the join is unit-testable; the CLI
imports and renders these rows.

LEFT JOIN semantics: ``build_rows`` emits one row per tenant data index and
NEVER drops a row — an index whose team doc is missing/deleted, or a team with
no org/workspace, still appears with the ``DASH`` placeholder in the
unresolved columns. Orphan indices are exactly what an operator debugs here.
"""

from lib.es_helpers import KNOWN_SOURCES, sanitize_team_title

DASH = "—"


def _src(resp):
    """Read ``_source`` off an ES get response (elasticsearch 9.x safe).

    The 9.x client returns an ``ObjectApiResponse`` (mapping interface, not a
    dict), so read via ``.body`` when present.
    """
    body = resp.body if hasattr(resp, "body") else resp
    return body.get("_source", {}) or {}


def split_index_name(name, sources=KNOWN_SOURCES):
    """Classify a data-index name.

    Returns ``(team_esid, source)`` for an ESID-keyed name
    (``{team_esid}__{source}``), ``(None, source)`` for a legacy
    ``{slug}_{source}`` name, or ``(None, None)`` if the name matches no known
    source. Sources are checked longest-first so ``fb_insights_window`` wins
    over ``fb_insights``, and the ``__`` (ESID) form is checked before the
    single-underscore (legacy) form for each source.
    """
    for source in sorted(sources, key=len, reverse=True):
        if name.endswith(f"__{source}"):
            return name[: -len(f"__{source}")], source
        if name.endswith(f"_{source}"):
            return None, source
    return None, None


def build_team_index(es):
    """Return ``(team_by_esid, slug_to_esids)`` from one scan of de_asset_teams.

    ``team_by_esid``: ``{esid: _source}`` keyed by the team's ES ``_id`` AND,
    additionally, by its lowercased form. ES index names are forced lowercase
    (``build_data_index_name`` lowercases), and real team ESIDs are mixed-case
    (e.g. ``GfgudJ4B4wpbHOcfRenn``) — so the esid recovered from an index name
    is lowercase and a raw exact ``_id`` get would 404. Keying by the
    lowercased esid too lets the reverse join resolve it. (If two teams' ESIDs
    only differ in case ES would already have rejected one of their index
    names as a duplicate, so the lowercased key is unambiguous in practice.)

    ``slug_to_esids``: ``{sanitized_title: [esid, ...]}`` for legacy reverse
    lookup; a slug with >1 esid is ambiguous and the caller renders ``?``.
    """
    team_by_esid = {}
    slug_to_esids = {}
    resp = es.search(index="de_asset_teams", size=10000, body={"query": {"match_all": {}}})
    body = resp.body if hasattr(resp, "body") else resp
    for hit in body.get("hits", {}).get("hits", []):
        esid = hit.get("_id")
        src = hit.get("_source", {}) or {}
        team_by_esid[esid] = src
        if esid:
            team_by_esid[esid.lower()] = src  # reverse-join key (index names are lowercase)
        title = src.get("team_title", "")
        if title:
            slug = sanitize_team_title(title)
            slug_to_esids.setdefault(slug, []).append(esid)
    return team_by_esid, slug_to_esids


class _TitleResolver:
    """Caches org/workspace title lookups so repeated joins don't re-hit ES."""

    def __init__(self, es):
        self.es = es
        self._org = {}
        self._ws = {}

    def org_title(self, org_esid):
        if not org_esid:
            return None
        if org_esid not in self._org:
            try:
                self._org[org_esid] = _src(self.es.get(index="de_asset_organization", id=org_esid)).get("organization_title")
            except Exception:
                self._org[org_esid] = None
        return self._org[org_esid]

    def workspace_title(self, ws_esid):
        if not ws_esid:
            return None
        if ws_esid not in self._ws:
            try:
                self._ws[ws_esid] = _src(self.es.get(index="de_asset_workspace", id=ws_esid)).get("workspace_title")
            except Exception:
                self._ws[ws_esid] = None
        return self._ws[ws_esid]

    def resolve_team(self, team_src):
        """(team, org, workspace) titles from a de_asset_teams _source.

        ``workspace_esid`` is read directly off the team doc (it
        lives on the team, not via an org hop). ``organization_esid`` is a
        list; the first entry drives the ORG column.
        """
        team = team_src.get("team_title") or DASH
        org_esids = team_src.get("organization_esid", [])
        if isinstance(org_esids, str):
            org_esids = [org_esids]
        org = self.org_title(org_esids[0]) if org_esids else None
        ws = self.workspace_title(team_src.get("workspace_esid"))
        return team, (org or DASH), (ws or DASH)


def build_rows(es, cat_entries, team_by_esid, slug_to_esids, resolver=None):
    """Build display rows (WORKSPACE, ORG, TEAM, SOURCE, INDEX, DOCS, SIZE).

    ``cat_entries`` is the list of dicts from ``es.cat.indices(format="json",
    h="index,docs.count,store.size")``. Non-tenant and system (``.`` -prefixed)
    indices are skipped. Every tenant index produces exactly one row (LEFT
    JOIN — orphans keep ``DASH`` placeholders).
    """
    if resolver is None:
        resolver = _TitleResolver(es)
    rows = []
    for entry in cat_entries:
        name = entry.get("index", "")
        if not name or name.startswith("."):
            continue
        esid, source = split_index_name(name)
        if source is None:
            continue
        docs = str(entry.get("docs.count", DASH) or DASH)
        size = str(entry.get("store.size", DASH) or DASH)

        if esid is not None:
            team_src = team_by_esid.get(esid)
            if team_src is None:
                try:
                    team_src = _src(es.get(index="de_asset_teams", id=esid))
                except Exception:
                    team_src = None
            if team_src:
                team, org, ws = resolver.resolve_team(team_src)
            else:
                team, org, ws = DASH, DASH, DASH  # orphan ESID index — never dropped
        else:
            slug = name[: -len(f"_{source}")]
            candidates = slug_to_esids.get(slug, [])
            if len(candidates) == 1:
                team, org, ws = resolver.resolve_team(team_by_esid[candidates[0]])
            elif len(candidates) > 1:
                team = "? " + "|".join(sorted(candidates))
                org, ws = DASH, DASH
            else:
                team, org, ws = DASH, DASH, DASH  # orphan legacy index — never dropped

        rows.append((ws, org, team, source, name, docs, size))
    return rows


_HEADERS = ("WORKSPACE", "ORG", "TEAM", "SOURCE", "INDEX", "DOCS", "SIZE")


def render_table(rows):
    """Return the formatted table as a string (column-aligned)."""
    widths = [len(h) for h in _HEADERS]
    for r in rows:
        for i, cell in enumerate(r):
            widths[i] = max(widths[i], len(cell))
    fmt = "  ".join(f"{{:<{w}}}" for w in widths)
    lines = [fmt.format(*_HEADERS), "  ".join("-" * w for w in widths)]
    lines += [fmt.format(*r) for r in rows]
    lines.append(f"\n{len(rows)} tenant data index(es).")
    return "\n".join(lines)


def main(argv=None):
    """CLI entry point (invoked by bin/scripts/list-tenant-indices.sh via
    ``python3 -m lib.tenant_index_view``). Filters are read from argv as
    ``--workspace W`` / ``--org O`` / ``--team T`` (case-insensitive substring
    match on the resolved title). Kept as a module entry point — NOT a shell
    heredoc — so it survives terminals that mangle heredoc stdin."""
    import argparse
    import sys

    sys.path.insert(0, "/app/python")
    from lib.es_helpers import get_elasticsearch_client

    parser = argparse.ArgumentParser(description="Reverse-lookup tenant data indices → team/org/workspace.")
    parser.add_argument("--workspace", default="")
    parser.add_argument("--org", default="")
    parser.add_argument("--team", default="")
    args = parser.parse_args(argv)
    f_ws, f_org, f_team = args.workspace.strip().lower(), args.org.strip().lower(), args.team.strip().lower()

    es = get_elasticsearch_client()
    if es is None:
        print("ERROR: could not connect to Elasticsearch", file=sys.stderr)
        return 1

    try:
        team_by_esid, slug_to_esids = build_team_index(es)
    except Exception as e:
        print(f"WARNING: de_asset_teams scan failed: {e}", file=sys.stderr)
        team_by_esid, slug_to_esids = {}, {}

    try:
        cat = es.cat.indices(format="json", h="index,docs.count,store.size")
        cat = cat.body if hasattr(cat, "body") else cat
    except Exception as e:
        print(f"ERROR: _cat/indices failed: {e}", file=sys.stderr)
        return 1

    rows = build_rows(es, cat, team_by_esid, slug_to_esids)

    def _match(val, flt):
        return (not flt) or (flt in (val or "").lower())

    rows = [r for r in rows if _match(r[0], f_ws) and _match(r[1], f_org) and _match(r[2], f_team)]
    rows.sort(key=lambda r: (r[0], r[1], r[2], r[3]))
    print(render_table(rows))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
