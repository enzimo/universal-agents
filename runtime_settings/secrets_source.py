"""Where an instance's secrets come from (``launch.secrets_source``).

``env_file`` (the default) keeps secrets and generated internal credentials in
the instance's ``team.env``: onboarding writes the file and the launcher reads
it. ``environment`` is for installs that hand every secret to the container as
environment variables or Docker secrets (``*_FILE``), such as Docker Swarm:
onboarding writes no ``team.env`` and generates no values, and the launcher
reads no env file. A second source of the same secrets would drift from the
real ones, so the two modes never mix.

This module has no project imports so ``default_values`` and every settings
model can use it.
"""

from __future__ import annotations

from typing import Literal

SECRETS_SOURCE_ENV_FILE = "env_file"
SECRETS_SOURCE_ENVIRONMENT = "environment"
SECRETS_SOURCES: tuple[str, ...] = (SECRETS_SOURCE_ENV_FILE, SECRETS_SOURCE_ENVIRONMENT)

SecretsSource = Literal["env_file", "environment"]


def normalize_secrets_source(value: object, *, default: str = SECRETS_SOURCE_ENV_FILE) -> str:
    """Return a known secrets source; empty means ``default``, anything else is refused."""

    normalized = str(value or "").strip().lower()
    if not normalized:
        return default
    if normalized not in SECRETS_SOURCES:
        raise ValueError(
            f"launch.secrets_source must be one of {', '.join(SECRETS_SOURCES)}; got {value!r}."
        )
    return normalized
