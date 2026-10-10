"""per-run staging directories for the TikTok importer.

Each ``run_daily_imports`` mints a run name and passes it to its three steps, so
the index and archive steps touch only the files THIS run fetched — two teams'
runs overlapping can't index or archive each other's snapshots. The name is
checked and joined under a fixed base (it arrives on a command line).
"""

from __future__ import annotations

import re
import uuid
from datetime import datetime, timezone

from lib.paths import UnsafePathError, safe_path_join

STAGING_BASE = "/app/data/tiktok/organic"
IMPORTED_BASE = "/app/data/tiktok/organic_imported"

_RUN_NAME = re.compile(r"^[A-Za-z0-9_-]{1,64}$")


def new_run_name() -> str:
    return f"{datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%S')}_{uuid.uuid4().hex[:8]}"


def run_dir(base: str, run_name: str) -> str:
    """``base/run_name``; raises UnsafePathError for anything but a plain name."""
    if not isinstance(run_name, str) or not _RUN_NAME.match(run_name):
        raise UnsafePathError(f"invalid TikTok run name {run_name!r}")
    return safe_path_join(base, run_name)
