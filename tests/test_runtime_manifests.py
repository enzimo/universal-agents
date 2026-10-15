"""Conformance tests for service-scoped runtime configuration manifests."""

from __future__ import annotations

import json

import pytest
from pydantic import ValidationError

from assistant_gateway.config import GatewayInstanceConfig
from runtime_hosting.manifests import RUNTIME_MANIFEST_FILENAMES, GatewayRuntimeManifestV1
from runtime_hosting.manifest_rendering import render_runtime_manifests
from scripts.export_runtime_manifest_schemas import (
    DEFAULT_OUTPUT_DIR,
    render_schema_files,
)


def _config(tmp_path) -> GatewayInstanceConfig:
    return GatewayInstanceConfig.create(
        instance_name="manifest-test",
        instance_root=str(tmp_path / "instance"),
        team_id="team_manifest",
        timezone="UTC",
    )


def test_runtime_manifests_are_deterministic_scoped_and_secret_free(tmp_path) -> None:
    config = _config(tmp_path)
    config.transport.nats_password = "do-not-serialize"
    first = render_runtime_manifests(config)
    second = render_runtime_manifests(config)

    assert [item.model_dump(mode="json") for item in first] == [
        item.model_dump(mode="json") for item in second
    ]
    assert {item.service for item in first} == set(RUNTIME_MANIFEST_FILENAMES)
    rendered = json.dumps([item.model_dump(mode="json") for item in first])
    assert "do-not-serialize" not in rendered
    assert "NATS_PASSWORD" in rendered
    assert "llm" not in first[1].model_dump(mode="json")
    assert "interactive_apps" not in first[2].model_dump(mode="json")
    # Execution artifacts and runs come from the derived instance layout.
    controller = next(item for item in first if item.service == "execution_controller")
    assert controller.artifact_store_path == str(config.layout.execution_artifacts_dir)
    assert controller.run_path == str(config.layout.execution_runs_dir)


def test_runtime_manifest_rejects_content_drift(tmp_path) -> None:
    manifest = render_runtime_manifests(_config(tmp_path))[0]
    payload = manifest.model_dump(mode="json")
    payload["instance_name"] = "tampered"

    with pytest.raises(ValidationError, match="content_sha256"):
        GatewayRuntimeManifestV1.model_validate(payload)


def test_tracked_runtime_manifest_schemas_match_models() -> None:
    rendered = render_schema_files()
    assert {
        path.name: path.read_text(encoding="utf-8")
        for path in DEFAULT_OUTPUT_DIR.glob("*.json")
    } == rendered
