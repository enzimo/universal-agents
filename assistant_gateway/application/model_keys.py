"""The gateway's side of model provider keys: what ``/auth model`` and ``/auth status`` show.

Command handlers stay free of message contracts
(``tests/test_gateway_command_boundaries.py``), so the contract-aware parts
of ``/auth model`` live here: the page options the gateway passes to the
broker (the configured endpoints, which the broker cannot read) and the
status lines built from what the broker reports. Neither ever holds a key.
"""

from __future__ import annotations

import json
import logging
from typing import Any, Awaitable, Callable, Sequence

from pydantic import ValidationError

from access_control.people import is_administrator
from custom_messages.contracts.model_keys_v1 import (
    MODEL_KEY_AUTH_PROVIDER,
    PERSONAL_MODEL_KEY_METHOD,
    TEAM_MODEL_KEY_METHOD,
    KeySource,
    ModelEndpointStatusV1,
    ModelKeyEndpointUseV1,
    ModelKeysStatusV1,
    StoredKeyState,
)

from secure_tools.chat_commands import MODEL_KEY_COMMAND

LOGGER = logging.getLogger(__name__)

MODEL_KEY_PROVIDER = MODEL_KEY_AUTH_PROVIDER
MODEL_KEY_METHOD = TEAM_MODEL_KEY_METHOD
PERSONAL_KEY_METHOD = PERSONAL_MODEL_KEY_METHOD
_PERSONAL_WORD = "me"
ModelEndpointUses = Callable[[], Sequence[ModelKeyEndpointUseV1]]

_STORED_STATE_TEXT = {
    StoredKeyState.STORED: "key stored",
    StoredKeyState.NO_KEY_NEEDED: "no key needed",
    StoredKeyState.MISSING: "no key stored",
}


def model_key_page_method(words: Sequence[str], roles: Sequence[str]) -> str | None:
    """Which page ``/auth model [me]`` opens: the team's for administrators, otherwise the person's own.

    ``/auth model me`` always opens the person's own page. None for anything
    else after ``model``.
    """

    if list(words) == [_PERSONAL_WORD]:
        return PERSONAL_KEY_METHOD
    if words:
        return None
    return MODEL_KEY_METHOD if is_administrator(tuple(roles)) else PERSONAL_KEY_METHOD


def model_key_page_options(uses: Sequence[ModelKeyEndpointUseV1]) -> dict[str, Any]:
    """What the broker's ``/auth model`` page needs from the gateway: the configured endpoints."""

    return {"endpoints": [json.dumps(use.model_dump(mode="json")) for use in uses]}


async def model_key_status_text(read_status: Callable[[], Awaitable[dict[str, Any]]]) -> str:
    """The model key lines of ``/auth status``: what is stored and what overrides it (never a key)."""

    try:
        status = ModelKeysStatusV1.model_validate(await read_status())
    except (RuntimeError, ValidationError) as exc:
        LOGGER.warning("Could not read the model key status. event=model_key_status_unavailable error=%s", exc)
        return "Model provider keys: the broker did not report them."
    if not status.endpoints:
        return f"Model provider keys: none stored. Administrators save them with {MODEL_KEY_COMMAND}."
    lines = ["Model provider keys:"]
    lines.extend(f"- {_endpoint_line(endpoint)}" for endpoint in status.endpoints)
    lines.append(f"Administrators save or replace keys with {MODEL_KEY_COMMAND}.")
    return "\n".join(lines)


def _endpoint_line(endpoint: ModelEndpointStatusV1) -> str:
    text = f"{endpoint.address.split('://', 1)[-1]}: {_STORED_STATE_TEXT[endpoint.state]}"
    names = sorted({item.env_var for item in endpoint.environment_overrides if item.env_var})
    if names:
        text += f"; overridden by {', '.join(names)} in the environment"
    rejection = endpoint.last_rejection
    if rejection is not None:
        whose = rejection.env_var if rejection.source is KeySource.ENVIRONMENT else "the key"
        text += f"; the provider rejected {whose} at {rejection.rejected_at:%Y-%m-%d %H:%M} UTC"
    return text


__all__ = [
    "MODEL_KEY_COMMAND",
    "MODEL_KEY_METHOD",
    "MODEL_KEY_PROVIDER",
    "PERSONAL_KEY_METHOD",
    "ModelEndpointUses",
    "model_key_page_method",
    "model_key_page_options",
    "model_key_status_text",
]
