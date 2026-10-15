"""Loopback browser review for an operator's transfer, with no secret values in the page."""

from __future__ import annotations

import asyncio
import hmac
import secrets
import webbrowser
from pathlib import Path

from aiohttp import web

from team_transfer.errors import TransferError
from team_transfer.review import ReviewFiles
from team_transfer.review_session import ReviewApplyV1, ReviewSession, ReviewUpdateV1

ASSETS = Path(__file__).resolve().parent / "review_assets"
SHARED_ASSETS = (
    Path(__file__).resolve().parents[1]
    / "assistant_gateway"
    / "onboarding_web"
    / "assets"
)


def build_review_app(files: ReviewFiles, token: str) -> web.Application:
    """The fragment token never enters request URLs, cookies, logs, or storage."""
    session = ReviewSession(files)

    @web.middleware
    async def private_response(request, handler):
        if request.path.startswith("/api/"):
            if request.host.split(":", 1)[0] not in {"127.0.0.1", "localhost"}:
                raise web.HTTPForbidden(
                    text="Open this review on the local operator host."
                )
            if not hmac.compare_digest(
                request.headers.get("X-Transfer-Token", ""), token
            ):
                raise web.HTTPForbidden(
                    text="Open the review link printed by the operator command."
                )
            # No cookies or CORS. The secret header also prevents form-based
            # cross-site requests, and an explicit Origin rejects DNS rebinding.
            origin = request.headers.get("Origin")
            if origin and origin != f"http://{request.host}":
                raise web.HTTPForbidden(
                    text="The review request came from another site."
                )
        try:
            response = await handler(request)
        except (TransferError, ValueError, OSError) as exc:
            response = web.json_response({"error": str(exc)}, status=400)
        response.headers["Cache-Control"] = "no-store"
        response.headers["Referrer-Policy"] = "no-referrer"
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["Content-Security-Policy"] = (
            "default-src 'self'; connect-src 'self'; img-src 'self'; style-src 'self'; "
            "script-src 'self'; object-src 'none'; frame-ancestors 'none'; base-uri 'none'"
        )
        return response

    app = web.Application(middlewares=[private_response], client_max_size=5 * 1024**2)

    async def state(request):
        return web.json_response(session.state())

    async def update(request):
        payload = ReviewUpdateV1.model_validate(await request.json())
        return web.json_response(await session.update(payload))

    async def validate(request):
        return web.json_response(await session.validate())

    async def apply(request):
        payload = ReviewApplyV1.model_validate(await request.json())
        result = await session.apply(payload)
        stored = all(outcome.get("imported") for outcome in result["credentials"])
        return web.json_response(
            {
                **result,
                "message": "Selected records imported. You can choose another batch when ready."
                if stored
                else "Settings imported. Some credentials were not stored; see the reasons below. Start the broker before importing credentials.",
            }
        )

    async def asset(request):
        name = request.match_info["name"]
        # The review script and styles are shared with onboarding's Import page.
        shared = {
            "styles.css",
            "appearance.js",
            "metallic_mesh.js",
            "agent-mark.png",
            "favicon.ico",
            "team_review.css",
            "team_review.js",
        }
        if name not in shared | {"review.html"}:
            raise web.HTTPNotFound()
        return web.FileResponse((SHARED_ASSETS if name in shared else ASSETS) / name)

    async def page(request):
        return web.FileResponse(ASSETS / "review.html")

    app.router.add_get("/", page)
    app.router.add_get("/assets/{name}", asset)
    app.router.add_get("/api/review", state)
    app.router.add_post("/api/record", update)
    app.router.add_post("/api/validate", validate)
    app.router.add_post("/api/import", apply)
    return app


async def serve_review(files: ReviewFiles):
    token = secrets.token_urlsafe(32)
    runner = web.AppRunner(build_review_app(files, token), access_log=None)
    await runner.setup()
    site = web.TCPSite(runner, "127.0.0.1", 0)
    await site.start()
    port = site._server.sockets[0].getsockname()[1]
    url = f"http://127.0.0.1:{port}/#{token}"
    print(
        f"Open {url}\nReview files: {files.root}\nPress Ctrl+C to close the review page.",
        flush=True,
    )
    webbrowser.open(url)
    try:
        await asyncio.Event().wait()
    finally:
        await runner.cleanup()
