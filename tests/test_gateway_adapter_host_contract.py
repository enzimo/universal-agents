from __future__ import annotations

import ast
import hashlib
import json
from pathlib import Path

import pytest
from jsonschema import Draft202012Validator, FormatChecker

from assistant_gateway.contracts.adapter_host_v1 import (
    ADAPTER_HOST_PROTOCOL_VERSION,
    AdapterCapabilityV1,
    AdapterHostContractError,
    decode_adapter_host_message_v1,
    encode_adapter_host_message_v1,
)
from assistant_gateway.interactive_apps.contracts import (
    InteractiveAppCapability,
)


REPO_ROOT = Path(__file__).resolve().parents[1]
SCHEMA_PATH = (
    REPO_ROOT
    / "data"
    / "schemas"
    / "gateway_adapter"
    / "v1"
    / "adapter_host.schema.json"
)
FIXTURE_PATH = SCHEMA_PATH.parent / "fixtures" / "conformance_cases.json"
MANIFEST_PATH = SCHEMA_PATH.parent / "manifest.json"


def _contract_cases() -> dict:
    return json.loads(FIXTURE_PATH.read_text(encoding="utf-8"))


def _schema_validator() -> Draft202012Validator:
    schema = json.loads(SCHEMA_PATH.read_text(encoding="utf-8"))
    Draft202012Validator.check_schema(schema)
    return Draft202012Validator(
        schema,
        format_checker=FormatChecker(),
    )


def test_adapter_host_valid_fixtures_match_schema_and_python_contract() -> None:
    cases = _contract_cases()
    validator = _schema_validator()

    assert cases["protocol_version"] == ADAPTER_HOST_PROTOCOL_VERSION
    for case in cases["valid"]:
        payload = case["payload"]
        validator.validate(payload)
        message = decode_adapter_host_message_v1(payload)
        encoded = encode_adapter_host_message_v1(message)
        validator.validate(encoded)
        assert encoded["protocol_version"] == ADAPTER_HOST_PROTOCOL_VERSION
        assert encoded["message_type"] == payload["message_type"]


def test_adapter_host_invalid_fixtures_have_stable_error_categories() -> None:
    cases = _contract_cases()
    validator = _schema_validator()

    for case in cases["invalid"]:
        payload = case["payload"]
        assert list(validator.iter_errors(payload)), case["name"]
        with pytest.raises(AdapterHostContractError) as error:
            decode_adapter_host_message_v1(payload)
        assert error.value.error_code == case["error_code"], case["name"]


def test_adapter_host_capabilities_match_the_in_process_plugin_contract() -> None:
    assert {item.value for item in AdapterCapabilityV1} == {
        item.value for item in InteractiveAppCapability
    }


def test_adapter_host_contract_does_not_import_gateway_or_infrastructure() -> None:
    contract_path = REPO_ROOT / "assistant_gateway" / "contracts" / "adapter_host_v1.py"
    tree = ast.parse(contract_path.read_text(encoding="utf-8"))
    forbidden_prefixes = (
        "assistant_gateway.application",
        "assistant_gateway.gateway",
        "assistant_gateway.store",
        "custom_agents",
        "custom_messages",
        "custom_transport",
        "storage",
    )
    violations = []
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom):
            if str(node.module or "").startswith(forbidden_prefixes):
                violations.append(node)
        elif isinstance(node, ast.Import):
            if any(alias.name.startswith(forbidden_prefixes) for alias in node.names):
                violations.append(node)

    assert violations == []


def test_adapter_host_schema_exposes_no_trusted_owner_or_credentials() -> None:
    schema_text = SCHEMA_PATH.read_text(encoding="utf-8")

    assert "canonical_user_id" not in schema_text
    assert "owner_canonical_user_id" not in schema_text
    assert "nats_credentials" not in schema_text
    assert "broker_credentials" not in schema_text
    assert "file:" not in schema_text


def test_adapter_host_contract_manifest_rejects_schema_or_fixture_drift() -> None:
    manifest = json.loads(MANIFEST_PATH.read_text(encoding="utf-8"))

    assert manifest["protocol_version"] == ADAPTER_HOST_PROTOCOL_VERSION
    for entry in (manifest["schema"], manifest["fixtures"]):
        path = MANIFEST_PATH.parent / entry["filename"]
        digest = hashlib.sha256(path.read_bytes()).hexdigest()
        assert digest == entry["sha256"], path
