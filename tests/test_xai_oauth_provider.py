from __future__ import annotations

import argparse
import asyncio
import base64
from concurrent.futures import ThreadPoolExecutor
import errno
import io
import json
import os
import stat
import time
from pathlib import Path
from urllib import error as urllib_error

import pytest

from assistant_cli import _resolve_xai_oauth_command_settings
from assistant_gateway.config import GatewayInstanceConfig
from assistant_gateway.runtime_config import build_runtime_config_from_instance_config
from config_loader import resolve_agent_type_settings
from custom_agents.runtime_paths import resolve_runtime_instance_root
from custom_agents.strands_model_factory import StrandsModelFactoryConfig, build_strands_model
from custom_agents.xai_oauth_auth import (
    XAI_OAUTH_CLIENT_ID,
    XAI_OAUTH_DEVICE_GRANT,
    XAIDeviceAuthorization,
    XAIOAuthCredentials,
    XAIOAuthError,
    _proactive_refresh_skew_seconds,
    make_xai_oauth_api_key_provider,
    poll_xai_device_token,
    refresh_xai_oauth_tokens,
    request_xai_device_authorization,
    resolve_xai_oauth_credentials,
    resolve_xai_oauth_settings,
    save_xai_oauth_device_tokens,
    validate_xai_url,
    xai_oauth_status,
)
from file_locking import FileLockUnavailable


def _jwt(*, exp_offset_seconds: int = 3600) -> str:
    header = {"alg": "none", "typ": "JWT"}
    payload = {"exp": int(time.time()) + exp_offset_seconds}

    def encode(value: dict[str, object]) -> str:
        raw = json.dumps(value, separators=(",", ":")).encode("utf-8")
        return base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")

    return f"{encode(header)}.{encode(payload)}."


class FakeResponse:
    def __init__(self, payload: dict[str, object], *, status: int = 200) -> None:
        self.status = status
        self._body = json.dumps(payload).encode("utf-8")

    def __enter__(self) -> "FakeResponse":
        return self

    def __exit__(self, *args: object) -> None:
        return None

    def read(self, _: int = -1) -> bytes:
        return self._body


def _authorization(*, expires_in: int = 900) -> XAIDeviceAuthorization:
    return XAIDeviceAuthorization(
        device_code="device-secret",
        user_code="ABCD-EFGH",
        verification_uri="https://auth.x.ai/activate",
        verification_uri_complete="https://auth.x.ai/activate?user_code=ABCD-EFGH",
        expires_in=expires_in,
        interval=1,
        token_endpoint="https://auth.x.ai/oauth2/token",
        device_authorization_endpoint="https://auth.x.ai/oauth2/device/code",
        requested_at_epoch_seconds=time.time(),
    )


def test_env_token_precedence_and_file_fallback_do_not_use_xai_api_key(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    token_file = tmp_path / "xai-token"
    token_file.write_text(_jwt() + "\n", encoding="utf-8")
    monkeypatch.setenv("XAI_OAUTH_ACCESS_TOKEN_FILE", str(token_file))
    monkeypatch.setenv("XAI_API_KEY", "must-not-be-used-for-oauth")

    credentials = resolve_xai_oauth_credentials({"auth_store_path": str(tmp_path / "missing.json")})

    assert credentials.source == "XAI_OAUTH_ACCESS_TOKEN"
    assert credentials.api_key != "must-not-be-used-for-oauth"


def test_xai_urls_are_pinned_to_https_xai_origin() -> None:
    assert validate_xai_url("https://api.x.ai/v1", field="base_url") == "https://api.x.ai/v1"
    assert validate_xai_url("https://staging.api.x.ai/v1/", field="base_url") == "https://staging.api.x.ai/v1"
    for invalid in (
        "http://api.x.ai/v1",
        "https://x.ai.attacker.example/v1",
        "https://attacker.example/v1",
        "https://user:secret@api.x.ai/v1",
    ):
        with pytest.raises(XAIOAuthError, match="xAI OAuth"):
            validate_xai_url(invalid, field="base_url")


def test_device_authorization_uses_discovery_and_returns_browser_safe_subset(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    calls: list[object] = []
    responses = iter(
        [
            FakeResponse(
                {
                    "issuer": "https://auth.x.ai",
                    "token_endpoint": "https://auth.x.ai/oauth2/token",
                    "device_authorization_endpoint": "https://auth.x.ai/oauth2/device/code",
                }
            ),
            FakeResponse(
                {
                    "device_code": "server-only-device-code",
                    "user_code": "ABCD-EFGH",
                    "verification_uri": "https://auth.x.ai/activate",
                    "verification_uri_complete": "https://auth.x.ai/activate?user_code=ABCD-EFGH",
                    "expires_in": 900,
                    "interval": 3,
                }
            ),
        ]
    )

    def fake_urlopen(request: object, timeout: float) -> FakeResponse:
        calls.append(request)
        assert timeout >= 5
        return next(responses)

    monkeypatch.setattr("custom_agents.xai_oauth_auth.urllib_request.urlopen", fake_urlopen)

    authorization = request_xai_device_authorization(instance_root=tmp_path)

    assert authorization.token_endpoint == "https://auth.x.ai/oauth2/token"
    assert authorization.interval == 3
    public = authorization.public_payload()
    assert public["user_code"] == "ABCD-EFGH"
    assert "device_code" not in public
    assert "access_token" not in public
    device_request = calls[1]
    assert f"client_id={XAI_OAUTH_CLIENT_ID}".encode() in device_request.data


def test_device_poll_handles_pending_slow_down_and_rotated_refresh_token(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    pending = io.BytesIO(b'{"error":"authorization_pending"}')
    slow_down = io.BytesIO(b'{"error":"slow_down"}')
    responses: list[object] = [
        urllib_error.HTTPError(
            url="https://auth.x.ai/oauth2/token",
            code=400,
            msg="pending",
            hdrs={},
            fp=pending,
        ),
        urllib_error.HTTPError(
            url="https://auth.x.ai/oauth2/token",
            code=400,
            msg="slow",
            hdrs={},
            fp=slow_down,
        ),
        FakeResponse(
            {
                "access_token": _jwt(),
                "refresh_token": "rotated-refresh",
                "expires_in": 900,
            }
        ),
    ]
    submitted: list[bytes] = []

    def fake_urlopen(request: object, timeout: float) -> FakeResponse:
        submitted.append(request.data)
        response = responses.pop(0)
        if isinstance(response, Exception):
            raise response
        return response

    monkeypatch.setattr("custom_agents.xai_oauth_auth.urllib_request.urlopen", fake_urlopen)
    monkeypatch.setattr("custom_agents.xai_oauth_auth._wait_for_poll_interval", lambda *args: None)

    tokens = poll_xai_device_token(_authorization())

    assert tokens["refresh_token"] == "rotated-refresh"
    assert f"grant_type={XAI_OAUTH_DEVICE_GRANT.replace(':', '%3A')}".encode() in submitted[0]
    assert b"device_code=device-secret" in submitted[0]


def test_device_tokens_use_owned_store_permissions_and_opaque_expiry_metadata(tmp_path: Path) -> None:
    store_path = tmp_path / "instance" / "auth" / "xai.json"
    saved = save_xai_oauth_device_tokens(
        _authorization(),
        {
            "access_token": "opaque-access-token",
            "refresh_token": "opaque-refresh-token",
            "expires_in": 900,
            "token_type": "Bearer",
        },
        {"auth_store_path": str(store_path)},
    )

    payload = json.loads(saved.read_text(encoding="utf-8"))
    assert payload["provider"] == "xai-oauth"
    assert payload["auth_mode"] == "oauth_device_code"
    assert payload["tokens"]["expires_at_epoch_seconds"] > payload["tokens"]["obtained_at_epoch_seconds"]
    if os.name != "nt":
        assert stat.S_IMODE(saved.stat().st_mode) == 0o600
        assert stat.S_IMODE(saved.parent.stat().st_mode) == 0o700


def test_owned_store_refreshes_once_under_concurrent_resolution(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    store_path = save_xai_oauth_device_tokens(
        _authorization(),
        {"access_token": _jwt(exp_offset_seconds=-10), "refresh_token": "old-refresh"},
        {"auth_store_path": str(tmp_path / "auth.json")},
    )
    refresh_calls = 0

    def fake_refresh(tokens: dict[str, object], **_: object) -> dict[str, object]:
        nonlocal refresh_calls
        refresh_calls += 1
        time.sleep(0.05)
        return {"access_token": _jwt(exp_offset_seconds=7200), "refresh_token": "new-refresh"}

    monkeypatch.setattr("custom_agents.xai_oauth_auth.refresh_xai_oauth_tokens", fake_refresh)
    settings = {"auth_store_path": str(store_path), "refresh_skew_seconds": 120}
    with ThreadPoolExecutor(max_workers=2) as executor:
        credentials = list(executor.map(lambda _: resolve_xai_oauth_credentials(settings), range(2)))

    assert refresh_calls == 1
    assert {credential.api_key for credential in credentials} == {
        json.loads(store_path.read_text(encoding="utf-8"))["tokens"]["access_token"]
    }
    assert json.loads(store_path.read_text(encoding="utf-8"))["tokens"]["refresh_token"] == "new-refresh"


def _refuse_file_locks(monkeypatch: pytest.MonkeyPatch) -> None:
    def refuse(_descriptor: int, _operation: int) -> None:
        raise OSError(errno.ENOLCK, os.strerror(errno.ENOLCK))

    monkeypatch.setattr("file_locking.fcntl.flock", refuse)


def test_lock_failure_stops_refresh_and_status_reports_it(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """A rotating refresh token is never spent without the store's lock."""

    store_path = save_xai_oauth_device_tokens(
        _authorization(),
        {"access_token": _jwt(exp_offset_seconds=-10), "refresh_token": "old-refresh"},
        {"auth_store_path": str(tmp_path / "auth.json")},
    )
    original = store_path.read_text(encoding="utf-8")

    def fail_refresh(*args: object, **kwargs: object) -> None:
        raise AssertionError("the refresh must not run without the store's lock")

    monkeypatch.setattr("custom_agents.xai_oauth_auth.refresh_xai_oauth_tokens", fail_refresh)
    _refuse_file_locks(monkeypatch)
    settings = {"auth_store_path": str(store_path), "refresh_skew_seconds": 120}

    with pytest.raises(XAIOAuthError) as raised:
        resolve_xai_oauth_credentials(settings)
    status = xai_oauth_status(settings)

    assert raised.value.code == "xai_oauth_auth_store_lock_unavailable"
    assert raised.value.relogin_required is False
    assert isinstance(raised.value.__cause__, FileLockUnavailable)
    assert f"{store_path}.lock" in str(raised.value)
    assert "supports file locks" in str(raised.value)
    assert status["authenticated"] is False
    assert status["error_code"] == "xai_oauth_auth_store_lock_unavailable"
    assert status["relogin_required"] is False
    assert store_path.read_text(encoding="utf-8") == original


def test_device_tokens_are_not_saved_when_the_store_cannot_be_locked(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    store_path = tmp_path / "instance" / "auth" / "xai.json"
    _refuse_file_locks(monkeypatch)

    with pytest.raises(XAIOAuthError) as raised:
        save_xai_oauth_device_tokens(
            _authorization(),
            {"access_token": "opaque-access-token", "refresh_token": "opaque-refresh-token"},
            {"auth_store_path": str(store_path)},
        )

    assert raised.value.code == "xai_oauth_auth_store_lock_unavailable"
    assert not store_path.exists()


@pytest.mark.parametrize(
    ("configured_path", "expected_relative_path"),
    [
        (None, Path("auth/xai_oauth_auth.json")),
        ("auth/custom_xai.json", Path("auth/custom_xai.json")),
    ],
)
def test_runtime_resolves_owned_store_from_declared_instance_root(
    configured_path: str | None,
    expected_relative_path: Path,
    tmp_path: Path,
) -> None:
    instance_root = tmp_path / "data"
    config = GatewayInstanceConfig.create(
        instance_name="xai-bundle-test",
        instance_root=str(instance_root),
        team_id="team_xai_bundle",
        timezone="America/Chicago",
    )
    config.llm.provider = "xai-oauth"
    config.llm.model_id = "grok-4.5"
    config.llm.xai_oauth.auth_store_path = configured_path
    config.save()
    store_path = save_xai_oauth_device_tokens(
        _authorization(),
        {
            "access_token": _jwt(exp_offset_seconds=7200),
            "refresh_token": "bundle-refresh-token",
            "expires_in": 7200,
        },
        {"auth_store_path": configured_path},
        instance_root=instance_root,
    )

    # Every process, the bundle's included, runs from the saved file.
    loaded_runtime = GatewayInstanceConfig.load_for_process(config.config_path, env={})
    runtime_config = build_runtime_config_from_instance_config(loaded_runtime)
    runtime_settings = resolve_agent_type_settings(runtime_config, "TeamArchitect")
    resolved_root = resolve_runtime_instance_root(
        bootstrap_config=runtime_settings["bootstrap_config"],
        instance_config_path=runtime_settings["instance_config_path"],
        workspace_dir=runtime_settings["workspace_dir"],
        env={},
    )

    credentials = resolve_xai_oauth_credentials(
        runtime_settings["xai_oauth"],
        instance_root=resolved_root,
        env={},
    )

    assert Path(resolved_root or "") == instance_root.resolve()
    assert store_path == (instance_root / expected_relative_path).resolve()
    assert credentials.source == "auth_store"
    assert Path(credentials.auth_store_path or "") == store_path


def test_short_lived_tokens_use_adaptive_refresh_skew() -> None:
    assert _proactive_refresh_skew_seconds({"access_token": _jwt(exp_offset_seconds=15 * 60)}, 3600) == 120
    assert _proactive_refresh_skew_seconds({"access_token": _jwt(exp_offset_seconds=4 * 60 * 60)}, 3600) == 3600


def test_refresh_403_is_tier_denial_not_relogin(monkeypatch: pytest.MonkeyPatch) -> None:
    def fake_urlopen(request: object, timeout: float) -> object:
        raise urllib_error.HTTPError(
            url="https://auth.x.ai/oauth2/token",
            code=403,
            msg="forbidden",
            hdrs={},
            fp=io.BytesIO(b'{"error":"access_denied","error_description":"tier not enabled"}'),
        )

    monkeypatch.setattr("custom_agents.xai_oauth_auth.urllib_request.urlopen", fake_urlopen)

    with pytest.raises(XAIOAuthError) as exc_info:
        refresh_xai_oauth_tokens(
            {"refresh_token": "still-valid"},
            token_endpoint="https://auth.x.ai/oauth2/token",
        )

    assert exc_info.value.code == "xai_oauth_tier_denied"
    assert exc_info.value.relogin_required is False


def test_status_never_returns_bearer_material(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setenv("XAI_OAUTH_ACCESS_TOKEN", "opaque-super-secret-token")

    payload = xai_oauth_status({"auth_store_path": str(tmp_path / "missing.json")})

    assert payload["authenticated"] is True
    assert "opaque-super-secret-token" not in json.dumps(payload)
    assert "api_key" not in payload


@pytest.mark.asyncio
async def test_async_api_key_provider_resolves_each_request(monkeypatch: pytest.MonkeyPatch) -> None:
    resolved = iter(("first-token", "second-token"))

    def fake_resolve(*args: object, **kwargs: object) -> XAIOAuthCredentials:
        return XAIOAuthCredentials(
            provider="xai-oauth",
            base_url="https://api.x.ai/v1",
            api_key=next(resolved),
            source="test",
            auth_mode="oauth_device_code",
        )

    monkeypatch.setattr("custom_agents.xai_oauth_auth.resolve_xai_oauth_credentials", fake_resolve)
    provider = make_xai_oauth_api_key_provider()

    assert await provider() == "first-token"
    assert await provider() == "second-token"


def test_model_factory_uses_responses_grok_45_and_provider_parameters(
    caplog: pytest.LogCaptureFixture,
) -> None:
    with caplog.at_level("WARNING", logger="custom_agents.strands_model_factory"):
        model = build_strands_model(
            StrandsModelFactoryConfig(
                provider="grok-oauth",
                model_id="",
                temperature=0.2,
                reasoning_effort="ultra",
                max_output_tokens=512,
                context_window_tokens=None,
                xai_oauth={"base_url": "https://api.x.ai/v1"},
            )
        )

    assert model.config["model_id"] == "grok-4.5"
    assert model.config["context_window_limit"] == 500000
    assert model.config["params"] == {
        "temperature": 0.2,
        "max_output_tokens": 512,
        "reasoning": {"effort": "high"},
    }
    assert model.config["stateful"] is False
    assert model.client_args["base_url"] == "https://api.x.ai/v1"
    assert asyncio.iscoroutinefunction(model.client_args["api_key"])
    assert "event=xai_oauth_reasoning_effort_mapped" in caplog.text


def test_model_factory_omits_reasoning_for_unverified_grok_model(
    caplog: pytest.LogCaptureFixture,
) -> None:
    with caplog.at_level("WARNING", logger="custom_agents.strands_model_factory"):
        model = build_strands_model(
            StrandsModelFactoryConfig(
                provider="xai-oauth",
                model_id="grok-legacy",
                reasoning_effort="medium",
                context_window_tokens=123456,
                xai_oauth={"base_url": "https://api.x.ai/v1"},
            )
        )

    assert model.config["context_window_limit"] == 123456
    assert model.config["params"] == {}
    assert "event=xai_oauth_reasoning_effort_omitted" in caplog.text


def test_cli_resolves_pre_onboarding_instance_root_and_auth_override(tmp_path: Path) -> None:
    root = tmp_path / "future-instance"
    override = tmp_path / "host-auth" / "xai.json"
    settings, instance_root, metadata = _resolve_xai_oauth_command_settings(
        argparse.Namespace(
            config=None,
            instance_root=str(root),
            env=None,
            auth_store_path=str(override),
            host_data_root=None,
            container_data_root="/data",
        )
    )

    assert instance_root == str(root.resolve())
    assert settings.auth_store_path == str(override.resolve())
    assert metadata["auth_store_path_source"] == "command_override"


def test_xai_network_timeout_uses_structured_auth_error(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        "custom_agents.xai_oauth_auth.urllib_request.urlopen",
        lambda *args, **kwargs: (_ for _ in ()).throw(TimeoutError("timed out")),
    )

    with pytest.raises(XAIOAuthError) as exc_info:
        request_xai_device_authorization()

    assert exc_info.value.code == "xai_oauth_discovery_network_error"
    assert "timed out" in str(exc_info.value)


def test_xai_settings_reject_non_xai_environment_override(tmp_path: Path) -> None:
    with pytest.raises(XAIOAuthError, match="must use HTTPS on x.ai"):
        resolve_xai_oauth_settings(
            {"auth_store_path": str(tmp_path / "auth.json")},
            env={"XAI_OAUTH_BASE_URL": "https://attacker.example/v1"},
        )
