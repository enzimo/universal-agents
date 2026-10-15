from __future__ import annotations

from custom_agents.task_operation_runtime import TaskOperationRuntime
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from custom_messages.contracts.attachments_v1 import (
    AttachmentStorageRef,
    UserTaskAttachment,
)
from custom_messages.contracts.task_v1 import Task
from custom_messages.enums import MessageMetaType
from custom_tools.runtime_context import _agent_ctx, set_agent_ref
from custom_tools.user_attachment_tools import inspect_user_attachment
from storage.config import resolve_storage_settings
from tests.broker_access_support import context_signer
from tests.change_approval_support import use_signed_contexts


def _attachment(
    path: Path, *, content_type: str = "text/plain", kind: str = "document"
) -> UserTaskAttachment:
    data = path.read_bytes() if path.exists() else b""
    return UserTaskAttachment(
        attachment_id="att_test",
        filename=path.name,
        content_type=content_type,
        kind=kind,  # type: ignore[arg-type]
        size_bytes=len(data),
        sha256="0" * 64,
        storage_refs=[AttachmentStorageRef(storage_type="local", path=str(path))],
    )


def _payload(result: dict) -> dict:
    return json.loads(result["content"][0]["text"])


ATTACHMENT_TEAM = "team_attachments"


@pytest.fixture(autouse=True)
def _signed_task_contexts(monkeypatch: pytest.MonkeyPatch) -> None:
    use_signed_contexts(monkeypatch)


def _set_person_agent(attachment: UserTaskAttachment, layout, person: str) -> None:
    """An agent whose task works for ``person`` inside an instance layout."""

    task = Task(
        task_id="task_1",
        task_type=MessageMetaType.REQUEST,
        problem_statement="Process the attachment.",
        attachments=[attachment],
    )
    # The person comes only from the gateway-signed task context.
    signed = context_signer().issue(team_id=ATTACHMENT_TEAM, person_id=person, person_roles=("member",))
    meta = SimpleNamespace(gateway_context=SimpleNamespace(canonical_user_id=person, task_context=signed))
    task_manager = SimpleNamespace(
        operations_runtime=TaskOperationRuntime(),
        current_task_id="task_1",
        get_task_info=lambda task_id: (
            SimpleNamespace(task=task, meta=meta) if task_id == "task_1" else None
        ),
        storage_settings=resolve_storage_settings(
            {"backend": "sqlite", "sqlite": {"path": str(layout.root / "manager.db")}}
        ),
    )
    set_agent_ref(
        SimpleNamespace(
            team_id=ATTACHMENT_TEAM,
            authorization_config={"enabled": False},
            task_manager=task_manager,
            instance_root=str(layout.root),
            workspace_dir=str(layout.workspaces_dir),
        )
    )


def test_upload_is_copied_into_the_persons_folder_for_processing(tmp_path: Path) -> None:
    from runtime_settings.instance_layout import InstanceLayout

    layout = InstanceLayout.at(tmp_path / "instance")
    layout.ensure_directories()
    upload = layout.uploads_dir / "session_1" / "report.csv"
    upload.parent.mkdir(parents=True)
    upload.write_text("a,b\n1,2\n", encoding="utf-8")
    _set_person_agent(_attachment(upload), layout, "alice")
    try:
        copied = _payload(
            inspect_user_attachment._tool_func(attachment_id="att_test", copy_to_workspace=True)
        )
        by_path = inspect_user_attachment._tool_func(path=str(upload))
    finally:
        _agent_ctx.set(None)

    target = layout.person_dir("alice") / "uploads" / "att_test_report.csv"
    assert copied["workspace_path"] == str(target)
    assert target.read_text(encoding="utf-8") == "a,b\n1,2\n"
    # Shell-reachable tools cannot open the upload store itself by path.
    assert by_path["status"] == "error"
    assert "closed to agent tools" in _payload(by_path)["error"]


def test_copy_to_workspace_needs_an_attachment_id(tmp_path: Path) -> None:
    from runtime_settings.instance_layout import InstanceLayout

    layout = InstanceLayout.at(tmp_path / "instance")
    layout.ensure_directories()
    note = layout.person_dir("alice") / "note.txt"
    note.parent.mkdir(parents=True)
    note.write_text("hello", encoding="utf-8")
    _set_person_agent(_attachment(note), layout, "alice")
    try:
        result = inspect_user_attachment._tool_func(path="note.txt", copy_to_workspace=True)
    finally:
        _agent_ctx.set(None)

    assert result["status"] == "error"
    assert "needs an attachment_id" in _payload(result)["error"]
