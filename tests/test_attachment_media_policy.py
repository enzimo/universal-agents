"""Shared attachment media-type allowlist and kind derivation tests (C2)."""

from __future__ import annotations

import hashlib
import json
import logging
import shutil
import subprocess
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from assistant_gateway.application.gateway_application import GatewayApplicationResponse
from assistant_gateway.application.request_context import GatewayRequestRoute
from assistant_gateway.application.transcript_service import GatewayTranscriptService
from assistant_gateway.attachment_media import (
    ATTACHMENT_SANDBOX_CSP,
    DEFAULT_ATTACHMENT_CONTENT_TYPE,
    INLINE_IMAGE_CONTENT_TYPES,
    attachment_disposition_for_content_type,
    attachment_kind_for_content_type,
    attachment_response_content_type,
    is_inline_attachment_content_type,
    normalize_media_type,
    parse_media_type,
    resolve_attachment_content_type,
)
from assistant_gateway.attachments import validate_user_notification_attachments
from assistant_gateway.config import GatewayInstanceConfig
from assistant_gateway.config_delivery_attachments import (
    ConfigGatewayDeliveryAttachmentResolver,
)
from assistant_gateway.store import GatewayStateStore
from custom_agents.task_operation_runtime import TaskOperationRuntime
from custom_messages.contracts.delivery_v1 import UserNotification, UserNotificationAttachment
from custom_messages.contracts.routing_v1 import GatewayContext, MessageMeta
from custom_tools.providers.communication import notify_user
from custom_tools.runtime_context import _agent_ctx, get_agent_ref
from id_generation import new_id
from runtime_settings.instance_layout import InstanceLayout
from storage.config import resolve_storage_settings
from tests.pending_work_support import call_tool_on_agent_loop
from tests.task_work_support import set_agent_ref


class _TestIds:
    def new_transcript_message_id(self) -> str:
        return new_id("msg")

    def new_transcript_attachment_id(self) -> str:
        return new_id("att")


def _write_instance_config(tmp_path: Path) -> Path:
    instance_root = tmp_path / "instance"
    config = GatewayInstanceConfig.create(
        instance_name="test-instance",
        instance_root=str(instance_root),
        team_id="test_team",
        timezone="UTC",
    )
    config.delivery.default_target.channel_name = "website"
    config.delivery.default_target.chat_id = "web-chat"
    config.user.canonical_user_id = "alice"
    config.user.default_session_key = "website:web-chat:default"
    config.manager.manager_agent_id = "manager_1"
    return config.save(instance_root / "instance_config.yaml")


def _fake_agent(config_path: Path, *, team_comms: SimpleNamespace) -> SimpleNamespace:
    active_meta = MessageMeta(
        conversation_id="conv_1",
        msg_correlation_id="corr_1",
        request_timezone="UTC",
        gateway_context=GatewayContext(
            session_key="website:web-chat:default",
            channel_name="website",
            chat_id="web-chat",
        ),
    )
    task_manager = SimpleNamespace(
        operations_runtime=TaskOperationRuntime(),
        current_task_id="task_1",
        get_task_info=lambda task_id: (
            SimpleNamespace(meta=active_meta) if task_id == "task_1" else None
        ),
        append_history_entry=MagicMock(),
    )
    task_manager.storage_settings = resolve_storage_settings(
        {"backend": "sqlite", "sqlite": {"path": str(config_path.parent / "task_history.db")}}
    )
    return SimpleNamespace(
        id="agent_1",
        name="TeamLead",
        named_agent_type="TeamLead",
        team_id="test_team",
        authorization_config={"enabled": False},
        memory_config={},
        instance_config_path=str(config_path),
        instance_root=str(config_path.parent),
        workspace_dir=str(config_path.parent / "workspaces"),
        task_manager=task_manager,
        team_comms=team_comms,
        test_task_id="task_1",
    )


def _layout(tmp_path: Path) -> InstanceLayout:
    layout = InstanceLayout.at(tmp_path / "instance")
    layout.ensure_directories()
    layout.person_dir("alice").mkdir(parents=True, exist_ok=True)
    return layout


@pytest.mark.parametrize(
    ("filename", "payload_bytes", "content_type"),
    [
        ("payload.html", b"<script>alert(1)</script>", "text/html"),
        ("diagram.svg", b"<svg onload=alert(1)></svg>", "image/svg+xml"),
    ],
)
def test_notify_user_rejects_non_raster_attachment_with_forged_image_kind(
    tmp_path: Path,
    filename: str,
    payload_bytes: bytes,
    content_type: str,
) -> None:
    config_path = _write_instance_config(tmp_path)
    config = GatewayInstanceConfig.load(config_path)
    target_file = config.layout.team_dir / filename
    target_file.parent.mkdir(parents=True, exist_ok=True)
    target_file.write_bytes(payload_bytes)
    team_comms = SimpleNamespace(send_message=AsyncMock())
    agent = _fake_agent(config_path, team_comms=team_comms)
    set_agent_ref(agent)
    try:
        result = call_tool_on_agent_loop(
            get_agent_ref(),
            notify_user._tool_func,
            "See attached file.",
            attachments=[
                {
                    "path": str(target_file),
                    "filename": filename,
                    "content_type": content_type,
                    "kind": "image",
                }
            ],
        )
    finally:
        _agent_ctx.set(None)

    assert result["status"] == "error"
    assert any("does not match content type" in err for err in result["attachment_errors"])
    team_comms.send_message.assert_not_awaited()


def test_validate_notification_attachment_suffix_never_overrides_explicit_content_type(
    tmp_path: Path,
) -> None:
    layout = _layout(tmp_path)
    spoofed = layout.person_dir("alice") / "spoofed.png"
    spoofed.write_bytes(b"<html><script>alert(1)</script></html>")
    config = SimpleNamespace(workspace_path=layout.workspaces_dir)

    accepted, errors = validate_user_notification_attachments(
        [{"path": "spoofed.png", "content_type": "text/html; charset=utf-8"}],
        config=config,
        person_id="alice",
    )

    assert errors == []
    assert len(accepted) == 1
    assert accepted[0].content_type == "text/html"
    assert accepted[0].kind == "document"


def test_validate_notification_attachment_svg_defaults_to_document_kind(
    tmp_path: Path,
) -> None:
    layout = _layout(tmp_path)
    svg = layout.person_dir("alice") / "chart.svg"
    svg.write_bytes(b"<svg xmlns='http://www.w3.org/2000/svg'></svg>")
    config = SimpleNamespace(workspace_path=layout.workspaces_dir)

    accepted, errors = validate_user_notification_attachments(
        [{"path": "chart.svg"}],
        config=config,
        person_id="alice",
    )

    assert errors == []
    assert len(accepted) == 1
    assert accepted[0].content_type == "image/svg+xml"
    assert accepted[0].kind == "document"


def test_validate_notification_attachment_flags_mismatched_kind_for_ambiguous_audio(
    tmp_path: Path,
) -> None:
    layout = _layout(tmp_path)
    voice = layout.person_dir("alice") / "recording.voicememo"
    voice.write_bytes(b"voice-bytes")
    config = SimpleNamespace(workspace_path=layout.workspaces_dir)

    accepted, errors = validate_user_notification_attachments(
        [{"path": "recording.voicememo", "kind": "audio"}],
        config=config,
        person_id="alice",
    )

    assert accepted == []
    assert len(errors) == 1
    assert "does not match content type" in errors[0]
    assert "content_type" in errors[0]

    accepted_explicit, errors_explicit = validate_user_notification_attachments(
        [{"path": "recording.voicememo", "content_type": "audio/x-caf", "kind": "audio"}],
        config=config,
        person_id="alice",
    )
    assert errors_explicit == []
    assert len(accepted_explicit) == 1
    assert accepted_explicit[0].content_type == "audio/x-caf"
    assert accepted_explicit[0].kind == "audio"


@pytest.mark.parametrize(
    "bad_content_type",
    ["png", "image", "image/png, image/jpeg", "image/png\r\nX-Injected: 1"],
)
@pytest.mark.parametrize("kind", [None, "image"])
def test_validate_notification_attachment_rejects_malformed_content_type(
    tmp_path: Path,
    bad_content_type: str,
    kind: str | None,
) -> None:
    layout = _layout(tmp_path)
    photo = layout.person_dir("alice") / "photo.png"
    photo.write_bytes(b"\x89PNG\r\n\x1a\n")
    config = SimpleNamespace(workspace_path=layout.workspaces_dir)
    payload: dict[str, object] = {"path": "photo.png", "content_type": bad_content_type}
    if kind is not None:
        payload["kind"] = kind

    accepted, errors = validate_user_notification_attachments(
        [payload],
        config=config,
        person_id="alice",
    )

    assert accepted == []
    assert len(errors) == 1
    assert "is not a media type" in errors[0]
    assert repr(bad_content_type) in errors[0]
    assert "application/octet-stream" not in errors[0]


def test_validate_notification_attachment_heic_mismatch_points_to_omitting_kind(
    tmp_path: Path,
) -> None:
    layout = _layout(tmp_path)
    photo = layout.person_dir("alice") / "photo.heic"
    photo.write_bytes(b"heic-bytes")
    config = SimpleNamespace(workspace_path=layout.workspaces_dir)

    accepted_auto, errors_auto = validate_user_notification_attachments(
        [{"path": "photo.heic"}],
        config=config,
        person_id="alice",
    )
    assert errors_auto == []
    assert len(accepted_auto) == 1
    assert accepted_auto[0].content_type == "image/heic"
    assert accepted_auto[0].kind == "document"

    accepted, errors = validate_user_notification_attachments(
        [{"path": "photo.heic", "content_type": "image/heic", "kind": "image"}],
        config=config,
        person_id="alice",
    )
    assert accepted == []
    assert len(errors) == 1
    assert "does not match content type 'image/heic'" in errors[0]
    assert "kind 'document'" in errors[0]
    assert "image/gif, image/jpeg, image/png, image/webp" in errors[0]
    assert "leave kind out" in errors[0]
    assert "set content_type" not in errors[0]


@pytest.mark.parametrize("requested", ["audio", "video"])
def test_validate_notification_attachment_audio_video_mismatch_points_to_content_type(
    tmp_path: Path,
    requested: str,
) -> None:
    layout = _layout(tmp_path)
    clip = layout.person_dir("alice") / "voice.webm"
    clip.write_bytes(b"webm-bytes")
    config = SimpleNamespace(workspace_path=layout.workspaces_dir)
    # Guessed from the suffix: .webm is video/webm, so kind audio disagrees.
    content_type = None if requested == "audio" else "audio/webm"
    payload: dict[str, object] = {"path": "voice.webm", "kind": requested}
    if content_type:
        payload["content_type"] = content_type
    derived = "video" if requested == "audio" else "audio"

    accepted, errors = validate_user_notification_attachments(
        [payload], config=config, person_id="alice"
    )

    assert accepted == []
    assert len(errors) == 1
    assert f"set content_type to the file's {requested} media type" in errors[0]
    assert f"'{requested}/webm'" in errors[0]
    assert f"leave kind out to send it as kind {derived!r}" in errors[0]


def test_delivery_resolver_delivers_valid_files_and_skips_old_format_ones(
    tmp_path: Path,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Old-code shapes queued at upgrade are dropped with a warning; the rest still go."""

    config = GatewayInstanceConfig.load(_write_instance_config(tmp_path))
    person_dir = config.layout.person_dir("alice")
    person_dir.mkdir(parents=True, exist_ok=True)
    (person_dir / "photo.png").write_bytes(b"\x89PNG\r\n\x1a\nphoto")
    (person_dir / "chart.svg").write_bytes(b"<svg xmlns='http://www.w3.org/2000/svg'></svg>")
    (person_dir / "scan.png").write_bytes(b"\x89PNG\r\n\x1a\nscan")
    (person_dir / "shot.png").write_bytes(b"\x89PNG\r\n\x1a\nshot")
    # name -> (old attachment fields, expected reason in the skip warning)
    old_shapes = {
        # Old code labelled every image/* kind image.
        "chart.svg": (
            {"content_type": "image/svg+xml", "kind": "image"},
            "kind 'image' does not match content type 'image/svg+xml'",
        ),
        # Old code kept an explicit kind as given.
        "scan.png": (
            {"content_type": "image/png", "kind": "document"},
            "kind 'document' does not match content type 'image/png'",
        ),
        # Old code passed content_type through unparsed.
        "shot.png": (
            {"content_type": "png", "kind": "image"},
            "content_type 'png' is not a media type",
        ),
    }
    notification = UserNotification(
        text="Files attached.",
        attachments=[
            UserNotificationAttachment(
                path=str(person_dir / "photo.png"), content_type="image/png", kind="image"
            ),
            *(
                UserNotificationAttachment(path=str(person_dir / name), **fields)
                for name, (fields, _reason) in old_shapes.items()
            ),
        ],
    )
    logger = logging.getLogger("tests.delivery_resolver")
    resolver = ConfigGatewayDeliveryAttachmentResolver(config=config, logger=logger)

    with caplog.at_level(logging.WARNING, logger=logger.name):
        staged = resolver.resolve(notification, person_id="alice")

    assert [item.filename for item in staged] == ["photo.png"]
    assert staged[0].kind == "image"
    assert staged[0].sha256
    assert "/scratch/sent/" in staged[0].path
    skipped = [
        record.getMessage()
        for record in caplog.records
        if "event=outgoing_attachment_skipped" in record.getMessage()
    ]
    assert len(skipped) == len(old_shapes)
    for index, (_fields, reason) in enumerate(old_shapes.values(), start=1):
        assert f"attachments[{index}].{reason}" in skipped[index - 1]
    assert all("person_id=alice" in message for message in skipped)


def test_validate_notification_attachment_normalizes_image_jpg_to_jpeg(
    tmp_path: Path,
) -> None:
    layout = _layout(tmp_path)
    photo = layout.person_dir("alice") / "photo.jpg"
    photo.write_bytes(b"\xff\xd8\xff\xd9")
    config = SimpleNamespace(workspace_path=layout.workspaces_dir)

    accepted, errors = validate_user_notification_attachments(
        [{"path": "photo.jpg", "content_type": "IMAGE/JPG; charset=binary", "kind": "image"}],
        config=config,
        person_id="alice",
    )

    assert errors == []
    assert len(accepted) == 1
    assert accepted[0].content_type == "image/jpeg"
    assert accepted[0].kind == "image"


def test_transcript_service_derives_notification_attachment_kind_from_media_type(
    tmp_path: Path,
) -> None:
    store = GatewayStateStore(str(tmp_path / "gateway.db"))
    session = store.create_session(
        session_key="website:web-chat:s1",
        conversation_id="conv_1",
        manager_agent_id="manager_1",
        channel_name="website",
        user_id="alice",
        chat_id="web-chat",
        owner_canonical_user_id="alice",
    )
    service = GatewayTranscriptService(
        repository=store.transcript_repository,
        unit_of_work_factory=store,
        session_repository=store.session_repository,
        id_generator=_TestIds(),
    )
    route = GatewayRequestRoute(
        channel_name="website",
        user_id="alice",
        chat_id="web-chat",
        canonical_user_id="alice",
        original_message_id="req_1",
    )
    html_file = tmp_path / "spoof.html"
    html_file.write_bytes(b"<script>alert(1)</script>")
    svg_file = tmp_path / "vector.svg"
    svg_file.write_bytes(b"<svg onload=alert(1)></svg>")
    png_file = tmp_path / "photo.png"
    png_file.write_bytes(b"\x89PNG\r\n\x1a\n")

    response = GatewayApplicationResponse(
        text="Files attached.",
        conversation_id="conv_1",
        correlation_id="corr_1",
        message_type="user_notification",
        attachments=(
            UserNotificationAttachment(
                path=str(html_file),
                filename="spoof.html",
                content_type="text/html; charset=utf-8",
                kind="image",
                size_bytes=html_file.stat().st_size,
                sha256=hashlib.sha256(html_file.read_bytes()).hexdigest(),
            ),
            UserNotificationAttachment(
                path=str(svg_file),
                filename="vector.svg",
                content_type="image/svg+xml",
                kind="image",
                size_bytes=svg_file.stat().st_size,
                sha256=hashlib.sha256(svg_file.read_bytes()).hexdigest(),
            ),
            UserNotificationAttachment(
                path=str(png_file),
                filename="photo.png",
                content_type="image/png",
                kind="document",
                size_bytes=png_file.stat().st_size,
                sha256=hashlib.sha256(png_file.read_bytes()).hexdigest(),
            ),
        ),
    )

    accepted = service.accept_user_message(
        session=session,
        route=route,
        conversation_id="conv_1",
        text="Send the files",
        attachments=(),
    )
    assert accepted is not None

    projected = service.append_gateway_response(
        session=session,
        route=route,
        response=response,
        reply_to=accepted,
    )

    assert projected is not None
    by_name = {ref.filename: ref for ref in projected.attachment_references}
    assert by_name["spoof.html"].content_type == "text/html"
    assert by_name["spoof.html"].kind == "document"
    assert by_name["vector.svg"].content_type == "image/svg+xml"
    assert by_name["vector.svg"].kind == "document"
    assert by_name["photo.png"].content_type == "image/png"
    assert by_name["photo.png"].kind == "image"


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("IMAGE/PNG; charset=utf-8", "image/png"),
        ("image/jpg", "image/jpeg"),
        ("Image/JPG; quality=90", "image/jpeg"),
        ("  audio/mpeg  ", "audio/mpeg"),
        ("text/html; charset=utf-8", "text/html"),
        ("image/svg+xml", "image/svg+xml"),
        ("", DEFAULT_ATTACHMENT_CONTENT_TYPE),
        (None, DEFAULT_ATTACHMENT_CONTENT_TYPE),
        ("not a media type", DEFAULT_ATTACHMENT_CONTENT_TYPE),
        ("image/png\r\nX-Injected: 1", DEFAULT_ATTACHMENT_CONTENT_TYPE),
        ("text/html; evil", "text/html"),
    ],
)
def test_normalize_media_type(raw: str | None, expected: str) -> None:
    assert normalize_media_type(raw) == expected


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("IMAGE/PNG; charset=utf-8", "image/png"),
        ("image/jpg", "image/jpeg"),
        ("", None),
        (None, None),
        ("png", None),
        ("image", None),
        ("image/png, image/jpeg", None),
        ("image/png\r\nX-Injected: 1", None),
        ("image/png; charset=utf-8\r\nX-Injected: 1", None),
        ("audio/mpeg\u0085", None),
        ("\ufeffimage/png", None),
        ("image/\u212a", None),
    ],
)
def test_parse_media_type(raw: str | None, expected: str | None) -> None:
    assert parse_media_type(raw) == expected


@pytest.mark.parametrize(
    ("content_type", "expected_kind", "expected_disposition", "expected_response_ct"),
    [
        ("image/png", "image", "inline", "image/png"),
        ("image/jpeg", "image", "inline", "image/jpeg"),
        ("image/jpg", "image", "inline", "image/jpeg"),
        ("image/gif", "image", "inline", "image/gif"),
        ("image/webp", "image", "inline", "image/webp"),
        ("audio/mpeg", "audio", "attachment", "audio/mpeg"),
        ("audio/x-caf", "audio", "attachment", "audio/x-caf"),
        ("video/mp4", "video", "attachment", "video/mp4"),
        ("image/svg+xml", "document", "attachment", DEFAULT_ATTACHMENT_CONTENT_TYPE),
        ("image/tiff", "document", "attachment", DEFAULT_ATTACHMENT_CONTENT_TYPE),
        ("image/heic", "document", "attachment", DEFAULT_ATTACHMENT_CONTENT_TYPE),
        ("text/html", "document", "attachment", DEFAULT_ATTACHMENT_CONTENT_TYPE),
        ("text/javascript", "document", "attachment", DEFAULT_ATTACHMENT_CONTENT_TYPE),
        ("application/x-javascript", "document", "attachment", DEFAULT_ATTACHMENT_CONTENT_TYPE),
        ("text/css", "document", "attachment", DEFAULT_ATTACHMENT_CONTENT_TYPE),
        ("application/pdf", "document", "attachment", DEFAULT_ATTACHMENT_CONTENT_TYPE),
        ("audio/fake+xml", "document", "attachment", DEFAULT_ATTACHMENT_CONTENT_TYPE),
        ("video/fake+xml", "document", "attachment", DEFAULT_ATTACHMENT_CONTENT_TYPE),
    ],
)
def test_attachment_media_allowlist_and_response_content_type(
    content_type: str,
    expected_kind: str,
    expected_disposition: str,
    expected_response_ct: str,
) -> None:
    assert ATTACHMENT_SANDBOX_CSP == "sandbox; default-src 'none'"
    assert INLINE_IMAGE_CONTENT_TYPES == frozenset(
        {"image/png", "image/jpeg", "image/gif", "image/webp"}
    )
    assert attachment_kind_for_content_type(content_type) == expected_kind
    assert attachment_disposition_for_content_type(content_type) == expected_disposition
    assert is_inline_attachment_content_type(content_type) == (
        expected_kind in {"image", "audio", "video"}
    )
    assert attachment_response_content_type(content_type) == expected_response_ct


def test_resolve_attachment_content_type_falls_back_to_filename_only_when_omitted() -> None:
    assert resolve_attachment_content_type(None, filename="shot.webp") == "image/webp"
    assert resolve_attachment_content_type("", filename="diagram.svg") == "image/svg+xml"
    assert (
        resolve_attachment_content_type("text/html", filename="shot.png")
        == "text/html"
    )
    assert (
        resolve_attachment_content_type(None, filename="recording.voicememo")
        == DEFAULT_ATTACHMENT_CONTENT_TYPE
    )


# Inputs both media-type parsers and kind rules must treat the same way.
_SHARED_MEDIA_TYPE_SAMPLES: tuple[str | None, ...] = (
    None,
    "",
    "image/png",
    "IMAGE/PNG; charset=utf-8",
    "image/jpg",
    "Image/JPG; quality=90",
    "image/gif",
    "image/webp ;q=1",
    "image/svg+xml",
    "image/heic",
    "  audio/mpeg  ",
    "audio/x-caf",
    "audio/fake+xml",
    "video/mp4",
    "video/fake+xml",
    "text/html; evil",
    "application/octet-stream",
    "png",
    "image",
    "image/",
    "/png",
    "image/ png",
    "image /png",
    ";image/png",
    "image/png, image/jpeg",
    "image/png\r\nX-Injected: 1",
    "\taudio/mpeg",
    "audio/mpeg\x7f",
    # str.strip() and String.prototype.trim() disagree on these.
    "audio/mpeg\u0085",
    "\ufeffimage/png",
    "image/png\u00a0",
    # Lowercases to ASCII "k" in both languages; must not pass as a token.
    "image/\u212a",
)


def test_chat_attachments_js_media_policy_matches_python() -> None:
    repo_root = Path(__file__).resolve().parents[1]
    module_path = (
        repo_root
        / "assistant_gateway"
        / "onboarding_web"
        / "assets"
        / "chat_attachments.mjs"
    )
    node = shutil.which("node") or "node"
    result = subprocess.run(
        [
            "rtk",
            "proxy",
            node,
            "--input-type=module",
            "-e",
            (
                'import { readFileSync } from "node:fs";'
                "import { INLINE_IMAGE_CONTENT_TYPES, normalizeMediaType, inferAttachmentKind }"
                f" from {json.dumps(module_path.as_uri())};"
                'const samples = JSON.parse(readFileSync(0, "utf8"));'
                "console.log(JSON.stringify({"
                "allowlist: INLINE_IMAGE_CONTENT_TYPES,"
                "rows: samples.map((s) => [normalizeMediaType(s),"
                " inferAttachmentKind({ content_type: s })]),"
                "}));"
            ),
        ],
        check=True,
        capture_output=True,
        text=True,
        input=json.dumps(list(_SHARED_MEDIA_TYPE_SAMPLES)),
    )
    js = json.loads(result.stdout.strip().splitlines()[-1])
    assert frozenset(js["allowlist"]) == INLINE_IMAGE_CONTENT_TYPES
    python_rows = [
        [normalize_media_type(sample), attachment_kind_for_content_type(sample)]
        for sample in _SHARED_MEDIA_TYPE_SAMPLES
    ]
    mismatches = [
        (sample, py_row, js_row)
        for sample, py_row, js_row in zip(_SHARED_MEDIA_TYPE_SAMPLES, python_rows, js["rows"])
        if py_row != js_row
    ]
    assert len(js["rows"]) == len(_SHARED_MEDIA_TYPE_SAMPLES)
    assert mismatches == []


def test_chat_attachments_js_unit_tests() -> None:
    node = shutil.which("node") or "node"
    result = subprocess.run(
        [
            "rtk",
            "proxy",
            node,
            "--test",
            str(Path(__file__).with_name("chat_attachments.test.mjs")),
        ],
        check=False,
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, result.stdout + result.stderr
