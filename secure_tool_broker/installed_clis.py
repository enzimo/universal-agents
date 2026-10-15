"""Running an installed CLI for a person: a checked copy, their credential, inside the sandbox.

Owner's decision 2026-09-28. An installed CLI's program comes from the team
workspace, which agents can write, and the broker holds every person's
credentials, so each run:

1. checks the argument list against the approved command table
   (``cli_passthrough.check_installed_command_line``); the access gate decided
   the level of the same list, and the broker runs exactly this list;
2. copies the workspace program into a new private run folder without
   following links and checks the copy's SHA-256 against the approved one
   (``access_control.cli_binaries.checked_copy``); a changed program is
   refused with a plain message and ``event=installed_cli_program_refused``
   naming the tool and both digests;
3. puts the credential of the account link the gate chose for the person
   the task works for (the tool's ``link_owners``) where the tool says: an
   environment variable or a file in the run folder, which is the program's
   home. Never on its command line;
4. runs the checked copy inside the same Landlock sandbox agents' code uses
   (``custom_tools.code_sandbox``), always required whatever the agents'
   sandbox setting: the program reads only system folders and its run folder
   and writes only its run folder, so the broker's files and every other
   credential stay unreadable. The network is open, because the program must
   reach its service; Landlock limits TCP by port, not by host, so per-host
   limits are not possible. Without Landlock the run is refused with a plain
   message and ``event=installed_cli_sandbox_unavailable``;
5. keeps at most ``MAX_OUTPUT_BYTES`` of output, removes the credential's
   values and token-shaped text from it, and deletes the run folder.
"""

from __future__ import annotations

import logging
import os
import shutil
import subprocess
import tempfile
from pathlib import Path
from typing import Any

from access_control.cli_binaries import ProgramRefused, checked_copy, workspaces_root
from custom_messages.contracts.access_v2 import (
    REASON_ACCOUNT_LINK_MISSING,
    InjectionPlacement,
    LinkOwnerKind,
)
from custom_messages.contracts.cli_tool_v1 import binary_parts
from custom_tools.code_sandbox import (
    ENV_CODE_SANDBOX_MODE,
    CODE_SANDBOX_REQUIRED,
    CodeSandboxUnavailable,
    prepare_sandboxed_command,
)
from secure_tool_broker.cli_passthrough import CommandRefused, check_installed_command_line
from secure_tool_broker.credential_catalog import resolve_secret_field
from secure_tool_broker.credential_catalog_store import CredentialOwnerScope
from secure_tool_broker.redaction import redact_text
from secure_tools.providers import ToolContext, ToolResult, ValidatedAction

logger = logging.getLogger(__name__)

RUN_TIMEOUT_SECONDS = 120
MAX_OUTPUT_BYTES = 1024 * 1024
REASON_SANDBOX_UNAVAILABLE = "installed_cli_sandbox_unavailable"
REASON_CREDENTIAL_UNAVAILABLE = "installed_cli_credential_unavailable"
REASON_TIMEOUT = "installed_cli_timeout"
REDACTED = "[REDACTED]"
# The only broker settings the sandbox reads: required mode, and the folders
# whose parents it never grants (the instance and the broker's credentials).
_SANDBOX_SETTING_KEYS = ("TEAM_INSTANCE_ROOT", "SECURE_TOOL_BROKER_CREDENTIALS_DIR", "PATH")
_CHILD_ENV_KEYS = ("PATH", "LANG", "LC_ALL")


def _refused(tool: Any, code: str, message: str) -> ToolResult:
    return ToolResult("error", {"tool_id": tool.tool_id, "error_code": code, "retry_safe": False}, message)


def _instance_root() -> str:
    root = str(os.getenv("TEAM_INSTANCE_ROOT") or "").strip()
    if not root:
        raise ProgramRefused("The broker has no instance folder (TEAM_INSTANCE_ROOT).", reason_code="cli_no_instance")
    return root


def _credential_values(tool: Any, context: ToolContext) -> dict[str, str] | ToolResult:
    """Each credential field the tool uses, from the account link the gate chose (field -> value)."""

    injections = _injections(tool, context)
    if not injections:
        return {}
    link = context.account_link
    if link is None:
        return _refused(tool, REASON_ACCOUNT_LINK_MISSING, f"No account is linked for {tool.display_name}.")
    scope = (
        CredentialOwnerScope.team(link.owner_id)
        if link.owner_kind is LinkOwnerKind.TEAM
        else CredentialOwnerScope.user(link.owner_id)
    )
    values: dict[str, str] = {}
    for injection in injections:
        resolved = resolve_secret_field(link.credential_key, injection.field, scope=scope)
        if isinstance(resolved, ToolResult) or not getattr(resolved, "value", ""):
            return _refused(
                tool,
                REASON_CREDENTIAL_UNAVAILABLE,
                f"The linked account for {tool.display_name} has no usable {injection.field}. Link it again.",
            )
        values[injection.field] = resolved.value
    return values


def _injections(tool: Any, context: ToolContext) -> tuple[Any, ...]:
    """Where each field goes: what the person confirmed on the link, else the tool's defaults."""

    link = context.account_link
    if link is not None and link.injections:
        return tuple(link.injections)
    return tuple(tool.credential_injections)


def _place_credentials(tool: Any, context: ToolContext, values: dict[str, str], run_dir: Path) -> dict[str, str]:
    """Credential files in the run folder (0600) and the credential environment variables."""

    env: dict[str, str] = {}
    for injection in _injections(tool, context):
        rendered = injection.render(values[injection.field])
        if injection.placement is InjectionPlacement.FILE:
            path = run_dir.joinpath(*injection.name.split("/"))
            path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
            descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW | os.O_CLOEXEC, 0o600)
            with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
                handle.write(rendered)
        else:
            env[injection.name] = rendered
    return env


def _child_env(credential_env: dict[str, str]) -> dict[str, str]:
    env = {key: os.environ[key] for key in _CHILD_ENV_KEYS if key in os.environ}
    env["NO_COLOR"] = "1"
    env.update(credential_env)
    return env


def _sandbox_settings() -> dict[str, str]:
    """The sandbox is always required for installed CLIs, whatever the agents' sandbox setting."""

    settings = {key: os.environ[key] for key in _SANDBOX_SETTING_KEYS if key in os.environ}
    settings[ENV_CODE_SANDBOX_MODE] = CODE_SANDBOX_REQUIRED
    return settings


def _redact(text: str, secrets: list[str]) -> str:
    for secret in sorted(secrets, key=len, reverse=True):
        if len(secret) >= 4:
            text = text.replace(secret, REDACTED)
    return redact_text(text)


def _read_limited(handle: Any) -> tuple[str, bool]:
    handle.seek(0)
    data = handle.read(MAX_OUTPUT_BYTES + 1)
    return data[:MAX_OUTPUT_BYTES].decode("utf-8", errors="replace"), len(data) > MAX_OUTPUT_BYTES


def _execute(command: Any, run_dir: Path, stdin: str | None) -> tuple[int, str, str, bool]:
    """Run the sandboxed command; output goes to broker-owned files outside the run folder."""

    with tempfile.TemporaryFile() as out, tempfile.TemporaryFile() as err:
        process = subprocess.run(
            list(command.argv),
            env=command.env,
            cwd=run_dir,
            input=stdin.encode("utf-8") if stdin is not None else None,
            stdin=None if stdin is not None else subprocess.DEVNULL,
            stdout=out,
            stderr=err,
            timeout=RUN_TIMEOUT_SECONDS,
            check=False,
        )
        stdout, cut_out = _read_limited(out)
        stderr, cut_err = _read_limited(err)
    return process.returncode, stdout, stderr, cut_out or cut_err


def _prepare(tool: Any, checked: Any, run_dir: Path, credential_env: dict[str, str]) -> Any:
    """Copy and check the program, then wrap the command in the sandbox."""

    cli = tool.installed_cli
    parts = binary_parts(cli.binary_path)
    program = run_dir / ".program" / parts[-1]
    program.parent.mkdir(mode=0o700)
    checked_copy(workspaces_root(_instance_root()), parts, cli.sha256, program)
    return prepare_sandboxed_command(
        (str(program), *checked.argv),
        task_roots=(),
        env=_child_env(credential_env),
        settings_env=_sandbox_settings(),
        network=True,
        scratch_dir=str(run_dir),
    )


def run_installed_cli(tool: Any, policy: Any, action: ValidatedAction, context: ToolContext) -> ToolResult:
    """Run one checked command of an installed CLI for the task's person (module docstring)."""

    try:
        checked = check_installed_command_line(policy, list(action.params["argv"]))
    except CommandRefused as exc:
        return _refused(tool, exc.reason_code, str(exc))
    values = _credential_values(tool, context)
    if isinstance(values, ToolResult):
        return values
    run_dir = Path(tempfile.mkdtemp(prefix="ua-cli-run-"))
    try:
        return _run(tool, checked, action, context, values, run_dir)
    finally:
        shutil.rmtree(run_dir, ignore_errors=True)


def _run(tool: Any, checked: Any, action: ValidatedAction, context: ToolContext, values: dict[str, str], run_dir: Path) -> ToolResult:
    try:
        command = _prepare(tool, checked, run_dir, _place_credentials(tool, context, values, run_dir))
    except ProgramRefused as exc:
        logger.error(
            "An installed CLI's program was refused. event=installed_cli_program_refused tool_id=%s reason=%s "
            "approved_sha256=%s actual_sha256=%s",
            tool.tool_id,
            exc.reason_code,
            tool.installed_cli.sha256,
            exc.actual_sha256 or "-",
        )
        return _refused(tool, exc.reason_code, str(exc))
    except CodeSandboxUnavailable as exc:
        logger.error(
            "An installed CLI was not run because the sandbox is unavailable. "
            "event=installed_cli_sandbox_unavailable tool_id=%s",
            tool.tool_id,
        )
        return _refused(
            tool,
            REASON_SANDBOX_UNAVAILABLE,
            f"{tool.display_name} runs only inside the sandbox, and this host cannot apply it (Linux Landlock). "
            f"The operator can enable Landlock on the broker's host. {exc}",
        )
    try:
        code, stdout, stderr, truncated = _execute(command, run_dir, action.params.get("stdin"))
    except subprocess.TimeoutExpired:
        return _refused(tool, REASON_TIMEOUT, f"{tool.display_name} did not finish within {RUN_TIMEOUT_SECONDS} seconds.")
    secrets = list(values.values())
    content = {
        "tool_id": tool.tool_id,
        "command": checked.command_path,
        "level": checked.level.value,
        "exit_code": code,
        "stdout": _redact(stdout, secrets),
        "stderr": _redact(stderr, secrets),
        "output_truncated": truncated,
    }
    logger.info(
        "An installed CLI ran. event=installed_cli_ran tool_id=%s command=%s exit_code=%s program_sha256=%s",
        tool.tool_id,
        checked.command_path,
        code,
        tool.installed_cli.sha256[:12],
    )
    if code:
        return ToolResult("error", content, f"The command exited with status {code}.")
    return ToolResult("success", content)


__all__ = ["MAX_OUTPUT_BYTES", "REASON_SANDBOX_UNAVAILABLE", "RUN_TIMEOUT_SECONDS", "run_installed_cli"]
