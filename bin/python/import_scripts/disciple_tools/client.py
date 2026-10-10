"""Disciple.Tools REST client over Site-to-Site Link auth.

Pulls ``dt-posts/v2`` posts from a DT WordPress site using DT-native
Site-to-Site Link authentication (no plugin install). Auth verified against a
live instance 2026-06-05; formula taken verbatim from the DT theme
(``dt-core/admin/site-link-post-type.php``):

    site_key       = md5(token + site1 + site2)        # domains: scheme/slashes stripped
    transfer_token = md5(site_key + gmdate('Y-m-dH'))  # UTC, e.g. "2026-06-0519"
    Authorization: Bearer <transfer_token>

DT accepts the previous, current, and next UTC hour. ``site1``/``site2`` is the
order the two domains were saved in the Site Link; an external caller can't know
it, so the client probes both orderings once and locks the one DT accepts.
"""

from __future__ import annotations

import hashlib
import logging
from datetime import datetime, timedelta, timezone

import requests as http_requests

from lib.public_host import (
    REASON_FORMAT,
    REASON_IP,
    REASON_NOT_FOUND,
    REASON_PRIVATE,
    REASON_PUBLIC_NAME,
    PublicHostError,
    vet_public_host,
)
from lib.user_errors import UserFacingError

DEFAULT_PAGE_SIZE = 100
MAX_PAGE_SIZE = 1000
REQUEST_TIMEOUT = 30


def _norm_domain(domain: str) -> str:
    """Strip scheme and all slashes — matches DT's save-time normalization."""
    d = (domain or "").replace("https://", "").replace("http://", "").replace("//", "")
    return d.replace("/", "").strip()


class DTAuthError(Exception):
    """Raised when no Site-to-Site Link bearer is accepted by the DT site."""


class DTDomainError(UserFacingError, ValueError):
    """The site domain is not a public HTTPS server. The message is safe to show."""


_DOMAIN_MESSAGES = {
    REASON_FORMAT: "Enter the Disciple.Tools site as a domain name, for example dt.example.org.",
    REASON_IP: "Use the Disciple.Tools site's domain name, not an IP address.",
    REASON_PUBLIC_NAME: "The Disciple.Tools site needs a public domain name, for example dt.example.org.",
    REASON_NOT_FOUND: "The Disciple.Tools site's domain name could not be found. Check the spelling.",
    REASON_PRIVATE: "The Disciple.Tools site's domain name points at a private network address. "
    "Only public sites are supported.",
}


def vet_dt_domain(domain: str) -> None:
    """refuse a site domain that is not a public server.

    The domain is user input that aims a credentialed server-side request (the
    wizard's probe and every nightly pull send the Site Link bearer to it), so it
    gets the same public-host rule as a self-hosted Chatwoot server
    (:mod:`lib.public_host`): no IP literals, no internal names, every resolved
    address globally routable. The domain itself is not rewritten — the Site
    Link bearer hashes it exactly as saved.

    Raises:
        DTDomainError: the domain is refused (fixed message, never the input).
    """
    d = _norm_domain(domain)
    host, port = d, None
    if ":" in d:
        host, port_text = d.rsplit(":", 1)
        if not port_text.isdigit():
            raise DTDomainError(_DOMAIN_MESSAGES[REASON_FORMAT])
        port = int(port_text)
    try:
        vet_public_host(host, port)
    except PublicHostError as e:
        raise DTDomainError(_DOMAIN_MESSAGES[e.reason]) from e


class DTSiteClient:
    """Authenticated reader for one DT site's ``dt-posts/v2`` API."""

    def __init__(self, dt_domain: str, our_domain: str, token: str):
        self.dt_domain = _norm_domain(dt_domain)
        self.our_domain = _norm_domain(our_domain)
        self.token = token or ""
        self.base = f"https://{self.dt_domain}/wp-json/dt-posts/v2"
        self._locked_bearer_order: tuple[str, str] | None = None
        self._domain_vetted = False

    # ── auth ────────────────────────────────────────────────────────────────
    def _bearer(self, s1: str, s2: str, when: datetime) -> str:
        # nosemgrep: python.lang.security.insecure-hash-algorithms-md5.insecure-hash-algorithm-md5
        # MD5 here is NOT a security control — it is the Disciple.Tools
        # Site-to-Site Link wire protocol (DT's WordPress side computes the
        # same md5(md5(token+s1+s2)+gmdate('Y-m-dH')) and compares). Changing
        # the algorithm would break the handshake. The token's secrecy, not
        # MD5's collision resistance, is what authenticates.
        site_key = hashlib.md5((self.token + s1 + s2).encode("utf-8")).hexdigest()  # nosemgrep
        stamp = when.strftime("%Y-%m-%d%H")  # PHP date('Y-m-dH')
        return hashlib.md5((site_key + stamp).encode("utf-8")).hexdigest()  # nosemgrep

    def _candidate_bearers(self) -> list[tuple[tuple[str, str], str]]:
        """All ((s1,s2), bearer) DT could accept now: both domain orderings ×
        prev/current/next UTC hour. If an ordering is already locked, only that
        ordering's current-hour bearer is returned (the common steady-state)."""
        now = datetime.now(timezone.utc)
        hours = [now, now - timedelta(hours=1), now + timedelta(hours=1)]
        if self._locked_bearer_order is not None:
            s1, s2 = self._locked_bearer_order
            return [((s1, s2), self._bearer(s1, s2, now))]
        orders = [(self.dt_domain, self.our_domain), (self.our_domain, self.dt_domain)]
        return [((s1, s2), self._bearer(s1, s2, h)) for (s1, s2) in orders for h in hours]

    def _get(self, path: str, params: dict | None = None) -> dict:
        """GET ``path`` under the dt-posts/v2 base, trying candidate bearers
        until one authenticates. Locks the working domain ordering for reuse.
        Raises DTAuthError if all candidates 401, requests.HTTPError otherwise.
        """
        if not self._domain_vetted:
            # once per client: the importer reuses one client per site.
            vet_dt_domain(self.dt_domain)
            self._domain_vetted = True
        url = f"{self.base}/{path.lstrip('/')}"
        last_status = None
        for (s1, s2), bearer in self._candidate_bearers():
            resp = http_requests.get(
                url,
                params=params or {},
                headers={"Authorization": f"Bearer {bearer}", "Accept": "application/json"},
                timeout=REQUEST_TIMEOUT,
                # a public site must not bounce the bearer inward.
                allow_redirects=False,
            )
            last_status = resp.status_code
            if resp.status_code == 401:
                continue
            if 300 <= resp.status_code < 400:
                raise http_requests.HTTPError(f"redirect refused (status {resp.status_code})", response=resp)
            resp.raise_for_status()
            self._locked_bearer_order = (s1, s2)
            return resp.json()
        raise DTAuthError(
            f"DT site-link auth rejected for {self.dt_domain} (last status {last_status}). "
            "Check the token and that both domains match the Site Link."
        )

    # ── reads ───────────────────────────────────────────────────────────────
    def fetch_posts(self, post_type: str, modified_since_ts: int | None = None, page_size: int = DEFAULT_PAGE_SIZE):
        """Yield posts of ``post_type`` (e.g. "contacts"/"groups"), newest first.

        Pages with ``sort=-last_modified``. When ``modified_since_ts`` (unix
        seconds) is given, stops as soon as a post's ``last_modified.timestamp``
        is <= the cursor — i.e. incremental sync. Otherwise pulls everything.
        """
        page_size = max(1, min(int(page_size), MAX_PAGE_SIZE))
        offset = 0
        seen = 0
        total = None
        while True:
            body = self._get(post_type, {"sort": "-last_modified", "offset": offset, "limit": page_size})
            posts = (body or {}).get("posts") or []
            if total is None:
                total = (body or {}).get("total")
                logging.info("DT %s @ %s: total=%s", post_type, self.dt_domain, total)
            if not posts:
                break
            for post in posts:
                if modified_since_ts is not None:
                    lm = post.get("last_modified") or {}
                    lm_ts = lm.get("timestamp") if isinstance(lm, dict) else None
                    if lm_ts is not None and int(lm_ts) <= int(modified_since_ts):
                        return  # newest-first → everything after is older too
                yield post
                seen += 1
            if len(posts) < page_size:
                break
            offset += page_size
        logging.info("DT %s @ %s: yielded %d post(s)", post_type, self.dt_domain, seen)
