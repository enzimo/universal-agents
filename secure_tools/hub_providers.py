"""Typed, secret-free contracts for the X and Hugging Face broker CLIs."""

from __future__ import annotations

import base64
import binascii
from pathlib import PurePosixPath
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator

from secure_tools.providers import ActionSpec, ToolContext, ToolResult, ValidatedAction

X_PROVIDER = "xurl"
HF_PROVIDER = "huggingface"
AUTH_STATUS = "auth.status"
POST_CREATE = "post.create"
FILE_DOWNLOAD = "file.download"
FILE_UPLOAD = "file.upload"
# Model metadata (pipeline tag and tags) for the team lead's model capability
# lookup; with the linked account so gated and private models are found too.
MODEL_INFO = "model.info"
MAX_FILE_BYTES = 1024 * 1024
HUB_CLI_REQUEST_TIMEOUT_SECONDS = 300


def is_hub_write(provider: str, action: str) -> bool:
    return (provider, action) in {(X_PROVIDER, POST_CREATE), (HF_PROVIDER, FILE_UPLOAD)}


class CredentialParams(BaseModel):
    """No account selector: the broker uses the account linked to the tool
    (``AccountLinkV1``), the person's own first, then the team's."""

    model_config = ConfigDict(extra="forbid", strict=True)


class PostParams(CredentialParams):
    # X owns weighted character validation, including URLs and Unicode.
    text: str = Field(min_length=1, max_length=25000)


class FileParams(CredentialParams):
    repo_id: str = Field(
        pattern=r"^[A-Za-z0-9][A-Za-z0-9_.-]*/[A-Za-z0-9][A-Za-z0-9_.-]*$",
        max_length=192,
    )
    repo_type: Literal["model", "dataset", "space"] = "model"
    filename: str = Field(min_length=1, max_length=240)
    revision: str = Field(pattern=r"^[a-f0-9]{40}$")

    @field_validator("filename")
    @classmethod
    def safe_filename(cls, value: str) -> str:
        path = PurePosixPath(value)
        if (
            not path.parts
            or path.is_absolute()
            or str(path) != value
            or path.parts[0] in {".cache", ".git"}
            or any(part in {"..", "."} or part.startswith("-") for part in path.parts)
            or any(
                ord(char) < 32 or ord(char) == 127 or char in "\\*?[]" for char in value
            )
        ):
            raise ValueError("filename must be a literal relative repository file path")
        return value


class ModelInfoParams(CredentialParams):
    # Older Hub models have no namespace (for example "gpt2").
    repo_id: str = Field(
        pattern=r"^(?:[A-Za-z0-9][A-Za-z0-9_.-]*/)?[A-Za-z0-9][A-Za-z0-9_.-]*$",
        max_length=192,
    )


class UploadParams(FileParams):
    content_base64: str = Field(max_length=4 * ((MAX_FILE_BYTES + 2) // 3))

    @field_validator("content_base64")
    @classmethod
    def bounded_content(cls, value: str) -> str:
        try:
            data = base64.b64decode(value, validate=True)
        except (ValueError, binascii.Error) as exc:
            raise ValueError("content_base64 must be valid base64") from exc
        if len(data) > MAX_FILE_BYTES:
            raise ValueError("file exceeds the 1 MiB broker transfer limit")
        return value


class HubCliProvider:
    """Validate before transport and again before broker execution."""

    def __init__(self, name: str, schemas: dict[str, type[CredentialParams]]) -> None:
        self.name = name
        self.schemas = schemas
        self.actions = {name: ActionSpec(name) for name in schemas}

    def validate(self, action: str, params: dict[str, Any]) -> ValidatedAction:
        schema = self.schemas.get(action)
        if schema is None:
            raise ValueError(
                f"Unsupported {self.name} action; use {', '.join(self.schemas)}"
            )
        try:
            parsed = schema.model_validate(params)
        except ValidationError as exc:
            # Pydantic's default message echoes rejected values, possibly secrets.
            fields = sorted(
                {str(error["loc"][0]) for error in exc.errors() if error["loc"]}
            )
            raise ValueError(
                f"Invalid {self.name} parameters: {', '.join(fields) or 'unexpected parameters'}"
            ) from None
        return ValidatedAction(self.name, action, parsed.model_dump(exclude_none=True))

    def execute(self, action: ValidatedAction, context: ToolContext) -> ToolResult:
        return ToolResult(
            status="error", error="Credentialed CLI execution is broker-owned."
        )


def hub_cli_providers() -> tuple[HubCliProvider, ...]:
    return (
        HubCliProvider(
            X_PROVIDER, {AUTH_STATUS: CredentialParams, POST_CREATE: PostParams}
        ),
        HubCliProvider(
            HF_PROVIDER,
            {
                AUTH_STATUS: CredentialParams,
                FILE_DOWNLOAD: FileParams,
                FILE_UPLOAD: UploadParams,
                MODEL_INFO: ModelInfoParams,
            },
        ),
    )
