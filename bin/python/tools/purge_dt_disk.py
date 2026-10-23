"""One-shot purge of Disciple.Tools person data already written to disk.

Before the DT importer wrote the UNFILTERED Disciple.Tools REST payload
(convert names, street addresses, phones, age, gender, faith status) to disk,
archived a fresh timestamped copy on every run, and geocoded the street itself,
caching street → coordinates. stops all of that going forward; this
removes what is already there:

* ``/app/data/disciple_tools/raw_imported/`` — every archived file is deleted
  (each one is an already-indexed full copy of a site's contacts / groups).
* ``/app/data/disciple_tools/raw/`` — a pending file still carrying raw REST
  ``posts`` is rewritten as minimised documents (``fetch.minimise_posts``, the
  same street-free mapping the importer now uses) so the next send still
  indexes it; an unreadable file is deleted; an already-minimised file is left.
* ``/app/data/geocoding_cache.json`` — every street-keyed ``addr|…`` entry is
  removed (``city|…`` / ``place|…`` entries are coarse and kept).
* ``/tmp/dt_geocode_cache.json`` (inside ``imports_container``) — deleted; it
  held earlier street keys. The next DT import's prewarm rebuilds it from
  location-grid names and ``meta.contact_place`` only.

Report-only by default: prints how many files / entries each step WOULD touch
and writes nothing. ``--apply`` performs the purge. Idempotent — a second
``--apply`` reports zero. Never prints a file's contents or a cache key, only
counts.

Usage::

    docker exec -w /app/python imports_container python3 -m tools.purge_dt_disk
    docker exec -w /app/python imports_container python3 -m tools.purge_dt_disk --apply
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import sys
from typing import Any, Callable

logging.basicConfig(level=logging.INFO, format="%(message)s")
logger = logging.getLogger(__name__)

DT_RAW_DIR = "/app/data/disciple_tools/raw"
DT_ARCHIVE_DIR = "/app/data/disciple_tools/raw_imported"
GEOCODE_CACHE_FILE = "/app/data/geocoding_cache.json"
LEGACY_DT_GEOCODE_CACHE = "/tmp/dt_geocode_cache.json"
STREET_KEY_PREFIX = "addr|"


def _files_under(root: str) -> list[str]:
    out: list[str] = []
    if not os.path.isdir(root):
        return out
    for dirpath, _dirs, files in os.walk(root):
        out.extend(os.path.join(dirpath, name) for name in files)
    return sorted(out)


def _atomic_write_json(path: str, data: Any) -> None:
    tmp = f"{path}.note_2.tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(data, fh, ensure_ascii=False)
    os.replace(tmp, path)


def purge_archive(archive_dir: str, *, apply: bool) -> dict[str, int]:
    files = _files_under(archive_dir)
    out = {"files": len(files), "deleted": 0, "errors": 0}
    logger.info("%-9s %s: %d archived file(s)", "deleting" if apply else "would_del", archive_dir, len(files))
    if apply:
        for path in files:
            try:
                os.remove(path)
                out["deleted"] += 1
            except OSError as exc:
                out["errors"] += 1
                logger.warning("could not remove a file under %s: %s", archive_dir, type(exc).__name__)
    return out


def minimise_raw(raw_dir: str, *, apply: bool, minimise: Callable[[list, str], list] | None = None) -> dict[str, int]:
    """Rewrite raw-``posts`` files as minimised ``docs``; delete unreadable ones."""
    out = {"files": 0, "raw_posts_files": 0, "minimised": 0, "unreadable": 0, "deleted": 0, "already_minimal": 0, "errors": 0}
    for path in _files_under(raw_dir):
        out["files"] += 1
        try:
            with open(path, "r", encoding="utf-8") as fh:
                payload = json.load(fh)
            if not isinstance(payload, dict):
                raise ValueError("not an object")
        except Exception:  # noqa: BLE001 — unreadable: nothing can use it, and it may hold person data
            out["unreadable"] += 1
            if apply:
                try:
                    os.remove(path)
                    out["deleted"] += 1
                except OSError as exc:
                    out["errors"] += 1
                    logger.warning("could not remove an unreadable raw file: %s", type(exc).__name__)
            continue
        if "posts" not in payload:
            out["already_minimal"] += 1
            continue
        out["raw_posts_files"] += 1
        if not apply:
            continue
        if minimise is None:
            from import_scripts.disciple_tools.fetch import minimise_posts as minimise  # type: ignore[no-redef]
        team_esid = str(payload.get("team_esid") or "")
        new_payload = {k: v for k, v in payload.items() if k != "posts"}
        new_payload["docs"] = minimise(payload.get("posts") or [], team_esid) if team_esid else []
        try:
            _atomic_write_json(path, new_payload)
            out["minimised"] += 1
        except OSError as exc:
            out["errors"] += 1
            logger.warning("could not rewrite a raw file: %s", type(exc).__name__)
    logger.info(
        "%-9s %s: %d file(s); %d carry raw REST posts, %d unreadable, %d already minimised",
        "fixing" if apply else "would_fix",
        raw_dir,
        out["files"],
        out["raw_posts_files"],
        out["unreadable"],
        out["already_minimal"],
    )
    return out


def strip_street_cache_keys(cache_file: str, *, apply: bool) -> dict[str, int]:
    out = {"entries": 0, "street_keys": 0, "removed": 0}
    if not os.path.exists(cache_file):
        logger.info("skip      %s: not present", cache_file)
        return out
    with open(cache_file, "r", encoding="utf-8") as fh:
        cache = json.load(fh)
    street = [k for k in cache if str(k).startswith(STREET_KEY_PREFIX)]
    out.update(entries=len(cache), street_keys=len(street))
    logger.info(
        "%-9s %s: %d street-keyed entr(ies) of %d", "removing" if apply else "would_del", cache_file, len(street), len(cache)
    )
    if apply and street:
        for k in street:
            cache.pop(k, None)
        _atomic_write_json(cache_file, cache)
        out["removed"] = len(street)
    return out


def delete_legacy_cache(path: str, *, apply: bool) -> dict[str, int]:
    present = os.path.exists(path)
    out = {"present": int(present), "deleted": 0}
    logger.info("%-9s %s: %s", "deleting" if apply else "would_del", path, "present" if present else "not present")
    if apply and present:
        os.remove(path)
        out["deleted"] = 1
    return out


def run(
    *,
    apply: bool,
    raw_dir: str = DT_RAW_DIR,
    archive_dir: str = DT_ARCHIVE_DIR,
    geocode_cache: str = GEOCODE_CACHE_FILE,
    legacy_cache: str = LEGACY_DT_GEOCODE_CACHE,
    minimise: Callable[[list, str], list] | None = None,
) -> dict[str, Any]:
    result = {
        "raw_imported": purge_archive(archive_dir, apply=apply),
        "raw": minimise_raw(raw_dir, apply=apply, minimise=minimise),
        "geocoding_cache": strip_street_cache_keys(geocode_cache, apply=apply),
        "legacy_dt_geocode_cache": delete_legacy_cache(legacy_cache, apply=apply),
    }
    logger.info("done%s — %s", "" if apply else " (report only, nothing changed)", result)
    return result


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--apply", action="store_true", help="perform the purge (default: report only)")
    args = parser.parse_args(argv)
    run(apply=args.apply)
    return 0


if __name__ == "__main__":
    sys.exit(main())
