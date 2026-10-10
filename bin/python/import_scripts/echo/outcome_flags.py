"""single source of truth for the Echo outcome category flags.

Both the importer (``get_conversations.process_conversation_entry``) and the
backfill migration (``migration/backfill_echo_outcome_fields.py``) stamp the
same five counter fields from a conversation's ``outcome`` string. They each
had their own copy of the mapping, and both compared case-sensitively against
``"Language barrier"`` while Echo returns ``"Language Barrier"`` — so the
``language_barrier`` flag was 0 on every doc ever imported and those
conversations inflated ``Other`` instead.

The comparison here is case-insensitive (and whitespace-trimmed). The stored
``outcome`` string itself is never modified — only the derived flags.

An empty/missing outcome intentionally keeps the legacy behavior: it counts
as ``Other`` (that null-outcome inflation is a separate, known concern — the
 equals-on-outcome direction — not silently changed here).
"""

from __future__ import annotations

# Lowercased outcome title -> flag field it sets.
KNOWN_OUTCOME_FLAGS: dict[str, str] = {
    "spiritual conversation only": "spiritual_conversation",
    "connected with multiplier": "multiplier",
    "no response": "no_response",
    "language barrier": "language_barrier",
}

# Every flag field stamped on a conversation doc (exactly one is 1).
ALL_FLAG_FIELDS: tuple[str, ...] = (
    "spiritual_conversation",
    "multiplier",
    "no_response",
    "language_barrier",
    "Other",
)


def compute_outcome_flags(outcome: str | None) -> dict[str, int]:
    """Derive the five outcome category counter fields from an outcome string.

    Exactly one field is 1: the matching known-outcome flag, or ``Other`` for
    any unknown, custom (e.g. "DJI 01"), or empty outcome.
    """
    normalized = (outcome or "").strip().lower()
    flags = {field: 0 for field in ALL_FLAG_FIELDS}
    known_flag = KNOWN_OUTCOME_FLAGS.get(normalized)
    if known_flag:
        flags[known_flag] = 1
    else:
        flags["Other"] = 1
    return flags
