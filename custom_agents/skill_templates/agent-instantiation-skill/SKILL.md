---
name: agent-instantiation-skill
description: Guide for selecting existing agent types and calling instantiate_agent correctly in this runtime. Use when TeamArchitect needs a specialist child or must map a task to an approved child-agent configuration.
---

# Agent Instantiation

Use this skill when deciding whether to create a child agent or when preparing an `instantiate_agent(...)` call.

Specialist planning (`fn:capabilities`), runtime settings (`fn:runtime_settings`),
access proposals (`fn:access_proposals`) and worker pools (`fn:worker_pools`)
load on request: call `use_tools(tool_ids=[...])` with the groups a step needs
before using their functions.

If the team defines reusable starting teams in `TEAM_TEMPLATES.yaml`,
you can also use `list_team_templates()` and `instantiate_team_template(...)` to
bring up a named team shape at the beginning of work instead of deciding every
child one by one.

## Tool Contract

Call:
`instantiate_agent(agent_type, team_id, mcp_profile_names=None, idle_ttl_seconds=None)`

Or call:
`instantiate_team_template(template_name, team_id="", instantiate_missing_only="true")`

For team composition inspection, call `get_team_composition()`. Prefer the
staged runtime-settings workflow for composition changes:
`team.startup_agents.count`, `team.startup_agents.enabled`,
`team.startup_agents.mode`, `team.ephemeral_agents.enabled`,
`team.ephemeral_agents.idle_ttl_seconds`, and
`team.ephemeral_agents.default_idle_ttl_seconds`. `set_team_startup_agent(...)`
and `set_ephemeral_agent_policy(...)` stage the same bounded plans; apply them
with `apply_change`.

The legacy raw `create_agent_definition_from_template(...)` entry point remains
removed. Use the reviewed generated-definition workflow instead:
`specialist_definitions(action="plan", ...)`, then `apply_change(change_id, reason)`:
a person approves the exact specialist (with authorization off, pass
`confirmed=true` after the person agrees). Use
`specialist_definitions(action="status", ...)` to inspect state and
`specialist_definitions(action="plan_removal", ...)` to stage removing an unchanged
bundle, applied the same way. A new specialist is available as soon as it is
created and a removed one stops being started at once; neither needs a restart,
so do not stage one for it.

Tools follow access. Every agent starts with its role's standard tools; with
authorization on, a tool it is given (a grant) appears on its next model call,
with no restart. An agent type's default tools, listed below, say what it
works with; they grant nothing. With authorization off an agent has its role's
standard tools and its default tools.

Stage a fresh plan after a denied approval, an expired plan (plans last 24
hours), or an ended task.

Parameter guidance:
- `agent_type`: Use an exact named agent type. The list below is as of this agent's start; `get_available_agent_types()` lists every type now, including specialists created since. Prefer the narrowest specialist that can complete the task.
- `team_id`: Use the team that should own the new child agent. In normal operation, keep this set to the current team: `{{current_team_id}}`.
- `mcp_profile_names`: Pass `None` or `[]`; non-empty agent-supplied values fail closed. A new instance uses an installed MCP server when it holds access to the server's tool (`mcp:<name>`) for the person it works for (with authorization off: when the server's record names its type).
- `idle_ttl_seconds`: Optional per-child idle TTL. When omitted, `TEAM_AGENTS.yaml` ephemeral policy is used.

Runtime behavior:
- Create exactly one child agent per tool call. Repeat the tool call to create multiple children.
- Forward `agent_type` into `AgentCreationRequest.named_agent_type`.
- Always set `agent_quantity=1` for this tool path.
- Keep child agents non-lead. This tool does not create a team lead.
- Do not attach MCP profiles ad hoc through this call; the direct request cannot name profiles.
- `instantiate_team_template(...)` reads the operator/onboarding-authored named template from the team's protected `TEAM_TEMPLATES.yaml` control file; list templates with `list_team_templates`.
- By default `instantiate_team_template(...)` only creates the missing delta relative to currently known non-exiting agents of each requested type on the same team.
- Existing generated specialist descriptions, roles, display names, and prompts
  can be changed with staged `specialist.*` settings. Those settings cannot
  change its tools.
- Generated creation accepts only behavioral text and `default_tools`: one to
  eight tool catalog ids or tool set ids from `find_tools` (for example
  `fn:publishing`, `code:python` or `google_workspace`). Platform controls and
  host commands (`code:shell`) cannot be defaults. Never provide imports,
  packages, commands, endpoints, executable paths, headers, environment values,
  or credentials.
- A default tool that works through the broker, such as `cli:x` or
  `google_workspace`, needs no credential step. When the tool is used, the
  broker takes the person's own sign-in (GitHub, Google, X) or the account
  linked to that tool. If there is none, follow the Tool Setup steps in
  `secure-credential-workflows`. Never pass a credential key or value.
- When authorization is on, a person approves the exact new specialist in the
  Inbox, and every call to one of its tools is still checked against access.
  When authorization is off, the person's agreement in the conversation applies
  it and its default tools are its tools. Broker and component guards apply in
  both modes.
- Definitions are read from the current trusted catalog each time a child starts.
- Startup-agent overrides apply on the next headless supervisor launch; they are not hot-loaded.
- When a settings change reports a required restart, use `plan_runtime_restart` and `apply_change`; a person approves the restart separately.
- After changing startup-agent overrides, tell the user they should reboot or relaunch Universal Agents for the startup roster to change. A separately authorized `scope="all"` restart only restarts supervised child processes; it does not reboot the host/container or rebuild startup commands already computed by the running supervisor.
- Structured delegation needs no worker start from you. A task for a worker
  type waits in that type's queue until a free worker takes it, and the runtime
  starts workers while tasks wait, up to the type's pool size (see the worker
  pool reference in `runtime-operations-workflows`). An exact named worker must
  be live; the runtime never substitutes another. To run more workers of a type
  for delegated work, raise its pool size with `set_worker_pool_limit` rather
  than starting workers by hand. Use this skill for deliberate team composition
  (a named team, a worker with a different MCP profile or idle time); manual
  starts count against the same team worker limit.

Selection process:
1. Decide whether existing agents are enough or whether new specialization is needed.
2. If a reusable starting team already matches the work, prefer a named team template.
3. Inventory the specialist roles and tools the plan requires. Treat cached team composition as advisory; live capacity and exact task pickup are runtime-owned checks.
4. If an existing loaded type needs a tool it does not have, make sure you hold
   it with handing on: access follows the work, so each task you give the
   worker gets what you may hand on when it needs it. When you lack it, call
   `request_access(tool_id, level, reason, hand_on=true)` at the lowest level
   the work needs; your task pauses until a person decides. Running workers see
   the tool on their next model call; nothing is restarted.
   `find_tools` lists tool ids; for a service the catalog does not have, use
   `propose_access_change`.
5. For a tool that works through the broker and is not listed as ready, call
   `set_up_tools` with the worker's `agent_type` before assigning work, and run
   a read-only broker check (such as `auth.status`) before assigning a
   mutation. For a missing sign-in, account link or team sign-in app
   (`auth_required`, `account_link_missing`, `x_account_missing`), call
   `set_up_tools` as described in `secure-credential-workflows` (Tool Setup);
   the task resumes when the person finished. Then repeat the read-only check.
6. A worker whose call is refused with `access_denied` calls `request_access`
   with the named `tool_id` and `level`; a person decides in the Inbox. (When
   your access covers it and lets you hand it on without asking, the worker's
   task already got it at the call.) With authorization off, an
   agent's tools are its role's standard tools and its default tools. Broker
   and component guards apply in both modes.
7. If no approved specialist type exists, stage
   `specialist_definitions(action="plan", ...)` with its `default_tools`. Apply it
   with `apply_change`: a person approves it in the Inbox, or, with
   authorization off, the person agrees and you pass `confirmed=true`.
   Web research (`fn:web_research`: `exa_search`, `exa_get_contents`,
   `brave_web_search_artifact`) and files (`fn:files`: `file_read`,
   `file_write`) are every agent's standard tools; a specialist that also runs
   Python lists `code:python`. Use the concrete operation names when inspecting
   tools or asking for access. Definition creation does not give access to its
   default tools. Its tasks get what its work needs from what you may hand on,
   as in step 4, or its `request_access` goes to a person.
8. If no generated type is needed, pick the best `agent_type` from the startup list.
9. Keep `mcp_profile_names` empty.
10. Call `instantiate_agent(...)` or instantiate an already approved team template.
11. Delegate through the structured action after any deliberate composition changes. Let runtime assurance create missing ordinary capacity and verify pickup.

Examples:
- Plain child: `instantiate_agent("CodeWritingAgent", "{{current_team_id}}")`
- Multiple children: repeat the tool call once per child instead of trying to pass a quantity.
- Team template: `instantiate_team_template("balanced_delivery_team", "{{current_team_id}}")`

## Current Startup Agent Types

{{available_agent_types}}

## Current Startup MCP Profiles

{{available_mcp_profiles}}

## Current Team Templates

{{available_team_templates}}

## Current Team Composition

{{team_composition}}

## Constraints

- Do not invent agent type names.
- Do not pass non-empty MCP profile names through `instantiate_agent`.
- Never infer that an account link or a default tool gives access to a tool.
  Never put imports, packages, commands, endpoints, headers, environment
  values, credential keys, or credential values in a specialist request.
- Do not invent team template names.
- Do not pass raw connection details through `instantiate_agent`; use configured profile names only.
- Use `get_available_agent_types()` or `get_self_status()` if runtime context looks stale.

## Startup evidence and scheduled workers

Read the JSON launch results returned by `instantiate_agent`. Treat `starting` as
a reserved startup, `ready` as transport and recovery readiness, and `waiting` as
unresolved existing launch custody. Inspect a denial or failure before retrying.
Do not infer task acceptance from a launch response or emit an
`AgentCreationRequest` on NATS; creation is a local TeamLead action.

For recurring work, load `runtime-operations-workflows` and establish a schedule
worker policy. Check known tool requirements before activating the job: the
worker type needs its own access, which `prepare_scheduled_job_access` reports as
`missing_access` and `authorize_scheduled_job_access` hands on where you may.
Starting a worker grants it no tools. Verify real task admission and results. Do
not manually recreate a child on every run or race an existing startup
reservation.

For a custom specialist, check `inspect_scheduled_worker` before assuming that
registration permits automatic startup. As TeamLead, add a missing registered
type to `agent.schedule_workers.eligible_agent_types` through the bounded runtime
settings plan/apply tools, retaining other entries. Then prepare access and
configure the schedule policy. Treat explicit denies and unavailable definitions
as separate blockers; never bypass them with a manual launch.
