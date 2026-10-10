"""Loose email-shape check without a backtracking regex.

Four call sites validated addresses with ``^[^@\\s]+@[^@\\s]+\\.[^@\\s]+$``.
That pattern is quadratic on long '@'/'.'-heavy input (the domain part can
match a dot both inside ``[^@\\s]+`` and at ``\\.``), so a crafted 40 KB token
pinned a worker for seconds. ``is_plausible_email`` accepts exactly the same
strings in linear time.
"""

from __future__ import annotations

EMAIL_MAX_LENGTH = 254


def is_plausible_email(value: str) -> bool:
    """True iff ``value`` looks like ``local@domain.tld``.

    Same language as ``^[^@\\s]+@[^@\\s]+\\.[^@\\s]+$``: no whitespace, exactly
    one '@', a non-empty local part, and a domain holding a '.' with at least
    one character on each side. Length is the caller's business.
    """
    if not value or any(ch.isspace() for ch in value):
        return False
    local, at, domain = value.partition("@")
    if not at or not local or "@" in domain:
        return False
    return "." in domain[1:-1]
