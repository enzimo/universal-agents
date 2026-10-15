"""X OAuth hydration with serialized, durable refresh before publishing."""

from __future__ import annotations

import fcntl
import hashlib
import json
import logging
from pathlib import Path
from typing import Any, Literal

import yaml
from pydantic import BaseModel, ConfigDict, Field

from secure_tool_broker.credential_state import (
    broker_credential_file,
    write_private_broker_file,
)
from secure_tool_broker.credential_state_store import (
    read_credential_state,
    write_credential_state,
)
from secure_tool_broker.cli_process import CliFailure, child_environment, run_cli
from secure_tools.hub_providers import POST_CREATE
from secure_tools.providers import ToolResult, ValidatedAction

XURL_STATE_KEY = "xurl.sessions_json"
_LOGGER = logging.getLogger(__name__)


class OAuthCredentials(BaseModel):
    """An X account for xurl: a person's sign-in (``x_sign_in``) or a linked ``oauth_json``.

    ``client_secret`` is empty for a public X app ("Native App"); xurl then
    sends the client id in the refresh request instead of HTTP Basic.
    """

    model_config = ConfigDict(extra="forbid", strict=True)
    auth_type: Literal["oauth2"] = "oauth2"
    client_id: str = Field(min_length=1, max_length=4096)
    client_secret: str = Field(default="", max_length=4096)
    username: str = Field(pattern=r"^[A-Za-z0-9_]{1,15}$")
    access_token: str = Field(min_length=1, max_length=16384)
    refresh_token: str = Field(min_length=1, max_length=16384)
    expiration_time: int = Field(ge=0)


def _initial_store(credentials: OAuthCredentials) -> dict[str, Any]:
    return {
        "default_app": "ua",
        "apps": {
            "ua": {
                "client_id": credentials.client_id,
                "client_secret": credentials.client_secret,
                "default_user": credentials.username,
                "oauth2_tokens": {
                    credentials.username: {
                        "type": "oauth2",
                        "oauth2": {
                            "access_token": credentials.access_token,
                            "refresh_token": credentials.refresh_token,
                            "expiration_time": credentials.expiration_time,
                        },
                    }
                },
            }
        },
    }


def _persist(sessions: dict[str, Any]) -> None:
    result = write_credential_state(XURL_STATE_KEY, json.dumps(sessions))
    if (
        result.status != "success"
        or result.content.get("credential_state_persisted") is False
    ):
        raise CliFailure("oauth_state_persistence_failed")


def _read_sessions() -> dict[str, Any]:
    raw = read_credential_state(XURL_STATE_KEY)
    if isinstance(raw, ToolResult):
        raise CliFailure("oauth_state_unavailable")
    sessions = json.loads(raw) if raw else {}
    if not isinstance(sessions, dict):
        raise CliFailure("oauth_state_invalid")
    return sessions


def _response(output: str, *, write: bool) -> dict[str, Any]:
    try:
        response = json.loads(output)
        data = response["data"]
        if not isinstance(data, dict) or not str(data.get("id", "")).isdigit():
            raise ValueError("missing id")
        return data
    except (ValueError, KeyError, TypeError):
        raise CliFailure(
            "outcome_unknown" if write else "provider_response_invalid", write=write
        ) from None


def _execute_locked(
    action: ValidatedAction, credential: str, home: Path, executable: str, *, credential_key: str
) -> ToolResult:
    credentials = OAuthCredentials.model_validate_json(credential)
    sessions = _read_sessions()
    # Refresh state is kept per account inside the owner's broker state
    # folder: the person's own sign-in (x_sign_in.X_SIGN_IN_SESSION_KEY) or
    # the linked credential's key (the account link chose owner and credential).
    key = credential_key
    fingerprint = hashlib.sha256(credential.encode()).hexdigest()
    previous = sessions.get(key, {})
    store = (
        previous["store"]
        if previous.get("source_sha256") == fingerprint
        else _initial_store(credentials)
    )
    auth_path = home / ".xurl" / "auth.yml"
    auth_path.parent.mkdir(mode=0o700)
    write_private_broker_file(auth_path, yaml.safe_dump(store))
    env = child_environment(home)
    # Refresh before any external write. xurl token emits the access token;
    # discard it completely and persist the updated store before proceeding.
    run_cli(
        [executable, "token", "--app", "ua", "--username", credentials.username], env
    )
    sessions[key] = {
        "source_sha256": fingerprint,
        "store": yaml.safe_load(auth_path.read_text()),
    }
    _persist(sessions)
    write = action.action == POST_CREATE
    command = [
        executable,
        "--auth",
        "oauth2",
        "--app",
        "ua",
        "--username",
        credentials.username,
    ]
    command += (
        ["-X", "POST", "/2/tweets", "-d", json.dumps({"text": action.params["text"]})]
        if write
        else ["/2/users/me"]
    )
    state_warning = False
    try:
        data = _response(run_cli(command, env, write=write), write=write)
    finally:
        # A refresh can also occur during the request. Save it even on HTTP
        # failure, and make persistence failure observable without hiding a
        # successful post receipt or inviting a duplicate publish.
        updated = yaml.safe_load(auth_path.read_text())
        if updated != sessions[key]["store"]:
            sessions[key]["store"] = updated
            try:
                _persist(sessions)
            except CliFailure:
                _LOGGER.error(
                    "event=xurl_post_request_state_persistence_failed action=%s",
                    action.action,
                )
                state_warning = True
    warning = (
        {
            "warning": "OAuth refresh state was not saved. Repair broker storage and reauthorize before further use."
        }
        if state_warning
        else {}
    )
    if write:
        return ToolResult(
            "success",
            {
                "post_id": data["id"],
                "url": f"https://x.com/i/web/status/{data['id']}",
                **warning,
            },
        )
    return ToolResult(
        "success",
        {
            "authenticated": True,
            "user_id": data["id"],
            "username": data.get("username"),
            **warning,
        },
    )


def execute_xurl(
    action: ValidatedAction, credential: str, home: Path, executable: str, *, credential_key: str
) -> ToolResult:
    lock_path = broker_credential_file("hub-cli", "xurl.lock")
    if lock_path is None:
        raise CliFailure("broker_credentials_directory_required")
    # flock serializes threads and processes sharing this owner's credential
    # volume. Distributed brokers must not share an X refresh token independently.
    with lock_path.open("a") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise CliFailure("account_busy") from None
        return _execute_locked(action, credential, home, executable, credential_key=credential_key)
