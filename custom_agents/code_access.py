"""What one code command may reach (access plan, Phase 5, section 8.6).

The sandbox launcher is an enforcement point. For each shell or Python
command it asks here:

- **Folders.** The task's own folders (the person's folder, ``team/``,
  ``agents/``), the operator's data folders, the folders in the agent's
  grants for this person's work (decision D13) and the folders other people
  shared with the person (decision D15) are each mounted read-write when both
  the agent and the person it works for may write there, read-only when they
  may only read, and not at all otherwise. Landlock rules only ever add access, so a folder with a block
  somewhere below it is not mounted at all (the command result says so)
  rather than mounted with the blocked part exposed. One-time grants never
  count: a mount lasts the whole command.
- **Network.** Code may open TCP connections only when the agent and its
  person hold internet access (``fn:internet``, decision D14). It is standard
  for every role, so the network is open unless a block closes it; full
  access to a code tool has nothing to do with it. A one-time grant would be
  used up by the call itself, so it never opens the network.
- **Outside the sandbox.** Only when the call asks for it and the agent and
  its person hold ``full`` on ``code:unconfined``, a separate tool so that
  network access never implies host access. The agent asks with
  ``request_access``; a person approves in the Inbox; the next command runs
  unconfined.

With authorization off (decision D8) there are no grants to consult: the
task's folders and the writable data folders are read-write, the others
read-only, the network is open, and code runs outside the sandbox only when
the operator turns the sandbox off.

A command with no agent behind it is refused. Every agent tool call knows its
agent (``custom_tools.runtime_context``); losing it on a thread hop once let
parallel shell commands run with the network open despite a person's block.
Only code that embeds the tools on purpose (tests, a script) may run commands
without an agent, inside ``embedded_commands()``, and never in a process the
runtime's supervisor started.
"""

from __future__ import annotations

import logging
import os
import threading
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from access_control.decisions import UNCONFINED_EXECUTION_OPERATION, UNCONFINED_EXECUTION_TOOL_ID
from access_control.folders import (
    FOLDER_ROOT_DATA,
    DataFolder,
    FolderIdError,
    FolderRoots,
    covers,
)
from access_control.decisions import AccessService
from access_control.entities import AgentPrincipal, PersonPrincipal
from access_control.identity import IdentityRejected
from access_control.standard_grants import INTERNET_TOOL_ID
from custom_agents.access_runtime import (
    REASON_ACCESS_DENIED,
    authorization_enabled,
    refusal_text,
    runtime_access,
    task_access,
)
from custom_messages.contracts.access_v2 import (
    AccessLevel,
    ClassificationSource,
    GrantKind,
    GrantV1,
    OperationResolutionV1,
    TargetKind,
    TargetRefV1,
)
from runtime_hosting.process_run import PROCESS_RUN_ID_ENV
from runtime_settings.instance_layout import WorkspaceScope

logger = logging.getLogger(__name__)

CODE_SHELL_TOOL_ID = "code:shell"
CODE_PYTHON_TOOL_ID = "code:python"
UNCONFINED_TOOL_ID = UNCONFINED_EXECUTION_TOOL_ID
UNCONFINED_OPERATION = UNCONFINED_EXECUTION_OPERATION
REASON_UNCONFINED_UNAVAILABLE = "unconfined_execution_unavailable"
REASON_IDENTITY_REJECTED = "identity_rejected"
REASON_NO_AGENT = "code_without_agent"
NO_AGENT_REFUSAL = (
    "This command has no agent behind it, so nothing decides what it may reach; it was not run. "
    "This is a runtime fault, not something to work around: report it."
)
UNCONFINED_WITH_AUTHORIZATION_OFF = (
    "Running code outside the sandbox is not something an agent can ask for while authorization "
    "is off: only the operator can turn the sandbox off, for every agent "
    "(security_layer.code_execution.sandbox: off). Tell the person exactly what the command needs "
    "and why, and let them decide."
)


class CodeAccessRefused(PermissionError):
    """The command may not run as asked; the message says why and what to do."""

    def __init__(self, message: str, *, reason_code: str) -> None:
        super().__init__(message)
        self.reason_code = reason_code


@dataclass(frozen=True)
class CodeAccess:
    """The folders, network and sandbox one command gets."""

    read_write: tuple[Path, ...]
    read_only: tuple[Path, ...]
    network: bool
    unconfined: bool = False
    notes: tuple[str, ...] = field(default=())


def _without_authorization(scope: WorkspaceScope, data_folders: tuple[DataFolder, ...], unconfined: bool) -> CodeAccess:
    if unconfined:
        raise CodeAccessRefused(UNCONFINED_WITH_AUTHORIZATION_OFF, reason_code=REASON_UNCONFINED_UNAVAILABLE)
    return CodeAccess(
        read_write=(*scope.roots, *(folder.path for folder in data_folders if folder.writable)),
        read_only=tuple(folder.path for folder in data_folders if not folder.writable),
        network=True,
    )


# How many embedded_commands() blocks are open. Process-wide rather than a
# context variable, because embedding code and tests run commands from threads
# that do not inherit context.
_embedded_users = 0
_embedded_lock = threading.Lock()


@contextmanager
def embedded_commands() -> Iterator[None]:
    """Let commands with no agent behind them run, for code that embeds the tools on purpose.

    Tests and scripts that call the shell or Python tools directly use this.
    It has no effect in a process the runtime's supervisor started
    (``UA_PROCESS_RUN_ID`` set, runtime_hosting.process_run), where a command
    without an agent is always a fault.
    """

    global _embedded_users
    with _embedded_lock:
        _embedded_users += 1
    try:
        yield
    finally:
        with _embedded_lock:
            _embedded_users -= 1


def no_agent_refusal_reason() -> str | None:
    """Why a command with no agent must be refused now; None inside embedded_commands()."""

    if os.environ.get(PROCESS_RUN_ID_ENV):
        return "runtime_process"
    if not _embedded_users:
        return "not_embedded"
    return None


def code_access_without_agent(scope: WorkspaceScope, *, unconfined: bool = False) -> CodeAccess:
    """Embedded use with no agent: the task's folders, as in single-person mode.

    Raises ``CodeAccessRefused`` unless inside ``embedded_commands()`` and
    outside a supervised runtime process.
    """

    if no_agent_refusal_reason() is not None:
        raise CodeAccessRefused(NO_AGENT_REFUSAL, reason_code=REASON_NO_AGENT)
    return _without_authorization(scope, (), unconfined)


@dataclass(frozen=True)
class _Planner:
    """One command's decisions for one agent, its person and its task."""

    service: AccessService
    principal: AgentPrincipal
    person: PersonPrincipal | None
    task_id: str | None

    def unconfined(self) -> CodeAccess:
        """Outside the sandbox: only on full access to code:unconfined."""

        decision = self.service.decide(
            agent=self.principal,
            person=self.person,
            resolution=OperationResolutionV1(
                tool_id=UNCONFINED_TOOL_ID,
                operation=UNCONFINED_OPERATION,
                level=AccessLevel.FULL,
                source=ClassificationSource.TRUSTED,
            ),
            task_id=self.task_id,
            hand_on_for_task=True,
        )
        if not decision.allowed:
            raise CodeAccessRefused(refusal_text(decision), reason_code=REASON_ACCESS_DENIED)
        logger.warning(
            "A command runs outside the code sandbox on an approved grant. event=code_unconfined_run "
            "agent_id=%s agent_type=%s person_id=%s task_id=%s grant_ids=%s",
            self.principal.agent_id,
            self.principal.agent_type,
            self.person.person_id if self.person else None,
            self.task_id,
            ",".join(decision.agent_grant_ids),
        )
        return CodeAccess(read_write=(), read_only=(), network=True, unconfined=True)

    def network(self) -> bool:
        """TCP only with internet access (decision D14), for the agent and its person.

        Internet access is standard for every role, so this is open unless a
        block closes it; full access to a code tool does not matter here.
        """

        internet = TargetRefV1(kind=TargetKind.TOOL, id=INTERNET_TOOL_ID)
        person_id = self.person.person_id if self.person else None
        return self.service.holds(
            self.principal, internet, AccessLevel.READ, task_id=self.task_id, person_id=person_id
        ) and (self.person is None or self.service.holds(self.person, internet, AccessLevel.READ, task_id=self.task_id))

    def grants(self) -> tuple[list[GrantV1], list[GrantV1]]:
        """The agent's grants for this person's work (with what was shared with the person), and the person's."""

        person_id = self.person.person_id if self.person else None
        agent_grants = self.service.agent_grants(self.principal, task_id=self.task_id, person_id=person_id)
        agent_grants += self.service.shared_with(self.principal, self.person, self.task_id)
        person_grants = self.service.valid_grants(self.person.subjects(), task_id=self.task_id) if self.person else []
        return agent_grants, person_grants

    def level(self, folder_id: str, writable_root: bool) -> AccessLevel | None:
        """The level to mount a folder at: write, read, or None (not at all)."""

        for level in (AccessLevel.WRITE, AccessLevel.READ):
            if level is AccessLevel.WRITE and not writable_root:
                continue
            if self.service.may_use_folder(
                agent=self.principal, person=self.person, folder_id=folder_id, level=level, task_id=self.task_id
            ):
                return level
        return None


def _candidate_folders(
    scope: WorkspaceScope, roots: FolderRoots, data_folders: tuple[DataFolder, ...], agent_grants: list[GrantV1]
) -> list[str]:
    """The task's folders, the data folders, and the folders in the agent's lasting grants."""

    candidates: dict[str, None] = {}
    for root in scope.roots:
        folder_id = roots.folder_id_of(Path(os.path.realpath(root)))
        if folder_id:
            candidates[folder_id] = None
    for folder in data_folders:
        candidates[f"{FOLDER_ROOT_DATA}/{folder.name}"] = None
    for grant in agent_grants:
        if grant.kind is GrantKind.ACCESS and grant.target.kind is TargetKind.FOLDER and grant.uses_remaining is None:
            candidates[grant.target.id] = None
    return list(candidates)


def _blocked_inside(folder_id: str, blocked: list[str]) -> list[str]:
    return sorted(block for block in blocked if block != folder_id and covers(folder_id, block))


def _planner(agent: Any) -> _Planner:
    access = runtime_access(agent)
    assert access.service is not None
    try:
        principal, person, task_id = task_access(agent, access)
    except (IdentityRejected, ValueError) as exc:
        raise CodeAccessRefused(
            f"Nobody verifiably asked for this task ({exc}); code cannot run for it.",
            reason_code=REASON_IDENTITY_REJECTED,
        ) from exc
    # Access for one task is bound to the request the task works on.
    return _Planner(access.service, principal, person, principal.work_id)


def code_access(
    agent: Any,
    scope: WorkspaceScope,
    *,
    tool_id: str,
    unconfined: bool = False,
    data_folders: tuple[DataFolder, ...] = (),
) -> CodeAccess:
    """What the agent's next command may reach; raises ``CodeAccessRefused``."""

    if not authorization_enabled(getattr(agent, "authorization_config", None)):
        return _without_authorization(scope, data_folders, unconfined)
    planner = _planner(agent)
    if unconfined:
        return planner.unconfined()
    roots = FolderRoots(Path(os.path.realpath(scope.workspaces_dir)), data_folders)
    agent_grants, person_grants = planner.grants()
    blocked = [
        grant.target.id
        for grant in (*agent_grants, *person_grants)
        if grant.kind is GrantKind.BLOCK and grant.target.kind is TargetKind.FOLDER
    ]
    mounts: dict[AccessLevel, list[Path]] = {AccessLevel.WRITE: [], AccessLevel.READ: []}
    notes: list[str] = []
    for folder_id in _candidate_folders(scope, roots, data_folders, agent_grants):
        try:
            path = roots.path_of(folder_id)
        except FolderIdError:
            continue  # a grant on a data folder the operator no longer registers
        inner = _blocked_inside(folder_id, blocked)
        if inner:
            notes.append(
                f"{folder_id}/ is not available to code because {', '.join(inner)} inside it is blocked; "
                "the sandbox cannot leave out part of a folder. File tools still reach the rest."
            )
            continue
        data = roots.data_folder(folder_id)
        level = planner.level(folder_id, writable_root=data is None or data.writable)
        if level is not None:
            mounts[level].append(path)
    planned = CodeAccess(
        tuple(mounts[AccessLevel.WRITE]), tuple(mounts[AccessLevel.READ]), planner.network(), notes=tuple(notes)
    )
    logger.info(
        "Code sandbox access planned. event=code_access_planned agent_id=%s task_id=%s tool_id=%s "
        "read_write=%s read_only=%s network=%s skipped=%s",
        planner.principal.agent_id,
        planner.task_id,
        tool_id,
        ",".join(str(path) for path in planned.read_write),
        ",".join(str(path) for path in planned.read_only),
        planned.network,
        len(notes),
    )
    return planned


__all__ = [
    "CODE_PYTHON_TOOL_ID",
    "CODE_SHELL_TOOL_ID",
    "CodeAccess",
    "CodeAccessRefused",
    "NO_AGENT_REFUSAL",
    "REASON_IDENTITY_REJECTED",
    "REASON_NO_AGENT",
    "REASON_UNCONFINED_UNAVAILABLE",
    "UNCONFINED_TOOL_ID",
    "UNCONFINED_WITH_AUTHORIZATION_OFF",
    "code_access",
    "code_access_without_agent",
    "embedded_commands",
    "no_agent_refusal_reason",
]
