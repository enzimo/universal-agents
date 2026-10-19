# Universal Agents architecture

Universal Agents is a runtime for teams of AI agents. It can run on a
workstation, in containers, or across several hosts.

The main architectural rule: models decide what work to attempt, while
deterministic runtime components own identity, validation, routing, persistence,
authority, credentials, delivery, retries, and recovery. Model output proposes
actions; trusted code decides whether and how they take effect.

## System map

```text
users and external apps
          |
          v
channel adapters and assistant gateway
          |
          v
      NATS / JetStream
          |
          +----> TeamArchitect manager ----> named specialist queues
          |                                        |
          |                                        v
          |                                specialist runtimes
          |
          +----> replies, control, and presence

execution state ----> deterministic execution controller
                              |
                              +----> local processes
                              +----> OpenShell sandboxes
                              +----> Kubernetes jobs and services

domain repositories <---- gateway, agents, scheduler, controller
secure tool broker  <---- approved credentialed operations
activity collector  <---- curated status and progress events
```

The execution controller is a side service; normal user requests go from the
gateway through NATS to the manager and specialists. NATS and JetStream are the
inter-process transport for work, replies, control messages, and presence. They
are not the database for business state, authorization, or credentials.

## Component ownership

| Component | Owns |
| --- | --- |
| Assistant gateway | Canonical user identity, human channels, sessions, accepted requests, attachments, presentation history, and delivery |
| `TeamArchitect` | Root Tasks, Plans, Objectives, schedules that require reasoning, delegation, recovery, and final synthesis |
| Specialists | One assigned Task and its model and tool loop |
| NATS and JetStream | Typed message delivery, shared worker queues read by pull consumers, routing, redelivery, control, and presence |
| Domain repositories | Durable workflow, conversation, memory, authorization, gateway, and execution state |
| Secure tool broker | Credential catalog, secret-free references, credential hydration and use, external provider access, and redaction. It always runs: every credentialed tool call (web research, GitHub, Google, Hugging Face, X, MCP servers) goes through it, so no setting turns it off |
| Execution controller | Immutable code revisions, deterministic jobs, fenced attempts, and managed-service reconciliation |
| SSH session broker | Pinned remote targets, broker-owned connection material, interactive PTYs, and bounded transcripts |
| Activity collector | A separate read model for progress and operational views |
| Trusted catalogs | Agent definitions (with their default catalog tools), prompt fragments, the tool catalog, credential templates, and approved runtime metadata |

Several components may run in one process or container. Co-location does not
transfer ownership or create a new authority path.

## How a request runs

1. A channel adapter sends text, files, or an app event to the gateway.
2. The gateway authenticates the user, resolves canonical identity, session,
   conversation, and request identifiers, persists admission state, and
   publishes a structured request.
3. JetStream delivers the request to `TeamArchitect`.
4. The manager admits the Task, assembles bounded context from repositories,
   and runs the model and tool loop.
5. If specialist work is needed, the manager publishes a child `Task` to a
   worker type's shared queue (or to an exact agent). A free worker of the type
   takes it, tells the manager it holds it, and returns a structured result
   through NATS. See [Worker pools](#worker-pools-and-parallel-work).
6. The manager merges results, handles clarification or recovery when needed,
   and publishes the terminal response.
7. The gateway correlates the response, records what the user may see, applies
   channel and delivery policy, and delivers it.

**The person the task works for.** The manager's bounded context pins two lines
about this person, read only from the task's verified identity: their known
tool setup states (see "Last known states" below) and their team role
(`custom_agents/person_role_context.py`, for example "Person you work for: a
member (not a team administrator)"). The role line grants nothing, because
every action is decided where it runs. It only shapes guidance, such as the
topics the `user-onboarding-guide` tour offers and the commands a reply
suggests, so members are not led into refusals. It is read rather than asked,
because asking would trust a claim the signed context already answers.

**Typed messages.** Agents communicate through typed Pydantic models serialized
as JSON, carrying routing, identity, correlation, task state, and result data.
Free-form prose may be content inside a typed field, but it is not the
agent-to-agent transport contract. Contract families that need replay or
cross-runtime compatibility use explicit versions.

**Teams and pull delivery.** Each team has a subject prefix and normally one
`TeamArchitect` plus named specialist pools. Every durable JetStream consumer
is a pull consumer: a receiver asks for its next message when it can act on it,
and NATS never picks a receiver and pushes. The transport adapter refuses a
push listener on the work stream, and an architecture test fails when
production code creates a deliver group or subscribes to NATS outside the
adapter (`tests/test_runtime_architecture_boundaries.py`).

**Presence.** Presence records let late joiners and the gateway discover live
agents, without making presence proof of identity or authority.

- Agent status requests and snapshots bypass the short broadcast age limit, so
  a restarted manager receives retained presence even when startup takes longer
  than 100 ms.
- Replay keeps the sender's original `last_seen_at`. Liveness checks use that
  time and process evidence, so an old snapshot never makes a stopped worker
  look newly alive.
- An explicit live probe accepts its correlated reply, or a presence snapshot
  whose sender time falls between the probe's start and arrival. Older retained
  snapshots remain discovery data and cannot verify an exact delegation target.
- Before changing discovery or acting on an exit, the receiver calls
  `custom_agents.agent_liveness.reconcile_received_status` to pick the newest
  sender-written status from the message, memory and the shared database. An
  old exit cannot fail current work after a newer running status, and an old
  running snapshot cannot restore an agent whose newer status says it exited.

## Work, scheduling, and recovery

### Worker pools and parallel work

TeamArchitect can hand out dozens of tasks at once, one `delegate_task` action
each. They run in parallel on as many workers as the team allows.

#### Queues and failed messages

- **Shared queues, pulled.** Each worker type has one queue
  (`<prefix>.type.<AgentType>.work`) read through one durable pull consumer. A
  worker asks for one task only while it can start it at once
  (`custom_agents/pool_intake.py`): no turn running, nothing queued locally,
  not paused, draining, winding down or waiting out a provider limit. Unstarted
  tasks stay in the queue, where any free worker takes them and their number
  can be read, instead of waiting in a busy worker's private inbox.
- **Mailboxes** (an agent's direct route, the lead route, manager control, the
  gateway's reply route) are pull consumers fetched continuously that stop
  asking while the agent's deferred inbox is full
  (`custom_transport/nats_pull.py`).
- **One message never stops a receiver.** A message the receiving release
  cannot read (malformed, or a message type or value another release added or
  removed) is logged without its content (`event=nats_message_undecodable`,
  with its id, type, delivery count and the failed field). It is given back to
  its queue after 10 seconds so a receiver that can read it may take it; its
  last allowed delivery ends it. An error that escapes a message's handling is
  logged (`event=nats_delivery_handler_failed`) and the message is given back
  the same way. The receiver keeps asking; otherwise a mailbox or the gateway's
  reply route would stop receiving, and a redelivered message would stop every
  worker of a queue in turn while the processes looked healthy.
- **Set-aside messages reach an administrator.**
  - On a failed message's last delivery, the receiver first saves the original
    bytes and non-reserved headers at `<prefix>.deadletter.raw.<copy-id>` and
    a separate diagnostic record at `<prefix>.deadletter.notice.<copy-id>`,
    both in `<PREFIX>_DEAD_LETTER` (kept up to 3 days and at most 64 MiB,
    oldest dropped first). Two records keep a broker-sized original within the
    message limit; diagnostic headers added to its body could close the
    receiver's connection.
  - Stable copy ids include source identity, publication time and content, so
    retries deduplicate and a recreated memory stream cannot reuse an old
    report.
  - On its scheduler pass the lead reads diagnostic records only
    (`custom_agents/dead_letter_watch.py`). It stores an Inbox notice for
    administrators, plus a message to the configured chat while that chat's
    person is still an active administrator, and acknowledges reports once
    either succeeds, even if the other fails. Reports cover up to 200 copies,
    list up to 10, and wait 15 minutes after success before the next batch.
    Notices name copy ids, message types, ids, receivers and reasons, never
    content.
  - An operator reads the retained bytes by the diagnostic header
    `UA-Dead-Letter-Raw-Subject`, using JetStream's last-message-by-subject
    lookup.
  - A failed copy publication is logged (`event=nats_dead_letter_failed`, with
    the raw subject) and the original still ends, so preservation is best
    effort when the broker cannot store the records. Deliberate expiry does not
    create a failed-message report.
- **Stopped receive loops.** A receive loop that ends anyway is not restarted.
  It is logged (`event=nats_pull_loop_stopped`) and shown as a problem on the
  Status page, which notifies administrators.

#### Claims and publishing

- **Claims.** A worker that admits a delegated task sends
  `DelegatedTaskClaimedV1` to the requester, which records the holder
  (`storage/delegated_task_repository.py`) and checks only that worker. Tasks
  still in a queue are neither probed nor timed out. Stops go to the holding
  worker; a task no worker took yet is withdrawn from the stream.
- **Record before publishing.** The requester writes the task's record before
  publishing it and adds the stream sequence afterwards. A free worker's claim
  can arrive before the publish call returns, and a record written later would
  turn it back into "waiting" with no holder, so stops would withdraw nothing
  and never reach the worker. A record with a holder goes back to "waiting"
  only through a handback by that holder.
- **Failed publishing.**
  - A delegation the outbox gives up publishing (its automatic retries are
    over) is failed with a cancellation record and its record closed
    (`event=delegated_task_never_published`).
  - A failed publish call whose task a worker already claimed did go through;
    only its acknowledgement was lost. The action is finished as sent, its
    post-publish steps run (the parent waits, a requested check-back is kept),
    and the rest of the turn's actions run
    (`event=delegated_task_publish_confirmed_by_claim`).
  - A publish that went through is recorded on the outbox action as soon as it
    is known (`publish_confirmed`, kept across later status changes and
    restarts). When a later step fails (for example storing the stream
    sequence), the retry runs only the steps after publishing; it never
    publishes or cancels the task again, so no cleanup cancels a task already
    waiting in NATS. These retries still count toward the automatic retry
    limit, so a persistent database error goes to the normal recovery path
    without cancelling the published task.
  - As a safeguard, the watchdog checks and times out a "waiting" record
    without a stream sequence once no outbox action is still trying to publish
    it (`event=delegated_record_never_published`). It does not go by the
    record's age alone, which misjudges a busy or restarted requester.

#### One holder per task

NATS hands a task out again when its acknowledgement does not arrive in time:
the worker that took it stopped after saving it, or NATS was unreachable for
longer than the wait. Custody is recorded team-wide, not per agent, so such a
copy never runs twice.

- **Custody row.** Each task an agent executes has one team-wide custody row
  (`runtime_task_custody`, `storage/task_custody_repository.py`), claimed in
  the transaction that saves the task and released when the task leaves the
  agent's saved work.
- **Copies held elsewhere** (`custom_agents/task_custody.py`).
  - A worker that receives a copy another agent holds drops it while that agent
    is alive (`event=task_copy_dropped_held_elsewhere`). It takes the task over
    when the holder is gone or handed it back after losing its lease
    (`event=task_custody_takeover`).
  - Whether the holder is gone is the team's owner-liveness rule
    (`agent_liveness.owner_liveness`, purpose `custody_admission`).
  - The takeover moves the row only while the judged-gone holder still holds
    it. A holder that released or finished the task meanwhile was alive after
    all: the copy is dropped and no fresh row is claimed for it, or, if the
    lead re-sent it after that holder's handback, it is admitted as a free
    task.
- **Finished tasks.**
  - Admission reads custody before the finished-task archive, because a holder
    archives a task before its save releases custody.
  - A copy of a task any agent of the team already finished is dropped.
  - Every custody acquisition (a fresh claim, a takeover and a recovery) also
    reads the finished-task archive in its own transaction and refuses a
    finished task, so a task another agent finished after the admission's own
    archive read is never claimed and run again.
- **The old holder lets go.**
  - A holder wrongly judged gone that keeps running finds its next save refused
    for that task (`custom_agents/custody_loss.py`,
    `event=task_custody_taken_over`). It drops the task from memory, stops its
    running turn without a reply, stops the work the task had delegated,
    withdraws the access requests it waited on, and keeps working. The task
    gets no cancellation record, because its new holder runs it.
  - The same happens when the row is gone although this agent never released
    it: the taker already finished the task
    (`event=task_custody_finished_elsewhere`) or gave it up unfinished, for
    example handed it back after a lost lease
    (`event=task_custody_released_elsewhere`).
  - The old holder never claims such a task afresh, because that would run it
    beside the copy the lead sends again. A task an operator retired from the
    taker is abandoned on purpose.
- **Saves.**
  - Task-sized history and result writes check the same custody rule as full
    snapshots; requester-side delegation records remain the requester's to
    update.
  - A mutation whose save releases its task reports that it did not commit.
    State changes then skip terminal callbacks and projection records, so an
    old holder never recreates records or reports an ending for released work.
  - The save checks this after its own-row check and before the deferred
    message's exact-message check, so a refused save consumes no deferred
    message; the save that follows, without the taken task, does.
  - A released turn's commands are stopped with the same reason as a whole-row
    takeover (`CUSTODY_LOST_REASON`).
- **Replacements and startup.**
  - A replacement taking over a gone worker's saved work leaves out tasks
    another agent holds or any agent finished (the gone worker itself too, if
    it stopped between archiving a task and saving), together with that work's
    records of the tasks it delegated, whose workers are told to stop
    (`orphaned_subtasks.stop_left_out_delegations`).
  - If custody changes while a starting agent adopts saved work, it reads the
    saved work again once. A second conflict while taking over gone agents'
    work starts the agent without that work (`event=role_takeover_lost_race`,
    logged as an error): it stays saved with its holder, and the lead's
    replacement pass adopts it if that holder is gone. The agent does not stop,
    because a startup agent that stopped here would stop the whole runtime.
  - A second conflict on the agent's own saved work stops the start
    (`RuntimeRecoveryStartFailed`), because starting without it could leave it
    unrun or let the agent's first save replace it.
  - A copy dropped while its holder looked alive relies on such a replacement
    if the holder stops later, so it is not run when
    `agent.same_role_runtime_takeover_enabled` is off.
- **Generated task ids.** Every delegation gets a generated task id, never one
  the model chose, so a new task can never look like a copy of an earlier one.

#### Pool size and scaling

- **Worker pool controller** (`custom_agents/worker_pool_controller.py`,
  trusted code in the team host loop). For each worker type it reads waiting
  tasks, idle workers asking and the oldest wait from NATS, plus running,
  starting and draining workers. A type with tasks and no worker gets one at
  once; more start when tasks waited `agent.worker_pools.scale_up_wait_seconds`
  with no free worker, up to the type's pool size and the team limit. Workers
  above a lowered size leave once they hold no work; idle workers leave after
  their idle time.
- **Pool sizes.** `agent.worker_pools.default_max_workers_per_type` (4) applies
  until TeamArchitect sets another size, lasting or until a time, with
  `set_worker_pool_limit`. It can never exceed the operator ceilings
  `max_workers_per_type_ceiling` (16) and `max_total_workers_per_team` (32).
  Administrators get an Inbox notice of every change. One size per type covers
  delegated and scheduled work, since a worker does both.
- **Reviews** (`custom_agents/worker_pool_reviews.py`). TeamArchitect gets a
  `worker_pool_review_...` task (working for the maintenance owner, an
  administrator) when a pool or the team reaches `capacity_warning_ratio`
  (95%) of its limit while tasks wait, when tasks of a type keep hitting model
  provider limits, or when a worker cannot be started. It chooses a larger pool
  for a while, a larger pool for good, no change, or fewer workers. Trusted
  code also sends a problem that keeps coming back (`recurring_incident_count`
  times in `recurring_incident_window_hours`) to administrators' Inbox.
- **Provider limits** (`custom_agents/provider_limits.py`). A worker whose
  model provider keeps refusing calls on a delegated task does not ask the
  person: it pauses the task, retries after a growing wait and takes no new
  work meanwhile, and no workers of its type are added. After
  `provider_limit.max_retries` the task fails with a typed `TaskFailureV1`
  (`provider_limited`), which wakes the requester.
- **Lost workers.** A task whose worker died is taken over by a replacement, or
  handed back to the queue after a lost lease. Each move to another worker is
  counted from claims; after `max_retries_after_worker_loss` (3) moves the task
  fails as `worker_lost`.

#### Waking the requester and routing

- **When the requester wakes** (`custom_agents/delegation_wake.py`). Its model
  runs again when every delegated task ended, at once when one failed with a
  typed reason, or when a `check_back_after_minutes` set on the delegation
  passed. An early wake-up holds the agent's execution slot and moves the
  waiting task to running with a compare-and-set, so it never runs a second
  turn for a task that a delegated result already resumed or finished
  meanwhile. While it waits, trusted code sends the person counts ("9 of 24
  done, 4 running, 11 waiting for a free worker") at most every
  `progress_notices.interval_minutes`.
- **One named worker.** A delegation that also names a worker
  (`target_agent_name`) goes to that worker's own mailbox
  (`<prefix>.agent.<id>.work`, the id it answers a live status check with),
  never to the shared queue; the type only checks that the worker is of that
  type. Only that worker can take it, so it is checked and timed out while it
  waits.
- **New types.** The team's worker types are read from the current definition
  catalog on every use (`LocalAgentTeamRuntime.agent_types`), so a specialist
  type TeamArchitect creates is listed in its routing context, accepted for
  delegation and scaled by the controller at once, with no restart.

#### Inspecting and controlling work

- TeamArchitect sees all of this with `inspect_worker_pools` and
  `inspect_delegated_work` and stops a delegated task with
  `cancel_delegated_task` (tool `fn:worker_pools`).
- Agents can inspect their current user's unfinished queue with
  `list_task_queue` and manage another task through `control_task_queue`.
  Cancellation retains history and stops attached work; resume refuses
  unresolved approvals, questions, delegation and runtime review. Pause notices
  identify the task and affected worker.
- Settings also pages finished task results across agents, including stopped
  workers, ten records at a time. Runtime Controls sorts schedules by next fire
  time, with undated schedules last.
- Self-status, own tool-result inspection and current-task history notes are
  part of every agent's standard access. Artifact inspection checks agent,
  team and task ownership; older manifests without ownership metadata are
  rejected.

### Kinds of work

Universal Agents keeps different kinds of work separate:

- A `Task` is one executable unit of agent work with a terminal lifecycle.
- A `Plan` is manager-owned orchestration state for one strategy or attempt.
- An `Objective` is a durable user-visible outcome that can span several Tasks
  and Plans. It belongs to the person its creating task worked for, or to the
  team if that task worked for no person.
- A manager schedule or `ScheduledMessage` creates agent work later, which a
  model interprets when it runs.
- An execution job runs one immutable Python or Rust revision on a one-shot,
  interval, or cron schedule; no model reinterprets the code at run time.

All of a team's Objectives sit in the team lead's one durable scope, so its
Objective tools check each one (`access_control/ownership.py`,
`objective_access`):

- A task reaches its person's and the team's Objectives; only work for no
  person or for an administrator changes the team's.
- A task names another task as a source of work, or reads its execution digest,
  only when both work for the same person (`named_task_access`). Reading the
  current task's own digest needs no signed identity; using it as a source of
  new work still requires verification.
- Creation checks the source's existing Objective and Plan, and refuses to
  reuse a Plan an Objective already owns, even for an administrator.
  `spawn_task` creates separate work with its own Plan.
- Applying a work-profile proposal checks current authority again and grants no
  lasting authority.
- Missing and inaccessible Objective and Plan ids return the same tool result.
- Linked heartbeat items follow the Objective's reading and changing rules,
  also in edits that supply no replacement Objective id. A shared heartbeat
  launcher keeps its own person's identity and delivery context, and before
  changing conversation or run state refuses an item whose Objective that
  person cannot change. This keeps private Objectives and Plans out of another
  person's work without a second signing path.
- A schedule payload uses only its authorized Objective link; rejecting an
  inherited link also clears it from the copied task metadata.

### Scheduled workers

For recurring specialist work, the TeamLead can authorize automatic worker
startup within current operator limits
([Scheduled workers](user_docs_source/scheduled-workers.md)). A deterministic
controller starts eligible workers, which claim schedules from the shared
database; durable occurrence receipts join task admission in one transaction.
Readiness checks the worker type's tools. Access is the worker type's own,
given ahead of time with `authorize_scheduled_job_access`, and every call of a
run is decided for the worker type and the owner.

A saved worker policy is compared only with what a team-written agent definition
declares (`custom_agents.schedule_definition_identity`): code releases never
pause a schedule, but an edited team definition pauses it until the owner
reviews the differences and TeamLead re-saves. A schedule's saved fingerprint
(`custom_agents.schedule_execution_identity`) covers its task, owner, target and
delivery, but not the run notes that runs write. Changes made with the
authority a re-save needs (the owner's own edit, an approved change, the
gateway moving a maintenance schedule to the person's current chat) move the
saved policy along (`follow_authorized_change`).

### Local scheduled tasks

Local scheduled tasks use database claims and durable run records. A separate
runtime loop starts needed local workers without a TeamArchitect model turn.
The default startup policy covers the specialist types that ship with
Universal Agents; tool and credential access remain separate. Memory-review
timers target MemReviewAgent after the explicit one-time upgrade described in
[user documentation](user_docs_source/scheduled-workers.md).

### Cron time zones

A cron schedule carries its own `cron_timezone`, and its next run is computed in
that zone (`custom_agents.schedule_store.compute_next_fire_at`); a cron schedule
without one is refused. When a schedule is added, the store fills in the
owner's request time zone from the schedule's metadata (the gateway sets it from
the person's profile), else the instance time zone (`user.timezone`). The
owner's zone is the default because the team's people may live in different
zones, and "every morning at 9" means their morning.

The zone counts as control state, so changing a system-managed schedule's zone
needs the same permit as changing its cron; callers that bind a permit before
saving set it themselves (`with_cron_start`). Each scheduler start fills any
zone still missing (`fill_missing_cron_timezones`, logged per schedule). The
launcher sets `TZ` from `user.timezone` for itself and every process, so log
times are local; an ambient `TZ` wins.

### Schedule writes

A schedule change never overwrites a newer one (`custom_agents.schedule_store`),
so a save cannot bring back a deleted schedule, undo a pause, or make a run
happen twice. Every write that depends on a schedule's current values reads,
decides and writes in one write transaction (`ScheduleRepository.write_locked`:
SQLite's write lock, or PostgreSQL's schedule-id and row locks).

- **Edits.** An edit made to a copy read earlier is refused when the schedule is
  gone or someone changed it meanwhile. The comparison includes a one-time
  reminder's due time and the run-note policy, so stale reschedules and
  notification edits are refused too. An edit keeps the next run time, count
  and notes unless it sets them. Explicitly moving a repeating run requires its
  next time to be unchanged since the read; other edits preserve automatic
  advances.
- **Runs.** A run changes only its notes. The advance after a run applies only
  while its agent holds the claim and the claimed run time still stands. A
  one-time schedule re-timed during its run stays at the new time; a counted
  series counts the run and finishes after its last one.
- **Claims.** Turning on and off, pausing and resuming keep the claim; whoever
  starts or signs a claimed run rechecks that the schedule is on, not paused
  and not ended. Before admission a worker releases a claim whose
  occurrence was re-timed, and the admission transaction refuses a future due
  time using the database clock.
- **Listing digest.** The Schedules page and the chat and CLI schedule commands
  bind their controls to one digest of the schedule as listed
  (`schedule_listing_digest`). It leaves out what runs change by themselves (a
  repeating schedule's next run time and the run notes' content), so a
  schedule that runs often can still be paused.
- **Rollback.** Objective schedule rollback compares against the state that
  installation wrote, so it cannot undo a later stop or edit. Reconfiguration
  and rollback both preserve notes written by running work.
- **Clocks.** Time checks keep subsecond precision: SQLite uses its process's
  host clock, and PostgreSQL reads the server's wall clock after any lock wait.

### Stuck schedules and tasks

Trusted code, not a model, reports stuck work: a self-addressed TeamLead review
task carries no person and no chat route, so its findings reach nobody.

**Stuck schedules** (`custom_agents.stuck_work`,
`custom_agents.stuck_schedules`). On every pass the team lead's scheduler loop
finds overdue schedules that nobody picked up. After
`agent.schedule_workers.review_interval_seconds` it tells the owner (Inbox
notice and one chat message) and, when only they can fix it, administrators.
Recovery is reported the same way, and each incident once
(`storage/stuck_work_repository.py`). The Schedules card shows **Not running**
with the same explanation. A missed one-time run is turned off and its owner
told.

**Stuck tasks** are reported the same way (`custom_agents.stuck_tasks`,
thresholds in `agent.schedule_maintenance.stuck_tasks`). The team lead reads
every agent's saved work in its team (read-only) and reports one incident per
request and condition: the agent holding the work stopped reporting in, a
handed-back task nobody picked up, an access decision or a question to the
person still waiting (reminders after a day), or no progress while in progress.
The person comes from the root task's signed context (read, never used to
authorize); without one only administrators are told, and their notices never
quote the request. Signed work for no person is labelled as system work or
maintenance. A cleared warning reports that the condition ended, without
claiming new task progress. The same pass frees a work slot held by a task
whose turn is gone (`turn_failure_runtime.release_orphaned_slot_when_idle`)
instead of waiting for new work to arrive.

**Scheduler pauses.** The task monitor observes its own scheduler cadence
(`custom_agents.liveness_observation`). After a pause longer than the normal
60-second scheduler wait plus one 20-second heartbeat interval, it gives workers
60 active seconds to refresh their heartbeats. During that window,
heartbeat-only takeover waits and task incidents remain unchanged; launch
records, process evidence and explicit exits still decide. Host sleep cannot
make a live startup agent look dead in any case, since it is judged by its
process (see "When an agent is gone").

### Repairs

Four repairs keep work from getting stuck in the first place:

- **Takeover.** The team lead starts a replacement worker for a dead agent that
  still holds unfinished work (`custom_agents.orphaned_work_takeover`); the
  replacement's startup takeover adopts it in one transaction.
- **Waiting work.** A worker stays up while it holds any unfinished task,
  waiting ones included (`strands_team.worker_holds_work`); replies to a
  waiting task are addressed to it.
- **Access decisions.** An access decision follows its task: any agent of the
  request's type whose task waits on it accepts the notice, and each agent
  periodically resumes tasks whose requests were decided while the notice went
  to a previous agent (`access_request_runtime.resume_decided_access_waits`).
- **Orphaned subtasks.** A task that ends with delegated work still running
  stops it (`custom_agents.orphaned_subtasks`), and a worker still reporting on
  work whose request ended is told to stop.

### When an agent is gone

One rule (`custom_agents.agent_liveness`) decides whether an agent is gone, for
takeover, replacements, schedule coalescing and stuck-task notices alike:

- A worker started inside another process is gone when its start record ended
  or its process is proven gone.
- A startup agent or the team lead reports its own process in its status
  (`AgentStatus.process_identity`) and is gone only when that process is proven
  gone. While it runs it keeps its work, however old its heartbeat.
- An agent that reports no process, or whose process cannot be checked from
  here (logged as `owner_liveness_unknown`), is judged by its heartbeat, after
  `agent.schedule_maintenance.stuck_tasks.owner_gone_after_seconds`.

Whether a worker start's process is gone is proven, never assumed from a timeout
(`runtime_hosting.process_run`). The supervisor holds an exclusive lock on
`runtime/process_run.lock` and passes the locked file to every agent process, so
the lock lasts while any process of that run lives. A new supervisor starts only
once it gets the lock, and gives the run a new id that every start records. A
start from an earlier run is therefore settled at once after a restart or a
replaced container, and new runs never attach to a start whose process cannot be
checked. A recreated container therefore cannot leave a start "unknown" for
good and block every later run of its worker type.

The proof assumes every agent process of a team is a child of one supervisor run
on one host. A process from another run counts as gone whatever host it names,
so agent processes of a second runtime sharing the database are not supported.

Each agent writes only its own status row as it stops, marked exiting from the
moment its exit begins. The team lead stores other agents' presence only when it
is newer, never later than its own clock, and removes rows of gone agents on its
own timer, so a manager-only restart does not make running startup agents look
gone.

### Scheduler loop and lease loss

Each agent's scheduler loop renews its launch lease, refreshes its status,
recovers stalled subtasks and fires due schedules in repeated passes. A failed
pass does not stop the loop: it is logged as `scheduler_pass_failed` with a
traceback and retried after a delay that doubles from one second up to 60
seconds.

A stale launch lease (`scheduler_launch_lease_lost`) is different: another owner
holds that launch, so the loop stops claiming scheduled work and the worker
winds down (`custom_agents/lease_loss_wind_down.py`):

1. It leaves the shared queue for its agent type, so no new delegated work
   arrives.
2. It returns each task it has not started, and the task it is executing, to the
   TeamLead as a `WorkerTaskHandbackV1` message, one task per message with a
   deterministic handback id. New requests that still reach it go the same way.
   - The running turn is interrupted without a cancellation record, so the task
     can run elsewhere. Its running shell and Python commands end before the
     handback is sent, so the rerun never overlaps them.
   - A running task whose final answer was already sent is completed by this
     worker instead; handing it back would make another worker run it again
     and send a second answer. "Sent" looks at the task's latest task
     response only: the task is in FINALIZING_PLAN (entered only before a real
     final answer) and the outbox shows that response dispatched or completed,
     or it reached the post-publish step.
   - The outbox gives an answer it held back (final review, pending delegated
     work, a manager escalation) its own status, `held_back`, so such a task is
     handed back and the next worker is not told the answer was sent.
3. It keeps tasks that already started and now wait on a person or subtask,
   because replies are addressed to this worker's id. They may finish until
   `agent.schedule_workers.lease_loss_wait_timeout_seconds` (default 30 minutes)
   after the lease was lost. Then they fail with an explanation to the
   requester, their pending subtasks are asked to stop, and an access request
   they wait on is withdrawn, so no reviewer is left with an open item.
4. It exits once it holds no unfinished task. Until then it keeps publishing
   presence, so a starting worker of the same type does not adopt its tasks.

### Worker handbacks

The TeamLead accepts a handback only when the sender owns the stated launch, and
saves it (`storage/worker_handback_repository.py`) before acting on it. Its
runtime (`custom_agents/worker_handback_runtime.py`, not the model) then sends
the task out again, unchanged, on the shared queue for that agent type, keeping
the original reply route so the result reaches the original requester.

- **Not resent.** A cancelled task, or a lead-delegated subtask that already
  finished, is not sent out again.
- **Skipped scheduled runs.** An interrupted scheduled run is not rerun, and its
  occurrence receipt is closed as `superseded`, when the schedule already ran
  again as its own task, is paused or disabled, or has its next run due within
  `interrupted_run_skip_window_seconds` (default 15 minutes). A one-time
  schedule has no next run, so its run is always rerun.
- **Queueing.** The task waits in the queue until a free worker takes it; the
  worker pool controller starts a worker if none is free. A winding-down worker
  stops asking for work, so it never takes it.
- **Redispatch notice.** An interrupted task carries a typed
  `redispatch_notice` in `MessageMeta`. The accepting worker turns it into a
  one-time instruction for the task's first turn: which actions the
  interrupted run already sent, and to check them before repeating anything.
- **Lead-delegated subtasks.** The subtask's record returns to "waiting in the
  queue" before the task is sent again, so it is not probed until a worker
  takes it. A claim recorded since (the new worker's, or one seen by a retried
  handback) is never undone.
- **Occurrence receipts.** The worker that accepts a rerun takes over its
  occurrence receipt, fenced on the previous owner, so the schedule execution
  scope and completion follow the run. If the schedule ran again while the
  rerun was in flight, the worker closes the run as superseded instead of
  running it.
- **Retries.** Processing runs on receipt and on every lead scheduler pass, so a
  publish failure or a lead restart only delays the re-dispatch.
- **Repeated interruptions.** After
  `agent.schedule_workers.max_consecutive_interruptions` (default 3)
  interruptions in a row, the runtime stops retrying
  (`custom_agents/worker_interruption_escalation.py`). Ordinary work is counted
  per task id and cancelled with the usual reply to its requester (a subtask the
  lead delegated itself fails, so its parent resumes). Scheduled work is counted
  per schedule since its last finished run, and the interrupted run is closed.
  The runtime then admits an `interruption_review_...` task to the lead with the
  evidence of each interruption. The lead investigates, tells the owner, and may
  pause the schedule with `pause_scheduled_job`. Reviews repeat every
  `max_consecutive_interruptions` further interruptions of the same streak.

### Overlapping runs and known limits

Runs of one schedule never overlap across workers. When a schedule comes due
while an earlier run of it is unfinished on this agent, or on another worker
whose presence is still live, the new occurrence is recorded as coalesced into
that run instead of starting a second task. A crashed worker's leftover receipt
does not count, so it cannot block the schedule.

Known limits of the lease-loss handling:

- If a worker exits before the lead re-dispatches its interrupted scheduled run,
  and the schedule fires in that gap, the new fire sees no live owner and runs.
  The rerun notices the newer run when a worker accepts it and closes itself,
  but a rerun accepted before that fire can briefly overlap it.
- Replies addressed to a worker's id after that worker exits (for example a
  subtask result for a task it failed at the wind-down deadline) are not
  delivered anywhere; the requester already received a failed result.
- The handback waits at most 10 seconds for the interrupted turn's tool calls.
  A call the process supervisor cannot end (an HTTP request, OpenShell sandbox
  creation) may still run after the handback; it is logged as
  `event=tool_work_outlived_turn`, and none of the stopped turn's remaining
  commands start. A process that moved itself into a new session or process
  group (`setsid`, daemonizing) is not ended at all, and is logged only while it
  keeps its command's output open (the call then counts as still running).

### Abandoned memory-review claims

Abandoned memory-review claims become inactive review records after a grace
period and checks for live or saved work. InternalImprovementAgent reviews their
metadata in its maintenance runs and uses bounded archive/purge tools, its fixed
duties as a built-in system agent. Memory review jobs are made for one person,
so the review tools show and resolve only jobs made for the person the task
works for. Cleanup keeps an audit history and never treats expiry as proof of
completion or a reason to replay work.

### Storage

SQLite is the default local store; PostgreSQL supports shared deployments.
Repositories keep state that must survive a process restart or stay
inspectable: Tasks, runtime snapshots, schedules, Plans, Objectives,
conversations, memory, authorization records, gateway sessions and execution
attempts. Agents still coordinate through NATS, not by polling each other's
tables. Async task and context operations use serialized storage execution,
with detached records at the caller boundary. SQLite connections close at the
end of their transaction context; PostgreSQL uses native SQL and connection
owners.

Stores whose writes read before they write make writers take turns on both
backends. SQLite starts every write with `BEGIN IMMEDIATE`. PostgreSQL's
default READ COMMITTED isolation lets two transactions read the same rows
before either writes, so the access and knowledge stores take their own
transaction-level advisory lock in `begin_write`
(`storage/postgres_connection.py`), one key per store so unrelated stores never
wait for each other; other stores lock the rows they change (`SELECT ... FOR
UPDATE`). A key covers the whole PostgreSQL database, so instances or teams
sharing one database in different schemas also wait for each other's writes to
that store. While a thread holds an open access-store write transaction (a
grant plan, a whole decision), its reads and writes join it, so no second
connection opens while the lock is held. Moving rows between tables uses
`DELETE ... RETURNING`, so exactly the rows deleted are the rows kept elsewhere
(a removed person's held records, `storage/person_departure_repository.py`).
Without these rules, on PostgreSQL an approved change could be claimed twice,
one of two edits to one person's model settings could be lost, and holding a
person's records could delete a row written meanwhile without holding it.

### Objectives

Every Objective change loads, changes and saves the stored record inside one
operation of the team lead's task operation lane
(`ObjectiveCoordinator.change_objective` in
`custom_agents/task_manager_objectives.py`). The store writes whole records, so
a copy kept from an earlier operation and saved later would erase whatever was
stored in between. A static test keeps every other module from saving
Objective records, and a lane refuses an operation submitted from inside one of
its own operations (`NestedTaskOperation`) instead of waiting for itself. The
host command line cannot change Objectives, because it would write the record
from another process while the team lead runs.

**Failed saves.** A change requested by a tool call or a person's command is
reported as not saved when the write fails (`ObjectiveSaveFailed`), so the
caller never gets back a changed record the store does not hold. Steps nobody
waits on (a task starting or ending, a plan following its task, a work session
recording progress) keep their own records elsewhere, go on without the
Objective's copy and log `event=objective_save_skipped` with the step. A
schedule or heartbeat item whose Objective could not record it stays, and the
reply warns that pausing the Objective will not reach it. The final deletion
status, completion timestamp and summary are saved together, so a failed
completion leaves the Objective deleting and retryable. Nothing writes metadata
after the record becomes deleted, because repeated deletion commands return
immediately for a completed deletion.

**Lifecycle changes.** A lifecycle change
(`custom_agents/objective_lifecycle.py`) is one such operation, for a person's
Runtime controls and for the team lead's `objectives(action="update_status")`.
It saves the status, turns the bounded work policy and the managed schedules
and heartbeat items off (or back on after an earlier control), and records
which links changed.

- Each caller passes its own rules for switching schedules: the person's
  gateway-signed permit, or the agent rule that leaves system-managed schedules
  to `apply_change`. A schedule the agent rule leaves alone, or a failed
  switch, is reported instead of recorded as switched.
- Runtime controls check ownership and the actions the current status offers in
  the same operation as the change; other actions get a plain refusal and no
  new status. A record digest in the request is checked in that operation too.
  The Web page sends the digest of the record it showed, so a change made after
  the person looked is refused. A chat command takes the digest just before it
  sends, which guards only against a change in that moment. A trusted stop (a
  departing person's Objectives) sends none.
- A control whose status is already stored is accepted as a retry: it records
  no new status or policy event and switches only the links that did not
  switch before, so a pause or abandon whose schedule failed can be repeated.
- The team lead cannot set `deleting` or `deleted`; deletion is the person's
  control. A task that ends while its Objective is paused or being deleted
  keeps that status.

### Memory and context

The memory system has the five stores the README lists, which agents recall
from (conversation history, user memory, team memory, the memory graph and the
knowledge base), plus task history. Who owns a record, and whose memory a task
loads, come only from the task's signed context or its scheduled run's
identity; a person id in the message fields alone never decides either.

#### Conversation history

Conversation history stores durable threads and compaction boundaries. It is a
separate record from the gateway presentation transcript, which reproduces what
a user-facing channel was allowed to display; the manager's conversation
history supports reasoning context and task continuity.

A conversation and each of its turns belong to the person the task's signed
context names, or, for a scheduled run, to the schedule's owner. The owner
decides who may read the conversation, and it leaves the team's records with
them. Because the earlier turns go into the task's prompt, a new task
continues a conversation only when it works for its owner, and work for no
person continues only an ownerless one (`access_control/ownership.py`,
`conversation_continuation`). Any other task naming the conversation is refused
and logged (`conversation_owner_mismatch_refused`).

Trusted code never hands work someone else's conversation:

- Runs for no person start their own.
- A team schedule moved from a departed person, and each item of a moved
  heartbeat launcher, continue in a new conversation.
- A gateway refresh gives a system schedule the session conversation of the
  person it names, which also moves a schedule an older release handed over.
- A heartbeat item whose conversation belongs to someone other than its run's
  person gets a new one before the run starts.
- A departing heartbeat launcher's item conversations change before its owner
  does. If any item update fails, the launcher keeps its inactive owner and
  runs stay stopped; the next departure retry resets every item again before
  moving the launcher. This order also protects removal and restore after a
  partial write or restart.

Work without a verified person stays ownerless through restarts. The
single-user owner assignment at gateway startup changes only gateway rows, and
the manager never gives its ownerless conversations, archived tasks or
schedules to anyone, because a missing owner means work for no person.
Schedule loading also treats an empty database owner as authoritative: it
clears stale personal identity, chat routing and conversation references from
executable metadata without assigning an owner or rewriting the row.

#### Task history

Task history and execution digests store progress, delegation, failures and
recovery decisions. A task record belongs to the person its task verifiably
works for (`owner_person_id` in `custom_agents/task_records.py`), decided once
at admission from its signed context or schedule run identity and kept in the
runtime snapshot; work for no person has no owner. Subtasks and the plan-step,
recovery and Objective tasks the manager makes from a task inherit its owner.
The archived task row, a person's task list and task controls, Objectives,
solution variations and a person's departure all read this owner. Deciding at
admission keeps the owner after the signed context expires (after 7 days) or
the runtime restarts, and one recorded owner keeps a task's active and archived
views in agreement.

#### Pinned memory and the memory graph

Pinned team and user memory stores reviewed facts and preferences with
provenance and validity periods. Provenance (source chat, session,
conversation, task, creator and the fact's internal UUID) and the reserved
`metadata.imported_from` record stay in storage. Agent memory tools and memory
reviews get a public view built field by field
(`custom_agents/pinned_memory_runtime.py`, `public_pinned_memory_fact`: text,
validity, tags, metadata, the importer key trusted imports use, timestamps,
revision digest); these views and context hydration keep ordinary skill
metadata, including onboarding progress. The Settings memory controls get the
same view without metadata (`pinned_memory_reply`). A Signal chat id is a phone
number, so it never reaches a model or a reply.

The memory graph stores episodes, entities, claims, evidence, handoffs, and
semantic retrieval data.

#### Knowledge base

The knowledge base (`knowledge_base/`, `storage/knowledge_repository.py`) holds
pages agents and people write together (how-tos, decisions, lessons and
references) in the team space or one person's own space, with every revision,
notes and `[[links]]`, searched with each database's full-text index. Agents
use it through `fn:knowledge` (`custom_tools/providers/knowledge.py`); people
use the Web Chat Knowledge page (see "Knowledge page" below). The
`knowledge_recall` memory step adds matching page titles to the team lead's
turns.

Three rules are trusted code, not prompts: an agent working for a person only
proposes team changes, which stay private to that person until reviewed
(pending contributions); credentials and identity numbers are never stored;
and contact details or text from the person's own memories are flagged for
review (`knowledge_base/sensitive_content.py`). The service scans page names
and tags before and after normalization, plus page text, change descriptions
and decision reasons, so metadata cannot bypass these rules. Published pages
take their name from the reviewed title unless the reviewer names the page
explicitly; a slash in a title is ordinary text.

**Write limits.** The store enforces per-agent write limits inside its write
transaction (`agent.knowledge.limits`), so no agent dominates the knowledge
base or rewrites a page in a loop. Direct and proposed archives share one
hourly budget. Revision fingerprints include the page type, so agents can
correct classification without changing the text.

**Curator.** KnowledgeCuratorAgent, a built-in system agent started on demand,
reviews each person's pending contributions in a run signed for that person and
tidies team pages in a run for no person (system schedule kinds
`knowledge_curation_person` and `knowledge_curation_team`;
`custom_agents/knowledge_curation_runtime.py` skips a run with nothing to
review). Its tools (`fn:knowledge_curation`) are in the `knowledge_review` tool
set, which the curator and people hold, never the team lead. Exact duplicate
active team pages, found from the existing content fingerprints, trigger
cleanup even when nothing else is wrong. Frozen notes stay open until a later
run can merge them after the freeze expires, or until the curator lifts the
freeze (`unfreeze_knowledge_page`; a freeze always lasts at least a minute). In
agents' personal work, direct team reverts and merges of team pages are
refused; team revert history records only the restored revision.

**Merging pages.** `KnowledgeService.merge` writes the merged text as the
target's next revision and archives the source in one transaction. The
source's name becomes a redirect (`knowledge_redirects`), so reading, noting,
editing and `[[links]]` by the old name reach the target, and a new page never
takes that name. Both histories get fixed text from trusted code, open notes
move to the target, and pending contributions are pointed at it against its
pre-merge revision, so their reviewer merges them again. Note and contribution
insertion rechecks redirects under the same transaction lock, including writes
prepared before the merge. Backlinks return each referring page once before
applying the result limit. The team transfer `knowledge` section carries
redirects from version 2.

**Notices.** The team run's preparation sends administrators one Inbox notice a
day (kind `knowledge_needs_attention`) when team pages froze in the last day or
an agent type had many refused writes (`NOTICE_REFUSED_WRITES`), with page
names and counts only, even when no model run is needed. Freeze notices include
freezes that have already expired or been lifted, and describe the current
state.

**Lessons from reviews.** An after-action report review can save its lesson as
a page (`saved_to_knowledge`, `custom_agents/after_action_report_knowledge.py`):
a review of a person's work proposes it for the curator, and a review of work
for nobody publishes it. The knowledge transaction also stores a receipt keyed
by the review, its verified person and an exact digest of the lesson fields. If
review resolution fails, a retry after restart reuses that receipt with no
further contribution, revision or budget charge; changed lesson fields are
refused. Receipts hold references, not lesson text, and are deleted with the
person's reviews on departure. They are runtime control state and are not
transferred, because pending reviews are not transferred.

**Live privacy tests.** Live privacy scenarios
(`tests/test_live_knowledge_privacy_scenarios.py`, opt-in with
`RUN_LIVE_KNOWLEDGE_PRIVACY=1`) run real agents and the curator with the
configured model and check that a person's planted private details never reach
published team pages or another person's model prompt, answers or tool calls.
They require completed reviews and successful scenario tool calls, so an idle
model or unfinished review cannot count as a privacy pass. Their model-free
check of published team content is `tests/knowledge_privacy_checks.py`. The
rationale and the storage benchmark are in the architecture repository's memory
and context architecture.

#### Context assembly

Before a model call, the runtime assembles context from the current request,
system and role instructions, task state, conversation history, relevant
memory, routing context, and bounded artifact references. A provider buffer may
retain inner-loop state, but durable continuity never depends on an opaque
provider session.

Personal memory (a person's facts and hints to their earlier threads) is added
only for the person the task's signed context or scheduled-run identity names,
and work for no person gets none. The turn's memory review job is made for that
same person. Before copying conversation turns into a review job, the runtime
checks their stored conversation owner through `access_control.ownership`; a
conversation owned by someone else or with no recorded owner is refused and its
turns stay unqueued. The working folder the prompt names is that person's
folder too, the same one the file tools use. Plan-step and recovery prompts
hide gateway details, but their working-folder selection verifies the original
task metadata separately from the sanitized prompt metadata.

### Recovery and runtime state

Runtime snapshots and idempotent admission let processes recover queued work,
waiting subtasks, deferred messages and recent in-flight Tasks. Outboxes record
ordered actions before external effects. Execution attempts and worker starts
use leases and fencing, so a stale holder cannot commit after another takes
over. A stopped or handed-back turn ends its running commands before the task
is archived or handed back. Repairs and fallback paths are logged, not hidden.

Saved runtime snapshots and deferred messages move only from an agent that
counts as gone, in one transaction that deletes its row. A previous owner that
is still running finds its row gone at its next save, which is refused
(`RuntimeCustodyLost`) before anything is written; it then drops that work from
memory without replying and keeps running (`custom_agents.custody_loss`).

How a deferred message leaves its row:

- A deferred request leaves atomically with admission of its own task.
- The reply choosing a delayed provider retry leaves atomically with insertion
  of its retry schedule. Both check ownership inside the effect's transaction
  with `storage.runtime_custody`. Restart keeps a delayed-retry task paused
  until its timer fires.
- Other messages, including clarification replies, stay until their handler
  completes; saving query history alone does not apply a resume or cancel
  decision.

Every task snapshot or history write by the dispatching asyncio task checks the
exact message in the locked deferred row
(`deferred_message_runtime.DeferredHandoff`), so a recreated row holding newer
messages cannot authorize an old dispatch, and that dispatch cannot clear the
new inbox when it finishes. Child tasks and other threads never inherit this
handoff. A crash therefore keeps an unadmitted request or unfinished reply in
the deferred row; an admitted request is held by its task snapshot.
Intermediate reply handling can be replayed after restart.

Not covered: work a previous owner does before its first save after the
takeover (a model turn in progress until it is stopped), and handling that
saves nothing (such as a reply about a task it no longer holds), may still
happen on both agents.

### State ownership and configuration

Task-scoped records (resume notes, pause records, tool-call overrides,
blockers, Plan and Objective links, outbox and mailbox records) have one owner:
`TASK_SCOPED_FIELDS` in `custom_agents/task_runtime_state.py` names the tasks
each entry belongs to. Removing a task forgets its entries through that table,
also when a task leaves without ending (a lease-loss handback). Restore keeps a
saved entry by the same rule, taking a task's entries only from the snapshot
whose task record it kept, and drops leftovers in older snapshots with one
`event=runtime_snapshot_orphans_dropped` warning per snapshot. This keeps
finished tasks' records out of later snapshots and keeps a takeover from
attaching one snapshot's leftover note to the same task restored from another.
The Objective review after a task ends gets the Objective id read before the
task is archived, never by searching Objective records for the task id.

Transport contracts, workflow records and snapshot codecs have separate owners.
Shared subsystem configuration lives in `runtime_settings/`; the gateway
composes those settings. The topology owner establishes JetStream streams
before consumers validate them.

Operator tool allow and block lists name exact built-in functions from the
release catalog; MCP tools follow their separate registration path and cannot
be named in them. Both configuration formats validate all such lists at file
load, before override resolution, including unused agent types and defaults
that an override replaces, so configuration errors do not become delayed
worker startup failures. The
generic loader selects only the list fields and reuses the shared setting
models, keeping full paths in errors without changing configuration values or
presence.

### Execution slot and deferred messages

Each agent owns one model execution slot across turn preparation, invocation
and completion. Parent recovery waits for that owner, queued tasks cannot claim
the slot during an active turn, and control and status messages stay
responsive.

Because a model call holds the slot, every model call is time-bounded:
`build_strands_model` wraps each provider stream with
`custom_agents.model_stream_watchdog`, which ends a call that sends no output
for `llm.stream_idle_timeout_seconds` or runs past
`llm.model_call_timeout_seconds`. The HTTP client timeout alone is not enough,
since proxy keep-alive pings keep an idle stream's bytes flowing. A stopped
call is a provider timeout: failover, or a failed task with a plain timeout
message.

A task claims the task manager's single execution slot by moving to
IN_PROGRESS before its turn, so every path that runs a turn (the queue, a turn
resumed after a sign-in or access decision, a resume with a directive) ends a
failed turn through `custom_agents.turn_failure_runtime`. As a safety net, the
queue logs `event=execution_slot_orphaned` and ends any IN_PROGRESS task that
still holds the slot when no turn is running. RETRYING keeps the slot on
purpose while the turn-action outbox waits to retry.

Busy or paused runtimes admit messages up to `agent.max_deferred_messages` (100
by default); excess messages stay with the broker for redelivery. The active
task snapshot layout has changed without a migration. Finish or explicitly
abandon in-flight work before upgrading; see the [configuration operating notes](user_docs_source/configuration.md#active-runtime-state-and-admission).

### Queue draining, shutdown and next-turn notes

Each agent runs its queued work in one retained drain
(`custom_agents/pending_work_runtime.py`). Every wake-up (a delivery, a fired
schedule, a decided access request, work recovered at startup) goes through it,
so neither a message handler nor the scheduler loop waits for a model turn. A
wake-up that arrives during a pass, or while another holder has the execution
slot, is kept and answered with another pass. A failed pass is retried after
5 s, doubling to at most 5 minutes.

Shutdown stops new work, then cancels and waits for the drain, its timers and
short sends (sync tools' sends included) before the transport closes, so
background work cannot write task state or publish after it. The final status
write still marks this agent exiting after delivery callbacks have settled. A
turn in progress at shutdown is cancelled; restart recovery queues its task
again if the agent restarts within `restart_executing_task_grace_seconds`
(900 s by default), and otherwise fails the task and tells the requester.

When a turn starts, it reads the note left for its task's next turn (an access
decision, a delegation decision, a restart notice) and the requester's partial
tool-call limit. They are released when the turn's decision is recorded, in the
same save as its outbox batch (`TurnStartNotes` in
`custom_agents/task_runtime_state.py`); a result saved outside the outbox (a
budget pause, a work-session yield) releases them when the turn returns. A turn
that pauses for an approval keeps them until the resumed turn's decision is
recorded, and a cancel, failover or failure before the decision is recorded
keeps them for the task's next turn. A note removed at turn start would be lost
when shutdown cancels the turn, and a note kept after its decision makes a later
turn act on it again; a repeated delegation is not deduplicated across turns.
Details are in the runtime execution architecture document of the
ua-architecture repository ("Pending work and shutdown", "Context assembly").

## Authorization and credentials

Every trust boundary keeps its own deterministic checks: gateway
authentication, owner and team scope, repository constraints, path and shell
policy, transport identity, broker policy and workload validation. Tool
visibility, model prose, task assignment and self-reported roles grant no
authority.

### Files and folders

**Instance root.** All instance files sit under one root, split by who may
touch them (`runtime_settings/instance_layout.py`): `control/` (team control
files and skills), `dbs/` (databases), `state/` (runtime records) and
`workspaces/`, the only area agents work in (`users/<person>`, `team`,
`agents`). Only the root is configurable; every other path is derived, so no
setting can put an agent-reachable folder over control or state files. Startup
refuses the old single-`workspace/` layout and prints the move commands instead
of moving files.

**Folders per task.** One process serves many people, so each tool call takes
its folders from the person its task works for
(`custom_tools/workspace_scope.py`): that person's folder plus `team/` and
`agents/`, or only the shared folders for work with no person. File tools,
`notify_user` attachments, research files, Python environments and the code
sandbox all use this one resolver. The gateway's workspace browser
(`assistant_gateway/application/workspace_browser.py`) opens for every
signed-in person with roles from the real session: members see only their own
folder, administrators also the shared folders, nobody another person's folder.

**Data folders.** Folders outside the instance reach agents only as data
folders that administrators register in the access store
(`access_control/data_folders.py`, decision D16: `/access folders`,
`assistant_cli access folders`, the Access page, or an approved proposal).
Processes read the current list at each use, so registering needs no restart. A
data folder may not hold or sit inside the instance, its configuration or the
broker's credentials, overlap another data folder, or look like a credential
store; this is checked at registration and at every use.
`tool_policy.data_folders` is removed and refused by name: configuration is
read only at start, while the access store lets administrators change the list
while agents run.

**Control files.** Agents read and change control files only through typed
services (settings, roster, provisioning, reviewed skill installation). Team
skills are the one readable exception, since agents must read a skill's files
and run its scripts; they are never writable, and new or changed skills go
through reviewed installation.

#### Research files

Web research tools save under `research/<task folder>/<result folder>/` in the
task's workspace. A task folder is a readable prefix plus a digest of the full
task id; hashing every id stops a short, unchanged id from matching another
task's folder name. Each call adds a random suffix to its result folder, so
repeated or parallel searches keep separate files. New writes use these names;
saved paths stay readable because inspection checks the person's research root
instead of rebuilding names.

#### Links in agent-writable folders

Agent code can create links in folders it writes and runs alongside trusted
code, so a tool that checks a path and then opens it by name can be redirected.
(A planted `research` link once let `web_artifact_inspect` read control files
and sent research writes into `control/`.)

Every agent file open therefore goes through `custom_tools/file_access.py`. It
makes the path absolute against the task's default folder, resolves links once
to judge the real target (`custom_tools/path_policy.py`: the task's folders,
data folders, what another person shared with the task's person and the
read-only skill folders; never control files, runtime records, another person's
unshared folder or a sensitive name), asks the access service for a folder
decision when authorization is on, and opens the real location through
`access_control/safe_open.py`. `safe_open` walks from a trusted root one
component at a time with `O_NOFOLLOW`, accepts only regular files with one
link, creates with `O_EXCL` and replaces in place through the checked
descriptor, so a link planted before or swapped in after the check makes the
open fail. The broker's staged CLI files (`secure_tool_broker/task_files.py`)
and the workspace browser use `safe_open` too.

Sensitive names (env files such as `team.env`, SSH private keys, `.pem` files,
credential folders such as `.ssh`) come from one list,
`access_control/sensitive_paths.py`, used by the file tools, the shell tool's
path arguments, attachments in both directions and the workspace browser, so no
check lets through a name another refuses.

#### Attachments sent to people

A `notify_user` attachment may come from the person's folder, `team/`,
`agents/`, a data folder or something shared with them, if the agent-side
decision allowed reading it. The gateway checks it again for the person, copies
it once without following links into that person's own folder
(`users/<person>/scratch/sent/<id>/`, or `agents/scratch/sent/` for work for no
person) and records the copy's SHA-256 (`assistant_gateway/attachments.py`).
Files a person receives stay with their files. Agent code can write that
folder, so every reader (chat channels, the external adapter host, the Web Chat
download) uses `StagedAttachmentReader`: it opens the copy with `safe_open`,
requires a one-link regular file of bounded size and checks the digest. A link
or changed bytes swapped in after staging are refused (Web Chat answers "no
longer available"), so the delivered file is the checked one. The Photon
sidecar reads a path, so it gets a private 0600 copy under `state/`, removed
after the call.

#### Published pages

Published pages (how-tos, articles, apps from `publish_single_page_site`) are
deliverables, so they go in the person's folder
(`users/<person>/sites/<site_id>/`), not `state/`. The tool also writes a
trusted record, `state/published_sites/<site_id>.json`, with the page's folder,
the person it was made for and each file's SHA-256
(`user_docs/published_sites.py`). The site host finds a page only through its
record and serves it only while the digests match, so agent code cannot change
what the Web Chat origin serves. Anyone with a link can open a page, so site
ids end with a random part. The `/docs/sites/` index lists only the signed-in
person's pages, and a page's chat works only in its owner's signed-in Web Chat
session, sending each message as that person.

### Scratch files and maintenance runs

Agents keep temporary files in their task folder's `scratch/`. When the
internal-improvement maintenance schedule fires, trusted manager code deletes
files older than `tool_policy.scratch_retention_days` (default 14, 0 turns it
off) from every person's and the agents' `scratch/`, walking folders without
following links, and gives InternalImprovementAgent the counts
(`custom_agents/scratch_retention.py`). Trusted code does this because the
maintenance run is system work for no person and cannot open people's folders.

The run may use the shared `team/` and `agents/` folders, sandboxed code there,
the memory-review cleanup tools and model lookup, and may read and record only
its own schedule (`access_control/ownership.py`,
`system_work_schedule_access`): people's schedules are private, and changing a
system schedule needs a person. So trusted manager code checks every schedule
before each run (`custom_agents/stale_schedules.py`): it deletes the own
schedules of people removed from the team and tells owners and administrators
about unused ones through the Inbox, once per list. The run gets counts only.
So that the run's findings reach a person, the manager signs TeamArchitect's
escalation task, with the scheduler key, for the owner stored on the
maintenance schedule (the first user), ignoring any person named in the
escalation (`custom_agents/task_context_runtime.py`, `sign_system_escalation`).

### Scheduled runs and their owners

Each scheduled run takes its owner's standing and roles at run time from the
gateway's copy of the team's people in the access store
(`access_control/people_directory.py`). The gateway updates the copy whenever a
person is added, changes role, or is disabled, enabled or removed, and rewrites
it in full at each start. The manager signs scheduled work but cannot read the
gateway's records, hence the copy; roles saved on a schedule would go stale (a
demoted administrator's schedules would keep administrator roles, a disabled
person's would keep running). A disabled or removed owner's schedules do not
run. Roles are given only when the owner is vouched for: a system-managed
schedule, or a context the gateway signed for that same person saved with the
schedule.

**Owner refresh.** At each start, and whenever the manager becomes available
again, the gateway sends a `refresh` so each maintenance schedule's owner stays
reachable. The manager replaces only the stored person, chat session and that
session's conversation, under the trusted-runtime permit, and keeps pause,
enabled state, cadence and next fire time
(`custom_agents/schedule_context_refresh.py`). A refresh never creates a
schedule. Memory-review schedules are installed the same way, once (`ensure`)
then refreshed, so a gateway restart does not reset an operator's pause or
cadence. The core-facts audit is one team schedule; the personal-facts audit
and stale-conversation review are one pair per active person, owned by them and
delivered to their own chat
(`assistant_gateway/application/person_maintenance.py`), because a run reviews
only its owner's memory and conversations
(`custom_agents/memory_review_runtime.py`).

**System schedule table.** One table, read by both gateway and manager, says
what the runtime does with each kind of schedule it manages
(`custom_messages/system_schedules.py`): whether the kind belongs to one person
(deleted when they leave) or the team (moved to another administrator), whether
its runs work for no person, which agent type runs it on a worker started on
demand, its delivery mode, and whether it is internal maintenance that never
gets its own conversation. A new kind adds a row, plus a hook in
`custom_agents` if its runs need preparing, instead of checks everywhere. A
kind that works for no person also needs a fire path that calls
`custom_agents.schedule_permits.sign_system_run`; the ordinary path signs for
the owner. The table decides no-person permit issuance and identity
verification, not the signing path.

### Tool catalog

Every callable operation has an access level (`read` < `write` < `full`) in one
tool catalog (`access_catalog/`, definitions in `ua_definitions/access`):
built-in functions, code tools, broker actions, `gh`/`gog` commands, web API
routes, confirmed MCP operations and installed procedures. Unclassified
operations need `full`; startup refuses a build whose tools lack a level.

Related functions on one kind of record are one tool with an `action` argument
(`memory` and `topic_tags`; `mcp_connections`, `hosted_mcp_servers`,
`installed_permissions` and `specialist_definitions` in `fn:capabilities`;
`objectives`, `objective_work_sessions` and `objective_work_profiles` in
`fn:objectives`), so requests carry fewer tool definitions. Each action is its
own operation (`memory.delete`) with its own level; an unlisted action needs
`full`, and the tool checks each action's inputs
(`custom_tools/action_inputs.py`). Grants cover the whole tool at one level,
each call needs its action's catalog level, and a block can still stop one
action.

The broker and runtime share the catalog package, which imports only contracts,
because the broker never imports agent code. The broker checks every `gh`/`gog`
command line with one unambiguous parser and runs exactly the checked
arguments; local files a command names are copied through a private folder and
limited to the requesting person's folders.

### Code sandbox

Outside OpenShell mode, agent-run shell and Python commands run in a Linux
Landlock sandbox. Path checks on command text cannot contain code that builds
paths at run time, so the operating system limits each command.

**Folders.** The launcher is an enforcement point. For each command,
`custom_agents/code_access.py` asks the access service which folders the agent
and its person may use (the task's folders, data folders, what was shared with
the person, folders in the agent's grants for this person's work) and grants
each read-write where both may write, read-only where they may only read, or
not at all. The command also gets read-only skill folders, a private scratch
home and read-only system files. Landlock rules only add access, so a folder
with a blocked folder below it is left out entirely, and the result says so,
rather than exposing the blocked part. Control files, databases, secrets,
instance configuration, other people's folders and other processes stay out of
reach. Writable folders get every right except making device files; links are
allowed (Python environments need them) because trusted code never follows
them.

**Network.** TCP connect and listen are open only when the agent and its person
hold internet access (`fn:internet`, operation `connect`, decision D14). It
sits in the `internet` tool set with the web research tools, and every role
holds that set as standard access, so code reaches the web by default; a block
on the set or on `fn:internet` (for an agent type, a role or the whole team)
closes it. Internet is its own grant, not a side effect of `full` on a code
tool: whether code may run (and write which folders) is a different question
from whether it may talk to the outside world, and tying the network to `full`
would make people grant far more than they mean just to let a package download.
Closing TCP needs Landlock ABI 4 (Linux 6.7); on older kernels a command
without internet access is refused rather than run with the network open.
Landlock does not cover UDP, so internal services such as the local NATS server
still require credentials sandboxed code cannot read.

**Every command needs an agent.** `shell(parallel=true)` runs commands in a
thread pool whose threads do not inherit context variables, so each command
runs in a copy of the calling thread's context; otherwise it would have no
agent. A command with no agent is refused (`event=code_refused_without_agent`),
because nothing would decide what it may reach. Only code that embeds the tools
on purpose (tests, scripts) may run such commands, inside
`custom_agents.code_access.embedded_commands()`, and never in a process the
supervisor started (`UA_PROCESS_RUN_ID` set, `runtime_hosting/process_run.py`),
where a missing agent is always a fault. Agent-run commands never get the
`UA_PROCESS_RUN_*` variables; only the agent process reads them.

#### Running outside the sandbox

Running a command outside the sandbox is its own catalog tool,
`code:unconfined`, so network access never implies host access.
`shell(..., unconfined=true)` and `python_env(..., unconfined=true)` run that
one command unconfined only when the agent and its person hold `full` on it,
after a person approved the agent's `request_access`; each run is logged
(`event=code_unconfined_run`). Deterministic execution jobs on a profile that
runs on the controller host (`local` backend) need the same access, lasting
beyond one task, at activation and before every run
(`runtime_hosting/authorization.py` `require_host_execution`). With
authorization off, the task's folders, data folders and the network are open,
and only the operator's `security_layer.code_execution.sandbox: off` runs code
unconfined.

#### OpenShell mode

OpenShell mode runs code in OpenShell sandboxes instead
(`custom_tools/openshell_sandboxes.py`). OpenShell copies files in at sandbox
creation rather than sharing a folder, so a shared sandbox would hold every
person's folder. Each person therefore gets their own sandbox, created on first
use with copies of only their folder, `team/` and `agents/`; work for no person
uses a shared one. The operator's OpenShell policy decides the network and
anything else there; the folder grants and network rule above do not apply.

### Stopping commands

Commands stop with their task. Strands runs `shell` and `python_env` in a
worker thread, and cancelling a turn does not stop that thread. Without a
separate stop, a command would keep running after a person stops its task,
after a lease-loss handback while another worker reruns the task in the same
folders, and through shutdown.

**Stop signal.** Each turn gets a stop signal owned by Universal Agents
(`custom_tools/tool_stop.py`), passed to Strands as the invocation's
`cancel_signal` and to the tools through `invocation_state`. Strands' own
`agent.cancel_signal` cannot carry it: Strands clears it when the invocation
ends, and a stopped turn ends before a tool thread looks. Every turn also
passes `callback_handler` to Strands as an extra keyword, so Strands hands
tools the turn's `invocation_state` one level down; tools read it through
`custom_tools.runtime_context.caller_invocation_state`, which handles both
shapes.

**Ending processes.** A stop sets the signal before it cancels the turn. The
process supervisor then ends the running command's whole process group
(SIGTERM, then SIGKILL a second later) and starts none of the turn's remaining
commands; a tool call starting after the stop does nothing. A command can leave
processes in its group after it returns (`server &`), so each command's process
group is recorded, even after the command exited (its leader is then a zombie
the supervisor has not collected yet). On a stop, wherever it lands (during or
after the model call, while the turn applies its result or handles an error),
groups that still have processes get the same SIGTERM and SIGKILL
(`event=tool_background_processes_ended`). A turn that ends normally leaves its
background processes running.

- Each check reads `/proc` once for all groups, in a worker thread, so the
  agent's event loop keeps running.
- The stat parser reads bytes: Linux process names need not be UTF-8, and
  truncating one can split a character. An unrelated process name must never
  abort cleanup.
- A group is signalled only while its number still belongs to that command: the
  number cannot be reused while any member lives, and a process holding it with
  a different start time is a reuse and is left alone.
- A tool call keeps the task it started for until it returns
  (`runtime_context.bind_tool_call_task`), so a call that outlives its turn
  never picks up the next task's person and folders.

**Waiting for cleanup.** The cancelled turn waits up to 10 seconds for its
running tool calls, then gives leftover processes one second after SIGTERM and
up to one more second to exit after SIGKILL
(`custom_agents/sdk/strands/turn_tool_stop.py`). So the stop reply, the
cancellation record, the next queued task, the lease-loss handback and shutdown
all come after its commands end. The stop path waits that plus two seconds, 14
seconds in all, within the gateway's 15-second control timeout. The wait is in
the turn, not in each stop path, because the code running the turn archives the
task and frees the execution slot as soon as the turn ends, whoever stopped it.
Each turn keeps one cleanup task, so overlapping stops, a lease-loss handback
and shutdown join the same work without restarting its grace period. Further
cancellation cannot cut cleanup short; the caller propagates it only after
cleanup finishes. Both turn runners also handle this cancellation outside their
error handlers, because writing context or settling a failed resumed turn may
still be in progress at a stop.

**What can outlive a stop.**

- A process still present after SIGKILL is logged with its group and process
  ids (`event=tool_process_groups_outlived_kill`); uninterruptible kernel work
  can delay its exit.
- A tool call still running after the wait is logged
  (`event=tool_work_outlived_turn`) and the stop goes ahead. It can only be
  work the supervisor cannot end: an HTTP request, OpenShell sandbox creation,
  or a process that moved itself into a new session or group and still holds
  its command's output open (one that closed its output is not seen at all).
- In OpenShell mode the supervisor ends the local `openshell sandbox exec`
  client; whether that ends the command inside the sandbox is not yet verified.

### Signed identity and schedule permits

Who a task works for is signed, not claimed. When the gateway admits a request,
it signs a task context (Ed25519) naming the person and their current team
roles. The manager's scheduler signs one for each scheduled run's owner
(`access_control/identity.py`), except runs of the runtime's own maintenance
schedule for InternalImprovementAgent, which it signs as system work for no
person (`custom_agents/task_context_runtime.py`). The context travels unchanged
on every delegated task, so no later hop can widen it. Private keys live only
in the gateway and manager processes; every checker holds the public keys.

**Runs claimed outside the manager.** Scheduled workers claim their own due
schedules from the shared database, so the claiming agent may be outside the
manager process. An agent inside it (the team lead, or a worker the manager
started on demand) signs the run itself. A worker started from team composition
is its own process without the scheduler key, because a key in every agent
process would let any one of them sign for anyone.

**One permit per schedule.** Instead the manager signs one permit per schedule
(`SchedulePermitV1`, `custom_agents/schedule_permits.py`). The team lead's
scheduler pass, in the process that holds the key, issues one for each
turned-on schedule whose owner is active and vouched for by the same rule as a
locally signed run (a runtime-managed schedule, or a saved context the gateway
signed for that person), and a permit for no person for
InternalImprovementAgent's maintenance schedule. A permit lasts 30 days, is
renewed in its last 7 and is re-checked when the schedule's owner changes; the
pass is one query, at most every 30 seconds. The permit is kept on the schedule
row (`run_permit_json`), not in the schedule, and like the next run time needs
no schedule permit to set. A worker that claims the schedule puts the permit,
with a fresh context id, the conversation and the start time, in the run's
`GatewayContext.schedule_run` and starts the run without contacting the
manager. One signature per schedule keeps the manager off the path of all
scheduled work; asking it for every run of every worker does not scale.

**Checking a run identity.** A permit names no roles, so it cannot carry stale
ones. Every run identity check (`access_control/task_identity.py`,
`resolve_task_identity`, used by the access hook, the broker's gate and the
schedule tools) verifies the permit, checks live that the schedule still
exists, is turned on and still belongs to the permit's person, and reads that
person's current standing and roles from the gateway's people copy. These live
facts are cached for 30 seconds per schedule and permit (bounded), so a role
change, a disable or removal, or a deleted or turned-off schedule reaches the
run's next check within 30 seconds. A permit and a task context are signed
under different domains and name different record kinds, so neither can pass as
the other.

**When there is no permit yet.** Only when a schedule has no usable permit (the
manager has not looked at a new schedule, or issued none for an unvouched
owner) does the worker ask the manager over NATS, showing its claim
(`ScheduledRunContextRequestV1` on the Core NATS subject
`<prefix>.lead.run_context`, answered on `<prefix>.agent.<id>.run_context`).
The manager signs only when the row's claim holder and claim token match the
request and the sender, takes the owner from the row by the same rules, and
answers with the signed context or a refusal reason
(`custom_agents/scheduled_run_contexts.py`). If it refuses or does not answer
within 20 seconds, the worker does not start the run and releases the claim, so
the schedule is retried later; a run without a signed context or permit would
have every tool call refused.

### Deciding each call

The secure tool broker checks every agent request before it runs anything
(`secure_tool_broker/access_gate.py`), in both authorization modes:

- the calling process's own broker token, which the supervisor derives per
  process from the broker's secret, so a separately started agent can act only
  as its own agent type and only the gateway reaches sign-in and credential
  forms;
- the signed task context or scheduled run identity (the same resolver as the
  agents' access hook) and the person it names;
- the operation and its level from the tool catalog;
- for web requests, a web API tool (`http:*`) that covers the host; other hosts
  are refused.

With authorization enabled, the access service (`access_control/decisions.py`)
then decides with Cedar whether both the agent and the person it works for hold
that level through grants, which live in the manager storage
(`storage/access_repository.py`).

One Strands `BeforeToolCallEvent` hook decides every function tool call
(`custom_agents/access_runtime.py`). It runs last, so an earlier cancellation
cannot use up a one-time grant. It verifies the signed task context in-process
and asks the access service. It skips calls the broker decides, Strands'
structured-output tool (the turn answer), and names the agent has no tool for.
Nothing runs for those; Strands answers "Unknown tool", and the runtime
guardrails add a hint when the name is a turn action such as `delegate_task`
(an access refusal there makes TeamLead report delegation as broken). With
authorization off, the hook allows every catalog tool (trusted single-person
mode); the broker, sandbox and folder rules still apply.

### Standard access

Roles hold standard access without grant rows
(`access_control/standard_grants.py`):

- `agent_runtime`: every agent (its own work, including the access tools and the
  SymPy `calculator`).
- `team_lead_runtime`: the team lead.
- `system_duties` and `sandboxed_code`: the built-in system agents
  (MemReviewAgent and InternalImprovementAgent, marked `access_role: system` in
  their shipped definitions), which alone may work with no person. Sandboxed
  code is a system duty because building team CLIs and skills is
  InternalImprovementAgent's job; with no person behind a task the sandbox
  reaches only `agents/` and `team/`.
- `internet`: every agent and person.
- `standard`: members. These everyday tools (never code or platform controls)
  let members authorize agents for their own work without an administrator
  deciding routine requests; an administrator restricts a member with a block.

The calculator is standard because it only computes
(`custom_tools/calculator.py`, level `read`). It replaces the
strands-agents-tools calculator, keeps its checks that stop an expression from
reaching Python code through SymPy's parser, and also checks the code the parser
generates (`custom_tools/calculator_parsing.py`). Tools run inside agent
processes, which must not start processes for them, so a long calculation runs
in its own thread and is interrupted between Python steps after 10 seconds
(`custom_tools/calculator_time_limit.py`). A single giant exact-number step
cannot be interrupted that way, so inputs that ask for one (huge exact powers,
factorials, precision) are refused before they run.

### Whose access counts

**Authority is per person** (decision D13). An agent grant carries
`for_person_id`, the person whose work it serves, and every validity check in
the access service (`valid_grants`, `holds`, `may_give`, `hand_down`, the Cedar
decision) takes the task's person. An agent working for one person therefore
has only what that person authorized. A hand-down inherits its parent's
person. `None` means everyone's work and is reserved for standard grants,
administrators' grants given with `--everyone`, and system work for no person.
`AccessService.grant_refusal` lets a person give agents, for their own work,
anything they hold themselves (consent within their own access, not new
authority); agent grants for someone else's work need the authority to give it
and happen only when approving that person's request.
`AccessRequests.may_decide` gives a request for a person who holds the access to
that person alone, and sends access the person lacks, and system work, to
whoever could give it (administrators for team targets).

Why per person: in a shared team the same agent types work for everyone, so
team-wide grants would let one member's approval (or TeamLead's authority
delegated by one member) act in another member's tasks. The person side of the
check alone cannot stop that, because both members may hold the same tool. The
first user is an owner and administrator but in normal use acts like any other
user, escalating only when a request needs it.

**Access follows the work.** People only talk to TeamLead; workers get work only
from TeamLead, scheduled runs or system jobs; and every tool call runs in a task
with a verified identity, so workers never act on their own account. An agent's
access (`AccessService.agent_grants`) is its role's standard access,
administrators' team defaults (grants for everyone's work), its current task's
access, and the lasting access of whom it works for: TeamLead's lasting access
for a person counts in all its work for that person, scheduled runs included,
and a scheduled job's run adds the schedule's access.

- **Schedules** are grant holders (`SubjectKind.SCHEDULE`, id
  `<team>/<schedule>`). The agent in one of a job's runs is in its schedule
  (`AgentPrincipal.schedule_id`, an extra parent entity in Cedar), taken only
  from a scheduler-signed identity (`task_identity.job_schedule_id`). A job run
  is a person's schedule that a worker type runs; its workers work for the
  schedule and get nothing handed on. Schedules TeamLead runs and the runtime's
  timers (heartbeat items, objective work sessions, after-action reviews) work
  like the person's request.
- **An after-action review** works for the person whose work the reports
  describe, taken from the original signed task context or schedule permit when
  a report arrives, even after that context expires or the source schedule
  ends. This verifies historical ownership and grants no current access: the
  scheduler signs the review with current identity, and each tool call still
  needs access. Reports and report IDs are kept per person, the review schedule
  is owned by that person, and the review tools see only that person's reports
  (`custom_agents/after_action_report_identity.py`).
- **Built-in system agent types** come from the release definitions alone
  (`access_control.system_agents`), so every access service agrees on them.
- **Workers** never get lasting access for a person's work
  (`AccessService.worker_lasting_refusal`, checked by every grant path);
  existing grants of that kind simply stop counting, with no migration. A
  worker's missing access is added to its task, inside the decision itself,
  from what TeamLead may hand on without asking
  (`AccessService.hand_on_for_task`, used by the function-tool hook, folder
  decisions, code access and the broker gate), so there is no `give_access`
  tool and no extra turn. Its tool list shows those tools (`hand_on_sources`).
- **"For one task" means the person's request.** Task-bound grants are bound to
  the signed context id every task of the request carries
  (`AgentPrincipal.work_id`, `AccessRequestV1.work_id`, never an agent-supplied
  task id). They end when the request's first task ends
  (`access_runtime._clean_up_ended_task`) and, as a backstop, when older than
  any context can live.

Why one source: two sources that override each other per tool need precedence
rules (tool set against tool against operation against folder subtree,
narrowing against widening, unnamed tools falling back), each an edge case; with
one source there is nothing to resolve. Access is always a level on a whole
tool: grants and requests naming one operation are refused
(`AccessService.operation_refusal`), because operation names are fragile
(sending mail is both `gmail_send` and `command:gmail.send`). Blocks may still
name one.

### One entry per tool

An agent's access to one tool for one person's work is one entry
(`access_control/grant_entries.py`). A new grant that covers an existing one
replaces it in the same transaction, and what was handed on from the old grant
moves to the new one (`AccessRepository._replace_in`, a `grant_moved` event), so
replacing never cuts off a worker; this relaxes "grants are immutable" for
`parent_grant_id` only. A grant an existing one covers is not stored. Grants
neither covers stay side by side (lasting access plus a step-up for one task).
Removing access removes the whole entry (`AccessService.revoke_by_person`,
`AccessRequests.revoke`, proposals' removals). Why: with several independent
grants for one tool, people assume the broadest replaces the others, and
removing one can leave another working.

The plan runs after the store's write lock is taken
(`AccessRepository.store_planned`, `approve_pending`, `apply_together`), so of
two grants stored at once the second sees the first. Two first calls of one
tool in one request therefore cannot both store a task grant, and a tool grant
stored with its tool set cannot stay as a separate entry that removing the set
leaves working. A grant that a person's decision replaces ends only if the
decision's outcome is stored (see
[Each request is decided once](#each-request-is-decided-once)).

### Folders and file sharing

Folders are access targets too (`access_control/folders.py`). A folder is named
by an id below a registered root, never by a host path, so grants survive a
move: `team/…` and `agents/…` (every role holds `write` on both as a standard
grant), `data/<name>/…` for registered data folders (these need grants), and
`users/<folder>` for a person's own folder.

A person's folder is owned by that person. The standard Cedar policy
`std:task-person-folder` lets an agent use, up to `write`, the folder of the
person its task works for (`context.person` comes from the signed task
context). Only the owner may give access to it or to a single file in it
(`TargetKind.FILE`), through `std:owner` and only to other people: a share
(decision D15). Administrators cannot open or share it, and agent grants on a
person's folder are refused. An agent working for the recipient uses what was
shared with that person at the shared level, for file tools and sandbox mounts,
through per-decision copies of the person's share grants
(`AccessService.shared_with`); blocks on the agent still apply.

In Cedar a file is inside each folder up to its root, so a grant or block on a
folder covers everything below it. `AccessService.decide_folder` decides each
file use (agent side, then person side). `custom_agents/folder_access.py` keeps
allowed answers for 10 seconds per folder, keyed by the access store version, so
a crawl or a Python environment does not ask for every file while every grant,
revocation or block still applies to the next file. `request_access`,
`restrict_access`, `/access` and `assistant_cli access grant|block --folder`
accept folders, and a worker's task gets a folder from TeamLead's access like a
tool.

People share and send files through `assistant_gateway/file_sharing.py`
(`/share`, `/unshare`, `/shares`, `/send`, and the workspace browser's Share and
Send). Sending into another person's folder is a change request made by a person
(`AccessRequestV1.requested_by_person_id`, kind `file_delivery`) that the
recipient or an administrator decides. The file's SHA-256 is recorded when the
request is made, and at the decision the gateway copies exactly that content,
without following links, into `users/<recipient>/received/<sender>/` (a move
removes the original only while it still matches). The digest is needed because
agents' code can write to the sender's folder: without it the recipient could
approve one file and receive another. Copying to the shared `team/` folder needs
no approval, and agents move files between folders they may write with
`file_move`.

### Access settings

Access has four settings: `authorization.enabled`, `cedar_binary_path`,
`starter_profile` and `presentation.mode`. Every check always applies: no switch
turns an individual check off or into observation only, and the Cedar policies
ship with the release (`ua_definitions/access/cedar/`) instead of being
configurable, so a configuration file cannot weaken a check.

`starter_profile` is the first user's default for TeamLead's standing access to
their work, applied by the gateway at start until that person chooses their
own. Every other person chooses theirs (`/access profile`, the Access page, or
`assistant_cli access starter-profile --by`), and it serves only their work.
Each choice is recorded with whether the person made it
(`access_profile_choices`), because "manual" leaves no grants to recognize and a
restart must not undo a person's decision.

The loader refuses by name, rather than ignoring them, the settings of the
earlier permission system, the removed `tool_policy` switches and
`tool_policy.data_folders` (`runtime_settings/governance.py`). Startup refuses
the removed file-path environment variables (`TOOL_RESTRICT_TO_WORKSPACE`,
`TOOL_DENY_PATHS`, `TOOL_DENIED_PATHS`). An operator thus learns that they no
longer do anything.

### Access requests and decisions

Agents ask for access with `request_access` (`access_control/requests.py`).
When the team lead holds an automatic, delegable grant covering a worker's
request, trusted code hands it down at once; the team lead's model does not
decide. Otherwise the request is stored and the task pauses with a Strands
interrupt that waits in the agent's own process; it is never sent to the
requester as a question. The gateway's access Inbox
(`assistant_gateway/access_inbox.py`) announces the request once and lets a
person who could give the access decide. The gateway then sends
`AccessRequestResolvedV1` (ids only) to the agent, which reads the decision from
the store and resumes the interrupted call, or the task after a restart
(`custom_agents/access_request_runtime.py`).

Notices for automatic hand-downs, team skill changes, release changes, shares,
received files and credentials to choose are stored for people and never pushed
to a chat. Each review card lists every row to approve
(`ChangeProposalV1.items`, `ReviewItemV1`). Grant rows carry "Keep for later
tasks" (off: this task only) with a "How long" choice, and TeamLead's own
requests carry a box that lets it hand the access on to workers without asking.
Chat buttons have no checkboxes, so they offer the same choices as separate
buttons (`assistant_gateway/application/access_reviews.py`).

#### Each request is decided once

`AccessRequests.decide` decides each request once. A person's decision stores
its grants and its outcome in one transaction
(`AccessRepository.decision_transaction`). It takes the access store's write
lock and checks that the request still waits. Everything the decision then does
on the store in that thread joins the transaction, so its authority checks see
the grants it stored a moment ago. Only the thread that opened the transaction
joins it: work handed to another thread, even with a copied context
(`asyncio.to_thread`), opens its own connection and sees only what was
committed. Other writers see none of the decision's grants until the outcome is
stored with them, and log lines about what the decision replaced are written
only after it commits (`AccessRepository.after_commit`). Replacing narrower
access (one entry per tool) happens in the same transaction. A denied or
withdrawn request denies its hand-downs still waiting for approval in the same
transaction; only a granted outcome keeps the hand-downs it approved in place.

A second decision made at the same moment (a double click, a chat button and
the web card, two reviewers, `assistant_cli access decide`), a withdrawal (the
task ended, or its person left) or another writer (a person giving access on
the Access page, a worker's request that trusted code may hand down) waits for
the lock and then works on what was committed: the second decision finds the
request decided. A decision that fails, or cannot get the lock, stores nothing,
so there is nothing to take back and no access in force for a request that
still waits. When the database was busy or could not be reached, the request is
first read back, because a lost PostgreSQL commit answer may hide an outcome
that was stored. The person is told so in plain words ("Nothing was changed; try
again"), and the failure is logged as `event=access_decision_failed` with the
request and the reviewer.

Why one transaction: with separate transactions for the grants and the outcome,
a lost race revokes the winner's grants and access in force before the request,
a failed approval leaves access in force that a later Deny does not remove, and
other writers rely on an undecided grant (a person's own grant is answered
"already covered", a worker's request is handed down from it) and lose that
when the decision fails. Claims, undo and postponed replacements cannot close
that window; one transaction removes it at the root. Holding the store's
lock across a decision cannot deadlock, because every read a decision makes goes
through the store's joined transaction, and the evaluator (Cedar) only computes
over the data it is given and never waits for the store while holding its own
lock. A decision holds the lock for the time of a few evaluations.

A grant that carries the approval id of a request that was not granted is never
kept in force. None can be written now, but older versions stored a decision's
grants before its outcome and could leave them behind when a decision stopped
in between. The gateway's Inbox pass ends such grants
(`end_grants_without_granted_outcome`, found by the indexed
`access_grants.approval_id` column). A decision ends those carrying its own
request's id before it writes anything, so a decision made before the first
Inbox pass cannot make one permanent. The team transfer leaves them behind.
Existing stores get the `approval_id` column added and filled from each grant's
record (`GrantV1.approval_id`) at their first open, in one transaction; that
record is how grants travel, so the transfer format does not change.

### Access proposals

When nobody can hand the access on, or a service is missing from the tool
catalog, TeamLead proposes the whole change with `propose_access_change`
(`custom_tools/providers/access_proposals.py`, catalog `fn:access_proposals`):
typed items for new web API tools, grants for agent types, restrictions, data
folders, account links and revocations (`AccessProposalV1`), never code or
policy text. `access_control/proposals.py` (`ProposalCompiler`) checks every
item against the catalog as it would be with the new tools and writes the
review rows in trusted code's words, flagging items that widen access. The
proposal is one change request on a platform tool at `full`, so an
administrator decides it, and the Inbox, chat buttons, workflow linking,
withdrawal and resume work as for any change.

### Changes applied at the decision

Some changes are applied by the gateway at the decision itself rather than later
by the agent that asked: approved proposals (`ProposalApplier`) and file
deliveries (`GatewayFileSharing`), registered as `AccessRequests.appliers` (a
`ChangeApplier` port, like `RequestWatcher`, so `access_control` never imports
gateway code). Deciding such a request without its applier, for example from
`assistant_cli access decide`, is refused. Why at the decision: only the gateway
holds the reviewer's verified roles at that moment, so it can check each item
against the reviewer's own authority, and nobody would be waiting to apply a
file a person sent.

The proposal applier re-reads the stored plan, compares its digest with the one
reviewed, writes any new tool file to `control/access/tools/` from the typed
item (`access_catalog/instance_tools.py`, only `http:*` tools), reloads the
catalog, and stores grants, blocks, data folders, revocations and the request
outcome in one transaction (`AccessRepository.apply_together`) under the
decision's claim. If anything fails, including a busy database, the tool files,
downloaded programs and MCP servers it wrote are taken back, so nothing is half
applied. A tool with no grants gives nobody anything, which is why writing its
file first is safe. The claim is held from before the first file is written, so
a second approval of the same proposal is refused while the first works; if the
first stalls past its limit and the second takes over, the first keeps the
programs the second may rely on (see [Decision claims](#decision-claims)).

Afterwards the gateway opens the broker's account link form for the approver,
watches it with the tool setup watcher
(`GatewayToolSetups.after_proposal_links`), and holds the decision message to
the waiting task until every form ended. The resumed `propose_access_change`
then reports which links exist. Without the hold, the task resumes before the
link exists and calls a tool still refused for a missing account. The hold
lives in gateway memory; after a restart the Inbox loop sends the message and
the tool reports the links as they are.

#### Decision claims

Appliers also do work outside the database: downloads, tool files, MCP records,
a copied file. They first claim the request (`AccessRepository.claim_decision`,
a short-lived row in `access_request_claims` with an owner and a time limit,
`DECISION_CLAIM_SECONDS`), do that work, and store their grants and outcome
together (`apply_together`, `finish_request`) only while the claim holds. Any
other decision is refused while the claim is current ("Someone is deciding this
request right now"). The claim is a separate transient table rather than a new
request status, so readers of waiting requests are unchanged and the team
transfer, which never carries access requests, is unaffected.

The claim is renewed before and after each program download. A download stops
after `DOWNLOAD_DEADLINE_SECONDS` measured from connecting to its last byte
(redirects, TLS and headers included). A timer shuts a duplicate of the
transport down, because the HTTP client detaches its socket after headers and a
socket timeout bounds one read only. The deadline is checked again after each
host name lookup, which nothing can interrupt, so a slow lookup overruns by its
own length at most. A claim therefore runs out only when a step stalls past its
limit or the process ends.

Once a claim runs out, the gateway first reconciles any recorded effects; then
another decision may take over. The stalled decision finds its claim lost at
its next renewal or when storing its outcome, and stores nothing. Cleanup reads
the outcome and claim under the access store's write lock, so a new decision
cannot take over between that check and removing local files. A losing decision
keeps programs another decision may use, whatever the exception type, including
a failed renewal that reports a database error.

#### Recovery records

Before doing external work, each applier stores a transient recovery record in
`access_decision_recoveries`: the expected decision and its effects list. Before
writing each file's bytes, it records the relative path, file identity,
expected size and content digest. Tool definitions stay in a hidden staging
file until complete, then are published without replacing an existing file. MCP
activation rechecks the claim under the cleanup lock after journaling, so a
paused approval cannot activate a server after recovery releases it.

A confirmed rollback removes only files still matching that ownership record; a
replacement or edited file is kept and logged. Failed cleanup keeps the record
for another pass. A lost PostgreSQL commit answer may hide a committed outcome,
so a failed readback is an unknown result and never authorizes cleanup: the
person is told to check the Inbox when the store returns, rather than told
nothing changed, and the files, claim and recovery record stay.

After the claim period, Inbox maintenance rereads the latest effects, outcome
and claim under the store lock, even after a gateway restart. Claim renewal also
postpones recovery, so an old maintenance scan cannot clean up a live download.
Committed work stays, an uncommitted delivery is removed, an uncommitted
proposal is undone, and a committed move finishes removing its unchanged
original. Delivery notices use a request-derived id, so recovery cannot store
duplicate notices. Unresolved recovery records prevent another decision from
taking over and prevent an access transfer export until the gateway has
reconciled them; they never travel in a transfer. The journal makes cleanup
retryable across database failures; filesystem changes are still separate from
the database transaction.

### People and roles

Each person holds one team role (`owner`, `admin` or `member`) in the
gateway's membership records, read again at every decision, so a role change
applies at the next one. An owner is also an administrator wherever roles are
checked. Only owners change who is an owner; the store refuses a change that
would leave the team without an owner or an administrator. These rules and the
gateway's checks for adding, listing, inviting, disabling and removing people
live in one place (`access_control/people.py`). The instance's user is created
as the first owner. Only an active person with an active membership in the
gateway's team resolves at any entry point (chat channels, Web Chat, invites).
The gateway copies each change to the access store for the scheduler
(`assistant_gateway/people_directory_sync.py`).

**Disabling** is reversible. The person keeps their membership, folder,
memory, conversations, grants and linked accounts, but their work ends: the
manager cancels their running and queued task trees, stops their objectives
and deletes their schedules, and the gateway withdraws access requests waiting
on them.

**Removing** (`/user remove`) marks the membership removed (the row stays for
history) and at once deletes their task records, objectives, chat sessions,
sign-ins, grants for their work and grants they gave, personal account links
with their stored credentials, their GitHub, Google and X sign-ins, the
broker's credentials folder for them, requests, notices and sandbox. Their
folder, personal memory, own knowledge pages and conversations are held out of
every agent's reach for `gateway.users.removed_data_retention_days` (the folder
under `state/held_people/`, the rows in `person_held_rows`), then purged by a
maintenance run unless `/user restore` (or adding them back the usual way
while held) brings them back.

**Schedules.** The runtime's own team schedules never leave with a person:
they move to another active administrator
(`access_control.people.team_work_owner`). The person's per-person system
schedules (memory review, objective work sessions, recovery checkpoints,
retries, after-action reviews) are deleted with them.

**Order of work.** The gateway does its part after the identity change commits
(`assistant_gateway/application/person_lifecycle.py`,
`assistant_gateway/person_departure.py`) and sends the manager a typed
`PersonStandingCommandV1`, which only a gateway-signed administrator context
can send. The manager acts on the standing in the people copy
(`custom_agents/person_departure_runtime.py`) and runs the same idempotent
step for every disabled or removed person at start and before each
maintenance run. Database cleanup resolves PostgreSQL tables and columns
through the connection's schema search path, as its deletion and restoration
queries do, so tables in unrelated schemas cannot change which records it
finds or restores.

### Blocking access

People block an agent type, an agent, a person, a role or the whole team (the
last three: owners and administrators only) from a tool set, a tool, one
operation or a folder with a block grant (`AccessRequests.block`, Cedar
`tpl:block`). From the next decision it wins over every grant, standard ones
included, and the access event log records it, so an administrator can stop
access at once; `/access block team internet` turns internet access off.
Standard catalog functions are withdrawn from both Strands' startup and
dynamic registries; unblocking restores them before the next model call.

**Stale blocks.** A block naming an operation the catalog no longer has (an
update renamed or merged it) blocks its whole tool
(`access_control.stale_blocks`, applied in `AccessService._evaluate`), so a
rename never lifts a block; the gateway tells administrators about each one at
start. Whoever may remove a stale block can narrow it with
`/access block ... --replace <block id>`: the service checks the same subject
and tool and keeps the expiry, and the store inserts the replacement and
revokes the old block in one guarded transaction. Removal authority applies
even while the old block stops the administrator's own access. The team
transfer converts the names its release renamed
(`team_transfer.access.RENAMED_OPERATIONS`) in both supported access formats
during review and application, because version 3 exports predate the action
tool merges and a schema conversion alone would miss their old names. Before
writing the destination it reviews stale blocks against accepted team-tool and
active MCP definitions.

### Access commands and the Access page

The `/access` chat command (`assistant_gateway/access_commands.py`) and
`assistant_cli access` share helpers
(`access_control/operator_commands.py`) and act within the person's own
authority. The Web Chat **Access** page (`assistant_gateway/access_map.py`,
`onboarding_web/assets/access_map.mjs`) is the access map, each view paged at
20 on the server:

- **By agent type:** TeamLead's standing access for the signed-in person's
  work, then each type and each scheduled job holding access: the standard
  access by role, one row per tool entry grouped by whose work it serves with
  its chain from `AccessService.chain`, and blocks. Members see their own work
  and everyone's, administrators all of it; a worker's lasting access that no
  longer counts is not listed.
- **By person:** members see only their own.
- **By tool:** operation levels and account links, never credential keys.
- **Files:** shares by and with the person, and data folders.

Its buttons call `revoke_by_person`, `AccessRequests.block` and `unblock`, and
the starter profile, share and data folder services, as the signed-in person.
The gateway only chooses which buttons to show; each action is decided again
when it runs. The page is built by the access Inbox on the same access
service, so the gateway keeps one Cedar evaluator for people.

### Platform changes

Platform changes (runtime settings, restarts, new specialists, team skills,
hosted MCP servers, managed execution services, typing into SSH sessions and
changes to schedules the runtime manages) are staged as exact, digest-checked
plans and applied with one tool, `apply_change` (`access_control/changes.py`,
`custom_agents/change_approvals.py`). Agents never hold `full` on platform
tools (a standard Cedar guardrail), so with authorization on a person
approves the exact plan in the Inbox; the resumed call claims the approval
once and applies the plan in the agent's process. With authorization off, the
person's conversational agreement applies it. The services only describe and
apply plans; the approval they record is the access request a person decided.
An approval never bypasses the owning component's validation, and installing
something grants no access to use it: the access service still decides every
call. TeamLead's first grants for a person's work come from that person's
starter profile (`access_control/starter_profiles.py`).

#### Model choice changes

One narrow class applies at once: a settings plan that only switches an agent
type's model (provider, model, reasoning, context window and input settings;
`custom_agents/model_choice.py`) is the `model_choice` change on
`fn:model_choice`. Only administrators hold that tool (Cedar
`guard:admin-only-tools`), so it applies when TeamLead holds it for an
administrator's work (the Trusted profile gives it), and that administrator
gets a notice. Model switches are frequent, bounded and easy to undo;
everything else keeps the review.

**Testing.** Staging a model-affecting plan tries each target model once
(`RuntimeLLMOverrideApplier.prepare_candidates` and `test_candidates`; try
once, then ask), including instance-baseline `llm.*` changes with the
baseline's own endpoint and request settings. A failed test stages nothing.
Applying retests before any write, because the endpoint, key or model may
change in between, then rechecks the source hash so an operator edit made
during the test is refused. Window notices use the target profile the test
used. With authorization off, a tested switch requires conversational
agreement (`confirmed=true`); with it on, the access-service decision remains
authoritative.

**When it takes effect** (also under "Model provider keys"). Model settings in
`LLM_OVERRIDES.yaml` (per agent type or the workspace `default:`) need no
restart for any agent type: every affected running agent rereads the file at
the start of each turn (`UniversalAgent.refresh_persistent_llm_override`,
through `model_failover_runtime.refresh_runtime_llm_selection`), and new agents
read it at start. A plan explicitly targeting the applying agent's own type
switches it at once; a default-only plan reaches it at its next turn when it
inherits the changed setting. A temporary failover lease on the primary reads
that profile too; a lease on a fallback keeps the fallback's profile and its
existing lifetime. Settings in `instance_config.yaml` (the `llm.*` baseline,
temperature, output limits, streaming) are read at start and still report a
manager restart. The apply result says when the change reaches running agents
(`running_agents`), so nobody is asked to approve a restart that changes
nothing. An idle agent's status report keeps the old values until its next
turn, so `get_runtime_configuration_context(agent_types=[...])` shows the saved
values, resolving the saved instance configuration on each call even when the
caller omits the full file snapshot. An unreadable file returns an error
instead of startup values.

**Clearing a setting.** Failover selection keeps explicit null override fields
distinct from missing fields, so disabling a reasoning summary or clearing
another optional model setting also takes effect next turn. A sparse
agent-type entry may clear an inherited setting without naming a model: the
loader preserves explicit nulls and inherits only omitted fields. This keeps
saved values, startup and next-turn selection consistent instead of treating
an intentional clear as an old bootstrap placeholder. Settings saves preserve
explicit null request constraints in the model catalog, since null disables an
inherited model option.

#### Approver notes and restart receipts

`apply_change` returns the approver's note from the Inbox with every decided
outcome (applied, denied, or refused because the approved plan no longer
applies), as `request_access` does.

A manager restart (scope `manager` or `all`) ends the process running
TeamArchitect, so the task that asked for it can stop before it reads the
result, and its model conversation is kept only in memory. On resuming it
would see only the person's message ("approve the restart") and ask for the
same restart again, in a loop. So the restart receipt names the task that
asked (`source_task_id`) and the approver's note, which the agent publishes
with its approved request in one atomic file write.

- **Dispatch.** The supervisor records the dispatch receipt before it removes
  the request or stops a process; preparing an unpublished request never
  counts as dispatch. Status is `requested` while the request waits and
  `dispatched` after supervisor acceptance. An accepted request retained across
  a supervisor interruption is not replayed. Supervisor request schema 4
  requires the task and note for agent requests; older pending request files
  are refused rather than converted.
- **Recovery.** Restart recovery (`custom_agents/runtime_recovery.py`,
  `tell_task_about_its_restarts`) reads receipts for every recovered unfinished
  TeamArchitect task, waiting and queued ones included. It adds a resume
  directive saying what happened, when, and what the approver wrote, with an
  instruction to check the result and report it instead of restarting again.
- **Continuations.** All runtime continuations append their instructions, so
  unread restart outcomes stay until the next model invocation consumes them.
  Duplicate notes are matched at their boundaries, so a shorter requester
  correction is kept. Interrupt replies include their steering event id, so
  distinct replies stay separate even when one ends at a paragraph boundary
  inside an earlier reply.

### MCP servers

An installed MCP server is one team tool, `mcp:<name>`, in the `mcp_servers`
tool set that members hold as a standard set (`access_catalog/catalog.py`,
`access_control/standard_grants.py`), so it follows grants like every other
tool. Installing a server is a team decision, like a new web API tool, while
using it is each person's decision for their own work.

**Installing** is an item of an access proposal
(`AccessProposalV1.mcp_servers`, `access_control/proposals.py`).
`mcp_connections(action="propose")` records the inert connection and proposes
it in the same call, with default rows from `access_control/mcp_servers.py`:
the proposed agent types and TeamLead, which may hand it on, get `full` for the
requesting person's work. An administrator approves once; the gateway's
applier activates the record as that administrator's approval with the
request id and stores the grants in the same store transaction as the rest,
putting the pending record back if that fails. An administrator can also
approve a pending server in Settings, which gives its target agent types
`full` for everyone's work. `full` on the tool covers every operation,
including unconfirmed ones (which need `full`) and ones the server adds later,
so agents never come back for each function; confirming lower levels is only
for narrower grants.

**Using.** There are no per-agent-type attachment records. The broker lists an
active server's tools for any agent type the calling process may act for,
decides each call with the access gate for the agent and the task's person (no
rule reserves a server to the person who proposed it), and fills credential
slots from the calling person's own account link, then the team's while the
team shares the server. It returns a refusal's reason, tool, operation, level
and next step as the JSON body of its 403, which the agent's MCP client gives
to the model (`custom_agents/sdk/strands/mcp.py`).

### Accounts and sharing

#### Account links

Credentials are chosen by account links, not by agents. A link says which
stored credential one tool uses for the team or for one person, and where each
credential field goes (a header, query value or environment variable). The
broker uses the person's own link first, then the team's, and sends the value
only to the tool's hosts or MCP connection. A link grants no access by itself.

**The link form.** People create links in the broker's labeled form
(`/link <tool>` in chat, or a setup page TeamLead opens). In one submission
the form (`secure_tool_broker/account_links.py`) either links a stored
credential that has every field the tool needs, or saves a new one the person
types through the broker's own store path (`collect_credential`, in the team's
collection for a team link and the person's for their own) and links it. A new
credential gets a key made from the tool and owner, plus a number when that
key is taken, so it never replaces a stored credential.

**One active link** per tool and owner: creating one reads and revokes the
earlier link in the same transaction, and a partial unique index refuses a
second active link. A store that holds two from before the index (two forms
finished at once) keeps the newest, the one in use, and revokes the older at
the first start, logging `event=account_link_duplicate_revoked`, so unlinking
the newest cannot put the older credential back in use.

**Used by.** The broker reads the form's **Used by** line (agent-side grants in
force that reach the tool for the owner's work) from the grant store it
already holds, not from the gateway, so the form request contract stays the
same and every caller sees the same line. It is for information only; the
access service still decides every call.

#### Team account sharing

Whether the team's account serves everyone is the team's choice per service
(`access_control.account_sharing`). A tool that takes both a person's and the
team's account (`ToolDefinitionV1.sharing_choice`) is either **shared** (the
team's account serves anyone without their own) or **private** (the team's
account is never used, not even for work that serves no person). The choice is
one `AccountSharingV1` row per service in the access store; without one, the
tool's `default_sharing` applies (shared when it names none). Services that act
as a person start private (X); services that supply a resource start shared
(search, model downloads). GitHub and Google use only each person's sign-in,
and the GitHub API (`http:github`) takes only each person's own link, so they
are always private. This lets an administrator keep personal accounts (code,
mail, posts) to their own work while still sharing cheap resources such as
search keys, and nothing falls back to someone else's account without that
decision.

**Changing it.** Administrators decide
(`access_control.people.account_sharing_refusal`) with `/accounts
share|private <tool_id>` in chat (`assistant_gateway/account_sharing.py`);
each change is in the access event log and sends administrators an Inbox
notice. `/accounts` and the Web Chat's read-only **Settings → Accounts** tab
(`GET /api/web-chat/accounts`, `onboarding_web/assets/accounts_view.mjs`) are
built from the same rows (`ServiceAccounts`, one gateway instance for both),
so the chat list and the tab cannot disagree. Changes stay in chat, where the
administrator check and the notices already are. The Accounts route runs the
whole read, including the person's current membership, through
`website_handlers.off_loop`, so slow identity or account storage cannot stop
the other chats that share the gateway's event loop.

**One answer everywhere.** Everything that picks or reports an account asks
the same `TeamAccountSharing`: the broker's gate drops the team from a private
tool's owners, the link form refuses the team's account for it
(`account_link_team_private`), the setup status reports a team account only
while it is used, TeamLead's setup check offers only the person's own link,
and the Access page says so. A team link that becomes unused is kept, so
sharing the service again needs no new link. The chat commands people are told
to send (`/link`, `/accounts`, `/setup`, `/auth model me`) are named once in
`secure_tools.chat_commands`, from which the gateway, the broker and the
agents' runtime build their hints.

#### First-time setup for a new person

A person an administrator adds is shown their first-time setup on their first
chat message (`assistant_gateway.personal_setup`): their own model, when they
may not use the team's model keys and saved none, and their own account for
every service the team keeps private that someone in the team already uses (a
linked account of any owner, or a sign-in), so a team that never uses GitHub
does not send every new person there. Trusted gateway code shows it, because a
person without a model cannot reach an agent. This way people provide their own
credentials first when the team keeps them private, without agents and without
blocking anyone the broker would let work.

Adding someone records `personal_setup_due` in `gateway_user_events`, and the
add reply promises the list only when the setup was actually recorded as due.
The message path (`GatewayInteractiveSubmissionService`, through
`GatewayPersonalSetupGate`) shows the list once (`personal_setup_shown`): as
the reply, holding the message back, when the model is missing; otherwise
posted in the chat before the message goes to the team. Later messages are
never held, because a personal key the person saved for the team's endpoint
also works and only the broker can see it; the broker's refusal names
`/setup`, which lists what is left at any time. People added before this setup
existed have no setup due.

#### Web search services

The web search services follow the same rule. Exa, Brave Search and the
instance's Crawl4AI server are web API tools (`http:exa`, `http:brave-search`,
`http:crawl4ai`) in the `internet` set. Their keys are account links, never
environment variables (startup refuses `EXA_API_KEY`, `BRAVE_SEARCH_API_KEY`
and `CRAWL4AI_API_TOKEN`), because keys in the agent processes' environment
would be readable by any code they run and could not follow a person's own
account or a block. The research tools (`custom_tools.exa_tools`, the Brave
artifact tools, `capture_web_screenshot`) send each request through the broker
without a key (`custom_tools.broker_web_requests`), and the broker adds the
linked key.

**Crawl4AI.** Its address is configuration, so its catalog entry takes its host
and port from `web_services.crawl4ai.base_url` and matches only that exact
address. Because it is the team's own server, only the team's token opens it:
the catalog requires `link_owners: [team]` of every tool whose address is a
setting, so the Crawl4AI web tool is always shared and no sharing choice can
stop its screenshots. The Crawl4AI MCP connection (`mcp:crawl4ai`) is an MCP
server like any other and keeps its choice (shared by default), because the
catalog cannot tell the team's own MCP server from an outside one.

**Keys from onboarding.** Onboarding cannot reach the broker, so it leaves the
Exa and Brave keys it was given in a private file under `state/`, with the
Secrets page's sharing choices. At start the gateway
(`assistant_gateway.onboarding_account_keys`) records the choices once and
removes them from the file, so a restart never undoes a later `/accounts`
change; the keys wait while the choices cannot be recorded. It then hands the
keys to the broker on a gateway-only route, each for the team when the team
shares the service and as the onboarded person's own when it is private (the
broker checks a team link like the form does), and deletes the file. The
page's rows come from the catalog (`onboarding_sharing_services`), and the
server refuses a choice for a service the team cannot choose
(`access_control.account_sharing.onboarding_choices`).

#### The /link command

`/link <tool_id>` is the one chat command for a tool's account. For a sign-in
tool it starts the person's own sign-in in the broker, for a linked-credential
tool it opens the link form, and for a tool with no account the broker replies
that there is nothing to link. The sign-in tools are listed in
`secure_tools/auth_methods.py` (`SIGN_IN_TOOLS`: `cli:github`, `cli:google`,
`cli:x`, each with its sign-in methods and whether it also takes a team link),
next to the broker's sign-in method names rather than in the tool catalog,
because the broker needs code for each sign-in
(`secure_tool_broker/auth_flows.py`, `secure_tool_broker/x_sign_in.py`).
`tests/test_access_catalog.py` keeps the list and the catalog's `account`
values in step.

The gateway watches each page `/link` opens. When the page ends (linked,
closed, expired, or not confirmable), it posts one message in the chat that ran
the command through its normal delivery path, and Web Chat removes the `/link`
card when that message arrives (`assistant_gateway/application/link_results.py`).
It uses the same broker watcher as forms an agent task waits on
(`assistant_gateway/application/broker_session_watch.py`), so both follow one
rule for how long to wait and what an unreadable page means. Watches live in
gateway memory; after a gateway restart no message follows for a page that was
still open.

`/auth` keeps only what is not a tool's account: sign-in status, the team's
sign-in apps (`/auth google team_client`, `/auth x team_client`;
administrators only, granting nothing, watched like `/link` pages), and the
password managers the broker reads saved credentials from (Bitwarden,
1Password, KeePassXC, Vault).

### Asking for credentials

**Only TeamLead asks the person for credentials.** Specialists get a turn
answer without the credential-form and signup actions
(`custom_agents/structured_llm_messages.py`, `ModelWorkerTurnDecision` and
`CompatibleWorkerTurnDecision`, chosen by `TurnRuntimeSettings.is_team_lead`).
If a model answers in prose, its structured-output recovery instructions name
the selected turn tool and the same role's allowed actions, so recovery does
not ask specialists to use credential actions their schema rejects. A
specialist that needs a credential, sign-in or account escalates
(`manager_escalation`, `needs_user_input`); TeamLead sets it up with
`set_up_tools` or its credential form and answers. TeamLead's credential forms
and signups go to the person's chat even while it works on an escalation
(`message_routing.requester_bound_primary_route`), like setup pages.

A request that still reaches an agent (another release, a legacy output path)
is answered with a non-terminal `SecureCredentialCollectionResult` telling the
specialist to escalate
(`task_message_runtime.refuse_credential_request_from_delegate`,
`event=delegate_credential_request_refused`). Otherwise the delegating agent
would drop it as an unhandled type and the specialist would wait until the
watchdog failed its task.

### Tool setup

TeamLead sets up the tools a request needs in one trusted call, without telling
the person which command to run or asking whether to start. The approval cards
and setup pages are the consent points, so asking first wastes a round trip.
Trusted code runs the routine the same way every time; a model following a
six-step routine does it differently from run to run. Nothing the model writes
opens a page: there is no `check_tool_setup` tool or `tool_setup_request` turn
action.

1. **The tool.** `set_up_tools` (`custom_tools/providers/tool_setup.py`,
   `fn:tool_setup` at write, standard for the team lead in both authorization
   modes) is the only way to open a setup page.
2. **The check** (`custom_tools/tool_setup_check.py`) reports per tool the
   access of TeamLead, a named worker type and the person (the access
   service's side-effect-free `holds`). From the broker's `tool_setup.status`
   action (`secure_tool_broker/tool_setup_status.py`, catalog tool
   `procedure:tool_setup`, reached only through `fn:tool_setup`) it adds the
   person's sign-in with the account name, their own and the team's links,
   and the team's sign-in app. The person always comes from the signed task
   context.
3. **The plan** (`custom_agents/tool_setup_runs.py`) turns the gaps and the
   tool's catalog `setup` data into pieces, with no per-tool code: access
   requests, the worker's access, sign-ins, account links, the team's sign-in
   app, and steps done outside Universal Agents
   (`ToolSetupSpecV1.external_steps`). Pieces follow the tool's
   `prerequisites`; a sign-in waits for the team's sign-in app, and an account
   link for the tool's outside steps. A piece only an administrator can do,
   for a person who is not one, ends the call before anything is asked
   (`needs_administrator`, plain lines). Setup data is catalog metadata from
   shipped YAML, an operator's instance file, or an approved access proposal
   (`ProposedHttpToolV1.setup`). It is checked strictly (bounded text, https
   links without sign-in details or key-like query values, nothing that looks
   like a secret) and shown on the review card in the exact words people will
   see.
4. **The run** (`ToolSetupRunV1`, stored in `tool_setup_runs` in the manager's
   database by `storage/tool_setup_run_repository.py`, compare-and-swap
   versions) holds each piece's state. The call publishes the access workflow
   if one is needed (see below), asks for TeamLead's missing access (found
   again by the call's id), and pauses the turn with a local Strands interrupt
   (`tool_setup_run`).
   `task_steering_runtime` keeps it local, like access requests, and records
   it as the task's blocker. `custom_agents/tool_setup_run_runtime.py` then
   opens every page that may start, all at once (one `ToolSetupRequestV1` per
   page to the gateway's route for the person's chat). It hands the worker its
   access with `AccessRequests.give` once TeamLead holds it, applies each
   `ToolSetupResultV1` and access decision, offers an expired or unwatched
   page again (at most three per tool and kind), and stops at a closed page or
   a denial. It then resumes the turn: the live interrupt is answered, or after
   an agent restart the task resumes from a directive with the same compact
   result. The call returns `ready` and `what_happened` per tool.
5. **The gateway** (`assistant_gateway/application/tool_setup.py`) opens the
   matching page through the same broker calls as `/link` and `/auth`, and
   opens several pages of one task side by side (a sibling page of a task
   that already waits opens too, `credential_handoff`).
   - **Workflow notice.** The run, not the gateway, announces its workflow:
     before the first page opens it sends one `UserNotification` to the
     person's chat with the page's full address (`workflow_notice`,
     `event=tool_setup_workflow_announced`, once per run). A link in a setup
     reply's text would not be shown, because Web Chat renders a setup card
     from its description and the transcript from its receipt, and channels
     such as Telegram show only text. A notification shows the same way in
     every channel.
   - **Event codes.** Steps trusted code changes carry its own event codes
     (`access_requested`, `access_approved`, `setup_*`), never
     `agent_reported_*` (`AccessWorkflowService.trusted_progress`), because
     that code means the agent reported it.
   - **Outside steps.** An `external_step` shows the catalog's words
     (`assistant_gateway/tool_setup_guide.py`), opens no broker page, never
     expires, and ends when the person marks it done on the workflow page
     (`POST .../workflows/{id}/setup_done`, owner only).
   - **Page limits.** The gateway still enforces them
     (`TOOL_SETUP_OFFER_LIMIT`, no page after a declined one,
     administrator-only steps), because it records every page.
   - **One result per wait.** Each wait is a row (`gateway_tool_setups`,
     `ToolSetupRecord`) finished with a conditional update, so exactly one
     result reaches the task whether the page's watcher, the person's own
     `/link` or `/auth ... team_client` (`link_results`), the Mark Done
     button, or start-up recovery ends it. After a gateway restart, pages
     still waiting are reported as unwatched (and offered again). Page
     addresses are never stored.

**Last known states instead of model re-checks.** Trusted code keeps one
`ToolSetupStateV1` per team, person and account tool
(`access_tool_setup_states` in the access store, which the broker, the agents'
runtime and the gateway all open in both authorization modes;
`access_control/tool_setup_states.py`): `ready` with how and when it last
worked, `needs_setup` with the missing step and who can do it, or `unknown`.

- **Who records them.** The broker records every account tool call
  (`secure_tool_broker/tool_setup_tracking.py`: success means ready, written
  at most hourly unless it changes; a setup error code means needs setup;
  other failures change nothing) and every setup status check. The gateway
  records finished setup pages and the person's own `/link` and
  `/auth ... team_client`; a team-wide fix (the team's sign-in app or
  account) turns other people's matching gaps into `unknown`. `set_up_tools`
  records the person's missing access.
- **How TeamLead uses them.** TeamLead gets the task person's states as one
  pinned context line (`custom_agents/tool_setup_runtime.tool_setup_context`,
  only for the verified person) and uses a ready tool without checking.
- **Periodic re-check.** The broker's own loop
  (`secure_tool_broker/tool_setup_recheck.py`) re-checks ready states nobody
  used or checked for `security_layer.tool_setup_recheck_days` days (default
  30, 0 off) and sends one Inbox notice per problem. It runs in the broker
  because only the broker can check sign-ins, so it needs no new route or
  signed task.
- States are deleted with a removed person's access records.

A model re-check before every use costs a call and can decide differently from
run to run; a state trusted code keeps is cheap and deterministic, and the
periodic check covers sign-ins that expire while nobody uses them.

**One workflow for several pieces.** When a request needs two or more pieces,
or any outside step, `set_up_tools` publishes one access workflow with
TeamLead's `extra_steps` and a final work step; with one piece it just opens
the page. Each step names its expected setup (`tool_setup`: tool, kind, owner,
and an outside step's `step_id`; checked against the catalog when the plan is
published) or tool access. Trusted code, not the model, keeps the steps
current, the same way access requests are linked
(`custom_agents/access_workflow_setups.py`):

- Each page the run opens is linked to the one step that expects it (open, or
  pending with its prerequisites done, because nobody marks a setup step
  running before its page opens), in the task's workflow or one whose task
  carries the same verified signed task context.
- Each result updates that step. Completed marks a pure setup step done once
  all its pages are finished; expired keeps it waiting and fails it after the
  third page; declined blocks it like a denial. The person's own `/link`
  answers the waiting page's id, so it marks the step too.
- The run completes an access step once its access is approved.
- A page that matches no single step stays on the workflow as not on a step
  and is logged (`event=workflow_setup_unassigned`).

One plan the person can follow beats several unconnected pages in chat, and the
model does not have to judge when a plan is worth it or remember to update it.
A single piece stays a one-click page because a workflow would only add a
click. The links live on the workflow record in the manager's storage, where
the verified task context is known; the page reads them and polls, so it
updates by itself.

### Installed CLIs

A service's command-line tool can be added as `cli:<name>` through an access
proposal (`ProposedCliToolV1`, `custom_messages/contracts/cli_tool_v1.py`),
with no code per CLI. TeamLead prefers it to a web API tool because the model
sees one compact provider, each command has its own level, and the broker keeps
the token hidden.

- **Provenance.** The program lives in the team workspace
  (`team/tools/<name>/<file>`), where agents put it (InternalImprovementAgent
  builds or downloads it). The administrator's approval pins its SHA-256. A
  proposal may also name an https download, which the gateway fetches at apply
  time (size and time limited, https-only redirects, one member of a
  `.tar.gz`/`.zip`), checks and writes without following links
  (`access_control/cli_binaries.py`, `access_control/proposal_clis.py`); any
  failure removes what was written, so nothing is applied. An update is a new
  proposal.
- **Catalog.** An instance file carries the entry and its `installed_cli` data;
  the loader accepts `cli:*` instance entries only with it, builds the command
  table (`cli_tables.policy_from_data`), and makes the name a `secure_cli`
  provider with one `exec` action (a name that is already a provider is
  refused). Levels come from the table; unlisted commands need full. Providers
  are resolved from the current catalog in the broker
  (`secure_tools.registry.set_provider_resolver`) and in the agent's
  `secure_cli` (`secure_tools/installed_clis.py`).
- **Execution** (`secure_tool_broker/installed_clis.py`). The argument list
  passes `cli_passthrough.check_installed_command_line` (the gate decided the
  level of the same list). The workspace program is copied with `safe_open`
  into a new private run folder and the copy's digest is checked on every run,
  since the workspace is agent-writable; a mismatch or a link is refused and
  logged (`event=installed_cli_program_refused` with both digests). The
  credential of the link the gate chose (`ToolDefinitionV1.link_owners`:
  person, person then team, or team) goes into an environment variable or a
  file in the run folder (`InjectionPlacement.FILE`), never argv, and its
  values are removed from the capped output.
- **Sandbox.** The copy always runs inside the same Landlock launcher agents'
  code uses (`custom_tools.code_sandbox.prepare_sandboxed_command`, with the
  run folder as its only writable folder). It reads only system folders and
  its run folder, so the broker's files and every other credential stay
  unreadable. This is required because the broker holds every person's
  credentials, and a pinned digest proves which file runs, not that it is
  harmless. The network is open because the program must reach its service;
  Landlock limits TCP by port, not host, so hosts are shown for review but not
  enforced. Without Landlock the run is refused
  (`event=installed_cli_sandbox_unavailable`).

### Credential forms and sign-in tools

A credential collection form ends its task with a fixed answer only when
storing or deleting the credential was the whole request; a request that sets
`continue_task` (collect, then link, then use) resumes through the model.

**GitHub and Google Workspace** (`cli:github`, `cli:google`, account mode
`personal_login`) use each person's own sign-in instead of links and have no
team account. `/link cli:github` and `/link cli:google` store it in the
credential store as that person's, and the broker runs `gh` or `gog` only for a
request that works for that person. Each `gog` command runs in a new private
folder under the person's broker credentials folder with an allowlisted
environment, deleted afterwards (`secure_tool_broker/google_workspace.py`),
because gog keeps its accounts and keyring in its home folder and a shared
home would let one person's request use another's Google account. People sign
in to Google through the team's OAuth client, which an administrator stores
with `/auth google team_client`, unless they pasted their own on the sign-in
page.

**X** (`cli:x`, account mode `linked_credential`) uses each person's own
sign-in first and the team's account link as a fallback while the team shares
X. Sharing starts private (`default_sharing: private`) because X posts as the
account.

- `/link cli:x` runs X's OAuth 2.0 authorization code flow with PKCE
  (`secure_tool_broker/x_sign_in.py`) through the team's X app, which an
  administrator stores with `/auth x team_client` in the team's part of the
  credential store. The browser returns to the broker's `/auth/x/callback`
  page (under the broker's existing public `/auth/` path, so proxies need no
  new route) or, with a `localhost` callback, the person pastes the address.
  The broker stores the result under the person in the same shape the X
  executor reads, and saves every refresh.
- `/link cli:x team` links the team's shared account (`oauth_json`). Personal
  X links are not created or used, because a person's own X account is their
  sign-in.
- The access gate hands the X executor only the team's link, and only while
  the team shares X. The executor uses the person's sign-in when there is one,
  then the team's link, and otherwise fails with `x_account_missing`.

A person's posts go out under their own account without anyone pasting a
hand-made token bundle, while a team that wants one shared account keeps it.
Neither a sign-in nor a link grants access; grants still decide every call.

**No tool token comes from environment variables.** Startup refuses the
removed ones (`runtime_settings/security.py`), including a Google
service-account key file (`GOOGLE_APPLICATION_CREDENTIALS`), because an
environment credential would be one team-wide identity for every person.

### Schedule changes

Schedule changes go through one path (`custom_agents/system_schedule_changes.py`,
built on `custom_agents/schedule_changes.py`).

- **A person's own schedule** changes at once: the access hook decided the
  tool call (`fn:schedules`) and the tool checks the schedule belongs to the
  person in the signed task context (`access_control/ownership.py`). The
  schedule store needs no permit for it.
- **A schedule the runtime manages** (memory review, heartbeat, Objective work
  sessions) never changes on an agent's say-so, because the store requires an
  exact permit for its control state (`custom_agents/schedule_management.py`).
  The tool stages the exact change (`system_schedule` on the platform tool
  `fn:system_schedules`) and a person approves it in the Inbox (its agreement
  with authorization off). `apply_change` rebuilds the change from the stored
  request, checks that the schedule and the change are unchanged, and issues
  the permit in the approval's mode.
- **Objective tools** issue `held_access` permits for the Objective's own
  work-session schedule.
- **Gateway commands** on schedules the runtime manages need a person who
  could approve such a change. The manager decides this with the person and
  roles from the command's gateway-signed context
  (`manager_control_runtime._may_change_system_schedules`, the same rule as the
  gateway's `PersonChangeCheck`, which the gateway keeps for showing
  controls). It awaits this storage and Cedar lookup on its bounded
  `TaskOperationRuntime` lane before checking the record digest or changing
  the schedule, so a slow lookup leaves the event loop responsive; a cancelled
  command joins the lookup and stops before mutation.

Every gateway-to-manager command (schedules, memory, topics, task and objective
controls, person standing) names its person only through that signed context
(`manager_control_runtime.verified_command_person`). The commands carry no
person, roles or authority flag, because fields in a command body are not
signed.

**Scheduled workers** get their tools like any agent type:
`prepare_scheduled_job_access` lists what the worker type and the owner lack,
`authorize_scheduled_job_access` hands on what the team lead may give, and each
call at run time is decided per call.

### Lifetimes and credential storage

Authority must last as long as the work. A grant tied to a creating Task ends
with that Task, so recurring jobs, service reconciliation and later SSH
interaction need authority that remains valid when each effect occurs.

Credentials stay outside agent processes and ordinary gateway storage. The
secure broker owns the credential catalog, secret-free references, provider
sessions, hydration, use and output redaction. Sealbox or another configured
provider stores secret material. Agents receive structured results, not raw
passwords, tokens, keys or credential paths. The broker derives identity from
the signed task context and chooses the credential from the tool's account link
on every use.

Paging bookmarks such as `nextPageToken` stay visible: they only ask an API for
its next page and are useless without the caller's sign-in, and hiding them
would stop agents from reading past the first page. Only the final `token` word
in a bookmark name is exempt from secret-field redaction. Other secret markers,
such as `password` in `passwordPageToken`, still hide the field, and bookmark
values still pass the credential-shape check.

## Deterministic execution and managed processes

Agent-produced code crosses a promotion boundary before it can become durable
automation:

1. Source bytes become a content-addressed artifact.
2. A trusted profile compiles or checks the source in a bounded environment.
3. A build attestation binds the source, toolchain, profile, and output.
4. Promotion creates an immutable revision.
5. Jobs and services refer to that revision instead of mutable source files.

Python validation runs in a separate resource-limited compiler process; Rust
compilation runs in an OpenShell sandbox. Execution profiles define runtime,
resource and placement policy independently of source code.

- **Jobs** (one-shot, interval and cron) use database time, deterministic
  occurrence identifiers, leases and fencing tokens. A restarted controller can
  recover or adopt work, and an earlier controller cannot commit a stale
  result. Adapters dispatch finite runs to local processes, OpenShell or
  Kubernetes.
- **Managed services** run long-running workloads as desired-state Kubernetes
  services and currently require Kubernetes. They receive verified promoted
  artifacts and can use a separate writable volume for application data. An
  agent-owned background process inside an ephemeral container is not the
  durability mechanism.
- **Managed MCP servers** use the service lifecycle. A healthy service creates
  only a pending connection, which an administrator installs like any MCP
  server (in Settings or through an access proposal); every call to its
  operations is still checked.
- **SSH** uses a brokered OpenSSH PTY against an operator-pinned host, user,
  host key and key material (`credential_binding_id`). Agents interact through
  a session id and receive bounded output. Local PTY state is lost when the
  broker restarts; an operator-configured remote `tmux` session can support
  explicit reattachment.

### Access for jobs, services and SSH

Jobs and SSH sessions are `write` operations of `fn:execution_control`, decided
by the agent's access decision hook. Each activation (`ActivationRecordV2`)
records who held access (agent, verified person and roles, catalog operation)
and how it was approved (`ChangeApprovalRecordV1`).

Before every run, service reconcile and SSH call,
`runtime_hosting/authorization.py` rebuilds the exact resource from trusted
records and decides again with the access service. Held access must still be
held, so revoking a grant stops the next run. Jobs run unattended, so only
access that outlives the creating task counts for them.

Managed services and typing into an SSH session are exact changes a person
approves (`managed_execution_service`, `execution_ssh_interaction`, staged in
`custom_agents/execution_changes.py` and applied with `apply_change`); their
approval record is the authority. Typing approvals live in their own
insert-once table because the broker rewrites the session record on every
lease renewal. The controller builds its access service like the broker does,
from the manager storage and the Cedar evaluator, and refuses to start while
records saved before this access model remain
(`assistant_cli.py execution-retire-old-records`).

## Gateway, tools, and models

The gateway supports the local CLI, Web Chat, channel plugins, scheduled
messages, proactive notifications, and generated sites. It owns canonical user
resolution, session continuity, attachment admission, clarification routing,
quiet hours, deferred delivery, and receipts. An agent response and a delivered
user message are separate facts.

### Messages and steering

- The runtime sets message sender names from the sending agent. New delegated
  tasks use `delegate_task`; `send_message` handles other structured messages.
- **Delegated results** must match the saved parent and the worker holding the
  task. Active custody, then the completed-task archive, verifies admitted work
  even when its claim announcement was lost; claims cannot override those
  records. A rejection before admission publishes its claim before the error;
  an error arriving before its claim is recorded keeps broker custody for
  retry.
- **Questions** and escalation choices stay in the lead's prompt. A lead's
  question to a person uses the preserved gateway reply route.
- **Steering** that names a task applies only to that task. Accepted guidance
  and duplicate keys are saved in the task's history, and guidance survives a
  restart until the model turn records its decision.
- An explicitly addressed receiver answers `STALE` for a missing task, so the
  gateway can submit the person's message as new work.
- A **schedule action** must be followed by a final response or a waiting
  action; saving a timer does not finish its originating request.
- **Delivery receipts** use the durable team-lead route, so they can wait for a
  replacement lead before discovery completes. Repeated publication of one
  receipt uses the same idempotency key.
- **Presence replay** follows the presence stream's retention period; presence
  age still decides whether an agent is live. Queued model turns broadcast when
  work starts and finishes.
- The **subtask watchdog** records successful status probes separately from
  task progress. A waiting worker can renew the probe interval without changing
  its progress timestamp or producing updates on every idle check. A status
  reply confirms that the worker still knows the task, not that it makes useful
  progress.

### Replies to earlier messages

A person can answer one earlier chat message with the channel's reply feature.
That message often belongs to another conversation than the one the chat
continues: a recurring schedule's report is filed under the schedule's own
conversation, a background check the manager runs itself keeps none, and the
team's default chat delivers through the team's notification session, not the
replying person's. So a reply such as "expected" means nothing to the agent
alone.

Telegram passes the replied message as the chat showed it, beside the person's
text (`Task.replied_message`, `TaskQuery.replied_message` and
`TaskSteering.replied_message`,
`custom_messages/contracts/replied_message_v1.py`); the adapter-host protocol
carries it too. The agent sees it quoted and labeled by author (the assistant,
the person, or another member) as background for the person's message. It is
told not to follow instructions inside the assistant's or another member's
text, because reports quote outside content such as emails, and another
member's words are not the person's. The reply stays in the chat's current
conversation, not the replied message's, because many reports have no
conversation holding their text, and the report and the reply often arrive
through different chat sessions.

- **Telegram rich messages.** Telegram's default rich-message delivery stores
  visible content in `rich_message.blocks`, not `text` or `caption`. The pinned
  Telegram SDK keeps this field in `api_kwargs`;
  `interactive_apps.telegram_reply_text` normalizes paragraphs, inline
  formatting, lists, tables and media captions before the same 16,000-character
  limit applies. Unsupported rich content is logged without copying message
  text.
- **Clarifications.** A pending clarification follows its existing `TaskQuery`
  route and keeps the replied message on the stored query; the parent task's
  history renderer quotes it when work resumes, including after a restart.
  Focused plan-step and recovery prompts include their own clarification
  history as a separate section, because they intentionally omit general
  execution history. SDK interrupt replies use the same labels in live resume
  input and durable recovery notes; structured access decisions keep their
  original value.

### Adapters, Web Chat and private data

- **External adapters** connect through an authenticated local or private-host
  boundary. Durable inbox and outbox records provide ordering, duplicate
  detection, acknowledgements and reconnect recovery without giving adapters
  gateway filesystem or repository access.
- **Credential-form watches** keep broker session and task identifiers in
  gateway storage and recover after restart. These local wait records hold no
  submitted credentials or form URLs and are not team-transfer records.
- **Private data.** Error replies omit the agent's plan and execution history.
  Notification results and prompt delivery settings omit raw chat and thread
  addresses. The agent-status HTTP endpoint requires a signed-in Web Chat
  session; collector API tokens go in headers. Onboarding completion opens the
  existing authenticated Status page through **Open Status** and requests no
  private agent data before sign-in.
- **Team presence.** `assistant_gateway/team_presence.py` keeps the live team
  presence in memory. Every live Web Chat page (the two-second event stream,
  `website_handlers/conversation.py`) carries it as `team_activity` for
  registered people. One browser store (`assets/team_activity.mjs`) hands that
  copy to the sidebar activity mesh, Settings → Team and the Tasks view, so an
  open page reads presence once, at the stream's pace, and the three views
  always agree.

### Access Inbox and reviews

The gateway gives people the places to decide; it never decides access itself.
The access Inbox (`assistant_gateway/access_inbox.py`) lists access and change
requests with notices and records decisions through the access service as the
signed-in person. It announces each waiting request once, in a chat where the
person can decide it. The delivery event carries the `access_request_id` and the
review choices; a staged change is announced with the one-line summary its
staging service writes from the plan.

- **Telegram and Discord** show the choices as buttons (button contract in
  `assistant_gateway/application/access_reviews.py`, decisions in
  `assistant_gateway/application/access_review_service.py`). A press carries
  only the request id and the choice, and the gateway decides with
  `GatewayAccessInbox.decide` as the person linked to the pressing chat
  account. Adapters in a separate adapter host send presses with the protocol
  command `access.review_decision`. The button holds no secret and no state,
  so it keeps working after a restart, and the person's current roles decide
  what a press may do.
- **Web Chat** shows the Inbox's review card in the conversation
  (`assets/chat_access_review.mjs`). It reads the request by id from the
  transcript row's `access_request_id` and decides through the Inbox decide
  route as the signed-in person.
- **Other channels** show the Inbox link.

Every web chat route, chat command and Settings view names its signed-in route
with `GatewayRouteContextV1` (`assistant_gateway/application/route_context.py`);
`SignedInPerson` turns that route into the person it acts for. People's
controls for MCP connections and hosted servers
(`assistant_gateway/mcp_controls.py`) and for installed-procedure proposals
(`assistant_gateway/installed_procedures.py`, listed, added or rejected in
Settings → Team) then check with `PersonChangeCheck` what that person may
change, from their current team roles.

- **Notices.** The Inbox pages notices on the server
  (`AccessRepository.notice_page`, 20 per page, unread only by default) and
  marks them read for one person by id or all at once
  (`POST /api/web-chat/access-inbox/notices/read`; only notices that person can
  see are marked).
- **Counts.** The Inbox tab shows one count, requests the person can decide
  plus unread notices, from `GET /api/web-chat/access-inbox/counts`. The
  browser reads it every 30 seconds while visible, when the page becomes
  visible and shortly after an agent's chat message (requests and stuck-work
  reports arrive with one), and takes the counts every Inbox read and change
  already returns. The Settings cog shows that count plus the Knowledge page's
  proposal count ("Knowledge page" below). Both counts use one browser helper
  (`settings_support.mjs` `createAttentionCount`), so every badge follows the
  same rule (`ui/DESIGN.md`, "Attention counts").
- **Stuck-work notices** (`custom_agents.stuck_work`) go to an owner and
  administrators as one notice when they read the same words; otherwise an
  owner who is an administrator gets only the administrators' notice, so one
  problem never fills an Inbox twice.

### Tools and MCP

Agent definitions are declarative and list the tools a type works with as tool
catalog ids or tool set ids (`default_tools`, `custom_agents/default_tools.py`);
the catalog maps them to reviewed callables (`access_catalog/tool_functions.py`).
YAML and model output cannot register arbitrary imports, commands, endpoints or
credentials as trusted tools. Default tools grant nothing.

- **Authorization on.** An agent starts with its role's standard tools. A
  `BeforeModelCallEvent` hook (`custom_agents/tool_sync.py`) adds and removes
  the functions of the catalog tools it holds for the current task's person,
  so a new grant is usable on the next model call without a restart; the
  defaults only tell TeamLead what to give a new type. The same hook loads the
  tools of each MCP server the agent holds (`mcp:<name>`) through the broker as
  dynamic tools and removes them when the access ends
  (`custom_agents/mcp_server_tools.py`).
- **Authorization off.** The role's standard tools and the default tools are
  the agent's tools, and a smaller hook loads the MCP servers meant for its
  type the same way.
- **MCP loading.** No MCP server loads at startup in either mode, so a server
  that does not answer never stops an agent from starting. The loader retries
  it within `mcp.load_retry_window_minutes`, then tells administrators, and
  does not retry a broker refusal until access or the server changes. There are
  no fixed tool groups, capability attachments or restart-to-attach. External
  and hosted MCP tools enter through the same installed registry and the
  broker's per-call access check.
- **Skills** provide reusable instructions without loading every procedure into
  the base prompt. A reviewed managed-skill change commits an immutable local
  receipt before TeamArchitect sends an authority-free team reload hint.
  Running specialists verify that receipt, refresh their own SDK cache, and
  republish presence. Heartbeat fingerprint checks repair missed live hints
  without a process restart.

#### Tools on request

In both modes the catalog's `on_request` groups (`functions.yaml`) are left out
until the agent loads them for its task with `use_tools`
(`custom_agents/tool_loading.py`). Why: TeamLead's standard tools alone were 140
functions, OpenAI models refuse more than 128 tools in one request, and the
definitions cost about 30,000 tokens per call; a one-line-per-group list in the
prompt costs under 1,000.

- A loaded group leaves after 8 unused model calls (counted, not timed, because
  tasks wait days for approvals) and when the task changes. Objective work
  sessions keep the objectives tools for their checkpoint pass. A call to a
  function not loaded loads its group and is still decided by the access hook.
- Each model's `max_tools_per_request` (the `model_capabilities` maps, 128 by
  default) caps what a request carries. Loads unload the least recently used
  groups first; definition defaults and tools required by an objective work
  session cannot be evicted. An agent whose always-shown tools alone are over
  the cap is reported to administrators. The cap follows the selected primary,
  fallback or personal model; override edits refresh between turns without
  replacing an active fallback.
- Capacity checks, eviction and loading share one per-agent lock because
  Strands can run tool calls concurrently.
- Workers can load tools TeamLead may automatically hand on, using
  `AccessService.may_hand_on_for_task`, which shares the actual delegation
  checks but writes nothing. The call's access decision still rechecks and
  creates the task grant. A successful load reports only functions the agent's
  definition and operator settings offer.

**Raising the cap.** TeamLead asks for a higher cap with the runtime setting
`model.max_tools_per_request` (`custom_agents/model_tool_limit.py`), for its own
model or, with `target_agent_type`, a worker type's (workers at their cap
report it in their result, since only TeamLead stages settings). The plan names
the exact provider and model. The cap belongs to the model and changes every
agent on it, so it is not `user_authorized`: administrators approve it in the
Inbox, like the team's model endpoint, and it is never the model-choice fast
path. It only raises, because lowering would take tools from every agent on
that model, and it refuses a person's own model, whose cap and key are not the
team's. One request with that many tools goes to the model at staging and again
before writing, because sending that many is the only reliable way to learn a
provider's limit.

### Model providers and context

Model providers sit behind factories and adapters, so task and message
contracts do not depend on one provider. Context budgeting, compaction and
artifact offloading happen in the runtime instead of relying on provider chat
history as the canonical record.

Two places move a tool result out of the transcript: the per-call limit
(`custom_agents/tool_result_offloader.py`) and the inner-loop context manager
(`custom_agents/filtered_tool_result_context.py`). Both save it in one format,
a folder with `manifest.json` naming its agent, team and task, because
`state/tool_results` is outside every agent folder and
`inspect_tool_result_artifact` is the only way an agent reads it back.

Projected context pressure shrinks only results the model has already read. A
result it has not read yet (a staged change id, for example) is shrunk only
after the provider actually rejects the request, and then after older ones. A
result counts as read only after Strands returns a successful model response
that its hooks accept. Failed, cancelled or retried calls leave the attempted
results unread, including a successful response discarded by a retry hook.

### Model provider keys

Model keys belong to an endpoint *address* (scheme, host and port,
`model_providers/endpoints.py`), never to a provider name. Well-known providers
have one fixed address each; the OpenAI-compatible endpoint
(`llm.litellm_api_base`, which can be switched) and an OpenAI-compatible
embedding service have their configured one. A key entered for one address is
never sent to another.

- **Model metadata at startup.** `custom_agents/litellm_runtime.py` selects the
  catalog bundled with LiteLLM before importing it. `strands_main` warms it on
  the main thread before creating agents; the reasoning adapter and capability
  checker use the same initialization path. This removes an import-time pricing
  download whose retry thread can race LiteLLM's logging imports under a network
  outage ([upstream import issue](https://github.com/BerriAI/litellm/issues/46142)).
  Built-in capabilities and prices update with dependency releases. Configured
  capabilities and explicit provider metadata requests remain separate sources.
- **Where keys live.** Administrators save the team's keys with `/auth model`,
  a broker page (`secure_tool_broker/model_key_page.py`) that tests each key
  with one real request (`model_providers/connection_test.py`, shared with
  onboarding's provider test) before it stores anything. The broker's
  credential store keeps one record per owner
  (`secure_tool_broker/model_keys.py`, `model.endpoint_keys_json`). Only
  processes that run agents (manager and agent processes, by their caller
  tokens) can fetch a key (`/v1/model-keys/lookup`); the gateway reads a status
  that never contains one.
- **Sealbox reads.** A Sealbox credential-state read maps only a missing secret
  or `sealbox_tenant_not_provisioned` to an absent record. Reads never create
  tenants; the existing authorized write provisions the first tenant. This lets
  read-modify-write save an owner's first key and lets an empty personal store
  reach the team's key when permitted. Existing tenant files must hold a valid
  matching RSA pair. Missing material, invalid pairs, authentication errors and
  unexpected storage failures remain errors, even with file fallback enabled,
  so damaged tenant state is never silently replaced.
- **How processes use them.** `custom_agents/model_keys.py` is the only place
  the runtime gets a model key. An environment variable (`LITELLM_API_KEY`,
  `EMBEDDING_API_KEY`, and so on, with their `_FILE` forms) overrides the
  stored key; the override is logged, reported to the broker by name and shown
  on `/auth model` and in `/auth status`. Otherwise the stored key is fetched,
  cached for a minute, kept when the broker briefly does not answer, and
  fetched again at once after a rejection. Each model call looks up its
  endpoint's key first (`bind_model_keys`), so model objects hold no key:
  LiteLLM reads it per call (`CallKeyedClientArgs`) and the OpenAI client calls
  a key function.
- **Key errors.** Typed `ModelKeyError` failures take precedence over provider
  status codes and text, including through SDK wrappers, and never enter
  provider overload, connectivity, timeout, key-rejection or failover
  classification. Exception traversal also recognizes Strands'
  `original_exception` field. The shared base type lives in the
  dependency-free `model_key_errors` module because model keys already import
  provider-error helpers.
- **Why.** With keys in the broker rather than `team.env`, a new key works on
  the next model call without a container restart or a fix on the host, and a
  switched endpoint never receives the old endpoint's key. Environment
  variables stay as an operator override, but never silently.
- **Well-known providers.** OpenAI, Anthropic, Gemini and xAI (provider `xai`,
  an API key; `xai-oauth` stays a sign-in) have one fixed address each, passed
  to their client explicitly so `OPENAI_BASE_URL` or `ANTHROPIC_BASE_URL`
  cannot send a key elsewhere. Gemini reads the call's key from
  `CallKeyedClientArgs` (Strands builds a client per call), Anthropic from a
  client subclass whose `X-Api-Key` header is the call's key, and the OpenAI
  clients from a key function. Bedrock keeps the AWS credential chain.
  `anthropic` is a runtime dependency because Strands' Anthropic adapter needs
  it; without it `provider: anthropic` cannot start.
- **Personal keys.** A person may save their own key for an endpoint the team
  uses (`/auth model me`, `PersonalModelKeyFlow`), stored in their own
  credential scope and deleted when they are removed. A model call sends its
  task's signed context with the lookup (`task_key_context`); the broker
  verifies it like a tool call (`BrokerAccessGate.verified_person_id`) and
  returns that person's key, else the team's. A rejected personal key stops
  the task and tells the person (no fallback, no administrator notice, not
  part of the team's key status), and its rate limits never trigger the agent
  type's failover (`last_call_used_personal_key`).
- **Team keys per person.** Whether a person's work may use the team's model
  keys is an administrator's decision
  (`access_control.people.team_model_keys_refusal`), stored with the person's
  other shared settings in the access store (`PersonSettingsV1`,
  `access_control.person_settings`) because the gateway, the broker and the
  agents all read it. New people get the administrator's
  `--team-keys`/`--own-keys` choice or the team's model-key sharing
  (`AccountSharingV1` subject `model_keys`, shared unless an administrator ran
  `/accounts private model`). That sharing record replaces the removed setting
  `gateway.users.new_people_use_team_model_keys`, so it can change from chat
  without a restart. People added before the switch existed have no record and
  keep the team's keys, so nothing changes for them without a decision. The
  broker enforces it (`lookup_handler` answers `team_keys_allowed: false`), and
  the runtime asks the broker before using any environment variable for a
  person's task, because an environment key is the team's too. Embedding
  lookups carry `purpose: embedding` and keep the team's key: memory search is
  the team's service.
- **A person's own model.** On `/auth model me` a person may save their own
  model (`PersonModelProfileV1`: any provider and address, tested first, with
  the address form that answered). Every agent's work for them uses it,
  including agent types pinned to a model (`custom_agents.person_model_runtime`).
  The agent's own model fields never change, because failover, the settings
  apply, the reasoning-summary repair and the status views read them as the
  team's. Instead a turn selects a per-person model bundle (model, context
  window, capabilities) once its task is known and before context assembly
  (`select_turn_model`, a turn effect), shows it in the turn settings
  (`turn_settings_with_person_model`), and gives the team's model back when
  the execution context is cleared. A `BeforeModelCallEvent` hook
  (`PersonModelHook`, order -50) sets the right model before every call, which
  covers resumed interrupted tasks, fresh Strands agents and rebuilds after
  failover or live settings changes. Failover skips while a person's model is
  active. Opt-in LLM turn summaries and compaction of a person's conversation
  use their model (`summaries_with_person_model`). Who a task works for comes
  only from its verified identity.
- **Onboarding and voice.** Web onboarding tests the model key, then hands it
  (with the embedding and xAI voice keys) to the broker through the same
  private hand-over file as the Exa and Brave keys
  (`assistant_gateway/onboarding_account_keys.py`, `OnboardingModelKeyV1`);
  the broker saves them as the team's keys when the person who onboarded is an
  administrator. They are never written to `team.env`, where they would
  override every key saved later, and are not listed as missing environment
  secrets. Grok voice looks the xAI key up per request; the gateway may fetch
  that one key and no other (`GATEWAY_KEY_ADDRESSES`). A separate adapter host
  has no broker token, so its voice reads `XAI_API_KEY` only.
- **Switching the endpoint.** `llm.litellm_api_base` is a team-wide runtime
  setting (`custom_agents/endpoint_switch.py`), never a model choice. It stays
  out of `RuntimeLLMProfile` and `LLM_OVERRIDES.yaml`, which model-choice plans
  write without administrator review.
  - Staging is refused while the new address has no key or while the
    operator's `LITELLM_API_BASE` would override it (the launcher's override
    list). Staging tests the address with and without `/v1`
    (`model_providers.connection_test.check_endpoint_address`) and stages the
    form that answered, because people name an endpoint by its host.
  - An administrator approves it in the Inbox, not the person who asked,
    because it moves the whole team's model traffic. The Inbox text names the
    host and whose key is used. Applying sends one test request first and
    saves nothing when it fails.
  - It applies without a restart: every agent compares the saved endpoint with
    its own at the start of each turn and rebuilds its model when it changed
    (`custom_agents.endpoint_refresh`). This reaches startup agents such as
    MemReviewAgent too; a manager restart would restart only TeamLead's
    process.
  - TeamLead sees the team's saved endpoints (addresses and the form that
    answered their test, never keys) in `get_runtime_configuration_context` →
    `model_endpoints`, served by the broker's `MODEL_KEY_ENDPOINTS_ROUTE`, so
    it can tell that an endpoint such as Bifrost is already set up instead of
    asking the person for its address.
  - Live tests wait as long as agents' model requests
    (`llm.request_timeout_seconds`): a 20-second wait refuses a working
    endpoint whose reasoning model stays silent longer.
- **Model settings reach other agents at their next turn.** Every agent reads
  `LLM_OVERRIDES.yaml` again at the start of each turn
  (`model_failover_runtime.refresh_runtime_llm_selection`) and rebuilds its
  model and compactor when its profile changed. So a TeamLead change to another
  agent type's model settings (`current_agent.llm.*` with `target_agent_type`)
  or to the workspace default (`workspace_default.llm.*`) needs no restart,
  which would reach only TeamLead's process anyway. A change explicitly
  targeting TeamLead's own type switches it at once; a default-only change
  reaches it at its next turn too. The plan's
  summary, its Inbox effect lines and the apply result's `running_agents` say
  that running agents switch at their next turn, because an idle agent's
  status shows its previous values until then
  (`runtime_setting_registry.next_turn_targets`). Instance-config model
  settings (`llm.*`, and `current_agent.llm.*` settings stored there, such as
  `temperature` and `max_output_tokens`) are read only at startup and still
  need the reported restart.
- **The `/auth model` pages.** One form per page whose Endpoint list fills in
  the fields; results show just above the form they came from (red for a
  failure, with what it most likely means). Pages last 24 hours
  (`MODEL_KEY_PAGE_SECONDS`, a flow's `page_seconds`) and their Close buttons
  end them. Because a page lives that long, each submission re-checks the
  person's current standing and roles in the gateway's copy of the team's
  people, not only the roles signed into the page.
- **Repair.** A provider rejecting a key (HTTP 401, an authentication error)
  ends the turn with a fixed message saying whose key it was and who fixes it
  (`ModelKeyRejectedError`), never fails over to another model, tells the
  broker, and sends administrators one Inbox notice per endpoint and day
  (`model_key_problem`). `/auth model` is a gateway command, so the repair
  works even when no agent can reach its model.

### Status page

Administrators' Status page (`/webchat?view=status`,
`assistant_gateway/status_page.py`) shows current findings
(`StatusFindingV1`, `custom_messages/contracts/status_v1.py`), not events: a
finding disappears once its condition is gone. Without it, conditions such as
an environment variable silently overriding a saved setting or key, a rejected
model key, or an MCP server that stopped answering would be only in logs or
known to one process. The gateway gathers findings on each read from:

- the launcher's list of environment overrides (names only,
  `InstanceLayout.environment_overrides_path`, written by
  `docker/settings_environment.py`);
- the broker's model key status and the configured endpoints;
- every agent's own findings in its status heartbeat
  (`AgentStatus.status_findings`, `custom_agents/status_findings.py`): MCP
  servers that do not load, degraded memory search, skill refresh errors,
  schedules for administrators, receive loops that stopped;
- whether TeamLead reports;
- the gateway's own configuration and receive loops.

A source that does not answer is listed and shown as a problem, because silence
must not look like health. Every five minutes the gateway sends administrators
one Inbox notice per new set of problems (`status_problems`), linking to the
page.

### Knowledge page

People read and write the knowledge base from Web Chat
(`/webchat?view=knowledge`, `knowledge_page.mjs`). Team and Personal open their
ordinary `home` article as an editable wiki landing page. Home is a page
convention, not a separate setting or stored navigation tree, so normal
ownership, revision checks, content review and transfer behavior apply. A
missing Home offers explicit creation and browsing; reading never creates it.
**All pages** reveals the searchable directory with a Current pages / Archived
pages filter. A page shows its links in, open notes and history, an editor,
revert and archive. **My proposals** lists the person's own pending team
contributions, which they publish or withdraw.

#### Proposal counts

`GET /api/web-chat/knowledge/counts` answers only how many proposals wait for
the person (`count_contributions`, a count query in the store). It feeds the
badges on **My proposals**, Settings' **Knowledge** link and the cog without
loading the list. On the chat page it asks in the open conversation and skips
the read when none is open, so counting never creates a conversation.

The browser discards older count replies when a list supplies a newer total.
After publishing or withdrawing a proposal, the refreshed list supplies the
count; if navigation discards that list or the list read fails, a fresh count
read updates the badge without changing the viewer. Sign-out detaches pending
reads so the next person can read immediately, and replies from an earlier
sign-in cannot start these reads in the next person's session.

#### Manager commands and ownership

The store lives in the manager's database, so the gateway's routes
(`website_handlers/knowledge.py`) send one `ManagerKnowledgeCommand` per
request through `GatewayManagerCommandService`, like Settings memories. The
command names no person: the manager builds a person actor (`ActorKind.PERSON`)
from the gateway-signed context only
(`custom_agents/knowledge_command_runtime.py`, `verified_command_person`), so
`personal/` is always the signed-in person's own space.

Both sides check the same ownership rule (`access_control/knowledge_access.py`
over `access_control.ownership`): every team member reads and edits team
pages; personal pages and proposals are their owner's. The knowledge service
then applies its own rules: a person's team edit publishes directly without
write limits, the never-stored content rules apply, content held for review
needs an explicit confirmation, and saves carry the base revision so a
concurrent change is a conflict, never an overwrite.

Refusals and review flags carry kind, field and position, never the matched
text. Archive and revert reasons are fixed by trusted code because they enter
shared history. Pages show `[[links]]` as buttons: the manager resolves each
one with the stored link rules, following merged names
(`KnowledgeService.page_links`), and the browser replaces link syntax in
rendered text nodes, never in code. The gateway replaces editor person ids
with names before answering the browser. Like other Web Chat routes, these
have no tool catalog levels: they are people's own controls behind the
session, the write checks and the ownership rule.

#### Lists, archives and edits

The repository filters and counts pages by status, and proposals by owner and
pending status, before applying SQL pagination. Archived lists exclude merged
source pages, which are redirects rather than restorable pages. This keeps
restoration reachable and proposal totals complete beyond 100 entries.

- An omitted summary preserves it; an explicit empty summary clears it.
- Edited note proposals publish the reviewed note argument and never fall back
  to the original text.
- Archive publication copies the current page fields and changes only its
  status, with the same optimistic revision check as other writes, so archive
  proposals have no edit option.
- The browser keeps the displayed page's full space and slug for Refresh, and
  discards replies from a previous view or signed-in session.
- The directory reads at most 20 rows per request. Rows keep their full height
  inside one scrolling pane, and the shared pagination controls and visible
  range stay in a separate footer, so CSS grid cannot compress and clip the
  list while its parent reports no overflow. When a removal empties the last
  page, the browser reads the last remaining page.
- The Home reader takes the full width until the person opens All pages;
  proposals keep their list visible.

#### Addresses and browser history

`knowledge_navigation.mjs` reads and builds article addresses, including a full
space/page reference and optional revision. Browser Back/Forward and reload
read through the existing gateway routes. Creating an article adds a history
entry; editing keeps that entry. A successful revert replaces the historical
address with the current article, so reload opens the new current revision.
Addresses convey no authority, and personal always means the signed-in
person's own space. A linked team article can be read while the Personal
directory stays selected. Sign-out clears the article address and stops history
navigation from fetching until sign-in.

#### Web Chat module registry

Every Web Chat module, including nested imports such as
`knowledge_navigation.mjs`, belongs in `WEB_CHAT_MODULE_ASSETS` in
`interactive_apps/website_support.py`. That registry supplies both public asset
routes and the shell's content revision. A missing entry stops the browser's
entire module load before sign-in can start, even when the independent
background animation still runs. The operator UI HTTP test follows nested
imports and checks that each file is served with `Cache-Control: no-store`.

## Deployment and observability

`config.yaml` holds generic runtime settings; onboarded assistants use the
separate `instance_config.yaml` schema. Durable non-secret settings live in
configuration; secrets come from environment variables or file references.
Child services receive service-specific runtime manifests, not the whole
instance configuration.

### One settings file

Every process and every settings writer uses
`<instance_root>/instance_config.yaml`. Running processes load it with
`GatewayInstanceConfig.load_for_process`, which applies the deployment's
environment in memory: NATS servers and credentials, the public origin and the
website listener (`assistant_gateway.deployment_environment`). The storage
resolvers apply the Docker bundle's own PostgreSQL address
(`TEAM_BUNDLED_POSTGRES_ADDRESS`). Writers load the file as stored
(`load_saved`), change only their own fields and save, so no deployment detail
or stale in-memory value is written back.

On each in-app restart the launcher recomputes the environment variables it
derived from the saved file (`docker.settings_environment`); variables the
operator or the bundle set, and all secrets, stay unchanged. Because
processes read the saved file, not a copy made at container start, a setting
that "takes effect after a restart" does so after the in-app restart.

### Secrets for each process

Each process the launcher starts, including processes restarted at run time,
receives only the secrets it uses. `docker/secret_env_vars.yaml` names each
secret's processes, and `env_secrets.environment_for` removes the rest (with
their `<NAME>_FILE` forms) from each process's environment. Third-party
programs (`nats-server`, `openshell-gateway`, `sealbox-server`, Postgres, the
Photon sidecar) receive none of the runtime's other secrets. No secret is
passed on a command line, which every process can read.

This contains accidental disclosure: a log line, an error report or a program
one process starts cannot reveal another process's secrets. All processes still
run as one Linux user, so the code sandbox, not this filter, keeps agent code
from reading other processes.

The list is YAML so each entry can carry a plain description (`about`), from
which `scripts/export_secret_env_docs.py` generates the Docker guide's tables.
Only `env_secrets.py` reads it, through a Pydantic model that refuses unknown
keys, unknown process names and bad `file` values. The container entrypoint
does not read secret files itself: every Python program it starts reads them at
startup (`env_secrets.materialize_secret_file_envs`). The entrypoint only runs
`env_secrets.py check-container-env`, which refuses a container environment
that sets both `<NAME>` and `<NAME>_FILE`. The check covers only the
container's own environment, because the environments the launchers build may
hold a `team.env` value next to its file form on purpose.

### Supervised services and diagnosis

The Docker bundle can supervise NATS, optional PostgreSQL, the manager, startup
specialists, gateway, website, activity collector, secure broker, and execution
controller. A deployment can instead use external NATS, PostgreSQL, credential
stores, reverse proxies, and adapter hosts. Process separation and external
services require operators to supply the matching network and workload identity
controls.

Runtime logs, task history, execution digests, schedule telemetry, presence,
and anomaly records support diagnosis. The activity collector consumes curated
events into a separate read model. It does not route Tasks or treat manager
tables as its control plane.

### X and Hugging Face tools

Both Docker images include `xurl` and `hf`, with matching agent skills.
Credentialed operations use the secure tool broker and the account linked to
`cli:x` or `cli:huggingface`. X token refresh state follows the configured
credential store. Private HF artifacts use temporary broker storage and bounded
transfer results. See the [operator guide](user_docs_source/hub-cli.md) for
supported actions and setup.

### Local build infrastructure

CI runs on a labeled self-hosted Linux runner, including the WSL 2 build host
shared with desktop development. Test services use unique ownership labels and
dynamically allocated loopback ports. Installed runner hooks reclaim job state
after action post steps and recover abandoned work before admitting another
job. Runtime data is outside that ownership boundary. See
[Local CI](docs/LOCAL_CI.md).

### Moving a team to a new version

Releases may change storage without migrations, so a team moves to a new
version through a transfer file (`team_transfer`; guide:
`user_docs_source/team-transfer.md`; commands:
`assistant_cli team export | inspect | review | import | import-credentials | finish-import`).

- **The file is the only contract between versions.** Each version exports its
  own state with its own repositories and never reads an older database.
  Sections hold plain, versioned records with checked-in JSON Schemas
  (`data/schemas/team-transfer/v1`, a test fails when they drift). The manifest
  names the persisted payload models and their owning sections. Frozen
  fingerprints in `data/schemas/team-transfer/section-contracts.json` catch
  payload changes even after schema regeneration: append a new version and
  converter rather than replacing an existing fingerprint.
- **One conversion step per changed section.** A version imports its own
  section versions and the previous release's, through one step per section
  changed since (`team_transfer.sections.SECTION_UPGRADES`). This is the one
  deliberate compatibility layer. When a release ships, the steps for older
  releases are deleted and their files are refused before anything is written.
  The refusal says an older export may need several import/export steps and
  points to the guide's release-specific paths. The guide owns these paths
  because section versions alone do not identify a suitable intermediate
  release.
- **Fresh first batch, same ids.** Import creates the instance (onboarding's
  first steps, `assistant_gateway.instance_secrets`), keeps team, person,
  schedule and grant ids, and signs person schedules again with the new gateway
  key. Later batches require its receipt and the exact same signed export, skip
  imported identities and keep existing destination records. Account links
  match by team, owner and tool, since a newer link has a new ID. If a file
  holds two for one tool and owner (from an older store without the
  one-active-link index), the older is refused with the newer named. Collision
  checks include all active destination chat bindings, removed people's too,
  through a separate read-only identity query.
- **Batch locks and failures.** Noncredential batches hold an import lock and
  the supervisor's run lock; broker batches hold the import lock while the
  runtime runs. The process config loader checks the unfinished-import marker
  for every entry point. A failed later batch requires restoring the pre-batch
  backup, because writers do not share a transaction across stores.
- **One planner, nothing silently dropped.** Every record is imported, imported
  with a note, or refused with a reason, checked with this version's own
  loaders and models. Sections see what earlier sections accept, so a grant on
  a tool that is not imported is refused, not carried dead. `--dry-run` shows
  the same plan. A knowledge contribution that changes an existing page
  requires that page to be selected or already present. Link rows are checked
  against the selected or retained page's current text with the link resolver
  of normal knowledge writes, so later batches preserve destination link edits.
- **Settings carry choices only**: those that differ from a freshly created
  configuration, so new defaults apply to the rest. Model control files
  likewise carry edits relative to bootstrap's rendered values. PostgreSQL
  writers use the destination's planned DSN, which bootstrap preserves for the
  next launch. Before the first write, validation resolves every actual
  database address through the normal storage resolvers (the bundle's address
  and a separate activity database included) and refuses occupied databases.
- **Two pages, one review.** The operator's loopback page (`team review`,
  `team_transfer.review_web`) and onboarding's **Import a team export**
  (`assistant_gateway.onboarding_web.team_import`, for an export copied into
  `InstanceLayout.import_dir`) both run `team_transfer.review_session`: the
  same selection, edits, schedule confirmation, validated digest and import,
  with readable record names from `team_transfer.titles`. Each page decides
  who may call it: the loopback page by host and printed token, onboarding by
  the export's passphrase, which gives one browser tab a token sent only as a
  header. Onboarding also asks where secrets come from
  (`team_transfer.sections.DestinationChoices`); in a first import only, the
  answer wins over the carried `launch.secrets_source`.
- **Private browser review.** `team_transfer.review` compiles selections and
  editable files into the existing section planners, shared by browser and CLI.
  Schedules travel as Markdown containing YAML, validated by the schedule
  model; IDs, ownership, routes and authorization provenance stay fixed.
  Confirmation binds to the exact submitted schedule text, without rereading a
  file an external editor could change; import binds to the validated review
  digest. Settings and memories are editable individually. The browser saves in
  request order, keeps later edits dirty, discards validation responses from an
  earlier edit revision, and blocks import while saves are pending. The
  loopback aiohttp page uses shared appearance assets, a temporary header
  token, origin checks and no caching, and never renders secret values.
- **Read-only validation.** Destination snapshots use current exporters under
  `storage.read_only.read_only_repositories`. Constructors skip initialization
  and connections enforce read-only access, so review cannot add tables,
  backfill records or repair schemas. The shared memory planner rejects text
  that is blank after trimming; schedule planning computes a next run with the
  store's timezone rules. Editable YAML errors show their location without
  repeating private input. These checks run before the unfinished-import
  marker is written.
- **Credentials stay with their owners.** A passphrase-derived key
  authenticates the manifest and encrypts secrets. Operator secrets from
  `team.env` travel encrypted in the file. The broker itself encrypts and
  re-stores its stored credentials, through two routes only the `operator`
  caller (`access_control.caller_tokens`) may use; the command never holds
  those values.
- **Credentials wait for the first start.** Only the running broker stores
  credentials and onboarding runs before it, so onboarding's import leaves the
  still-encrypted credential section, its encryption subkey (never the
  passphrase) and the signed manifest in
  `InstanceLayout.team_import_credentials_path` (`state/`, 0600). This handover
  is written before the unfinished-import marker is removed; if that write
  fails, the instance stays unstartable. Private writes sync both the file and
  its directory entry.
- **Finishing the import at startup.** The launcher starts the broker, then
  waits for `team finish-import` (`build_finish_import_task`) before
  initializing agents or the gateway, so their first model or embedding lookup
  sees restored keys. The step stores the selection as
  `team import-credentials` does and notifies people and the exporting owner.
  Retryable broker record failures are retried within the startup window. When
  retries run out or the whole request fails, the handover is kept and startup
  stops; the next start skips completed credentials through the receipt.
  Terminal refusals are reported without retrying. The handover is deleted only
  when no retryable failures remain.
- **Atomic private files.** Broker file-backed credentials, imported
  password-manager sessions and template files share an atomic private writer.
  Imports publish complete files exclusively, preserving a destination sign-in
  that appeared while the broker was checking it. Normal saves atomically
  replace complete files. A failed write leaves the old contents or no
  destination, so a retry cannot mistake a truncated file for an existing
  credential. Owner-file exports exclude the writer's unpublished temporary
  files.

Transfer confidence rests mainly on integration tests with real repositories
and HTTP routes, plus a process-level CLI/broker round trip. Parser tests and
frozen previous-section fixtures cover conversions and refused edits. Browser
checks cover unsaved edits, confirmation invalidation, selection changes and
the validate/import sequence. PostgreSQL and Sealbox acceptance require those
services; file-store tests do not prove their deployment behavior.

## Visual workflow coordination

The [visual workflow guide](ACCESS_WORKFLOWS.md) describes the user and agent
contract. A workflow is a view over access requests: each step names the kinds
of staged change and the tools (with a level) it needs, checked against the
tool catalog. The graph neither executes commands nor grants access.

- **Plans.** `custom_agents.access_workflows` owns plans, progress and the link
  from each access or change request to its step.
  `storage.access_workflow_repository` persists plans with versions and events.
- **Linking a request.** The link is made when the request is stored, through
  the `RequestWatcher` port of `access_control.requests`, so the access model
  never imports workflow code. A delegated worker's request reaches its
  parent's step only through the shared, verified signed task context.
- **Deciding.** The gateway's workflow page
  (`assistant_gateway.access_workflows_page`) reads each request's current
  status from the access store and decides through the access Inbox. A denied
  request closes its step, and a new request that only that step covers is
  refused.
- **Approvals.** `request_access_workflow_approvals` submits one change request
  per staged change and pauses once; `apply_change` finds each approval by its
  change id.

## Where to look next

- [README.md](README.md) gives the product overview.
- [QUICKSTART.md](QUICKSTART.md) covers initial setup.
- [Feature guide](user_docs_source/features.md) describes user-facing behavior.
- [Configuration](user_docs_source/configuration.md) documents durable settings.
- [Docker deployment](user_docs_source/docker.md) covers containers, volumes,
  and networking.
- [Interactive app plugins](user_docs_source/interactive-app-plugins.md) covers
  external adapters.
- [Model providers](user_docs_source/model-providers.md) covers provider setup
  and provider-specific behavior.
- [Generated sites](user_docs_source/generated-sites.md),
  [skills](user_docs_source/skills.md), and
  [tools](user_docs_source/tools/index.md) cover the extension points.
- [config.example.yaml](config.example.yaml) is the generic configuration
  contract.
