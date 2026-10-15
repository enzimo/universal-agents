"""Configured secret variable names shared by launch and team transfer."""

from typing import Any

from default_values import ACTIVITY_COLLECTOR_DEFAULTS, INTERACTIVE_APP_DEFAULTS
from runtime_settings.interactive_apps import ADAPTER_HOST_TOKEN_ENV_VAR


def configured_secret_names(config: Any) -> dict[str, str]:
    """Secret variables whose name the configuration chooses, mapped to the listed name.

    A custom ``web_chat_token_env_var`` (or Photon, adapter-host or ingest
    token variable) must reach the same processes as the listed default it
    replaces (docker/secret_env_vars.yaml).
    """

    apps = config.interactive_apps
    photon = apps.photon
    chosen = (
        (
            apps.internal_website.web_chat_token_env_var,
            INTERACTIVE_APP_DEFAULTS.internal_website_web_chat_token_env_var.value,
        ),
        (apps.adapter_host.token_env_var, ADAPTER_HOST_TOKEN_ENV_VAR),
        (
            photon.project_id_env_var,
            INTERACTIVE_APP_DEFAULTS.photon_project_id_env_var.value,
        ),
        (
            photon.project_secret_env_var,
            INTERACTIVE_APP_DEFAULTS.photon_project_secret_env_var.value,
        ),
        (
            photon.sidecar_token_env_var,
            INTERACTIVE_APP_DEFAULTS.photon_sidecar_token_env_var.value,
        ),
        (
            config.activity_collector.ingest_token_env_var,
            ACTIVITY_COLLECTOR_DEFAULTS.ingest_token_env_var.value,
        ),
    )
    return {name: listed for name, listed in chosen if name and name != listed}
