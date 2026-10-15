from __future__ import annotations

import pytest

from assistant_gateway.application.workspace_text_presentation import (
    is_workspace_text_file,
    present_workspace_text,
)


def test_workspace_json_presentation_formats_and_escapes_untrusted_text() -> None:
    content = '{"script":"<script>alert(1)</script>","enabled":true}\n'

    presentation = present_workspace_text(
        name="settings.json",
        content=content,
        size_bytes=len(content.encode("utf-8")),
    )

    assert presentation.syntax_language == "JSON"
    assert presentation.formatted_content == (
        '{\n  "script": "<script>alert(1)</script>",\n  "enabled": true\n}\n'
    )
    assert presentation.highlighted_html is not None
    assert "ua-syntax-" in presentation.highlighted_html
    assert "&lt;script&gt;alert(1)&lt;/script&gt;" in presentation.highlighted_html
    assert "<script>" not in presentation.highlighted_html


def test_workspace_invalid_json_remains_raw_but_keeps_json_highlighting() -> None:
    content = '{"enabled": true,}'

    presentation = present_workspace_text(
        name="settings.json",
        content=content,
        size_bytes=len(content.encode("utf-8")),
    )

    assert presentation.syntax_language == "JSON"
    assert presentation.formatted_content is None
    assert presentation.highlighted_html is not None


@pytest.mark.parametrize(
    ("name", "content", "expected_language"),
    [
        ("client.ts", "const enabled: boolean = true;\n", "TypeScript"),
    ],
)
def test_workspace_known_source_types_receive_syntax_highlighting(
    name: str,
    content: str,
    expected_language: str,
) -> None:
    presentation = present_workspace_text(
        name=name,
        content=content,
        size_bytes=len(content.encode("utf-8")),
    )

    assert presentation.syntax_language == expected_language
    assert presentation.formatted_content is None
    assert presentation.highlighted_html is not None
    assert "ua-syntax-" in presentation.highlighted_html


def test_workspace_text_detection_keeps_plain_and_source_types_readable() -> None:
    assert is_workspace_text_file(name="notes.txt", content_type="text/plain")
    assert is_workspace_text_file(name="worker.py", content_type=None)
    assert is_workspace_text_file(name="Dockerfile", content_type=None)
    assert not is_workspace_text_file(
        name="archive.bin",
        content_type="application/octet-stream",
    )
