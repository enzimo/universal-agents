"""One OpenShell sandbox per person (access plan, Phase 5; decision D6).

In OpenShell mode (``security_layer.mode: openshell``) agent code runs in an
OpenShell sandbox instead of the built-in Landlock sandbox. OpenShell copies
files into a sandbox when it is created (``--upload``); it does not mount
them. One shared sandbox would therefore hold a copy of every person's folder,
and one person's code could read another's. So each person gets their own
sandbox, created the first time their code runs, holding copies of only their
folder and the shared ``team/`` and ``agents/`` folders; work for no person
gets a ``shared`` sandbox with the two shared folders.

What stays with the operator in this mode: the OpenShell policy file decides
the network and anything else the sandbox may reach, and the folder grants and
network rule of the built-in sandbox (``custom_agents.code_access``) do not
apply to it. Files written inside an OpenShell sandbox stay there; files
written on the host after the sandbox was created are not copied in.

The launcher passes the settings as environment variables
(``OPENSHELL_SANDBOX_NAME`` is the name prefix) and deletes the instance's
sandboxes when it stops. This module uses only the standard library and the
instance layout, so the launcher can use it too.
"""

from __future__ import annotations

import logging
import subprocess
import threading
import time
from collections.abc import Mapping
from pathlib import Path

from runtime_settings.instance_layout import (
    AGENTS_DIRNAME,
    TEAM_DIRNAME,
    USERS_DIRNAME,
    WorkspaceScope,
)
from runtime_settings.openshell_naming import (
    DEFAULT_SANDBOX_PREFIX,
    ENV_SANDBOX_NAME,
    is_instance_sandbox,
    sandbox_name,
)

logger = logging.getLogger(__name__)

ENV_WORKSPACE_MOUNT = "OPENSHELL_WORKSPACE_MOUNT"
ENV_SANDBOX_IMAGE = "OPENSHELL_SANDBOX_IMAGE"
ENV_POLICY_FILE = "OPENSHELL_SANDBOX_POLICY_FILE"
ENV_UPLOAD_WORKSPACE = "OPENSHELL_UPLOAD_WORKSPACE"
ENV_NO_GIT_IGNORE = "OPENSHELL_NO_GIT_IGNORE"
ENV_PROVISION_TIMEOUT = "OPENSHELL_PROVISION_TIMEOUT"
ENV_READINESS_TIMEOUT = "OPENSHELL_READINESS_TIMEOUT_SECONDS"
ENV_CREATE_IF_MISSING = "OPENSHELL_CREATE_IF_MISSING"
ENV_INSTANCE_LABEL = "OPENSHELL_INSTANCE_LABEL"
INSTANCE_LABEL_KEY = "team_nats_agents_instance"
DEFAULT_WORKSPACE_MOUNT = "/workspace"
DEFAULT_PROVISION_TIMEOUT_SECONDS = 300
DEFAULT_READINESS_TIMEOUT_SECONDS = 120
_TRUE_WORDS = frozenset({"1", "true", "yes", "on"})

_ready: set[str] = set()
_ready_lock = threading.Lock()


class OpenShellSandboxError(RuntimeError):
    """The person's OpenShell sandbox could not be found or created."""


def workspace_mount(env: Mapping[str, str]) -> str:
    return (str(env.get(ENV_WORKSPACE_MOUNT) or "").strip() or DEFAULT_WORKSPACE_MOUNT).rstrip("/")


def upload_arguments(scope: WorkspaceScope, mount: str) -> list[str]:
    """``--upload`` copies of the person's folder and the shared folders, nothing else."""

    arguments: list[str] = []
    for root in scope.roots:
        relative = Path(root).relative_to(scope.workspaces_dir).as_posix()
        if relative.split("/", 1)[0] not in {USERS_DIRNAME, TEAM_DIRNAME, AGENTS_DIRNAME}:
            raise OpenShellSandboxError(f"Not a workspace folder: {root}")
        arguments += ["--upload", f"{root}:{mount}/{relative}"]
    return arguments


def create_arguments(name: str, scope: WorkspaceScope, env: Mapping[str, str]) -> list[str]:
    """``openshell sandbox create`` arguments for one person's sandbox."""

    arguments = ["sandbox", "create", "--name", name, "--no-tty", "--no-auto-providers"]
    label = str(env.get(ENV_INSTANCE_LABEL) or "").strip()
    if label:
        arguments += ["--label", f"{INSTANCE_LABEL_KEY}={label}"]
    image = str(env.get(ENV_SANDBOX_IMAGE) or "").strip()
    if image:
        arguments += ["--from", image]
    policy = str(env.get(ENV_POLICY_FILE) or "").strip()
    if policy:
        arguments += ["--policy", policy]
    if str(env.get(ENV_UPLOAD_WORKSPACE, "true")).strip().lower() in _TRUE_WORDS:
        for root in scope.roots:
            Path(root).mkdir(parents=True, exist_ok=True)
        arguments += upload_arguments(scope, workspace_mount(env))
        if str(env.get(ENV_NO_GIT_IGNORE, "")).strip().lower() in _TRUE_WORDS:
            arguments.append("--no-git-ignore")
    return [*arguments, "--", "sh", "-lc", "true"]


def _run(openshell: str, arguments: list[str], env: Mapping[str, str], timeout: int) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [openshell, *arguments],
        env=dict(env),
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=timeout,
        check=False,
    )


def existing_sandboxes(openshell: str, env: Mapping[str, str]) -> set[str]:
    completed = _run(openshell, ["sandbox", "list", "--names"], env, 60)
    if completed.returncode != 0:
        raise OpenShellSandboxError(f"Could not list OpenShell sandboxes: {(completed.stdout or '').strip()}")
    return {line.strip() for line in (completed.stdout or "").splitlines() if line.strip()}


def _wait_until_ready(openshell: str, name: str, env: Mapping[str, str], timeout: int) -> None:
    deadline = time.monotonic() + max(1, timeout)
    last = ""
    while time.monotonic() < deadline:
        completed = _run(openshell, ["sandbox", "exec", "-n", name, "--no-tty", "--timeout", "5", "--", "true"], env, 15)
        if completed.returncode == 0:
            return
        last = (completed.stdout or "").strip()
        time.sleep(1)
    raise OpenShellSandboxError(f"OpenShell sandbox {name!r} did not become ready: {last}")


def ensure_sandbox(openshell: str, scope: WorkspaceScope, env: Mapping[str, str]) -> str:
    """The name of the sandbox for this scope's person, created on first use."""

    name = sandbox_name(str(env.get(ENV_SANDBOX_NAME) or DEFAULT_SANDBOX_PREFIX), scope.person_id)
    with _ready_lock:
        if name in _ready:
            return name
        if name not in existing_sandboxes(openshell, env):
            if str(env.get(ENV_CREATE_IF_MISSING, "true")).strip().lower() not in _TRUE_WORDS:
                raise OpenShellSandboxError(
                    f"OpenShell sandbox {name!r} does not exist and creating sandboxes is turned off "
                    "(security_layer.openshell.create_if_missing); the operator must create it."
                )
            timeout = int(str(env.get(ENV_PROVISION_TIMEOUT) or DEFAULT_PROVISION_TIMEOUT_SECONDS))
            logger.info(
                "Creating a person's OpenShell sandbox. event=openshell_sandbox_create name=%s person_id=%s",
                name,
                scope.person_id,
            )
            completed = _run(openshell, create_arguments(name, scope, env), env, timeout + 30)
            if completed.returncode != 0:
                raise OpenShellSandboxError(
                    f"Could not create OpenShell sandbox {name!r}: {(completed.stdout or '').strip()}"
                )
            _wait_until_ready(
                openshell, name, env, int(str(env.get(ENV_READINESS_TIMEOUT) or DEFAULT_READINESS_TIMEOUT_SECONDS))
            )
        _ready.add(name)
    return name


def delete_person_sandbox(openshell: str, person_id: str, env: Mapping[str, str]) -> bool:
    """Delete one person's sandbox (a person removed from the team); False when they had none.

    Their files inside it are copies, so nothing held for a restore is lost.
    Raises ``OpenShellSandboxError`` when OpenShell refuses.
    """

    name = sandbox_name(str(env.get(ENV_SANDBOX_NAME) or DEFAULT_SANDBOX_PREFIX), person_id)
    with _ready_lock:
        _ready.discard(name)
        if name not in existing_sandboxes(openshell, env):
            return False
        completed = _run(openshell, ["sandbox", "delete", name], env, 60)
    if completed.returncode != 0:
        raise OpenShellSandboxError(f"Could not delete OpenShell sandbox {name!r}: {(completed.stdout or '').strip()}")
    logger.info(
        "Deleted a removed person's OpenShell sandbox. event=openshell_sandbox_deleted name=%s person_id=%s",
        name,
        person_id,
    )
    return True


def instance_sandboxes(openshell: str, env: Mapping[str, str]) -> list[str]:
    """This instance's sandboxes: the per-person and shared ones, and an old one named the prefix."""

    prefix = str(env.get(ENV_SANDBOX_NAME) or DEFAULT_SANDBOX_PREFIX)
    return sorted(name for name in existing_sandboxes(openshell, env) if is_instance_sandbox(prefix, name))


def forget_ready_sandboxes() -> None:
    """Forget which sandboxes exist (tests, and after the launcher deleted them)."""

    with _ready_lock:
        _ready.clear()


__all__ = [
    "DEFAULT_WORKSPACE_MOUNT",
    "ENV_CREATE_IF_MISSING",
    "ENV_INSTANCE_LABEL",
    "ENV_NO_GIT_IGNORE",
    "ENV_POLICY_FILE",
    "ENV_PROVISION_TIMEOUT",
    "ENV_READINESS_TIMEOUT",
    "ENV_SANDBOX_IMAGE",
    "ENV_SANDBOX_NAME",
    "ENV_UPLOAD_WORKSPACE",
    "ENV_WORKSPACE_MOUNT",
    "OpenShellSandboxError",
    "create_arguments",
    "delete_person_sandbox",
    "ensure_sandbox",
    "existing_sandboxes",
    "forget_ready_sandboxes",
    "instance_sandboxes",
    "sandbox_name",
    "upload_arguments",
    "workspace_mount",
]
