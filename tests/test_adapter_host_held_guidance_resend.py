"""Guidance held across the adapter-host files upgrade keeps its file when the gateway sends it again.

The gateway stores held guidance as the ``TaskSteering`` it will send
(``assistant_gateway.application.thread_guidance``: conversation id and
idempotency key set when it holds it), then at each send adds the target
fields and signs a fresh context whose id is computed from that stored copy.
The team lead keeps a guidance file only when it opens inside
``state/uploads`` and the signed context id matches the message
(``access_control.ownership.sent_files_access``). So the upgrade step
(``instance_upgrades.adapter_host_files``) must point the stored copy at the
moved file: these tests send the copy through the lead's real steering
handler, after the step and, as the baseline, without it.

The step's own cases (which copies follow, which stay, the warnings) are in
``tests/test_adapter_host_files_upgrade.py``.
"""

# ruff: noqa: F811 -- imported pytest fixtures are injected by name

from __future__ import annotations

import logging
from pathlib import Path

import pytest

from custom_agents.conversation_attachments import TaskAttachments
from custom_messages.contracts.task_v1 import TaskSteering
from custom_messages.enums import TaskSteeringStatus, TaskSteeringType
from tests.test_adapter_host_files_upgrade import _held_copy, _hold, _Instance, _turn_record, instance  # noqa: F401
from tests.test_conversation_attachments_regression import CONVERSATION as LEAD_CONVERSATION
from tests.test_conversation_attachments_regression import _admit, _lead
from tests.test_turn_files_recording import (  # noqa: F401  (autouse fixtures and statuses)
    TASK,
    _messages,
    _no_background_work,
    _send,
    _upload,
    _verify_keys,
    statuses,
)

pytestmark = pytest.mark.integration

HELD = "guide_photo"


def _hold_as_the_gateway_does(instance: _Instance) -> None:
    """Guidance for the lead's running request, naming the photo by its 0.9.13 record (transfer folder path)."""

    steering = TaskSteering(
        steering_type=TaskSteeringType.PROACTIVE_GUIDANCE,
        conversation_id=LEAD_CONVERSATION,
        guidance_text="Use this photo too.",
        idempotency_key=HELD,
        attachments=[_turn_record(instance, "att_photo")],
    )
    _hold(instance, HELD, raw=steering.model_dump_json())


async def _send_the_held_copy(tmp_path: Path, instance: _Instance) -> list[str]:
    """The gateway sends the stored copy to the lead's request; the files the task keeps are returned."""

    lead = _lead(tmp_path / "lead", instance.layout)
    await _admit(lead, task_id=TASK, text="Read this.", attachments=[_upload(instance.layout, "page one.png")])
    stored = TaskSteering.model_validate_json(_held_copy(instance, HELD))
    # thread_guidance._envelope: only the target changes; _send signs the context from this copy.
    await _send(lead, stored.model_copy(update={"target_task_id": TASK}))
    kept = TaskAttachments.load(lead.task_manager.get_task_info(TASK), None)
    return [attachment.attachment_id for attachment in kept.own]


@pytest.mark.asyncio
@pytest.mark.parametrize("instance", ["sqlite"], indirect=True)
async def test_held_guidance_sent_after_the_step_keeps_its_moved_file(
    tmp_path: Path, instance: _Instance, statuses, caplog: pytest.LogCaptureFixture
) -> None:
    (tmp_path / "lead").mkdir()
    _hold_as_the_gateway_does(instance)

    result = instance.run()
    with caplog.at_level(logging.INFO):
        own = await _send_the_held_copy(tmp_path, instance)

    assert result.finished and result.counts.moved == 1
    assert [status for _id, status, _reason in statuses] == [TaskSteeringStatus.ACCEPTED]
    assert "att_photo" in own
    assert _messages(caplog, "attachment_outside_upload_folders") == []


@pytest.mark.asyncio
@pytest.mark.parametrize("instance", ["sqlite"], indirect=True)
async def test_held_guidance_sent_without_the_step_loses_its_file(
    tmp_path: Path, instance: _Instance, statuses, caplog: pytest.LogCaptureFixture
) -> None:
    """The baseline: the same copy still naming the transfer folder is accepted, but its file is not kept."""

    (tmp_path / "lead").mkdir()
    _hold_as_the_gateway_does(instance)

    with caplog.at_level(logging.INFO):
        own = await _send_the_held_copy(tmp_path, instance)

    assert [status for _id, status, _reason in statuses] == [TaskSteeringStatus.ACCEPTED]
    assert "att_photo" not in own
    assert _messages(caplog, "attachment_outside_upload_folders")
