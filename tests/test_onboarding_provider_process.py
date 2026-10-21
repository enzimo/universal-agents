"""Real model probes preserve results and keep keys out of process arguments."""

from __future__ import annotations

import asyncio
import json
import signal
import sys

from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer
import pytest

from assistant_gateway.onboarding_web.provider_process import ProviderCheck, ProviderChecks, _stop_process
from assistant_gateway.onboarding_web.server import OnboardingWebService, OnboardingWebSettings


@pytest.mark.asyncio
@pytest.mark.parametrize("status", [200, 401])
async def test_provider_process_returns_the_shared_checks_result_without_exposing_keys(monkeypatch, status) -> None:
    key = "test-private-provider-key"
    requests: list[dict] = []
    children: list[asyncio.subprocess.Process] = []
    start_process = asyncio.create_subprocess_exec

    async def record_process(*args, **kwargs):
        assert all(key not in argument for argument in args)
        process = await start_process(*args, **kwargs)
        children.append(process)
        return process

    async def provider(request):
        assert request.headers["Authorization"] == f"Bearer {key}"
        requests.append(await request.json())
        body = {"choices": [{"message": {"content": "connected"}}]} if status == 200 else {"error": {"message": f"Refused {key}"}}
        return web.Response(text=json.dumps(body), content_type="application/json", status=status)

    monkeypatch.setattr(asyncio, "create_subprocess_exec", record_process)
    app = web.Application()
    app.router.add_post("/v1/chat/completions", provider)
    checks = ProviderChecks()
    async with TestServer(app) as server:
        result = await checks.run(
            provider="OpenAI Compatible", endpoint=str(server.make_url("/v1")), token=key,
            model="test-model", reasoning_effort="medium",
        )
    await checks.close(app)

    assert result.ok is (status == 200)
    assert result.sample == ("connected" if status == 200 else "")
    assert key not in result.message
    assert len(requests) == 1 and requests[0]["reasoning_effort"] == "medium"
    assert children and all(child.returncode == 0 for child in children)


@pytest.mark.asyncio
async def test_shutdown_refuses_new_provider_checks() -> None:
    checks = ProviderChecks()
    await checks.close(None)

    with pytest.raises(asyncio.CancelledError):
        await checks.run(provider="OpenAI Compatible", endpoint="http://example.test", token="test-key", model="test-model")


@pytest.mark.asyncio
async def test_invalid_provider_input_is_refused_without_logging_submitted_values(tmp_path, caplog) -> None:
    key = "test-private-provider-key"
    service = OnboardingWebService(OnboardingWebSettings(instance_root=str(tmp_path / "instance")))
    async with TestClient(TestServer(service.build_app())) as client:
        response = await client.post("/api/provider-test", json={
            "provider": "OpenAI Compatible", "endpoint": "https://example.test/v1", "model": "test-model",
            "token": key, "reasoning_summary": {"invalid": key},
        })
        body = await response.text()

    assert response.status == 400
    assert key not in body
    assert key not in caplog.text
    assert "invalid fields" in body


@pytest.mark.asyncio
async def test_only_two_provider_processes_run_and_the_limit_releases_after_completion(tmp_path) -> None:
    arrived: asyncio.Queue[None] = asyncio.Queue()
    release = asyncio.Event()

    async def provider(_request):
        arrived.put_nowait(None)
        await release.wait()
        return web.json_response({"choices": [{"message": {"content": "connected"}}]})

    upstream = web.Application()
    upstream.router.add_post("/v1/chat/completions", provider)
    service = OnboardingWebService(OnboardingWebSettings(instance_root=str(tmp_path / "instance")))
    async with TestServer(upstream) as server, TestClient(TestServer(service.build_app())) as client:
        payload = {
            "provider": "OpenAI Compatible", "endpoint": str(server.make_url("/v1")),
            "token": "test-key", "model": "test-model",
        }
        pending = [asyncio.create_task(client.post("/api/provider-test", json=payload)) for _ in range(2)]
        try:
            for _ in pending:
                await asyncio.wait_for(arrived.get(), timeout=20)
            refused = await client.post("/api/provider-test", json=payload)
            assert refused.status == 429
            assert "already running" in (await refused.json())["message"]
            assert arrived.empty()
        finally:
            release.set()
            finished = await asyncio.gather(*pending)
        assert all(response.status == 200 for response in finished)
        accepted = await client.post("/api/provider-test", json=payload)
        assert accepted.status == 200


def test_provider_check_repr_does_not_contain_keys() -> None:
    check = ProviderCheck(
        provider="OpenAI Compatible", endpoint="https://example.test", token="private-provider-token",
        model="test-model", xai_oauth={"access_token": "private-sign-in-token"},
    )

    assert "private-provider-token" not in repr(check)
    assert "private-sign-in-token" not in repr(check)


@pytest.mark.asyncio
async def test_a_probe_that_ignores_sigterm_is_killed_and_reaped(caplog) -> None:
    process = await asyncio.create_subprocess_exec(
        sys.executable, "-c",
        "import signal,sys,time; signal.signal(signal.SIGTERM, signal.SIG_IGN); "
        "sys.stdout.write('ready\\n'); sys.stdout.flush(); time.sleep(60)",
        stdout=asyncio.subprocess.PIPE,
    )
    try:
        assert process.stdout is not None
        assert await asyncio.wait_for(process.stdout.readline(), timeout=10) == b"ready\n"
        await asyncio.wait_for(_stop_process(process), timeout=3)
    finally:
        if process.returncode is None:
            process.kill()
            await process.wait()

    assert process.returncode == -signal.SIGKILL
    assert f"event=onboarding_provider_process_killed pid={process.pid}" in caplog.text
