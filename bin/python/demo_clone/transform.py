"""Pure document rewriting for the anonymized demo clone.

No Elasticsearch here: :func:`transform_doc` takes a source document and the
demo team's identity and returns the safe copy plus the list of fields it had
to drop because :mod:`demo_clone.policy` did not know them.

Determinism is a requirement, not a nicety. The clone runs daily and upserts;
a campaign must map to the same pseudonymous id and the same "Demo Campaign
4217" label on every run, or the demo's time series fragment into a new entity
each morning. Everything derives from SHA-256 over a salt, a namespace and the
original value — no randomness, no state between runs.

The salt IS a secret. The inputs are enumerable platform ids — a
Facebook page id is public, an ad-account id is a 15-digit number — so with a
known salt anyone holding the demo tenant can hash candidate ids and recover
which real page, account and therefore team the demo is cloned from. The salt
therefore lives only in the ``DEMO_CLONE_SALT`` environment variable (SSM →
compose ``.env`` on the hosts, never in git), and the retired public value is
refused outright. Rotating it changes every pseudonym and destination ``_id``,
so a rotation is always a ``--full --recreate`` run.
"""

from __future__ import annotations

import hashlib
import os
import re
from dataclasses import dataclass
from typing import Any

from demo_clone import SaltMissing, policy

SALT_ENV = "DEMO_CLONE_SALT"
SALT_MIN_LENGTH = 16

# Salts that were once committed to this repository and are therefore public.
# The runner refuses to run under any of them, and after every apply it checks
# the destination for documents still keyed by them (see ``runner``).
RETIRED_SALTS: tuple[str, ...] = ("kavanah-demo-clone-v1",)


def salt() -> str:
    """The current pseudonym salt from ``DEMO_CLONE_SALT``, validated.

    Raises :class:`SaltMissing` (a ``CloneRefused``) when the variable is unset,
    shorter than ``SALT_MIN_LENGTH``, or equal to a retired public salt. There
    is deliberately no default: a clone that silently fell back to a fixed
    string would reintroduce the exact weakness removes.
    """
    value = (os.environ.get(SALT_ENV) or "").strip()
    if not value:
        raise SaltMissing(f"{SALT_ENV} is not set; the demo clone refuses to pseudonymize without a secret salt")
    if value in RETIRED_SALTS:
        raise SaltMissing(f"{SALT_ENV} is a retired public salt; set a fresh secret and re-clone with --full --recreate")
    if len(value) < SALT_MIN_LENGTH:
        raise SaltMissing(f"{SALT_ENV} must be at least {SALT_MIN_LENGTH} characters")
    return value


_ALNUM_RUN = re.compile(r"[A-Za-z0-9]+")
_DIGITS = "0123456789"
_LOWER = "abcdefghijklmnopqrstuvwxyz"
_UPPER = _LOWER.upper()


@dataclass(frozen=True)
class TargetTeam:
    """The demo team's identity, read from its ``de_asset_teams`` document."""

    esid: str
    title: str
    organization_title: Any  # the importers write a list; keep whatever the team doc has
    site_url: str | None = None
    country: str | None = None
    city: str | None = None
    country_iso3: str | None = None

    def field_value(self, leaf: str) -> Any:
        """Value to stamp for a team-identity field, or ``None`` to omit it."""
        return {
            "team_esid": self.esid,
            "team_title": self.title,
            "organization_title": self.organization_title,
            "team_site_url": self.site_url,
            "team_url": self.site_url,
            "team_country": self.country,
            "team_city": self.city,
            "team_country_iso3_code": self.country_iso3,
        }[leaf]


def _stream(namespace: str, value: str, *, salt_value: str) -> bytes:
    """A 64-byte deterministic byte stream for (salt, namespace, value)."""
    seed = f"{salt_value}|{namespace}|{value}".encode("utf-8")
    return hashlib.sha256(seed).digest() + hashlib.sha256(seed + b"|1").digest()


def bucket(namespace: str, value: str, *, lo: int = 1000, span: int = 9000, salt_value: str | None = None) -> int:
    """Stable integer in ``[lo, lo + span)`` for the value — the ``{n}`` in labels."""
    s = salt() if salt_value is None else salt_value
    digest = hashlib.sha256(f"{s}|{namespace}|{value}".encode("utf-8")).digest()
    return lo + int.from_bytes(digest[:8], "big") % span


def pseudonym(namespace: str, value: str, *, keep_prefix: int = 0, salt_value: str | None = None) -> str:
    """Same-shape pseudonym: every alphanumeric run is replaced character by
    character (digit → digit, lower → lower, upper → upper) from a hash
    stream; separators (``_``, ``-``, ``/``, ``.``) and the first
    ``keep_prefix`` characters are preserved.

    Shape preservation matters because the dashboards and the ES mappings do
    not care what an id says, only that it looks like one: ``post_id`` keeps
    its ``{page}_{post}`` form, a Google Ads customer id keeps ``xxx-xxx-xxxx``,
    a YouTube channel id keeps its ``UC`` prefix. A digit run never starts
    with ``0`` so numeric ids survive a round trip through ``int``.
    """
    head, tail = value[:keep_prefix], value[keep_prefix:]
    stream = _stream(namespace, value, salt_value=salt() if salt_value is None else salt_value)
    pos = 0

    def take() -> int:
        nonlocal pos
        b = stream[pos % len(stream)]
        pos += 1
        return b

    def rewrite(match: re.Match[str]) -> str:
        out: list[str] = []
        for i, ch in enumerate(match.group(0)):
            if ch.isdigit():
                d = _DIGITS[take() % 10]
                if i == 0 and d == "0":
                    d = _DIGITS[1 + take() % 9]
                out.append(d)
            elif ch.isupper():
                out.append(_UPPER[take() % 26])
            else:
                out.append(_LOWER[take() % 26])
        return "".join(out)

    return head + _ALNUM_RUN.sub(rewrite, tail)


def pseudonymous_doc_id(source_id: str, *, salt_value: str | None = None) -> str:
    """Destination ``_id`` for a source ``_id`` — stable, so re-runs upsert instead of duplicating.

    ``salt_value`` exists so the runner can compute the ids a *retired* salt
    would have produced and prove they are gone; normal callers
    leave it None and get the live secret.
    """
    return pseudonym("_id", source_id, salt_value=salt_value)


class Transformer:
    """Rewrites documents from one source index for the demo team.

    Collects the paths it dropped as unclassified so the runner can report
    them; that report is the only signal that a new field needs a rule.
    """

    def __init__(self, source: str, target: TargetTeam) -> None:
        self.source = source
        self.target = target
        self.unclassified: dict[str, int] = {}
        # Resolve the secret once per transformer so a missing salt fails at
        # construction, before any document is read, and so a mid-run change
        # to the environment cannot split one run across two salts.
        self._salt = salt()

    def transform(self, doc: dict[str, Any]) -> dict[str, Any]:
        """Return the safe copy of ``doc``. The input is not modified."""
        out = self._walk_dict(doc, prefix="")
        # Team identity is stamped even when the source doc lacks a field —
        # tenant scoping filters on team_esid, and a clone without it would
        # be invisible to the demo team and visible to no one else.
        for leaf in ("team_esid", "team_title", "organization_title"):
            value = self.target.field_value(leaf)
            if value is not None:
                out[leaf] = value
        return out

    # -- walkers ----------------------------------------------------------------

    def _walk_dict(self, node: dict[str, Any], *, prefix: str) -> dict[str, Any]:
        out: dict[str, Any] = {}
        for key, value in node.items():
            path = f"{prefix}{key}"
            leaf = key
            if leaf in policy.TEAM_FIELDS:
                stamped = self.target.field_value(leaf)
                if stamped is not None:
                    out[key] = stamped
                continue
            if path in policy.DROP_FIELDS or leaf in policy.DROP_FIELDS:
                continue
            rewritten = self._value(path, value)
            if rewritten is not _DROPPED:
                out[key] = rewritten
        return out

    def _value(self, path: str, value: Any) -> Any:
        if value is None or isinstance(value, (bool, int, float)):
            return value
        if isinstance(value, dict):
            return self._walk_dict(value, prefix=f"{path}.")
        if isinstance(value, list):
            items = [self._value(path, item) for item in value]
            return [item for item in items if item is not _DROPPED]
        if isinstance(value, str):
            return self._string(path, value)
        # Anything else (bytes, Decimal from a custom deserializer) is not
        # something an importer writes; refuse to guess.
        self._note(path)
        return _DROPPED

    def _string(self, path: str, value: str) -> Any:
        action = policy.classify(path, value)
        if action in ("keep", "date", "number"):
            return value
        if action == "drop":
            return _DROPPED
        if value in policy.SENTINELS:
            # "all" / "unknown" / "" survive every rewrite — they are grain
            # sentinels and missing-value markers, not identity.
            return value
        if action == "id":
            namespace, keep_prefix = policy.id_rule(path)
            return pseudonym(namespace, value, keep_prefix=keep_prefix, salt_value=self._salt)
        if action == "label":
            return policy.label_template(path).format(n=bucket(path.rsplit(".", 1)[-1], value, salt_value=self._salt))
        if action == "url":
            return policy.url_template(path).format(n=bucket(path.rsplit(".", 1)[-1], value, salt_value=self._salt))
        if action == "team":  # nested team field (not seen in practice) — stamp like the top level
            stamped = self.target.field_value(path.rsplit(".", 1)[-1])
            return stamped if stamped is not None else _DROPPED
        self._note(path)
        return _DROPPED

    def _note(self, path: str) -> None:
        self.unclassified[path] = self.unclassified.get(path, 0) + 1


class _Dropped:
    """Sentinel: the field is removed from the output."""

    def __repr__(self) -> str:  # pragma: no cover — debugging aid
        return "<DROPPED>"


_DROPPED = _Dropped()


def transform_doc(source: str, doc: dict[str, Any], target: TargetTeam) -> tuple[dict[str, Any], dict[str, int]]:
    """One-shot convenience around :class:`Transformer` for a single document."""
    t = Transformer(source, target)
    return t.transform(doc), t.unclassified
