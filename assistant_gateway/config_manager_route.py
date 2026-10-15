"""Configuration-edge persistence for the discovered manager route."""

from __future__ import annotations

import logging

from assistant_gateway.config import GatewayInstanceConfig

logger = logging.getLogger(__name__)


class ConfigGatewayManagerRouteWriter:
    """Persist manager route changes without exposing aggregate config to the client.

    The gateway runs with the saved settings plus its deployment's details
    applied in memory (``GatewayInstanceConfig.load_for_process``), loaded when
    it started. Saving that object would write those details into the saved
    file and undo every setting saved since the gateway started, so only the
    route is changed, in the saved file as it is now.
    """

    def __init__(self, config: GatewayInstanceConfig) -> None:
        self._config = config

    def __call__(self, manager_id: str | None) -> None:
        self._config.manager.manager_agent_id = manager_id
        path = self._config.config_path
        if not path.is_file():
            # Only a gateway built in memory has no saved file; a launched one
            # always starts from it. Its settings are not written out.
            logger.warning(
                "No saved instance config to record the manager route in; it is kept in memory. "
                "event=gateway_manager_route_not_saved path=%s manager_agent_id=%s",
                path,
                manager_id,
            )
            return
        saved = GatewayInstanceConfig.load_saved(path)
        saved.manager.manager_agent_id = manager_id
        saved.save()
