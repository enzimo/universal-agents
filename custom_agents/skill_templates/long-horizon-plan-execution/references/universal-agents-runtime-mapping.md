# Universal Agents Runtime Mapping

## Apply Runtime Primitives

Objective, work-session and work-profile tools (`fn:objectives`), solution
variations (`fn:solution_variations`), access workflows and proposals
(`fn:access_workflows`, `fn:access_proposals`) load on request: call
`use_tools(tool_ids=[...])` with the groups a step needs before using their
functions. `activate_plan_execution` and `start_isolated_recovery` are always in
your tool list, and objective work sessions already have the objectives tools.

Map the OODA operating model to Universal Agents without inventing identifiers,
side channels, or authority:

| OODA concept | Universal Agents mechanism |
| --- | --- |
| Team lead | `TeamArchitect` |
| Bounded execution | Structured `Task` |
| Human decision | `TaskQuery` through the manager |
| Durable outcome | Manager-owned `ObjectiveRecord` |
| Living strategy | Manager-owned `PlanRecord` |
| Bounded plan execution | `activate_plan_execution` |
| Multi-session bounded execution | Opt-in `ObjectiveWorkPolicy` and `ObjectiveWorkSession` |
| Cross-session handoff | `ObjectiveWorkCheckpoint` |
| Current state | Execution digest |
| Chronology | Task history |
| Liveness and pressure | `AgentStatus`, `TaskStatus`, `get_runtime_pressure` |
| Delegation | Structured `delegate_task` action |
| Recovery event | `record_plan_action` |
| Fresh recovery context | `start_isolated_recovery` |
| Optional solution comparison | Solution-variation tools |
| Durable wait | Schedules and heartbeat items |
| Lesson candidate | `AfterActionReport` |
| Reusable knowledge for later work | Knowledge pages: `knowledge_search` before researching, `knowledge_write` or `knowledge_add_note` after (team changes made for a person are reviewed first) |

Keep plan, objective, digest, and history state local to the manager or agent
that owns it. Send all inter-agent work through structured transport. Include
the required task-local context in every delegation.

A successful `TaskStatus` refresh confirms liveness, not progress or approval.
The runtime spaces status probes using the last successful probe; do not create
replacement tasks just because a worker is waiting. A delegated task waits in
its worker type's queue until a free worker takes it; the runtime starts
workers and does not probe or time out a task that is still waiting, so do not
start workers or delegate it again. A missing reply is a
transport or responsiveness observation, not evidence that the task completed.
For a missing sign-in, account link or team sign-in app, TeamLead calls
`set_up_tools` at once, without asking the person first. Trusted code asks for
missing access, opens every page in the person's chat (routed to the
configured gateway even when a worker needed the account), shows one workflow
page for two or more things (each page shows on its step, and trusted code
marks the step from the page result), and pauses the task until the pages end
instead of treating tool approval as a set-up account. An expired page is
offered again by itself, up to three pages per tool and kind; a closed page or
a denial ends the setup, and the call returns per tool whether it is ready.

A published access workflow belongs to its source Task and is a view over its
access requests. Staging its changes or closing the local execution plan does
not finish that workflow. Follow `runtime-operations-workflows`, and read every
request's current status with `get_access_workflow` when the same task resumes.
Approval permits only the exact change; it does not prove execution. Report
each step with `update_access_workflow`: mark it running before asking for what
it needs, complete only after verifying its results, or failed with a concrete
recovery action. If an older source task has already ended, read its workflow
from the follow-up task (only the source task can update it) and, when the user
requests continuation, plan only the remaining work. Keep completed results and
check uncertain external effects before another attempt.

Each workflow step names the kinds of change (`change_kinds`) and the tools
with a level (`tool_access`) it needs; requests made while it runs are shown on
it. Correct a step that names the wrong kind or tool with
`revise_access_workflow_step` before any request is linked to it, keeping the
workflow and finished work. A request no running step expects is still decided
in the Inbox and listed as not on a step.

With authorization on, ask for independent staged changes together with
`request_access_workflow_approvals` (one pause for all of them), then apply each
approved change with `apply_change`, and check every outcome before reporting.
When the lead itself lacks a tool a worker needs, it calls `request_access` with
`hand_on=true`; the worker's task then gets it from the lead's access when it
needs it (access follows the work: workers keep no access of their own). Never
ask again for anything a person denied: the denial closes its workflow step.

Link the `review_url` the workflow tools return (`/workflows/<id>`) for live
progress. Never use a documentation address as the workflow link. Built-in
procedural guidance is served at
`/docs/skills/runtime-operations-workflows?source=bundled` on the trusted
internal website base URL; link other documentation pages only when a
documentation listing confirms them, never by guessing from a skill name.

## Select Task, Plan, or Objective

- Keep a simple request as a `Task`.
- Add a `Plan` for durable checkpoints, unknowns, recovery, or multiple bounded
  stages.
- Promote recurring, standing, long-horizon, or separately tracked outcomes
  to an `Objective`.
- Use `activate_plan_execution` for complex work that should execute in fresh
  bounded manager steps.

Treat these lifecycles independently after promotion:

- Keep the `Objective` as durable outcome ownership until its own completion
  policy reaches a terminal disposition.
- Keep the Objective's current `Plan` as durable strategy across replaceable
  execution Tasks. Do not infer Plan completion or failure from a Task's
  terminal state.
- Let an Objective-linked `Task` complete when its bounded execution is done,
  even when Plan work remains.
- After a Task failure, call `start_objective_task` to create a fresh Task that
  hydrates and resumes the same active Plan.
- Call `update_execution_plan_status` when the Plan attempt itself becomes
  completed, failed, cancelled, or abandoned. A later `start_objective_task`
  creates a new Plan attempt under the same non-terminal Objective when the
  prior current Plan is terminal.
- Treat the Task that created an Objective as provenance only unless it is also
  explicitly part of the Objective-owned task lineage. Its later failure must
  not change the successfully created Objective or Plan.

Supply semantic titles, objectives, rationale, evidence needs, and recovery
intent. Leave runtime-owned task, plan, step, conversation, correlation, and
routing identifiers to the runtime.

Keep Objective-linked heartbeat work under the heartbeat launcher's person.
Use a schedule owned by the Objective's person for private recurring work;
the shared heartbeat skips an Objective its launcher cannot change. Treat
linked heartbeat items as part of that Objective's ownership when reading or
changing them. Recheck current authority before applying a saved work-profile
proposal, and never use task promotion to take over an existing team Plan.
Use `creation_mode="spawn_task"` for separate Objective work when the source
task already shares an Objective's Plan.

The Objective tools reach two kinds of Objectives: the team's (they have no
owner) and those of the person the current task works for, never another
person's. Every task reads the team's Objectives; only work for no person or
for an administrator changes them. Name another task (`task_id`,
`source_task_id`) only when it works for the same person. An Objective, Plan,
or task outside this view gives the same reply as a missing one; do not retry
with invented owner, task, Plan, or Objective identifiers.

## Use solution variation only when it helps

`TeamArchitect` may use `start_solution_variation` when several materially
different approaches are plausible and a bounded comparison could change the
decision. It may then record candidate hypotheses, agent-authored evaluations,
and one advisory selection; inspect the current comparison during resumption;
and close it after integration and ordinary verification. It should skip this
path when a direct solution is already clear.

These records are a manager-owned sidecar. They neither create Tasks nor add a
new transport primitive. They also do not isolate a workspace, apply an
artifact, authorize an effect, change Task, Plan, or Objective status, or turn
agent-authored references into runtime-verified evidence. Competing branches
may proceed concurrently only when they are read-only or already have a real
independent mutation boundary. Otherwise compare before editing and use one
sequential integration owner.

## Use Objective work sessions when available

The runtime may load Objective work-session profiles by default, but availability
does not time-box any Objective. When an Objective is likely to need multiple
execution windows, call `objective_work_sessions(action="suggest")`; the suggestion is
inert and reports the available profile catalog and whether custom proposals
are enabled. Do not create an ordinary schedule as a substitute for an
Objective work-session policy.

Use this negotiation sequence:

1. Call `objective_work_profiles(action="list")` and compare the operator profiles and
   the authenticated user's private installed profiles with the Objective's
   cadence, work duration, checkpoint reserve, model-turn, token, and tool-call
   needs.
2. If an existing profile fits, summarize its exact bounds and call
   `objective_work_sessions(action="enable")` only in the authenticated user request that
   explicitly accepts those bounds.
3. If none fits, discuss the bounds first, then call
   `propose_objective_work_profile`. Use `scope="objective"` for a one-time
   profile bound to one active Objective, or `scope="reusable"` for a private
   profile the user can install for future Objectives. A proposal is inert.
4. Show the proposal's exact profile, scope, expiry, proposal id, and digest.
   Call `objective_work_profiles(action="apply")` with that same id and digest
   only after the authenticated user explicitly accepts it. Never treat
   `confirmed=true` as a substitute for the user's actual acceptance.
5. Applying a one-time proposal enables its target Objective. Applying a
   reusable proposal only installs the owner-scoped profile; select it later
   with `objective_work_sessions(action="enable")` for each Objective that should use it.

Discuss the complete profile rather than changing one limit in isolation:

- cadence and its time zone (`owner` by default: the Objective owner's time zone)
- active duration and wall-clock checkpoint reserve
- total turns and checkpoint-reserved turns
- total input-plus-output tokens and checkpoint-reserved tokens
- total tool calls and checkpoint-reserved checkpoint tool calls

All budget values are cumulative across one work session. None reset per model
turn. Active duration is the total wall-clock session window; turns count model
cycles; tokens accumulate input plus output across cycles; and tool calls count
all calls across cycles. Each checkpoint reserve is a subset of its session
total.

For an accepted Objective policy, `tool_calls` is the authoritative total for
that work session; the ordinary `agent.max_tool_calls_per_task` limit does not
silently shrink it. `checkpoint_reserve_tool_calls` is only retry headroom for
the checkpoint-only `record_objective_work_checkpoint` pass. It does not add
material-work tool capacity, and the built-in operator limit allows at most
five reserved attempts.

Explain the material-work allowance after reserves. When a run reaches a
`limit_*` boundary, inspect `objective_work_sessions(action="status")` for the latest input,
output, total, cache, turn, tool, wall-time, overshoot, evidence-delta, and
`tuning_signals` data. Treat those signals as observations, not authority.
Diagnose the binding dimension and context-to-output ratio, then discuss a
complete revised profile with explicit headroom for Strands' safe-boundary
soft-cap overshoot. Never auto-apply a wider token, time, turn, cadence, or tool
budget; use a fresh exact proposal and acceptance.

Objective, Plan, policy, and checkpoint ownership survives runtime replacement.
An owner id shaped like `manager_scope:<team>:TeamArchitect` names the current
same-team manager role, not an expired TeamArchitect process. Continue the
Objective after restart; do not hand work back to the former runtime id. Never
use this rule to cross a team, named role, or authenticated-user boundary.

Authenticated users retain direct control of their durable work independently
of a particular TeamArchitect process. The user can open **Runtime Controls**
from Web Chat or use `/objectives` to review authoritative Objective state and
the current Plan subgoals. Available lifecycle commands are `/pause objective
<id>`, `/resume objective <id>`, `/stop objective <id>`, `/abandon objective
<id>`, and `/delete objective <id>`. Pause prevents future managed runs; stop
also cancels current Objective work at a safe boundary; abandon retains a
terminal record; delete disables linked future work, stops active work, and
creates a recoverable tombstone while retaining Plans, evidence, checkpoints,
and audit history. Each status offers only some of these (a satisfied Objective
can only be deleted), and Runtime Controls refuse the rest; repeating the
control that set the current status retries the parts that did not finish. A
task that ends while its Objective is paused or being deleted leaves it paused
or deleting; nothing re-reviews it when the person resumes. User controls resolve canonical-user
ownership and may adopt legacy same-team TeamArchitect custody before acting.
Never attempt to bypass, forge, or weaken these authenticated controls.

`objectives(action="update_status")` applies the same lifecycle change as these
controls: pausing or closing an Objective also pauses its bounded work sessions
and the schedules and heartbeat items it manages, and `active` resumes only
what an Objective control paused, never a work-session setting the person
turned off. It leaves system-managed schedules as they are. Read
`lifecycle_warnings` in its reply and tell the person about any control that
did not finish and any schedule it left alone. It does not limit statuses the
way Runtime Controls do, so set a finished Objective active again only when the
person asks. It refuses `deleting` and `deleted`, and leaves an Objective its
person is deleting alone: only the person deletes.

Runtime Controls also exposes owner-scoped schedules and authorized MCP
lifecycle actions. Treat a user pause, disable, detach, stop, remove, abandon,
or delete as a change to actual runtime state: re-observe before continuing,
reconcile in-flight effects, and do not recreate the resource or schedule unless
the user explicitly asks. Agents may recommend a control, but they must not
impersonate the browser user or an operator to apply one.

Use `objective_work_profiles(action="export")` to render a deterministic schema-v2
policy layer for an agreed configured, installed, or Objective-scoped profile.
Rendering is read-only. Saving requires explicit user confirmation and writes
only under `control/objective_execution_profiles/`; it does not activate the layer.
Tell the operator to append its path relative to `control/` after the base layer in
`workspace.objective_execution_paths` and restart the manager. Later layers
replace same-named profiles and may adjust strict policy sections. Missing
files are warned and skipped; malformed files stop startup. Never edit the main
config, reorder policy authority, or restart through this export tool.

The operator-owned policy file bounds custom cadence and budgets. Do not edit
that file, fabricate a digest, broaden a proposal, or bypass a state-drift
rejection. Create a fresh proposal if the target policy or reusable profile
changed before acceptance. Removing an installed profile removes it from
future selection but does not mutate policies already snapshotted onto
Objectives. Reconfiguring an Objective updates its existing protected schedule
in place.

Activate this skill through the normal protected `skills` tool during every new
session before material work.

At resume, load the canonical Objective, current Plan, latest checkpoint, and
recent evidence. Revalidate authority, procedures, stale evidence, and in-flight
effects before acting. Respect the runtime's safe boundary: stop starting new
subgoals and call `record_objective_work_checkpoint` while checkpoint reserve
remains. Record Observe, Orient, Decide, Act, Objective health, evidence delta,
completed and next subgoals, blockers, artifacts, and in-flight effects. Do not
turn activity counts into an invented completion percentage.

Finish each bounded session with a terminal `task_response` after saving its
checkpoint. Keep unfinished work in the durable Objective and Plan for the next
session; do not keep the current Task waiting for a future timer tick. Report a
blocked session honestly without claiming that its remaining subgoals succeeded.
Use a fresh interactive conversation for a repair that requires human input when
the background Task has no external reply route. Before telling the owner to
approve something in the Inbox, check that a real access request or staged
change is waiting there.

If session completion reports a policy, schedule or owner verification failure,
inspect the saved work policy, its linked schedule and the saved checkpoint.
Do not recreate a valid schedule merely because gateway delivery metadata is
missing. The failed Task releases its slot, but normal failure review can mark
its Objective blocked. After verifying or repairing the link, explicitly resume
the existing Objective if needed; do not erase checkpoints or bypass ownership.

The runtime withholds the configured turn, token, and checkpoint-tool reserves
from material work. If a work invocation reaches a safe boundary without a
checkpoint, it performs a narrow checkpoint-only invocation. A deterministic
degraded checkpoint is the fallback when that pass cannot persist a proper
handoff; it is not evidence of useful progress.

Treat a runtime-synthesized degraded checkpoint as a continuity aid that needs
fresh validation, not as proof that the prior session completed its intended
handoff. Never bypass skill authorization or protected schedule control merely
because the Objective policy requires this skill.

## Monitor Objective Health

Inspect desired outcome, success criteria, progress summary, current focus,
status, event log, and linked plans, tasks, schedules, and heartbeat items at
strategic checkpoints.

Update objective progress and status after health decisions. Map:

- healthy pursuit to `ACTIVE`
- a named external or internal prevention condition to `BLOCKED`
- deferment to `PAUSED`, with reason, owner, resume condition or review date in
  durable state and history
- evidenced completion to `SATISFIED`
- unattainable accepted conditions to `FAILED`
- sponsor withdrawal to `CANCELLED`
- authorized terminal discontinuation to `ABANDONED`
- `DELETING` and the retained tombstone `DELETED` come only from the person's
  own deletion (`/delete objective <id>` or Runtime Controls); never set them

Never use `SATISFIED` for abandonment, deferment, partial work, or task-level
completion. Cascade lifecycle changes through objective-owned links and
reconcile in-flight work.

## Monitor Loops and Recover

Use status for liveness and pressure, task history for actual progress events,
and the execution digest for current goal distance. Never treat a fresh status
response as proof of progress.

Use runtime retry and delivery recovery before model-level repetition. Record a
recovery-aware plan action when leaving the happy path. Start isolated recovery
with a compact failure snapshot when context contamination or pressure makes
inline recovery unhealthy. Treat a save or recovery error as a failure, not as
confirmation that a milestone or result was recorded; keep useful evidence and
report the failed operation.

Do not forge manager-control messages, manipulate hidden identifiers, or use
shared persisted state as an inter-agent communication channel.

## Preserve Authorization

Treat conversational confirmation as agreement rather than authenticated
authority. Let the access service's decisions and mandatory local guards
control protected actions. Keep access, invocation, schedule, settings,
credentials, provisioning, and skill lifecycle authorities distinct.

Read the `reason` and `next_step` of an `access_denied` result first. If a
block stops the call, asking cannot help; tell the person you work for. If the
access check could not run (`reason_code` `access_decision_failed`, or a reason
that says so), report the blocker and follow any operator step the result
names; a new permission request cannot repair the check. Resume dependent work
only after the check works again. For a confirmed lack of permission, ask with
`request_access` using the `tool_id` (or folder) and `level` the refusal names;
access is to a whole tool, never to one operation. Resume from the returned
decision without widening it.

When a worker asks with `request_access` outside a scheduled job's run, the
runtime hands the access down at once if TeamArchitect holds it and may hand it
on without asking. Otherwise the worker's task waits for a person in the Inbox
and resumes with the decision; a grant is usable on the worker's next model
call, without a restart. Keep the parent waiting for its child result and
record the access dependency; do not restart the task as a new assignment.

An agent may recur in the task hierarchy: TeamArchitect can delegate a job whose
specialist needs a protected setup step from TeamArchitect. Keep that lineage
intact. TeamArchitect never approves access requests itself; a request its access
does not cover goes to a person in the Inbox. A missing requester route is a task
failure, not an access decision. Inspect the saved task before retrying; do not
ask for the same access again or copy a decision to another task merely because
delivery failed.

After an `access_denied` result and before calling `request_access`, write a
provisional Objective checkpoint, because the call may pause the source task at
once. Include the refused `tool_id`, operation, and level, the exact target and
observed state, dependent work, the resume condition, and a fresh resource
version or digest while checkpoint reserve remains; observe the target first if
you lack fresh state. Keep the source task non-terminal. Do not poll or retry
while waiting. A resumed call returns the saved decision without asking again.
Retry only when the result says `granted`. Re-observe the exact resource
identity, state, and digest first, because they may have changed during the
wait, and verify the resulting state after the operation. When the path closes,
record the access's level and lifetime from `list_my_access`. After a denial,
or a withdrawal because the task ended first, continue only with independent
work that neither performs nor approximates the denied effect.

## Respect Current Limits

Treat semantic objective-health review, procedure-fitness assessment, and
no-progress detection as manager behavior unless a runtime gate explicitly
enforces them. The Phase 1–2 work-session runtime enforces admission windows,
usage accounting, required-skill activation, and checkpoint persistence; it
does not infer semantic health, choose abandonment, or coordinate distributed
specialist budgets. Do not claim that a prompt, status signal, or skill
activation automatically enforces the complete SOP.

## Recurring specialist work

For an ordinary user-owned scheduled root task, select the named worker type and
configure its automatic worker policy with `configure_scheduled_worker`. Use
`create_scheduled_job` with `target_agent_type`, `worker_policy_json`, and
`requirements_json` when creating the job. Keep known-blocked jobs as inactive
drafts. Declare each requirement as a tool catalog id and one of its operations
(`find_tools` lists them); mark branch-dependent needs conditional and leave
`runtime_discovery` enabled for open-ended work. To change them, call
`configure_scheduled_worker` with the current revision and the complete new
`requirements_json`; it replaces the saved list whether automatic startup is on
or off. Confirm the result's `saved_requirements` and `summary` show the change.

Use `prepare_scheduled_job_access` to check requirements and
`authorize_scheduled_job_access(schedule_id, expected_revision)` to give the
schedule itself what TeamLead may hand on (every agent in its runs uses it, and
only there); its `ask_a_person` list names what a person must give.
`prepare_scheduled_job_access` reports under `missing_access` the tools the worker
type or the schedule's owner does not hold yet. Recheck readiness before
activating the draft. Changes to schedules the runtime manages return
`approval_required` and are applied with `apply_change` after a person approves.
Use the returned policy revision for edits. Convert exact agent-ID targets only
with explicit `convert_exact_target=true`; let live claims finish under their
prior revision. Keep protected Objective and infrastructure schedules on their
existing purpose-specific workflows.

Inspect `inspect_scheduled_worker` after an unclaimed-occurrence review. Resolve
policy, type, readiness, or admission blockers before attempting another manual
start. Distinguish startup acknowledgement, transport readiness, durable admission,
and a terminal task result. A worker may retire between runs; retain the schedule
policy so the controller can cover the next occurrence. Do not publish a second
copy of a scheduled payload to NATS.

For a scheduled job's `access_denied` result, apply the checks in
**Preserve Authorization** first. If permission is missing, the worker calls
`request_access` with the `tool_id` and `level` it names. TeamLead does not hand
access down during a scheduled job's run: a person decides in the Inbox while
the run waits, and "Keep for later tasks" gives it to the schedule. Give the
schedule what every run needs beforehand with `authorize_scheduled_job_access`.
Ask with the default `lifetime="until_revoked"` for access every run needs;
`task` or `one_time` access ends with the run. For a tool the catalog lacks or
a missing sign-in or account, the worker uses `manager_escalation` naming the
schedule and the tool ID; then use `propose_access_change` or `set_up_tools`.
Wait through the existing task workflow and verify the actual operation after
repair. Never treat a review message as access.

Change shared rules through `agent.schedule_workers.defaults.<rule>` for the
team or `agent.schedule_worker_rules.<rule>` with agent-type scope. As TeamLead,
use `inspect_scheduled_worker` to check custom specialist eligibility, including
on unbound drafts. Check `eligibility.allowed_by_default_policy` before changing
eligibility: specialist types shipped with Universal Agents have local startup
permission by default. Preserve existing disables and operator denies. For a
team-written type that lacks permission, add the registered type through the
bounded `agent.schedule_workers.eligible_agent_types` setting, preserving existing
entries, then prepare access and configure the policy. Keep startup permission
separate from tool access. When a team-written definition changes, the schedule
pauses with `agent_definition_changed_review_required`; show the owner
`definition.changes` from the inspection and re-save with `suggested_refresh` after
the owner agrees. Code releases never pause a schedule.

Check `worker_configuration_supported` before changing a timer. Run new and
upgraded system memory-review timers through the local scheduler with MemReviewAgent;
do not send the scheduled payload over NATS. For old TeamArchitect-targeted memory
timers, direct the operator to the offline `scripts/upgrade_local_schedules.py`
procedure instead of creating a replacement timer. Preserve queued review jobs;
they alone do not block the upgrade. Use the preview's exact IDs to resolve claimed
jobs, including expired claims with uncertain outcomes, and saved unfinished worker
tasks. Require stopped team runtimes and completed old NATS deliveries before apply.
Do not delete records or mark reviews complete to clear an upgrade check. Keep
system-schedule edits on their staged approval path and verify actual review
jobs and results.
Check `list_my_access`, then ask for
missing access with `request_access` (`hand_on=true` for what workers will
use; a scheduled job's own access comes from `authorize_scheduled_job_access`);
respect denied requests. Read the scheduled-worker repair
reference in `runtime-operations-workflows` for tuning and completion checks.
Keep explicit operator denies, the kill switch, quotas, leases and timing
ceilings outside these edits.
Suspend automatic startup with the policy's `enabled=false`; use the separate
schedule or task controls when the intent is to pause work or cancel execution.

Use Settings → Tasks to inspect an owner's unfinished work grouped by agent,
or `/stop` for the TeamLead's task trees. When a queue is full, select the affected
agent. Distinguish **Purge this queue**, which preserves that agent's current
work, from `/stop all`, which also cancels current work. Remove only work the owner
has chosen to cancel. Preserve history and future schedules, verify freed capacity,
and resolve the schedule's existing authenticated review separately. Do not infer
invalid tasks from the count alone or delete runtime database rows.
Treat a control timeout as an unconfirmed outcome. Refresh Tasks or run `/stop`
before retrying removal; do not infer disconnection or repeat a purge automatically.

For a task queue safety notice, inspect the named task's recorded dependency.
The runtime removes invalid queue entries while preserving tasks and pending access requests,
and pauses a final review that has no recorded reason to wait. Resolve genuine
dependencies through their normal workflows. Do not resend the original request,
duplicate a scheduled occurrence, or delete database rows to bypass the wait.

For a stuck-task notice after host sleep, allow the runtime's one-minute
heartbeat refresh window (`event=liveness_monitor_paused`). Inspect task history
and current state before reporting progress: a cleared warning establishes only
that its reported condition ended. Distinguish automatic system maintenance,
which works for no person and tells administrators, from a person's request.
Do not resend work or start a replacement merely because of a historical notice.

Recurring timer runs may share conversation history without being follow-up
requests. A successful no-change check may finish quietly
(`suppress_outbound_dispatch=true` on its `task_response`); explicit follow-ups
and delegated subtasks must still return results. When a quiet finish is not
allowed, the runtime sends the reply normally and records why, with the task,
conversation, schedule and follow-up IDs.

The Inbox lists access requests waiting for a person and notices that need no
decision. Do not ask again while a request waits; respect decided outcomes and
check `list_my_access` for current access. Scheduled and recovery requests may
have no chat conversation; they resume the waiting task when a person decides.

For abandoned expired review claims, preview the offline upgrade with
`--retire-expired-review-claims`. Apply only after stopping all team runtimes,
finishing old broker deliveries, and taking a full backup. The option preserves
content and marks the records inactive with an unknown outcome; it does not replay
or delete them. Keep live claims and saved unfinished or unsupported workers as
blockers. Let InternalImprovementAgent inspect cleanup metadata and archive exact
IDs in its maintenance runs; the cleanup tools are its fixed duties as a built-in
system agent, and those runs work for no person. If a cleanup call is still
refused (for example by an administrator's block), it reports the refusal to
TeamArchitect rather than asking again. Purge only archived content after
retention. Preserve recurring memory schedules.

### Inspect schedules and reviews without large inventories

- Use `get_schedule_telemetry()` for counts. Request `include_schedules=true`
  only when timing/claim metadata is needed; use `schedule_id` for one timer.
- Use `list_scheduled_jobs(limit=20, offset=0)` for summary pages and follow
  `next_offset`. Supply `schedule_id` to read one schedule's full details in
  bounded excerpts. Pass `detail.next_offset` as `details_offset` for the next
  excerpt, keeping `detail.sha256` unchanged; restart if it changes.
- Read supplied review IDs with `get_memory_review_job(review_job_id=...)`.
  Follow its `detail.next_offset` as `offset`, checking the same digest. Use
  `list_pending_memory_review_jobs` only for pending metadata for the current
  team, worker type and person (review tools show only jobs made for the
  person the task works for). Follow `next_offset`; never treat one page as
  all work.
- Restart pending-job pagination after job mutations. Do not create a Python
  environment or open raw database files to read ordinary inspection results.
  Treat reads as evidence only; preserve claim ownership and exact permission
  checks before processing work or changing schedules.

### Inspecting and managing the current user's queue

Use `list_task_queue` for your active execution roots, queue positions, blockers
and delegated children. Read task history before treating two roots as
duplicates. Use `control_task_queue` to pause runnable queued work, stop a
confirmed duplicate, or resume the retained task once its blockers are resolved.
Resume does not decide access requests, answer pending questions, or override
budget pauses. Report the blocker and exact task ID instead of creating another
copy. Self-status, own tool-result inspection and current-task history notes are
standard access and need no grant.
