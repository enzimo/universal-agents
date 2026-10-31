"""The kinds of staged changes agents apply with ``apply_change``.

Each adapter connects one catalog change kind (``changes:`` in
``ua_definitions/access/tools``) to the service that staged the plan; the
flow and its rules are in ``access_control.changes``. Adding a kind means an
adapter here plus its catalog entry.
"""

from __future__ import annotations

from typing import Any

from access_control.changes import ChangeAdapter, ChangeApproval
from custom_messages.contracts.access_v2 import ChangeProposalV1
from custom_messages.contracts.schedule_v1 import SYSTEM_SCHEDULE_CHANGE_KIND


class RuntimeSettingsChanges:
    """Runtime settings plans; one that changes only model-choice settings is
    described as the ``model_choice`` kind (``custom_agents.model_choice``)."""

    kind = "runtime_settings"

    def owns(self, change_id: str) -> bool:
        return change_id.startswith("settings_change_")

    def describe(self, agent: Any, change_id: str) -> ChangeProposalV1:
        from custom_agents.runtime_settings_service import RuntimeSettingsService

        return RuntimeSettingsService(agent).describe(change_id)

    def apply(self, agent: Any, change_id: str, approval: ChangeApproval) -> dict[str, Any]:
        from custom_agents.runtime_settings_service import RuntimeSettingsService

        return RuntimeSettingsService(agent).apply(change_id, approval)


class RuntimeRestartChanges:
    kind = "runtime_restart"

    def owns(self, change_id: str) -> bool:
        return change_id.startswith("runtime_restart_")

    def describe(self, agent: Any, change_id: str) -> ChangeProposalV1:
        from custom_agents.runtime_restart_service import RuntimeRestartService

        return RuntimeRestartService(agent).describe(change_id)

    def apply(self, agent: Any, change_id: str, approval: ChangeApproval) -> dict[str, Any]:
        from custom_agents.runtime_restart_service import RuntimeRestartService

        return RuntimeRestartService(agent).apply(change_id, approval)


class GeneratedAgentChanges:
    """A new specialist (``generated_agent_``) or its removal (``generated_agent_rollback_``)."""

    kind = "agent_definition"

    def owns(self, change_id: str) -> bool:
        return change_id.startswith("generated_agent_")

    def describe(self, agent: Any, change_id: str) -> ChangeProposalV1:
        from custom_agents.generated_agent_provisioning import GeneratedAgentProvisioningService

        return GeneratedAgentProvisioningService(agent).describe(change_id)

    def apply(self, agent: Any, change_id: str, approval: ChangeApproval) -> dict[str, Any]:
        from custom_agents.generated_agent_provisioning import GeneratedAgentProvisioningService

        return GeneratedAgentProvisioningService(agent).apply(change_id, approval)


class TeamSkillInstallChanges:
    kind = "team_skill_install"

    def owns(self, change_id: str) -> bool:
        return change_id.startswith("skill_install_")

    def describe(self, agent: Any, change_id: str) -> ChangeProposalV1:
        from custom_agents.skill_installation import SkillInstallationService

        return SkillInstallationService(agent).describe(change_id)

    def apply(self, agent: Any, change_id: str, approval: ChangeApproval) -> dict[str, Any]:
        from custom_agents.skill_installation import SkillInstallationService

        return SkillInstallationService(agent).apply(change_id, approval)

    async def after_apply(self, agent: Any, result: dict[str, Any]) -> dict[str, Any]:
        return await _after_team_skill_change(agent, result, "installed", "install")


class TeamSkillUninstallChanges:
    kind = "team_skill_uninstall"

    def owns(self, change_id: str) -> bool:
        return change_id.startswith("skill_uninstall_")

    def describe(self, agent: Any, change_id: str) -> ChangeProposalV1:
        from custom_agents.skill_uninstallation import SkillUninstallationService

        return SkillUninstallationService(agent).describe(change_id)

    def apply(self, agent: Any, change_id: str, approval: ChangeApproval) -> dict[str, Any]:
        from custom_agents.skill_uninstallation import SkillUninstallationService

        return SkillUninstallationService(agent).apply(change_id, approval)

    async def after_apply(self, agent: Any, result: dict[str, Any]) -> dict[str, Any]:
        return await _after_team_skill_change(agent, result, "removed", "uninstall")


class HostedMCPServerChanges:
    kind = "hosted_mcp_server"

    def owns(self, change_id: str) -> bool:
        return change_id.startswith("mcp_host_change_")

    def describe(self, agent: Any, change_id: str) -> ChangeProposalV1:
        from custom_agents.mcp_hosting_changes import HostedMCPServerChangeService

        return HostedMCPServerChangeService(agent).describe(change_id)

    def apply(self, agent: Any, change_id: str, approval: ChangeApproval) -> dict[str, Any]:
        from custom_agents.mcp_hosting_changes import HostedMCPServerChangeService

        return HostedMCPServerChangeService(agent).apply(change_id, approval)


class ManagedExecutionServiceChanges:
    """Creating a managed service, or starting, stopping or removing one."""

    kind = "managed_execution_service"

    def owns(self, change_id: str) -> bool:
        return change_id.startswith("exec_svc_change_")

    def describe(self, agent: Any, change_id: str) -> ChangeProposalV1:
        from custom_agents.execution_changes import ManagedServiceChangeService

        return ManagedServiceChangeService(agent).describe(change_id)

    def apply(self, agent: Any, change_id: str, approval: ChangeApproval) -> dict[str, Any]:
        from custom_agents.execution_changes import ManagedServiceChangeService

        return ManagedServiceChangeService(agent).apply(change_id, approval)


class ExecutionSSHInteractionChanges:
    """Typing into one SSH session."""

    kind = "execution_ssh_interaction"

    def owns(self, change_id: str) -> bool:
        return change_id.startswith("ssh_interaction_")

    def describe(self, agent: Any, change_id: str) -> ChangeProposalV1:
        from custom_agents.execution_changes import SSHInteractionChangeService

        return SSHInteractionChangeService(agent).describe(change_id)

    def apply(self, agent: Any, change_id: str, approval: ChangeApproval) -> dict[str, Any]:
        from custom_agents.execution_changes import SSHInteractionChangeService

        return SSHInteractionChangeService(agent).apply(change_id, approval)


class SystemScheduleChanges:
    """Updating, pausing, resuming, turning off or removing a schedule the runtime manages."""

    kind = SYSTEM_SCHEDULE_CHANGE_KIND

    def owns(self, change_id: str) -> bool:
        return change_id.startswith("sys_sched_chg_")

    def describe(self, agent: Any, change_id: str) -> ChangeProposalV1:
        from custom_agents.system_schedule_changes import SystemScheduleChangeService

        return SystemScheduleChangeService(agent).describe(change_id)

    def apply(self, agent: Any, change_id: str, approval: ChangeApproval) -> dict[str, Any]:
        from custom_agents.system_schedule_changes import SystemScheduleChangeService

        return SystemScheduleChangeService(agent).apply(change_id, approval)


class HistoryCleanupChanges:
    """Deleting part of a person's own chat history and finished tasks (``custom_agents.history_cleanup``)."""

    kind = "history_cleanup"

    def owns(self, change_id: str) -> bool:
        return change_id.startswith("history_cleanup_")

    def describe(self, agent: Any, change_id: str) -> ChangeProposalV1:
        from custom_agents.history_cleanup import HistoryCleanupService

        return HistoryCleanupService(agent).describe(change_id)

    def apply(self, agent: Any, change_id: str, approval: ChangeApproval) -> dict[str, Any]:
        from custom_agents.history_cleanup import HistoryCleanupService

        return HistoryCleanupService(agent).apply(change_id, approval)


async def _after_team_skill_change(agent: Any, result: dict[str, Any], verb: str, operation: Any) -> dict[str, Any]:
    """Tell administrators (decision L2) and refresh running agents' skills."""

    from custom_agents.team_skill_changes import notice_team_skill_change, reload_runtime_skills_after_change

    if result.get("error"):
        return result
    notice_team_skill_change(agent, result, verb)
    return await reload_runtime_skills_after_change(agent, result, operation=operation)


CHANGE_ADAPTERS: tuple[ChangeAdapter, ...] = (
    RuntimeSettingsChanges(),
    RuntimeRestartChanges(),
    GeneratedAgentChanges(),
    TeamSkillInstallChanges(),
    TeamSkillUninstallChanges(),
    HostedMCPServerChanges(),
    ManagedExecutionServiceChanges(),
    ExecutionSSHInteractionChanges(),
    SystemScheduleChanges(),
    HistoryCleanupChanges(),
)


def change_adapter(change_id: str) -> ChangeAdapter:
    normalized = str(change_id or "").strip()
    for adapter in CHANGE_ADAPTERS:
        if adapter.owns(normalized):
            return adapter
    raise ValueError(f"{normalized or 'That id'} is not a staged change; use the change_id a plan tool returned.")


__all__ = ["CHANGE_ADAPTERS", "change_adapter"]
