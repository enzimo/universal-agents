"""MCP adapter process for one reviewed declarative hosted definition.

The server is built through ``mcp_control.sdk_compat``, so it runs on the MCP
SDK's 1.x and 2.x lines. It checks arguments against each tool's input schema
itself (2.x no longer does that for the low-level server) and reports
failures as tool error results, so both lines answer the same way.
"""

from __future__ import annotations

import argparse
import logging
from pathlib import Path
from typing import Any

from jsonschema.validators import Draft202012Validator
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import PlainTextResponse

from mcp_control import sdk_compat
from mcp_control.hosting import (
    HostedMCPBehavior,
    MCPHostedServerDefinitionV1,
)


LOGGER = logging.getLogger(__name__)


def _answer(tool: Any, arguments: dict[str, Any]) -> dict[str, Any]:
    if tool.behavior == HostedMCPBehavior.ECHO_ARGUMENTS:
        return {"arguments": dict(arguments)}
    if tool.behavior == HostedMCPBehavior.STATIC_RESPONSE:
        return dict(tool.static_response or {})
    raise ValueError("Unsupported trusted hosted MCP behavior.")


def build_managed_mcp_app(
    definition: MCPHostedServerDefinitionV1,
    *,
    bearer_token: str,
):
    """Build the isolated streamable-HTTP app for a trusted definition, behind its bearer token."""

    async def list_tools() -> list[Any]:
        return [
            sdk_compat.tool_definition(
                name=tool.name,
                description=tool.description,
                input_schema=tool.input_schema,
                output_schema=tool.output_schema,
            )
            for tool in definition.tools
        ]

    async def call_tool(name: str, arguments: dict[str, Any]) -> Any:
        tool = next((item for item in definition.tools if item.name == name), None)
        if tool is None:
            return sdk_compat.error_result("Unknown hosted MCP operation.")
        problem = next(iter(Draft202012Validator(tool.input_schema).iter_errors(arguments)), None)
        if problem is not None:
            return sdk_compat.error_result(f"Input validation error: {problem.message}")
        try:
            return sdk_compat.structured_result(_answer(tool, arguments))
        except ValueError as exc:
            return sdk_compat.error_result(str(exc))

    app = sdk_compat.streamable_http_app(
        name=definition.server_name,
        instructions=definition.purpose,
        host=definition.bind_host,
        port=definition.port,
        path="/mcp",
        list_tools=list_tools,
        call_tool=call_tool,
    )

    async def require_bearer(request: Request, call_next):
        if request.headers.get("authorization") != f"Bearer {bearer_token}":
            return PlainTextResponse("Forbidden", status_code=403)
        return await call_next(request)

    app.add_middleware(BaseHTTPMiddleware, dispatch=require_bearer)
    return app


def build_arg_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Run one managed declarative MCP server")
    parser.add_argument("--definition", required=True)
    parser.add_argument("--token-file", required=True)
    return parser


def main() -> None:
    import uvicorn

    args = build_arg_parser().parse_args()
    definition = MCPHostedServerDefinitionV1.model_validate_json(
        Path(args.definition).read_text(encoding="utf-8")
    )
    bearer_token = Path(args.token_file).read_text(encoding="utf-8").strip()
    if not bearer_token:
        raise RuntimeError("Managed MCP sidecar token is empty.")
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    LOGGER.info(
        "Starting managed declarative MCP sidecar. event=mcp_host_sidecar_starting "
        "hosted_server_id=%s definition_digest=%s bind_host=%s port=%s",
        definition.hosted_server_id,
        definition.definition_digest,
        definition.bind_host,
        definition.port,
    )
    uvicorn.run(
        build_managed_mcp_app(definition, bearer_token=bearer_token),
        host=definition.bind_host,
        port=definition.port,
        log_level="info",
        access_log=False,
    )


if __name__ == "__main__":
    main()
