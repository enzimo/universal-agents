from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

import pytest
from strands.models.litellm import LiteLLMModel

from custom_tools.local_media_context_tool import load_local_media_for_model_context
from custom_tools.runtime_context import _agent_ctx
from tests.task_work_support import set_agent_ref
from runtime_settings.instance_layout import InstanceLayout


@pytest.fixture(autouse=True)
def reset_agent_context():
    _agent_ctx.set(None)
    yield
    _agent_ctx.set(None)


@pytest.fixture
def workspace(tmp_path: Path, monkeypatch) -> Path:
    """The default folder of a task that works for no person.

    The instance sits in ``tmp_path/instance``; agent tools may only open the
    task's folders under its ``workspaces/`` area.
    """

    layout = InstanceLayout.at(tmp_path / "instance")
    layout.ensure_directories()
    monkeypatch.setenv("AGENT_WORKSPACE_DIR", str(layout.workspaces_dir))
    monkeypatch.setenv("TEAM_INSTANCE_ROOT", str(layout.root))
    return layout.agents_dir


def _metadata(result: dict) -> dict:
    return json.loads(result["content"][0]["text"])


def test_load_local_media_for_model_context_returns_image_block(
    workspace: Path,
) -> None:
    image_path = workspace / "screenshot.png"
    image_bytes = b"\x89PNG\r\n\x1a\nimage-bytes"
    image_path.write_bytes(image_bytes)

    result = load_local_media_for_model_context._tool_func(
        path="screenshot.png",
        purpose="Inspect the screenshot.",
    )

    metadata = _metadata(result)
    assert result["status"] == "success"
    assert metadata["loaded_into_model_context"] is True
    assert metadata["media_kind"] == "image"
    assert metadata["purpose"] == "Inspect the screenshot."
    assert result["content"][1] == {
        "image": {
            "format": "png",
            "source": {"bytes": image_bytes},
        }
    }


def test_load_local_media_for_model_context_returns_document_block(
    workspace: Path,
) -> None:
    doc_path = workspace / "Quarterly Report.pdf"
    doc_bytes = b"%PDF-1.4\nfake-pdf"
    doc_path.write_bytes(doc_bytes)

    result = load_local_media_for_model_context._tool_func(path=str(doc_path))

    metadata = _metadata(result)
    assert result["status"] == "success"
    assert metadata["media_kind"] == "document"
    assert result["content"][1] == {
        "document": {
            "format": "pdf",
            "name": "Quarterly_Report",
            "source": {"bytes": doc_bytes},
        }
    }


def test_load_local_media_for_model_context_gates_documents_by_active_profile(
    workspace: Path,
) -> None:
    doc_path = workspace / "notes.md"
    doc_path.write_text("# Notes", encoding="utf-8")
    set_agent_ref(
        SimpleNamespace(
            authorization_config={"enabled": False},
            model_provider="litellm",
            model_id="vision-only",
            supports_image_input=True,
            supports_file_input=None,
            supports_audio_input=False,
        )
    )
    try:
        result = load_local_media_for_model_context._tool_func(path=str(doc_path))
    finally:
        _agent_ctx.set(None)

    assert result["status"] == "error"
    assert _metadata(result)["capability_field"] == "supports_file_input"


def test_load_local_media_for_model_context_rejects_audio_with_explicit_guidance(
    workspace: Path,
) -> None:
    audio_path = workspace / "clip.mp3"
    audio_path.write_bytes(b"ID3")

    result = load_local_media_for_model_context._tool_func(path="clip.mp3")

    payload = _metadata(result)
    assert result["status"] == "error"
    assert payload["media_kind"] == "audio"
    assert "cannot currently be inserted" in payload["error"]
    assert "transcription" in payload["recommended_next_step"]


def test_litellm_formatter_preserves_image_tool_result_as_followup_user_media() -> None:
    formatted = LiteLLMModel.format_request_messages(
        [
            {
                "role": "assistant",
                "content": [
                    {
                        "toolUse": {
                            "toolUseId": "tool_1",
                            "name": "load_local_media_for_model_context",
                            "input": {"path": "screenshot.png"},
                        }
                    }
                ],
            },
            {
                "role": "user",
                "content": [
                    {
                        "toolResult": {
                            "toolUseId": "tool_1",
                            "status": "success",
                            "content": [
                                {"text": "Loaded screenshot.png into model context."},
                                {
                                    "image": {
                                        "format": "png",
                                        "source": {"bytes": b"abc"},
                                    }
                                },
                            ],
                        }
                    }
                ],
            },
        ]
    )

    assert formatted[0]["role"] == "assistant"
    assert (
        formatted[0]["tool_calls"][0]["function"]["name"]
        == "load_local_media_for_model_context"
    )
    assert formatted[1]["role"] == "tool"
    assert formatted[1]["tool_call_id"] == "tool_1"
    assert formatted[2] == {
        "role": "user",
        "content": [
            {
                "image_url": {
                    "detail": "auto",
                    "format": "image/png",
                    "url": "data:image/png;base64,YWJj",
                },
                "type": "image_url",
            }
        ],
    }
