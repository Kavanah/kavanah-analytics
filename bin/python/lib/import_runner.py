"""thin wrapper that runs an import subprocess and records its
real exit status into the per-import status file.

Without this wrapper, ``ImportService.get_import_status`` only sees PID
liveness (alive vs gone) and unconditionally marks every gone process
as ``success: True``. That conflates "process exited" with "import
succeeded" — silently masking subprocess crashes, OOM kills, container
restarts (the a teammate-restart-mid-flight scenario), and any non-zero
exit from the import scripts themselves.

The wrapper records the real exit code in a try/finally so the writer
always runs, even on signals or unexpected exceptions.

Invoke with:

    python3 -m lib.import_runner --import_id <id> --source <s> [--item <k>] -- <real-cmd...>

The real command (everything after ``--``) is exec'd as a child; its
exit code is propagated. The status file is updated under a file lock
to keep concurrent writers (multiple sources finishing within the same
import) from clobbering each other.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from datetime import datetime

# fcntl is Unix-only. The wrapper only ever runs inside imports_container
# (Linux), but we import it defensively so the module is still importable
# on Windows for unit tests / local dev.
try:
    import fcntl  # type: ignore[import-not-found]
except ImportError:
    fcntl = None  # type: ignore[assignment]


STATUS_DIR = "/app/data/import_status/"


def _status_file_path(import_id: str) -> str:
    # Caller-side validation also exists in ImportService._get_status_file;
    # this is defense in depth for the subprocess context.
    if not import_id or not all(c in "0123456789abcdef" for c in import_id):
        raise ValueError(f"invalid import_id: {import_id!r}")
    return os.path.join(STATUS_DIR, f"{import_id}.json")


def _record_completion(
    import_id: str,
    source: str,
    exit_code: int | None,
    error: str | None = None,
    item: str | None = None,
) -> None:
    """Append a per-item completion record into the import_id status file.

    ``item`` is the batch-item key (``<source>:<index>``) stamped by
    ImportService.run_imports. When present, records are keyed by it, so two
    same-source imports in one batch keep two separate records (previously the
    second youtube result vanished — records collapsed by source). When absent
    (an import launched by earlier code), the legacy by-source keying is
    preserved so an in-flight upgrade doesn't strand its status file.

    Atomic under fcntl.flock so concurrent sources finishing at the same
    instant do not lose updates. The flock is on the actual status file;
    if the file goes missing (status_dir cleaned up underneath us) we
    fall back to a no-op rather than recreate the file from scratch
    because we don't have the original ``started_at``/``pids`` context.
    """
    path = _status_file_path(import_id)
    if not os.path.exists(path):
        # The status file should always exist when the wrapper runs —
        # ImportService writes it before launching the subprocess. If
        # it's gone the most likely cause is cleanup_old_status_files
        # racing a long-running import; nothing we can do here.
        return

    record = {
        "source": source,
        "success": exit_code == 0,
        "exit_code": exit_code,
        "completed_at": datetime.now().isoformat(),
    }
    if item:
        record["item"] = item
    if error:
        record["error"] = error

    # Record identity: the batch-item key when we have one, else the source.
    def _same_record(r: dict) -> bool:
        if item:
            return r.get("item") == item
        return r.get("source") == source

    # Open r+ so we can read-modify-write under a single flock.
    with open(path, "r+") as f:
        if fcntl is not None:
            try:
                fcntl.flock(f.fileno(), fcntl.LOCK_EX)
            except OSError:
                # Best effort — if flock isn't supported (e.g. some FUSE
                # mounts), proceed without it. Concurrent writes are rare
                # enough that this is acceptable.
                pass

        try:
            f.seek(0)
            data = json.load(f)
        except (json.JSONDecodeError, ValueError):
            # Corrupt status file — refuse to overwrite. The poller
            # will handle missing-record-as-error.
            return

        # If cancel_import already wrote a cancelled record for this
        # item, leave it alone — the SIGTERM we received is the
        # cancellation, and the cancelled flag is more useful to the UI
        # than a generic non-zero exit.
        existing = [r for r in data.get("results", []) if _same_record(r)]
        if existing and existing[0].get("cancelled"):
            return

        # Replace any prior record for this item (last writer wins
        # for the unlikely double-write case).
        data["results"] = [r for r in data.get("results", []) if not _same_record(r)]
        data["results"].append(record)

        f.seek(0)
        f.truncate()
        json.dump(data, f)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--import_id", required=True)
    parser.add_argument("--source", required=True)
    # batch-item key (``<source>:<index>``); optional so imports
    # launched by older code keep the legacy by-source record keying.
    parser.add_argument("--item", default=None)
    parser.add_argument("cmd", nargs=argparse.REMAINDER)
    args = parser.parse_args(argv)

    if not args.cmd or args.cmd[0] == "--":
        cmd = args.cmd[1:] if args.cmd and args.cmd[0] == "--" else []
    else:
        cmd = args.cmd

    if not cmd:
        print("import_runner: no command supplied", file=sys.stderr)
        return 2

    exit_code: int | None = None
    error: str | None = None
    try:
        # exec-style: replace this process with the child? No — we need
        # to record completion, so spawn and wait.
        import subprocess

        proc = subprocess.Popen(cmd)
        try:
            exit_code = proc.wait()
        except KeyboardInterrupt:
            proc.terminate()
            exit_code = proc.wait()
            error = "interrupted"
    except Exception as e:
        error = f"{type(e).__name__}: {e}"
    finally:
        try:
            _record_completion(args.import_id, args.source, exit_code, error, item=args.item)
        except Exception as e:
            # Swallow recording errors — we don't want the wrapper to
            # itself fail and confuse the poller about whether the
            # underlying import succeeded.
            print(f"import_runner: failed to record completion: {e}", file=sys.stderr)

    return exit_code if exit_code is not None else 1


if __name__ == "__main__":
    sys.exit(main())
