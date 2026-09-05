"""Backend-independent IP subnet helpers."""

from __future__ import annotations

import ipaddress


def get_subnet(ip: str) -> str:
    """Return the IPv4 /24 subnet in canonical CIDR notation."""
    try:
        address = ipaddress.ip_address(ip)
    except ValueError:
        return "other"
    if address.version == 4:
        return str(ipaddress.ip_network(f"{address}/24", strict=False))
    return "other"
