"""The saved runtime configuration, read on every call.

Kept apart from the launch service so settings readers (scheduled workers,
worker pools, readiness) can use it without importing the launch service.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from config_loader import Config


def current_launch_config(agent: Any) -> Config:
    """The saved instance configuration with this deployment's details, read on every call.

    Scheduled-worker settings (eligible types, denies, the kill switch, limits,
    default and per-type rules) change while the runtime runs, through
    TeamLead's runtime settings or the operator's configuration editor, and
    take effect on the next check with no restart. Launches use the same
    reading, so a worker starts with the settings saved now
    (``GatewayInstanceConfig.load_for_process``). Never mutates globals.
    """
    from config_loader import get_config

    if getattr(agent, "instance_config_path", None):
        from assistant_gateway.config import GatewayInstanceConfig
        from assistant_gateway.runtime_config import (
            build_runtime_config_from_instance_config,
        )

        return build_runtime_config_from_instance_config(
            GatewayInstanceConfig.load_for_process(agent.instance_config_path)
        )
    return get_config()


__all__ = ["current_launch_config"]
