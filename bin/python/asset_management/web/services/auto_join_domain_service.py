"""
Auto-Join Domain Service

Looks up an organization in ``de_asset_organization`` whose
``auto_join_email_domains`` list contains the domain part of the
caller-supplied email. Used by the onboarding wizard's step-1 POST
handler to surface a "Join {OrgName} on Kavanah?" prompt when a
signup email matches an opted-in org.

Privacy contract (see ``the private docs`` entry):

* Match is only attempted when the email domain is **not** on
  ``FREE_MAIL_DOMAINS``. A signup from a free-mail domain therefore
  cannot probe for orgs that happen to have ``gmail.com`` listed (and
  any org that did would be misusing the field).
* The org owner must have explicitly listed the exact domain on
  their org doc; there is no fuzzy / suffix / subdomain match.
* The service returns the first match. Two orgs both claiming the
  same domain is an operator-set ambiguity we don't try to resolve
  here — the admin UI should prevent that downstream.
"""

from __future__ import annotations

import logging
import sys
from typing import TypedDict

sys.path.insert(0, "/app/python")
from lib.es_helpers import get_elasticsearch_client

ORG_INDEX = "de_asset_organization"

# Free-mail / shared-mailbox domains where email-domain ownership does
# not imply org ownership. Listed in lower-case; the matcher lower-cases
# its input before comparison. Kept conservative on purpose — adding
# obscure providers as we encounter them is cheaper than guessing.
FREE_MAIL_DOMAINS: frozenset[str] = frozenset(
    {
        "gmail.com",
        "googlemail.com",
        "outlook.com",
        "hotmail.com",
        "live.com",
        "msn.com",
        "yahoo.com",
        "yahoo.co.uk",
        "ymail.com",
        "icloud.com",
        "me.com",
        "mac.com",
        "aol.com",
        "proton.me",
        "protonmail.com",
        "pm.me",
        "gmx.com",
        "gmx.net",
        "mail.com",
        "fastmail.com",
        "zoho.com",
        "yandex.com",
        "yandex.ru",
    }
)


class AutoJoinMatch(TypedDict):
    """Shape returned by ``find_match`` on a successful hit."""

    org_esid: str
    org_title: str
    domain: str


def _email_domain(email: str | None) -> str | None:
    """Return the lower-cased domain portion of an email, or ``None``.

    Duplicated rather than imported from ``onboarding_service`` so the
    matcher has no cyclic import path back into the wizard write path.
    The behaviour is intentionally identical: bad/empty input returns
    ``None`` and the caller short-circuits.
    """
    if not email or "@" not in email:
        return None
    domain = email.rsplit("@", 1)[1].strip().lower()
    if not domain or "." not in domain:
        return None
    return domain


class AutoJoinDomainService:
    """Signup-time lookup: email domain → opted-in org, or nothing."""

    @staticmethod
    def find_match(email: str | None) -> AutoJoinMatch | None:
        """Return the first opted-in org for this email's domain.

        Returns ``None`` when:

        * the email is missing / malformed
        * the domain is on ``FREE_MAIL_DOMAINS``
        * no org has listed this domain in ``auto_join_email_domains``
        * Elasticsearch is unreachable (fail-closed: a transient ES
          outage should never silently surface a join prompt; missing
          a prompt is a softer failure than a wrong one)
        """
        domain = _email_domain(email)
        if domain is None:
            return None
        if domain in FREE_MAIL_DOMAINS:
            return None

        es = get_elasticsearch_client()
        if es is None:
            logging.warning(
                "AutoJoinDomainService: ES unavailable; skipping match for %r",
                domain,
            )
            return None

        try:
            # ``auto_join_email_domains`` is explicit keyword in the
            # asset schema (asset_schema_organization.json), so the bare field
            # IS the exact-match leaf; the dynamic-mapping-era ``.keyword``
            # multi-field no longer exists post-reindex (a term on it silently
            # matches nothing). The input domain is lower-cased by the caller,
            # so the comparison stays exact-match on a normalised value.
            result = es.search(
                index=ORG_INDEX,
                body={
                    "query": {"term": {"auto_join_email_domains": domain}},
                    "size": 1,
                },
            )
        except Exception as e:
            logging.warning("AutoJoinDomainService: lookup failed for %r: %s", domain, e)
            return None

        hits = result.get("hits", {}).get("hits", [])
        if not hits:
            return None

        source = hits[0].get("_source") or {}
        title = source.get("organization_title") or ""
        if not title:
            # An org doc that has the domain listed but no title is a
            # data-quality bug; refuse to render an empty prompt rather
            # than fall back to the ESID.
            return None

        return AutoJoinMatch(
            org_esid=hits[0]["_id"],
            org_title=title,
            domain=domain,
        )
