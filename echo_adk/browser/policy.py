"""Exact-origin and address checks also used by the filtering HTTP proxy.

The proxy connects to a checked numeric address, not a second DNS resolution.
This is application egress control, not an OS sandbox against a compromised browser.
"""

from __future__ import annotations

import ipaddress
import socket
from dataclasses import dataclass
from urllib.parse import parse_qsl, urlsplit, urlunsplit

from echo_adk.core.redact import is_secret_field, redact

from .config import BrowserConfig, origin
from .errors import BrowserError

_AUTH_QUERY_FIELDS = {"code", "state", "assertion", "client_secret", "session", "sessionid", "ticket", "signature", "sig"}


@dataclass(frozen=True)
class Target:
    origin: str
    scheme: str
    host: str
    port: int
    path: str


def safe_url(value: str) -> str:
    try:
        p = urlsplit(value)
        clean_origin = origin(urlunsplit((p.scheme, p.netloc, "", "", "")))
        return redact(clean_origin + p.path)[0][:2048]
    except (BrowserError, ValueError):
        return ""


class NetworkPolicy:
    def __init__(self, config: BrowserConfig):
        self.config = config

    def validate(self, value: str, *, navigation: bool = False) -> Target:
        try:
            if not isinstance(value, str) or len(value) > 8192 or any(ord(c) < 32 or ord(c) == 127 for c in value) or "\\" in value:
                raise ValueError
            p = urlsplit(value)
            target_origin = origin(urlunsplit((p.scheme, p.netloc, "", "", "")))
            if target_origin not in self.config.allowed_origins:
                raise ValueError
            if navigation and any(
                is_secret_field(k) or k.lower() in _AUTH_QUERY_FIELDS for k, _ in parse_qsl(p.query, keep_blank_values=True)
            ):
                raise BrowserError("credential_url", 403, "Credential or OAuth redirect URLs are not accepted by the browser tool")
            host = p.hostname.encode("idna").decode("ascii").lower().rstrip(".")
            return Target(
                target_origin,
                p.scheme,
                host,
                p.port or (443 if p.scheme == "https" else 80),
                (p.path or "/") + ("?" + p.query if p.query else ""),
            )
        except (ValueError, UnicodeError, AttributeError):
            raise BrowserError("network_denied", 403, "URL is outside the host browser network policy") from None
        except BrowserError as exc:
            if exc.code == "invalid_config":
                raise BrowserError("network_denied", 403, "URL is outside the host browser network policy") from None
            raise

    def addresses(self, target: Target) -> list[tuple[int, str]]:
        try:
            answers = socket.getaddrinfo(target.host, target.port, type=socket.SOCK_STREAM)
        except OSError:
            raise BrowserError("network_unavailable", 503, "The allowed browser origin could not be resolved") from None
        addresses = []
        for family, _, _, _, address in answers:
            if family not in (socket.AF_INET, socket.AF_INET6):
                continue
            try:
                ip = ipaddress.ip_address(address[0])
            except ValueError:
                raise BrowserError("network_denied", 403, "Unrecognized address in browser DNS result") from None
            # IPv4-mapped IPv6 is evaluated as the underlying address as well.
            check = ip.ipv4_mapped if isinstance(ip, ipaddress.IPv6Address) and ip.ipv4_mapped else ip
            if target.origin not in self.config.private_origins and (not ip.is_global or not check.is_global):
                raise BrowserError("network_denied", 403, "Private, local and metadata addresses require an exact host exception")
            item = (family, str(ip))
            if item not in addresses:
                addresses.append(item)
        if not addresses:
            raise BrowserError("network_unavailable", 503, "The allowed browser origin has no supported address")
        return addresses
