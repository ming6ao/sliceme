"""The loopback trust boundary for the review server.

The server can invoke ``deliver``, so every write must prove it comes from the
minted token and a loopback origin.  The token lives in the URL fragment, which
the browser never sends in the ``Referer`` header and never writes to history.

There are no cookies and no ambient credentials.
"""

from __future__ import annotations

import hmac
import secrets
from urllib.parse import urlsplit

__all__ = [
    "LOOPBACK_HOSTS",
    "constant_time_token_match",
    "is_loopback_host",
    "is_loopback_origin",
    "mint_token",
]

LOOPBACK_HOSTS = frozenset({"127.0.0.1", "::1", "localhost"})


def mint_token() -> str:
    """A fresh, URL-safe write token."""
    return secrets.token_urlsafe(32)


def constant_time_token_match(expected: str, presented: str | None) -> bool:
    if not expected or not presented:
        return False
    return hmac.compare_digest(expected, presented)


def is_loopback_host(host: str | None) -> bool:
    if not host:
        return False
    name = host.strip().lower()
    if name.startswith("["):
        end = name.find("]")
        if end == -1:
            return False
        name = name[1:end]
    elif name.count(":") == 1:
        name = name.rsplit(":", 1)[0]
    return name in LOOPBACK_HOSTS


def is_loopback_origin(origin: str | None) -> bool:
    """Whether *origin* is a loopback HTTP origin (or absent, for same-origin)."""
    if not origin:
        return True
    try:
        parts = urlsplit(origin)
    except ValueError:
        return False
    if parts.scheme not in {"http", "https"}:
        return False
    return is_loopback_host(parts.hostname)
