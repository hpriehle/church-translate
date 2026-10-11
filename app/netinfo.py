"""Find the Mac's address on the church Wi-Fi (not Tailscale/VPN) and make QR codes."""
from __future__ import annotations

import ipaddress
import socket

import psutil
import segno

_SKIP_PREFIXES = ("lo", "utun", "tailscale", "docker", "br-", "veth", "bridge", "awdl", "llw",
                  "vmnet", "vboxnet", "anpi", "ap1", "gif", "stf", "zt", "wg", "tun", "tap")
_TAILSCALE = ipaddress.ip_network("100.64.0.0/10")


def _default_route_ip() -> str | None:
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        s.connect(("192.0.2.1", 9))  # no packet is sent; just asks the OS which interface it would use
        return s.getsockname()[0]
    except OSError:
        return None
    finally:
        s.close()


def candidate_ips() -> list[dict]:
    out = []
    stats = psutil.net_if_stats()
    for iface, addrs in psutil.net_if_addrs().items():
        if iface.startswith(_SKIP_PREFIXES):
            continue
        if iface in stats and not stats[iface].isup:
            continue
        for a in addrs:
            if a.family != socket.AF_INET:
                continue
            ip = ipaddress.ip_address(a.address)
            if ip.is_loopback or ip.is_link_local or ip in _TAILSCALE:
                continue
            out.append({"iface": iface, "ip": a.address, "private": ip.is_private})
    route_ip = _default_route_ip()

    def rank(c: dict) -> tuple:
        return (c["ip"] == route_ip, c["private"], c["iface"] in ("en0", "en1"), c["iface"].startswith(("en", "eth", "wl")))

    return sorted(out, key=rank, reverse=True)


def best_lan_ip(override: str = "") -> str:
    if override:
        return override
    c = candidate_ips()
    return c[0]["ip"] if c else "127.0.0.1"


def local_addresses() -> set[str]:
    addrs = {"127.0.0.1", "::1", "localhost"}
    for iface_addrs in psutil.net_if_addrs().values():
        for a in iface_addrs:
            if a.family in (socket.AF_INET, socket.AF_INET6):
                addrs.add(a.address.split("%")[0])
    return addrs


def qr_svg(url: str, scale: int = 10, dark: str = "#000", light: str = "#fff") -> str:
    import io

    qr = segno.make(url, error="m")
    buf = io.BytesIO()
    qr.save(buf, kind="svg", scale=scale, dark=dark, light=light, border=2, xmldecl=False)
    return buf.getvalue().decode()
