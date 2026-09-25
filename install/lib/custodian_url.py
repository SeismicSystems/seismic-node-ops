"""Validate Custodian base URLs before embedding them in Supervisor/nginx plans.

Use a deliberately narrower, ASCII-only subset of the Rust URL parser's inputs.
In particular, '%' is not safe in an unescaped Supervisor command, and URL
credentials, queries, fragments and shell metacharacters are not accepted.
"""

from __future__ import annotations

import argparse
import ipaddress
import re
from urllib.parse import urlsplit


def valid_url(value: str, *, allow_loopback_http: bool = False) -> bool:
    if not re.fullmatch(r"[A-Za-z0-9:/._~\[\]-]+", value, flags=re.ASCII):
        return False
    try:
        parsed = urlsplit(value)
        host = parsed.hostname
        port = parsed.port
        if not host or parsed.username is not None or parsed.password is not None:
            return False
        if parsed.query or parsed.fragment or (port is not None and port < 1):
            return False
        # Reject empty explicit ports and ambiguous/noncanonical authorities.
        if parsed.netloc.endswith(":"):
            return False
        try:
            address = ipaddress.ip_address(host)
            loopback = address.is_loopback
        except ValueError:
            if len(host) > 253 or not all(
                re.fullmatch(r"[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?", label)
                for label in host.split(".")
            ):
                return False
            # Reject legacy numeric IP forms accepted differently by URL parsers.
            if re.fullmatch(r"[0-9.]+", host):
                return False
            loopback = host.lower() == "localhost"
        if parsed.scheme != "https" and not (
            allow_loopback_http and parsed.scheme == "http" and loopback
        ):
            return False
        return not any(part in {".", ".."} for part in parsed.path.split("/"))
    except ValueError:
        return False


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("url")
    parser.add_argument("--allow-loopback-http", action="store_true")
    args = parser.parse_args()
    return 0 if valid_url(args.url, allow_loopback_http=args.allow_loopback_http) else 1


if __name__ == "__main__":
    raise SystemExit(main())
