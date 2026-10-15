"""HTTP adapter for gateway activity-notice polling."""

from __future__ import annotations

from typing import Any

import aiohttp

from assistant_gateway.config import (
    GatewayInstanceConfig,
    resolve_activity_collector_runtime_base_url,
)


class HttpGatewayActivitySource:
    """Fetch activity without access to gateway state or delivery policy."""

    def __init__(
        self,
        *,
        base_url: str | None,
        token: str | None,
        connect_timeout_seconds: float,
        request_timeout_seconds: float,
    ) -> None:
        self._base_url = str(base_url or "").rstrip("/") or None
        self._token = str(token or "").strip() or None
        self._timeout = aiohttp.ClientTimeout(
            total=request_timeout_seconds,
            connect=connect_timeout_seconds,
        )
        self._session: aiohttp.ClientSession | None = None

    @classmethod
    def from_gateway_config(
        cls,
        config: GatewayInstanceConfig,
    ) -> "HttpGatewayActivitySource":
        settings = config.activity_collector
        return cls(
            base_url=resolve_activity_collector_runtime_base_url(config),
            token=settings.resolve_user_token(),
            connect_timeout_seconds=settings.connect_timeout_seconds,
            request_timeout_seconds=settings.request_timeout_seconds,
        )

    @property
    def enabled(self) -> bool:
        return bool(self._base_url and self._token)

    async def fetch_recent_activity(self) -> list[dict[str, Any]]:
        if not self.enabled:
            return []
        if self._session is None or self._session.closed:
            self._session = aiohttp.ClientSession(timeout=self._timeout)
        async with self._session.get(
            f"{self._base_url}/api/user/recent-activity",
            params={"token": self._token},
        ) as response:
            if response.status >= 300:
                raise RuntimeError(
                    f"activity collector returned HTTP {response.status}"
                )
            payload = await response.json()
        if not isinstance(payload, dict):
            raise RuntimeError("activity collector returned a non-object response")
        recent_activity = payload.get("recent_activity")
        return list(recent_activity) if isinstance(recent_activity, list) else []

    async def close(self) -> None:
        if self._session is not None:
            await self._session.close()
            self._session = None
