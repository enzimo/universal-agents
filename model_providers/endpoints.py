"""Model endpoints and which key each one uses.

A model key belongs to an endpoint *address*: the scheme, host and port of
the service it is sent to (``https://bifrost.example.com``), never a path.
Keys are stored per address in the broker's credential store
(``secure_tool_broker.model_keys``), and the runtime asks for the key of the
address it is about to call (``custom_agents.model_keys``). So a key entered
for one service is never sent to another one, even when an administrator
switches the team's OpenAI-compatible endpoint (``llm.litellm_api_base``).

Two kinds of endpoint exist:

- **Well-known providers** (OpenAI, Anthropic, Gemini, xAI) have one fixed
  address each. Choosing the provider chooses the endpoint.
- **The OpenAI-compatible endpoint** (provider ``litellm``) is any service
  that speaks the OpenAI API: OpenRouter, a LiteLLM proxy, Bifrost, vLLM. Its
  address is ``llm.litellm_api_base`` and can be switched. An
  OpenAI-compatible embedding service (``embedding.api_base``) is the same
  kind of endpoint.

Environment variables still override stored keys (owner's decision
2026-09-30): ``endpoint.env_vars`` lists, in order, the variables that do.
The override is reported, never silent (``custom_agents.model_keys``).
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from urllib.parse import urlsplit

LITELLM_API_KEY_ENV = "LITELLM_API_KEY"
EMBEDDING_API_KEY_ENV = "EMBEDDING_API_KEY"
OPENAI_API_KEY_ENV = "OPENAI_API_KEY"
ANTHROPIC_API_KEY_ENV = "ANTHROPIC_API_KEY"
GEMINI_API_KEY_ENV = "GEMINI_API_KEY"
XAI_API_KEY_ENV = "XAI_API_KEY"
# Every variable that overrides a stored model key (the launcher lists the set
# ones for the Status page).
MODEL_KEY_ENV_VARS = (
    LITELLM_API_KEY_ENV,
    EMBEDDING_API_KEY_ENV,
    OPENAI_API_KEY_ENV,
    ANTHROPIC_API_KEY_ENV,
    GEMINI_API_KEY_ENV,
    XAI_API_KEY_ENV,
)

_DEFAULT_PORTS = {"http": 80, "https": 443}
_MAX_ADDRESS_CHARS = 512


class EndpointKind(StrEnum):
    """What protocol an endpoint speaks, which decides how a key is tested and sent."""

    OPENAI_COMPATIBLE = "openai_compatible"
    OPENAI = "openai"
    ANTHROPIC = "anthropic"
    GEMINI = "gemini"
    XAI = "xai"


class InvalidEndpointAddress(ValueError):
    """An endpoint URL that cannot hold a model key: the message says why, in plain words."""


@dataclass(frozen=True)
class WellKnownProvider:
    """A provider with one fixed endpoint, its API base and the variable that overrides its key."""

    kind: EndpointKind
    label: str
    base_url: str
    env_var: str

    @property
    def address(self) -> str:
        return endpoint_address(self.base_url)


WELL_KNOWN_PROVIDERS: dict[EndpointKind, WellKnownProvider] = {
    provider.kind: provider
    for provider in (
        WellKnownProvider(EndpointKind.OPENAI, "OpenAI", "https://api.openai.com/v1", OPENAI_API_KEY_ENV),
        WellKnownProvider(EndpointKind.ANTHROPIC, "Anthropic", "https://api.anthropic.com/v1", ANTHROPIC_API_KEY_ENV),
        WellKnownProvider(
            EndpointKind.GEMINI, "Google Gemini", "https://generativelanguage.googleapis.com/v1beta", GEMINI_API_KEY_ENV
        ),
        WellKnownProvider(EndpointKind.XAI, "xAI", "https://api.x.ai/v1", XAI_API_KEY_ENV),
    )
}


# llm.provider -> the kind of endpoint whose key it uses. OAuth providers
# (openai-codex, xai-oauth) sign in instead, and bedrock uses AWS credentials.
PROVIDER_ENDPOINT_KINDS: dict[str, EndpointKind] = {
    "litellm": EndpointKind.OPENAI_COMPATIBLE,
    "openai": EndpointKind.OPENAI,
    "anthropic": EndpointKind.ANTHROPIC,
    "gemini": EndpointKind.GEMINI,
    "xai": EndpointKind.XAI,
}


@dataclass(frozen=True)
class ModelEndpoint:
    """One endpoint a process sends model requests to, and where its key may come from.

    ``env_vars`` are the environment variables that override the stored key,
    in the order they are checked.
    """

    address: str
    kind: EndpointKind
    env_vars: tuple[str, ...]

    @property
    def host(self) -> str:
        return display_host(self.address)


def checked_endpoint_url(url: object) -> str:
    """A model endpoint URL with its trailing slash removed, or ``InvalidEndpointAddress``.

    Refused: anything but ``http``/``https``, a missing host, a user name or
    password in the URL (it would end up in the configuration file), and a
    query or fragment (the runtime appends API paths to the URL).
    """

    text = str(url or "").strip()
    if not text:
        raise InvalidEndpointAddress("The endpoint address is empty.")
    if len(text) > _MAX_ADDRESS_CHARS:
        raise InvalidEndpointAddress(f"The endpoint address is longer than {_MAX_ADDRESS_CHARS} characters.")
    try:
        parts = urlsplit(text)
        parts.port  # noqa: B018 - reading the port raises ValueError for a malformed one
    except ValueError as exc:
        raise InvalidEndpointAddress(f"The endpoint address is not a valid URL: {exc}.") from exc
    if parts.scheme.lower() not in _DEFAULT_PORTS:
        raise InvalidEndpointAddress("The endpoint address must start with https:// or http://.")
    if not parts.hostname:
        raise InvalidEndpointAddress("The endpoint address has no host name.")
    if parts.username is not None or parts.password is not None:
        raise InvalidEndpointAddress(
            "The endpoint address contains a user name or password. Enter the key in the key field instead."
        )
    if parts.query or parts.fragment:
        raise InvalidEndpointAddress("The endpoint address must not contain ? or # parts.")
    return text.rstrip("/")


def endpoint_address(url: object) -> str:
    """The address a key belongs to: lower-case scheme and host, and the port unless it is the default."""

    parts = urlsplit(checked_endpoint_url(url))
    scheme = parts.scheme.lower()
    host = str(parts.hostname or "").lower()
    if ":" in host:
        host = f"[{host}]"
    port = parts.port
    suffix = f":{port}" if port is not None and port != _DEFAULT_PORTS[scheme] else ""
    return f"{scheme}://{host}{suffix}"


def display_host(address: str) -> str:
    """The host (and port) people see for an address: ``bifrost.example.com``."""

    return str(address or "").split("://", 1)[-1]


def openai_compatible_endpoint(api_base: object) -> ModelEndpoint:
    """The team's OpenAI-compatible model endpoint (``llm.litellm_api_base``)."""

    return ModelEndpoint(
        address=endpoint_address(api_base),
        kind=EndpointKind.OPENAI_COMPATIBLE,
        env_vars=(LITELLM_API_KEY_ENV,),
    )


def embedding_endpoint(api_base: object, *, chat_api_base: object = None) -> ModelEndpoint:
    """An OpenAI-compatible embedding service (``embedding.api_base``).

    ``EMBEDDING_API_KEY`` overrides its key. ``LITELLM_API_KEY`` does too, but
    only when the embedding service has the same address as the model
    endpoint: before 2026-10 the embedding key fell back to
    ``LITELLM_API_KEY`` for any address, which sent that key to a service it
    was not meant for.
    """

    address = endpoint_address(api_base)
    env_vars: tuple[str, ...] = (EMBEDDING_API_KEY_ENV,)
    if chat_api_base and _same_address(chat_api_base, address):
        env_vars = (EMBEDDING_API_KEY_ENV, LITELLM_API_KEY_ENV)
    return ModelEndpoint(address=address, kind=EndpointKind.OPENAI_COMPATIBLE, env_vars=env_vars)


def well_known_endpoint(kind: EndpointKind) -> ModelEndpoint:
    """The fixed endpoint of a well-known provider."""

    provider = WELL_KNOWN_PROVIDERS[kind]
    return ModelEndpoint(address=provider.address, kind=kind, env_vars=(provider.env_var,))


def well_known_provider_at(address: str) -> WellKnownProvider | None:
    """The well-known provider whose fixed endpoint is ``address``, if any."""

    return next((item for item in WELL_KNOWN_PROVIDERS.values() if item.address == address), None)


def _same_address(url: object, address: str) -> bool:
    try:
        return endpoint_address(url) == address
    except InvalidEndpointAddress:
        return False


__all__ = [
    "ANTHROPIC_API_KEY_ENV",
    "EMBEDDING_API_KEY_ENV",
    "EndpointKind",
    "GEMINI_API_KEY_ENV",
    "InvalidEndpointAddress",
    "LITELLM_API_KEY_ENV",
    "MODEL_KEY_ENV_VARS",
    "PROVIDER_ENDPOINT_KINDS",
    "ModelEndpoint",
    "OPENAI_API_KEY_ENV",
    "WELL_KNOWN_PROVIDERS",
    "WellKnownProvider",
    "XAI_API_KEY_ENV",
    "checked_endpoint_url",
    "display_host",
    "embedding_endpoint",
    "endpoint_address",
    "openai_compatible_endpoint",
    "well_known_endpoint",
    "well_known_provider_at",
]
