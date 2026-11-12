"""Start a gateway with the built-in Website app on a free loopback port, for live scenarios.

Kept apart from ``live_gateway_scenarios`` so unit tests can import it: that script configures
logging when it is imported.
"""

from __future__ import annotations

import contextlib
import errno
import logging
import socket
from collections.abc import AsyncIterator, Awaitable, Callable

from assistant_gateway.config import GatewayInstanceConfig
from assistant_gateway.gateway import UserGateway
from assistant_gateway.interactive_apps.manager import InteractiveAppManager

logger = logging.getLogger(__name__)

#: How many free ports a start tries before it gives up.
WEBSITE_PORT_ATTEMPTS = 3


def available_loopback_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as listener:
        listener.bind(("127.0.0.1", 0))
        return int(listener.getsockname()[1])


async def _stop_if_running(app_manager: InteractiveAppManager) -> None:
    if app_manager.has_running_apps:
        await app_manager.stop()


@contextlib.asynccontextmanager
async def gateway_with_website(
    config: GatewayInstanceConfig, *, ready: Callable[[UserGateway], Awaitable[None]]
) -> AsyncIterator[tuple[UserGateway, InteractiveAppManager, int]]:
    """Yield a connected gateway, its running Website app and the port the app serves on.

    ``UserGateway`` copies the Website address into its Web Chat and access Inbox links
    when it is built, so each attempt sets the port first, then builds the gateway, awaits
    ``ready(gateway)`` (a live scenario waits for the manager's route there), and starts
    the app. The port is picked free and closed before the app binds it, so another program
    can take it in between; the app binds in this process and fails with "address already
    in use" rather than answering for another server. That attempt's gateway is then
    closed and both start again on another port, so the links always name the port the
    app serves on.

    However the block ends, the app is stopped if it still runs and the gateway is closed.
    """

    website_settings = config.interactive_apps.internal_website
    for attempt in range(1, WEBSITE_PORT_ATTEMPTS + 1):
        port = available_loopback_port()
        website_settings.bind_port = port
        website_settings.public_port = port
        async with contextlib.AsyncExitStack() as stack:
            gateway = await stack.enter_async_context(UserGateway(config, output_sink=None))
            await ready(gateway)
            app_manager = InteractiveAppManager(config, gateway.application, include_apps=["website"])
            try:
                await app_manager.start()
            except OSError as error:
                if error.errno != errno.EADDRINUSE:
                    raise
                logger.warning(
                    "event=live_website_port_taken port=%s attempt=%s of=%s",
                    port,
                    attempt,
                    WEBSITE_PORT_ATTEMPTS,
                )
                continue
            stack.push_async_callback(_stop_if_running, app_manager)
            yield gateway, app_manager, port
            return
    raise AssertionError("Every port picked for the Website app was taken before it could listen.")
