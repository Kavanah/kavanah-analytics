"""
Snapshot Service — wraps the Elasticsearch ``_snapshot`` API.

Provides:
- Listing registered snapshot repositories.
- Listing snapshots within a repository.
- Inspecting a single snapshot to see which indices it contains.
- Restoring a user-selected subset of indices from a snapshot.

Restore semantics:
- ES will not restore over an open index. Indices that already exist and are
  also in the user's selection are deleted first, then restored.
- Indices that are in the snapshot but not in the user's selection are skipped.
- Indices that are in the user's selection but not in the snapshot are
  rejected up front (caller must filter).

This service intentionally does NOT touch the filesystem. It uses the ES
snapshot mechanism, which restores indices in place — no docker stop, no
data-dir swap. That makes it the preferred path for cross-environment data
moves and for cherry-picking specific indices back from a known-good point.
"""

import logging
import os
import re
import sys

sys.path.insert(
    0,
    os.path.dirname(os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))),
)

from lib.es_helpers import get_elasticsearch_client
from lib.errors import InvalidRequestError, RequestedItemNotFoundError, UserFacingError


# Identifiers ES will accept for repos / snapshots / indices. Whitelisted to
# block path traversal and accidental wildcard injection in URL components.
_SAFE_NAME_RE = re.compile(r"^[a-zA-Z0-9._\-]+$")


class SnapshotServiceError(UserFacingError):
    """Base error for snapshot operations."""


class SnapshotService:

    @staticmethod
    def _validate_name(name: str, label: str) -> str:
        if not isinstance(name, str) or not name:
            raise InvalidRequestError(f"{label} is required")
        if not _SAFE_NAME_RE.match(name):
            raise InvalidRequestError(f"Invalid {label}: {name!r}")
        return name

    # ------------------------------------------------------------------ list

    @staticmethod
    def list_repos() -> list[dict]:
        """Return all registered snapshot repositories.

        Each entry: ``{"name": str, "type": str, "settings": dict}``.
        """
        es = get_elasticsearch_client()
        if es is None:
            raise SnapshotServiceError("Could not connect to Elasticsearch")

        response = es.snapshot.get_repository(name="_all")
        body = response.body if hasattr(response, "body") else response
        repos: list[dict] = []
        for repo_name, info in (body or {}).items():
            repos.append(
                {
                    "name": repo_name,
                    "type": info.get("type", ""),
                    "settings": info.get("settings", {}),
                }
            )
        repos.sort(key=lambda r: r["name"])
        return repos

    @staticmethod
    def list_snapshots(repo: str) -> dict:
        """Return all readable snapshots in ``repo``, newest first.

        A repository's index can reference snapshots whose data files have
        been deleted or corrupted on disk. The bulk ``_all`` fetch fails
        wholesale in that case. We fall back to a name-only listing and
        enrich each entry individually, skipping ones that fail to load
        and recording them in ``warnings``.

        Returns:
            ``{"snapshots": [...], "warnings": [str, ...]}``.
        """
        SnapshotService._validate_name(repo, "repository name")
        es = get_elasticsearch_client()
        if es is None:
            raise SnapshotServiceError("Could not connect to Elasticsearch")

        warnings: list[str] = []

        # First try the cheap bulk fetch.
        try:
            response = es.snapshot.get(repository=repo, snapshot="_all")
            body = response.body if hasattr(response, "body") else response
            return {
                "snapshots": SnapshotService._format_snapshot_list(body or {}),
                "warnings": warnings,
            }
        except Exception as bulk_exc:
            logging.warning(
                f"SnapshotService: bulk list for {repo} failed ({bulk_exc}); " f"falling back to per-snapshot fetch"
            )

        # Fallback: name-only listing, then enrich per-snapshot.
        try:
            response = es.snapshot.get(repository=repo, snapshot="_all", verbose=False)
            body = response.body if hasattr(response, "body") else response
        except Exception as exc:
            logging.warning("SnapshotService: name-only list for %s failed: %s", repo, exc)
            raise SnapshotServiceError(
                "Could not list snapshots in this repository. Check that it is registered and reachable."
            ) from exc

        names = [s.get("snapshot") for s in (body or {}).get("snapshots", []) if s.get("snapshot")]

        out: list[dict] = []
        for name in names:
            try:
                detail_response = es.snapshot.get(repository=repo, snapshot=name)
                detail_body = detail_response.body if hasattr(detail_response, "body") else detail_response
                snaps = (detail_body or {}).get("snapshots", [])
                if not snaps:
                    warnings.append(f"{name}: not returned by ES")
                    continue
                out.append(SnapshotService._format_snapshot(snaps[0]))
            except Exception as exc:
                logging.warning(f"SnapshotService: skipping broken snapshot {name}: {exc}")
                warnings.append(f"{name}: {type(exc).__name__}")

        out.sort(key=lambda s: s.get("start_time", ""), reverse=True)
        return {"snapshots": out, "warnings": warnings}

    @staticmethod
    def _format_snapshot(snap: dict) -> dict:
        indices = snap.get("indices", [])
        return {
            "snapshot": snap.get("snapshot"),
            "state": snap.get("state", ""),
            "start_time": snap.get("start_time", ""),
            "end_time": snap.get("end_time", ""),
            "indices_count": len(indices),
            "indices": indices,
        }

    @staticmethod
    def _format_snapshot_list(body: dict) -> list[dict]:
        out = [SnapshotService._format_snapshot(s) for s in body.get("snapshots", [])]
        out.sort(key=lambda s: s.get("start_time", ""), reverse=True)
        return out

    # ----------------------------------------------------------------- inspect

    @staticmethod
    def inspect_snapshot(repo: str, snapshot: str) -> dict:
        """Return full details for one snapshot, including conflict info.

        Returns:
            {
              "snapshot": str,
              "state": str,
              "start_time": str,
              "end_time": str,
              "indices": [
                  {"name": str, "exists_in_cluster": bool},
                  ...
              ],
            }
        """
        SnapshotService._validate_name(repo, "repository name")
        SnapshotService._validate_name(snapshot, "snapshot name")
        es = get_elasticsearch_client()
        if es is None:
            raise SnapshotServiceError("Could not connect to Elasticsearch")

        response = es.snapshot.get(repository=repo, snapshot=snapshot)
        body = response.body if hasattr(response, "body") else response
        snaps = (body or {}).get("snapshots", [])
        if not snaps:
            raise RequestedItemNotFoundError(f"Snapshot not found: {snapshot}")
        snap = snaps[0]

        snapshot_indices = snap.get("indices", [])
        existing = SnapshotService._existing_indices(es, snapshot_indices)

        indices_info = [{"name": idx, "exists_in_cluster": idx in existing} for idx in sorted(snapshot_indices)]

        return {
            "snapshot": snap.get("snapshot"),
            "state": snap.get("state", ""),
            "start_time": snap.get("start_time", ""),
            "end_time": snap.get("end_time", ""),
            "indices": indices_info,
        }

    @staticmethod
    def _existing_indices(es, candidates: list[str]) -> set[str]:
        """Return the subset of ``candidates`` that already exist in the cluster.

        Fetches all cluster indices once and intersects locally — passing a
        long comma-separated index list to ``cat.indices`` either truncates
        on ES's URL length cap or 404s on the first missing name, both of
        which made every snapshot index look "new" when none were missing.
        """
        if not candidates:
            return set()
        try:
            response = es.cat.indices(h="index", expand_wildcards="open,closed")
            text = response.body if hasattr(response, "body") else response
            cluster_indices = {line.strip() for line in str(text).splitlines() if line.strip()}
        except Exception as exc:
            logging.warning(f"SnapshotService: could not fetch cluster indices: {exc}")
            return set()
        return cluster_indices.intersection(candidates)

    # ----------------------------------------------------------------- restore

    @staticmethod
    def restore_indices(
        repo: str,
        snapshot: str,
        indices: list[str],
        replace_existing: bool = False,
    ) -> dict:
        """Restore selected indices from a snapshot.

        Args:
            repo: snapshot repository name.
            snapshot: snapshot name within the repo.
            indices: list of index names to restore. Must all be present in
                the snapshot. Empty list is rejected.
            replace_existing: if True, indices that already exist in the
                cluster are deleted before restore. If False and any selected
                index already exists, the call fails up front with a
                ``ValueError`` listing the conflicts.

        Returns:
            ``{"restored": [...], "deleted_before_restore": [...], "snapshot_state": str}``.
        """
        SnapshotService._validate_name(repo, "repository name")
        SnapshotService._validate_name(snapshot, "snapshot name")

        if not isinstance(indices, list) or not indices:
            raise InvalidRequestError("indices must be a non-empty list")
        for idx in indices:
            SnapshotService._validate_name(idx, "index name")

        es = get_elasticsearch_client()
        if es is None:
            raise SnapshotServiceError("Could not connect to Elasticsearch")

        # Verify the snapshot exists and contains every requested index.
        response = es.snapshot.get(repository=repo, snapshot=snapshot)
        body = response.body if hasattr(response, "body") else response
        snaps = (body or {}).get("snapshots", [])
        if not snaps:
            raise RequestedItemNotFoundError(f"Snapshot not found: {snapshot}")
        snapshot_indices = set(snaps[0].get("indices", []))

        missing = [i for i in indices if i not in snapshot_indices]
        if missing:
            raise InvalidRequestError(f"Indices not in snapshot {snapshot}: {', '.join(sorted(missing))}")

        # Conflict detection.
        existing = SnapshotService._existing_indices(es, indices)
        deleted: list[str] = []
        if existing:
            if not replace_existing:
                raise InvalidRequestError(
                    "Indices already exist in cluster (use replace_existing=true to delete and restore): "
                    + ", ".join(sorted(existing))
                )
            es.indices.delete(index=",".join(sorted(existing)), ignore_unavailable=True)
            deleted = sorted(existing)
            logging.info(f"SnapshotService: deleted {len(deleted)} existing indices before restore")

        # Run the restore. wait_for_completion=true so the API returns when done.
        es.snapshot.restore(
            repository=repo,
            snapshot=snapshot,
            body={
                "indices": ",".join(indices),
                "ignore_unavailable": False,
                "include_global_state": False,
                "include_aliases": True,
            },
            wait_for_completion=True,
        )

        return {
            "restored": sorted(indices),
            "deleted_before_restore": deleted,
            "snapshot_state": snaps[0].get("state", ""),
        }
