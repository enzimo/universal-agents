"""The single definition of where an instance keeps its files.

Everything an instance stores lives under one instance root (``/data`` in
Docker). The root is split by who may touch each area:

``control/``
    Files that control the team: TeamLead's ``SOUL.md``, model overrides, the
    startup roster, team templates, agent definitions, and team skills. Only
    trusted runtime services write here, for example the settings, provisioning
    and skill-installation services TeamLead uses. Agent tools and agent-run code
    can never open it.
``dbs/``
    Databases (task history, gateway state, activity collector) and the
    conversation artifacts stored beside the task database.
``state/``
    File-based runtime records: offloaded tool results, chat uploads, the
    records of published pages (which folder, whose, and the SHA-256 of each
    file), model failover state, diagnostics, execution artifacts, and the
    folders of people removed from the team, held until they are restored or
    purged (``held_people/``, ``access_control.held_person_folders``).
    Trusted maintenance deletes old offloaded tool results here, and old
    conversation artifacts in ``dbs/`` (``custom_agents.output_retention``).
``workspaces/``
    The only area agents work in:

    - ``users/<person>/`` is private to one person. An agent working for that
      person uses it as its default folder; it holds that person's research
      files, Python environments, uploads copied in for processing, published
      pages (``sites/``) and temporary files (``scratch/``, including the
      gateway's copies of files sent to the person in ``scratch/sent/``).
      Trusted maintenance deletes old files from ``scratch/`` and old result
      folders from ``research/``.
    - ``team/`` is shared by everyone on the team, people and agents.
    - ``agents/`` is shared by the team's agents and is the working folder of
      tasks that run for no person (system maintenance). Execution control's
      run and build folders are in ``agents/execution``; trusted maintenance
      deletes old ones.

Operator files (``instance_config.yaml``, ``team.env``), broker credentials,
``auth/`` and ``runtime/`` stay directly under the root, outside every area an
agent can reach:

``auth/``
    Model sign-in token stores (OpenAI Codex and xAI), written by onboarding
    and the runtime when it refreshes a token.
``runtime/``
    Records trusted services keep while the team runs: installed MCP
    connections (``mcp-connections/``), hosted MCP server definitions and their
    state (``mcp-hosting/``), installed permission definitions
    (``installed-permissions/``) and reviewed skill installs
    (``skill-installation/``).

Tools the team adds (web API tools and installed command-line tools) are
control files: ``control/access/tools``.

Two skill folders are the one exception to "control and runtime files are
closed": ``control/skills`` (operator-added and external-repository skills) and
``runtime/skill-installation/installed`` (reviewed installs). Agents read a
skill's reference files and run its bundled scripts, so file tools and
sandboxed code may read both folders. Nothing an agent runs may write them;
changes go through reviewed installation (``plan_team_skill_change``).

The layout is derived, not configured: only the root is a setting. This keeps
one answer to "where is X" for every component, and a folder that agents can
reach can never be configured to overlap control or state files.
"""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass
from pathlib import Path

CONTROL_DIRNAME = "control"
STATE_DIRNAME = "state"
DBS_DIRNAME = "dbs"
WORKSPACES_DIRNAME = "workspaces"
USERS_DIRNAME = "users"
TEAM_DIRNAME = "team"
AGENTS_DIRNAME = "agents"
# Everyone's shared working folder, team/team-shared: every person's agents,
# administrators' agents and the team's agents read and write it (like all of
# team/, access_control.folders.STANDARD_SHARED_FOLDERS). Owner's decision 2026-10-02.
TEAM_SHARED_DIRNAME = "team-shared"

SOUL_FILENAME = "SOUL.md"
LLM_OVERRIDES_FILENAME = "LLM_OVERRIDES.yaml"
TEAM_COMPOSITION_FILENAME = "TEAM_AGENTS.yaml"
TEAM_TEMPLATES_FILENAME = "TEAM_TEMPLATES.yaml"
AGENT_DEFINITIONS_DIRNAME = "agent_definitions"
AGENT_BUNDLES_DIRNAME = "agent_bundles"
GENERATED_AGENT_BUNDLES_DIRNAME = "team_lead_generated"
SKILLS_DIRNAME = "skills"
OBJECTIVE_PROFILES_DIRNAME = "objective_execution_profiles"
TASK_DATABASE_FILENAME = "task_history.db"
# Long texts offloaded from conversation turns; each file belongs to a row of the
# task database (its full path is stored there), so it lives beside it in dbs/.
CONVERSATION_ARTIFACTS_DIRNAME = "conversation_artifacts"
# Operator secrets file, used when launch.secrets_source is env_file.
TEAM_ENV_FILENAME = "team.env"
# The operator's instance configuration; every settings change writes this file.
INSTANCE_CONFIG_FILENAME = "instance_config.yaml"
RUNTIME_DIRNAME = "runtime"
SKILL_INSTALLATION_DIRNAME = "skill-installation"
ENVIRONMENT_OVERRIDES_FILENAME = "environment_overrides.json"
# Locked by the supervisor and every agent process of one runtime run
# (runtime_hosting.process_run).
PROCESS_RUN_LOCK_FILENAME = "process_run.lock"
INSTALLED_SKILLS_DIRNAME = "installed"
# Team import (``team_transfer``): reports, and a marker that stays while an
# import is unfinished so a half-imported team never starts.
TEAM_IMPORT_DIRNAME = "team-import"
TEAM_IMPORT_MARKER_FILENAME = "in-progress.json"
# Onboarding's Import: team exports the operator copied in, the review
# folder of the opened export, and the credentials left for the first start.
IMPORT_DIRNAME = "import"
TEAM_IMPORT_REVIEW_DIRNAME = "review"
TEAM_IMPORT_CREDENTIALS_FILENAME = "team_import_credentials.json"
# Installed MCP connections (mcp_control.registry) and hosted MCP servers
# (mcp_control.hosting), both under runtime/.
MCP_CONNECTIONS_DIRNAME = "mcp-connections"
MCP_HOSTING_DIRNAME = "mcp-hosting"
MCP_HOSTING_DEFINITIONS_DIRNAME = "definitions"
MCP_HOSTING_STATE_DIRNAME = "state"
# Installed permission definitions (custom_agents.installed_permissions).
INSTALLED_PERMISSIONS_DIRNAME = "installed-permissions"
# Team-added tool definitions live in control/access/tools.
ACCESS_DIRNAME = "access"
INSTANCE_TOOLS_DIRNAME = "tools"
# Model sign-in token stores, directly under the root.
AUTH_DIRNAME = "auth"
OPENAI_CODEX_AUTH_FILENAME = "openai_codex_auth.json"
XAI_OAUTH_AUTH_FILENAME = "xai_oauth_auth.json"

# A removed person's folder is moved here, outside workspaces/, so no agent
# can reach it while it waits to be restored or purged.
HELD_PEOPLE_DIRNAME = "held_people"
# Keys typed during onboarding until the gateway hands them to the broker.
ONBOARDING_ACCOUNT_KEYS_FILENAME = "onboarding_account_keys.json"

PERSON_UPLOADS_DIRNAME = "uploads"
PERSON_RESEARCH_DIRNAME = "research"
PERSON_PYTHON_ENVS_DIRNAME = "python_envs"
# Temporary files of a person's tasks (agents/scratch for work for no person).
# Trusted maintenance deletes old files here (tool_policy.scratch_retention_days).
SCRATCH_DIRNAME = "scratch"
# Inside scratch/: the gateway's copies of files sent with notify_user.
SCRATCH_SENT_DIRNAME = "sent"
# Pages published for a person (publish_single_page_site), one folder per site.
PERSON_SITES_DIRNAME = "sites"
# Run and build folders of execution control, in agents/ (execution_runs_dir).
EXECUTION_DIRNAME = "execution"
# Content-addressed Rust build folders inside the execution folder.
EXECUTION_RUST_BUILDS_DIRNAME = "rust-builds"

# The one legacy location: every file used to sit in <root>/workspace.
LEGACY_WORKSPACE_DIRNAME = "workspace"

_SAFE_PERSON_FOLDER = re.compile(r"^[a-z0-9][a-z0-9_]{0,63}$")
_SLUG_UNSAFE = re.compile(r"[^a-z0-9]+")


def person_folder_name(canonical_user_id: str) -> str:
    """Folder name for one person, stable and safe on every filesystem.

    Generated ids such as ``usr_3f9a1b2c4d5e`` are used as they are. Any other
    id (the first user's id defaults to the display name, which may contain
    spaces, capitals, ``/`` or ``..``) becomes a readable slug plus 12 hex
    characters of its SHA-256. A hashed name always contains ``-``, which a
    pass-through name never does, so two people can never share a folder.
    """

    person = str(canonical_user_id or "").strip()
    if not person:
        raise ValueError("A person folder needs a canonical user id.")
    if _SAFE_PERSON_FOLDER.fullmatch(person):
        return person
    slug = _SLUG_UNSAFE.sub("-", person.lower()).strip("-")[:24].strip("-") or "user"
    digest = hashlib.sha256(person.encode("utf-8")).hexdigest()[:12]
    return f"{slug}-{digest}"


@dataclass(frozen=True)
class WorkspaceScope:
    """The folders one task may use, derived from the person it works for.

    ``home`` is the default working folder for shell, Python, and relative file
    paths. ``roots`` are the only folders the task's tools and code may read or
    write.
    """

    workspaces_dir: Path
    person_id: str | None
    home: Path
    roots: tuple[Path, ...]

    def contains(self, path: Path) -> bool:
        resolved = Path(path)
        return any(resolved == root or root in resolved.parents for root in self.roots)


@dataclass(frozen=True)
class InstanceLayout:
    """Paths of one instance, derived from its root."""

    root: Path

    @classmethod
    def at(cls, root: str | Path) -> "InstanceLayout":
        raw = str(root or "").strip()
        if not raw:
            raise ValueError("The instance root is not configured.")
        return cls(Path(raw).expanduser().resolve())

    # Operator files
    @property
    def instance_config_path(self) -> Path:
        """The saved instance configuration (``GatewayInstanceConfig.load_saved``)."""
        return self.root / INSTANCE_CONFIG_FILENAME

    @property
    def team_env_path(self) -> Path:
        return self.root / TEAM_ENV_FILENAME

    @property
    def import_dir(self) -> Path:
        """Team exports waiting for onboarding's Import.

        The operator copies an export (``assistant_cli team export``) here
        before onboarding a fresh instance; onboarding lists the files and
        deletes the one it imported (``assistant_gateway.onboarding_web.team_import``).
        Outside ``workspaces/``, so no agent can reach it, and not one of the
        folders a first import requires to be empty."""

        return self.root / IMPORT_DIRNAME

    # Areas
    @property
    def control_dir(self) -> Path:
        return self.root / CONTROL_DIRNAME

    @property
    def state_dir(self) -> Path:
        return self.root / STATE_DIRNAME

    @property
    def dbs_dir(self) -> Path:
        return self.root / DBS_DIRNAME

    @property
    def workspaces_dir(self) -> Path:
        return self.root / WORKSPACES_DIRNAME

    @property
    def users_dir(self) -> Path:
        return self.workspaces_dir / USERS_DIRNAME

    @property
    def team_dir(self) -> Path:
        return self.workspaces_dir / TEAM_DIRNAME

    @property
    def team_shared_dir(self) -> Path:
        return self.team_dir / TEAM_SHARED_DIRNAME

    @property
    def agents_dir(self) -> Path:
        return self.workspaces_dir / AGENTS_DIRNAME

    # Control files
    @property
    def soul_path(self) -> Path:
        return self.control_dir / SOUL_FILENAME

    @property
    def llm_overrides_path(self) -> Path:
        return self.control_dir / LLM_OVERRIDES_FILENAME

    @property
    def team_composition_path(self) -> Path:
        return self.control_dir / TEAM_COMPOSITION_FILENAME

    @property
    def team_templates_path(self) -> Path:
        return self.control_dir / TEAM_TEMPLATES_FILENAME

    @property
    def agent_definitions_dir(self) -> Path:
        return self.control_dir / AGENT_DEFINITIONS_DIRNAME

    @property
    def agent_bundles_dir(self) -> Path:
        return self.control_dir / AGENT_BUNDLES_DIRNAME

    @property
    def generated_agent_bundles_dir(self) -> Path:
        return self.agent_bundles_dir / GENERATED_AGENT_BUNDLES_DIRNAME

    @property
    def skills_dir(self) -> Path:
        return self.control_dir / SKILLS_DIRNAME

    @property
    def objective_profiles_dir(self) -> Path:
        return self.control_dir / OBJECTIVE_PROFILES_DIRNAME

    @property
    def instance_tools_dir(self) -> Path:
        """Tools the team added: web API tools and installed command-line tools.

        One YAML file per tool, written only by trusted code when an
        administrator approves an access proposal or installs a CLI
        (``access_catalog.instance_tools``); the tool catalog loads them beside
        the built-in definitions (``access_catalog.catalog.load_catalog``).
        """

        return self.control_dir / ACCESS_DIRNAME / INSTANCE_TOOLS_DIRNAME

    # Operator-side files outside control/
    @property
    def auth_dir(self) -> Path:
        """Model sign-in token stores, outside every area an agent can reach."""

        return self.root / AUTH_DIRNAME

    @property
    def openai_codex_auth_path(self) -> Path:
        """OpenAI Codex sign-in tokens, written by onboarding and refreshed by the runtime
        (``custom_agents.openai_codex_auth``)."""

        return self.auth_dir / OPENAI_CODEX_AUTH_FILENAME

    @property
    def xai_oauth_auth_path(self) -> Path:
        """xAI sign-in tokens, written by onboarding and refreshed by the runtime
        (``custom_agents.xai_oauth_auth``)."""

        return self.auth_dir / XAI_OAUTH_AUTH_FILENAME

    @property
    def runtime_dir(self) -> Path:
        """Records trusted services keep while the team runs (see module docstring)."""

        return self.root / RUNTIME_DIRNAME

    @property
    def environment_overrides_path(self) -> Path:
        """Names of the environment variables that override saved settings, written by the launcher.

        Read by the gateway's Status page (``assistant_gateway.status_page``); never holds a value.
        """

        return self.runtime_dir / ENVIRONMENT_OVERRIDES_FILENAME

    @property
    def process_run_lock_path(self) -> Path:
        """Held by every process of the current runtime run (``runtime_hosting.process_run``).

        Taking it proves that no agent process of an earlier run is alive.
        """

        return self.runtime_dir / PROCESS_RUN_LOCK_FILENAME

    # Team import
    @property
    def team_import_dir(self) -> Path:
        """Reports of ``assistant_cli team import`` (``team_transfer.service``)."""

        return self.root / RUNTIME_DIRNAME / TEAM_IMPORT_DIRNAME

    @property
    def team_import_marker_path(self) -> Path:
        """Present while a team import is unfinished; the runtime refuses to start then."""

        return self.team_import_dir / TEAM_IMPORT_MARKER_FILENAME

    @property
    def team_import_receipt_path(self) -> Path:
        """The source and record IDs already applied by reviewed import batches."""
        return self.team_import_dir / "receipt.json"

    @property
    def team_import_lock_path(self) -> Path:
        """Serializes batch imports, including broker-only batches during a run."""
        return self.team_import_dir / "import.lock"

    @property
    def team_import_review_dir(self) -> Path:
        """Onboarding's review folders, one per opened export (``team_transfer.review``)."""
        return self.team_import_dir / TEAM_IMPORT_REVIEW_DIRNAME

    @property
    def team_import_credentials_path(self) -> Path:
        """Credentials an onboarding import left for the first start.

        Onboarding runs before the broker, which alone stores credentials, so
        it leaves the export's still-encrypted credentials and their key here
        (0600). The supervisor's first-start step hands them to the broker and
        deletes the file (``team_transfer.credential_handover``). Under
        ``state/`` so no agent tool or sandboxed code can reach it."""

        return self.state_dir / TEAM_IMPORT_CREDENTIALS_FILENAME

    # Skills
    @property
    def runtime_restart_authorization_dir(self) -> Path:
        """Staged restart plans and supervisor-recorded dispatch receipts."""
        return self.runtime_dir / "restart-authorization"

    @property
    def runtime_restart_requests_dir(self) -> Path:
        """Atomic approved requests waiting for the supervisor."""
        return self.runtime_dir / "restart_requests"

    @property
    def skill_installation_dir(self) -> Path:
        """Quarantine, plans, receipts and installs of reviewed skill changes."""

        return self.runtime_dir / SKILL_INSTALLATION_DIRNAME

    @property
    def installed_skills_dir(self) -> Path:
        return self.skill_installation_dir / INSTALLED_SKILLS_DIRNAME

    @property
    def agent_readable_skill_dirs(self) -> tuple[Path, ...]:
        """Skill folders agents may read but never write (see module docstring)."""

        return (self.skills_dir, self.installed_skills_dir)

    def agent_readable_skill_dir_of(self, path: str | Path) -> Path | None:
        """The read-only skill folder containing ``path`` (resolved), if any."""

        resolved = Path(path)
        for skill_dir in self.agent_readable_skill_dirs:
            if resolved == skill_dir or skill_dir in resolved.parents:
                return skill_dir
        return None

    # MCP connections, hosted MCP servers and installed permissions
    @property
    def mcp_connections_dir(self) -> Path:
        """Installed MCP connections: their records, approval receipts, the
        static baseline and each connection's confirmed operation levels.

        Written only by trusted code (``mcp_control.registry``,
        ``mcp_control.operation_levels``) when a person approves a connection or
        the operator's configuration lists one; read by the tool catalog and
        the agents that load the servers' tools.
        """

        return self.runtime_dir / MCP_CONNECTIONS_DIRNAME

    @property
    def mcp_hosting_definitions_dir(self) -> Path:
        """Reviewed definitions of MCP servers the instance hosts itself
        (``mcp_control.hosting``), written when an administrator approves one."""

        return self.runtime_dir / MCP_HOSTING_DIRNAME / MCP_HOSTING_DEFINITIONS_DIRNAME

    @property
    def mcp_hosting_state_dir(self) -> Path:
        """Running state of hosted MCP servers: removed definitions, access
        tokens and logs (``mcp_control.hosting``), written by the broker."""

        return self.runtime_dir / MCP_HOSTING_DIRNAME / MCP_HOSTING_STATE_DIRNAME

    @property
    def installed_permissions_dir(self) -> Path:
        """Proposals, installed definitions and receipts of installed permissions
        (``custom_agents.installed_permissions``); administrators install them in
        the gateway."""

        return self.runtime_dir / INSTALLED_PERMISSIONS_DIRNAME

    # Databases and runtime records
    @property
    def task_database_path(self) -> Path:
        return self.dbs_dir / TASK_DATABASE_FILENAME

    @property
    def conversation_artifacts_dir(self) -> Path:
        """Long texts offloaded from conversation turns, whatever the storage backend."""

        return self.dbs_dir / CONVERSATION_ARTIFACTS_DIRNAME

    @property
    def tool_results_dir(self) -> Path:
        return self.state_dir / "tool_results"

    @property
    def published_sites_dir(self) -> Path:
        """Trusted records of published pages (``<site_id>.json``); the pages
        themselves live in the person's folder (``user_docs.published_sites``)."""

        return self.state_dir / "published_sites"

    @property
    def model_failover_dir(self) -> Path:
        return self.state_dir / "model_failover"

    @property
    def diagnostics_dir(self) -> Path:
        return self.state_dir / "diagnostics"

    @property
    def execution_artifacts_dir(self) -> Path:
        """Content-addressed execution artifacts; verified by hash on every read."""

        return self.state_dir / "execution_artifacts"

    @property
    def execution_runs_dir(self) -> Path:
        """Run and build folders. OpenShell sandboxes mount workspaces/, so these
        must sit inside it; they hold materialized copies of verified artifacts."""

        return self.agents_dir / EXECUTION_DIRNAME

    @property
    def execution_rust_builds_dir(self) -> Path:
        """Content-addressed Rust build folders (``runtime_hosting.builds``)."""

        return self.execution_runs_dir / EXECUTION_RUST_BUILDS_DIRNAME

    @property
    def uploads_dir(self) -> Path:
        """Chat uploads, by session. The owner is unknown when a chat adapter
        stores the bytes, so they live here; an agent reads one by id from its
        task's attachment list and may copy it into the task's own folder."""

        return self.state_dir / "uploads"

    @property
    def attachment_copies_dir(self) -> Path:
        """Private, short-lived copies of sent files for readers that need a path.

        The Photon sidecar reads a file by path, so the gateway gives it a
        0600 copy of the checked bytes here and removes it after the call
        (``assistant_gateway.attachments.StagedAttachmentReader``)."""

        return self.state_dir / "attachment_copies"

    @property
    def onboarding_account_keys_path(self) -> Path:
        """Keys typed during onboarding, waiting for the gateway to hand them to the broker.

        Onboarding runs before the broker, which owns the credential store, so
        it leaves the Exa and Brave Search keys here (0600, written atomically)
        and the gateway links them for the team at its next start, then deletes
        the file (``assistant_gateway.onboarding_account_keys``). Under
        ``state/`` so no agent tool or sandboxed code can reach it."""

        return self.state_dir / ONBOARDING_ACCOUNT_KEYS_FILENAME

    @property
    def held_people_dir(self) -> Path:
        """Folders of people removed from the team, held until restored or purged.

        Under ``state/`` so no agent tool or sandboxed code can reach them,
        and on the same file system as ``workspaces/`` so the folder moves in
        one rename (``access_control.held_person_folders``)."""

        return self.state_dir / HELD_PEOPLE_DIRNAME

    @property
    def python_env_locks_dir(self) -> Path:
        """Per-environment use/cleanup locks, outside agent-writable folders."""

        return self.runtime_dir / "python-env-locks"

    # People
    def person_scratch_dir(self, canonical_user_id: str | None) -> Path:
        """``scratch/`` of a person's folder, or of ``agents/`` for work for no person."""

        return self.workspaces_dir.joinpath(*scratch_folder_parts(canonical_user_id))

    def person_sites_dir(self, canonical_user_id: str | None) -> Path:
        """``sites/`` of a person's folder, or of ``agents/`` for work for no person."""

        return self.workspaces_dir.joinpath(*sites_folder_parts(canonical_user_id))

    def person_dir(self, canonical_user_id: str) -> Path:
        return self.users_dir / person_folder_name(canonical_user_id)

    def person_research_dir(self, canonical_user_id: str | None) -> Path:
        base = self.person_dir(canonical_user_id) if canonical_user_id else self.agents_dir
        return base / PERSON_RESEARCH_DIRNAME

    def person_python_envs_dir(self, canonical_user_id: str | None) -> Path:
        base = self.person_dir(canonical_user_id) if canonical_user_id else self.agents_dir
        return base / PERSON_PYTHON_ENVS_DIRNAME

    def scope_for(self, canonical_user_id: str | None) -> WorkspaceScope:
        return workspace_scope(self.workspaces_dir, canonical_user_id)

    def ensure_directories(self) -> None:
        """Create the areas; people's folders are created on first use."""

        for directory in (
            self.control_dir,
            self.state_dir,
            self.dbs_dir,
            self.users_dir,
            self.team_dir,
            self.team_shared_dir,
            self.agents_dir,
        ):
            directory.mkdir(parents=True, exist_ok=True)

    def is_instance_private(self, path: str | Path) -> bool:
        """Whether a resolved path is inside the root but outside ``workspaces/``.

        Such paths (control, state, databases, credentials, runtime records,
        operator files) are refused to agent tools even when the operator turns
        off the workspace restriction, and never granted to sandboxed code.
        """

        resolved = Path(path)
        if not (resolved == self.root or self.root in resolved.parents):
            return False
        return not (
            resolved == self.workspaces_dir or self.workspaces_dir in resolved.parents
        )


def person_area_parts(canonical_user_id: str | None) -> tuple[str, ...]:
    """The folder a person's work keeps its files in, relative to ``workspaces/``.

    ``("users", <person folder>)`` for a person, ``("agents",)`` for work for
    no person. Trusted code opens paths below ``workspaces/`` one component at
    a time (``access_control.safe_open``), so it needs the parts, not a path.
    """

    person = str(canonical_user_id or "").strip()
    if not person:
        return (AGENTS_DIRNAME,)
    return (USERS_DIRNAME, person_folder_name(person))


def scratch_folder_parts(canonical_user_id: str | None) -> tuple[str, ...]:
    """``scratch/`` of the person's folder (or of ``agents/``), relative to ``workspaces/``."""

    return (*person_area_parts(canonical_user_id), SCRATCH_DIRNAME)


def sites_folder_parts(canonical_user_id: str | None) -> tuple[str, ...]:
    """``sites/`` of the person's folder (or of ``agents/``), relative to ``workspaces/``."""

    return (*person_area_parts(canonical_user_id), PERSON_SITES_DIRNAME)


def workspace_scope(workspaces_dir: str | Path, canonical_user_id: str | None) -> WorkspaceScope:
    """Folders for a task working for ``canonical_user_id`` (or for no person)."""

    base = Path(workspaces_dir).expanduser().resolve()
    team = base / TEAM_DIRNAME
    agents = base / AGENTS_DIRNAME
    person = str(canonical_user_id or "").strip() or None
    if person is None:
        return WorkspaceScope(base, None, agents, (agents, team))
    home = base / USERS_DIRNAME / person_folder_name(person)
    return WorkspaceScope(base, person, home, (home, team, agents))
