"""Public host vetting for credentialed server-side fetches.

Some connectors send a stored credential to a host the user typed: a self-hosted
Chatwoot server or a Disciple.Tools site. That host is user
input aiming a server-side request (SSRF), so it is held to a public server:

* No IP literals (including a numeric last label, which the resolver would read
  as IPv4), no single-label names (``es01``, ``localhost``) and no internal-only
  suffixes.
* Every address the host resolves to must be globally routable, including the
  IPv4 address inside a NAT64 or IPv4-compatible IPv6 answer (6to4, 2002::/16, is
  never global to ``ipaddress`` on Python 3.13, so it is refused outright).
* The lookup has a deadline (:data:`_RESOLVE_TIMEOUT_SECONDS`), so a stalled
  nameserver cannot hang a form save or an import batch.

A resolver that answers differently between this check and the request that
follows is accepted residual risk (the HTTP client does its own lookup); callers
also refuse redirects, so a public server cannot bounce the request inward.

Failures raise :class:`PublicHostError` with a ``reason`` code; each caller maps
the code to its own user-facing text (the connector's name, its own examples).
"""

from __future__ import annotations

import concurrent.futures
import ipaddress
import socket
from typing import Any

REASON_FORMAT = "format"
REASON_IP = "ip"
REASON_PUBLIC_NAME = "public_name"
REASON_NOT_FOUND = "not_found"
REASON_PRIVATE = "private"

_RESOLVE_TIMEOUT_SECONDS = 5.0
_INTERNAL_SUFFIXES = (".local", ".localhost", ".internal", ".lan", ".home.arpa")
_LABEL_CHARS = frozenset("abcdefghijklmnopqrstuvwxyz0123456789-")


class PublicHostError(ValueError):
    """The host is not an acceptable public server; ``reason`` says why."""

    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


def vet_public_host(host: str, port: int | None, *, resolve: bool = True) -> str:
    """Return ``host`` lower-cased and IDNA-encoded if it names a public server.

    ``host`` is the bare hostname (no scheme, port or path). ``resolve=False``
    skips the public-address lookup; use it only to compare stored values, never
    before a request.

    Raises:
        PublicHostError: the host is refused; ``reason`` is one of the
            ``REASON_*`` codes.
    """
    host = (host or "").rstrip(".").lower()
    if not host:
        raise PublicHostError(REASON_FORMAT)
    if _is_ip_literal(host):
        raise PublicHostError(REASON_IP)
    try:
        host = host.encode("idna").decode("ascii")
    except UnicodeError as e:
        raise PublicHostError(REASON_FORMAT) from e
    # After IDNA, so look-alike dots ("10。1") are already plain dots. A numeric
    # last label is never a real TLD; the resolver would read "10.1" as IPv4.
    if _is_ip_literal(host) or host.rsplit(".", 1)[-1].isdigit():
        raise PublicHostError(REASON_IP)
    if port == 0:
        raise PublicHostError(REASON_FORMAT)
    if not _is_dns_hostname(host):
        raise PublicHostError(REASON_PUBLIC_NAME if "." not in host else REASON_FORMAT)
    if host.endswith(_INTERNAL_SUFFIXES):
        raise PublicHostError(REASON_PUBLIC_NAME)
    if resolve:
        _require_public_addresses(host, port or 443)
    return host


def _is_dns_hostname(host: str) -> bool:
    """Two or more labels of [a-z0-9-], none empty, none starting or ending with '-'.

    replaces ``[a-z0-9](?:[a-z0-9-]*[a-z0-9])?(?:\\.…)+``, which is
    quadratic on long hyphen-heavy input; this accepts exactly the same names.
    """
    labels = host.split(".")
    return len(labels) >= 2 and all(
        label and label[0] != "-" and label[-1] != "-" and set(label) <= _LABEL_CHARS for label in labels
    )


def _is_ip_literal(host: str) -> bool:
    try:
        ipaddress.ip_address(host)
    except ValueError:
        return False
    return True


# IPv6 ranges whose low 32 bits carry an IPv4 address a gateway may route to:
# NAT64 well-known and local-use prefixes, and the deprecated IPv4-compatible ::/96.
_IPV4_EMBEDDING_NETWORKS = (
    ipaddress.ip_network("64:ff9b::/96"),
    ipaddress.ip_network("64:ff9b:1::/48"),
    ipaddress.ip_network("::/96"),
)


def _resolve(host: str, port: int) -> list[Any]:
    # A single-use worker so a stalled nameserver costs at most the deadline; the
    # abandoned lookup finishes (or times out in the OS) on its own thread.
    pool = concurrent.futures.ThreadPoolExecutor(max_workers=1)
    try:
        future = pool.submit(socket.getaddrinfo, host, port, proto=socket.IPPROTO_TCP)
        return future.result(timeout=_RESOLVE_TIMEOUT_SECONDS)
    finally:
        pool.shutdown(wait=False)


def _addresses_to_check(ip: ipaddress.IPv4Address | ipaddress.IPv6Address) -> list[Any]:
    """The address itself plus any IPv4 address embedded in an IPv6 answer."""
    found: list[Any] = [ip]
    if ip.version == 6:
        if ip.ipv4_mapped is not None:
            found.append(ip.ipv4_mapped)
        if any(ip in net for net in _IPV4_EMBEDDING_NETWORKS):
            found.append(ipaddress.IPv4Address(int(ip) & 0xFFFFFFFF))
    return found


def _require_public_addresses(host: str, port: int) -> None:
    try:
        infos = _resolve(host, port)
    except (socket.gaierror, UnicodeError, concurrent.futures.TimeoutError) as e:
        raise PublicHostError(REASON_NOT_FOUND) from e
    addresses = {info[4][0] for info in infos}
    if not addresses:
        raise PublicHostError(REASON_NOT_FOUND)
    for address in addresses:
        ip = ipaddress.ip_address(address.split("%", 1)[0])
        if not all(candidate.is_global for candidate in _addresses_to_check(ip)):
            raise PublicHostError(REASON_PRIVATE)
