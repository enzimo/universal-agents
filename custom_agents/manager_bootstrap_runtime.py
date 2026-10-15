"""Manager-owned bootstrap and legacy-state migrations.

The gateway supplies only configuration values. This module performs every
manager-store read and write from inside the manager process.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Optional

from custom_agents.heartbeat_store import HeartbeatStore, extract_active_heartbeat_tasks
from custom_agents.runtime_paths import agent_layout
from custom_agents.pinned_memory_runtime import (
    PinnedMemoryActor,
    PinnedMemoryCommand,
    PinnedMemoryRequestContext,
    PinnedMemoryService,
)
from id_generation import new_id
from storage.pinned_memory_repository import USERMEM_BANK

ONBOARDING_USER_CONTEXT_SOURCE_KEY = "onboarding:user_context:v1"
LEGACY_USER_MD_SOURCE_KEY = "legacy_workspace:USER.md:v1"
LEGACY_ARCHIVE_DIR = "workspace_legacy_markdown"


@dataclass(frozen=True)
class WorkspaceMarkdownMigrationResult:
    filename: str
    status: str
    source_path: Optional[Path] = None
    archive_path: Optional[Path] = None
    imported_count: int = 0
    memory_id: Optional[int] = None
    source_hash: Optional[str] = None


@dataclass(frozen=True)
class ManagerBootstrapResult:
    onboarding_memory_id: Optional[int]
    workspace_migrations: tuple[WorkspaceMarkdownMigrationResult, ...]


def _content_hash(content: str) -> str:
    return hashlib.sha256(content.encode("utf-8")).hexdigest()


def _archive_legacy_file(
    *,
    instance_root: Path,
    source_path: Path,
    source_hash: str,
) -> Path:
    archive_dir = instance_root / "migrations" / LEGACY_ARCHIVE_DIR
    archive_dir.mkdir(parents=True, exist_ok=True)
    archive_path = archive_dir / f"{source_path.stem}.{source_hash[:12]}{source_path.suffix}"
    if archive_path.exists():
        if _content_hash(archive_path.read_text(encoding="utf-8")) != source_hash:
            raise RuntimeError(f"Legacy workspace archive collision for {source_path.name}: {archive_path}")
        source_path.unlink()
        return archive_path
    source_path.replace(archive_path)
    return archive_path


def seed_onboarding_user_context(
    *,
    service: PinnedMemoryService,
    team_id: str,
    canonical_user_id: str,
    request_timezone: str,
    instance_name: str,
    user_context: str,
) -> Optional[dict[str, Any]]:
    """Persist configuration-provided onboarding prose idempotently."""

    content = str(user_context or "").strip()
    if not content:
        return None
    source_hash = _content_hash(content)
    context = PinnedMemoryRequestContext(
        team_id=team_id,
        canonical_user_id=canonical_user_id,
        request_timezone=request_timezone,
        actor=PinnedMemoryActor(kind="system", id="manager_bootstrap", display_name="Manager Bootstrap"),
    )
    facts = service.execute(
        PinnedMemoryCommand(bank=USERMEM_BANK, action="list", range_start=1, range_end=1000),
        context=context,
    ).get("facts", [])
    existing = next(
        (fact for fact in facts if str(fact.get("source_key") or "") == ONBOARDING_USER_CONTEXT_SOURCE_KEY),
        None,
    )
    result = service.execute(
        PinnedMemoryCommand(
            bank=USERMEM_BANK,
            action="replace" if existing is not None else "create",
            memory_id=int(existing["memory_id"]) if existing is not None else None,
            fact_text=content,
            tags=["profile", "onboarding"],
            metadata={
                "source_kind": "onboarding",
                "source_key": ONBOARDING_USER_CONTEXT_SOURCE_KEY,
                "source_hash": source_hash,
                "schema_version": 1,
                "instance_name": instance_name,
            },
            source_key=ONBOARDING_USER_CONTEXT_SOURCE_KEY if existing is None else None,
        ),
        context=context,
    )
    return result


def migrate_legacy_user_markdown(
    *,
    workspace_dir: Path,
    instance_root: Path,
    service: PinnedMemoryService,
    team_id: str,
    canonical_user_id: str,
    request_timezone: str,
) -> WorkspaceMarkdownMigrationResult:
    source_path = workspace_dir / "USER.md"
    if not source_path.exists():
        return WorkspaceMarkdownMigrationResult(filename=source_path.name, status="not_present")
    raw_content = source_path.read_text(encoding="utf-8")
    content = raw_content.strip()
    source_hash = _content_hash(raw_content)
    memory_id: Optional[int] = None
    if content:
        context = PinnedMemoryRequestContext(
            team_id=team_id,
            canonical_user_id=canonical_user_id,
            request_timezone=request_timezone,
            actor=PinnedMemoryActor(
                kind="system",
                id="manager_legacy_workspace_migration",
                display_name="Manager Legacy Workspace Migration",
            ),
        )
        result = service.execute(
            PinnedMemoryCommand(
                bank=USERMEM_BANK,
                action="create",
                fact_text=content,
                tags=["profile", "legacy-import"],
                metadata={
                    "source_kind": "legacy_workspace_markdown",
                    "source_key": LEGACY_USER_MD_SOURCE_KEY,
                    "source_hash": source_hash,
                    "schema_version": 1,
                    "legacy_filename": source_path.name,
                },
                source_key=LEGACY_USER_MD_SOURCE_KEY,
            ),
            context=context,
        )
        memory_id = int(result["memory_id"])
        facts = service.execute(
            PinnedMemoryCommand(bank=USERMEM_BANK, action="list", range_start=1, range_end=1000),
            context=context,
        ).get("facts", [])
        if not any(
            int(fact.get("memory_id") or 0) == memory_id
            and str(fact.get("source_key") or "") == LEGACY_USER_MD_SOURCE_KEY
            and str(fact.get("fact_text") or "") == content
            and str((fact.get("metadata") or {}).get("source_hash") or "") == source_hash
            for fact in facts
        ):
            raise RuntimeError(
                f"Legacy USER.md usermem verification failed for canonical user {canonical_user_id}."
            )
    archive_path = _archive_legacy_file(
        instance_root=instance_root,
        source_path=source_path,
        source_hash=source_hash,
    )
    return WorkspaceMarkdownMigrationResult(
        filename=source_path.name,
        status="migrated",
        source_path=source_path,
        archive_path=archive_path,
        imported_count=1 if content else 0,
        memory_id=memory_id,
        source_hash=source_hash,
    )


def report_obsolete_tools_markdown(*, workspace_dir: Path) -> WorkspaceMarkdownMigrationResult:
    source_path = workspace_dir / "TOOLS.md"
    if not source_path.exists():
        return WorkspaceMarkdownMigrationResult(filename=source_path.name, status="not_present")
    return WorkspaceMarkdownMigrationResult(
        filename=source_path.name,
        status="obsolete_user_review_required",
        source_path=source_path,
    )


def migrate_legacy_heartbeat_markdown(
    *,
    workspace_dir: Path,
    instance_root: Path,
    heartbeat_store: HeartbeatStore,
    source_id: str,
    conversation_id_factory: Optional[Callable[[], str]] = None,
) -> WorkspaceMarkdownMigrationResult:
    source_path = workspace_dir / "HEARTBEAT.md"
    if not source_path.exists():
        return WorkspaceMarkdownMigrationResult(filename=source_path.name, status="not_present")
    content = source_path.read_text(encoding="utf-8")
    source_hash = _content_hash(content)
    active_tasks = extract_active_heartbeat_tasks(content)
    existing_items = heartbeat_store.list_items(source_id)
    matching_prefix = f"legacy_heartbeat_md_v1:{source_hash}:"
    if existing_items and not any(str(item.source_key or "").startswith(matching_prefix) for item in existing_items):
        created = []
        status = "archived_canonical_state_present"
    else:
        created = heartbeat_store.seed_items_from_markdown(
            source_id=source_id,
            markdown_content=content,
            conversation_id_factory=(
                conversation_id_factory
                or (lambda: new_id("conversation_id"))
            ),
        )
        imported_keys = {
            str(item.source_key or "")
            for item in heartbeat_store.list_items(source_id)
            if str(item.source_key or "").startswith(matching_prefix)
        }
        expected_keys = {f"{matching_prefix}{index}" for index in range(1, len(active_tasks) + 1)}
        if imported_keys != expected_keys:
            raise RuntimeError(
                f"Legacy HEARTBEAT.md verification failed for source {source_id}: "
                f"expected {len(expected_keys)} source keys, found {len(imported_keys)}."
            )
        status = "migrated"
    archive_path = _archive_legacy_file(
        instance_root=instance_root,
        source_path=source_path,
        source_hash=source_hash,
    )
    return WorkspaceMarkdownMigrationResult(
        filename=source_path.name,
        status=status,
        source_path=source_path,
        archive_path=archive_path,
        imported_count=len(created),
        source_hash=source_hash,
    )


def run_manager_bootstrap(agent: Any) -> ManagerBootstrapResult:
    """Initialize manager-owned state using configuration handed to the agent."""

    if not getattr(agent, "is_team_lead", False):
        return ManagerBootstrapResult(None, ())
    config = dict(getattr(agent, "bootstrap_config", {}) or {})
    if not config:
        return ManagerBootstrapResult(None, ())
    owner = str(config.get("canonical_user_id") or "").strip()
    team_id = str(getattr(agent, "team_id", "") or config.get("team_id") or "").strip()
    if not owner or not team_id:
        agent._logger.warning(
            "[%s] Manager bootstrap skipped because canonical user or team id is missing. "
            "event=manager_bootstrap_owner_missing",
            agent.id,
        )
        return ManagerBootstrapResult(None, ())

    # Rows in the manager's store keep the owner their verified identity gave
    # them; one with no owner is work for no person and is never handed to
    # this first user (storage.multi_user_owner_migration).
    service = PinnedMemoryService(
        agent._pinned_memory_repository,
        config=dict(getattr(agent, "memory_config", {}) or {}),
        command_origin="manager_bootstrap",
        logger=agent._logger,
    )
    initial_result = seed_onboarding_user_context(
        service=service,
        team_id=team_id,
        canonical_user_id=owner,
        request_timezone=str(config.get("timezone") or "UTC"),
        instance_name=str(config.get("instance_name") or "default"),
        user_context=str(config.get("initial_user_context") or ""),
    )

    layout = agent_layout(agent)
    instance_root = layout.root
    # Very old instances kept USER.md, HEARTBEAT.md and TOOLS.md in the single
    # workspace; the layout move commands put that folder's files in team/.
    workspace_dir = layout.team_dir
    migrations = [
        migrate_legacy_user_markdown(
            workspace_dir=workspace_dir,
            instance_root=instance_root,
            service=service,
            team_id=team_id,
            canonical_user_id=owner,
            request_timezone=str(config.get("timezone") or "UTC"),
        )
    ]
    heartbeat_source_id = str(config.get("heartbeat_source_id") or "").strip()
    if heartbeat_source_id:
        heartbeat_store = HeartbeatStore(
            db_path=agent.task_manager._db_path,
            logger_=agent._logger,
            storage_settings=agent.task_manager.storage_settings,
        )
        heartbeat_store.ensure_source(
            source_id=heartbeat_source_id,
            title=f"{config.get('instance_name') or 'Universal Agents'} heartbeat",
            enabled=bool(config.get("heartbeat_enabled", True)),
        )
        migrations.append(
            migrate_legacy_heartbeat_markdown(
                workspace_dir=workspace_dir,
                instance_root=instance_root,
                heartbeat_store=heartbeat_store,
                source_id=heartbeat_source_id,
            )
        )
    tools_result = report_obsolete_tools_markdown(workspace_dir=workspace_dir)
    migrations.append(tools_result)
    if tools_result.status == "obsolete_user_review_required":
        agent._logger.warning(
            "[%s] Legacy TOOLS.md is obsolete and is not loaded. "
            "event=legacy_tools_md_obsolete path=%s",
            agent.id,
            tools_result.source_path,
        )
    agent._logger.info(
        "[%s] Manager-owned bootstrap complete. event=manager_bootstrap_complete "
        "owner_canonical_user_id=%s onboarding_memory_id=%s",
        agent.id,
        owner,
        (initial_result or {}).get("memory_id"),
    )
    return ManagerBootstrapResult(
        onboarding_memory_id=int(initial_result["memory_id"]) if initial_result else None,
        workspace_migrations=tuple(migrations),
    )
