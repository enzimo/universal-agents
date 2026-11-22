"""In-process registry for secure tool providers."""

from __future__ import annotations

from typing import Callable

from secure_tools.providers import SecureToolProvider


_PROVIDERS: dict[str, SecureToolProvider] = {}
_PROVIDER_ALIASES: dict[str, str] = {
    "1password": "onepassword",
    "op": "onepassword",
    "keepassxc": "keepass",
    "keepassxc-cli": "keepass",
    "hashicorp-vault": "vault",
    "sealbox-cli": "sealbox",
}


# Providers that come from data rather than code (installed CLIs,
# ``secure_tools.installed_clis``): the process that knows the current tool
# catalog sets one resolver, consulted for names no built-in provider has.
_RESOLVER: Callable[[str], SecureToolProvider | None] | None = None


def register_provider(provider: SecureToolProvider) -> None:
    _PROVIDERS[provider.name] = provider


def set_provider_resolver(resolver: Callable[[str], SecureToolProvider | None] | None) -> None:
    global _RESOLVER
    _RESOLVER = resolver


def get_provider(name: str) -> SecureToolProvider | None:
    provider_name = str(name or "").strip().lower()
    provider = _PROVIDERS.get(_PROVIDER_ALIASES.get(provider_name, provider_name))
    if provider is None and _RESOLVER is not None and provider_name:
        provider = _RESOLVER(provider_name)
    return provider


def list_providers(*, secure_cli_only: bool = False) -> list[str]:
    if not secure_cli_only:
        return sorted(_PROVIDERS)
    return sorted(
        name
        for name, provider in _PROVIDERS.items()
        if getattr(provider, "secure_cli_callable", True)
    )
