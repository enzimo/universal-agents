"""Keep each child process's copies of saved settings current across in-app restarts.

The headless launcher hands every child environment variables derived from
the saved instance configuration (``build_instance_env_defaults``: model,
embedding, website, broker and more), and children treat those variables as
overrides of the file (``runtime_settings.environment``,
``assistant_gateway.deployment_environment``). Restarts reuse each child's
environment, so before this, a setting saved while the runtime ran (TeamLead's
model change, the gateway's configuration editor) was overridden by the value
from launch time even after the restart that was meant to apply it.

On every in-app restart the supervisor therefore recomputes these variables
from the saved file and replaces only the ones that still hold their launch-
time value from the file. Left as they are: variables the operator set
(environment, env file or Docker secrets), the deployment's own details (the
Docker bundle's NATS and PostgreSQL, which it sets in the launcher's
environment), values the launcher adjusted after deriving them (local NATS,
website address), and every listed secret, so a restart never hands a process
a secret it did not start with.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Mapping

from datetime import datetime, timezone

from assistant_gateway.config import GatewayInstanceConfig
from custom_messages.contracts.status_v1 import EnvironmentOverridesV1, OverrideOrigin, OverrideSourceV1
from env_secrets import FILE_SUFFIX, LISTED_VARIABLES
from model_providers.endpoints import MODEL_KEY_ENV_VARS

LOGGER = logging.getLogger("docker.settings_environment")

DefaultsBuilder = Callable[..., dict[str, str]]


def _is_listed_secret(key: str, renamed: frozenset[str]) -> bool:
    return key in renamed or any(entry.covers(key) for entry in LISTED_VARIABLES)


@dataclass(frozen=True)
class SettingsEnvironment:
    """Which variables carry saved settings, and how to recompute them from the saved file."""

    config_path: Path
    defaults: DefaultsBuilder
    derived_keys: frozenset[str]
    launch_default_keys: frozenset[str]
    operator_keys: frozenset[str]
    secret_names: frozenset[str]

    @classmethod
    def at_launch(
        cls,
        config: GatewayInstanceConfig,
        *,
        defaults: DefaultsBuilder,
        launch_env: Mapping[str, str],
        operator_keys: frozenset[str],
        secret_names: frozenset[str] = frozenset(),
    ) -> "SettingsEnvironment":
        """Record, at launch, which variables are unchanged copies of saved settings."""

        launch_defaults = defaults(config, env=launch_env)
        derived = frozenset(
            key
            for key, value in launch_defaults.items()
            if key not in operator_keys
            and launch_env.get(key) == value
            and not _is_listed_secret(key, secret_names)
        )
        return cls(
            config_path=config.config_path,
            defaults=defaults,
            derived_keys=derived,
            launch_default_keys=frozenset(launch_defaults),
            operator_keys=operator_keys,
            secret_names=secret_names,
        )

    def _current_defaults(self, env: Mapping[str, str]) -> dict[str, str]:
        # The copies being recomputed are not deployment details: applying them
        # (load_for_process) would put their launch-time values back.
        deployment = {key: value for key, value in env.items() if key not in self.derived_keys}
        config = GatewayInstanceConfig.load_for_process(self.config_path, env=deployment)
        return self.defaults(config, env=deployment)

    def refresh(self, env: Mapping[str, str], *, process: str) -> dict[str, str]:
        """``env`` with its copies of saved settings recomputed from the saved file now."""

        try:
            current = self._current_defaults(env)
        except Exception as exc:
            # The restarted process reads the same file and reports the fault
            # itself; starting it with its previous variables loses nothing.
            LOGGER.error(
                "Could not recompute settings from the saved instance config; the process restarts "
                "with its previous environment. event=process_settings_environment_refresh_failed "
                "process=%s path=%s error_type=%s error=%s",
                process,
                self.config_path,
                exc.__class__.__name__,
                exc,
            )
            return dict(env)
        refreshed = dict(env)
        for key in self.derived_keys & env.keys():
            if key in current:
                refreshed[key] = current[key]
            else:
                refreshed.pop(key)
        for key in current.keys() - self.launch_default_keys:
            if key not in self.operator_keys and not _is_listed_secret(key, self.secret_names):
                refreshed[key] = current[key]
        changed = sorted(key for key in refreshed.keys() | env.keys() if refreshed.get(key) != env.get(key))
        LOGGER.info(
            "Recomputed a restarted process's settings from the saved instance config. "
            "event=process_settings_environment_refreshed process=%s changed=%s",
            process,
            ",".join(changed) or "-",
        )
        return refreshed


def environment_overrides(
    settings: SettingsEnvironment,
    *,
    config: GatewayInstanceConfig,
    launch_env: Mapping[str, str],
    env_file: Path | None = None,
    env_file_values: Mapping[str, str] | None = None,
    container_env: Mapping[str, str] | None = None,
) -> EnvironmentOverridesV1:
    """Which variables the operator set that override a saved setting or a saved model key, and where (names only).

    A variable overrides a saved setting when the operator set it (team.env
    or the container environment) and its value differs from what the saved
    instance configuration gives. Such a value wins until the container
    restarts, also over changes saved later, which is why the Status page
    shows it. ``env_file``, ``env_file_values`` and ``container_env`` let it
    say where each one came from (``override_sources``).
    """

    defaults = settings.defaults(config, env=launch_env)
    overriding = sorted(
        key
        for key, value in defaults.items()
        if key in settings.operator_keys
        and not _is_listed_secret(key, settings.secret_names)
        and str(launch_env.get(key, "")) != value
    )
    model_keys = sorted(
        name
        for name in MODEL_KEY_ENV_VARS
        if str(launch_env.get(name) or "").strip() or str(launch_env.get(f"{name}{FILE_SUFFIX}") or "").strip()
    )
    sources = {
        name: override_sources(name, env_file=env_file, env_file_values=env_file_values or {}, container_env=container_env or {})
        for name in (*overriding, *model_keys)
    }
    return EnvironmentOverridesV1(
        written_at=datetime.now(timezone.utc),
        setting_variables=tuple(overriding),
        model_key_variables=tuple(model_keys),
        sources={name: found for name, found in sources.items() if found},
    )


def override_sources(
    name: str,
    *,
    env_file: Path | None,
    env_file_values: Mapping[str, str],
    container_env: Mapping[str, str],
) -> tuple[OverrideSourceV1, ...]:
    """Every place ``name`` (or its ``_FILE`` form) was set: the container's environment first, as it wins."""

    found: list[OverrideSourceV1] = []
    for origin, values in (
        (OverrideOrigin.CONTAINER_ENVIRONMENT, container_env),
        (OverrideOrigin.ENV_FILE, env_file_values),
    ):
        where = str(env_file) if origin is OverrideOrigin.ENV_FILE and env_file is not None else ""
        if str(values.get(name) or "").strip():
            found.append(OverrideSourceV1(origin=origin, variable=name, path=where))
        secret_path = str(values.get(f"{name}{FILE_SUFFIX}") or "").strip()
        if secret_path:
            # The _FILE form names a file; its path is not a secret, its contents are never read here.
            found.append(OverrideSourceV1(origin=OverrideOrigin.SECRET_FILE, variable=f"{name}{FILE_SUFFIX}", path=secret_path))
    return tuple(found)


def write_environment_overrides(path: Path, overrides: EnvironmentOverridesV1) -> None:
    """Save the override names for the gateway; a failure is logged and never stops the launch."""

    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        temporary = path.with_suffix(".tmp")
        temporary.write_text(overrides.model_dump_json(), encoding="utf-8")
        temporary.chmod(0o600)
        temporary.replace(path)
    except OSError as exc:
        LOGGER.error(
            "Could not save which environment variables override saved settings; the Status page will not "
            "show them. event=environment_overrides_write_failed path=%s error=%s",
            path,
            exc,
        )
        return
    LOGGER.info(
        "Saved which environment variables override saved settings. event=environment_overrides_written "
        "settings=%s model_keys=%s",
        ",".join(overrides.setting_variables) or "-",
        ",".join(overrides.model_key_variables) or "-",
    )


__all__ = ["SettingsEnvironment", "environment_overrides", "write_environment_overrides"]
