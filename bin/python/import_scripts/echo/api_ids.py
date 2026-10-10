"""Echo API ids that become file names.

The Echo importers name their staging files after ids from the Echo GraphQL
response (user ids, conversation ids). Those ids are third-party data — and
 the response could come from any host a workspace owner typed
into the tenant subdomain — so an id is only used as a file name when it is a
plain token. ``.`` and ``/`` are never allowed, so a traversal (``../x``) is
impossible by construction; ``lib.paths.safe_path_join`` stays as the second
layer at the write / remove site.

Prod scan 2026-10-07: every stored id matches (140,500 conversation ids — mostly
8 digits, ~306 alphanumeric with hyphens — and 33,971 numeric user ids).
"""

from __future__ import annotations

import re

_ECHO_ID_RE = re.compile(r"[A-Za-z0-9_-]{1,64}")


def is_safe_echo_id(value: object) -> bool:
    """True when ``value`` (str or int) is safe to use as a file-name stem."""
    return isinstance(value, (str, int)) and not isinstance(value, bool) and bool(_ECHO_ID_RE.fullmatch(str(value)))
