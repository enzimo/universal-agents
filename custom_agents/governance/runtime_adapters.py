"""The audit sink change services record staged and applied changes with."""

from __future__ import annotations

import logging

from custom_agents.governance.authorized_operations import GovernanceAuditEvent


logger = logging.getLogger(__name__)


class LoggingGovernanceAuditSink:
    """Structured, secret-free lifecycle logging for operation diagnostics."""

    def record(self, event: GovernanceAuditEvent) -> None:
        logger.info(
            "event=authorized_operation_lifecycle operation=%s phase=%s "
            "change_id=%s plan_digest=%s task_id=%s authority_mode=%s",
            event.operation,
            event.phase,
            event.change_id,
            event.plan_digest,
            event.task_id,
            event.authority_mode,
        )


