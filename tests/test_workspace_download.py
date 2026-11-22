from __future__ import annotations

import asyncio
import logging
import os
import threading
from pathlib import Path
from types import SimpleNamespace
from urllib.parse import quote

import pytest
from aiohttp import ClientError, DummyCookieJar
from aiohttp.test_utils import TestClient, TestServer

from assistant_gateway.application.website_application import (
    GatewayWorkspaceFileDownload,
)
from assistant_gateway.application.workspace_browser import WORKSPACE_PREVIEW_MAX_BYTES
from assistant_gateway.interactive_apps.website_handlers.workspace import (
    WebsiteWorkspaceHandlers,
)
from assistant_gateway.interactive_apps.website_handlers.workspace_download import (
    ByteRange,
    accepts_html_navigation,
    build_attachment_disposition,
    parse_byte_range,
    unauthenticated_download_redirect_location,
)
from runtime_settings.instance_layout import person_folder_name
from tests.test_gateway_website_app import (
    InternalWebsiteInteractiveApp,
    _UserTokenFakeGateway,
    _config,
)


def test_parse_byte_range_covers_valid_unsatisfiable_and_ignored_headers() -> None:
    assert parse_byte_range(None, 100).status == "none"
    assert parse_byte_range("", 100).status == "none"
    assert parse_byte_range("   ", 100).status == "none"

    valid_explicit = parse_byte_range("bytes=0-9", 100)
    assert valid_explicit.status == "satisfiable"
    assert valid_explicit.byte_range == ByteRange(start=0, end=9)
    assert valid_explicit.byte_range.length == 10

    clamped = parse_byte_range("bytes=90-999", 100)
    assert clamped.status == "satisfiable"
    assert clamped.byte_range == ByteRange(start=90, end=99)

    open_ended = parse_byte_range("bytes=95-", 100)
    assert open_ended.status == "satisfiable"
    assert open_ended.byte_range == ByteRange(start=95, end=99)

    suffix = parse_byte_range("bytes=-10", 100)
    assert suffix.status == "satisfiable"
    assert suffix.byte_range == ByteRange(start=90, end=99)

    suffix_clamped = parse_byte_range("bytes=-500", 100)
    assert suffix_clamped.status == "satisfiable"
    assert suffix_clamped.byte_range == ByteRange(start=0, end=99)

    for unsatisfiable in ("bytes=100-110", "bytes=100-", "bytes=10-5", "bytes=-0"):
        assert parse_byte_range(unsatisfiable, 100).status == "unsatisfiable"
    for empty_range in ("bytes=0-0", "bytes=0-", "bytes=-1"):
        assert parse_byte_range(empty_range, 0).status == "unsatisfiable"

    for ignored in (
        "items=0-5",
        "bytes=0-1, 2-3",
        "bytes=abc-def",
        "bytes=10",
        "bytes=-",
        "bytes=0 - 5",
        "bytes=²-5",
        "malformed",
    ):
        assert parse_byte_range(ignored, 100).status == "none"


def test_build_attachment_disposition_sanitizes_and_encodes_utf8() -> None:
    plain = build_attachment_disposition("report.pdf")
    assert plain == 'attachment; filename="report.pdf"; filename*=UTF-8\'\'report.pdf'

    utf8 = build_attachment_disposition("café résumé (final).pdf")
    assert utf8.startswith('attachment; filename="caf_ r_sum_ (final).pdf"; ')
    assert "filename*=UTF-8''caf%C3%A9%20r%C3%A9sum%C3%A9%20%28final%29.pdf" in utf8

    injected = build_attachment_disposition('evil\r\nX-Test: 1\x00/dir\\a"b.txt')
    assert "\r" not in injected
    assert "\n" not in injected
    assert "\x00" not in injected
    assert "/" not in injected
    assert "\\" not in injected
    assert 'filename="evilX-Test: 1dira_b.txt"' in injected

    fallback = build_attachment_disposition("\r\n\x00")
    assert fallback == 'attachment; filename="download"; filename*=UTF-8\'\'download'


def test_accepts_html_navigation_and_redirect_location() -> None:
    assert accepts_html_navigation(
        "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8"
    )
    assert accepts_html_navigation("application/json, text/html; level=1; q=0.5")
    assert not accepts_html_navigation("text/html;q=0, application/json")
    assert not accepts_html_navigation("text/html;q=notanumber")
    assert not accepts_html_navigation("*/*")
    assert not accepts_html_navigation("application/json")
    assert not accepts_html_navigation("")
    assert not accepts_html_navigation(None)

    assert unauthenticated_download_redirect_location("") == "/webchat?view=files"
    assert (
        unauthenticated_download_redirect_location("team/report 1.pdf")
        == "/webchat?view=files&path=team%2Freport%201.pdf&download=1"
    )
    evil_redirect = unauthenticated_download_redirect_location("https://evil.example/x")
    assert evil_redirect.startswith("/webchat?view=files&path=")
    assert evil_redirect.endswith("&download=1")
    assert not evil_redirect.startswith("//")


def _setup_workspace_app(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> tuple[InternalWebsiteInteractiveApp, Path, str, dict[str, tuple[str, ...]]]:
    config = _config(tmp_path)
    config.interactive_apps.internal_website.web_chat_enabled = True
    monkeypatch.setenv("WEB_CHAT_AUTH_TOKEN", "uaweb_test_token")
    workspace = config.workspace_path
    workspace.mkdir(parents=True, exist_ok=True)
    admin_id = config.user.canonical_user_id
    gateway = _UserTokenFakeGateway()
    team_roles: dict[str, tuple[str, ...]] = {
        admin_id: ("owner",),
        "user_alpha": ("member",),
    }
    gateway.access_inbox = SimpleNamespace(
        person=lambda person_id: (
            SimpleNamespace(roles=team_roles[person_id])
            if person_id in team_roles
            else None
        )
    )
    app = InternalWebsiteInteractiveApp(
        config,
        gateway=gateway,
        bridge=SimpleNamespace(),
    )
    return app, workspace, admin_id, team_roles


async def _auth_headers(client: TestClient, token: str) -> dict[str, str]:
    response = await client.post("/api/web-chat/auth", json={"token": token})
    assert response.status == 200
    cookie = response.cookies["ua_web_chat_session"].value
    return {"Cookie": f"ua_web_chat_session={cookie}"}


def _assert_download_security_headers(headers: dict[str, str] | object) -> None:
    assert headers["X-Content-Type-Options"] == "nosniff"
    assert headers["Cache-Control"] == "no-store"
    assert headers["Cross-Origin-Resource-Policy"] == "same-origin"
    assert headers["Referrer-Policy"] == "no-referrer"
    assert headers["Accept-Ranges"] == "bytes"


@pytest.mark.asyncio
async def test_workspace_download_streams_large_binary_ranges_head_and_utf8(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    app, workspace, admin_id, _roles = _setup_workspace_app(tmp_path, monkeypatch)
    admin_dir = workspace / "users" / person_folder_name(admin_id)
    admin_dir.mkdir(parents=True)

    large_size = WORKSPACE_PREVIEW_MAX_BYTES + 4096
    large_bytes = b"HEAD" + (b"\x01\x02\x03\x04" * ((large_size - 8) // 4)) + b"TAIL"
    assert len(large_bytes) == large_size
    (admin_dir / "recording.mp3").write_bytes(large_bytes)

    utf8_name = "café report (v2).tar.gz"
    utf8_bytes = b"\x1f\x8b\x08\x00compressed-payload-bytes"
    (admin_dir / utf8_name).write_bytes(utf8_bytes)
    (admin_dir / "empty.bin").write_bytes(b"")

    read_calls = 0
    original_open = app.application.operations.open_web_chat_workspace_download
    loop_thread_id = threading.get_ident()
    observed_threads: dict[str, set[int]] = {"open": set(), "read": set(), "close": set()}
    opened_downloads: list[GatewayWorkspaceFileDownload] = []

    def tracked_open(**kwargs) -> GatewayWorkspaceFileDownload:
        observed_threads["open"].add(threading.get_ident())
        download = original_open(**kwargs)
        opened_downloads.append(download)
        orig_read_at = download.read_at
        orig_close = download.close

        def wrapped_read_at(offset: int, max_bytes: int) -> bytes:
            nonlocal read_calls
            read_calls += 1
            observed_threads["read"].add(threading.get_ident())
            return orig_read_at(offset, max_bytes)

        def wrapped_close() -> None:
            observed_threads["close"].add(threading.get_ident())
            orig_close()

        download.read_at = wrapped_read_at  # type: ignore[method-assign]
        download.close = wrapped_close  # type: ignore[method-assign]
        return download

    monkeypatch.setattr(
        app.application.operations,
        "open_web_chat_workspace_download",
        tracked_open,
    )

    large_path = quote(f"users/{person_folder_name(admin_id)}/recording.mp3", safe="")
    utf8_path = quote(f"users/{person_folder_name(admin_id)}/{utf8_name}", safe="")
    empty_path = quote(f"users/{person_folder_name(admin_id)}/empty.bin", safe="")

    async with TestClient(TestServer(app.build_app()), cookie_jar=DummyCookieJar()) as client:
        headers = await _auth_headers(client, "uaweb_test_token")

        # HEAD must not read file bytes
        head_resp = await client.head(
            f"/api/web-chat/workspace/download?path={large_path}",
            headers=headers,
        )
        assert head_resp.status == 200
        assert await head_resp.read() == b""
        assert read_calls == 0
        assert head_resp.headers["Content-Length"] == str(large_size)
        assert head_resp.headers["Content-Type"] == "audio/mpeg"
        assert 'attachment; filename="recording.mp3"' in head_resp.headers["Content-Disposition"]
        _assert_download_security_headers(head_resp.headers)

        # Full GET of >5 MB binary file streams exact content in multiple chunks
        full_resp = await client.get(
            f"/api/web-chat/workspace/download?path={large_path}",
            headers=headers,
        )
        assert full_resp.status == 200
        assert await full_resp.read() == large_bytes
        assert read_calls > 1
        assert full_resp.headers["Content-Length"] == str(large_size)
        assert full_resp.headers["Content-Type"] == "audio/mpeg"
        _assert_download_security_headers(full_resp.headers)

        # UTF-8 filename + compressed archive MIME type (C06)
        utf8_resp = await client.get(
            f"/api/web-chat/workspace/download?path={utf8_path}",
            headers=headers,
        )
        assert utf8_resp.status == 200
        assert await utf8_resp.read() == utf8_bytes
        assert utf8_resp.headers["Content-Type"] == "application/gzip"
        assert (
            "filename*=UTF-8''caf%C3%A9%20report%20%28v2%29.tar.gz"
            in utf8_resp.headers["Content-Disposition"]
        )
        _assert_download_security_headers(utf8_resp.headers)

        # Single byte ranges: explicit, open-ended, suffix, clamped
        r1 = await client.get(
            f"/api/web-chat/workspace/download?path={large_path}",
            headers={**headers, "Range": "bytes=0-15"},
        )
        assert r1.status == 206
        assert await r1.read() == large_bytes[:16]
        assert r1.headers["Content-Range"] == f"bytes 0-15/{large_size}"
        assert r1.headers["Content-Length"] == "16"
        _assert_download_security_headers(r1.headers)

        r2 = await client.get(
            f"/api/web-chat/workspace/download?path={large_path}",
            headers={**headers, "Range": f"bytes={large_size - 8}-"},
        )
        assert r2.status == 206
        assert await r2.read() == large_bytes[-8:]
        assert r2.headers["Content-Range"] == f"bytes {large_size - 8}-{large_size - 1}/{large_size}"
        assert r2.headers["Content-Length"] == "8"

        r3 = await client.get(
            f"/api/web-chat/workspace/download?path={large_path}",
            headers={**headers, "Range": "bytes=-4"},
        )
        assert r3.status == 206
        assert await r3.read() == b"TAIL"
        assert r3.headers["Content-Range"] == f"bytes {large_size - 4}-{large_size - 1}/{large_size}"

        # Unsatisfiable ranges -> 416 with Content-Range: bytes */<size> and body-sized Content-Length
        for bad_range in ("bytes=99999999-", "bytes=10-5", "bytes=-0"):
            r416 = await client.get(
                f"/api/web-chat/workspace/download?path={utf8_path}",
                headers={**headers, "Range": bad_range},
            )
            assert r416.status == 416
            body_416 = await r416.read()
            assert r416.headers["Content-Range"] == f"bytes */{len(utf8_bytes)}"
            assert r416.headers["Content-Length"] == str(len(body_416))
            _assert_download_security_headers(r416.headers)

        # Malformed and multi-range headers are ignored and serve 200 full file
        for ignored_range in ("bytes=abc-def", "bytes=0-1, 2-3", "items=0-3"):
            r200 = await client.get(
                f"/api/web-chat/workspace/download?path={utf8_path}",
                headers={**headers, "Range": ignored_range},
            )
            assert r200.status == 200
            assert await r200.read() == utf8_bytes

        # 0-byte file: GET -> 200 empty, Range -> 416 bytes */0
        empty_get = await client.get(
            f"/api/web-chat/workspace/download?path={empty_path}",
            headers=headers,
        )
        assert empty_get.status == 200
        assert await empty_get.read() == b""
        assert empty_get.headers["Content-Length"] == "0"

        empty_range = await client.get(
            f"/api/web-chat/workspace/download?path={empty_path}",
            headers={**headers, "Range": "bytes=0-0"},
        )
        assert empty_range.status == 416
        assert empty_range.headers["Content-Range"] == "bytes */0"

    # All disk open, read, and close calls ran off the asyncio event loop and closed cleanly
    assert observed_threads["open"] and loop_thread_id not in observed_threads["open"]
    assert observed_threads["read"] and loop_thread_id not in observed_threads["read"]
    assert observed_threads["close"] and loop_thread_id not in observed_threads["close"]
    assert opened_downloads and all(download.closed for download in opened_downloads)


@pytest.mark.asyncio
async def test_workspace_download_enforces_safety_member_team_visibility_and_auth_redirect(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    app, workspace, admin_id, team_roles = _setup_workspace_app(tmp_path, monkeypatch)
    admin_folder = workspace / "users" / person_folder_name(admin_id)
    member_folder = workspace / "users" / person_folder_name("user_alpha")
    team_folder = workspace / "team"
    agents_folder = workspace / "agents"
    for folder in (admin_folder, member_folder, team_folder, agents_folder):
        folder.mkdir(parents=True)

    (admin_folder / "private.txt").write_text("admin-only", encoding="utf-8")
    (member_folder / "mine.txt").write_text("member-own", encoding="utf-8")
    (team_folder / "guide.md").write_text("# Team guide\n", encoding="utf-8")
    (agents_folder / "job.log").write_text("agent-log", encoding="utf-8")
    (team_folder / ".env").write_text("SECRET=1\n", encoding="utf-8")
    (team_folder / ".git").mkdir()
    (team_folder / ".git" / "config").write_text("git", encoding="utf-8")
    (team_folder / "node_modules").mkdir()
    (team_folder / "node_modules" / "index.js").write_text("x", encoding="utf-8")

    outside = tmp_path / "outside.txt"
    outside.write_text("outside", encoding="utf-8")
    os.symlink(outside, team_folder / "symlink.txt")
    os.link(outside, team_folder / "hardlink.txt")

    async with TestClient(TestServer(app.build_app()), cookie_jar=DummyCookieJar()) as client:
        # A3: Unauthenticated HTML GET redirects 302 to /webchat?view=files&path=...&download=1
        html_redirect = await client.get(
            "/api/web-chat/workspace/download?path=team%2Fguide.md",
            headers={"Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8"},
            allow_redirects=False,
        )
        assert html_redirect.status == 302
        assert (
            html_redirect.headers["Location"]
            == "/webchat?view=files&path=team%2Fguide.md&download=1"
        )
        assert html_redirect.headers["Cache-Control"] == "no-store"
        assert html_redirect.headers["Referrer-Policy"] == "no-referrer"

        # Unauthenticated non-HTML GET and HEAD return 401
        for accept_header in (None, "*/*", "application/json", "text/html;q=0"):
            req_headers = {"Accept": accept_header} if accept_header is not None else {}
            unauth_api = await client.get(
                "/api/web-chat/workspace/download?path=team%2Fguide.md",
                headers=req_headers,
                allow_redirects=False,
            )
            assert unauth_api.status == 401

        unauth_head = await client.head(
            "/api/web-chat/workspace/download?path=team%2Fguide.md",
            headers={"Accept": "text/html"},
            allow_redirects=False,
        )
        assert unauth_head.status == 401

        admin_headers = await _auth_headers(client, "uaweb_test_token")
        member_headers = await _auth_headers(client, "ua_user_token")

        # A4: Member can download team/ and own users/<me>/, not agents/ or users/<other>/
        member_team = await client.get(
            "/api/web-chat/workspace/download?path=team%2Fguide.md",
            headers=member_headers,
        )
        assert member_team.status == 200
        assert await member_team.read() == b"# Team guide\n"

        member_team_head = await client.head(
            "/api/web-chat/workspace/download?path=team%2Fguide.md",
            headers=member_headers,
        )
        assert member_team_head.status == 200
        assert member_team_head.headers["Content-Length"] == str(len(b"# Team guide\n"))

        member_own = await client.get(
            f"/api/web-chat/workspace/download?path=users%2F{person_folder_name('user_alpha')}%2Fmine.txt",
            headers=member_headers,
        )
        assert member_own.status == 200
        assert await member_own.read() == b"member-own"

        member_agents = await client.get(
            "/api/web-chat/workspace/download?path=agents%2Fjob.log",
            headers=member_headers,
        )
        assert member_agents.status == 403

        member_other = await client.get(
            f"/api/web-chat/workspace/download?path=users%2F{person_folder_name(admin_id)}%2Fprivate.txt",
            headers=member_headers,
        )
        assert member_other.status == 403

        # Admin can download agents/ and team/, but NOT another person's users/<other>/
        admin_agents = await client.get(
            "/api/web-chat/workspace/download?path=agents%2Fjob.log",
            headers=admin_headers,
        )
        assert admin_agents.status == 200
        assert await admin_agents.read() == b"agent-log"

        admin_other = await client.get(
            f"/api/web-chat/workspace/download?path=users%2F{person_folder_name('user_alpha')}%2Fmine.txt",
            headers=admin_headers,
        )
        assert admin_other.status == 403

        # A2: Refuses symlinks, hard links, directories, traversal, missing, sensitive, hidden tooling
        refused_cases = (
            ("team/symlink.txt", 403),
            ("team/hardlink.txt", 403),
            ("team", 400),
            ("../outside.txt", 403),
            ("team/does-not-exist.bin", 404),
            ("team/.env", 403),
            ("team/.git/config", 403),
            ("team/node_modules/index.js", 403),
        )
        for bad_path, expected_status in refused_cases:
            resp = await client.get(
                f"/api/web-chat/workspace/download?path={quote(bad_path, safe='')}",
                headers=admin_headers,
            )
            assert resp.status == expected_status, (bad_path, resp.status)

        # Inactive member gets 403 (not a 302 redirect loop)
        team_roles.pop("user_alpha")
        inactive_resp = await client.get(
            "/api/web-chat/workspace/download?path=team%2Fguide.md",
            headers={**member_headers, "Accept": "text/html"},
            allow_redirects=False,
        )
        assert inactive_resp.status == 403


@pytest.mark.asyncio
async def test_workspace_download_forces_close_on_midstream_truncation(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    app, workspace, _admin_id, _roles = _setup_workspace_app(tmp_path, monkeypatch)
    team_folder = workspace / "team"
    team_folder.mkdir(parents=True)
    target = team_folder / "shrinking.bin"
    target.write_bytes(b"A" * 1024)

    original_open = app.application.operations.open_web_chat_workspace_download
    opened: list[GatewayWorkspaceFileDownload] = []

    def truncating_open(**kwargs) -> GatewayWorkspaceFileDownload:
        download = original_open(**kwargs)
        opened.append(download)
        orig_read_at = download.read_at
        calls = 0

        def short_read(offset: int, max_bytes: int) -> bytes:
            nonlocal calls
            calls += 1
            if calls == 1:
                return orig_read_at(offset, 128)
            return b""

        download.read_at = short_read  # type: ignore[method-assign]
        return download

    monkeypatch.setattr(
        app.application.operations,
        "open_web_chat_workspace_download",
        truncating_open,
    )

    caplog.set_level(logging.WARNING)
    async with TestClient(TestServer(app.build_app()), cookie_jar=DummyCookieJar()) as client:
        headers = await _auth_headers(client, "uaweb_test_token")
        # Must fail promptly with a client incomplete-transfer error rather than hanging
        with pytest.raises(ClientError):
            async with client.get(
                "/api/web-chat/workspace/download?path=team%2Fshrinking.bin",
                headers=headers,
            ) as resp:
                assert resp.status == 200
                await asyncio.wait_for(resp.read(), timeout=5.0)

    assert any("event=workspace_download_truncated" in rec.message for rec in caplog.records)
    assert opened and opened[0].closed is True


@pytest.mark.asyncio
async def test_close_download_safely_closes_descriptor_even_when_cancelled(
    tmp_path: Path,
) -> None:
    target = tmp_path / "sample.bin"
    target.write_bytes(b"hello")
    fd = os.open(target, os.O_RDONLY)
    started = threading.Event()
    release = threading.Event()
    close_calls = 0

    def slow_close() -> None:
        nonlocal close_calls
        started.set()
        release.wait(timeout=2.0)
        close_calls += 1
        os.close(fd)

    download = GatewayWorkspaceFileDownload(
        path="team/sample.bin",
        name="sample.bin",
        size_bytes=5,
        content_type="application/octet-stream",
        file_descriptor=fd,
        close_callback=slow_close,
    )

    task = asyncio.create_task(WebsiteWorkspaceHandlers._close_download_safely(download))
    await asyncio.to_thread(started.wait, 2.0)
    task.cancel()
    release.set()
    with pytest.raises(asyncio.CancelledError):
        await task

    assert download.closed is True
    assert close_calls == 1

