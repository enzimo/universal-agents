"""Broker-only hydration and bounded CLI execution for X and Hugging Face.

Public HF operations use the normal shell tool. Credentialed operations accept
content and repository identifiers, never paths into the broker's filesystem.

Which account a call uses: for X, the person's own browser sign-in
(``x_sign_in``, made with ``/link cli:x``) first, then the account link the
broker's gate chose (the team's); for Hugging Face, the account link (the
person's own first, then the team's).
"""

from __future__ import annotations

import base64
import json
import logging
import shutil
import tempfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from secure_tool_broker.cli_process import CliFailure, child_environment, run_cli
from custom_messages.contracts.access_v2 import REASON_ACCOUNT_LINK_MISSING, LinkOwnerKind
from secure_tool_broker.credential_catalog import (
    _redact_known_secret_values,
    resolve_secret_field,
)
from secure_tool_broker.credential_catalog_store import CredentialOwnerScope
from secure_tool_broker.credential_diagnostics import (
    CREDENTIAL_FIELD_NOT_SECRET,
    CREDENTIAL_VALUE_UNAVAILABLE,
    CREDENTIAL_UNAVAILABLE,
    credential_resolution_failure,
)
from secure_tool_broker.credential_state import (
    broker_credential_file,
    credential_owner_context,
)
from secure_tool_broker.x_sign_in import SIGN_IN_COMMAND as X_SIGN_IN_COMMAND
from secure_tool_broker.x_sign_in import X_SIGN_IN_SESSION_KEY, X_TOOL, person_sign_in
from secure_tools.chat_commands import ACCOUNTS_SHARE, accounts_command
from secure_tools.hub_providers import (
    AUTH_STATUS,
    FILE_DOWNLOAD,
    HF_PROVIDER,
    MAX_FILE_BYTES,
    MODEL_INFO,
    X_PROVIDER,
    hub_cli_providers,
)
from secure_tools.providers import ToolContext, ToolResult, ValidatedAction

_LOGGER = logging.getLogger(__name__)


def _credential_failure(action: ValidatedAction, code: str, field: str) -> ToolResult:
    content = credential_resolution_failure(code, required_field=field)
    _LOGGER.warning(
        "event=hub_cli_failed provider=%s action=%s code=%s stage=%s action_owner=%s",
        action.provider, action.action, content["error_code"],
        content["failure_stage"], content["action_owner"],
    )
    return ToolResult("error", content, content["next_action"])


@dataclass(frozen=True)
class _Account:
    """The credential one call uses: whose it is, what names its saved state, and its value."""

    scope: CredentialOwnerScope
    key: str
    value: str = field(repr=False)


# X with no sign-in for the person and no team account link.
X_ACCOUNT_MISSING = "x_account_missing"
# The executor does not see the team's sharing (the gate hands it a team link
# only while the team shares X), so the message holds for both.
X_ACCOUNT_MISSING_MESSAGE = (
    f"No X account is available for this work. The person signs in with {X_SIGN_IN_COMMAND}. "
    f"A team that shares X ({accounts_command(ACCOUNTS_SHARE, X_TOOL.tool_id)}) can also link one team X account for everyone "
    f"with {X_SIGN_IN_COMMAND} team."
)


def _x_sign_in(context: ToolContext) -> _Account | None:
    """The person's own X sign-in (``x_sign_in``), which comes before any account link."""

    person = str(context.canonical_user_id or "").strip()
    if not person:
        return None
    stored = person_sign_in(person)
    if isinstance(stored, ToolResult):
        raise CliFailure(CREDENTIAL_UNAVAILABLE)
    return _Account(CredentialOwnerScope.user(person), X_SIGN_IN_SESSION_KEY, stored) if stored else None


def _linked_account(action: ValidatedAction, context: ToolContext) -> _Account | ToolResult:
    """The credential of the account link the broker's gate chose (for X, the team's)."""

    link = context.account_link
    if link is None:
        if action.provider == X_PROVIDER:
            _LOGGER.info(
                "event=hub_cli_failed provider=%s action=%s code=%s", action.provider, action.action, X_ACCOUNT_MISSING
            )
            return ToolResult(
                "error",
                {"error_code": X_ACCOUNT_MISSING, "user_action": X_SIGN_IN_COMMAND, "retry_safe": False},
                X_ACCOUNT_MISSING_MESSAGE,
            )
        raise CliFailure(REASON_ACCOUNT_LINK_MISSING)
    scope = (
        CredentialOwnerScope.team(link.owner_id)
        if link.owner_kind is LinkOwnerKind.TEAM
        else CredentialOwnerScope.user(link.owner_id)
    )
    field_name = "api_token" if action.provider == HF_PROVIDER else "oauth_json"
    resolved = resolve_secret_field(link.credential_key, field_name, scope=scope)
    if isinstance(resolved, ToolResult):
        return _credential_failure(
            action, str(resolved.content.get("error_code") or CREDENTIAL_UNAVAILABLE), field_name
        )
    if not resolved.secret:
        return _credential_failure(action, CREDENTIAL_FIELD_NOT_SECRET, field_name)
    if not resolved.value:
        return _credential_failure(action, CREDENTIAL_VALUE_UNAVAILABLE, field_name)
    return _Account(scope, link.credential_key, resolved.value)


def _account(action: ValidatedAction, context: ToolContext) -> _Account | ToolResult:
    """X: the person's own sign-in, else the linked account; Hugging Face: the linked account."""

    if action.provider == X_PROVIDER:
        own = _x_sign_in(context)
        if own is not None:
            return own
    return _linked_account(action, context)


def _hf_download(
    action: ValidatedAction, token: str, home: Path, executable: str
) -> dict[str, Any]:
    from huggingface_hub import get_hf_file_metadata, hf_hub_url

    params = action.params
    url = hf_hub_url(
        params["repo_id"],
        params["filename"],
        repo_type=params["repo_type"],
        revision=params["revision"],
        endpoint="https://huggingface.co",
    )
    metadata = get_hf_file_metadata(url, token=token)
    if (
        metadata.size is None
        or metadata.size > MAX_FILE_BYTES
        or metadata.commit_hash != params["revision"]
    ):
        raise CliFailure("download_size_or_revision_rejected")
    destination = home / "download"
    env = child_environment(home)
    env["HF_TOKEN"] = token
    # HF_HUB_DISABLE_IMPLICIT_TOKEN blocks ambient auth for public shell use;
    # this isolated child is expressly credentialed for this one operation.
    env["HF_HUB_DISABLE_IMPLICIT_TOKEN"] = "0"
    run_cli(
        [
            executable,
            "download",
            params["repo_id"],
            params["filename"],
            "--repo-type",
            params["repo_type"],
            "--revision",
            params["revision"],
            "--local-dir",
            str(destination),
            "--quiet",
        ],
        env,
    )
    path = destination / params["filename"]
    if path.is_symlink() or not path.is_file() or path.stat().st_size != metadata.size:
        raise CliFailure("download_verification_failed")
    return {
        "repo_id": params["repo_id"],
        "filename": params["filename"],
        "revision": params["revision"],
        "size_bytes": metadata.size,
        "content_base64": base64.b64encode(path.read_bytes()).decode("ascii"),
    }


def _hf_model_info(action: ValidatedAction, token: str) -> ToolResult:
    """Public metadata of one model, read with the linked account's token."""

    from huggingface_hub import HfApi

    repo_id = action.params["repo_id"]
    info = HfApi(endpoint="https://huggingface.co", token=token).model_info(repo_id)
    return ToolResult(
        "success",
        {
            "repo_id": repo_id,
            "pipeline_tag": getattr(info, "pipeline_tag", None),
            "tags": [str(tag) for tag in (getattr(info, "tags", None) or [])],
            "gated": bool(getattr(info, "gated", False)),
            "private": bool(getattr(info, "private", False)),
        },
    )


def _hf_action(
    action: ValidatedAction, token: str, home: Path, executable: str
) -> ToolResult:
    from huggingface_hub import HfApi

    if action.action == MODEL_INFO:
        return _hf_model_info(action, token)
    if action.action == AUTH_STATUS:
        env = child_environment(home)
        env["HF_TOKEN"] = token
        env["HF_HUB_DISABLE_IMPLICIT_TOKEN"] = "0"
        output = json.loads(run_cli([executable, "auth", "whoami", "--json"], env))
        # Only expose public account identity, never token metadata.
        name = output.get("user")
        if not isinstance(name, str) or not name:
            raise CliFailure("malformed_identity_response")
        return ToolResult("success", {"authenticated": True, "username": name})
    if action.action == FILE_DOWNLOAD:
        return ToolResult("success", _hf_download(action, token, home, executable))
    # hf upload implicitly creates repositories and branches. The SDK's atomic
    # parent_commit check preserves a bounded write to an existing main branch.
    params = action.params
    api = HfApi(endpoint="https://huggingface.co", token=token)
    info = api.repo_info(
        params["repo_id"], repo_type=params["repo_type"], revision="main"
    )
    if info.sha != params["revision"]:
        raise CliFailure("upload_revision_conflict")
    try:
        commit = api.upload_file(
            path_or_fileobj=base64.b64decode(params["content_base64"], validate=True),
            path_in_repo=params["filename"],
            repo_id=params["repo_id"],
            repo_type=params["repo_type"],
            revision="main",
            parent_commit=params["revision"],
            commit_message=f"Update {params['filename']}",
        )
    except Exception:
        raise CliFailure("outcome_unknown", write=True) from None
    return ToolResult(
        "success",
        {
            "repo_id": params["repo_id"],
            "filename": params["filename"],
            "commit_id": commit.oid,
            "url": commit.commit_url,
        },
    )


def execute_hub_cli(action: ValidatedAction, context: ToolContext) -> ToolResult:
    """Revalidate trusted action/schema and hydrate from the selected owner's catalog."""
    try:
        provider = next(
            provider
            for provider in hub_cli_providers()
            if provider.name == action.provider
        )
        action = provider.validate(action.action, dict(action.params))
        account = _account(action, context)
        if isinstance(account, ToolResult):
            return account
        executable = shutil.which("hf" if action.provider == HF_PROVIDER else "xurl")
        if executable is None:
            raise CliFailure("cli_unavailable")
        # A separate state owner namespaces team/user material without exposing
        # a provider's token directory to an agent's shell.
        with credential_owner_context(account.scope.owner_id, scope_kind=account.scope.scope_kind):
            anchor = broker_credential_file("hub-cli", "anchor")
            if anchor is None:
                raise CliFailure("broker_credentials_directory_required")
            with tempfile.TemporaryDirectory(
                prefix="invocation-", dir=anchor.parent
            ) as directory:
                home = Path(directory)
                if action.provider == X_PROVIDER:
                    from secure_tool_broker.xurl_executor import execute_xurl

                    return execute_xurl(
                        action, account.value, home, executable, credential_key=account.key
                    )
                result = _hf_action(action, account.value, home, executable)
                result.content = _redact_known_secret_values(
                    result.content, [account.value]
                )
                return result
    except CliFailure as exc:
        code = exc.code
    except Exception as exc:
        # Provider exceptions may include raw rejected input or HTTP headers.
        code = "invalid_parameters" if isinstance(exc, ValueError) else "provider_error"
    _LOGGER.warning(
        "event=hub_cli_failed provider=%s action=%s code=%s",
        action.provider,
        action.action,
        code,
    )
    return ToolResult(
        "error",
        {"error_code": code, "retry_safe": False},
        "Outcome unknown; reconcile the remote result before another write."
        if code == "outcome_unknown"
        else f"{action.provider}.{action.action} failed: {code}.",
    )
