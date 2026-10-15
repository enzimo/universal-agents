"""Infrastructure adapter for the Website bootstrap credential."""

from __future__ import annotations

from env_secrets import get_env_with_file_fallback


class EnvironmentGatewayBootstrapTokenSource:
    """Resolve the configured secret or Docker-secret file on every auth attempt."""

    def __init__(self, env_var: str) -> None:
        normalized = str(env_var or "").strip()
        if not normalized:
            raise ValueError("Website bootstrap token environment variable is required.")
        self._env_var = normalized

    def read(self) -> str | None:
        value = str(get_env_with_file_fallback(self._env_var) or "").strip()
        return value or None
