# Skills

Installed skills are served dynamically from filesystem `SKILL.md` files at
runtime.

Open `/docs/skills/` to browse team skills in `control/skills`, verified
managed installations, and built-in runtime guides. The guides ship with the
application, so they do not require a separate installation.

Agents can read every team skill and run its bundled scripts, but no agent can
change one directly. To add or change a team skill, ask TeamArchitect: it (or a
specialist it asks) writes a draft folder in the task's own folders, proposes
it with `plan_team_skill_change`, and shows you the scan result and, for a
change, the added, removed, and changed files. You approve every skill change
before it is installed, and a replaced version is kept so it can be restored.

Runtime workflow guidance is available at
[/docs/skills/runtime-operations-workflows?source=bundled](/docs/skills/runtime-operations-workflows?source=bundled).
This explains procedures. To follow a particular workflow's progress, use the
agent's saved `/workflows/<id>` link. Reading a skill page does not grant tools or
mean that every agent has that skill enabled.

The index keeps cards compact by shortening visible skill names after 48
characters and descriptions after 160 characters. These are display-only
previews: open a skill's detail page to read its complete canonical name,
description, and instructions.

Project-owned skills currently include:

- `gog`, for Google Workspace through `gog` and brokered Google actions.
- `bw`, for Bitwarden through `bw` and brokered Bitwarden actions.

Runtime prompt skills may also be attached from tracked templates under
`custom_agents/skill_templates/`. For example, `secure-credential-workflows`
keeps credential, `secure_cli`, brokered auth, secure HTTP, OpenShell lease, and
provider-specific credential procedures out of the shared base system prompt,
while `runtime-operations-workflows` keeps runtime settings, heartbeat,
notification, and binary artifact delivery procedures out of the base prompt.
The bundled `convert-documents-to-markdown` skill teaches agents to use the
container-installed native `anydoc` binary for local Office, OpenDocument, RTF,
EPUB, CSV, and text-based PDF conversion without an npm or hosted-service
fallback.

A skill can name the tool groups its procedure uses in its `SKILL.md`
frontmatter, under the Agent Skills `metadata` map, as space-separated catalog
tool ids:

```yaml
metadata:
  ua-tools: fn:access_workflows fn:access_proposals
```

When an agent activates the skill, the runtime makes those groups available for
the task through the same path as `use_tools`: only groups the agent already
holds, within its model's tool cap. A skill never gives access. The skill result
says which groups were loaded, which are in the agent's tool index (called with
`call_tool`, see [How agents' tools are shown](configuration.md#how-agents-tools-are-shown-experiment)),
which did not fit, and which are not available to this agent. It does not ask
the agent to request access to those, because a skill names everything it can
use, not what one task needs. An MCP server named there loads on its own once
the agent has access. Unknown ids are skipped and logged. Name only the few
groups the skill's main procedure needs (at most six).

`scripts/check_skill_tool_names.py SKILL_DIR...` reports `ua-tools` ids that
are not catalog tools, declarations of more than six groups, and backticked
calls such as `` `name(...)` `` to functions that no agent tool has. It works on
any skill folder, for example a `ua-skills` checkout; a test runs it over the
bundled templates.

These packaged skills are fallbacks. If `control/skills`, an imported `ua-skills`
catalog, or another configured external source provides the same
case-insensitive skill name, UA loads only the external definition. Removing
that external definition makes the packaged fallback available again on the
next filesystem refresh.

Installable skills can also come from the `ua-skills` catalog. For example,
`user-onboarding-guide` gives the assistant a warm first-run tour: a core
walkthrough of the team, the Web Chat screen, sign-ins, the Inbox, schedules,
objectives, pages, memory, files and tasks, then a menu of deeper topics, with
administrator topics only for owners and administrators. It remembers progress
when the user says "finish". Bare greetings can still be treated as opt-in
offers, but explicit requests such as `/tour` or "give me the tour" start the
walkthrough immediately. See [Conversations](features.md#conversations) for
what the tour covers.

The static MkDocs site is generated during Docker builds. The `/docs/skills/` pages
remain dynamic so local skill changes can be inspected without rebuilding the
image.
