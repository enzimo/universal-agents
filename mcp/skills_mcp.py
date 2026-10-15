import json
import os
import sys
from pathlib import Path

# Add project root to sys.path to resolve internal imports
sys.path.append(os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from pydantic import BaseModel, ConfigDict, Field

from custom_agents.sdk.strands.skills import describe_skill, list_skill_metadata
from mcp.types import ToolAnnotations
from mcp_control.sdk_compat import run_tool_server, tool_server

# Initialize the MCP server. Bind settings are process-owned so live canaries
# and side-by-side deployments can allocate a non-conflicting listener. The
# server comes from mcp_control.sdk_compat, so it runs on the MCP SDK's 1.x or
# 2.x line (2.x takes the address when it runs).
BIND_HOST = os.getenv("SKILLS_MCP_BIND_HOST", "0.0.0.0")
BIND_PORT = int(os.getenv("SKILLS_MCP_BIND_PORT", "8000"))
mcp = tool_server("skills_mcp", host=BIND_HOST, port=BIND_PORT)
SKILLS_DIR = str(
    Path(os.getenv("SKILLS_MCP_SKILLS_DIR", str(Path.cwd() / ".skills")))
    .expanduser()
    .resolve()
)


class LoadSkillInput(BaseModel):
    """Input model for listing or loading a skill."""

    model_config = ConfigDict(str_strip_whitespace=True)

    skill_name: str = Field(
        default="list",
        description='The name of the skill to load (e.g. "skill-creator"). Pass "list" to see all available skills.',
    )


@mcp.tool(
    name="load_skill",
    annotations=ToolAnnotations(
        title="Load Skill",
        readOnlyHint=True,
        destructiveHint=False,
        idempotentHint=True,
        openWorldHint=False,
    ),
)
async def load_skill(params: LoadSkillInput) -> str:
    """
    Inspect skills from the configured skills directory using Strands' official skill model.
    """
    if params.skill_name.lower() == "list":
        return json.dumps(
            {
                "available_skills": list_skill_metadata(skills_dir=SKILLS_DIR),
            },
            indent=2,
        )

    try:
        return json.dumps(describe_skill(params.skill_name, skills_dir=SKILLS_DIR), indent=2)
    except FileNotFoundError as exc:
        return json.dumps({"error": str(exc)}, indent=2)


if __name__ == "__main__":
    # When run directly, use HTTP (sse) transport
    run_tool_server(mcp, "sse", host=BIND_HOST, port=BIND_PORT)
