"""Contract tests for the neutral MCP broker boundary."""

from __future__ import annotations

import json

import pytest
from jsonschema import Draft202012Validator
from mcp import types
from pydantic import ValidationError

from custom_messages.contracts.common_v1 import (
    PrincipalDescriptorV1,
    PrincipalType,
)
from custom_messages.contracts.mcp_v1 import (
    MCPDiscoveryRequestV1,
    MCPDiscoveryResponseV1,
    MCPHostLifecycleRequestV1,
    MCPInvocationRequestV1,
    MCPInvocationResponseV1,
    MCPToolDefinitionV1,
    MCPToolResultV1,
)
from scripts.export_mcp_schemas import DEFAULT_OUTPUT_DIR, render_schema_files
from access_control.identity import ContextVerifier
from tests.broker_access_support import context_verify_keys, signed_task_context


def test_tracked_mcp_schemas_match_neutral_contract_models() -> None:
    rendered = render_schema_files()

    assert {path.name for path in DEFAULT_OUTPUT_DIR.glob("*.json")} == set(rendered)
    for filename, expected in rendered.items():
        schema_path = DEFAULT_OUTPUT_DIR / filename
        assert schema_path.read_text(encoding="utf-8") == expected
        if filename != "manifest.json":
            Draft202012Validator.check_schema(json.loads(expected))

    manifest = json.loads(rendered["manifest.json"])
    assert {entry["model"] for entry in manifest["schemas"]} == {
        MCPDiscoveryRequestV1.__name__,
        MCPDiscoveryResponseV1.__name__,
        MCPInvocationRequestV1.__name__,
        MCPInvocationResponseV1.__name__,
        MCPHostLifecycleRequestV1.__name__,
        MCPToolDefinitionV1.__name__,
        MCPToolResultV1.__name__,
    }


def test_mcp_native_result_round_trips_without_strands_projection() -> None:
    sdk_result = types.CallToolResult.model_validate(
        {
            "content": [
                {"type": "text", "text": "complete"},
                {
                    "type": "image",
                    "data": "aW1hZ2UtYnl0ZXM=",
                    "mimeType": "image/png",
                },
            ],
            "structuredContent": {"score": 0.75, "nested": {"ok": True}},
            "_meta": {"trace": "trace-1"},
            "isError": False,
        }
    )

    portable = MCPToolResultV1.from_sdk_result(sdk_result)
    wire = json.loads(portable.model_dump_json(by_alias=True, exclude_none=True))
    restored = types.CallToolResult.model_validate(wire)

    assert restored == sdk_result
    assert wire["content"][1]["data"] == "aW1hZ2UtYnl0ZXM="
    assert "status" not in wire
    assert "toolUseId" not in wire


def test_mcp_invocation_contract_rejects_unowned_authority_fields() -> None:
    principal = PrincipalDescriptorV1(
        principal_type=PrincipalType.AGENT,
        principal_id="agent_contract",
        team_id="team_contract",
        named_agent_type="TeamArchitect",
        on_behalf_of_user_id="usr_contract",
    )

    with pytest.raises(ValidationError, match="extra_forbidden"):
        MCPInvocationRequestV1.model_validate(
            {
                "record_digest": "a" * 64,
                "operation_name": "read",
                "arguments": {},
                "tool_use_id": "tool-use-1",
                "task_id": "task-1",
                "principal": principal.model_dump(mode="json"),
                "authorization_allowed": True,
            }
        )


def test_mcp_invocation_contract_carries_the_signed_task_context_only() -> None:
    principal = PrincipalDescriptorV1(
        principal_type=PrincipalType.AGENT,
        principal_id="agent_contract",
        team_id="team-1",
        named_agent_type="TeamArchitect",
        on_behalf_of_user_id="user-1",
    )
    signed = signed_task_context("user-1")
    payload = {
        "record_digest": "a" * 64,
        "operation_name": "read",
        "tool_use_id": "tool-use-1",
        "principal": principal.model_dump(mode="json"),
        "task_context": signed,
    }

    request = MCPInvocationRequestV1.model_validate(payload)
    wire = json.loads(request.model_dump_json(by_alias=True, exclude_none=True))

    # The broker verifies this signature, so it must still verify after the wire.
    verified = ContextVerifier.from_value(context_verify_keys()).verify(
        wire["task_context"], team_id="team-1"
    )
    assert verified.person_id == "user-1"
    # The account link, never the request, chooses the credential.
    for field in ("credential_binding_id", "credential_key", "account_link"):
        with pytest.raises(ValidationError, match="extra_forbidden"):
            MCPInvocationRequestV1.model_validate({**payload, field: "chosen"})


def test_mcp_contract_rejects_non_sha256_record_digest() -> None:
    with pytest.raises(ValidationError, match="string_pattern_mismatch"):
        MCPDiscoveryRequestV1(
            record_digest="z" * 64,
            named_agent_type="TeamArchitect",
        )


def test_discovery_request_names_only_the_connection_version_and_agent_type() -> None:
    """Listing tools needs no person: the broker checks the calling process and the attachment."""

    request = MCPDiscoveryRequestV1(record_digest="a" * 64, named_agent_type="TeamArchitect")

    assert request.model_dump(mode="json") == {
        "schema_version": 1,
        "record_type": "mcp_discovery_request",
        "record_digest": "a" * 64,
        "named_agent_type": "TeamArchitect",
    }
    principal = PrincipalDescriptorV1(
        principal_type=PrincipalType.AGENT,
        principal_id="agent_contract",
        team_id="team-1",
        named_agent_type="TeamArchitect",
        on_behalf_of_user_id="user-1",
    )
    for field, value in (
        ("principal", principal.model_dump(mode="json")),
        ("task_id", "task-1"),
        ("task_context", signed_task_context("user-1")),
    ):
        with pytest.raises(ValidationError, match="extra_forbidden"):
            MCPDiscoveryRequestV1.model_validate(
                {"record_digest": "a" * 64, "named_agent_type": "TeamArchitect", field: value}
            )


def test_hosted_server_start_or_stop_request_carries_only_the_definition_digest() -> None:
    """Who asked was checked before the request (the per-call hook or the gateway's person check)."""

    request = MCPHostLifecycleRequestV1(definition_digest="b" * 64)

    assert request.model_dump(mode="json") == {
        "schema_version": 1,
        "record_type": "mcp_host_lifecycle_request",
        "definition_digest": "b" * 64,
    }
    principal = PrincipalDescriptorV1(
        principal_type=PrincipalType.USER,
        principal_id="user-1",
        team_id="team-1",
        roles=("admin",),
    )
    with pytest.raises(ValidationError, match="extra_forbidden"):
        MCPHostLifecycleRequestV1.model_validate(
            {"definition_digest": "b" * 64, "principal": principal.model_dump(mode="json")}
        )
