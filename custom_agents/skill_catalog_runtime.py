"""Cross-agent convergence for the shared managed skill catalog."""

from __future__ import annotations

from collections import deque
from dataclasses import dataclass, field as dataclass_field
import json
import logging
from pathlib import Path
from typing import Any, Literal, Mapping

from custom_agents.skill_installation import (
    resolve_skill_installation_paths,
    verify_managed_skill_attestation,
)
from custom_agents.sdk.strands.skills import (
    is_agent_skills_plugin,
    list_agent_skill_names,
)
from custom_messages.contracts.envelope_v1 import MessageEnvelope
from custom_messages.contracts.routing_v1 import MessageHeader, MessageMeta
from custom_messages.contracts.runtime_v1 import TEAM_ARCHITECT_AGENT_TYPE
from custom_messages.enums import RoutingType
from custom_messages.skill_catalog import ManagedSkillCatalogChangedV1


logger = logging.getLogger(__name__)
_SEEN_MUTATION_LIMIT = 256


@dataclass(frozen=True)
class SkillCatalogRefreshResult:
    """Result of one local filesystem fingerprint and SDK cache refresh."""

    changed: bool
    reloaded_plugin_count: int
    available_skill_names: tuple[str, ...]


@dataclass
class SkillCatalogRuntimeState:
    """Process-local convergence diagnostics and notification deduplication."""

    last_error: str | None = None
    seen_mutations: deque[str] = dataclass_field(
        default_factory=lambda: deque(maxlen=_SEEN_MUTATION_LIMIT)
    )


def _mutation_key(notification: ManagedSkillCatalogChangedV1) -> str:
    return ":".join(
        (
            notification.team_id,
            notification.operation,
            notification.change_id,
            notification.installed_tree_sha256,
        )
    )


def _seen_mutations(agent: Any) -> deque[str]:
    return _runtime_state(agent).seen_mutations


def _runtime_state(agent: Any) -> SkillCatalogRuntimeState:
    state = getattr(agent, "_skill_catalog_runtime_state", None)
    if isinstance(state, SkillCatalogRuntimeState):
        return state
    replacement = SkillCatalogRuntimeState()
    setattr(agent, "_skill_catalog_runtime_state", replacement)
    return replacement


def _load_receipt(path: Path) -> Mapping[str, Any]:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise PermissionError("managed skill mutation receipt is missing") from exc
    except (OSError, json.JSONDecodeError) as exc:
        raise PermissionError("managed skill mutation receipt is unreadable") from exc
    if not isinstance(payload, dict):
        raise PermissionError("managed skill mutation receipt must be a JSON object")
    return payload


def verify_managed_skill_catalog_notification(
    agent: Any,
    notification: ManagedSkillCatalogChangedV1,
) -> Mapping[str, Any]:
    """Verify a reload hint against the exact immutable local receipt."""

    local_team_id = str(getattr(agent, "team_id", "") or "")
    if notification.team_id != local_team_id:
        raise PermissionError("managed skill catalog notification crossed team scope")
    paths = resolve_skill_installation_paths(agent)
    receipt_root = (
        paths.receipts
        if notification.operation == "install"
        else paths.uninstallation_receipts
    )
    receipt = _load_receipt(receipt_root / f"{notification.change_id}.json")
    expected = {
        "change_id": notification.change_id,
        "team_id": notification.team_id,
        "skill_name": notification.skill_name,
        "installed_tree_sha256": notification.installed_tree_sha256,
    }
    for field_name, expected_value in expected.items():
        if str(receipt.get(field_name) or "") != expected_value:
            raise PermissionError(
                f"managed skill mutation receipt does not match {field_name}"
            )
    if notification.operation == "install":
        installed_digest = verify_managed_skill_attestation(
            agent,
            paths.installed / notification.skill_name,
        )
        if installed_digest != notification.installed_tree_sha256:
            raise PermissionError(
                "managed skill installation attestation does not match the notification"
            )
    return receipt


def verify_managed_skill_catalog_integrity(agent: Any) -> None:
    """Fail closed when a runtime-owned managed skill no longer matches attestation."""

    try:
        paths = resolve_skill_installation_paths(agent)
    except RuntimeError:
        return
    if not paths.installed.exists():
        return
    for entry in sorted(
        paths.installed.iterdir(), key=lambda item: item.name.casefold()
    ):
        if not entry.is_dir():
            raise PermissionError(
                "managed skill installation root contains a non-directory entry"
            )
        verify_managed_skill_attestation(agent, entry)


def _live_strands_agents(agent: Any) -> list[Any]:
    """Every Strands agent the agent's turns may call now: the main one, each
    parallel lane's and each running turn's (``custom_agents.execution_lanes_runtime``)."""

    candidates = [getattr(agent, "_strands_agent", None)]
    lanes = getattr(agent, "_parallel_lanes", None)
    if lanes is not None:
        candidates.extend(parts.agent for parts in lanes.parts.values())
    for work in list(getattr(agent, "_running_turns", {}).values()):
        if work.strands is not None:
            candidates.append(work.strands.agent)
    unique: dict[int, Any] = {}
    for sdk_agent in candidates:
        if sdk_agent is not None:
            unique.setdefault(id(sdk_agent), sdk_agent)
    return list(unique.values())


async def _hydrate_skill_plugins(
    agent: Any,
    *,
    changed: bool,
) -> tuple[int, set[str]]:
    """Reload changed skills into every live Strands agent; the main agent's names report them.

    A refresh clears the shared plugin's skills of every Strands agent, so
    each one running a turn now loads them again at once, not only the main
    one: a turn in a parallel lane otherwise lacked them until its next model
    call.
    """

    sdk_agent = getattr(agent, "_strands_agent", None)
    plugins = getattr(agent, "plugins", None) or []
    reloaded_plugin_count = 0
    available_skill_names: set[str] = set()
    for plugin in plugins:
        if not is_agent_skills_plugin(plugin):
            continue
        if changed:
            live = _live_strands_agents(agent)
            for live_agent in live:
                await plugin.init_agent(live_agent)
            reloaded_plugin_count += 1 if live else 0
        if sdk_agent is not None:
            available_skill_names.update(list_agent_skill_names(plugin, sdk_agent))
    return reloaded_plugin_count, available_skill_names


async def _publish_refresh_presence(agent: Any, *, publish_presence: bool) -> None:
    if (
        publish_presence
        and getattr(agent, "team_id", None)
        and getattr(agent, "_runtime_loop", None) is not None
    ):
        await agent._publish_presence_status(is_exiting=False)


async def refresh_agent_skill_catalog(
    agent: Any,
    *,
    force: bool,
    publish_presence: bool,
    reason: str,
) -> SkillCatalogRefreshResult:
    """Rescan local sources, hydrate SDK caches, and optionally publish presence."""

    refresh = getattr(agent, "_refresh_skills_plugins", None)
    if not callable(refresh):
        return SkillCatalogRefreshResult(False, 0, ())

    previous_fingerprint = getattr(agent, "_skills_fingerprint", None)
    runtime_state = _runtime_state(agent)
    prior_error = str(runtime_state.last_error or "")
    try:
        verify_managed_skill_catalog_integrity(agent)
        refresh(force=force)
        current_fingerprint = getattr(agent, "_skills_fingerprint", None)
        changed = bool(
            force or prior_error or previous_fingerprint != current_fingerprint
        )
        reloaded_plugin_count, available_skill_names = await _hydrate_skill_plugins(
            agent,
            changed=changed,
        )
        runtime_state.last_error = None
    except Exception as exc:
        runtime_state.last_error = f"{type(exc).__name__}: {exc}"
        raise

    result = SkillCatalogRefreshResult(
        changed=changed,
        reloaded_plugin_count=reloaded_plugin_count,
        available_skill_names=tuple(sorted(available_skill_names, key=str.casefold)),
    )
    if not changed:
        return result

    refresh_state = getattr(agent, "_refresh_self_agent_state", None)
    if callable(refresh_state):
        await refresh_state()
    await _publish_refresh_presence(agent, publish_presence=publish_presence)
    getattr(agent, "_logger", logger).info(
        "[%s] Reloaded %s AgentSkills plugin(s). "
        "event=managed_skill_catalog_reloaded reason=%s available_skill_count=%s",
        getattr(agent, "id", "unknown"),
        reloaded_plugin_count,
        reason,
        len(result.available_skill_names),
    )
    return result


def build_managed_skill_catalog_notification(
    agent: Any,
    result: Mapping[str, Any],
    *,
    operation: Literal["install", "uninstall"],
) -> ManagedSkillCatalogChangedV1:
    """Build a secret-free hint from one committed mutation result."""

    if not bool(getattr(agent, "is_team_lead", False)):
        raise PermissionError("only TeamArchitect may publish skill catalog changes")
    if str(getattr(agent, "named_agent_type", "") or "") != TEAM_ARCHITECT_AGENT_TYPE:
        raise PermissionError("skill catalog changes require TeamArchitect identity")
    return ManagedSkillCatalogChangedV1(
        team_id=str(getattr(agent, "team_id", "") or ""),
        source_agent_id=str(getattr(agent, "id", "") or ""),
        source_named_agent_type=TEAM_ARCHITECT_AGENT_TYPE,
        operation=operation,
        change_id=str(result.get("change_id") or ""),
        skill_name=str(result.get("skill_name") or ""),
        installed_tree_sha256=str(result.get("installed_tree_sha256") or ""),
    )


async def publish_managed_skill_catalog_notification(
    agent: Any,
    result: Mapping[str, Any],
    *,
    operation: Literal["install", "uninstall"],
) -> ManagedSkillCatalogChangedV1:
    """Publish a live all-agent reload hint after the receipt is durable."""

    notification = build_managed_skill_catalog_notification(
        agent,
        result,
        operation=operation,
    )
    envelope = MessageEnvelope(
        msg_content=notification,
        msg_header=MessageHeader(
            from_agent_id=str(agent.id),
            to_routing_path=RoutingType.ALL_AGENTS_ROUTE,
            agent_signature="",
            idempotency_key=notification.notification_id,
        ),
        msg_meta=MessageMeta(from_agent_name=str(agent.name)),
    )
    await agent._send_operational_message(envelope)
    return notification


async def handle_managed_skill_catalog_notification(
    agent: Any,
    *,
    notification: ManagedSkillCatalogChangedV1,
    msg_header: MessageHeader,
    msg_meta: MessageMeta,
) -> bool:
    """Verify and apply one authority-free team catalog reload hint."""

    _ = msg_meta
    agent_logger = getattr(agent, "_logger", logger)
    if msg_header.from_agent_id != notification.source_agent_id:
        agent_logger.warning(
            "Rejected managed skill catalog notification with mismatched sender. "
            "event=managed_skill_catalog_sender_rejected agent_id=%s source_agent_id=%s "
            "header_from_agent_id=%s change_id=%s",
            getattr(agent, "id", "unknown"),
            notification.source_agent_id,
            msg_header.from_agent_id,
            notification.change_id,
        )
        return False
    if notification.source_named_agent_type != TEAM_ARCHITECT_AGENT_TYPE:
        agent_logger.warning(
            "Rejected managed skill catalog notification from a non-manager type. "
            "event=managed_skill_catalog_sender_type_rejected agent_id=%s "
            "source_named_agent_type=%s change_id=%s",
            getattr(agent, "id", "unknown"),
            notification.source_named_agent_type,
            notification.change_id,
        )
        return False

    mutation_key = _mutation_key(notification)
    seen = _seen_mutations(agent)
    if mutation_key in seen:
        return True
    if bool(getattr(agent, "is_executing", False)):
        agent_logger.info(
            "[%s] Deferred managed skill catalog notification until the active "
            "turn completes. event=managed_skill_catalog_notification_deferred_busy "
            "change_id=%s operation=%s skill_name=%s",
            getattr(agent, "id", "unknown"),
            notification.change_id,
            notification.operation,
            notification.skill_name,
        )
        return True
    try:
        verify_managed_skill_catalog_notification(agent, notification)
        await refresh_agent_skill_catalog(
            agent,
            force=True,
            publish_presence=True,
            reason=f"managed_{notification.operation}_notification",
        )
    except Exception as exc:
        agent_logger.warning(
            "Managed skill catalog notification could not be applied. "
            "event=managed_skill_catalog_notification_failed agent_id=%s "
            "change_id=%s operation=%s error_type=%s error=%s",
            getattr(agent, "id", "unknown"),
            notification.change_id,
            notification.operation,
            type(exc).__name__,
            exc,
        )
        return False

    seen.append(mutation_key)
    agent_logger.info(
        "[%s] Applied managed skill catalog notification. "
        "event=managed_skill_catalog_notification_applied change_id=%s "
        "operation=%s skill_name=%s",
        getattr(agent, "id", "unknown"),
        notification.change_id,
        notification.operation,
        notification.skill_name,
    )
    return True


async def repair_agent_skill_catalog(agent: Any, *, reason: str) -> bool:
    """Best-effort convergence when a non-durable notification was missed."""

    if bool(getattr(agent, "is_executing", False)):
        return False

    try:
        result = await refresh_agent_skill_catalog(
            agent,
            force=False,
            publish_presence=False,
            reason=reason,
        )
    except Exception as exc:
        getattr(agent, "_logger", logger).warning(
            "Managed skill catalog repair failed; stale presence will remain visible. "
            "event=managed_skill_catalog_repair_failed agent_id=%s reason=%s "
            "error_type=%s error=%s",
            getattr(agent, "id", "unknown"),
            reason,
            type(exc).__name__,
            exc,
        )
        return False
    return result.changed


__all__ = [
    "ManagedSkillCatalogChangedV1",
    "SkillCatalogRefreshResult",
    "SkillCatalogRuntimeState",
    "build_managed_skill_catalog_notification",
    "handle_managed_skill_catalog_notification",
    "publish_managed_skill_catalog_notification",
    "refresh_agent_skill_catalog",
    "repair_agent_skill_catalog",
    "verify_managed_skill_catalog_notification",
    "verify_managed_skill_catalog_integrity",
]
