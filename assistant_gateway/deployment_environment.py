"""Deployment details applied in memory to the saved instance configuration.

Owner's decision (2026-09-29): every process runs from one saved settings
file, ``<instance_root>/instance_config.yaml``. What a deployment supplies for
its own services (the Docker bundle's NATS server, an operator's NATS
credentials, the public origin, the website's listener) arrives as environment
variables and is applied here, when a process loads its configuration
(``GatewayInstanceConfig.load_for_process``). Nothing here is ever saved: code
that writes settings loads the file with ``GatewayInstanceConfig.load_saved``
and changes only its own fields. The bundle's PostgreSQL address is applied by
the storage resolvers (``storage.postgres_connection.BUNDLED_POSTGRES_ADDRESS_ENV``),
so every connection string is covered however it is read.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Mapping

from env_secrets import get_env_with_file_fallback
from runtime_settings.network import reject_removed_public_url_env_vars

if TYPE_CHECKING:
    from assistant_gateway.config import GatewayInstanceConfig

_TRUE_VALUES = frozenset({"1", "true", "yes", "on"})


def _flag(env: Mapping[str, str], name: str) -> bool | None:
    raw = str(env.get(name) or "").strip()
    return raw.lower() in _TRUE_VALUES if raw else None


def _apply_transport(config: "GatewayInstanceConfig", env: Mapping[str, str]) -> None:
    transport = config.transport
    transport.type = "nats"
    if env.get("NATS_URL"):
        transport.nats_url = env["NATS_URL"]
        transport.nats_servers = [transport.nats_url]
    if env.get("NATS_SERVERS"):
        transport.nats_servers = [item.strip() for item in env["NATS_SERVERS"].split(",") if item.strip()]
    if env.get("NATS_NAME"):
        transport.nats_name = env["NATS_NAME"]
    if env.get("NATS_SUBJECT_PREFIX"):
        transport.subject_prefix = env["NATS_SUBJECT_PREFIX"]
    jetstream = _flag(env, "NATS_JETSTREAM_ENABLED")
    if jetstream is not None:
        transport.jetstream_enabled = jetstream
    if env.get("NATS_ACK_WAIT_SECONDS"):
        transport.ack_wait_seconds = float(env["NATS_ACK_WAIT_SECONDS"])
    if env.get("NATS_MAX_DELIVER"):
        transport.max_deliver = int(env["NATS_MAX_DELIVER"])
    if env.get("NATS_PROCESS_CONSUMER_INACTIVE_THRESHOLD_SECONDS"):
        transport.process_consumer_inactive_threshold_seconds = float(
            env["NATS_PROCESS_CONSUMER_INACTIVE_THRESHOLD_SECONDS"]
        )
    # Secrets come from the environment or Docker secret files, never the file.
    if user := get_env_with_file_fallback("NATS_USER", env=env):
        transport.nats_user = user
    if password := get_env_with_file_fallback("NATS_PASSWORD", env=env):
        transport.nats_password = password
    if env.get("NATS_CREDENTIALS_FILE"):
        transport.credentials_file = env["NATS_CREDENTIALS_FILE"]


def _apply_website(config: "GatewayInstanceConfig", env: Mapping[str, str]) -> None:
    website = config.interactive_apps.internal_website
    enabled = _flag(env, "INTERNAL_WEBSITE_ENABLED")
    if enabled is not None:
        website.enabled = enabled
    if env.get("INTERNAL_WEBSITE_BIND_HOST"):
        website.bind_host = env["INTERNAL_WEBSITE_BIND_HOST"]
    if env.get("INTERNAL_WEBSITE_BIND_PORT"):
        website.bind_port = int(env["INTERNAL_WEBSITE_BIND_PORT"])
    if "INTERNAL_WEBSITE_PUBLIC_PORT" in env:
        raw_port = str(env.get("INTERNAL_WEBSITE_PUBLIC_PORT") or "").strip()
        website.public_port = int(raw_port) if raw_port else None
    if env.get("INTERNAL_WEBSITE_DOCS_ROOT"):
        website.docs_root = env["INTERNAL_WEBSITE_DOCS_ROOT"]
    if env.get("INTERNAL_WEBSITE_SKILLS_DIR"):
        website.skills_dir = env["INTERNAL_WEBSITE_SKILLS_DIR"]


def apply_deployment_environment(
    config: "GatewayInstanceConfig", env: Mapping[str, str]
) -> "GatewayInstanceConfig":
    """The configuration with the deployment's own details from ``env``, changed in place and returned."""

    reject_removed_public_url_env_vars(env)
    _apply_transport(config, env)
    if env.get("SECURE_TOOL_BROKER_INTERNAL_BASE_URL"):
        config.security_layer.broker_internal_base_url = env["SECURE_TOOL_BROKER_INTERNAL_BASE_URL"]
    if env.get("NETWORK_PUBLIC_ORIGIN"):
        config.network.public_origin = env["NETWORK_PUBLIC_ORIGIN"]
    _apply_website(config, env)
    return config


__all__ = ["apply_deployment_environment"]
