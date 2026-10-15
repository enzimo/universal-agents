from __future__ import annotations

from pathlib import Path

import pytest
import yaml

from config_loader import lookup_configured_model_request_constraints
from custom_agents.reasoning_summary_recovery import (
    disable_unsupported_reasoning_summary,
    is_unsupported_reasoning_summary_error,
)


class FakeBadRequestError(RuntimeError):
    def __init__(
        self,
        message: str,
        *,
        param: str = "reasoning.summary",
        code: str = "unsupported_parameter",
    ) -> None:
        super().__init__(message)
        self.status_code = 400
        self.body = {
            "error": {
                "message": message,
                "param": param,
                "code": code,
            }
        }


def _classified(error: BaseException, **overrides: object) -> bool:
    values = {
        "provider": "openai",
        "model_id": "gpt-5.6-luna",
        "litellm_api_mode": "responses",
        "requested_summary": "detailed",
        **overrides,
    }
    return is_unsupported_reasoning_summary_error(error, **values)


def test_classifies_explicit_native_openai_reasoning_summary_rejection() -> None:
    error = FakeBadRequestError("reasoning.summary is not supported for this model")

    assert _classified(error) is True


def test_classifies_explicit_openai_codex_reasoning_summary_rejection() -> None:
    error = FakeBadRequestError("reasoning.summary is not supported for this model")

    assert _classified(error, provider="openai-codex") is True


def test_classifies_wrapped_litellm_openai_responses_rejection() -> None:
    cause = FakeBadRequestError("The reasoning summary parameter is unsupported")
    wrapper = RuntimeError("provider request failed")
    wrapper.__cause__ = cause

    assert (
        _classified(
            wrapper,
            provider="litellm",
            model_id="openai/gpt-5.6-luna",
        )
        is True
    )


def test_repair_writes_model_scoped_null_with_backup_and_is_idempotent(
    tmp_path: Path,
) -> None:
    override_path = tmp_path / "LLM_OVERRIDES.yaml"
    override_path.write_text(
        """
default:
  provider: litellm
  model_id: openai/gpt-5.6-luna
  reasoning_summary: detailed
model_capabilities:
  litellm:
    other/model:
      supports_image_input: true
""",
        encoding="utf-8",
    )
    for index in range(6):
        (tmp_path / f"LLM_OVERRIDES.yaml.bak.preexisting.{index}").write_text(
            f"backup {index}\n",
            encoding="utf-8",
        )

    repair = disable_unsupported_reasoning_summary(
        provider="litellm",
        model_id="openai/gpt-5.6-luna",
        requested_summary="detailed",
        instance_root=str(tmp_path),
        explicit_override_path=str(override_path),
    )

    assert repair.changed is True
    assert repair.backup_path is not None and repair.backup_path.exists()
    assert len(list(tmp_path.glob("LLM_OVERRIDES.yaml.bak.*"))) == 5
    persisted = yaml.safe_load(override_path.read_text(encoding="utf-8"))
    assert persisted["default"]["reasoning_summary"] == "detailed"
    assert (
        persisted["model_capabilities"]["litellm"]["openai/gpt-5.6-luna"][
            "reasoning_summary"
        ]
        is None
    )
    assert lookup_configured_model_request_constraints(
        persisted["model_capabilities"],
        provider="litellm",
        model_id="openai/gpt-5.6-luna",
    ) == {"reasoning_summary": None}

    second = disable_unsupported_reasoning_summary(
        provider="litellm",
        model_id="openai/gpt-5.6-luna",
        requested_summary="detailed",
        instance_root=str(tmp_path),
        explicit_override_path=str(override_path),
    )
    assert second.changed is False
    assert second.backup_path is None


def test_repair_refuses_to_overwrite_invalid_yaml(tmp_path: Path) -> None:
    override_path = tmp_path / "LLM_OVERRIDES.yaml"
    original = "model_capabilities:\n  litellm:\n    model: invalid\n"
    override_path.write_text(original, encoding="utf-8")

    with pytest.raises(ValueError, match="Cannot repair invalid LLM override file"):
        disable_unsupported_reasoning_summary(
            provider="litellm",
            model_id="openai/gpt-5.6-luna",
            requested_summary="auto",
            instance_root=str(tmp_path),
            explicit_override_path=str(override_path),
        )

    assert override_path.read_text(encoding="utf-8") == original


def test_repair_uses_composite_key_when_provider_has_direct_entry(
    tmp_path: Path,
) -> None:
    override_path = tmp_path / "LLM_OVERRIDES.yaml"
    override_path.write_text(
        """
model_capabilities:
  openai:
    supports_image_input: true
""",
        encoding="utf-8",
    )

    repair = disable_unsupported_reasoning_summary(
        provider="openai",
        model_id="gpt-5.6-luna",
        requested_summary="auto",
        instance_root=str(tmp_path),
        explicit_override_path=str(override_path),
    )

    persisted = yaml.safe_load(override_path.read_text(encoding="utf-8"))
    assert (
        repair.yaml_path == "model_capabilities.openai/gpt-5.6-luna.reasoning_summary"
    )
    assert persisted["model_capabilities"]["openai"]["supports_image_input"] is True
    assert persisted["model_capabilities"]["openai/gpt-5.6-luna"] == {
        "reasoning_summary": None
    }
    assert lookup_configured_model_request_constraints(
        persisted["model_capabilities"],
        provider="openai",
        model_id="gpt-5.6-luna",
    ) == {"reasoning_summary": None}
