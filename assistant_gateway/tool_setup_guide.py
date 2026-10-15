"""What the gateway's setup pages need from the catalog and the workflow store.

``GatewayToolSetups`` (``assistant_gateway.application.tool_setup``) shows a
tool's external setup step in the catalog's words, never words from the
message that asked for it, and links the first setup page of a task to the
task's access workflow page. This adapter reads both from trusted stores:
the tool catalog (``CatalogSource``, which includes the instance's approved
tools) and the manager's workflow table (``AccessWorkflowRepository``). It
is composed by ``GatewayAccessInbox`` when the gateway hands it the setup
service.
"""

from __future__ import annotations

import logging
from typing import Any

from urllib.parse import urljoin

from custom_messages.contracts.tool_setup_spec_v1 import ExternalSetupStepV1
from storage.access_workflow_repository import AccessWorkflowRepository

logger = logging.getLogger(__name__)


class GatewaySetupGuide:
    """External step words from the catalog, and a task's workflow page address."""

    def __init__(self, *, catalogs: Any, storage_settings: Any, team_id: str, site_url: str = "") -> None:
        self._catalogs = catalogs
        # The web chat's address (absolute when the instance has a public origin),
        # so a workflow link opens from any channel.
        self._site_url = site_url
        self._storage_settings = storage_settings
        self._team_id = team_id
        self._workflows: AccessWorkflowRepository | None = None

    def external_step(self, tool_id: str, step_id: str) -> tuple[str, ExternalSetupStepV1] | None:
        """The tool's display name and the step, or None when the catalog has no such step."""

        tool = self._catalogs.current().tools.get(tool_id)
        step = tool.setup.step(step_id) if tool is not None and tool.setup else None
        return (tool.display_name, step) if step is not None else None

    def workflow_url(self, task_id: str) -> str | None:
        """The workflow page of the task's access workflow, if it has one (logged on failure)."""

        try:
            if self._workflows is None:
                self._workflows = AccessWorkflowRepository(self._storage_settings)
            records = self._workflows.for_task(task_id=task_id, team_id=self._team_id)
        except Exception as exc:  # noqa: BLE001 - the page opens without the link
            logger.warning(
                "Could not read the task's workflow for a setup page. event=gateway_setup_workflow_read_failed "
                "task_id=%s error=%s",
                task_id,
                type(exc).__name__,
            )
            return None
        return urljoin(self._site_url or "/", f"/workflows/{records[0].workflow_id}") if records else None


__all__ = ["GatewaySetupGuide"]
