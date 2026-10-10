"""One-shot migration: retire the membership indices, finish the rename.

 P3b makes scoped roles the single source of truth and renames
``workspace_member`` → ``workspace_viewer``. The application code already
(a) reads "belongs to" from the role indices, (b) writes ``is_primary`` on the
workspace role doc, and (c) stops writing membership docs. This tool migrates
the EXISTING prod data to match, in three idempotent passes:

1. **Rename role docs** — every ``de_authz_user_workspace_role`` doc with
   ``role_esid == "workspace_member"`` is rewritten to ``workspace_viewer``.
   ``role_esid`` is part of the deterministic doc id
   (``{username}:{workspace_esid}:{role_esid}``), so the rewrite is a
   delete-old + create-new pair, carrying every other field forward.

2. **Backfill is_primary** — any ``de_authz_user_workspace_role`` doc written
   before P3a (no ``is_primary`` field) inherits the flag from the matching
   ``de_authz_workspace_membership`` doc (``{username}:{workspace_esid}``)
   before that membership doc is dropped. Docs that already have the field
   are left alone. Missing membership → default ``True`` for the user's
   lowest-``created_at`` workspace role, else ``False`` (mirrors the old
   membership sort tiebreak).

3. **Drop membership docs** — delete every doc in
   ``de_authz_workspace_membership`` / ``de_authz_org_membership`` /
   ``de_authz_team_membership``. The indices themselves are left in place
   (empty) so concurrent code that still names them degrades to "no rows"
   rather than index_not_found.

**Dry-run by default.** ``--apply`` writes. Before any mutation in ``--apply``
mode the tool snapshots the affected role + membership indices (
discipline) so a bad run is recoverable. Each pass is idempotent — a second
run produces zero writes.

DEPLOY ORDER (important): run this on a cluster BEFORE deploying the code that
drops ``workspace_member`` from the seeder, otherwise any un-migrated
``workspace_member`` role doc would fail ``_validate_role_for_tier``. Pass 1
must complete first.

Usage::

    # Dry-run everything (default).
    docker exec -w /app/python imports_container python3 \\
        -m tools.retire_workspace_membership

    # Apply all passes (snapshots first).
    docker exec -w /app/python imports_container python3 \\
        -m tools.retire_workspace_membership --apply

    # Apply a single pass.
    docker exec -w /app/python imports_container python3 \\
        -m tools.retire_workspace_membership --apply --pass rename
"""

from __future__ import annotations

import argparse
import logging
import sys
from pathlib import Path

_HERE = Path(__file__).resolve().parent
for _candidate in (
    _HERE.parents[1],
    _HERE.parents[2] / "bin" / "python",
    Path("/app/python"),
):
    if (_candidate / "asset_management" / "__init__.py").exists():
        if str(_candidate) not in sys.path:
            sys.path.insert(0, str(_candidate))
        break

from lib.es_helpers import get_elasticsearch_client  # noqa: E402

WORKSPACE_ROLE_INDEX = "de_authz_user_workspace_role"
WORKSPACE_MEMBERSHIP_INDEX = "de_authz_workspace_membership"
ORG_MEMBERSHIP_INDEX = "de_authz_org_membership"
TEAM_MEMBERSHIP_INDEX = "de_authz_team_membership"
_MEMBERSHIP_INDICES = (WORKSPACE_MEMBERSHIP_INDEX, ORG_MEMBERSHIP_INDEX, TEAM_MEMBERSHIP_INDEX)

LEGACY_ROLE = "workspace_member"
CANONICAL_ROLE = "workspace_viewer"

PASSES = ("rename", "is-primary", "drop-membership", "all")

# Preferred snapshot repo on prod (the SLM `daily_repo`). Staging/test
# clusters don't have it — they use `migration_repo` (the repo the
# index migrations snapshot into). Rather than hard-code a single name (which
# made --apply permanently un-runnable on staging: daily_repo 404s there), the
# repo is resolved at runtime: an explicit --snapshot-repo wins, else the first
# of these preferred names that exists, else any existing repo. A destructive
# --apply still REFUSES to run if no repo exists at all (no silent skip of the
# safety snapshot).
_PREFERRED_SNAPSHOT_REPOS = ("daily_repo", "migration_repo")


def _resolve_snapshot_repo(es, override: str | None) -> str:
    """Pick the snapshot repo to use, or raise if none is available.

    Order: explicit override (must exist) → first existing preferred repo →
    any existing repo. Raises RuntimeError when the cluster has no snapshot
    repository, so a destructive --apply can never proceed without a snapshot
    target.
    """
    try:
        existing = list((es.snapshot.get_repository() or {}).keys())
    except Exception as e:  # pragma: no cover - defensive; surfaced to caller
        raise RuntimeError(f"could not list snapshot repositories: {e}") from e
    if override:
        if override not in existing:
            raise RuntimeError(f"--snapshot-repo {override!r} not found; available: {existing or 'none'}")
        return override
    for name in _PREFERRED_SNAPSHOT_REPOS:
        if name in existing:
            return name
    if existing:
        return existing[0]
    raise RuntimeError(
        "no snapshot repository exists on this cluster — register one (or pass "
        "--snapshot-repo) before running --apply; refusing to mutate without a "
        "pre-snapshot safety net."
    )


def _scan(es, index: str, query: dict | None = None):
    """Yield every doc in ``index`` via the scroll API; empty on missing index."""
    body = {"query": query or {"match_all": {}}, "size": 1000}
    try:
        resp = es.search(index=index, body=body, scroll="2m")
    except Exception as e:
        if "index_not_found" in str(e).lower() or "404" in str(e):
            return
        raise
    scroll_id = resp.get("_scroll_id")
    hits = resp.get("hits", {}).get("hits", [])
    while hits:
        for h in hits:
            yield h
        resp = es.scroll(scroll_id=scroll_id, scroll="2m")
        scroll_id = resp.get("_scroll_id")
        hits = resp.get("hits", {}).get("hits", [])


def _snapshot(es, *, apply: bool, repo_override: str | None = None) -> None:
    """Snapshot the affected indices before mutation.

    The repo is resolved at runtime (daily_repo on prod, migration_repo on
    staging, or an explicit ``repo_override``). In dry-run we still resolve it
    so the operator sees the real target — and a missing repo surfaces as a
    dry-run error rather than a surprise mid-apply.
    """
    indices = ",".join((WORKSPACE_ROLE_INDEX, *_MEMBERSHIP_INDICES))
    name = "retire-membership-presnapshot"
    repo = _resolve_snapshot_repo(es, repo_override)
    if not apply:
        logging.info("[dry-run] would snapshot %s -> %s/%s", indices, repo, name)
        return
    try:
        es.snapshot.create(
            repository=repo,
            snapshot=name,
            body={"indices": indices, "include_global_state": False},
            wait_for_completion=True,
        )
        logging.info("snapshot %s/%s created for %s", repo, name, indices)
    except Exception as e:
        # A pre-existing snapshot of the same name (idempotent re-run) is fine.
        if "snapshot_name_already_exists" in str(e).lower() or "invalid_snapshot_name" in str(e).lower():
            logging.info("snapshot %s already exists — reusing", name)
            return
        raise


def pass_rename(es, *, apply: bool, limit: int | None) -> dict:
    """Pass 1 — rewrite workspace_member role docs to workspace_viewer."""
    summary = {"scanned": 0, "to_rename": 0, "renamed": 0, "errors": 0}
    shown = 0
    for hit in _scan(es, WORKSPACE_ROLE_INDEX, {"term": {"role_esid": LEGACY_ROLE}}):
        summary["scanned"] += 1
        src = hit.get("_source", {}) or {}
        username = src.get("username")
        ws = src.get("workspace_esid")
        if not username or not ws:
            continue
        summary["to_rename"] += 1
        new_id = f"{username}:{ws}:{CANONICAL_ROLE}"
        new_doc = {**src, "role_esid": CANONICAL_ROLE}
        if not apply:
            if limit is None or shown < limit:
                logging.info("[dry-run] would rename %s -> %s", hit["_id"], new_id)
                shown += 1
            continue
        try:
            es.index(index=WORKSPACE_ROLE_INDEX, id=new_id, document=new_doc, op_type="create")
            es.delete(index=WORKSPACE_ROLE_INDEX, id=hit["_id"])
            summary["renamed"] += 1
        except Exception as e:
            if "version_conflict" in str(e).lower():
                # viewer doc already exists (idempotent re-run) — just drop the old.
                try:
                    es.delete(index=WORKSPACE_ROLE_INDEX, id=hit["_id"])
                    summary["renamed"] += 1
                except Exception:
                    summary["errors"] += 1
            else:
                logging.warning("rename failed for %s: %s", hit["_id"], e)
                summary["errors"] += 1
    if apply:
        es.indices.refresh(index=WORKSPACE_ROLE_INDEX)
    return summary


def pass_is_primary(es, *, apply: bool, limit: int | None) -> dict:
    """Pass 2 — backfill is_primary onto role docs missing it, from membership."""
    summary = {"scanned": 0, "missing_field": 0, "backfilled": 0, "errors": 0}
    # Map {username:workspace_esid} -> is_primary from the membership index.
    membership_primary: dict[str, bool] = {}
    for hit in _scan(es, WORKSPACE_MEMBERSHIP_INDEX):
        src = hit.get("_source", {}) or {}
        u, ws = src.get("username"), src.get("workspace_esid")
        if u and ws:
            membership_primary[f"{u}:{ws}"] = bool(src.get("is_primary", False))

    # Group role docs missing is_primary by user, to pick a fallback primary.
    missing: list[tuple[str, dict]] = []
    by_user: dict[str, list[dict]] = {}
    for hit in _scan(es, WORKSPACE_ROLE_INDEX):
        summary["scanned"] += 1
        src = hit.get("_source", {}) or {}
        if "is_primary" in src:
            continue
        summary["missing_field"] += 1
        missing.append((hit["_id"], src))
        by_user.setdefault(src.get("username", ""), []).append(src)

    # Fallback primary per user: lowest created_at workspace role.
    fallback_primary_id: dict[str, str] = {}
    for user, docs in by_user.items():
        if not user:
            continue
        chosen = min(docs, key=lambda d: d.get("created_at") or "")
        fallback_primary_id[user] = f"{chosen.get('username')}:{chosen.get('workspace_esid')}"

    shown = 0
    for doc_id, src in missing:
        u, ws = src.get("username"), src.get("workspace_esid")
        key = f"{u}:{ws}"
        if key in membership_primary:
            is_primary = membership_primary[key]
        else:
            is_primary = fallback_primary_id.get(u or "") == key
        if not apply:
            if limit is None or shown < limit:
                logging.info("[dry-run] would set is_primary=%s on %s", is_primary, doc_id)
                shown += 1
            continue
        try:
            es.update(index=WORKSPACE_ROLE_INDEX, id=doc_id, body={"doc": {"is_primary": is_primary}})
            summary["backfilled"] += 1
        except Exception as e:
            logging.warning("is_primary backfill failed for %s: %s", doc_id, e)
            summary["errors"] += 1
    if apply:
        es.indices.refresh(index=WORKSPACE_ROLE_INDEX)
    return summary


def pass_drop_membership(es, *, apply: bool, limit: int | None) -> dict:
    """Pass 3 — delete all docs in the three membership indices."""
    del limit  # whole-index delete; no per-doc preview
    summary: dict[str, int] = {}
    for index in _MEMBERSHIP_INDICES:
        if not apply:
            try:
                cnt = es.count(index=index, body={"query": {"match_all": {}}}).get("count", 0)
            except Exception:
                cnt = 0
            logging.info("[dry-run] would delete %d doc(s) from %s", cnt, index)
            summary[index] = 0
            continue
        try:
            resp = es.delete_by_query(
                index=index,
                body={"query": {"match_all": {}}},
                refresh=True,
                conflicts="proceed",
            )
            summary[index] = int(resp.get("deleted", 0))
            logging.info("deleted %d doc(s) from %s", summary[index], index)
        except Exception as e:
            if "index_not_found" in str(e).lower() or "404" in str(e):
                summary[index] = 0
                continue
            logging.warning("drop-membership failed for %s: %s", index, e)
            summary[index] = 0
    return summary


def run(*, apply: bool, which: str, limit: int | None, snapshot_repo: str | None = None) -> dict:
    es = get_elasticsearch_client()
    if es is None:
        logging.error("retire_workspace_membership: no ES client")
        return {}

    # Resolve + (on apply) take the pre-mutation snapshot. Run the resolution
    # in dry-run too so a missing/misnamed repo is caught before --apply
    # instead of aborting mid-run (the daily_repo-only bug that blocked
    # staging).
    _snapshot(es, apply=apply, repo_override=snapshot_repo)

    report: dict[str, dict] = {}
    # Order matters: rename + is_primary backfill BEFORE dropping membership
    # docs (pass 2 reads membership to source is_primary).
    if which in ("rename", "all"):
        report["rename"] = pass_rename(es, apply=apply, limit=limit)
    if which in ("is-primary", "all"):
        report["is_primary"] = pass_is_primary(es, apply=apply, limit=limit)
    if which in ("drop-membership", "all"):
        report["drop_membership"] = pass_drop_membership(es, apply=apply, limit=limit)
    return report


def main() -> int:
    parser = argparse.ArgumentParser(description="retire membership indices + finish the workspace_viewer rename.")
    parser.add_argument("--apply", action="store_true", help="write changes (default: dry-run)")
    parser.add_argument("--pass", dest="which", choices=PASSES, default="all", help="which pass to run")
    parser.add_argument("--limit", type=int, default=20, help="max dry-run examples per pass (full counts always logged)")
    parser.add_argument(
        "--snapshot-repo",
        default=None,
        help="snapshot repo to use (default: auto — daily_repo on prod, "
        "migration_repo on staging, else the first existing repo)",
    )
    args = parser.parse_args()

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    mode = "APPLY" if args.apply else "DRY-RUN"
    logging.info("retire_workspace_membership: %s pass=%s", mode, args.which)
    report = run(apply=args.apply, which=args.which, limit=args.limit, snapshot_repo=args.snapshot_repo)
    logging.info("retire_workspace_membership summary: %s", report)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
