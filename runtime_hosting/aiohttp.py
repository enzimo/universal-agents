"""Runtime-neutral aiohttp hosting helpers for small services."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from aiohttp import web


_UNSET = object()


@dataclass
class AiohttpSite:
    runner: Any
    site: Any
    host: str
    port: int
    scheme: str = "http"

    @property
    def base_url(self) -> str:
        return format_listen_url(self.host, self.port, scheme=self.scheme)

    async def stop(self) -> None:
        await self.site.stop()
        await self.runner.cleanup()

    async def cleanup(self) -> None:
        await self.runner.cleanup()


def format_listen_url(host: str, port: int, *, scheme: str = "http") -> str:
    rendered_host = str(host or "").strip() or "127.0.0.1"
    if ":" in rendered_host and not rendered_host.startswith("["):
        rendered_host = f"[{rendered_host}]"
    return f"{scheme}://{rendered_host}:{int(port)}"


async def start_aiohttp_site(
    app: Any,
    *,
    host: str,
    port: int,
    access_log: Any = _UNSET,
    web_module: Any = web,
) -> AiohttpSite:
    runner_kwargs: dict[str, Any] = {}
    if access_log is not _UNSET:
        runner_kwargs["access_log"] = access_log
    runner = web_module.AppRunner(app, **runner_kwargs)
    await runner.setup()
    site = web_module.TCPSite(runner, host=host, port=port)
    await site.start()
    return AiohttpSite(runner=runner, site=site, host=host, port=int(port))
