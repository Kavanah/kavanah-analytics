"""Centralized helper for safely joining a base directory with a user-supplied filename.

 Phase 4: routes and services that build file paths from request
arguments (log filenames, snapshot dates, backup names) showed up in
CodeQL ``py/path-injection`` (37 findings as of 2026-04-29). The existing
defense across these call sites is ``os.path.basename(filename)``, which
strips ``../`` traversal but is not recognized by CodeQL as a sanitizer.

``werkzeug.utils.safe_join`` is the canonical, CodeQL-recognized
containment check: it normalizes the joined path and guarantees the
result stays under ``directory``. It returns ``None`` on rejection. This
helper raises ``UnsafePathError`` instead so callers can use a single
try/except for both "file not found" and "rejected as unsafe".
"""

from __future__ import annotations

from werkzeug.utils import safe_join


class UnsafePathError(ValueError):
    """Raised when a user-supplied filename escapes its base directory."""


def safe_path_join(base_dir: str, *parts: str) -> str:
    """Join ``base_dir`` with ``parts`` and verify containment.

    Wraps ``werkzeug.utils.safe_join`` so a rejection becomes an
    exception rather than a ``None`` return.

    Args:
        base_dir: Trusted directory the result must stay under.
        *parts: Untrusted path components (typically a single filename
            from request data).

    Returns:
        The joined path, guaranteed by Werkzeug to be inside ``base_dir``.

    Raises:
        UnsafePathError: If the joined path would escape ``base_dir``
            (absolute paths, ``..`` traversal, null bytes, etc.).
    """
    joined = safe_join(base_dir, *parts)
    if joined is None:
        raise UnsafePathError(f"path components {parts!r} escape {base_dir!r}")
    return joined
