"""Which secrets an instance needs and which of them the environment lacks.

Onboarding generates internal credentials into ``team.env`` when
``launch.secrets_source`` is ``env_file``. When it is ``environment`` (for
example Docker Swarm with every secret supplied as a Docker secret), onboarding
writes and generates nothing, so it instead checks that the environment holds
what this configuration needs and names anything missing. Both paths build the
credential request here (``internal_credential_request``) so the list of
required secrets matches what generation would have produced.

The list never includes secret values, only names, what each one is for, and
how to create it.
"""

from __future__ import annotations

from dataclasses import dataclass
import os
from pathlib import Path
import shlex
from typing import Iterable, Mapping
from urllib.parse import urlparse

from access_control.identity import ENV_GATEWAY_SIGNING_KEY, ENV_SCHEDULER_SIGNING_KEY, ENV_VERIFY_KEYS
from assistant_gateway.config import GatewayInstanceConfig
from env_secrets import get_env_with_file_fallback, materialize_secret_file_envs
from internal_credentials import (
    INTERNAL_ENV_KEYS,
    InternalCredentialRequest,
    generate_internal_credentials,
    internal_credential_names,
    merge_env_file,
    parse_env_file,
)

GENERATE_CREDENTIALS_MODULE = "scripts.generate_internal_credentials"
# Where the docs explain supplying secrets from the environment, including
# one-line commands for the two signing keys.
ENVIRONMENT_SECRETS_DOCS = "/docs/docker/#secrets-from-the-environment"

# Model keys are not environment secrets: they are saved per endpoint with
# /auth model (onboarding hands the tested key to the credential store), and
# the Status page lists a configured endpoint that has none.

# Generated names the environment does not have to hold: the supervisor derives
# the verification keys from the signing keys; the local message broker makes
# and stores its own user and password on first start when none are set
# (docker.nats_service.ensure_local_nats_credentials); and only Google
# Workspace sign-in needs the keyring password (the broker says so when it is
# missing).
_NOT_REQUIRED_INTERNAL_NAMES = frozenset(
    {ENV_VERIFY_KEYS, "NATS_USER", "NATS_PASSWORD", "GOG_KEYRING_PASSWORD"}
)
# Secrets a channel names but does not need to start: the Photon sidecar token
# is made fresh at each start when unset.
_OPTIONAL_CHANNEL_SECRETS = frozenset({"PHOTON_SIDECAR_TOKEN"})

_INTERNAL_PURPOSES: dict[str, str] = {
    ENV_GATEWAY_SIGNING_KEY: "The gateway signs whom each task works for with it.",
    ENV_SCHEDULER_SIGNING_KEY: "The scheduler signs whom each scheduled task works for with it.",
    "ACTIVITY_COLLECTOR_USER_TOKEN": "Opens the activity collector's user pages.",
    "ACTIVITY_COLLECTOR_ADMIN_TOKEN": "Opens the activity collector's admin pages.",
    "ACTIVITY_COLLECTOR_INGEST_TOKEN": "Lets agents send activity events to the collector.",
    "WEB_CHAT_AUTH_TOKEN": "The token people use to sign in to Web Chat.",
    "POSTGRES_DSN": "Connection string for the PostgreSQL database.",
    "SECURE_TOOL_BROKER_TOKEN": "The secure tool broker's secret; every process gets a token made from it.",
}


@dataclass(frozen=True)
class SecretRequirement:
    """One secret the environment must hold: its name, use and how to create it."""

    name: str
    purpose: str
    how_to_create: str

    def as_dict(self) -> dict[str, str]:
        return {"name": self.name, "purpose": self.purpose, "how_to_create": self.how_to_create}


def should_generate_internal_nats_credentials(config: GatewayInstanceConfig, env: Mapping[str, str]) -> bool:
    """Local NATS needs a generated user and password; an external one brings its own."""

    if env.get("NATS_CREDENTIALS_FILE"):
        return False
    explicit_servers = _split_nats_servers(env.get("NATS_SERVERS"))
    explicit_url = _split_nats_servers(env.get("NATS_URL"))
    if explicit_servers or explicit_url:
        endpoints = explicit_servers or explicit_url
    else:
        configured_servers = list(getattr(config.transport, "nats_servers", []) or [])
        configured_url = str(getattr(config.transport, "nats_url", "") or "").strip()
        endpoints = configured_servers or ([configured_url] if configured_url else [])
    return bool(endpoints) and all(_is_local_nats_endpoint(endpoint) for endpoint in endpoints)


def _split_nats_servers(raw: object) -> list[str]:
    return [item.strip() for item in str(raw or "").split(",") if item.strip()]


def _is_local_nats_endpoint(endpoint: str) -> bool:
    parsed = urlparse(str(endpoint).strip())
    host = parsed.hostname
    if host is None:
        host = str(endpoint).strip().split(":", 1)[0]
    return host.lower() in {"localhost", "127.0.0.1", "::1"}


def internal_credential_request(
    config: GatewayInstanceConfig,
    *,
    env: Mapping[str, str],
    existing_postgres_dsn: str | None = None,
    rotate: bool = False,
) -> InternalCredentialRequest:
    """The one description of which internal credentials this configuration uses.

    The broker token and the Google keyring password the broker uses are
    always included, because the launcher always starts the broker.
    """

    return InternalCredentialRequest(
        instance_name=config.instance_name,
        storage_backend=config.state.storage.backend,
        existing_postgres_dsn=existing_postgres_dsn,
        include_nats_credentials=should_generate_internal_nats_credentials(config, env),
        rotate=rotate,
    )


def environment_secret_values(env: Mapping[str, str] | None = None) -> dict[str, str]:
    """The process environment with Docker secret files (``*_FILE``) read in.

    The Docker entrypoint already reads them; running onboarding directly
    (without the entrypoint) gets the same view this way.
    """

    values = dict(os.environ if env is None else env)
    materialize_secret_file_envs(values)
    return values


def secret_present(name: str, env: Mapping[str, str]) -> bool:
    """Whether ``name`` or a readable ``name_FILE`` holds a non-empty value."""

    try:
        value = get_env_with_file_fallback(name, env=env)
    except (OSError, ValueError):
        return False
    return bool(str(value or "").strip())


def required_secrets(
    config: GatewayInstanceConfig,
    *,
    env: Mapping[str, str],
    entered_names: Iterable[str] = (),
) -> tuple[SecretRequirement, ...]:
    """Secrets this configuration needs from the environment, in a stable order.

    ``entered_names`` are secrets the person typed during onboarding; with
    secrets from the environment they are not saved, so they must be present
    there too.
    """

    requirements: dict[str, SecretRequirement] = {}
    for requirement in (
        *_internal_requirements(config, env),
        *_channel_requirements(config),
        *_entered_requirements(entered_names),
    ):
        requirements.setdefault(requirement.name, requirement)
    return tuple(requirements.values())


def missing_secrets(
    config: GatewayInstanceConfig,
    *,
    env: Mapping[str, str],
    entered_names: Iterable[str] = (),
) -> tuple[SecretRequirement, ...]:
    """The required secrets the environment does not hold (names only, never values)."""

    return tuple(
        requirement
        for requirement in required_secrets(config, env=env, entered_names=entered_names)
        if not secret_present(requirement.name, env)
    )


def missing_secrets_text(missing: Iterable[SecretRequirement]) -> str:
    """Plain lines naming each missing secret, what it is for, and how to create it."""

    lines = [
        f"- {item.name}: {item.purpose} {item.how_to_create} "
        f"Set {item.name}, or {item.name}_FILE to a mounted secret file."
        for item in missing
    ]
    return "\n".join(lines)


def generate_credentials_command(request: InternalCredentialRequest) -> str:
    """The script command that prints values for ``request`` without writing a file."""

    parts = ["python3", "-m", GENERATE_CREDENTIALS_MODULE, "--instance-name", request.instance_name]
    if str(request.storage_backend or "").strip().lower() in {"postgres", "postgresql"}:
        parts += ["--storage-backend", "postgres"]
    for flag, enabled in (
        ("--include-nats-credentials", request.include_nats_credentials),
    ):
        if enabled:
            parts.append(flag)
    parts.append("--print")
    return shlex.join(parts)


def _internal_requirements(config: GatewayInstanceConfig, env: Mapping[str, str]) -> list[SecretRequirement]:
    request = internal_credential_request(config, env=env)
    command = generate_credentials_command(request)
    how = f"Create it with `{command}` and store the printed value as a Docker secret."
    signing_how = f"{how} The docs ({ENVIRONMENT_SECRETS_DOCS}) also show a one-line command for each signing key."
    signing_keys = {ENV_GATEWAY_SIGNING_KEY, ENV_SCHEDULER_SIGNING_KEY}
    return [
        SecretRequirement(
            name=name,
            purpose=_INTERNAL_PURPOSES.get(name, "An internal service credential."),
            how_to_create=signing_how if name in signing_keys else how,
        )
        for name in internal_credential_names(request)
        if name not in _NOT_REQUIRED_INTERNAL_NAMES
    ]


def _channel_requirements(config: GatewayInstanceConfig) -> list[SecretRequirement]:
    from assistant_gateway.interactive_apps.builtin_registry import build_interactive_app_registry
    from assistant_gateway.interactive_apps.catalog import build_interactive_app_catalog

    photon = config.interactive_apps.photon
    configured_elsewhere = {photon.project_id_env_var} if str(photon.project_id or "").strip() else set()
    catalog = build_interactive_app_catalog(config.interactive_apps, build_interactive_app_registry(config))
    return [
        SecretRequirement(
            name=name,
            purpose=f"Needed by the enabled {entry.display_name} channel.",
            how_to_create=f"Get it from the {entry.display_name} setup (see the channel's page in the docs).",
        )
        for entry in catalog
        if entry.configured_enabled
        for name in entry.secret_reference_names
        if name not in _OPTIONAL_CHANNEL_SECRETS and name not in configured_elsewhere
    ]


def _entered_requirements(entered_names: Iterable[str]) -> list[SecretRequirement]:
    return [
        SecretRequirement(
            name=name,
            purpose="You entered a value for it during onboarding.",
            how_to_create="Onboarding did not save that value; add it as a Docker secret.",
        )
        for name in entered_names
    ]


def ensure_internal_credential_env_file(
    config: GatewayInstanceConfig,
    *,
    existing_postgres_dsn: str | None = None,
    preserve_env_keys: Iterable[str] = (),
    rotate: bool = False,
) -> Path:
    """Write the instance's internal credentials into its ``team.env``, keeping values it already has.

    The one implementation shared by both onboarding front ends (the web
    onboarding engine and ``assistant_cli onboard``) and the team import
    (``team_transfer``), so a new instance gets the same keys however it was
    made. Internal keys that no longer apply are removed, and so are plain NATS
    values when NATS credentials come from a file; ``preserve_env_keys`` are
    never removed.
    """

    env_path = Path(config.launch.env_file_path or config.layout.team_env_path).expanduser().resolve()
    existing = parse_env_file(env_path)
    request = internal_credential_request(
        config,
        env=existing,
        existing_postgres_dsn=existing_postgres_dsn,
        rotate=rotate,
    )
    generated = generate_internal_credentials(request, existing=existing)
    remove_keys = set(INTERNAL_ENV_KEYS - {"NATS_USER", "NATS_PASSWORD"}) - set(generated)
    for key in ("NATS_USER", "NATS_PASSWORD"):
        if existing.get(f"{key}_FILE") or existing.get("NATS_CREDENTIALS_FILE"):
            remove_keys.add(key)
    merge_env_file(env_path, generated, rotate=rotate, remove_keys=remove_keys - set(preserve_env_keys))
    return env_path


def ensure_instance_sealbox_material(config: GatewayInstanceConfig, *, rotate_token: bool = False) -> tuple[Path, ...]:
    """Create the broker's own Sealbox store material when the broker manages and initializes Sealbox."""

    from secure_tool_broker.sealbox_runtime import ensure_managed_sealbox_material, resolve_sealbox_runtime_config

    sealbox = config.security_layer.sealbox
    if not (sealbox.enabled and sealbox.manage_service and sealbox.auto_initialize):
        return ()
    runtime = resolve_sealbox_runtime_config(sealbox, broker_credentials_dir=config.broker_credentials_path)
    return ensure_managed_sealbox_material(runtime, rotate_token=rotate_token)


__all__ = [
    "ENVIRONMENT_SECRETS_DOCS",
    "SecretRequirement",
    "ensure_instance_sealbox_material",
    "ensure_internal_credential_env_file",
    "environment_secret_values",
    "generate_credentials_command",
    "internal_credential_request",
    "missing_secrets",
    "missing_secrets_text",
    "required_secrets",
    "secret_present",
    "should_generate_internal_nats_credentials",
]
