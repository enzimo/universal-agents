# Visual workflows

Before the team lead asks for anything, it can show you the whole goal as a
map: the steps, what each one needs, and the order they run in. The map is a
view over the ordinary access requests and change requests you decide in the
Inbox. A plan grants nothing. Each request is still decided on its own, by
you or by the access service, and every tool call is checked again when it is
made.

Open **Inbox → Open Workflows**, or the workflow card the agent posts in chat.

## What the page shows

- **Steps and their order.** Arrows show what each step needs first.
- **Each step's requests and their current status**: waiting for a decision,
  approved, approved and applied (for a change), denied, or withdrawn because
  the task ended first. A waiting request has an **Open in Inbox** link to its
  review. A request that a worker made shows the worker's name.
- **Each step's setup pages**: the sign-ins, account links and team sign-in
  apps the step waits for, with how each one stands: waiting for you to
  finish the page, **Signed in as ...**, the page expired (a new one is
  offered, at most three), or closed without finishing. These lines update by
  themselves when you finish or close a page in chat.
- **Other requests from this task**: requests that matched no single step of
  the plan, including every folder request, because steps name tools and
  changes, not folders. They are decided in the Inbox as usual. Setup pages
  that matched no single step are listed here too.
- **Activity**: what the agent reported and what people decided. Progress
  marked "Agent reported" is the agent's own report; request status comes from
  the access service.

If the task that published the plan ends before the plan is finished, the page
says so and explains how to continue.

## Deciding requests

**Review Ready Requests** shows every request of the plan that waits for a
decision now, as the same review cards the Inbox shows. For a staged change
or an access proposal you choose **Approve** or **Deny**. For access you tick
**Keep for later tasks** on each grant you want to last beyond this task
(with **How long**: until you remove it, or 1, 7 or 30 days), then choose
**Approve**, **Approve reading only** (when more than read was asked), or
**Deny**. You can also **Approve All** or **Deny All**. Each decision is saved
exactly as if you made it in the Inbox, and the waiting agent is told. If one
request was decided elsewhere meanwhile, the page says how many decisions were
saved.

A request that only someone else can decide is shown without choices. Access
for the work of a person who holds it is decided only by that person. Access
the person lacks, proposals and platform changes are decided by an
administrator; a change to one person's own setting can also be decided by
that person.

When you deny a request, its step is closed: it is marked **Denied: it will
not be asked for again**, and the agent cannot ask for anything that step
covers again in this plan. Agree a different plan with the agent instead.

Some steps depend on a result that does not exist yet, such as a linked
account or a written draft. Their requests appear later on the same page.

When a request needs two or more things set up or approved (for example
"What repos can you access in GitHub?" when TeamLead has no GitHub access and
you are not signed in), TeamLead's `set_up_tools` publishes one workflow with
a step for each of them and a final step for the work, and one chat message
links it before any page opens. TeamLead never asks first whether to start. When only
one thing is missing, there is no workflow: the page just opens.

When a step needs an account, the setup page opens in chat (a sign-in,
**Choose your account for ...**, or the team's sign-in app for an
administrator). Complete the broker's page; the step on the workflow page
changes by itself:

- finished: the step shows **Signed in as ...** (or **Set up**) and is done
  once every page it waits for is finished;
- the page expired: the step keeps waiting and a new page is offered; after
  the third expired page the step fails and says you can set it up later with
  `/link <tool>`;
- closed without finishing: the step is blocked, like a denied request, and
  is not asked for again in this plan.

A step you do outside Universal Agents, such as creating a token on the
service's website, shows its instructions in chat and on its step, with a
**Mark Done** button on the workflow page. Only you can mark it done; the
waiting task then continues (the account link that stores the token opens
next). These steps come from the tool's catalog setup data, in the exact
words an administrator approved, never from the agent.

Setting the tool up yourself with `/link <tool>` while the task waits marks
the step the same way. The task continues by itself. The agent learns only
whether the setup finished, never a credential. Setting a tool up grants no
access by itself.

**Download Diagnostic Logs** saves the steps, request outcomes, event times and
codes, and task references. It leaves out chat, request reasons, work content,
credentials, and form links, and says when older events were left out.

## How the agent uses workflows

These tools belong to the team lead's standard tools. They coordinate and
grant nothing.

1. `submit_access_workflow` publishes the complete goal, including the work
   itself and a final check. Each step lists what it needs:
   - `change_kinds`: kinds of staged change applied with `apply_change`, such
     as `agent_definition`, `runtime_restart` or `team_skill_install`, and
     `access_proposal` for a proposal (`propose_access_change`, or
     `mcp_connections(action="propose")`, which proposes installing an MCP server);
   - `tool_access`: tools with the level they need, such as
     `{"tool_id": "cli:google", "level": "write"}`. `find_tools` lists tools.
     A plan with a step that installs an MCP server (`access_proposal` or
     `hosted_mcp_server`) may name its tool (`mcp:<name>`) before it is
     installed. Folders cannot be named; a folder request is
     shown with the requests that are not on a step.

   - `tool_setup`: the setup pages an `account` or `setup` step waits for,
     such as `{"tool_id": "cli:github", "kind": "sign_in"}` (kinds
     `sign_in`, `account_link` with `owner` `person` or `team`,
     `team_sign_in_app`, and `external_step` with the catalog `step_id`).
     They are checked against the tool: a tool without a personal sign-in
     cannot expect one, and an external step must be in the tool's setup
     data.

   Account steps name no changes or tool access. Unknown kinds and tools are
   refused. For tool setup TeamLead does not publish the plan itself:
   `set_up_tools` publishes one for two or more things to set up or ask for
   (or any step done outside Universal Agents), with TeamLead's own
   `extra_steps` before the final work step.
2. `update_access_workflow` marks a step running before the agent asks for
   what it needs. A request made then is shown on the one running step that
   expects it: the same kind of change, or the same tool at the requested
   level or lower. A worker's request is shown on its parent's step, because
   the worker's task carries the same signed task context as the parent task.
   A setup page TeamLead opens is shown on the one step that expects that
   tool, kind and owner and is running, waiting, or ready to start (its
   prerequisites are done); the step then waits for the person. Trusted code
   marks the step from the page's result, as described above. A step with an
   unfinished setup page cannot be reported complete.
3. `request_access_workflow_approvals(workflow_id, change_ids, reason)` asks
   for several staged changes together: one change request per change, bound
   to that exact plan, and one pause until every one is decided. The agent
   then applies each approved change with `apply_change(change_id, reason)`,
   which finds the approval by its change id. Denied changes are reported and
   never asked for again.
4. `get_access_workflow` reads the plan and each request's status before the
   agent continues, for example after a restart.
5. `revise_access_workflow_step` corrects what an unfinished step needs, as
   long as no request is linked to it.

A step is reported complete only after its requests were approved and its
changes applied. A task cannot finish while its plan has unfinished steps: it
gets one turn to finish or report them, and a repeated attempt without
progress fails with an explicit error. An uncertain external result is
checked before any retry.

With authorization off there are no requests: the agent can still publish and
follow a plan, and applies each change after you agree in the conversation.
`request_access_workflow_approvals` is not available then.

The default limit is 32 steps (`access_workflows.max_steps`, at most 64).
Diagnostic downloads keep up to 1,000 events
(`access_workflows.diagnostic_event_limit`). Both settings are in generic and
instance configuration. Lists and activity use pages of 20.

Runnable plan examples are in
[`data/examples/access_workflows.json`](data/examples/access_workflows.json):
a new research specialist with a schedule, a reviewed announcement, and adding
a crawler MCP server to a specialist. Adapt them to the goal; do not stop at
setup.

## Changes in this release

- Steps name kinds of change and tools with a level instead of action ids.
  Workflows saved by earlier releases are not shown and cannot be continued;
  the agent publishes a new plan.
- Routine delegation limits and `plan_routine_team_delegation` are removed.
  The team lead hands on access from the starter profile you chose.
- The workflow page no longer has a review conversation; ask questions in chat.
- Steps can name the setup pages they wait for (`tool_setup`), and the page
  shows each one's status; a request that needs two or more things set up
  gets one workflow, published by `set_up_tools`. Steps done outside
  Universal Agents have **Mark Done**. `check_tool_setup` and the
  `tool_setup_request` action are removed: TeamLead calls `set_up_tools`.
- The settings `human_interactions.workflow_max_steps` and
  `human_interactions.workflow_diagnostic_event_limit` moved to
  `access_workflows.max_steps` and `access_workflows.diagnostic_event_limit`.
  The whole `human_interactions` block is removed: startup refuses it, and the
  refusal names the new keys for these two values. Copy the values over and
  delete the block.
