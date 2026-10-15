from __future__ import annotations

from types import SimpleNamespace

import litellm

from custom_agents import model_capability_checks as checks


class _FakeResponse:
    def __init__(self, payload):
        self._payload = payload

    def raise_for_status(self):
        return None

    def json(self):
        return self._payload


class _FakeOpenRouterSession:
    def __init__(self, payload):
        self.payload = payload
        self.calls = []

    def get(self, url, *, params=None, timeout=None):
        self.calls.append({"url": url, "params": params, "timeout": timeout})
        return _FakeResponse(self.payload)


def test_openrouter_capability_check_parses_input_modalities() -> None:
    session = _FakeOpenRouterSession(
        {
            "data": [
                {
                    "id": "openai/gpt-4o",
                    "architecture": {
                        "modality": "text+image+file->text",
                        "input_modalities": ["text", "image", "file"],
                        "output_modalities": ["text"],
                    },
                }
            ]
        }
    )

    result = checks.check_openrouter_model_capabilities(
        "openrouter/openai/gpt-4o", session=session
    )

    assert result["status"] == "found"
    assert result["supports_image_input"] is True
    assert result["supports_audio_input"] is False
    assert result["supports_file_input"] is True
    assert session.calls[0]["params"] == {"output_modalities": "all"}


def test_litellm_capability_check_uses_helper_fallbacks(monkeypatch) -> None:
    monkeypatch.setattr(
        litellm,
        "get_model_info",
        lambda *, model: {"supports_vision": None, "supports_audio_input": None},
    )
    monkeypatch.setattr(litellm, "supports_vision", lambda *, model: True)
    monkeypatch.setattr(litellm, "supports_audio_input", lambda *, model: False)

    result = checks.check_litellm_model_capabilities("mapped/model")

    assert result["status"] == "found"
    assert result["supports_image_input"] is True
    assert result["supports_audio_input"] is False
    assert any("supports_vision()" in item for item in result["evidence"])


def test_huggingface_capability_check_uses_pipeline_and_tags() -> None:
    api = SimpleNamespace(
        model_info=lambda repo_id: SimpleNamespace(
            pipeline_tag="any-to-any",
            tags=["transformers", "multimodal", "audio-to-text"],
        )
    )

    result = checks.check_huggingface_model_capabilities(
        "Qwen/Qwen2.5-Omni-7B", api=api
    )

    assert result["status"] == "found"
    assert result["supports_image_input"] is True
    assert result["supports_audio_input"] is True
    assert result["source"]["repo_id"] == "Qwen/Qwen2.5-Omni-7B"


def test_lookup_aggregates_consensus_and_feedback_options(monkeypatch) -> None:
    monkeypatch.setattr(
        checks,
        "check_openrouter_model_capabilities",
        lambda model_id, timeout_seconds=10.0: {
            "method": "openrouter",
            "status": "found",
            "supports_image_input": True,
            "supports_audio_input": False,
            "supports_file_input": True,
            "confidence": "high",
            "evidence": ["OpenRouter input modalities: text, image."],
            "source": {"model_id": model_id},
            "raw": {},
        },
    )
    monkeypatch.setattr(
        checks,
        "check_litellm_model_capabilities",
        lambda model_id: {
            "method": "litellm",
            "status": "not_found",
            "supports_image_input": None,
            "supports_audio_input": None,
            "supports_file_input": None,
            "confidence": "none",
            "evidence": [],
            "source": {"model_id": model_id},
            "raw": {},
        },
    )

    payload = checks.lookup_model_capability_candidates(
        provider="litellm",
        model_id="openrouter/openai/gpt-4o",
        methods="openrouter,litellm",
    )

    assert payload["consensus"]["overall_status"] == "candidate"
    assert payload["consensus"]["supports_image_input"] is True
    assert payload["consensus"]["supports_audio_input"] is False
    assert payload["consensus"]["supports_file_input"] is True
    option_ids = {option["id"] for option in payload["user_feedback_options"]}
    assert "accept_lookup_recommendation" in option_ids
    assert "keep_unknown" in option_ids
    assert payload["write_policy"].startswith("Read-only lookup")


def test_huggingface_check_uses_the_linked_account_lookup() -> None:
    looked_up = []

    def linked(repo_id: str) -> checks.HubModelInfo:
        looked_up.append(repo_id)
        return checks.HubModelInfo(pipeline_tag="image-text-to-text", tags=("vision",))

    result = checks.check_huggingface_model_capabilities("org/gated-model", account_lookup=linked)

    assert looked_up == ["org/gated-model"]
    assert result["status"] == "found"
    assert result["supports_image_input"] is True
    assert result["source"]["account"] == "linked"


def test_huggingface_check_without_an_account_says_so_and_never_uses_an_ambient_token(monkeypatch) -> None:
    import huggingface_hub

    monkeypatch.setenv("HF_TOKEN", "ambient-canary")
    seen = {}

    class AnonymousApi:
        def __init__(self, *, token):
            seen["token"] = token

        def model_info(self, repo_id):
            return SimpleNamespace(pipeline_tag="automatic-speech-recognition", tags=[])

    def unavailable(repo_id: str) -> checks.HubModelInfo:
        raise checks.HuggingFaceAccountUnavailable("account_link_missing")

    monkeypatch.setattr(huggingface_hub, "HfApi", AnonymousApi)
    result = checks.check_huggingface_model_capabilities("org/public-model", account_lookup=unavailable)

    assert seen["token"] is False
    assert result["supports_audio_input"] is True
    assert result["source"]["account"] == "none"
    assert any("without a Hugging Face account (account_link_missing)" in item for item in result["evidence"])


def test_the_team_leads_lookup_goes_through_the_broker(monkeypatch) -> None:
    import json

    from custom_tools import secure_cli_tool
    from custom_tools.providers import runtime_settings

    requests = []

    def broker(provider, action, params, reason):
        requests.append((provider, action, params))
        if params["repo_id"] == "org/missing-link":
            payload = {"http_status": 403, "reason_code": "account_link_missing", "error": "No account is linked"}
            return {"status": "error", "content": [{"text": "No account is linked"}, {"text": json.dumps(payload)}]}
        payload = {"status": "success", "content": {"pipeline_tag": "any-to-any", "tags": ["multimodal"]}}
        return {"status": "success", "content": [{"text": "done"}, {"text": json.dumps(payload)}]}

    monkeypatch.setattr(secure_cli_tool, "_secure_cli_impl", broker)

    info = runtime_settings._huggingface_model_info_through_broker("org/model")
    assert info == checks.HubModelInfo(pipeline_tag="any-to-any", tags=("multimodal",))
    assert requests[0] == ("huggingface", "model.info", {"repo_id": "org/model"})
    try:
        runtime_settings._huggingface_model_info_through_broker("org/missing-link")
    except checks.HuggingFaceAccountUnavailable as exc:
        assert exc.reason == "account_link_missing"
    else:
        raise AssertionError("a refused lookup must say why")


class _FakeProxySession:
    """A LiteLLM server's ``/model/info`` answer, recording the request."""

    def __init__(self, payload=None, error: Exception | None = None):
        self.payload = payload
        self.error = error
        self.calls = []

    def get(self, url, *, headers=None, timeout=None):
        self.calls.append({"url": url, "headers": headers, "timeout": timeout})
        if self.error:
            raise self.error
        return _FakeResponse(self.payload)


_PROXY_MODELS = {
    "data": [
        {
            "model_name": "gpt-6-luna",
            "litellm_params": {"model": "openai/gpt-6-luna", "api_base": "https://upstream.example", "api_key": "sk-***"},
            "model_info": {
                "max_input_tokens": 400000,
                "max_output_tokens": 128000,
                "supports_vision": True,
                "supports_pdf_input": True,
                "supports_audio_input": False,
                "supports_reasoning": True,
                "mode": "chat",
            },
        },
        {"model_name": "cheap", "litellm_params": {"model": "deepseek/deepseek-v4.1-flash"}, "model_info": {}},
    ]
}


def test_the_litellm_server_lookup_reads_the_models_it_routes_and_never_returns_its_key() -> None:
    session = _FakeProxySession(_PROXY_MODELS)

    result = checks.check_litellm_proxy_model_info(
        "gpt-6-luna", api_base="https://litellm.example/v1/", api_key="sk-team-canary", session=session
    )

    assert session.calls[0]["url"] == "https://litellm.example/v1/model/info"
    assert session.calls[0]["headers"] == {"Authorization": "Bearer sk-team-canary"}
    assert result["status"] == "found"
    assert result["context_window_tokens"] == 400000
    assert result["supports_reasoning"] is True
    assert (result["supports_image_input"], result["supports_audio_input"], result["supports_file_input"]) == (
        True,
        False,
        True,
    )
    assert result["raw"]["routed_model"] == "openai/gpt-6-luna"
    text = str(result)
    assert "sk-team-canary" not in text and "upstream.example" not in text and "sk-***" not in text


def test_the_litellm_server_lookup_also_finds_a_model_by_the_model_it_routes_to() -> None:
    result = checks.check_litellm_proxy_model_info(
        "deepseek/deepseek-v4.1-flash", api_base="https://litellm.example", session=_FakeProxySession(_PROXY_MODELS)
    )

    assert result["status"] == "found"
    assert result["raw"]["model_name"] == "cheap"
    assert result["supports_image_input"] is None


def test_the_litellm_server_lookup_fails_without_its_key_in_the_error(caplog) -> None:
    error = RuntimeError("401 for https://litellm.example/model/info with Bearer sk-team-canary")
    result = checks.check_litellm_proxy_model_info(
        "gpt-6-luna", api_base="https://litellm.example", api_key="sk-team-canary", session=_FakeProxySession(error=error)
    )
    skipped = checks.check_litellm_proxy_model_info("gpt-6-luna", api_base="")

    assert result["status"] == "error"
    assert "sk-team-canary" not in str(result) and "sk-team-canary" not in caplog.text
    assert skipped["status"] == "skipped"


def test_the_team_server_comes_first_for_the_context_window(monkeypatch) -> None:
    monkeypatch.setattr(
        checks,
        "check_litellm_proxy_model_info",
        lambda model_id, **_kwargs: checks._result(
            method="litellm_proxy", status="found", context_window_tokens=400000, supports_reasoning=True
        ),
    )
    monkeypatch.setattr(
        checks,
        "check_litellm_model_capabilities",
        lambda model_id: checks._result(method="litellm", status="found", context_window_tokens=128000),
    )

    payload = checks.lookup_model_capability_candidates(
        provider="litellm", model_id="gpt-6-luna", methods="proxy,litellm", litellm_api_base="https://litellm.example"
    )

    assert payload["methods_requested"] == ["litellm_proxy", "litellm"]
    assert payload["consensus"]["context_window_tokens"] == 400000
    assert payload["consensus"]["supports_reasoning"] is True


def test_the_team_leads_lookup_gives_the_litellm_server_only_for_the_litellm_provider(monkeypatch) -> None:
    import json

    from custom_tools.providers import runtime_settings

    seen = []
    monkeypatch.setattr(
        runtime_settings,
        "build_model_capability_candidates",
        lambda **kwargs: seen.append(kwargs) or {"status": "ok"},
    )
    from custom_agents import model_keys
    from tests.model_key_support import env_model_keys

    agent = SimpleNamespace(model_provider="litellm", model_id="old", litellm_api_base="https://litellm.example")
    monkeypatch.setattr(runtime_settings, "get_agent_ref", lambda: agent)
    # The endpoint's key comes from the process's model keys, never from the agent.
    monkeypatch.setattr(model_keys, "process_model_keys", lambda: env_model_keys("sk-canary"))

    through_litellm = runtime_settings.lookup_model_capability_candidates(model_id="gpt-6-luna")
    runtime_settings.lookup_model_capability_candidates(provider="gemini", model_id="gemini-3-pro")

    assert "sk-canary" not in through_litellm
    assert json.loads(through_litellm) == {"status": "ok"}
    assert (seen[0]["litellm_api_base"], seen[0]["litellm_api_key"]) == ("https://litellm.example", "sk-canary")
    assert (seen[1]["litellm_api_base"], seen[1]["litellm_api_key"]) == ("", None)
