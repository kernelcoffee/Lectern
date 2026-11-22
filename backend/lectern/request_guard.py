"""Refuse requests that do not come from the LAN, before any route runs.

Lectern has no login: a device on the LAN is trusted. The admin's browser is on
the LAN too, though, and it carries requests for any page it opens. Two checks
keep a page from outside the LAN from driving Lectern through that browser:

- **Host.** The ``Host`` header must name Lectern the way a LAN client does: an
  IP address, a LAN-safe name (``localhost``, a single word, or a name under
  ``.local``, ``.lan``, ``.home.arpa`` or ``.internal``) or a name listed in
  ``LECTERN_ALLOWED_HOSTS``. A DNS-rebinding page always arrives with its own
  public name here.
- **Origin.** When a request carries ``Origin``, its host must be the
  request's own host, a loopback, private or link-local address, a LAN-safe
  name or a listed name. A public IP address is accepted as ``Host`` (it is
  what the admin typed) but not as a foreign ``Origin``: a hostile page can be
  served from a bare public IP. ``Origin: null`` is refused.

Browsers send ``Origin`` on every WebSocket handshake, every request other than
``GET`` or ``HEAD``, and every CORS-mode ``fetch``. A cross-site simple ``GET``
(an ``<img>``, a ``<script>``, a link the admin follows) carries no ``Origin``
and faces only the Host check, so a page outside the LAN can still make the
admin's browser send one to an IP address. ``GET`` routes must therefore have
no side effects.

Ports are ignored on both sides (the Vite proxy serves the page on one port
and the API on another). ``LECTERN_ALLOWED_HOSTS=*`` switches both checks off.

``RequestGuard`` is a pure ASGI middleware because ``BaseHTTPMiddleware`` never
sees WebSocket scopes.
"""

from __future__ import annotations

import ipaddress
import logging
import re
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Literal, TypeAlias

from starlette import status
from starlette.responses import JSONResponse
from starlette.types import ASGIApp, Receive, Scope, Send
from starlette.websockets import WebSocketClose

logger = logging.getLogger(__name__)

ALLOWED_HOSTS_VARIABLE = "LECTERN_ALLOWED_HOSTS"

# Name suffixes that nobody outside the LAN can make resolve: ``.local`` (mDNS)
# and ``.home.arpa`` are reserved by RFC, ``.internal`` by ICANN for private
# use, ``.lan`` is the de facto home-router convention.
LAN_SUFFIXES = (".local", ".lan", ".home.arpa", ".internal")

# Private address ranges accepted as a foreign Origin, spelled out rather than
# taken from ``ip.is_private``: that one also covers the documentation and
# benchmarking ranges (203.0.113.0/24 among them), which are not the LAN, and
# its exact set has changed between Python releases.
_PRIVATE_NETWORKS = (
    ipaddress.ip_network("10.0.0.0/8"),
    ipaddress.ip_network("172.16.0.0/12"),
    ipaddress.ip_network("192.168.0.0/16"),
    ipaddress.ip_network("fc00::/7"),
)

# How many distinct refused values are remembered so each is logged once. The
# oldest is forgotten first, so a flood of made-up hosts cannot grow memory.
REFUSAL_LOG_MEMORY = 256

# Longest refused value echoed in a log line or a response body.
_MAX_SHOWN = 200

_PORT = re.compile(r"[0-9]{1,5}")
_LABEL = re.compile(r"[a-z0-9_-]{1,63}")
_ORIGIN = re.compile(r"(?:http|https)://([^/?#@\s]+)")

IPAddress: TypeAlias = ipaddress.IPv4Address | ipaddress.IPv6Address


def parse_host(value: str) -> str | None:
    """Normalised hostname of a ``Host`` value (or of a list entry): port and
    IPv6 brackets stripped, lower-cased, one trailing dot removed, an IP
    address in its canonical form. ``None`` for anything that is not a valid
    ``host[:port]``, a bare IPv6 address included."""
    value = value.strip().lower()
    if value.startswith("["):
        literal, bracket, rest = value[1:].partition("]")
        if not bracket or (rest and not _is_port_suffix(rest)):
            return None
        try:
            return str(ipaddress.IPv6Address(literal))
        except ValueError:
            return None
    name, colon, port = value.partition(":")
    if colon and not _PORT.fullmatch(port):
        return None
    name = name.removesuffix(".")
    if not name or len(name) > 253:
        return None
    try:
        return str(ipaddress.IPv4Address(name))
    except ValueError:
        pass
    if not all(_LABEL.fullmatch(label) for label in name.split(".")):
        return None
    return name


def parse_origin(value: str) -> str | None:
    """Normalised hostname of an ``Origin`` value (``http`` or ``https``, no
    path, no credentials). ``None`` for ``null``, other schemes and garbage."""
    match = _ORIGIN.fullmatch(value.strip().lower())
    if match is None:
        return None
    return parse_host(match.group(1))


def _is_port_suffix(rest: str) -> bool:
    return rest.startswith(":") and _PORT.fullmatch(rest[1:]) is not None


def _as_ip(name: str) -> IPAddress | None:
    try:
        return ipaddress.ip_address(name)
    except ValueError:
        return None


def is_lan_safe(name: str) -> bool:
    """A normalised hostname nobody outside the LAN can make resolve:
    ``localhost``, a single word, or a name under one of ``LAN_SUFFIXES``. An
    IP address is never LAN-safe by name (a bare IPv6 address has no dot)."""
    if _as_ip(name) is not None:
        return False
    return name == "localhost" or "." not in name or name.endswith(LAN_SUFFIXES)


def is_local_ip(ip: IPAddress) -> bool:
    """Loopback, private (RFC 1918, IPv6 unique local) or link-local."""
    if isinstance(ip, ipaddress.IPv6Address) and ip.ipv4_mapped is not None:
        ip = ip.ipv4_mapped
    return ip.is_loopback or ip.is_link_local or any(ip in net for net in _PRIVATE_NETWORKS)


@dataclass(frozen=True)
class AllowPolicy:
    """The parsed ``LECTERN_ALLOWED_HOSTS``: exact names, the off switch, and
    the entries that are not host names (logged at startup, never matched)."""

    listed: frozenset[str] = frozenset()
    disabled: bool = False
    rejected: tuple[str, ...] = ()


def _parse_entry(entry: str) -> str | None:
    """One list entry: a bare IP address (an unbracketed IPv6 address is
    unambiguous between commas) or anything ``parse_host`` accepts."""
    ip = _as_ip(entry)
    if ip is not None:
        return str(ip)
    return parse_host(entry)


def parse_allowed_hosts(raw: str) -> AllowPolicy:
    """Split on commas, trim, normalise each entry like a ``Host`` value
    (letter case, port, trailing dot, IPv6 with or without brackets). ``*``
    anywhere switches the checks off."""
    entries = [entry.strip() for entry in raw.split(",")]
    if "*" in entries:
        return AllowPolicy(disabled=True)
    listed: set[str] = set()
    rejected: list[str] = []
    for entry in entries:
        if not entry:
            continue
        name = _parse_entry(entry)
        if name is None:
            rejected.append(entry)
        else:
            listed.add(name)
    return AllowPolicy(listed=frozenset(listed), rejected=tuple(rejected))


def host_allowed(host: str, policy: AllowPolicy) -> bool:
    """The Host rule, on a normalised hostname: an IP address, LAN-safe or listed."""
    return _as_ip(host) is not None or is_lan_safe(host) or host in policy.listed


def origin_allowed(origin: str, host: str, policy: AllowPolicy) -> bool:
    """The Origin rule, on normalised hostnames: the request's own host, a
    loopback, private or link-local address, LAN-safe or listed."""
    if origin == host or origin in policy.listed:
        return True
    ip = _as_ip(origin)
    if ip is not None:
        return is_local_ip(ip)
    return is_lan_safe(origin)


@dataclass(frozen=True)
class Refusal:
    """Why a request was refused: which header, and the value it carried."""

    header: Literal["Host", "Origin"]
    value: str | None  # None: the header was missing

    @property
    def message(self) -> str:
        if self.value is None:
            return (
                f"Refused: the request has no {self.header} header. Lectern only answers "
                f"requests addressed to an allowed host (see {ALLOWED_HOSTS_VARIABLE})."
            )
        shown = repr(self.value[:_MAX_SHOWN])
        if self.header == "Host":
            return (
                f"Refused: Host {shown} is not allowed. To reach Lectern by this name, "
                f"add it to {ALLOWED_HOSTS_VARIABLE}."
            )
        return (
            f"Refused: Origin {shown} is not allowed. If pages from this site should use "
            f"Lectern, add its host name to {ALLOWED_HOSTS_VARIABLE}."
        )


def evaluate(
    hosts: Sequence[str], origins: Sequence[str], policy: AllowPolicy
) -> Refusal | None:
    """Apply both checks to a request's ``Host`` and ``Origin`` header values
    (a header sent twice is refused). ``None`` when the request may pass."""
    if policy.disabled:
        return None
    if len(hosts) != 1:
        return Refusal("Host", ", ".join(hosts) if hosts else None)
    host = parse_host(hosts[0])
    if host is None or not host_allowed(host, policy):
        return Refusal("Host", hosts[0])
    if not origins:
        return None
    if len(origins) > 1:
        return Refusal("Origin", ", ".join(origins))
    origin = parse_origin(origins[0])
    if origin is None or not origin_allowed(origin, host, policy):
        return Refusal("Origin", origins[0])
    return None


def _header_values(scope: Scope, name: bytes) -> list[str]:
    # ASGI header names are lower-case byte strings; values are latin-1.
    return [value.decode("latin-1") for key, value in scope["headers"] if key == name]


class RequestGuard:
    """Pure ASGI middleware: refuses an HTTP request with 403 and a JSON
    ``detail``, and a WebSocket handshake by closing it before ``accept``,
    when ``evaluate`` refuses it. Lifespan and other scopes pass through."""

    def __init__(self, app: ASGIApp, policy: AllowPolicy) -> None:
        self.app = app
        self.policy = policy
        # Insertion-ordered, used as a bounded set of refusals already logged.
        self._logged: dict[tuple[str, str | None], None] = {}

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] not in ("http", "websocket"):
            await self.app(scope, receive, send)
            return
        refusal = evaluate(
            _header_values(scope, b"host"), _header_values(scope, b"origin"), self.policy
        )
        if refusal is None:
            await self.app(scope, receive, send)
            return
        self._log_once(refusal)
        if scope["type"] == "http":
            response = JSONResponse(
                {"detail": refusal.message}, status_code=status.HTTP_403_FORBIDDEN
            )
            await response(scope, receive, send)
        else:
            # A close before accept makes the server answer the handshake 403.
            await WebSocketClose(code=status.WS_1008_POLICY_VIOLATION)(scope, receive, send)

    def _log_once(self, refusal: Refusal) -> None:
        key = (refusal.header, None if refusal.value is None else refusal.value[:_MAX_SHOWN])
        if key in self._logged:
            return
        if len(self._logged) >= REFUSAL_LOG_MEMORY:
            del self._logged[next(iter(self._logged))]
        self._logged[key] = None
        logger.warning("%s", refusal.message)
