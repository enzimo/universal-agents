"""Whom an after-action report's work was for, and whom its review works for.

A specialist sends an after-action report (AAR) to the agent that delegated the
work, once its task ends. The delegating agent collects the reports for one
parent task in a batch and reviews them later in a scheduled review task
(``task_manager_after_action_reports``,
``scheduled_runtime.prepare_after_action_report_batch_review``). The review
reads the reports and may record lessons, including in the person's own
memory, so it must work for the person whose work produced the reports and
never mix two people's reports.

Before 2026-10-10 the review schedule carried no identity: the review ran as
work nobody verifiably asked for, so with authorization on every tool call of
the review was refused (``resolve_after_action_report_review`` included), and
with authorization off it ran as work for nobody instead of for the person.

Whom the work was for comes only from verified identity
(``task_identity.reported_task_person``), never from the ``canonical_user_id`` a
message names:

- The report message carries the specialist task's meta, and a delegated task
  carries the parent request's signed context (or schedule run identity)
  unchanged. The original signature proves whom the work was for even after
  the context expires or the source schedule ends. It grants no current
  access: the scheduler signs the review afresh and every call is decided.
- While the parent task is still active, its own verified identity must name
  the same person (or nobody). Otherwise the report is about someone else's
  work, and it is not recorded at all (logged): it would put one person's
  findings into another person's task history and review. Once the parent is
  archived only the report's identity is left; the specialist's runtime sets
  the report's task links from its own task, never from the model
  (``UniversalAgent._send_after_action_report_followup``).
- A report whose work nothing verifiably names is not recorded with
  authorization on (its review could call no tool). With authorization off,
  unproven work is work for nobody, as everywhere in that mode
  (``access_runtime.verified_person_id``).

The person (None for work for nobody) is stored on the review and batch
records, and batches are kept per person. The review schedule is a
runtime-managed schedule owned by that person (``review_gateway_context``), so
the scheduler's usual path signs each review run for them with their current
roles (``task_context_runtime.sign_scheduled_context``, or the manager for a
worker process without the key), a disabled or removed person's review does not
run (``task_context_runtime.inactive_owner``), and the schedule is deleted with
their other personal schedules rather than moved to an administrator
(``custom_messages.system_schedules``). A review of work
for nobody has a schedule that names nobody, and the scheduler signs its run
for no person through the same path; no permit for no person is needed
(``system_schedules.is_system_work_kind`` stays as it is), because the
reviewing team lead runs in the manager process, which holds the key.
"""

from __future__ import annotations

import logging
from typing import Any, Optional

from access_control.identity import IdentityRejected
from access_control.task_identity import reported_task_person
from custom_agents.access_runtime import authorization_enabled, runtime_access
from custom_agents.schedule_store import owner_canonical_user_id_for_schedule
from custom_agents.task_records import ReportedWorkIdentity
from custom_messages.contracts.routing_v1 import GatewayContext
from custom_messages.contracts.task_v1 import AfterActionReport
from custom_messages.records.reviews import AfterActionReportReviewBatchRecord

logger = logging.getLogger(__name__)

# Stands for "nothing verifiably names whom the work was for" when comparing identities.
_UNVERIFIED = object()


def _verified_person(agent: Any, meta: Any) -> object:
    """The verified person of a task's meta, None for work for nobody, or ``_UNVERIFIED``."""

    gateway_context = getattr(meta, "gateway_context", None)
    try:
        access = runtime_access(agent)
        return reported_task_person(
            verifier=access.verifier,
            team_id=access.team_id,
            task_context=getattr(gateway_context, "task_context", None),
            schedule_run=getattr(gateway_context, "schedule_run", None),
        )
    except (IdentityRejected, ValueError, RuntimeError):
        return _UNVERIFIED


def _describe(person: object) -> str:
    if person is _UNVERIFIED:
        return "unverified"
    return str(person) if person is not None else "nobody"


def reported_work_identity(
    agent: Any,
    *,
    report: AfterActionReport,
    report_meta: Any,
    parent_meta: Any,
) -> Optional[ReportedWorkIdentity]:
    """Whom the reported work was for (module docstring), or None when the report must not be recorded (logged).

    ``report_meta`` is the report message's meta; ``parent_meta`` is the
    parent task's meta while it is active, None once it is archived. Reads
    storage for schedule run identities, so async callers run it in a thread.
    """

    reported = _verified_person(agent, report_meta)
    if parent_meta is not None:
        parent = _verified_person(agent, parent_meta)
        if parent != reported:  # _UNVERIFIED equals only itself
            logger.warning(
                "An after-action report is not about its parent task's work: they work for different people. "
                "The report is not recorded. event=after_action_report_person_mismatch agent_id=%s report_id=%s "
                "parent_task_id=%s source_task_id=%s report_person=%s parent_person=%s",
                getattr(agent, "id", None),
                report.report_id,
                report.parent_task_id,
                report.source_task_id,
                _describe(reported),
                _describe(parent),
            )
            return None
    if reported is not _UNVERIFIED:
        return ReportedWorkIdentity(person_id=reported if isinstance(reported, str) else None)
    if authorization_enabled(getattr(agent, "authorization_config", None)):
        logger.warning(
            "Nothing verifiably says whose work an after-action report describes, so it is not recorded; "
            "its review could call no tool. event=after_action_report_unverified agent_id=%s report_id=%s "
            "parent_task_id=%s source_task_id=%s",
            getattr(agent, "id", None),
            report.report_id,
            report.parent_task_id,
            report.source_task_id,
        )
        return None
    logger.info(
        "Nothing verifiably says whose work an after-action report describes; with authorization off it is "
        "reviewed as work for nobody. event=after_action_report_unverified_work_for_nobody agent_id=%s "
        "report_id=%s parent_task_id=%s",
        getattr(agent, "id", None),
        report.report_id,
        report.parent_task_id,
    )
    return ReportedWorkIdentity(person_id=None)


def review_gateway_context(batch: AfterActionReportReviewBatchRecord) -> Optional[GatewayContext]:
    """The review schedule's gateway context: its owner is the batch's person (none for work for nobody).

    Trusted runtime code sets the owner of a runtime-managed schedule, so the
    scheduler signs each run for that person with their current roles
    (``task_context_runtime``). None for a batch saved before the person was
    recorded: no review may run for it.
    """

    if not batch.identity_verified:
        return None
    return GatewayContext(canonical_user_id=batch.person_id)


def review_runs_for_batch_person(
    batch: AfterActionReportReviewBatchRecord, scheduled_message: Any, *, schedule_id: str
) -> bool:
    """Whether the review schedule's owner (from its row) is the batch's person; logged when not.

    The schedule is made for the batch's person and its owner never changes
    (departure deletes it), so a difference means the run would work for
    someone the reports are not about; it does not run.
    """

    owner = owner_canonical_user_id_for_schedule(scheduled_message)
    if batch.identity_verified and owner == batch.person_id:
        return True
    logger.error(
        "An after-action review schedule's owner is not the person the reports are about; the review does not "
        "run. event=after_action_review_owner_mismatch schedule_id=%s batch_id=%s schedule_owner=%s "
        "batch_person=%s",
        schedule_id,
        batch.batch_id,
        owner or "nobody",
        batch.person_id or "nobody",
    )
    return False


__all__ = ["reported_work_identity", "review_gateway_context", "review_runs_for_batch_person"]
