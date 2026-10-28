"""Access model contracts, version 2: the tool catalog and access levels.

Everything access can be given on is a *tool*, and everything a tool can do is
an *operation* with a required *level*. Levels are ordered
``read`` < ``write`` < ``full``; a grant at a level covers every operation that
needs that level or a lower one. An operation nobody has classified requires
``full``, so a narrowed grant never covers work nobody reviewed.

The catalog is built from trusted definitions (``ua_definitions/access``), the
installed MCP connections, and installed procedures (``access_catalog``). It
grants nothing by itself; grants arrive with the access service.

These contracts do not depend on the version 1 authorization contracts, which
the access model replaces.
"""

from __future__ import annotations

import hashlib
import json
import re
from datetime import datetime
from enum import StrEnum
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from custom_messages.contracts.cli_tool_v1 import CLI_NAME_PATTERN, InstalledCliV1
from model_providers.endpoints import EndpointKind, endpoint_address
from custom_messages.contracts.tool_setup_spec_v1 import ToolSetupSpecV1


ACCESS_CONTRACT_SCHEMA_VERSION: Literal[2] = 2

TOOL_ID_PATTERN = re.compile(r"^(fn|cli|http|mcp|procedure|code):[a-z0-9][a-z0-9_.-]{0,95}$")
OPERATION_NAME_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.:/-]{0,199}$")
TOOL_SET_ID_PATTERN = re.compile(r"^[a-z0-9][a-z0-9_.-]{0,63}$")
INJECTION_NAME_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,127}$")
_FILE_PART_PATTERN = re.compile(r"^[A-Za-z0-9._][A-Za-z0-9_.-]{0,127}$")
CREDENTIAL_KEY_PATTERN = re.compile(r"^[a-zA-Z0-9][a-zA-Z0-9_.-]{0,119}$")
HTTP_METHODS = frozenset({"GET", "HEAD", "OPTIONS", "POST", "PUT", "PATCH", "DELETE"})


class AccessLevel(StrEnum):
    """How much a grant allows; ordered read < write < full."""

    READ = "read"
    WRITE = "write"
    FULL = "full"

    @property
    def rank(self) -> int:
        return _LEVEL_RANK[self]

    def covers(self, required: "AccessLevel") -> bool:
        """Whether holding this level allows an operation that needs ``required``."""

        return self.rank >= required.rank


_LEVEL_RANK = {AccessLevel.READ: 0, AccessLevel.WRITE: 1, AccessLevel.FULL: 2}


class ToolKind(StrEnum):
    """How a tool is delivered. The grant is on the tool, not on its kind."""

    FN = "fn"
    CLI = "cli"
    HTTP = "http"
    MCP = "mcp"
    PROCEDURE = "procedure"
    CODE = "code"


class ClassificationSource(StrEnum):
    """Who decided an operation's level."""

    TRUSTED = "trusted"  # shipped with Universal Agents and reviewed in code review
    ADMIN_CONFIRMED = "admin_confirmed"  # an administrator confirmed it for this instance
    UNREVIEWED = "unreviewed"  # nobody classified it; it requires full


class AccountMode(StrEnum):
    """Which account a tool acts with."""

    NONE = "none"
    PERSONAL_LOGIN = "personal_login"  # the acting person's own broker-held login
    LINKED_CREDENTIAL = "linked_credential"  # chosen through an account link


class AccountSharing(StrEnum):
    """Whether the team's account for a service serves everyone's work (``access_control.account_sharing``)."""

    SHARED = "shared"  # the team's account serves anyone without their own
    PRIVATE = "private"  # the team's account is never used; each person uses their own


class OwnerScope(StrEnum):
    TEAM = "team"
    PLATFORM = "platform"  # platform objects: only administrators hold full
    # Team settings every person shares (model choice): only administrators
    # hold or hand them on at any level (Cedar guard:admin-only-tools), and an
    # agent may hold them for an administrator's work.
    ADMINISTRATORS = "administrators"


def contract_digest(payload: Any) -> str:
    """SHA-256 of canonical JSON, used to pin definitions."""

    rendered = json.dumps(payload, sort_keys=True, separators=(",", ":"), default=str)
    return hashlib.sha256(rendered.encode("utf-8")).hexdigest()


class AccessContractModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class OperationDefinitionV1(AccessContractModel):
    """One thing a tool can do, and the level it needs."""

    name: str = Field(min_length=1, max_length=200)
    level: AccessLevel
    description: str = Field(default="", max_length=1000)
    source: ClassificationSource = ClassificationSource.TRUSTED
    # From untrusted hints (MCP annotations); shown to the person confirming.
    suggested_level: AccessLevel | None = None

    @field_validator("name")
    @classmethod
    def _valid_name(cls, value: str) -> str:
        if not OPERATION_NAME_PATTERN.fullmatch(value):
            raise ValueError(f"Invalid operation name: {value!r}")
        return value

    @model_validator(mode="after")
    def _unreviewed_is_full(self) -> "OperationDefinitionV1":
        if self.source is ClassificationSource.UNREVIEWED and self.level is not AccessLevel.FULL:
            raise ValueError(f"Unreviewed operation {self.name!r} must require full access.")
        return self


class HttpRouteV1(AccessContractModel):
    """A named route of a web API tool; overrides the method's default level."""

    name: str = Field(min_length=1, max_length=120)
    method: str
    path: str = Field(min_length=1, max_length=500)
    level: AccessLevel

    @field_validator("method")
    @classmethod
    def _valid_method(cls, value: str) -> str:
        method = value.strip().upper()
        if method not in HTTP_METHODS:
            raise ValueError(f"Unsupported HTTP method: {value!r}")
        return method

    @field_validator("path")
    @classmethod
    def _valid_path(cls, value: str) -> str:
        if not value.startswith("/") or "//" in value or "/../" in f"{value}/" or "?" in value:
            raise ValueError(f"Route paths are absolute, normalized, and have no query: {value!r}")
        return value


class InjectionPlacement(StrEnum):
    """Where the broker puts a credential value."""

    HEADER = "header"  # web requests and MCP servers over HTTP
    QUERY = "query"  # web requests whose API takes a key in the query
    ENV = "env"  # MCP servers started as local processes, and installed CLIs
    # A file in an installed CLI's private home folder; ``name`` is its path there.
    FILE = "file"


_FORBIDDEN_HEADERS = frozenset({"host", "content-length", "transfer-encoding", "connection"})


class CredentialInjectionV1(AccessContractModel):
    """One credential field and where the broker injects it (never the value)."""

    placement: InjectionPlacement
    name: str = Field(min_length=1, max_length=128)
    field: str = Field(min_length=1, max_length=120)
    # A file's template may span lines (a small config file); others are one line.
    template: str = Field(default="{value}", min_length=7, max_length=1000)

    @field_validator("name")
    @classmethod
    def _valid_name(cls, value: str) -> str:
        # A file name is a relative path of plain names, which may start with a
        # dot (.config); other names are checked per placement below.
        if not all(_FILE_PART_PATTERN.fullmatch(part) and part not in {".", ".."} for part in value.split("/")):
            raise ValueError(f"Invalid injection name: {value!r}")
        return value

    @field_validator("template")
    @classmethod
    def _valid_template(cls, value: str) -> str:
        if value.count("{value}") != 1 or value.replace("{value}", "").count("{") or "}" in value.replace("{value}", ""):
            raise ValueError("An injection template contains '{value}' exactly once and no other braces.")
        if "\r" in value:
            raise ValueError("An injection template has no carriage returns.")
        return value

    @model_validator(mode="after")
    def _allowed_header(self) -> "CredentialInjectionV1":
        if self.placement is InjectionPlacement.HEADER and self.name.lower() in _FORBIDDEN_HEADERS:
            raise ValueError(f"The broker does not inject credentials into the {self.name} header.")
        if self.placement is not InjectionPlacement.FILE and (
            "\n" in self.template or not INJECTION_NAME_PATTERN.fullmatch(self.name)
        ):
            raise ValueError(f"Invalid injection name {self.name!r}: only a file injection spans lines or names a path.")
        if self.placement is InjectionPlacement.FILE and ".." in self.name.split("/"):
            raise ValueError("A credential file stays inside the program's home folder.")
        return self

    def render(self, value: str) -> str:
        return self.template.replace("{value}", value)


class ToolDefinitionV1(AccessContractModel):
    """A registered capability people grant access to."""

    schema_version: Literal[2] = ACCESS_CONTRACT_SCHEMA_VERSION
    tool_id: str
    kind: ToolKind
    display_name: str = Field(min_length=1, max_length=120)
    description: str = Field(default="", max_length=2000)
    owner_scope: OwnerScope = OwnerScope.TEAM
    operations: tuple[OperationDefinitionV1, ...] = ()
    account: AccountMode = AccountMode.NONE
    hosts: tuple[str, ...] = ()
    routes: tuple[HttpRouteV1, ...] = ()
    tool_sets: tuple[str, ...] = ()
    # Where a linked credential goes by default; the account link form shows
    # it and the link records what the person confirmed.
    credential_injections: tuple[CredentialInjectionV1, ...] = ()
    # Steps outside Universal Agents and tools to set up first (set_up_tools).
    setup: ToolSetupSpecV1 | None = None
    # Whose account link the broker may use, in order: the person's own, then
    # the team's (the default); only the person's; or only the team's.
    link_owners: tuple[Literal["person", "team"], ...] = ("person", "team")
    # For a tool that takes both (``sharing_choice``): whether the team's
    # account serves everyone until an administrator chooses otherwise
    # (``access_control.account_sharing``); None there means shared. Only
    # such tools may name one.
    default_sharing: AccountSharing | None = None
    # A CLI the team installed through a proposal (custom_messages.contracts.cli_tool_v1).
    installed_cli: InstalledCliV1 | None = None

    @property
    def sharing_choice(self) -> bool:
        """Whether the team chooses if its account serves everyone: a linked account that takes the person's and the team's."""

        return self.account is AccountMode.LINKED_CREDENTIAL and set(self.link_owners) == {LinkOwnerKind.PERSON.value, LinkOwnerKind.TEAM.value}

    @field_validator("tool_id")
    @classmethod
    def _valid_tool_id(cls, value: str) -> str:
        if not TOOL_ID_PATTERN.fullmatch(value):
            raise ValueError(f"Invalid tool id: {value!r}; expected '<kind>:<name>'.")
        return value

    @field_validator("hosts")
    @classmethod
    def _valid_hosts(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        hosts = tuple(host.strip().lower() for host in value)
        for host in hosts:
            if not host or "*" in host or "/" in host or ":" in host:
                raise ValueError(f"Hosts are exact names without wildcards or ports: {host!r}")
        return hosts

    @model_validator(mode="after")
    def _consistent(self) -> "ToolDefinitionV1":
        if self.tool_id.split(":", 1)[0] != self.kind.value:
            raise ValueError(f"Tool id {self.tool_id!r} does not match kind {self.kind.value!r}.")
        names = [operation.name for operation in self.operations]
        if len(names) != len(set(names)):
            raise ValueError(f"Tool {self.tool_id!r} lists an operation twice.")
        self._check_routes()
        if self.credential_injections and self.account is not AccountMode.LINKED_CREDENTIAL:
            raise ValueError(f"Only tools that use a linked credential declare injections: {self.tool_id!r}.")
        self._check_setup()
        self._check_installed_cli()
        if self.default_sharing is not None and not self.sharing_choice:
            raise ValueError(
                f"Only a tool that takes a person's and the team's linked account has a default_sharing: {self.tool_id!r}."
            )
        for tool_set in self.tool_sets:
            if not TOOL_SET_ID_PATTERN.fullmatch(tool_set):
                raise ValueError(f"Invalid tool set id: {tool_set!r}")
        return self

    def _check_installed_cli(self) -> None:
        """Installed programs are CLI tools named after their folder; file credentials are theirs only."""

        if self.installed_cli is not None and self.tool_id != f"cli:{self.installed_cli.name}":
            raise ValueError(f"An installed CLI's tool id is cli:<its folder name>, not {self.tool_id!r}.")
        if self.installed_cli is None and any(
            item.placement is InjectionPlacement.FILE for item in self.credential_injections
        ):
            raise ValueError(f"Only installed CLIs take a credential file: {self.tool_id!r}.")
        if not self.link_owners or len(set(self.link_owners)) != len(self.link_owners):
            raise ValueError(f"link_owners lists person and/or team once each: {self.tool_id!r}.")

    def _check_setup(self) -> None:
        """External steps prepare an account, so only tools with one have them; no tool needs itself."""

        if self.setup is None:
            return
        if self.setup.external_steps and self.account is AccountMode.NONE:
            raise ValueError(f"Only tools that act with an account have setup steps: {self.tool_id!r}.")
        if self.tool_id in self.setup.prerequisites:
            raise ValueError(f"Tool {self.tool_id!r} cannot be its own prerequisite.")

    def _check_routes(self) -> None:
        """Web API tools name at least one host and each route once; no other tool has routes."""

        if self.kind is not ToolKind.HTTP:
            if self.routes:
                raise ValueError(f"Only web API tools have routes: {self.tool_id!r}.")
            return
        if not self.hosts:
            raise ValueError(f"Web API tool {self.tool_id!r} needs at least one host.")
        route_names = [route.name for route in self.routes]
        if len(route_names) != len(set(route_names)):
            raise ValueError(f"Web API tool {self.tool_id!r} names a route twice.")

    def operation(self, name: str) -> OperationDefinitionV1 | None:
        for operation in self.operations:
            if operation.name == name:
                return operation
        return None

    @property
    def definition_digest(self) -> str:
        """Changes whenever the definition changes; recorded in audit."""

        return contract_digest(self.model_dump(mode="json"))


class ToolSetDefinitionV1(AccessContractModel):
    """A named group of tools; a grant on it covers tools added later."""

    tool_set_id: str
    display_name: str = Field(min_length=1, max_length=120)
    description: str = Field(default="", max_length=1000)

    @field_validator("tool_set_id")
    @classmethod
    def _valid_id(cls, value: str) -> str:
        if not TOOL_SET_ID_PATTERN.fullmatch(value):
            raise ValueError(f"Invalid tool set id: {value!r}")
        return value


class OperationResolutionV1(AccessContractModel):
    """The tool, operation, and level one concrete call needs."""

    tool_id: str
    operation: str
    level: AccessLevel
    source: ClassificationSource
    detail: str = Field(default="", max_length=500)


# ---------------------------------------------------------------------------
# Grants: the one authority record
# ---------------------------------------------------------------------------


class SubjectKind(StrEnum):
    """Who a grant is for. A grant to a type, role, or team covers its members.

    A schedule holds the access of one scheduled job (owner's decision
    2026-09-30, "access follows the work"): every agent in one of its runs is
    in it, whatever agent type runs it. Its id is ``<team>/<schedule id>``.
    """

    AGENT = "agent"
    AGENT_TYPE = "agent_type"
    ROLE = "role"
    TEAM = "team"
    USER = "user"
    SCHEDULE = "schedule"


class TargetKind(StrEnum):
    TOOL = "tool"
    TOOL_SET = "tool_set"
    OPERATION = "operation"
    CREDENTIAL = "credential"
    # A folder id below a registered root (team/…, agents/…, data/<name>/…,
    # users/<folder>/… for shares); see access_control.folders.
    FOLDER = "folder"
    # One file, named like a folder id; used when a person shares a single file.
    FILE = "file"


class GrantKind(StrEnum):
    ACCESS = "access"
    BLOCK = "block"  # denies whatever grants exist; restrictions and emergency stops
    CREDENTIAL_USE = "credential_use"


class GrantLifetime(StrEnum):
    """How long a grant lasts.

    A *task* here is the person's request (owner's decision 2026-09-30): the
    work one signed task context stands for, which every agent task working
    on it carries unchanged (the team lead's and each worker it hands work
    to; a scheduled run is one task). ``GrantV1.task_id`` is that context's id.
    """

    ONE_TIME = "one_time"  # one use, within one task; never delegable
    TASK = "task"  # until the task (the person's request) ends
    UNTIL = "until"  # until expires_at_utc
    UNTIL_REVOKED = "until_revoked"


LIFETIME_RANK = {
    GrantLifetime.ONE_TIME: 0,
    GrantLifetime.TASK: 1,
    GrantLifetime.UNTIL: 2,
    GrantLifetime.UNTIL_REVOKED: 3,
}

# Lifetimes that bind a grant to one task (GrantV1.task_id).
TASK_BOUND_LIFETIMES = frozenset({GrantLifetime.ONE_TIME, GrantLifetime.TASK})


class DelegationMode(StrEnum):
    AUTOMATIC = "automatic"  # hand-downs take effect at once; people are notified
    ASK_OWNER = "ask_owner"  # each hand-down waits for the target's approver


class GrantStatus(StrEnum):
    ACTIVE = "active"
    PENDING = "pending"  # a hand-down waiting for approval
    REVOKED = "revoked"
    DENIED = "denied"  # a pending hand-down that was refused


class SubjectRefV1(AccessContractModel):
    kind: SubjectKind
    id: str = Field(min_length=1, max_length=240)


class TargetRefV1(AccessContractModel):
    kind: TargetKind
    id: str = Field(min_length=1, max_length=400)


class GrantV1(AccessContractModel):
    """One grant, block, or credential-use permission.

    Grants are immutable except for status, use count and revocation fields:
    a change means revoke and reissue. A grant is valid only while every grant
    up its parent chain is valid, so revoking a parent ends its children.
    """

    schema_version: Literal[1] = 1
    grant_id: str = Field(min_length=1, max_length=120)
    team_id: str = Field(min_length=1, max_length=120)
    kind: GrantKind = GrantKind.ACCESS
    subject: SubjectRefV1
    target: TargetRefV1
    level: AccessLevel = AccessLevel.FULL
    lifetime: GrantLifetime = GrantLifetime.UNTIL_REVOKED
    expires_at_utc: datetime | None = None
    task_id: str | None = Field(default=None, max_length=240)
    uses_remaining: int | None = Field(default=None, ge=0)
    delegation_depth: int = Field(default=0, ge=0, le=2)
    delegation_mode: DelegationMode = DelegationMode.AUTOMATIC
    delegate_to: tuple[SubjectRefV1, ...] = ()
    parent_grant_id: str | None = Field(default=None, max_length=120)
    granted_by: SubjectRefV1
    approval_id: str | None = Field(default=None, max_length=240)
    reason: str = Field(default="", max_length=2000)
    status: GrantStatus = GrantStatus.ACTIVE
    created_at_utc: datetime
    revoked_at_utc: datetime | None = None
    revoked_by: SubjectRefV1 | None = None
    # Whose work an agent grant serves (decision D13): valid only while the
    # task works for this person, so what one person authorizes never carries
    # over to another person's tasks. None: work for anyone (standard grants,
    # administrators' grants for the team, and system work for no person).
    # An agent's restriction (a block an agent made) names the person too: it
    # is bound to the request it was made in (lifetime task, task_id) and that
    # request's person, so it never reaches the person's other requests or
    # other people's (owner's decision 2026-10-19). People's blocks name no
    # person and apply to the whole team.
    for_person_id: str | None = Field(default=None, max_length=240)

    @model_validator(mode="after")
    def _consistent(self) -> "GrantV1":
        if self.lifetime is GrantLifetime.ONE_TIME:
            if self.delegation_depth or not self.task_id or self.uses_remaining is None:
                raise ValueError("One-time grants are task-bound, have a use count, and are never delegable.")
        if self.lifetime is GrantLifetime.TASK and not self.task_id:
            raise ValueError("Task grants need the task id.")
        if self.lifetime is GrantLifetime.UNTIL and self.expires_at_utc is None:
            raise ValueError("Grants that last until a time need expires_at_utc.")
        if self.kind is not GrantKind.ACCESS and self.delegation_depth:
            raise ValueError("Only access grants can be handed on.")
        if self.subject == self.granted_by and self.parent_grant_id:
            raise ValueError("Nobody hands access to themselves.")
        problem = self._whose_work_problem()
        if problem:
            raise ValueError(problem)
        return self

    def _whose_work_problem(self) -> str | None:
        """Why this grant may not name the person whose work it serves (``for_person_id``), or None."""

        if self.for_person_id is None:
            return None
        if self.kind is GrantKind.CREDENTIAL_USE or self.subject.kind is SubjectKind.USER:
            return "Only agents' access grants and restrictions serve one person's work; people's grants cannot."
        if self.kind is GrantKind.BLOCK and (self.lifetime is not GrantLifetime.TASK or not self.task_id):
            return "A restriction for one person's work is bound to one request (lifetime task)."
        return None


class AccessEventV1(AccessContractModel):
    """Append-only record of a grant change or a decision."""

    event_id: str
    team_id: str
    kind: Literal[
        "grant_created",
        "grant_revoked",
        # A hand-down moved to the broader grant that replaced the one it came
        # from (access_control.grant_entries); detail from_parent, to_parent.
        "grant_moved",
        "grant_consumed",
        "grant_approved",
        "grant_denied",
        "decision",
        "account_link_created",
        "account_link_revoked",
        # An administrator chose whether the team's account for a service
        # serves everyone (AccountSharingV1); detail subject, sharing, previous.
        "account_sharing_changed",
        "data_folder_registered",
        "data_folder_removed",
    ]
    # The grant; the account link for link events; the folder id (data/<name>)
    # for data folder events.
    grant_id: str | None = None
    actor: SubjectRefV1 | None = None
    detail: dict[str, Any] = Field(default_factory=dict)
    recorded_at_utc: datetime


DATA_FOLDER_NAME_PATTERN = r"^[a-z0-9][a-z0-9_-]{0,63}$"


class DataFolderStatus(StrEnum):
    ACTIVE = "active"
    REMOVED = "removed"


class DataFolderRecordV1(AccessContractModel):
    """A folder outside the workspace registered as an access target (decision D16).

    Agents name it ``data/<name>`` (``access_control.folders``). With
    authorization on an agent reaches it only through folder grants;
    ``writable: false`` refuses writes whatever the grants say. Registered by
    an administrator (or through an approved proposal) and read by every
    process from the access store, so a new folder needs no restart.
    """

    schema_version: Literal[1] = 1
    record_id: str = Field(min_length=1, max_length=120)
    team_id: str = Field(min_length=1, max_length=120)
    name: str = Field(pattern=DATA_FOLDER_NAME_PATTERN)
    path: str = Field(min_length=2, max_length=1000)
    writable: bool = True
    reason: str = Field(default="", max_length=2000)
    registered_by: SubjectRefV1
    created_at_utc: datetime
    status: DataFolderStatus = DataFolderStatus.ACTIVE
    removed_at_utc: datetime | None = None
    removed_by: SubjectRefV1 | None = None

    @field_validator("path")
    @classmethod
    def _absolute(cls, value: str) -> str:
        if not value.startswith("/") or value.rstrip("/") == "" or "\x00" in value:
            raise ValueError("A data folder path must be an absolute folder, not the filesystem root.")
        return value

    @property
    def folder_id(self) -> str:
        return f"data/{self.name}"


class AccessDecisionV1(AccessContractModel):
    """The outcome of one access check, with a reason people can act on."""

    allowed: bool
    tool_id: str
    operation: str
    level: AccessLevel
    # Set for a file or folder check: the folder id of the path decided.
    folder_id: str | None = Field(default=None, max_length=400)
    reason: str = Field(default="", max_length=1000)
    next_step: str = Field(default="", max_length=1000)
    agent_grant_ids: tuple[str, ...] = ()
    person_grant_ids: tuple[str, ...] = ()
    blocking_policy_ids: tuple[str, ...] = ()
    consumed_grant_ids: tuple[str, ...] = ()


# ---------------------------------------------------------------------------
# Verified identity: signed task contexts
# ---------------------------------------------------------------------------


class TaskContextV1(AccessContractModel):
    """Who an agent task works for, signed by a trusted issuer.

    The gateway signs a context when it admits a person's task; the scheduler
    signs one for a schedule's owner; delegated child tasks carry their root
    context unchanged, so later hops cannot widen it.
    """

    schema_version: Literal[1] = 1
    context_id: str = Field(min_length=1, max_length=120)
    issuer: Literal["gateway", "scheduler"]
    team_id: str = Field(min_length=1, max_length=120)
    person_id: str | None = Field(default=None, max_length=240)
    person_roles: tuple[str, ...] = ()
    root_task_id: str | None = Field(default=None, max_length=240)
    conversation_id: str | None = Field(default=None, max_length=240)
    schedule_id: str | None = Field(default=None, max_length=240)
    issued_at_utc: datetime
    expires_at_utc: datetime


class SignedTaskContextV1(AccessContractModel):
    context: TaskContextV1
    key_id: str = Field(min_length=1, max_length=120)
    signature: str = Field(min_length=1, max_length=400)


# The prefix of a schedule run's own context id, so it can never equal the id
# of a signed task context (``task_context_...``).
SCHEDULE_RUN_ID_PREFIX = "schedule_run"


class SchedulePermitV1(AccessContractModel):
    """The manager's signed statement that one schedule's runs work for one person.

    The scheduler signs it once per schedule and renews it before it expires
    (``custom_agents.schedule_permits``), so a worker that holds no key can
    start the schedule's runs without asking the manager each time. It names
    no roles: whoever checks a run reads the person's current roles and
    standing, and the schedule row's current owner (``access_control.task_identity``).
    ``person_id`` None is system work for no person, only for the runtime's
    maintenance schedule of a built-in system agent. ``record_kind`` and the
    signature's own domain keep a permit from ever passing as a task context.
    """

    schema_version: Literal[1] = 1
    record_kind: Literal["schedule_permit"] = "schedule_permit"
    permit_id: str = Field(min_length=1, max_length=120)
    issuer: Literal["scheduler"]
    team_id: str = Field(min_length=1, max_length=120)
    schedule_id: str = Field(min_length=1, max_length=240)
    person_id: str | None = Field(default=None, max_length=240)
    issued_at_utc: datetime
    expires_at_utc: datetime


class SignedSchedulePermitV1(AccessContractModel):
    permit: SchedulePermitV1
    key_id: str = Field(min_length=1, max_length=120)
    signature: str = Field(min_length=1, max_length=400)


class ScheduleRunIdentityV1(AccessContractModel):
    """Who one scheduled run works for: its schedule's signed permit and this run's own fields.

    Travels in ``GatewayContext.schedule_run`` instead of a signed task
    context when the agent that started the run holds no scheduler key.
    ``context_id``, ``conversation_id`` and ``started_at_utc`` are not signed:
    they tie the run's tasks together and date the run, and grant nothing.
    """

    permit: SignedSchedulePermitV1
    context_id: str = Field(min_length=1, max_length=120, pattern=rf"^{SCHEDULE_RUN_ID_PREFIX}_")
    conversation_id: str | None = Field(default=None, max_length=240)
    started_at_utc: datetime


# ---------------------------------------------------------------------------
# Account links: which stored credential a tool uses
# ---------------------------------------------------------------------------


# Reason code when a tool that needs a linked account has none; the gate, the
# executors and the MCP runtime all report this one value.
REASON_ACCOUNT_LINK_MISSING = "account_link_missing"


class LinkOwnerKind(StrEnum):
    TEAM = "team"  # the team's account; an administrator links it
    PERSON = "person"  # a person's own account; used when agents work for them


class AccountLinkStatus(StrEnum):
    ACTIVE = "active"
    REVOKED = "revoked"


class AccountLinkV1(AccessContractModel):
    """The stored credential a tool uses for a team or a person, and where it goes.

    Links grant nothing: access to the tool comes from grants. The broker uses
    the person's own link first and the team's link otherwise, and sends the
    credential only to the hosts of the tool's definition.
    """

    schema_version: Literal[1] = 1
    link_id: str = Field(min_length=1, max_length=120)
    team_id: str = Field(min_length=1, max_length=120)
    tool_id: str
    owner_kind: LinkOwnerKind
    owner_id: str = Field(min_length=1, max_length=240)
    credential_key: str
    injections: tuple[CredentialInjectionV1, ...] = ()
    created_by: SubjectRefV1
    created_at_utc: datetime
    status: AccountLinkStatus = AccountLinkStatus.ACTIVE
    revoked_at_utc: datetime | None = None
    revoked_by: SubjectRefV1 | None = None

    @field_validator("tool_id")
    @classmethod
    def _valid_tool_id(cls, value: str) -> str:
        if not TOOL_ID_PATTERN.fullmatch(value):
            raise ValueError(f"Invalid tool id: {value!r}")
        return value

    @field_validator("credential_key")
    @classmethod
    def _valid_key(cls, value: str) -> str:
        if not CREDENTIAL_KEY_PATTERN.fullmatch(value):
            raise ValueError(f"Invalid credential key: {value!r}")
        return value

    @model_validator(mode="after")
    def _consistent(self) -> "AccountLinkV1":
        if self.owner_kind is LinkOwnerKind.TEAM and self.owner_id != self.team_id:
            raise ValueError("A team account link belongs to its own team.")
        names = [(item.placement, item.name.lower()) for item in self.injections]
        if len(names) != len(set(names)):
            raise ValueError("An account link injects into the same place twice.")
        return self


# The sharing subject for the team's model provider keys (``AccountSharingV1``).
MODEL_KEYS_SHARING_SUBJECT = "model_keys"


class AccountSharingV1(AccessContractModel):
    """An administrator's choice: whether the team's account for one service serves everyone's work.

    ``subject`` is a tool id whose tool takes a person's and the team's
    account (``ToolDefinitionV1.sharing_choice``), or
    ``MODEL_KEYS_SHARING_SUBJECT``: whether people added from now on may use
    the team's model keys. Without a record the tool's ``default_sharing``
    (shared when it names none) and, for model keys, shared apply
    (``access_control.account_sharing``). A choice picks accounts; it never
    grants access.
    """

    schema_version: Literal[1] = 1
    team_id: str = Field(min_length=1, max_length=120)
    subject: str = Field(min_length=1, max_length=200)
    sharing: AccountSharing
    changed_by_person_id: str | None = Field(default=None, max_length=240)
    changed_at_utc: datetime

    @field_validator("subject")
    @classmethod
    def _valid_subject(cls, value: str) -> str:
        if value != MODEL_KEYS_SHARING_SUBJECT and not TOOL_ID_PATTERN.fullmatch(value):
            raise ValueError(f"Invalid sharing subject: {value!r}; expected a tool id or {MODEL_KEYS_SHARING_SUBJECT!r}.")
        return value


# ---------------------------------------------------------------------------
# Access requests: an agent asks for access it lacks
# ---------------------------------------------------------------------------


class AccessRequestStatus(StrEnum):
    WAITING = "waiting"  # an Inbox review card waits for a person
    GRANTED = "granted"
    DENIED = "denied"
    WITHDRAWN = "withdrawn"  # the waiting task ended first


class AccessReviewChoice(StrEnum):
    """What the reviewing person decides (access plan section 9.1)."""

    APPROVE = "approve"
    APPROVE_READ = "approve_read"  # read only, whatever was asked
    APPROVE_TASK = "approve_task"  # only for the waiting task
    # A change or proposal approved as a whole, every keepable row kept as
    # proposed (the chat button; the web card sends approve with its checkboxes).
    APPROVE_ALL = "approve_all"
    DENY = "deny"


class AccessRequestKind(StrEnum):
    ACCESS = "access"  # access to a tool, operation or tool set
    CHANGE = "change"  # one exact staged change, applied by trusted code after approval


class ChangeApprovalMode(StrEnum):
    # A person approved the exact change: in the Inbox (with the request id),
    # in the gateway's own controls, or with `access approve-existing`.
    PERSON_APPROVAL = "person_approval"
    HELD_ACCESS = "held_access"  # the agent and its person already hold the change's level
    AGREEMENT = "agreement"  # authorization is off; the person agreed in conversation
    # The operator's configuration file named it (an MCP server listed there
    # when the MCP registry was first created); approved_by names that file.
    OPERATOR_CONFIGURATION = "operator_configuration"


class ChangeApprovalRecordV1(AccessContractModel):
    """How a staged change was approved, kept with what it created.

    Saved in approved configuration (team roster attachments, MCP connection
    approvals, generated specialists) and in receipts. ``access_request_id``
    names the Inbox request a person approved; it is empty when a person
    re-approved existing configuration from the command line.
    """

    mode: ChangeApprovalMode
    approved_by: str = Field(min_length=1, max_length=240)
    approved_at_utc: datetime
    access_request_id: str | None = Field(default=None, max_length=120)

    @model_validator(mode="after")
    def _request_only_for_person_approval(self) -> "ChangeApprovalRecordV1":
        if self.access_request_id and self.mode is not ChangeApprovalMode.PERSON_APPROVAL:
            raise ValueError("Only a person's approval names an access request.")
        return self


# A change's one-line summary (ChangeProposalV1.summary): short enough for a
# chat message and a card heading.
CHANGE_SUMMARY_MAX_CHARS = 300
CHANGE_EFFECT_MAX_CHARS = 1000


def summary_line(text: str) -> str:
    """One line from trusted words: whitespace collapsed, cut to the summary limit with an ellipsis."""

    line = " ".join(str(text or "").split())
    if len(line) <= CHANGE_SUMMARY_MAX_CHARS:
        return line
    return line[: CHANGE_SUMMARY_MAX_CHARS - 1].rstrip() + "…"


# Warning chips on review rows for items that broaden access (section 7.2).
REVIEW_FLAGS = frozenset(
    {
        "new_host",
        "private_address",
        "full_access",
        "can_hand_on",
        "runs_code",
        "new_folder",
        "setup_steps",
        "new_program",
        "download_host",
        "team_account",
    }
)
REVIEW_SECTIONS = ("tool", "access", "restriction", "folder", "account", "revocation", "delivery")


class ReviewItemV1(AccessContractModel):
    """One thing a person approves on a review card, in trusted code's words.

    A grant row is ``keepable``: the reviewer ticks "Keep for later tasks" to
    give it as asked (normally until removed); otherwise it lasts for the
    task only. Items that broaden access carry ``flags`` shown as warnings.
    """

    item_id: str = Field(min_length=1, max_length=80)
    section: Literal["tool", "access", "restriction", "folder", "account", "revocation", "delivery"]
    # Long enough for one proposed setup step in its exact words (tool_setup_spec_v1 limits).
    text: str = Field(min_length=1, max_length=1600)
    flags: tuple[str, ...] = ()
    keepable: bool = False

    @field_validator("flags")
    @classmethod
    def _known_flags(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        unknown = sorted(set(value) - REVIEW_FLAGS)
        if unknown:
            raise ValueError(f"Unknown review flags: {', '.join(unknown)}")
        return value


class ChangeProposalV1(AccessContractModel):
    """One exact staged change, as trusted code describes it for review.

    The service that staged the plan builds this from the stored plan; the
    reviewer sees ``effect`` and approves exactly ``plan_digest``. Applying
    re-checks the digest, so a changed plan needs a new approval.
    """

    kind: str = Field(min_length=1, max_length=80)  # a catalog change kind (changes: entries)
    change_id: str = Field(min_length=1, max_length=120)
    plan_digest: str = Field(min_length=1, max_length=128)
    title: str = Field(min_length=1, max_length=400)
    # One plain line, written by the staging service from the plan's own
    # values (never from agent text or setting descriptions), such as "Switch
    # TeamArchitect to deepseek/deepseek-v4.1-flash via openrouter. Takes
    # effect now." Chat announcements and the chat card lead with it; the
    # Inbox keeps ``effect`` as the full detail. Build it with ``summary_line``.
    summary: str = Field(min_length=1, max_length=CHANGE_SUMMARY_MAX_CHARS)
    effect: tuple[str, ...] = Field(min_length=1, max_length=40)
    # A person who owns what the change affects (such as their own delivery
    # target) may decide it besides those who could give its level.
    owner_person_id: str | None = Field(default=None, max_length=240)
    # With authorization off: whether the person must agree in conversation
    # first (some bounded settings, such as adding a registered worker type to
    # the schedule list, need no agreement).
    agreement_required: bool = True
    # Rows for the review card (proposals, deliveries); ``effect`` stays the
    # plain summary for chat and older views.
    items: tuple[ReviewItemV1, ...] = Field(default=(), max_length=80)

    @field_validator("effect")
    @classmethod
    def _bounded_lines(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        if any(not line.strip() or len(line) > CHANGE_EFFECT_MAX_CHARS for line in value):
            raise ValueError(f"Each effect line is between 1 and {CHANGE_EFFECT_MAX_CHARS} characters.")
        return value

    @field_validator("summary")
    @classmethod
    def _one_line(cls, value: str) -> str:
        if not value.strip() or "\n" in value or "\r" in value:
            raise ValueError("A change summary is one non-empty line.")
        return value


# ---------------------------------------------------------------------------
# Access change proposals (access plan section 7, Phase 6)
# ---------------------------------------------------------------------------


class ProposedHttpToolV1(AccessContractModel):
    """A web API tool TeamLead proposes; trusted code builds the catalog entry from it.

    Levels: every request method has its default level (GET read, POST write,
    DELETE full, ...) unless a named route says otherwise. The tool belongs to
    the team; tool sets are never proposed here.
    """

    tool_id: str = Field(pattern=r"^http:[a-z0-9][a-z0-9_.-]{0,95}$")
    display_name: str = Field(min_length=1, max_length=120)
    description: str = Field(default="", max_length=2000)
    hosts: tuple[str, ...] = Field(min_length=1, max_length=8)
    routes: tuple[HttpRouteV1, ...] = Field(default=(), max_length=60)
    uses_credential: bool = True
    credential_injections: tuple[CredentialInjectionV1, ...] = Field(default=(), max_length=4)
    # Steps outside Universal Agents (such as creating a token on the service's
    # site) and tools to set up first; shown on the review card as proposed.
    setup: ToolSetupSpecV1 | None = None

    @model_validator(mode="after")
    def _setup_needs_an_account(self) -> "ProposedHttpToolV1":
        if self.setup is not None and self.setup.external_steps and not self.uses_credential:
            raise ValueError(f"Only a tool that uses a credential has setup steps: {self.tool_id!r}.")
        return self


class ProposedCliToolV1(AccessContractModel):
    """A CLI TeamLead proposes; trusted code builds the catalog entry and installs the program.

    ``link_owners`` is whose account it uses: ``("person",)`` each person's own
    only, ``("person", "team")`` the person's own and else the team's, or
    ``("team",)`` the team's only. ``credential_injections`` say how the
    credential reaches the program: an environment variable (``env``) or a
    file in its private home folder (``file``), never its command line.
    """

    tool_id: str = Field(pattern=r"^cli:[a-z0-9][a-z0-9_-]{0,39}$")
    display_name: str = Field(min_length=1, max_length=120)
    description: str = Field(default="", max_length=2000)
    installed_cli: InstalledCliV1
    credential_injections: tuple[CredentialInjectionV1, ...] = Field(default=(), max_length=4)
    link_owners: tuple[Literal["person", "team"], ...] = ("person",)
    setup: ToolSetupSpecV1 | None = None

    @model_validator(mode="after")
    def _consistent(self) -> "ProposedCliToolV1":
        if self.tool_id != f"cli:{self.installed_cli.name}":
            raise ValueError("The tool id is cli:<the program's folder name under team/tools>.")
        if not CLI_NAME_PATTERN.fullmatch(self.installed_cli.name):
            raise ValueError("The CLI name is a plain lowercase name.")
        for injection in self.credential_injections:
            if injection.placement not in {InjectionPlacement.ENV, InjectionPlacement.FILE}:
                raise ValueError("A CLI gets its credential in an environment variable or a file, never in argv.")
        if self.setup is not None and self.setup.external_steps and not self.credential_injections:
            raise ValueError("Only a CLI that uses a credential has setup steps.")
        return self


class ProposedMCPServerV1(AccessContractModel):
    """A pending MCP connection to install (access plan 7d).

    Names the inert record ``mcp_connections(action="propose")`` (or an operator's
    configuration, a hosted server or a managed service) left in the MCP
    registry, bound by its record digest: approving installs exactly that
    record, and a record changed since the proposal is refused. Installed, it
    is the team tool ``mcp:<name>``; the proposal's grant rows say who may use
    it.
    """

    connection_id: str = Field(min_length=1, max_length=240)
    record_digest: str = Field(pattern=r"^[0-9a-f]{64}$")


class ProposedGrantV1(AccessContractModel):
    """Access for an agent type, for the work of the person the proposal's task works for (D13)."""

    agent_type: str = Field(min_length=1, max_length=120)
    target: TargetRefV1
    level: AccessLevel
    # until_revoked: the reviewer may keep it for later tasks (a checkbox);
    # task: for the proposal's task only, whatever the reviewer ticks.
    lifetime: Literal["until_revoked", "task"] = "until_revoked"
    delegation_depth: int = Field(default=0, ge=0, le=1)
    reason: str = Field(default="", max_length=1000)


class ProposedBlockV1(AccessContractModel):
    """A restriction: stop a worker type using a tool, operation or folder."""

    agent_type: str = Field(min_length=1, max_length=120)
    target: TargetRefV1
    reason: str = Field(min_length=1, max_length=1000)


class ProposedAccountLinkV1(AccessContractModel):
    """Which credential a tool uses is chosen by a person in the broker's form after approval."""

    tool_id: str
    owner: Literal["team", "person"] = "team"
    purpose: str = Field(min_length=1, max_length=500)


class ProposedDataFolderV1(AccessContractModel):
    """A host folder to register as ``data/<name>`` (decision D16)."""

    name: str = Field(pattern=DATA_FOLDER_NAME_PATTERN)
    path: str = Field(min_length=2, max_length=1000)
    writable: bool = False


class AccessProposalV1(AccessContractModel):
    """Several access changes reviewed once and applied together (section 7).

    TeamLead proposes; trusted code validates it, describes the effect in its
    own words, and a person approves or denies it as a whole. Nothing here is
    code or policy text: every item is a typed record.
    """

    schema_version: Literal[1] = 1
    title: str = Field(min_length=1, max_length=300)
    reason: str = Field(min_length=1, max_length=2000)
    tools: tuple[ProposedHttpToolV1, ...] = Field(default=(), max_length=4)
    clis: tuple[ProposedCliToolV1, ...] = Field(default=(), max_length=2)
    mcp_servers: tuple[ProposedMCPServerV1, ...] = Field(default=(), max_length=4)
    grants: tuple[ProposedGrantV1, ...] = Field(default=(), max_length=20)
    blocks: tuple[ProposedBlockV1, ...] = Field(default=(), max_length=20)
    account_links: tuple[ProposedAccountLinkV1, ...] = Field(default=(), max_length=4)
    data_folders: tuple[ProposedDataFolderV1, ...] = Field(default=(), max_length=4)
    revoke_grant_ids: tuple[str, ...] = Field(default=(), max_length=20)

    @model_validator(mode="after")
    def _not_empty(self) -> "AccessProposalV1":
        items = (
            self.tools,
            self.clis,
            self.mcp_servers,
            self.grants,
            self.blocks,
            self.account_links,
            self.data_folders,
            self.revoke_grant_ids,
        )
        if not any(items):
            raise ValueError("A proposal needs at least one item.")
        return self

    @property
    def installs(self) -> bool:
        """Whether it adds to the team's catalog or data folders (an administrator's decision)."""

        return bool(self.tools or self.clis or self.mcp_servers or self.data_folders)


class AccessRequestV1(AccessContractModel):
    """One request for access or a change, from an agent or a person.

    An agent asks with ``request_access`` (or stages a change): a worker's
    request is handed down at once when the team lead holds an automatic,
    delegable grant covering it; otherwise it waits for a person who can give
    the access, reviewed in the Inbox, and the waiting task resumes with the
    decision. A person asks for a change that someone else decides (sending a
    file into another person's folder): ``requested_by_person_id`` is set,
    no agent waits, and the gateway applies it when approved.
    """

    schema_version: Literal[1] = 1
    request_id: str = Field(min_length=1, max_length=120)
    team_id: str = Field(min_length=1, max_length=120)
    kind: AccessRequestKind = AccessRequestKind.ACCESS
    change: ChangeProposalV1 | None = None
    requester_agent_id: str | None = Field(default=None, min_length=1, max_length=240)
    requester_agent_type: str | None = Field(default=None, min_length=1, max_length=120)
    requested_by_person_id: str | None = Field(default=None, min_length=1, max_length=240)
    person_id: str | None = Field(default=None, max_length=240)
    subject: SubjectRefV1
    target: TargetRefV1
    level: AccessLevel
    lifetime: GrantLifetime = GrantLifetime.UNTIL_REVOKED
    expires_at_utc: datetime | None = None
    reason: str = Field(min_length=1, max_length=2000)
    task_id: str | None = Field(default=None, max_length=240)
    # The request the asking task works on (its verified signed context id,
    # shared by every task of that request): access approved for the task
    # only is bound to it (GrantV1.task_id), so every agent helping with the
    # request can use it. task_id above is the asking agent's own task, which
    # pauses and resumes around the request.
    work_id: str | None = Field(default=None, max_length=240)
    # The scheduled job whose run asked (from the run's verified identity):
    # access approved for it is kept for that schedule's runs only.
    schedule_id: str | None = Field(default=None, max_length=240)
    conversation_id: str | None = Field(default=None, max_length=240)
    # The tool call that asked; a resumed call finds its request by it.
    tool_use_id: str | None = Field(default=None, max_length=240)
    status: AccessRequestStatus = AccessRequestStatus.WAITING
    person_lacks_access: bool = False  # the person must be given it too (dual check)
    # The team lead asks for access it may hand on to its workers' tasks
    # without asking (set_up_tools for work a worker does; request_access
    # hand_on=True). The review card's "may hand it on" box is ticked from the start.
    hand_on: bool = False
    grant_ids: tuple[str, ...] = ()
    decided_by: SubjectRefV1 | None = None
    decision: AccessReviewChoice | None = None
    decision_note: str = Field(default="", max_length=2000)
    created_at_utc: datetime
    decided_at_utc: datetime | None = None
    # For changes: when trusted code applied the approved change (once), and why it failed.
    applied_at_utc: datetime | None = None
    apply_error: str = Field(default="", max_length=2000)
    # The review rows the reviewer chose to keep for later tasks, and for how
    # many hours (None: until removed).
    kept_item_ids: tuple[str, ...] = ()
    keep_hours: int | None = Field(default=None, ge=1)

    @model_validator(mode="after")
    def _one_requester(self) -> "AccessRequestV1":
        by_agent = self.requester_agent_id is not None and self.requester_agent_type is not None
        if by_agent == (self.requested_by_person_id is not None):
            raise ValueError("A request comes from one agent (id and type) or from one person.")
        return self

    @property
    def requester_name(self) -> str:
        """The asking agent's type, or the asking person's id."""

        return self.requester_agent_type or self.requested_by_person_id or ""

    @model_validator(mode="after")
    def _change_has_proposal(self) -> "AccessRequestV1":
        if (self.kind is AccessRequestKind.CHANGE) != (self.change is not None):
            raise ValueError("A change request carries its proposal, and only a change request does.")
        return self

    @model_validator(mode="after")
    def _until_has_expiry(self) -> "AccessRequestV1":
        # A new request for one task must name the request it works on
        # (work_id); AccessRequests._check_request refuses one without it.
        # That is not checked here, because this model also reads stored
        # requests, and those saved before access for one task covered the
        # whole request (v0.6.0) carry only the agent's task id. They still
        # show on chat cards and can be denied, never approved
        # (AccessRequests.decide, before_work_ids).
        if (self.lifetime is GrantLifetime.UNTIL) != (self.expires_at_utc is not None):
            raise ValueError("Access until a time needs expires_at_utc, and only it has one.")
        return self


class AccessRequestResolvedV1(BaseModel):
    """Tells the requesting agent that its waiting request was decided.

    Carries only identifiers: the agent reads the decision from the access
    store, so the message grants nothing and a forged one changes nothing.
    """

    model_config = ConfigDict(extra="forbid", frozen=True, str_strip_whitespace=True)

    schema_version: Literal[1] = 1
    message_class: Literal["AccessRequestResolvedV1"] = "AccessRequestResolvedV1"
    message_id: str = Field(min_length=1, max_length=240)
    team_id: str = Field(min_length=1, max_length=120)
    request_id: str = Field(min_length=1, max_length=120)

    def get_id(self) -> str:
        return self.message_id


class AccessNoticeKind(StrEnum):
    HAND_DOWN = "hand_down"  # an agent gave access without asking anyone
    TEAM_SKILL_CHANGED = "team_skill_changed"
    RELEASE_CHANGED = "release_changed"  # a release changed a scheduled worker
    SHARED = "shared"  # a person shared a file or folder with you (D15)
    FILE_RECEIVED = "file_received"  # a file sent to you arrived in received/ (D15)
    ACCOUNT_LINK_NEEDED = "account_link_needed"  # choose the credential for a tool you approved
    MCP_SERVER_UNAVAILABLE = "mcp_server_unavailable"  # agents stopped retrying an MCP server that does not answer
    SCHEDULES_TO_REVIEW = "schedules_to_review"  # schedules that look stale, or team schedules that moved because their owner left
    ONBOARDING_KEYS_LINKED = "onboarding_keys_linked"  # keys typed during onboarding became the team's linked accounts
    MODEL_CHANGED = "model_changed"  # TeamLead switched an agent type's model with model-choice access
    TOOL_SETUP_NEEDED = "tool_setup_needed"  # a tool that worked for you needs setting up again
    MODEL_KEY_PROBLEM = "model_key_problem"  # a model provider rejected a key, or an endpoint has none
    STATUS_PROBLEMS = "status_problems"  # new problems on administrators' Status page
    WORK_STUCK = "work_stuck"  # a schedule or task is stuck, or working again (custom_agents.stuck_work)
    WORKER_POOL_CHANGED = "worker_pool_changed"  # TeamArchitect changed a worker pool size (custom_tools.providers.worker_pools)
    CREDENTIALS_IMPORTED = "credentials_imported"  # your sign-ins and keys moved here with a team transfer (team_transfer)
    MESSAGES_SET_ASIDE = "messages_set_aside"  # messages no receiver could read or handle were set aside (custom_agents.dead_letter_watch)
    ACCOUNT_SHARING_CHANGED = "account_sharing_changed"  # an administrator shared a service's team account or made it private
    TOOL_LIMIT_REACHED = "tool_limit_reached"  # an agent's always-shown tools are more than its model takes in one request (custom_agents.tool_loading)
    BLOCK_WIDENED = "block_widened"  # a block named an operation an update removed, so it blocks the whole tool (access_control.stale_blocks)
    KNOWLEDGE_NEEDS_ATTENTION = "knowledge_needs_attention"  # pages frozen or agents refused many knowledge writes (custom_agents.knowledge_curation_runtime)
    SAVED_OUTPUT_RETENTION = "saved_output_retention"  # saved tool outputs will start being deleted after the grace period (custom_agents.output_retention)


class AccessNoticeV1(AccessContractModel):
    """Something people may want to check; informs, decides nothing.

    Notices go to one person (the person at the root of a hand-down's chain,
    or a schedule's owner) or to people holding a team role (administrators,
    for team skill changes). They have no deadline and never wake a task.
    """

    schema_version: Literal[1] = 1
    notice_id: str = Field(min_length=1, max_length=120)
    team_id: str = Field(min_length=1, max_length=120)
    kind: AccessNoticeKind
    recipient_person_id: str | None = Field(default=None, max_length=240)
    recipient_roles: tuple[str, ...] = ()
    title: str = Field(min_length=1, max_length=400)
    summary: str = Field(min_length=1, max_length=4000)
    grant_id: str | None = Field(default=None, max_length=120)  # offered for one-click revoke
    created_at_utc: datetime

    @model_validator(mode="after")
    def _has_recipient(self) -> "AccessNoticeV1":
        if not self.recipient_person_id and not self.recipient_roles:
            raise ValueError("A notice needs a person or a team role to go to.")
        return self


class ToolSetupState(StrEnum):
    """Whether a tool that acts with an account is set up for one person (last known)."""

    READY = "ready"
    NEEDS_SETUP = "needs_setup"
    UNKNOWN = "unknown"


class ToolSetupStateV1(AccessContractModel):
    """Trusted code's last known setup state of one tool for one person.

    Written only by trusted code (``access_control.tool_setup_states``): the
    broker after each call of an account tool and each setup check, the
    gateway when a setup page or the person's own ``/link`` finishes, and the
    broker's periodic re-check. TeamLead reads it as a fact and skips the
    setup check for a ``ready`` tool. It covers the person's account and the
    person's own access, never an agent's access. ``how`` says how a ready
    tool works (``signed_in``, ``own_link``, ``team_link``); ``missing_step``
    and ``fixed_by`` say what a tool that needs setup lacks and who can do it.
    """

    schema_version: Literal[1] = 1
    team_id: str = Field(min_length=1, max_length=120)
    person_id: str = Field(min_length=1, max_length=240)
    tool_id: str = Field(min_length=1, max_length=200)
    state: ToolSetupState
    how: str | None = Field(default=None, max_length=40)
    account_label: str | None = Field(default=None, max_length=200)
    missing_step: str | None = Field(default=None, max_length=40)
    fixed_by: str | None = Field(default=None, max_length=40)
    source: str = Field(min_length=1, max_length=40)
    last_worked_at_utc: datetime | None = None
    checked_at_utc: datetime
    changed_at_utc: datetime


class PersonStanding(StrEnum):
    """Where a person stands in a team (the gateway's people records decide it)."""

    ACTIVE = "active"
    DISABLED = "disabled"  # reversible; their work stops and their schedules are deleted
    REMOVED = "removed"  # taken off the team; their work and personal records are deleted or held


class TeamPersonV1(AccessContractModel):
    """One person's current standing and roles in a team, as the gateway last recorded them.

    The gateway owns people (``access_control.people``) and keeps this copy in
    the access store whenever a person is added, changes role, is disabled,
    enabled or removed, and in full at each gateway start
    (``assistant_gateway.people_directory_sync``). Trusted runtime code that
    cannot read the gateway's own records, such as the manager's scheduler,
    reads it through ``access_control.people_directory``. ``roles`` are the
    effective roles (an owner is also an administrator) and are empty unless
    the person is active.
    """

    schema_version: Literal[1] = 1
    team_id: str = Field(min_length=1, max_length=120)
    person_id: str = Field(min_length=1, max_length=240)
    standing: PersonStanding
    roles: tuple[str, ...] = ()
    changed_at_utc: datetime

    @model_validator(mode="after")
    def _roles_only_when_active(self) -> "TeamPersonV1":
        if self.roles and self.standing is not PersonStanding.ACTIVE:
            raise ValueError("Only an active person holds roles.")
        return self


class AccountLinkRequestV1(AccessContractModel):
    """The gateway asks the broker to open the account link form for a person.

    The gateway vouches for the person and their current team roles (it holds
    the broker's gateway caller token); linking the team's account needs the
    admin or owner role.
    """

    request_id: str = Field(min_length=1, max_length=160)
    team_id: str = Field(min_length=1, max_length=120)
    tool_id: str
    owner_kind: LinkOwnerKind
    person_id: str = Field(min_length=1, max_length=240)
    person_roles: tuple[str, ...] = ()
    reason: str = Field(default="", max_length=2000)

    @field_validator("tool_id")
    @classmethod
    def _valid_tool_id(cls, value: str) -> str:
        if not TOOL_ID_PATTERN.fullmatch(value):
            raise ValueError(f"Invalid tool id: {value!r}")
        return value


class PersonCredentialDeletionV1(AccessContractModel):
    """The gateway asks the broker to delete a removed person's stored credentials.

    Sent after an administrator removes the person (``/user remove``,
    ``assistant_gateway.person_departure``). ``credential_keys`` are the keys of
    the person's account links; the broker also deletes every other credential
    stored in the person's own scope.
    """

    team_id: str = Field(min_length=1, max_length=120)
    person_id: str = Field(min_length=1, max_length=240)
    credential_keys: tuple[str, ...] = ()

    @field_validator("credential_keys")
    @classmethod
    def _valid_keys(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        for key in value:
            if not CREDENTIAL_KEY_PATTERN.fullmatch(key):
                raise ValueError(f"Invalid credential key: {key!r}")
        return value


class SignInService(StrEnum):
    """A service whose per-person credentials the broker keeps; they are that person's alone.

    GitHub, Google and X are sign-ins (``/link``). ``MODEL_KEYS`` are a
    person's own model provider keys (``/auth model``). A removed person's
    entries are deleted and named by these labels
    (``secure_tool_broker.departed_person_credentials``).
    """

    GITHUB = "github"
    GOOGLE = "google"
    X = "x"
    MODEL_KEYS = "model_keys"

    @property
    def label(self) -> str:
        return _SIGN_IN_SERVICE_LABELS[self]


_SIGN_IN_SERVICE_LABELS = {
    SignInService.GITHUB: "GitHub",
    SignInService.GOOGLE: "Google",
    SignInService.X: "X",
    SignInService.MODEL_KEYS: "model provider keys",
}


class CredentialsFolderOutcome(StrEnum):
    """What happened to a removed person's private credentials folder in the broker."""

    DELETED = "deleted"
    ABSENT = "absent"  # they had none
    KEPT = "kept"  # a sign-in could not be deleted; the folder may hold what a retry needs
    FAILED = "failed"


class PersonCredentialDeletionResultV1(AccessContractModel):
    """What the broker deleted for a removed person, and what it could not.

    ``deleted``/``failed`` are credential keys (account links and the person's
    own catalog credentials); ``sign_ins_deleted``/``sign_ins_failed`` name the
    services whose per-person sign-ins were found and deleted, or could not be
    deleted; ``folder`` is the fate of their private credentials folder.
    """

    deleted: tuple[str, ...] = ()
    failed: tuple[str, ...] = ()
    sign_ins_deleted: tuple[SignInService, ...] = ()
    sign_ins_failed: tuple[SignInService, ...] = ()
    folder: CredentialsFolderOutcome = CredentialsFolderOutcome.ABSENT


class OnboardingAccountKeyV1(AccessContractModel):
    """One key typed during onboarding, for a web API tool that uses a linked account.

    ``owner_kind``: the team's account, or the onboarded person's own when
    the team keeps the service private (``access_control.account_sharing``);
    the gateway decides it from the team's sharing.
    """

    tool_id: str
    owner_kind: LinkOwnerKind
    # Secret: kept out of repr and logs; sent only on the gateway's broker route.
    value: str = Field(min_length=1, max_length=4096, repr=False)

    @field_validator("tool_id")
    @classmethod
    def _valid_tool_id(cls, value: str) -> str:
        if not TOOL_ID_PATTERN.fullmatch(value) or not value.startswith("http:"):
            raise ValueError(f"Onboarding keys are for web API tools (http:*), not {value!r}.")
        return value


class OnboardingModelKeyV1(AccessContractModel):
    """A model key typed and tested during onboarding, for the team's endpoint at ``address``.

    Stored as the team's model key (``secure_tool_broker.model_keys``) instead
    of ``team.env``, where it would override every key saved later.
    """

    address: str = Field(min_length=1, max_length=520)
    kind: EndpointKind
    model_id: str = Field(default="", max_length=512)
    # Secret: kept out of repr and logs; sent only on the gateway's broker route.
    value: str = Field(min_length=1, max_length=8192, repr=False)

    @field_validator("address")
    @classmethod
    def _address(cls, value: str) -> str:
        return endpoint_address(value)


class OnboardingAccountKeysV1(AccessContractModel):
    """The gateway hands the broker the keys typed during onboarding, as linked accounts.

    Onboarding cannot reach the broker (it is not running yet), so it leaves
    the keys in a private hand-over file (``assistant_gateway.onboarding_account_keys``);
    at start the gateway sends them here, for the person who onboarded, with
    that person's current team roles. The broker links each one for its
    ``owner_kind``: for the team only when those roles allow team links (an
    administrator) and the team shares the service, otherwise as the person's
    own, with the same checks as the account link form
    (``secure_tool_broker.account_links``). Model keys are always the team's.
    """

    team_id: str = Field(min_length=1, max_length=120)
    person_id: str = Field(min_length=1, max_length=240)
    person_roles: tuple[str, ...] = ()
    keys: tuple[OnboardingAccountKeyV1, ...] = Field(default=(), max_length=8)
    model_keys: tuple[OnboardingModelKeyV1, ...] = Field(default=(), max_length=8)

    @model_validator(mode="after")
    def _something_to_hand_over(self) -> "OnboardingAccountKeysV1":
        if not self.keys and not self.model_keys:
            raise ValueError("An onboarding hand-over holds at least one key.")
        return self


class OnboardingAccountKeyFailureV1(AccessContractModel):
    tool_id: str
    reason_code: str = Field(min_length=1, max_length=120)
    message: str = Field(default="", max_length=1000)


class OnboardingAccountKeysResultV1(AccessContractModel):
    """Which tools the broker linked (for the owner each key named), and which it could not (never a value)."""

    linked: tuple[str, ...] = ()
    failed: tuple[OnboardingAccountKeyFailureV1, ...] = ()
    # Model keys by endpoint address; a failure's ``tool_id`` is the address.
    model_keys_saved: tuple[str, ...] = ()
    model_keys_failed: tuple[OnboardingAccountKeyFailureV1, ...] = ()


__all__ = [
    "ACCESS_CONTRACT_SCHEMA_VERSION",
    "AccessNoticeKind",
    "AccessRequestKind",
    "AccessNoticeV1",
    "AccessRequestResolvedV1",
    "AccessRequestStatus",
    "OnboardingAccountKeyFailureV1",
    "OnboardingAccountKeyV1",
    "OnboardingAccountKeysResultV1",
    "OnboardingAccountKeysV1",
    "OnboardingModelKeyV1",
    "PersonCredentialDeletionResultV1",
    "PersonCredentialDeletionV1",
    "AccessRequestV1",
    "AccessReviewChoice",
    "REASON_ACCOUNT_LINK_MISSING",
    "AccountLinkRequestV1",
    "ChangeApprovalMode",
    "ChangeApprovalRecordV1",
    "CHANGE_SUMMARY_MAX_CHARS",
    "CHANGE_EFFECT_MAX_CHARS",
    "ChangeProposalV1",
    "AccountLinkStatus",
    "AccountLinkV1",
    "AccountSharing",
    "AccountSharingV1",
    "MODEL_KEYS_SHARING_SUBJECT",
    "CredentialInjectionV1",
    "CredentialsFolderOutcome",
    "InjectionPlacement",
    "LinkOwnerKind",
    "AccessDecisionV1",
    "AccessEventV1",
    "DelegationMode",
    "GrantKind",
    "GrantLifetime",
    "GrantStatus",
    "GrantV1",
    "LIFETIME_RANK",
    "SignInService",
    "SignedTaskContextV1",
    "SCHEDULE_RUN_ID_PREFIX",
    "SchedulePermitV1",
    "ScheduleRunIdentityV1",
    "SignedSchedulePermitV1",
    "PersonStanding",
    "SubjectKind",
    "SubjectRefV1",
    "TeamPersonV1",
    "ToolSetupState",
    "ToolSetupStateV1",
    "TargetKind",
    "TargetRefV1",
    "TaskContextV1",
    "AccessContractModel",
    "AccessLevel",
    "AccountMode",
    "ClassificationSource",
    "HTTP_METHODS",
    "HttpRouteV1",
    "OperationDefinitionV1",
    "OperationResolutionV1",
    "OwnerScope",
    "ToolDefinitionV1",
    "ToolKind",
    "ToolSetDefinitionV1",
    "contract_digest",
    "summary_line",
]
