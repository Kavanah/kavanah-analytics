"""de_dashboard_templates under source control.

The connector dashboard templates (meta_kpi, fb_page_engagement, the
first_look_* pack, ...) are the product's face — the overview report a new
team lands on — yet they lived only as ES runtime state, hand-patched per
environment (the field deprecation reached every.py file but not
the fb_page_engagement template; staging and prod drifted silently).

This tool makes ``bin/python/dashboard_templates/*.json`` the source of
truth. One file per template doc; the filename stem is the ES ``_id``.
Files are normalized (sorted keys, 2-space indent) so diffs are readable.

Modes:

* (default) **diff** — compare git files against the live index and report
  per template: ``in_sync`` / ``differs`` / ``missing_in_es`` /
  ``not_in_git`` (ES-only docs are REPORTED, never deleted — retiring a
  template is a deliberate act, done by hand or a future --prune).
* ``--apply`` — upsert every ``differs`` / ``missing_in_es`` template from
  git into ES. Idempotent: a second run reports all in_sync.
* ``--export`` — write the live ES docs into the git files (the initial
  capture, and the sanctioned way to freeze a live-edited template back
  into source control). Use ``--only id1,id2`` to limit either direction.

Workflow for future template edits: edit the JSON in git (code-reviewed,
diffable) → deploy. Hand-editing ES directly is what this tool exists to
end; if it happens anyway, ``diff`` shows it and ``--export`` adopts it.

Deploy path
----------------------
``--apply`` was wired into no deploy path when this shipped, so templates
only changed when someone remembered to run it — the same
invisible-to-git-diffs class as the ``update_vN`` migrations.
Now the web app calls :func:`sync_on_startup` at boot (``app.py``, next to
the index-template installs): every ``differs`` /
``missing_in_es`` template is upserted from git, fail-soft, with each
overwrite logged at WARNING so a reverted live edit is visible in the
container log. The CLI ``--apply`` remains for out-of-band use.

Retiring or deleting a template
------------------------------------------
Git is the source of truth, so a template DELETED in ES (superuser, curl,
mistake) is resurrected by the next startup sync or ``--apply``. That is
the contract, not a bug: to retire a template, delete its JSON file in git
and deploy; the ES doc then shows as ``not_in_git`` (reported, never
deleted) until removed by hand or a future ``--prune``. To change a
template, edit the JSON in git; ``--export`` is the only sanctioned way to
freeze a live edit back into source control.

Exit codes
---------------------
``0`` in sync / applied / exported; ``1`` ES unavailable or aborted (partial
view, write failure); ``2`` diff mode found drift (``differs`` or
``missing_in_es``) — so a scripted drift check re-alarms every run instead of
going quiet after the first report.

Usage::

    docker exec -w /app/python imports_container python3 \\
        -m tools.sync_dashboard_templates [--apply | --export] [--only a,b]
"""

from __future__ import annotations

import argparse
import json
import logging
import sys
from pathlib import Path

_HERE = Path(__file__).resolve().parent
for _candidate in (
    _HERE.parents[1],
    _HERE.parents[2] / "bin" / "python",
    Path("/app/python"),
):
    if (_candidate / "asset_management").exists():
        sys.path.insert(0, str(_candidate))
        _PY_ROOT = _candidate
        break
else:  # pragma: no cover - defensive
    _PY_ROOT = _HERE.parents[1]

from lib.es_helpers import get_elasticsearch_client  # noqa: E402

logger = logging.getLogger(__name__)

TEMPLATES_INDEX = "de_dashboard_templates"
TEMPLATES_DIR = _PY_ROOT / "dashboard_templates"

# Page size for the live read. Well above today's 14 templates; the read
# refuses (rather than silently truncates) if the index ever holds more.
ES_PAGE_SIZE = 1000

EXIT_OK = 0
EXIT_ABORT = 1
EXIT_DRIFT = 2


def normalize(doc: dict) -> str:
    """Canonical JSON for both file content and comparison — sorted keys,
    2-space indent, trailing newline. Comparing normalized strings means
    'in sync' is exactly 'the file would be byte-identical'."""
    return json.dumps(doc, sort_keys=True, indent=2, ensure_ascii=False) + "\n"


def load_git_templates(directory: Path = TEMPLATES_DIR) -> dict[str, dict]:
    """{template_id: doc} from the versioned JSON files."""
    out: dict[str, dict] = {}
    if not directory.is_dir():
        return out
    for f in sorted(directory.glob("*.json")):
        out[f.stem] = json.loads(f.read_text())
    return out


def load_es_templates(es) -> dict[str, dict]:
    """{template_id: _source} from the live index. Raises on failure —
    a partial view must never drive an apply.

    the page size used to be a bare ``size: 500`` with no check
    against ``hits.total``; past that count the tail would have been silently
    dropped and every dropped template reported ``missing_in_es`` — and then
    re-created by ``--apply`` over whatever the live doc held. The read now
    asks for the exact total and refuses when the page is short.
    """
    resp = es.search(
        index=TEMPLATES_INDEX,
        body={"size": ES_PAGE_SIZE, "track_total_hits": True, "query": {"match_all": {}}},
    )
    hits = resp.get("hits", {}).get("hits", [])
    total = resp.get("hits", {}).get("total")
    total_value = total.get("value") if isinstance(total, dict) else total
    if isinstance(total_value, int) and total_value > len(hits):
        raise RuntimeError(
            f"{TEMPLATES_INDEX} holds {total_value} template(s) but the page returned {len(hits)} — "
            f"refusing a partial view (raise ES_PAGE_SIZE or paginate)"
        )
    return {h["_id"]: (h.get("_source") or {}) for h in hits}


def diff_state(git_docs: dict[str, dict], es_docs: dict[str, dict]) -> dict[str, list[str]]:
    """Classify every template id across both stores."""
    state: dict[str, list[str]] = {"in_sync": [], "differs": [], "missing_in_es": [], "not_in_git": []}
    for tid, doc in sorted(git_docs.items()):
        if tid not in es_docs:
            state["missing_in_es"].append(tid)
        elif normalize(doc) != normalize(es_docs[tid]):
            state["differs"].append(tid)
        else:
            state["in_sync"].append(tid)
    state["not_in_git"] = sorted(set(es_docs) - set(git_docs))
    return state


def _differing_keys(a: dict, b: dict) -> list[str]:
    keys = sorted(set(a) | set(b))
    return [k for k in keys if normalize({k: a.get(k)}) != normalize({k: b.get(k)})]


def run(es, *, apply: bool, export: bool, only: set[str] | None, directory: Path | None = None) -> dict:
    # Resolved at call time (not bound as a default) so the module constant can
    # be pointed elsewhere by tests and tooling.
    directory = directory or TEMPLATES_DIR
    git_docs = load_git_templates(directory)
    es_docs = load_es_templates(es)
    if only:
        git_docs = {k: v for k, v in git_docs.items() if k in only}
        es_docs = {k: v for k, v in es_docs.items() if k in only}

    if export:
        directory.mkdir(parents=True, exist_ok=True)
        written = []
        for tid, doc in sorted(es_docs.items()):
            path = directory / f"{tid}.json"
            content = normalize(doc)
            if path.exists() and path.read_text() == content:
                continue
            path.write_text(content)
            written.append(tid)
        logger.info("EXPORT: wrote %d file(s): %s", len(written), written or "(all already current)")
        return {"mode": "export", "written": written, "total": len(es_docs)}

    state = diff_state(git_docs, es_docs)
    for tid in state["differs"]:
        logger.info("differs      %s  (keys: %s)", tid, ", ".join(_differing_keys(git_docs[tid], es_docs[tid])))
    for tid in state["missing_in_es"]:
        logger.info("missing_in_es %s", tid)
    for tid in state["not_in_git"]:
        logger.warning("not_in_git   %s  — ES-only doc; export to adopt it or retire it by hand", tid)
    logger.info(
        "STATE: %d in_sync, %d differ, %d missing_in_es, %d not_in_git",
        len(state["in_sync"]),
        len(state["differs"]),
        len(state["missing_in_es"]),
        len(state["not_in_git"]),
    )

    applied = []
    if apply:
        for tid in state["differs"]:
            # A differing live doc is being OVERWRITTEN from git — someone's
            # hand edit is reverting. Say so at WARNING so it is visible in the
            # container log rather than discovered from a changed dashboard.
            es.index(index=TEMPLATES_INDEX, id=tid, body=git_docs[tid], refresh="wait_for")
            applied.append(tid)
            logger.warning("APPLIED %s — live doc differed from git and was overwritten", tid)
        for tid in state["missing_in_es"]:
            es.index(index=TEMPLATES_INDEX, id=tid, body=git_docs[tid], refresh="wait_for")
            applied.append(tid)
            logger.info("APPLIED %s — created (missing in ES)", tid)
        # ES-only docs are deliberately untouched (report-only above).
    return {"mode": "apply" if apply else "diff", "state": state, "applied": applied}


def sync_on_startup(es=None, directory: Path | None = None) -> dict:
    """Deploy path: align ES to git when the web app boots.

    Same contract as the index-template installs it sits
    beside in ``app.py``: idempotent, fail-soft. A failure here logs and
    returns ``{}`` — template drift must never block startup, and the CLI
    ``diff`` (exit 2) is the loud signal for an operator. Returns the
    :func:`run` result on success.
    """
    try:
        client = es if es is not None else get_elasticsearch_client()
        if client is None:
            logger.warning("Elasticsearch unavailable — dashboard templates not synced this start")
            return {}
        result = run(client, apply=True, export=False, only=None, directory=directory)
        if result.get("applied"):
            logger.warning(
                "startup template sync applied %d template(s): %s", len(result["applied"]), result["applied"]
            )
        else:
            logger.info("startup template sync — all %d template(s) in sync", len(result["state"]["in_sync"]))
        return result
    except Exception as e:  # noqa: BLE001 — fail-soft at the startup boundary, mirrors
        logger.warning("startup template sync failed (%s) — startup continues; run the CLI diff", e)
        return {}


def main() -> int:
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    ap = argparse.ArgumentParser(description="Sync de_dashboard_templates with the versioned JSON files.")
    mode = ap.add_mutually_exclusive_group()
    mode.add_argument("--apply", action="store_true", help="upsert git -> ES (default: diff report only)")
    mode.add_argument("--export", action="store_true", help="write ES -> git files")
    ap.add_argument("--only", default="", help="comma-separated template ids to limit the operation to")
    args = ap.parse_args()
    es = get_elasticsearch_client()
    if es is None:
        logger.error("Elasticsearch unavailable")
        return EXIT_ABORT
    only = {t.strip() for t in args.only.split(",") if t.strip()} or None
    try:
        result = run(es, apply=args.apply, export=args.export, only=only)
    except Exception as e:  # noqa: BLE001 — fail closed: a partial view must never drive an apply
        logger.error("Aborting: %s", e)
        return EXIT_ABORT
    if result["mode"] == "diff" and (result["state"]["differs"] or result["state"]["missing_in_es"]):
        # drift is a non-zero exit so a scripted check re-alarms on
        # every run; the old exit 0 went quiet after the first report.
        logger.warning(
            "Drift found (exit %d). Re-run with --apply to align ES to git, or --export to adopt ES into git.", EXIT_DRIFT
        )
        return EXIT_DRIFT
    return EXIT_OK


if __name__ == "__main__":
    raise SystemExit(main())
