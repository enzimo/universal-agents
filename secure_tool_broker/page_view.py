"""Which view of a broker page a request gets, and the headers that protect it.

A broker page (``defaults.BROKER_PAGE_PREFIXES``) has two views at one address:

- the **full page**, opened in its own browser tab. Nothing may frame it
  (``frame-ancestors 'none'`` and ``X-Frame-Options: DENY``);
- the **card view** (``?view=card``), which Web Chat shows inside the
  conversation in a sandboxed iframe. Only the Web Chat's own address may frame
  it, and its Content-Security-Policy sandboxes the document itself
  (``defaults.CARD_FRAME_SANDBOX``, never ``allow-same-origin``). Behind a reverse
  proxy the broker shares the chat's address; the opaque origin is what keeps
  any page on that address, the chat included, from reading what a person types,
  however the page was framed.

The card view exists only when the broker knows the Web Chat's address
(``web_chat_origin``, derived from the existing public-address settings);
otherwise every request gets the full page. Both views send
``Referrer-Policy: no-referrer``, so a link out of a page never carries the
page's private address.

The view is kept in a ``ContextVar`` for the request, so the page renderers
(``auth_pages``) ask ``current_view()`` instead of taking a parameter.
aiohttp runs each request in its own task, and ``asyncio.to_thread`` copies the
context, so one request's view never leaks into another.

A sign-in page's own status route (``defaults.AUTH_PAGE_STATUS_ROUTE``) is read
by the page's script with ``fetch``. Inside the card the document's origin is
opaque, so the browser sends ``Origin: null`` and lets the script read only an
answer that allows that origin. The middleware adds
``Access-Control-Allow-Origin: null`` to those answers, raised errors
included, without credentials; the route still needs the page's private link
and returns no secret.
"""

from __future__ import annotations

import ipaddress
import logging
import re
from collections.abc import Awaitable, Callable
from contextvars import ContextVar
from dataclasses import dataclass
from urllib.parse import urlsplit

from aiohttp import web

from secure_tool_broker.defaults import (
    AUTH_PAGE_STATUS_ROUTE,
    BROKER_PAGE_PREFIXES,
    CARD_FRAME_SANDBOX,
    CARD_VIEW_PARAM,
    CARD_VIEW_VALUE,
)

LOGGER = logging.getLogger(__name__)

FULL_PAGE_CSP = "frame-ancestors 'none'"
# A host name or IPv4 address as it may appear in a Content-Security-Policy source.
_HOST_NAME = re.compile(r"[a-z0-9](?:[a-z0-9.-]{0,251}[a-z0-9])?")
# The browser sends this Origin for a request from a sandboxed (opaque-origin) document.
OPAQUE_ORIGIN = "null"


@dataclass(frozen=True)
class PageView:
    """The view one request gets; ``parent_origin`` is the only address that may frame a card."""

    card: bool = False
    parent_origin: str | None = None


FULL_PAGE = PageView()
_CURRENT_VIEW: ContextVar[PageView] = ContextVar("broker_page_view", default=FULL_PAGE)


def current_view() -> PageView:
    """The view of the page being rendered; the full page outside a page request."""

    return _CURRENT_VIEW.get()


def web_chat_origin(base_url: str | None) -> str | None:
    """The origin (scheme, host, port) of the Web Chat's public base address, or None if unusable.

    The origin goes into a Content-Security-Policy header, so it is rebuilt from
    checked parts, never copied: a host of letters, digits, dots and hyphens (or
    an IPv6 address) and a numeric port. Anything else (a semicolon that would
    start another directive, a space, a comma, a bad port) turns the card view
    off with a warning instead of changing the policy.
    """

    text = str(base_url or "").strip()
    if not text:
        return None
    parts = urlsplit(text)
    host = parts.hostname or ""
    try:
        port = parts.port
    except ValueError:
        port = None
        host = ""
    if (
        parts.scheme not in {"http", "https"}
        or parts.username
        or parts.password
        or not (_HOST_NAME.fullmatch(host) or _is_ipv6(host))
    ):
        LOGGER.warning(
            "The Web Chat address is not a plain http(s) address; broker pages will not show inside the chat. "
            "event=broker_card_view_origin_unusable",
        )
        return None
    shown_host = f"[{host}]" if _is_ipv6(host) else host
    return f"{parts.scheme}://{shown_host}" + (f":{port}" if port is not None else "")


def _is_ipv6(host: str) -> bool:
    try:
        return isinstance(ipaddress.ip_address(host), ipaddress.IPv6Address)
    except ValueError:
        return False


def card_view_headers(parent_origin: str) -> dict[str, str]:
    """Headers of a card view response: framed only by Web Chat, always sandboxed."""

    return {
        "Content-Security-Policy": f"frame-ancestors {parent_origin}; sandbox {CARD_FRAME_SANDBOX}",
        "Referrer-Policy": "no-referrer",
    }


def full_page_headers() -> dict[str, str]:
    """Headers of a full page response: nobody may frame it."""

    return {
        "Content-Security-Policy": FULL_PAGE_CSP,
        "X-Frame-Options": "DENY",
        "Referrer-Policy": "no-referrer",
    }


def _is_status_route(request: web.Request) -> bool:
    route = request.match_info.route
    resource = route.resource if route is not None else None
    return resource is not None and resource.canonical == AUTH_PAGE_STATUS_ROUTE


def _status_cors_headers(request: web.Request) -> dict[str, str]:
    if request.headers.get("Origin") != OPAQUE_ORIGIN:
        return {"Vary": "Origin"}
    return {"Access-Control-Allow-Origin": OPAQUE_ORIGIN, "Vary": "Origin"}


Handler = Callable[[web.Request], Awaitable[web.StreamResponse]]
ErrorPage = Callable[[web.HTTPException], web.Response]


def page_view_middleware(parent_origin: str | None, *, error_page: ErrorPage) -> Callable[..., Awaitable[web.StreamResponse]]:
    """The middleware for broker pages: pick the view, add its headers, show errors as pages.

    ``parent_origin`` is the Web Chat's origin, or None when the card view is
    off. ``error_page`` renders an HTTP error (status 400 or above) of a page as
    a broker page, so a card or tab shows the reason in the page's own style
    instead of raw text. The status route keeps its plain errors: scripts and
    the gateway read it.
    """

    @web.middleware
    async def middleware(request: web.Request, handler: Handler) -> web.StreamResponse:
        if not request.path.startswith(BROKER_PAGE_PREFIXES):
            return await handler(request)
        if _is_status_route(request):
            return await _status_answer(request, handler, card_view_on=parent_origin is not None)
        card = parent_origin is not None and request.query.get(CARD_VIEW_PARAM) == CARD_VIEW_VALUE
        view = PageView(card=True, parent_origin=parent_origin) if card else FULL_PAGE
        headers = card_view_headers(parent_origin) if card and parent_origin else full_page_headers()
        token = _CURRENT_VIEW.set(view)
        try:
            try:
                response = await handler(request)
            except web.HTTPException as exc:
                if exc.status < 400:
                    exc.headers.update(headers)
                    raise
                response = error_page(exc)
            response.headers.update(headers)
            return response
        finally:
            _CURRENT_VIEW.reset(token)

    return middleware


async def _status_answer(request: web.Request, handler: Handler, *, card_view_on: bool) -> web.StreamResponse:
    """The status route's answer, readable from the card when the card view is on."""

    headers = _status_cors_headers(request) if card_view_on else {}
    try:
        response = await handler(request)
    except web.HTTPException as exc:
        exc.headers.update(headers)
        raise
    response.headers.update(headers)
    return response


__all__ = [
    "FULL_PAGE",
    "PageView",
    "card_view_headers",
    "current_view",
    "full_page_headers",
    "page_view_middleware",
    "web_chat_origin",
]
