"""One bounded page size for browser list endpoints."""

from aiohttp import web

WEB_LIST_PAGE_SIZE = 20


def list_page_query(request: web.Request) -> dict[str, int]:
    try:
        offset = int(request.query.get("offset", "0"))
        limit = int(request.query.get("limit", str(WEB_LIST_PAGE_SIZE)))
    except ValueError as exc:
        raise web.HTTPBadRequest(text="offset and limit must be integers.") from exc
    if offset < 0 or limit < 1:
        raise web.HTTPBadRequest(text="offset must be nonnegative and limit positive.")
    return {"offset": offset, "limit": min(limit, WEB_LIST_PAGE_SIZE)}
