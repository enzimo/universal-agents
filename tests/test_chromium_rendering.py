"""Opt-in rendering checks against built Docs and the shipped browser assets."""

import asyncio
import os
import shutil
from pathlib import Path

import pytest
from aiohttp import web
from aiohttp.test_utils import TestServer

from tests.test_account_links import (  # noqa: F401 - pytest fixtures
    _isolated_credentials,
    link_request,
    save_credentials,
    token_credential,
    world,
)
from user_docs.service import UserDocsService


@pytest.mark.integration
@pytest.mark.live
@pytest.mark.asyncio
@pytest.mark.skipif(
    os.environ.get("RUN_CHROMIUM_TESTS") != "1",
    reason="set RUN_CHROMIUM_TESTS=1 with Node Playwright and Chromium installed",
)
@pytest.mark.parametrize("probe", ["tasks_ui_probe.cjs", "team_attention_ui_probe.cjs"])
async def test_chromium_task_lists(probe):
    process = await asyncio.create_subprocess_exec(
        "rtk", "proxy", shutil.which("node") or "node",
        str(Path(__file__).with_name(probe)),
        stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT,
    )
    try:
        output, _ = await asyncio.wait_for(process.communicate(), timeout=90)
    except TimeoutError:
        process.kill()
        await process.wait()
        raise
    assert process.returncode == 0, output.decode()


@pytest.mark.integration
@pytest.mark.live
@pytest.mark.asyncio
@pytest.mark.skipif(
    os.environ.get("RUN_CHROMIUM_TESTS") != "1",
    reason="set RUN_CHROMIUM_TESTS=1 with Node Playwright and Chromium installed",
)
async def test_chromium_ui_settings_improvements(tmp_path):
    from datetime import datetime, timedelta, timezone
    from secure_tool_broker.service import (
        CredentialCollectionSession,
        SecureToolBrokerService,
    )
    from tests.test_credential_catalog import TEST_TOOL_CONTEXT

    form = tmp_path / "credential.html"
    form.write_text(
        SecureToolBrokerService._render_credential_collection_form(
            CredentialCollectionSession(
                request_payload={
                    "service_name": "Test",
                    "credential_key": "test",
                    "fields": [
                        {"name": "api_key", "kind": "token", "visibility": "secret"},
                        {
                            "name": "username",
                            "kind": "username",
                            "visibility": "metadata_visible",
                        },
                    ],
                },
                tool_context=TEST_TOOL_CONTEXT,
                created_at=datetime.now(timezone.utc),
                expires_at=datetime.now(timezone.utc) + timedelta(minutes=15),
            )
        )
    )
    process = await asyncio.create_subprocess_exec(
        "rtk",
        "proxy",
        shutil.which("node") or "node",
        str(Path(__file__).with_name("ui_settings_improvements_probe.cjs")),
        str(form),
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.STDOUT,
    )
    try:
        output, _ = await asyncio.wait_for(process.communicate(), timeout=90)
    except TimeoutError:
        process.kill()
        await process.wait()
        raise
    assert process.returncode == 0, output.decode()


@pytest.mark.integration
@pytest.mark.live
@pytest.mark.asyncio
@pytest.mark.skipif(os.environ.get("RUN_CHROMIUM_TESTS") != "1", reason="set RUN_CHROMIUM_TESTS=1 with Node Playwright and Chromium installed")
@pytest.mark.parametrize("probe", ["web_chat_history_probe.cjs", "web_chat_scroll_probe.cjs", "web_chat_submission_probe.cjs", "web_chat_startup_probe.cjs"])
async def test_chromium_web_chat_transcript(probe):
    process = await asyncio.create_subprocess_exec(
        "rtk", "proxy", shutil.which("node") or "node",
        str(Path(__file__).with_name(probe)),
        stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT,
    )
    try:
        output, _ = await asyncio.wait_for(process.communicate(), timeout=60)
    except TimeoutError:
        process.kill()
        await process.wait()
        raise
    assert process.returncode == 0, output.decode()


@pytest.mark.integration
@pytest.mark.live
@pytest.mark.asyncio
@pytest.mark.skipif(os.environ.get("RUN_CHROMIUM_TESTS") != "1", reason="set RUN_CHROMIUM_TESTS=1 with Node Playwright and Chromium installed")
async def test_chromium_navigation_backgrounds_and_settings(tmp_path):
    from mkdocs.commands.build import build
    from mkdocs.config import load_config

    repo = Path(__file__).resolve().parents[1]
    site = tmp_path / "site"
    config = load_config(config_file=str(repo / "mkdocs.yml"), site_dir=str(site), strict=True)
    await asyncio.to_thread(build, config)

    async def appearance(_request):
        return web.json_response({"appearance": None})

    service = UserDocsService(
        docs_root=site,
        extra_routes=[("GET", "/api/web-chat/appearance", appearance)],
    )
    async with TestServer(service.build_app()) as server:
        process = await asyncio.create_subprocess_exec(
            shutil.which("node") or "node",
            str(Path(__file__).with_name("chromium_rendering_probe.cjs")),
            str(server.make_url("/")),
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.STDOUT,
        )
        try:
            output, _ = await asyncio.wait_for(process.communicate(), timeout=90)
        except TimeoutError:
            process.kill()
            await process.wait()
            raise
        assert process.returncode == 0, output.decode()


@pytest.mark.integration
@pytest.mark.live
@pytest.mark.asyncio
@pytest.mark.skipif(os.environ.get("RUN_CHROMIUM_TESTS") != "1", reason="set RUN_CHROMIUM_TESTS=1 with Node Playwright and Chromium installed")
async def test_chromium_broker_account_link_checkbox(
    world, monkeypatch  # noqa: F811 - the imported pytest fixture
):
    from aiohttp.test_utils import TestClient
    from secure_tool_broker.credential_catalog_store import CredentialOwnerScope
    from secure_tool_broker.service import SecureToolBrokerService
    from tests.broker_access_support import gateway_headers

    gate, _store = world
    monkeypatch.setenv("PORTAINER_TOKEN", "portainer-secret")
    save_credentials(
        CredentialOwnerScope.user("user-1"),
        portainer_token=token_credential("PORTAINER_TOKEN"),
    )
    service = SecureToolBrokerService(token="test-token", access_gate=gate)
    async with TestClient(TestServer(service.build_app())) as client:
        response = await client.post(
            "/v1/account-link/sessions", headers=gateway_headers(), json=link_request()
        )
        assert response.status == 200
        payload = await response.json()
        form_url = str(client.make_url(f"/account-link/{payload['session_id']}"))
        process = await asyncio.create_subprocess_exec(
            shutil.which("node") or "node", str(Path(__file__).with_name("broker_account_link_checkbox_probe.cjs")),
            form_url, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT)
        try:
            output, _ = await asyncio.wait_for(process.communicate(), timeout=60)
        except TimeoutError:
            process.kill()
            await process.wait()
            raise
        assert process.returncode == 0, output.decode()
