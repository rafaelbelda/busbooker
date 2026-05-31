"""
Client identification for request logging / traceability.

Production runs uvicorn bound to loopback behind nginx (see ``busbooker`` nginx
config), which sets ``X-Real-IP`` and ``X-Forwarded-For``. Forwarded headers are
honoured **only** when the immediate TCP peer is a trusted proxy (loopback by
default, plus any CIDRs in the ``TRUSTED_PROXIES`` setting). Otherwise the raw
peer address is used and forwarded headers are recorded but flagged untrusted —
so a public client cannot spoof the IP we attribute actions to.

Spoofing note: with ``X-Forwarded-For $proxy_add_x_forwarded_for`` nginx
*appends* the real peer to whatever the client sent, so the **left-most** XFF
entry is client-controlled and must never be trusted. We therefore prefer
``X-Real-IP`` (nginx sets it to ``$remote_addr`` — the real immediate client)
and only fall back to the **right-most** XFF entry.
"""
from __future__ import annotations

import ipaddress
from dataclasses import dataclass
from typing import List, Optional, Union

from ..config import settings

_IpNet = Union[ipaddress.IPv4Network, ipaddress.IPv6Network]

# Tailscale CGNAT range — tailnet peers present an address in here.
_TAILSCALE_NET = ipaddress.ip_network("100.64.0.0/10")


def _parse_networks(raw: str) -> List[_IpNet]:
    nets: List[_IpNet] = []
    for part in raw.split(","):
        part = part.strip()
        if not part:
            continue
        try:
            nets.append(ipaddress.ip_network(part, strict=False))
        except ValueError:
            continue  # ignore malformed entries rather than crash at import
    return nets


_TRUSTED_NETS = _parse_networks(getattr(settings, "trusted_proxies", "") or "")


def _ip_or_none(value: Optional[str]):
    try:
        return ipaddress.ip_address(value)  # type: ignore[arg-type]
    except (ValueError, TypeError):
        return None


def _is_trusted_proxy(host: str) -> bool:
    ip = _ip_or_none(host)
    if ip is None:
        return False
    if ip.is_loopback:  # nginx → uvicorn on 127.0.0.1/::1
        return True
    return any(ip in net for net in _TRUSTED_NETS)


def _label(ip_str: str) -> str:
    ip = _ip_or_none(ip_str)
    if ip is not None and ip in _TAILSCALE_NET:
        return " (tailscale)"
    return ""


@dataclass(frozen=True)
class ClientInfo:
    ip: str                      # best-effort real client IP
    peer: str                    # immediate TCP peer (the proxy, in prod)
    forwarded_for: Optional[str]
    real_ip: Optional[str]
    trusted: bool                # did `ip` come from a trusted proxy's headers?
    user_agent: Optional[str]

    def log_str(self) -> str:
        via = "proxy" if self.trusted else "peer"
        return f"ip={self.ip}{_label(self.ip)} via={via} peer={self.peer}"


def client_info(request) -> ClientInfo:
    """Resolve the best-effort client identity for a Starlette/FastAPI request."""
    peer = request.client.host if request.client else "-"
    xff = request.headers.get("x-forwarded-for")
    xri = request.headers.get("x-real-ip")
    ua = request.headers.get("user-agent")

    if _is_trusted_proxy(peer):
        # Prefer the unspoofable X-Real-IP; else the right-most XFF entry.
        xff_real = xff.split(",")[-1].strip() if xff else ""
        client = xri or xff_real or peer
        trusted = True
    else:
        client = peer
        trusted = False

    return ClientInfo(
        ip=client,
        peer=peer,
        forwarded_for=xff,
        real_ip=xri,
        trusted=trusted,
        user_agent=ua,
    )
