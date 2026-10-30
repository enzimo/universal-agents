# Scheduled worker repair

Use this procedure for an ordinary user-owned recurring specialist task, either
at setup or after an unclaimed-occurrence review. Keep protected Objective work
sessions and infrastructure timers on their existing lifecycle workflows.

## Identify who owns execution before repairing it

Read `worker_configuration_supported` from `inspect_scheduled_worker` first.
When false, follow `execution` and `repair_instructions`; do not apply ordinary
worker eligibility or configuration steps. A null `eligibility` means this
mechanism does not apply, not that a specialist is disallowed.

System memory-review timers now belong to MemReviewAgent and use local database
claims, startup policies and occurrence records. Do not delegate the scheduled
payload over NATS or create a replacement schedule. Old timers targeting
TeamArchitect require the explicit offline `scripts/upgrade_local_schedules.py`
procedure; report that requirement instead of attempting an agent-authored retarget.
The preview distinguishes queued review records from claimed jobs and saved worker
tasks. Queued records alone do not block the upgrade and must be preserved. Use
the reported job/task/worker IDs to check actual blockers, including expired claims
with uncertain outcomes. Route unsupported saved runtimes through the existing
snapshot upgrade procedure. Do not delete records or mark jobs complete to clear the
check. The operator must stop all team runtimes and finish old NATS deliveries
before applying; queued database status does not establish broker delivery status.
For a large preview, use `scripts/summarize_local_schedule_upgrade.py` on the full
saved output, including stderr. Start with `look_at_first`, then inspect expired
claims grouped by worker. Use `--worker` for exact expired job details. Treat expiry
as a diagnostic distinction, not proof of completion or permission to retry; the
read-only filter does not clear upgrade blockers.
New and upgraded timers are schedules the runtime manages. Any later policy edit
through `configure_scheduled_worker` returns `approval_required` with a
`change_id`; apply it with `apply_change` after a person approves the exact
change. Their worker type must remain MemReviewAgent. Knowledge curation timers
(`knowledge_curation:person:` and `knowledge_curation:team:`) work the same way
and must remain KnowledgeCuratorAgent; a run with nothing to review is skipped
on purpose, not stuck. Other system timers and Objective timers keep their own
workflows.

The default startup policy covers specialist types shipped with Universal Agents,
including types a later release adds. `eligibility.allowed_by_default_policy`
means no eligibility edit or renewed startup approval is needed. Team-written types
(generated specialists, team definitions in `control/agent_definitions`, a team
redefinition of a shipped type) need an `eligible_agent_types` entry. Existing disabled policies and
operator denies remain effective. Startup permission never grants tool or
credential access. Preserve valid existing access and request only what an actual
readiness check or trusted denial says is missing.

## Changed agent definitions

`repair_instructions` in `inspect_scheduled_worker` is specific to the block
reason; read it before acting. For `agent_definition_changed_review_required`:

1. Read `definition.changes`: role, own prompt before and after, default tools,
   skills setting and source. `saved_record_available=false` means there is no
   usable saved copy (the policy was saved before definition records existed,
   or before default tools replaced tool groups); show the current definition
   instead.
2. Show the owner the differences and ask whether the schedule should run with the
   new definition. The owner was already told the schedule is stuck; do this in
   their conversation.
3. After the owner agrees, call `configure_scheduled_worker` with the values in
   `suggested_refresh`. Leave `worker_policy_json` empty to keep the saved startup
   settings; `"{}"` resets them to defaults.
4. Access belongs to the worker type, so re-saving keeps it. Run
   `prepare_scheduled_job_access` to see whether the new definition needs more,
   then verify an admitted occurrence and a completed run.

A shipped definition never causes this block. When a release changes a scheduled
worker's tools or prompt, the schedule keeps running and
`definition.last_release_change` records what changed; mention it to the owner
when it matters for the job.

The local scheduler keeps working while the lead is busy or paused. It checks the
local worker registry; a remote NATS responder does not satisfy a local schedule.
Inspect actual admission and completion before reporting successful repair.

For a one-time reminder or the final run of a counted job, an admitted run
keeps the schedule and its access until the work finishes. Zero remaining
runs means it cannot fire again while finishing, not that its active run
lost authority. Do not move reminders to a permanently running agent to avoid
worker replacement. Inspect the run's history and outcome instead. A failed
top-level scheduled task sends its owner a runtime failure notice, including
when it started on time. That notice survives archival and restarts; check
what effects occurred before offering a retry.

## Select and enable the intended specialist

1. Call `get_available_agent_types` and choose an exact registered type that can
   perform the job. Include custom types in this search. A specialist created
   with `specialist_definitions(action="plan")` is listed, startable and schedulable as
   soon as its change is applied; it needs no restart.
2. Call `inspect_scheduled_worker(schedule_id)`, including for an inactive draft
   with no automatic policy. Read `eligibility`, current and desired assignment,
   policy revision, readiness, attempts and the last occurrence. Re-read evidence
   on every review because the original incident may already have recovered.
3. If a registered type has neither default-policy eligibility nor membership in
   `agent.schedule_workers.eligible_agent_types`, add it through
   `plan_runtime_setting_change` and `apply_change`. Pass the
   inspection's `suggested_settings_change` to the planning tool, then use its
   returned `change_id` to apply. Before a schedule exists, read
   `schedule_workers.limits.eligible_agent_types` from
   `get_runtime_configuration_context`: it is the list the launcher enforces,
   read from the saved settings on every check. Preserve existing
   entries when adding a type. Do not replace the entire list with only the new
   type or assume the shipped two-type default is an exhaustive catalog.
4. Use team scope for eligibility. This lead-managed setting needs no separate
   conversational confirmation or restart: the next check reads it. If a check
   still reports `agent_type_ineligible_for_automatic_launch` after the change
   applied, report it as a runtime defect instead of planning the same change
   again or asking for a restart. Current setting authorization
   still applies. Planning and apply reject new entries that are unregistered,
   explicitly denied, or `TeamArchitect`. After a stale-plan error, inspect the
   current configuration and stage a fresh change rather than replaying it.
5. Respect `denied_agent_types`, the `enabled` kill switch, and operator limits.
   A removed definition or explicit deny invalidates later launches even if the
   type remains on the candidate list. Retarget to a suitable permitted type or
   report the exact operator decision needed. Do not remove a deny, silently
   substitute a less capable type, or bypass the block through manual creation.

For example, when `InvoiceReviewAgent` is registered but the list contains only
`WebResearchAgent` and `PersonalAdminAgent`, plan this exact change after checking
that those are still the current entries:

```json
{
  "path": "agent.schedule_workers.eligible_agent_types",
  "value": ["WebResearchAgent", "PersonalAdminAgent", "InvoiceReviewAgent"],
  "target_scope": "team",
  "reason": "Allow the registered invoice specialist to cover the recurring review"
}
```

Apply the returned plan. Do not call the schedule repaired yet. Candidate status
does not create a policy, launch a worker, or authorize an operation.

## Establish lasting coverage and access

Each requirement names a tool catalog id (`capability_id`) and one of that
tool's operations, never the function an agent calls to use it: `secure_cli`
runs every command-line tool and is not an operation. A job that checks Gmail
declares:

```json
{"requirements": [
  {"capability_id": "cli:google", "operation": "gmail_search", "necessity": "required"},
  {"capability_id": "cli:google", "operation": "gmail_read", "necessity": "required"}
]}
```

Add `gmail_send` only when the job sends mail. A check blocked with
`not_in_tool_catalog` names the valid operations in `suggestions` (the tool's
named operations first) and explains the mistake in `detail`;
`requirement_not_a_catalog_tool` suggests close tool ids. `find_tools` lists
every tool id and operation with its level. Requirements stay per operation so
the worker type is given only what the job uses; do not declare every operation
of a tool.

Preview exact known capabilities with `prepare_scheduled_job_access`. This tool
does not persist requirements or create a binding. For an unbound inactive draft,
call `configure_scheduled_worker` with the intended target, `requirements_json`,
`expected_revision=0` and `activate_schedule=false` to save the policy and
requirements first. Keep the schedule itself inactive during setup. Then call
`authorize_scheduled_job_access` with the returned revision, re-run preparation,
and activate through `configure_scheduled_worker` using the current revision.
Read each result and advance only after the prior step succeeds.

To replace a wrong requirement (for example `cli:google` / `secure_cli`), call
`configure_scheduled_worker` with the same target, the current revision, the
complete corrected `requirements_json` and `activate_schedule=true` once the
owner agrees. It replaces the saved list whether automatic startup is on or
off. The result's `saved_requirements` and `summary` state what was saved and
whether it is ready; if they still show the old requirement, stop and report it
rather than saving again.

Distinguish missing exposure, a missing account link, and missing access. Use
the existing attachment and account link workflows for the first two. For the
third, read `missing_access` from `prepare_scheduled_job_access`: each entry names
the tool, operation and level, and whether the worker type, the owner, or both
lack it. `authorize_scheduled_job_access` gives the worker type what you may hand
on and lists the rest under `ask_a_person` with the `request_access` call to use.
When the owner lacks a tool, only a team administrator can give it; tell the
owner. `list_my_access` shows what you hold and whether you may hand it on. Hand
on only what the job needs, at the lowest level its requirements need (prefer
read); access is one grant per tool, never one operation. Basic web search,
file read and job listing do not cover brokered Gmail or memory changes. Do not
treat adding eligibility as giving access. Recheck after each repair because
another requirement may still fail.

`ask_a_person` entries create no Inbox item by themselves. Do not mark the
workflow as waiting for user approval until a real request is waiting (a
hand-on under "ask me each time", or a worker's `request_access`).

Leave known-blocked drafts inactive. Keep conditional requirements conditional
and enable runtime discovery for unknown branches. When a run is refused, the
worker calls `request_access`; ask for lasting access (the default
`lifetime="until_revoked"`) so later runs keep it. Verify the real operation
after repair, since a review message grants no access.

Use `configure_scheduled_worker` with the inspected `expected_revision`, zero
for the first binding. Supply the intended policy, preserving expiry and rules
that should remain: `worker_policy_json` replaces the policy rather than patching
individual fields. Use `activate_schedule=true` only after known requirements
pass. Convert an exact agent-ID target explicitly with `convert_exact_target=true`
when the intended behavior is replaceable workers. For a retarget, preview the
new type and requirements, save them in the binding, then give access for the
returned revision. Giving access before saving the new target would give it to
the old type. Wait for a live claim to end before expecting a retarget to take
effect. Recheck access for the new type.

## Choose the smallest useful adjustment

Prefer a per-schedule `rules` override for one job. Use runtime settings at
`agent.schedule_worker_rules.<rule>` with `target_scope="agent_type"` and the
exact `target_agent_type` for shared type needs. Use
`agent.schedule_workers.defaults.<rule>` at team scope for team-wide defaults.
Per-schedule overrides win over type rules, which win over team defaults.

| Evidence | Adjustment or next action |
| --- | --- |
| No worker claims the due job | Check assignment and eligibility first; lower `claim_wait_seconds` if waiting for an existing worker is unnecessary. |
| Workers normally arrive shortly after due time | Raise `claim_wait_seconds` to avoid avoidable starts. |
| Startup is healthy but consistently slower than the deadline | Raise `startup_timeout_seconds` within the operator ceiling; inspect startup errors before extending it. |
| Worker is ready but the task is not admitted | Inspect exact target, current claim, custody and access; tune `admission_timeout_seconds` only for demonstrated admission delay. |
| Transient startup failures | Tune `max_launch_attempts_per_occurrence` and `retry_backoff_seconds` within limits. Do not retry denied access, removed definitions, or unknown liveness blindly. |
| `launch_liveness_unknown` | A start whose process could not be checked. It clears by itself once the process is proven gone: after a restart, starts from the earlier run are settled on the next scan. If it stays, report the launch id to an administrator; never start a second worker for the occurrence. |
| Changed schedule after your own edit | An edit the owner asked for, or a change a person approved, moves the worker policy along automatically. A remaining `schedule_execution_changed_review_required` means the schedule changed some other way: show the owner, then re-save with `configure_scheduled_worker`. |
| All matching workers are busy | Consider `launch_when_busy=true` only when parallel work is appropriate. Starts stay within the type's worker pool size (one size covers scheduled and delegated work; see `worker-pools.md`). |
| Interrupted runs overlap the next run, or reruns are skipped too often | Tune `interrupted_run_skip_window_seconds`: a larger window skips more reruns near the next run; zero always reruns unless the schedule already ran again. |
| Repeated `interruption_review_` tasks for one schedule | Workers keep losing their launch lease. Look for storage errors or stalled workers in the evidence before retrying; `max_consecutive_interruptions` and `lease_seconds` are operator-owned. |
| A useful worker retires between nearby runs | Increase `idle_ttl_seconds`, or leave it unset to inherit team composition. Retirement is otherwise expected with durable automatic coverage. |
| Automation is disabled or its policy expired | Review the intent and revise the policy's `enabled` or `expires_at_utc`; preserve the other fields. |
| Schedule itself is disabled, paused, expired or finished | Use the schedule's ordinary controls after checking intended recurrence and remaining work. Enabling worker creation alone does not resume it. |
| Capacity/rate ceiling, kill switch or explicit deny blocks work | Report the exact operator limit and evidence, or choose a suitable permitted assignment. These controls are not lead-editable. |
| Definition, execution digest or access requirements changed | Reinspect, prepare and review the current policy; do not reuse stale access evidence. |

## Verify and close

Distinguish a starting worker, transport readiness, durable task admission and a
terminal task result. Preserve the original pending occurrence until admission;
never publish a second scheduled payload to NATS or count the review as a run.
Verify the saved target, eligibility, enabled automatic policy and access for
future occurrences. If the next run is not due yet, report that future coverage
is configured and distinguish it from a run already observed. Report an
unresolved blocker to the owner with the exact needed action.


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
