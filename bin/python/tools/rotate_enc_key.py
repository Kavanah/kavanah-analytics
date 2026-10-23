"""Re-encrypt every Fernet ciphertext under the current primary key.

Rotation of ``ENC_KEY`` is a three-state process; this tool does the middle step:

1. **Stage** — the new key becomes ``/km/<env>/deam/enc-key`` (primary) and the
   old one moves to ``/km/<env>/deam/enc-key-previous``; the secrets wrapper
   recreates ``imports_container`` with ``ENC_KEY`` + ``ENC_KEY_PREVIOUS``. From
   here every read still works (``lib.encryption.decrypt`` tries both keys) and
   every new write already uses the new key.
2. **Re-encrypt (this tool)** — walk every document that can hold a ciphertext,
   find Fernet-shaped strings, and rewrite the ones still under the previous key
   so they are under the primary. Also rewrites the ``*.enc`` files under
   ``/app/reporting_configs`` and ``/app/data/google_analytics/credentials``.
3. **Retire** — once this tool reports ``stale=0``, delete the ``enc-key-previous``
   SSM param and re-run the wrapper; the old key is then held nowhere but SSM's
   parameter history (needed only to read a pre-rotation ES snapshot).

Finding ciphertexts: rather than trusting a hand-kept list of field names — the
class of bug that orphans a token silently — the tool walks each ``_source``
recursively and treats any string with the Fernet token shape
(``lib.encryption.looks_like_fernet_token``) as a candidate. ``INVENTORY`` below
still names the fields we *know* about; a hit outside it is rotated too and
logged as ``uninventoried`` so the list gets updated.

Each candidate is classified by trying the keys, never by reading the plaintext:

* ``current``        — decrypts under the primary key; untouched.
* ``stale``          — decrypts only under a previous key; rotated with ``--apply``.
* ``undecryptable``  — decrypts under no held key; left alone and counted. These
  are pre-existing orphans (a key lost) and are not made worse.

Writes are whole-document re-indexes guarded by ``if_seq_no`` /
``if_primary_term``: a document changed between read and write is skipped as a
``conflict`` and picked up by the next run. **Dry-run by default**; ``--apply``
writes. Idempotent: a second ``--apply`` finds ``stale=0``.

Nothing this tool prints is a key or a plaintext: output is index, ``_id``, JSON
path and counts only.

Usage (inside imports_container)::

    docker exec -w /app/python imports_container python3 -m tools.rotate_enc_key            # dry-run
    docker exec -w /app/python imports_container python3 -m tools.rotate_enc_key --status   # + key source / residual checks
    docker exec -w /app/python imports_container python3 -m tools.rotate_enc_key --apply
    docker exec -w /app/python imports_container python3 -m tools.rotate_enc_key --apply --index-pattern 'de_asset_*'

Exit status: 0 when nothing is left to do (or dry-run completed), 2 when
``--apply`` left stale values behind (conflicts, or a key that could not
decrypt), 1 on a hard error (no ES, no key).
"""

from __future__ import annotations

import argparse
import logging
import os
import sys
from dataclasses import dataclass, field
from typing import Any, Iterator

logging.basicConfig(level=logging.INFO, format="%(message)s")
logger = logging.getLogger(__name__)

# Index → fields known to hold Fernet ciphertexts. Documentation + drift alarm;
# the scanner does not depend on it (see module docstring).
INVENTORY: dict[str, tuple[str, ...]] = {
    "de_connection": ("oauth_token_enc", "pending_reauth_token_enc"),
    "de_asset_fb_apps": ("fb_app_secret", "fb_app_token"),
    "de_asset_fb_app_users": ("fb_app_user_token",),
    "de_asset_echo_tenants": ("echo_tenant_api_key",),
    "de_asset_chatwoot_accounts": ("chatwoot_api_token",),
    "de_asset_youtube_accounts": ("youtube_account_token",),
    "de_asset_ga4_accounts": ("ga4_account_token",),
    "de_asset_user_mfa": ("totp_secret_enc",),
    "de_pending_registrations": ("encrypted_password",),
    # publishes[].publish_token_enc (display copy of a public report token).
    "de_asset_echarts_dashboards": ("publishes",),
}

# Directories whose ``*.enc`` files are Fernet tokens written by
# lib.encryption.encrypt_json_file (service accounts, OAuth client files).
ENC_DIRS: tuple[str, ...] = (
    "/app/reporting_configs",
    "/app/data/google_analytics/credentials",
)

MASTER_CONFIG_PATH = "/app/reporting_configs/master-config.yaml"

PAGE_SIZE = 500
SCROLL_TTL = "5m"


# ---------------------------------------------------------------------------
# Finding and classifying ciphertexts
# ---------------------------------------------------------------------------

Path = tuple[Any, ...]  # keys (str) and list indexes (int) from the doc root


def find_tokens(value: Any, _path: Path = ()) -> Iterator[tuple[Path, str]]:
    """Yield ``(path, token)`` for every Fernet-shaped string inside ``value``."""
    from lib.encryption import looks_like_fernet_token

    if isinstance(value, dict):
        for k, v in value.items():
            yield from find_tokens(v, _path + (k,))
    elif isinstance(value, list):
        for i, v in enumerate(value):
            yield from find_tokens(v, _path + (i,))
    elif looks_like_fernet_token(value):
        yield _path, value


def set_at(doc: Any, path: Path, new_value: str) -> None:
    """Replace the value at ``path`` inside ``doc`` in place."""
    cur = doc
    for key in path[:-1]:
        cur = cur[key]
    cur[path[-1]] = new_value


Ring = tuple[Any, Any]  # (primary Fernet, MultiFernet) — see lib.encryption.key_ring


def _ring(ring: Ring | None) -> Ring:
    if ring is not None:
        return ring
    from lib.encryption import key_ring

    return key_ring()


def classify(token: str, ring: Ring | None = None) -> str:
    """``current`` / ``stale`` / ``undecryptable`` — by key, never by plaintext.

    ``ring`` is the per-run ``(primary, multi)`` pair from ``key_ring()``; built
    once by ``run()`` so a scan of N tokens costs N decrypts, not 2N Fernet
    constructions plus 2N environment reads (review).
    """
    from cryptography.fernet import InvalidToken

    primary, multi = _ring(ring)
    data = token.encode("utf-8")
    try:
        primary.decrypt(data)
        return "current"
    except InvalidToken:
        pass
    try:
        multi.decrypt(data)
    except InvalidToken:
        return "undecryptable"
    return "stale"


def path_str(path: Path) -> str:
    return ".".join(str(p) for p in path)


# ---------------------------------------------------------------------------
# Reports
# ---------------------------------------------------------------------------


@dataclass
class IndexReport:
    index: str
    docs: int = 0
    current: int = 0
    stale: int = 0
    undecryptable: int = 0
    rotated: int = 0
    conflicts: int = 0
    uninventoried: list[str] = field(default_factory=list)


@dataclass
class FileReport:
    scanned: int = 0
    current: int = 0
    stale: int = 0
    undecryptable: int = 0
    rotated: int = 0


# ---------------------------------------------------------------------------
# Elasticsearch side
# ---------------------------------------------------------------------------


def resolve_indices(es: Any, explicit: list[str] | None, pattern: str | None) -> list[str]:
    """Indices to scan: ``--indices`` verbatim, else INVENTORY keys that exist,
    plus everything matching ``--index-pattern``."""
    if explicit:
        return list(explicit)
    names: list[str] = []
    for name in INVENTORY:
        if es.indices.exists(index=name):
            names.append(name)
        else:
            logger.info("skip %-32s (index does not exist here)", name)
    if pattern:
        for name in sorted(es.indices.get(index=pattern).keys()):
            if name not in names:
                names.append(name)
    return names


def iter_hits(es: Any, index: str) -> Iterator[dict[str, Any]]:
    """Scroll every document of ``index`` with its seq_no / primary_term."""
    resp = es.search(
        index=index,
        query={"match_all": {}},
        size=PAGE_SIZE,
        scroll=SCROLL_TTL,
        seq_no_primary_term=True,
    )
    scroll_id = resp.get("_scroll_id")
    try:
        while True:
            hits = resp.get("hits", {}).get("hits", [])
            if not hits:
                return
            yield from hits
            if scroll_id is None:
                return
            resp = es.scroll(scroll_id=scroll_id, scroll=SCROLL_TTL)
            scroll_id = resp.get("_scroll_id", scroll_id)
    finally:
        if scroll_id is not None:
            try:
                es.clear_scroll(scroll_id=scroll_id)
            except Exception:  # noqa: BLE001 — best-effort cleanup of a server-side cursor
                pass


def rotate_index(es: Any, index: str, *, apply: bool, ring: Ring | None = None) -> IndexReport:
    """Scan one index; rotate its stale ciphertexts when ``apply``.

    Re-indexes the WHOLE document (``es.index`` with the read ``_source`` and the
    seq_no / primary_term guard), so it relies on ``_source`` being complete: no
    inventoried index may configure ``_source.excludes`` or disable ``_source``
    (true for every index in ``INVENTORY`` today; a partial source would be
    written back as the full document and silently drop the excluded fields).
    """
    from elasticsearch import ConflictError

    _primary, multi = _ring(ring)
    report = IndexReport(index=index)
    known = set(INVENTORY.get(index, ()))
    for hit in iter_hits(es, index):
        report.docs += 1
        source = hit.get("_source") or {}
        stale_paths: list[Path] = []
        for path, token in find_tokens(source):
            top = str(path[0]) if path else ""
            if top not in known:
                label = path_str(path)
                if label not in report.uninventoried:
                    report.uninventoried.append(label)
            state = classify(token, (_primary, multi))
            if state == "current":
                report.current += 1
            elif state == "undecryptable":
                report.undecryptable += 1
                logger.warning(
                    "undecryptable %s/%s %s (no held key decrypts it; left alone)", index, hit.get("_id"), path_str(path)
                )
            else:
                report.stale += 1
                stale_paths.append(path)
        if not stale_paths:
            continue
        if not apply:
            logger.info("would rotate %s/%s [%s]", index, hit.get("_id"), ", ".join(path_str(p) for p in stale_paths))
            continue
        for path in stale_paths:
            cur = source
            for key in path:
                cur = cur[key]
            set_at(source, path, multi.rotate(cur.encode("utf-8")).decode("utf-8"))
        try:
            es.index(
                index=index,
                id=hit["_id"],
                document=source,
                if_seq_no=hit["_seq_no"],
                if_primary_term=hit["_primary_term"],
            )
        except ConflictError:
            report.conflicts += 1
            logger.warning("conflict %s/%s changed between read and write — re-run to pick it up", index, hit.get("_id"))
            continue
        report.rotated += len(stale_paths)
        logger.info("rotated %s/%s [%s]", index, hit.get("_id"), ", ".join(path_str(p) for p in stale_paths))
    return report


# ---------------------------------------------------------------------------
# .enc files
# ---------------------------------------------------------------------------


def rotate_enc_files(dirs: tuple[str, ...], *, apply: bool, ring: Ring | None = None) -> FileReport:
    """Classify and (with ``apply``) rotate every ``*.enc`` file in ``dirs``."""
    from cryptography.fernet import InvalidToken

    primary, multi = _ring(ring)
    report = FileReport()
    for d in dirs:
        if not os.path.isdir(d):
            continue
        for name in sorted(os.listdir(d)):
            if not name.endswith(".enc"):
                continue
            path = os.path.join(d, name)
            with open(path, "rb") as fh:
                token = fh.read().strip()
            report.scanned += 1
            try:
                primary.decrypt(token)
                report.current += 1
                continue
            except InvalidToken:
                pass
            try:
                multi.decrypt(token)
            except InvalidToken:
                report.undecryptable += 1
                logger.warning("undecryptable file %s (no held key decrypts it; left alone)", path)
                continue
            report.stale += 1
            if not apply:
                logger.info("would rotate file %s", path)
                continue
            tmp = path + ".rotating"
            with open(tmp, "wb") as fh:
                fh.write(multi.rotate(token))
            os.chmod(tmp, 0o600)
            os.replace(tmp, path)
            report.rotated += 1
            logger.info("rotated file %s", path)
    return report


# ---------------------------------------------------------------------------
# Status (key provenance — never values)
# ---------------------------------------------------------------------------


def key_status() -> dict[str, Any]:
    """Where the keys come from and whether a copy still sits on disk in the container."""
    from lib.encryption import key_source, load_previous_keys

    residual = False
    if os.path.exists(MASTER_CONFIG_PATH):
        try:
            import yaml

            with open(MASTER_CONFIG_PATH, "r", encoding="utf-8") as fh:
                cfg = yaml.safe_load(fh) or {}
            residual = bool(cfg.get("enc_key"))
        except Exception:  # noqa: BLE001 — a malformed YAML is reported as "unreadable", not fatal
            residual = False
    return {
        "primary_source": key_source(),
        "previous_key_count": len(load_previous_keys()),
        "master_config_has_enc_key": residual,
    }


def print_status(status: dict[str, Any]) -> None:
    logger.info("primary key source     : %s", status["primary_source"])
    logger.info("previous keys held     : %d", status["previous_key_count"])
    if status["master_config_has_enc_key"]:
        logger.warning(
            "master-config.yaml still carries enc_key — a second on-disk copy inside the container; "
            "strip it  Phase 3 once ENC_KEY is env-sourced"
        )
    else:
        logger.info("master-config enc_key  : absent (no on-disk copy in the container)")


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------


def run(
    *,
    apply: bool,
    indices: list[str] | None,
    index_pattern: str | None,
    enc_dirs: tuple[str, ...],
    skip_files: bool,
    status: bool,
    es: Any = None,
) -> int:
    from lib.encryption import key_ring, load_encryption_key

    load_encryption_key()  # fail fast, before any ES call, if no primary key is present
    ring = key_ring()  # built once per run; classify/rotate reuse it for every token
    if status:
        print_status(key_status())

    if es is None:
        from lib.es_helpers import get_elasticsearch_client

        es = get_elasticsearch_client()
    if es is None:
        logger.error("Could not connect to Elasticsearch")
        return 1

    mode = "APPLY" if apply else "DRY-RUN"
    reports: list[IndexReport] = []
    for index in resolve_indices(es, indices, index_pattern):
        reports.append(rotate_index(es, index, apply=apply, ring=ring))

    file_report = FileReport() if skip_files else rotate_enc_files(enc_dirs, apply=apply, ring=ring)

    logger.info("")
    logger.info("%s summary", mode)
    logger.info("%-32s %6s %8s %6s %6s %8s %9s", "index", "docs", "current", "stale", "rotated", "conflict", "undecrypt")
    for r in reports:
        logger.info(
            "%-32s %6d %8d %6d %6d %8d %9d", r.index, r.docs, r.current, r.stale, r.rotated, r.conflicts, r.undecryptable
        )
        for label in r.uninventoried:
            logger.warning("  uninventoried field %s.%s — add it to tools.rotate_enc_key.INVENTORY", r.index, label)
    if not skip_files:
        logger.info(
            "%-32s %6d %8d %6d %6d %8s %9d",
            "*.enc files",
            file_report.scanned,
            file_report.current,
            file_report.stale,
            file_report.rotated,
            "-",
            file_report.undecryptable,
        )

    remaining = sum(r.stale - r.rotated for r in reports) + (file_report.stale - file_report.rotated)
    conflicts = sum(r.conflicts for r in reports)
    if apply:
        if remaining or conflicts:
            logger.warning(
                "stale remaining=%d conflicts=%d — re-run --apply before retiring the previous key", remaining, conflicts
            )
            return 2
        logger.info("stale remaining=0 — safe to delete /km/<env>/deam/enc-key-previous and re-run the secrets wrapper")
        return 0
    logger.info("stale=%d would be rotated by --apply", remaining)
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--apply", action="store_true", help="write rotated ciphertexts (default: dry-run)")
    parser.add_argument(
        "--status", action="store_true", help="also print key provenance (source, previous-key count, on-disk residual)"
    )
    parser.add_argument("--indices", help="comma-separated indices to scan (default: INVENTORY indices that exist)")
    parser.add_argument("--index-pattern", help="additionally scan every index matching this pattern, e.g. 'de_asset_*'")
    parser.add_argument("--enc-dirs", help="comma-separated directories of *.enc files (default: %s)" % ", ".join(ENC_DIRS))
    parser.add_argument("--skip-files", action="store_true", help="do not touch *.enc files")
    args = parser.parse_args(argv)

    indices = [s.strip() for s in args.indices.split(",") if s.strip()] if args.indices else None
    enc_dirs = tuple(s.strip() for s in args.enc_dirs.split(",") if s.strip()) if args.enc_dirs else ENC_DIRS
    return run(
        apply=args.apply,
        indices=indices,
        index_pattern=args.index_pattern,
        enc_dirs=enc_dirs,
        skip_files=args.skip_files,
        status=args.status,
    )


if __name__ == "__main__":
    sys.exit(main())
