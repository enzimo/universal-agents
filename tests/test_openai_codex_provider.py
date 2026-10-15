from __future__ import annotations

import base64
import io
import json
import logging
import os
import stat
import time
from pathlib import Path
from urllib import error as urllib_error

import pytest

from assistant_cli import (
    _map_container_path_to_host,
)
from custom_agents.openai_codex_auth import (
    OpenAICodexAuthError,
    OpenAICodexCredentials,
    OpenAICodexSettings,
    codex_backend_headers,
    import_shared_codex_auth,
    refresh_openai_codex_tokens,
    resolve_openai_codex_credentials,
)
from custom_agents.openai_codex_models import (
    DEFAULT_CODEX_MODELS,
    OpenAICodexModelDiscoverySettings,
    get_codex_model_ids,
)
from custom_agents.openai_reasoning_responses_model import OpenAIReasoningResponsesModel
from custom_agents.strands_model_factory import (
    StrandsModelFactoryConfig,
    build_strands_model,
)


def _jwt(*, exp_offset_seconds: int = 3600, account_id: str = "acct_123") -> str:
    header = {"alg": "none", "typ": "JWT"}
    payload = {
        "exp": int(time.time()) + exp_offset_seconds,
        "https://api.openai.com/auth": {"chatgpt_account_id": account_id},
    }

    def encode(value: dict[str, object]) -> str:
        raw = json.dumps(value, separators=(",", ":")).encode("utf-8")
        return base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")

    return f"{encode(header)}.{encode(payload)}."


def _codex_auth_payload(access_token: str, refresh_token: str = "refresh-token") -> str:
    return json.dumps(
        {"tokens": {"access_token": access_token, "refresh_token": refresh_token}}
    )


def test_env_token_precedence_and_headers(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    env_token = _jwt(account_id="acct_env")
    store_token = _jwt(account_id="acct_store")
    auth_store = tmp_path / "auth.json"
    auth_store.write_text(_codex_auth_payload(store_token), encoding="utf-8")
    monkeypatch.setenv("OPENAI_CODEX_ACCESS_TOKEN", env_token)

    credentials = resolve_openai_codex_credentials({"auth_store_path": str(auth_store)})

    assert credentials.source == "OPENAI_CODEX_ACCESS_TOKEN"
    assert credentials.api_key == env_token
    assert credentials.default_headers["ChatGPT-Account-ID"] == "acct_env"
    assert codex_backend_headers("not-a-jwt")["originator"] == "codex_cli_rs"


def test_codex_access_token_file_fallback(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    token_file = tmp_path / "token.txt"
    token = _jwt(account_id="acct_file")
    token_file.write_text(token + "\n", encoding="utf-8")
    monkeypatch.setenv("CODEX_ACCESS_TOKEN_FILE", str(token_file))

    credentials = resolve_openai_codex_credentials(
        {"auth_store_path": str(tmp_path / "missing.json")}
    )

    assert credentials.source == "CODEX_ACCESS_TOKEN"
    assert credentials.api_key == token
    assert credentials.default_headers["ChatGPT-Account-ID"] == "acct_file"


def test_import_shared_codex_auth_writes_owned_store_without_mutating_shared(
    tmp_path: Path,
) -> None:
    shared_home = tmp_path / "codex-home"
    shared_home.mkdir()
    source_token = _jwt()
    shared_auth = shared_home / "auth.json"
    original = _codex_auth_payload(source_token, "source-refresh")
    shared_auth.write_text(original, encoding="utf-8")

    target = import_shared_codex_auth(
        codex_home=shared_home,
        raw_settings={"auth_store_path": "auth/openai_codex_auth.json"},
        instance_root=tmp_path / "instance",
    )

    assert target == tmp_path / "instance" / "auth" / "openai_codex_auth.json"
    assert shared_auth.read_text(encoding="utf-8") == original
    saved = json.loads(target.read_text(encoding="utf-8"))
    assert saved["provider"] == "openai-codex"
    assert saved["tokens"]["access_token"] == source_token
    if os.name != "nt":
        assert stat.S_IMODE(target.stat().st_mode) == 0o600


def test_import_shared_codex_auth_can_target_translated_host_store(
    tmp_path: Path,
) -> None:
    shared_home = tmp_path / "codex-home"
    shared_home.mkdir()
    source_token = _jwt()
    shared_auth = shared_home / "auth.json"
    shared_auth.write_text(
        _codex_auth_payload(source_token, "source-refresh"), encoding="utf-8"
    )
    host_target = tmp_path / "host-data" / "auth" / "openai_codex_auth.json"

    target = import_shared_codex_auth(
        codex_home=shared_home,
        raw_settings=OpenAICodexSettings(
            auth_store_path="/data/auth/openai_codex_auth.json"
        ),
        target_auth_store_path=host_target,
    )

    assert target == host_target.resolve()
    saved = json.loads(host_target.read_text(encoding="utf-8"))
    assert saved["tokens"]["access_token"] == source_token
    assert saved["source"] == f"codex_cli_import:{shared_auth.resolve()}"


def test_openai_codex_command_rejects_container_path_outside_mounted_root(
    tmp_path: Path,
) -> None:
    with pytest.raises(ValueError, match="not under container data root"):
        _map_container_path_to_host(
            "/other/auth/openai_codex_auth.json",
            host_data_root=str(tmp_path / "docker-instance"),
            container_data_root="/data",
        )


def test_openai_codex_command_rejects_container_path_traversal(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="escapes host data root"):
        _map_container_path_to_host(
            "/data/../outside/openai_codex_auth.json",
            host_data_root=str(tmp_path / "docker-instance"),
            container_data_root="/data",
        )


def test_shared_codex_auth_is_read_only_and_never_refreshed(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    shared_home = tmp_path / "codex-home"
    shared_home.mkdir()
    shared_auth = shared_home / "auth.json"
    shared_token = _jwt(exp_offset_seconds=10)
    original = _codex_auth_payload(shared_token, "source-refresh")
    shared_auth.write_text(original, encoding="utf-8")

    def fail_refresh(*args: object, **kwargs: object) -> None:
        raise AssertionError("shared Codex auth must not be refreshed")

    monkeypatch.setattr(
        "custom_agents.openai_codex_auth.refresh_openai_codex_tokens", fail_refresh
    )

    credentials = resolve_openai_codex_credentials(
        {
            "auth_store_path": str(tmp_path / "owned.json"),
            "shared_codex_home": str(shared_home),
            "refresh_skew_seconds": 120,
        }
    )

    assert credentials.source == f"shared_codex_auth:{shared_auth.resolve()}"
    assert credentials.api_key == shared_token
    assert shared_auth.read_text(encoding="utf-8") == original


def test_owned_auth_store_refreshes_expiring_token(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    auth_store = tmp_path / "auth.json"
    auth_store.write_text(
        _codex_auth_payload(_jwt(exp_offset_seconds=10), "old-refresh"),
        encoding="utf-8",
    )
    refreshed_token = _jwt(account_id="acct_new")

    class FakeResponse:
        status = 200

        def __enter__(self) -> "FakeResponse":
            return self

        def __exit__(self, *args: object) -> None:
            return None

        def read(self, _: int = -1) -> bytes:
            return json.dumps(
                {"access_token": refreshed_token, "refresh_token": "new-refresh"}
            ).encode("utf-8")

    def fake_urlopen(request: object, timeout: float) -> FakeResponse:
        assert timeout >= 5
        return FakeResponse()

    monkeypatch.setattr(
        "custom_agents.openai_codex_auth.urllib_request.urlopen", fake_urlopen
    )

    credentials = resolve_openai_codex_credentials(
        {"auth_store_path": str(auth_store), "refresh_skew_seconds": 120}
    )

    assert credentials.api_key == refreshed_token
    saved = json.loads(auth_store.read_text(encoding="utf-8"))
    assert saved["tokens"]["refresh_token"] == "new-refresh"


def test_refresh_token_reused_is_relogin_required(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    body = json.dumps(
        {
            "error": {
                "code": "refresh_token_reused",
                "message": "refresh token reused",
            }
        }
    ).encode("utf-8")

    def fake_urlopen(request: object, timeout: float) -> object:
        raise urllib_error.HTTPError(
            url="https://auth.openai.com/oauth/token",
            code=400,
            msg="bad request",
            hdrs={},
            fp=io.BytesIO(body),
        )

    monkeypatch.setattr(
        "custom_agents.openai_codex_auth.urllib_request.urlopen", fake_urlopen
    )

    with pytest.raises(OpenAICodexAuthError) as exc_info:
        refresh_openai_codex_tokens({"refresh_token": "consumed"})

    assert exc_info.value.code == "refresh_token_reused"
    assert exc_info.value.relogin_required is True


def test_refresh_rate_limit_is_retryable(monkeypatch: pytest.MonkeyPatch) -> None:
    def fake_urlopen(request: object, timeout: float) -> object:
        raise urllib_error.HTTPError(
            url="https://auth.openai.com/oauth/token",
            code=429,
            msg="too many requests",
            hdrs={},
            fp=io.BytesIO(b'{"error":"rate_limited"}'),
        )

    monkeypatch.setattr(
        "custom_agents.openai_codex_auth.urllib_request.urlopen", fake_urlopen
    )

    with pytest.raises(OpenAICodexAuthError) as exc_info:
        refresh_openai_codex_tokens({"refresh_token": "still-valid"})

    assert exc_info.value.code == "openai_codex_refresh_rate_limited"
    assert exc_info.value.relogin_required is False


def test_model_discovery_filters_hidden_keeps_supported_in_api_false(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    payload = {
        "models": [
            {"slug": "hidden-model", "visibility": "hidden", "priority": 0},
            {"slug": "visible-low", "visibility": "visible", "priority": 20},
            {
                "slug": "visible-high",
                "visibility": "visible",
                "priority": 1,
                "supported_in_api": False,
            },
        ]
    }

    class FakeResponse:
        def __enter__(self) -> "FakeResponse":
            return self

        def __exit__(self, *args: object) -> None:
            return None

        def read(self, _: int = -1) -> bytes:
            return json.dumps(payload).encode("utf-8")

    monkeypatch.setattr(
        "custom_agents.openai_codex_models.urllib_request.urlopen",
        lambda request, timeout: FakeResponse(),
    )
    settings = OpenAICodexModelDiscoverySettings(
        cache_path=str(tmp_path / "models.json")
    )

    models = get_codex_model_ids(settings, access_token=_jwt())

    assert models[:2] == ["visible-high", "visible-low"]
    assert "hidden-model" not in models
    assert (tmp_path / "models.json").exists()


def test_model_discovery_uses_cache_when_live_fails_without_static_injection(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    cache_path = tmp_path / "models.json"
    cache_path.write_text(
        json.dumps(
            {
                "fetched_at_epoch_seconds": time.time(),
                "models": ["cached-new-model", "cached-older-model"],
            }
        ),
        encoding="utf-8",
    )

    def fail_urlopen(request: object, timeout: float) -> object:
        raise urllib_error.URLError("offline")

    monkeypatch.setattr(
        "custom_agents.openai_codex_models.urllib_request.urlopen", fail_urlopen
    )
    settings = OpenAICodexModelDiscoverySettings(cache_path=str(cache_path))

    assert get_codex_model_ids(settings, access_token=_jwt()) == [
        "cached-new-model",
        "cached-older-model",
    ]


def test_model_discovery_static_defaults_when_sources_unavailable(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    monkeypatch.setenv("CODEX_HOME", str(tmp_path / "empty-codex-home"))
    monkeypatch.setenv("HOME", str(tmp_path / "empty-home"))
    settings = OpenAICodexModelDiscoverySettings(
        cache_path=str(tmp_path / "missing.json")
    )

    assert get_codex_model_ids(settings, access_token=None) == DEFAULT_CODEX_MODELS


def test_openai_codex_model_factory_uses_responses_adapter(
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    fake_credentials = OpenAICodexCredentials(
        provider="openai-codex",
        base_url="https://chatgpt.com/backend-api/codex",
        api_key="access-token",
        source="test",
        auth_mode="chatgpt",
        default_headers={"originator": "codex_cli_rs"},
    )
    monkeypatch.setattr(
        "custom_agents.strands_model_factory.resolve_openai_codex_credentials",
        lambda *args, **kwargs: fake_credentials,
    )

    with caplog.at_level(logging.WARNING, logger="custom_agents.strands_model_factory"):
        model = build_strands_model(
            StrandsModelFactoryConfig(
                provider="openai-codex",
                model_id="gpt-5.6-luna",
                temperature=0.0,
                reasoning_effort="xhigh",
                reasoning_summary="detailed",
                max_output_tokens=128,
                context_window_tokens=1_050_000,
            )
        )

    assert isinstance(model, OpenAIReasoningResponsesModel)
    assert model.config["model_id"] == "gpt-5.6-luna"
    assert model.config["context_window_limit"] == 1_050_000
    assert model.config["params"] == {
        "include": ["reasoning.encrypted_content"],
        "reasoning": {"effort": "xhigh", "summary": "detailed"},
    }
    assert model.client_args["base_url"] == "https://chatgpt.com/backend-api/codex"
    assert model.client_args["default_headers"]["originator"] == "codex_cli_rs"
    assert "event=openai_codex_unsupported_parameters_omitted" in caplog.text
    assert "parameter_names=temperature,max_output_tokens" in caplog.text
