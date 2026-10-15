"""Outer adapter for reloadable gateway delivery preferences."""

from __future__ import annotations

import logging

from assistant_gateway.application.proactive_delivery import GatewayProactiveTarget
from assistant_gateway.config import GatewayInstanceConfig
from assistant_gateway.delivery import (
    GatewayDeliveryPolicy,
    GatewayQuietHoursPolicy,
)


class ConfigGatewayDeliveryPreferences:
    """Reload delivery settings and present application-owned target values."""

    def __init__(
        self,
        *,
        config: GatewayInstanceConfig,
        logger: logging.Logger | None = None,
    ) -> None:
        self._config = config
        self._logger = logger or logging.getLogger(__name__)
        self._config_file_mtime_ns = self._read_config_file_mtime_ns()

    def refresh(self) -> None:
        """Reload delivery settings when the instance configuration changes."""

        config_path = self._config.config_path
        try:
            if not config_path.exists():
                return
            mtime_ns = config_path.stat().st_mtime_ns
            if self._config_file_mtime_ns == mtime_ns:
                return
            latest = GatewayInstanceConfig.load_for_process(config_path)
            self._config.delivery = latest.delivery
            self._config_file_mtime_ns = mtime_ns
        except Exception as exc:
            self._logger.warning(
                "Gateway failed to refresh delivery preferences. "
                "event=gateway_delivery_preferences_refresh_failed "
                "config_path=%s error=%s",
                config_path,
                exc,
            )

    def current_proactive_target(self) -> GatewayProactiveTarget:
        """Return the latest preferred proactive route as a narrow DTO."""

        self.refresh()
        target = self._config.delivery.default_target
        return GatewayProactiveTarget(
            channel_name=target.channel_name,
            chat_id=target.chat_id,
            thread_id=target.thread_id or None,
        )

    def current_delivery_policy(self) -> GatewayDeliveryPolicy:
        """Return the latest delivery policy as an application-owned snapshot."""

        self.refresh()
        delivery = self._config.delivery
        quiet_hours = delivery.quiet_hours
        return GatewayDeliveryPolicy(
            quiet_hours=GatewayQuietHoursPolicy(
                enabled=quiet_hours.enabled,
                start_hour_local=quiet_hours.start_hour_local,
                end_hour_local=quiet_hours.end_hour_local,
                allow_urgent_delivery=quiet_hours.allow_urgent_delivery,
            ),
            digest_delivery_hour_local=delivery.digest_delivery_hour_local,
            urgent_keywords=tuple(delivery.urgent_keywords),
        )

    def _read_config_file_mtime_ns(self) -> int | None:
        try:
            config_path = self._config.config_path
            if not config_path.exists():
                return None
            return config_path.stat().st_mtime_ns
        except OSError:
            return None
