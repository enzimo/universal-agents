"""Model provider keys between the agent runtime, the broker and the gateway.

Keys live in the broker's credential store, one per endpoint address
(``model_providers.endpoints``). Processes that make model calls ask the
broker for the key of the address they call (routes in
``secure_tool_broker.defaults``) (``ModelKeyLookupV1`` ->
``ModelKeyReplyV1``) and tell it which source they use and when a provider
rejected a key (``ModelKeyReportV1``), naming environment variables but
never holding their values. The gateway reads what the broker knows
(``ModelKeysStatusV1``) for ``/auth model`` and the Status page; that status
never contains a key.
"""

from __future__ import annotations

from datetime import datetime
from enum import StrEnum
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator

from model_providers.endpoints import EndpointKind, endpoint_address

# The /auth provider and methods that open the model key page.
MODEL_KEY_AUTH_PROVIDER = "model"
TEAM_MODEL_KEY_METHOD = "team"
PERSONAL_MODEL_KEY_METHOD = "personal"
# Whose key a reply holds.
KeyOwner = Literal["team", "person"]
_MAX_KEY_CHARS = 8192


class ModelKeyContractModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


def _address(value: str) -> str:
    return endpoint_address(value)


class StoredKeyState(StrEnum):
    """What the credential store holds for an address."""

    STORED = "stored"
    NO_KEY_NEEDED = "no_key_needed"
    MISSING = "missing"


class KeySource(StrEnum):
    """Where a process takes the key of an address from.

    ``personal``: the key the person the task works for saved for themselves
    (``/auth model me``); it is theirs alone and never falls back to the
    team's key when rejected.
    """

    ENVIRONMENT = "environment"
    STORED = "stored"
    NO_KEY_NEEDED = "no_key_needed"
    PERSONAL = "personal"


class KeyReportEvent(StrEnum):
    IN_USE = "in_use"
    REJECTED = "rejected"


class ConnectionPurposeV1(StrEnum):
    CHAT = "chat"
    EMBEDDING = "embedding"


class ModelKeyLookupV1(ModelKeyContractModel):
    """A process asks for the key of the address it is about to call.

    ``context`` is the trusted broker context of the task the call is for
    (``custom_tools.secure_cli_tool.trusted_broker_context``), carrying its
    signed task context. The broker verifies it and answers with that
    person's own key when they saved one; without it, the team's key.
    ``purpose`` says what the call is for: embeddings (memory search) keep
    the team's key even for a person whose work may not use it
    (``access_control.person_settings``), because they are the team's service.
    """

    address: str = Field(min_length=1, max_length=520)
    context: dict[str, Any] | None = None
    purpose: ConnectionPurposeV1 = ConnectionPurposeV1.CHAT

    _valid_address = field_validator("address")(_address)


class ModelKeyReplyV1(ModelKeyContractModel):
    """The broker's answer; ``key`` is set only when ``state`` is ``stored``.

    ``team_keys_allowed`` is False when the task works for a person whose
    work may not use the team's keys and who saved no key of their own for
    the address: the call must not run, with the team's key or an
    environment override (``custom_agents.model_keys``).
    """

    address: str
    state: StoredKeyState
    key: str = Field(default="", max_length=_MAX_KEY_CHARS, repr=False)
    saved_at: datetime | None = None
    owner: KeyOwner = "team"
    team_keys_allowed: bool = True


class ModelKeyReportV1(ModelKeyContractModel):
    """Which source a process uses for an address, or that the provider rejected that key.

    ``env_var`` names the overriding variable, never its value. The process
    is not named here: the broker knows it from the caller token.
    """

    address: str = Field(min_length=1, max_length=520)
    kind: EndpointKind
    event: KeyReportEvent
    source: KeySource
    env_var: str | None = Field(default=None, max_length=120)
    provider: str = Field(default="", max_length=120)
    model_id: str = Field(default="", max_length=512)
    status_code: int | None = None

    _valid_address = field_validator("address")(_address)


class ModelKeyEndpointUseV1(ModelKeyContractModel):
    """An endpoint the team's configuration uses, offered on the ``/auth model`` page.

    The gateway reads the instance configuration (the broker cannot) and
    passes these when it opens the page, so the page can fill in the model
    to test each key with.
    """

    base_url: str = Field(min_length=1, max_length=520)
    kind: EndpointKind
    purpose: ConnectionPurposeV1 = ConnectionPurposeV1.CHAT
    model_id: str = Field(default="", max_length=512)
    api_mode: str = Field(default="chat_completions", max_length=32)
    label: str = Field(default="", max_length=120)

    @property
    def address(self) -> str:
        return endpoint_address(self.base_url)


class ProcessKeySourceV1(ModelKeyContractModel):
    """One process's last report for an address."""

    caller: str
    source: KeySource
    env_var: str | None = None
    reported_at: datetime


class KeyRejectionV1(ModelKeyContractModel):
    caller: str
    source: KeySource
    env_var: str | None = None
    provider: str = ""
    model_id: str = ""
    status_code: int | None = None
    rejected_at: datetime


class ModelEndpointStatusV1(ModelKeyContractModel):
    """What the broker knows about one address: the stored key's details (never the key) and reports."""

    address: str
    kind: EndpointKind | None = None
    state: StoredKeyState
    saved_at: datetime | None = None
    saved_by_person_id: str | None = None
    tested_model_id: str | None = None
    sources: tuple[ProcessKeySourceV1, ...] = ()
    last_rejection: KeyRejectionV1 | None = None

    @property
    def environment_overrides(self) -> tuple[ProcessKeySourceV1, ...]:
        return tuple(item for item in self.sources if item.source is KeySource.ENVIRONMENT)


class ModelKeysStatusV1(ModelKeyContractModel):
    endpoints: tuple[ModelEndpointStatusV1, ...] = ()


class SavedEndpointV1(ModelKeyContractModel):
    """An endpoint the team has a key for (never the key), so TeamLead can switch to it by name.

    ``base_url`` is the address form that answered the key's test (with or
    without ``/v1``); empty for keys saved before it was recorded.
    """

    address: str
    base_url: str = ""
    kind: EndpointKind
    tested_model_id: str = ""
    tested_api_mode: str = ""
    saved_at: datetime
    no_key_needed: bool = False


class SavedEndpointsV1(ModelKeyContractModel):
    """The team's saved endpoints (``MODEL_KEY_ENDPOINTS_ROUTE``)."""

    endpoints: tuple[SavedEndpointV1, ...] = ()


__all__ = [
    "ConnectionPurposeV1",
    "KeyRejectionV1",
    "KeyReportEvent",
    "KeySource",
    "MODEL_KEY_AUTH_PROVIDER",
    "ModelEndpointStatusV1",
    "ModelKeyEndpointUseV1",
    "ModelKeyLookupV1",
    "ModelKeyReplyV1",
    "ModelKeyReportV1",
    "ModelKeysStatusV1",
    "PERSONAL_MODEL_KEY_METHOD",
    "ProcessKeySourceV1",
    "SavedEndpointV1",
    "SavedEndpointsV1",
    "StoredKeyState",
    "TEAM_MODEL_KEY_METHOD",
]
