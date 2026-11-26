"""Shared names for ``agent.tool_presentation``: how an agent's tools are shown to its model.

Why: every model call carries the full definition of every tool the model can
see (about 11,500 tokens for a worker, 17,600 for the team lead), which costs
context and makes the model choose worse. The setting compares three ways of
showing tools (ua-architecture ``plans/tool_index_and_scripts_plan.md``):

- ``full_definitions``: today's behavior. The model sees every tool it holds,
  except the catalog's ``on_request`` groups, which wait for ``use_tools``.
- ``index``: only the catalog's ``always_shown`` tools and the groups the
  agent's definition names (``default_tools``) keep full definitions. Every
  other held tool waits for ``use_tools`` and is listed in a one-line index in
  the system prompt, so nothing the agent holds depends on search to be found
  (``custom_agents.tool_index``).
- ``index_and_scripts``: ``index`` plus ``run_tool_script``, which lets a short
  sandboxed script call the agent's read-level tools without loading them.

The setting exists for the comparison; the winner becomes the only behavior.
Configuration validates the value (``runtime_settings.agents``) and the agent
reads it from its tool settings (``custom_agents.tool_presentation``), so the
settings layer never imports agent code.
"""

TOOL_PRESENTATION_FULL_DEFINITIONS = "full_definitions"
TOOL_PRESENTATION_INDEX = "index"
TOOL_PRESENTATION_INDEX_AND_SCRIPTS = "index_and_scripts"
TOOL_PRESENTATIONS = (
    TOOL_PRESENTATION_FULL_DEFINITIONS,
    TOOL_PRESENTATION_INDEX,
    TOOL_PRESENTATION_INDEX_AND_SCRIPTS,
)
DEFAULT_TOOL_PRESENTATION = TOOL_PRESENTATION_FULL_DEFINITIONS
