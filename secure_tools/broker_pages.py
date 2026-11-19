"""What the broker and the gateway share about the broker's browser pages.

Kept in ``secure_tools`` because both sides import it: the gateway application
layer must not import the broker package (tests/test_runtime_architecture_boundaries.py),
and the broker must not import the gateway.

- Which paths are broker pages people open (sign-ins, credential forms, account
  links), and the card view of a page that Web Chat frames: the same address
  with ``?view=card``, always sandboxed with ``CARD_FRAME_SANDBOX`` (never
  ``allow-same-origin``: behind a reverse proxy the broker shares the chat's
  address, and only an opaque origin keeps the chat page from reading the form).
- ``policy_origin``: an origin rebuilt from checked parts, safe to put into a
  Content-Security-Policy (the broker's ``frame-ancestors``, the chat's
  ``frame-src``).
- How a sign-in page ended, as its status route reports it and the gateway's
  watchers read it.
"""

from __future__ import annotations

import ipaddress
import re
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

# Sign-ins (/auth/...), credential collection and deletion forms
# (/credential-collection/...) and account link forms (/account-link/...). Each
# address holds the page's private link; the broker's caller-token middleware
# lets these paths through and the page itself checks the link.
BROKER_PAGE_PREFIXES = ("/auth/", "/credential-collection/", "/account-link/")

CARD_VIEW_PARAM = "view"
CARD_VIEW_VALUE = "card"
# The sandbox of the card view's Content-Security-Policy and of the iframe Web
# Chat builds (assistant_gateway/onboarding_web/assets/broker_card.js).
CARD_FRAME_SANDBOX = "allow-scripts allow-forms allow-popups allow-popups-to-escape-sandbox"

# A sign-in page's final statuses; "declined" means the person chose Decline.
SIGN_IN_STATUS_SUCCESS = "success"
SIGN_IN_STATUS_ERROR = "error"
SIGN_IN_STATUS_DECLINED = "declined"
SIGN_IN_FINAL_STATUSES = frozenset({SIGN_IN_STATUS_SUCCESS, SIGN_IN_STATUS_ERROR, SIGN_IN_STATUS_DECLINED})

# A host name or IPv4 address as it may appear in a Content-Security-Policy source.
_HOST_NAME = re.compile(r"[a-z0-9](?:[a-z0-9.-]{0,251}[a-z0-9])?")


def card_view_url(url: str) -> str:
    """The card view of a broker page address: the same address with ``view=card``."""

    parts = urlsplit(url)
    query = [(name, value) for name, value in parse_qsl(parts.query, keep_blank_values=True) if name != CARD_VIEW_PARAM]
    query.append((CARD_VIEW_PARAM, CARD_VIEW_VALUE))
    return urlunsplit((parts.scheme, parts.netloc, parts.path, urlencode(query), parts.fragment))


def policy_origin(base_url: str | None) -> str | None:
    """The origin (scheme, host, port) of an address, rebuilt from checked parts; None if unusable.

    The result goes into a Content-Security-Policy header, so it is never
    copied from the text: a host of letters, digits, dots and hyphens (or an
    IPv6 address) and a numeric port. Anything else (a semicolon that would
    start another directive, a space, a comma, a bad port) gives None.
    """

    text = str(base_url or "").strip()
    if not text:
        return None
    parts = urlsplit(text)
    host = parts.hostname or ""
    try:
        port = parts.port
    except ValueError:
        return None
    if (
        parts.scheme not in {"http", "https"}
        or parts.username
        or parts.password
        or not (_HOST_NAME.fullmatch(host) or _is_ipv6(host))
    ):
        return None
    shown_host = f"[{host}]" if _is_ipv6(host) else host
    return f"{parts.scheme}://{shown_host}" + (f":{port}" if port is not None else "")


def _is_ipv6(host: str) -> bool:
    try:
        return isinstance(ipaddress.ip_address(host), ipaddress.IPv6Address)
    except ValueError:
        return False


__all__ = [
    "BROKER_PAGE_PREFIXES",
    "CARD_FRAME_SANDBOX",
    "CARD_VIEW_PARAM",
    "CARD_VIEW_VALUE",
    "SIGN_IN_FINAL_STATUSES",
    "SIGN_IN_STATUS_DECLINED",
    "SIGN_IN_STATUS_ERROR",
    "SIGN_IN_STATUS_SUCCESS",
    "card_view_url",
    "policy_origin",
]
