# Universal Agents architecture

Universal Agents is a runtime for teams of AI agents. It can run on a
workstation, in containers, or across several hosts.

The main architectural rule is that models decide what work to attempt, while
deterministic runtime components own identity, validation, routing, persistence,
authority, credentials, delivery, retries, and recovery. Model output proposes
actions. Trusted code decides whether and how those actions take effect.

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

The execution controller is a side service. Normal user requests travel from
the gateway through NATS to the manager and specialists.

NATS and JetStream are the inter-process transport. They carry work, replies,
control messages, and presence, but they are not the business-state,
authorization, or credential database.

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

The manager's bounded context includes two pinned lines about the person the
task works for, both read only from the task's verified identity: their known
tool setup states (see "Last known states" below) and their team role
(`custom_agents/person_role_context.py`, for example "Person you work for: a
member (not a team administrator)"). The role line only shapes guidance, such
as which topics the `user-onboarding-guide` tour offers and which commands a
reply suggests; it grants nothing, because every action is decided where it
runs. Rationale: guidance that suggests administrator commands to members
leads them into refusals, and asking the person would trust a claim the
signed context already answers.

Agents communicate through typed Pydantic models serialized as JSON. The
contracts carry routing, identity, correlation, task state, and result data.
Free-form prose may be content inside a typed field, but it is not the
agent-to-agent transport contract. Contract families that need replay or
cross-runtime compatibility use explicit versions.

Each team has a subject prefix and normally contains one `TeamArchitect` plus
named specialist pools. Every durable JetStream consumer is a pull consumer: a
receiver asks for its next message when it can act on it, and NATS never picks a
receiver and pushes (decision of 2026-10-04). Workers of one type share one
queue and ask for a task only while they are free. The transport adapter
refuses a push listener on the work stream, and an architecture test fails when
production code creates a deliver group or subscribes to NATS outside the
adapter (`tests/test_runtime_architecture_boundaries.py`). Presence records let late
joiners and the gateway discover live agents without turning presence into
proof of identity or authority.

## Work, scheduling, and recovery

### Worker pools and parallel work

TeamArchitect can hand out dozens of tasks at once, one `delegate_task` action
each. They run in parallel on as many workers as the team allows:

- **Shared queues, pulled.** Each worker type has one queue
  (`<prefix>.type.<AgentType>.work`) read through one durable pull consumer. A
  worker asks for one task only while it can start it at once
  (`custom_agents/pool_intake.py`): no turn running, nothing queued locally,
  not paused, draining, winding down or waiting out a provider limit. Unstarted
  tasks stay in the queue, where any free worker takes them and where their
  number can be read. Mailboxes (an agent's direct route, the lead route, manager
  control, the gateway's reply route) are pull consumers fetched continuously
  that stop asking while the agent's deferred inbox is full
  (`custom_transport/nats_pull.py`). Before this, NATS pushed each task to a worker
  of its choice, a busy worker kept it in its private inbox, and the lead's pickup
  check failed tasks that were only waiting.
- **One message never stops a receiver.** A message the receiving release cannot
  read (malformed, or a message type or value another release added or removed)
  is logged without its content (`event=nats_message_undecodable`, with its id,
  type, delivery count and the failed field), then given back to its queue after
  10 seconds so a receiver that can read it may take it; its last allowed
  delivery ends it. An error that escapes a message's handling is logged
  (`event=nats_delivery_handler_failed`) and the message is given back the same
  way, while the receiver keeps asking. Before 0.8.1 either one ended the
  receiver's pull loop: an agent mailbox or the gateway's reply route stopped
  receiving, and on a worker queue the redelivered message stopped every worker
  in turn while the processes still looked healthy.
- **Set-aside messages reach an administrator.** Before a receiver ends a failed
  message on its last delivery, it saves the original bytes and non-reserved
  headers at `<prefix>.deadletter.raw.<copy-id>` and a separate diagnostic record
  at `<prefix>.deadletter.notice.<copy-id>`. Both use `<PREFIX>_DEAD_LETTER`,
  kept up to 3 days and at most 64 MiB, dropping oldest records first. Separate
  records keep a broker-sized original within the message limit; adding
  diagnostic headers to its body could close the receiver's connection.
  Stable copy ids include source identity, publication time and content, so
  retries deduplicate and a recreated memory stream cannot reuse an old report.
  On its scheduler pass the lead reads diagnostic records only and stores an
  Inbox notice for administrators plus a message to the configured chat when
  its person is still an active administrator (`custom_agents/dead_letter_watch.py`).
  It acknowledges reports after either destination succeeds, even if the other
  fails. Reports cover up to 200 copies, list up to 10, and wait 15 minutes after
  success before the next batch. Notices name copy ids, message types, ids,
  receivers and reasons, never content. An operator reads the retained bytes
  by the diagnostic header `UA-Dead-Letter-Raw-Subject`, using JetStream's
  last-message-by-subject lookup. Copy publications that fail are logged with
  `event=nats_dead_letter_failed` and the raw subject; the original still ends,
  so preservation is best effort when the broker cannot store the records.
  Deliberate expiry does not create a failed-message report. A receive loop that
  ends anyway is not restarted: it is logged (`event=nats_pull_loop_stopped`) and
  shown as a problem on the Status page, which notifies administrators. Before
  this change an ended message was gone, with only a log line left.
- **Claims.** A worker that admits a delegated task sends `DelegatedTaskClaimedV1`
  to the requester, which records the holder (`storage/delegated_task_repository.py`)
  and checks only that worker. Tasks still in a queue are neither probed nor timed
  out. Stops go to the holding worker; a task no worker took yet is withdrawn
  from the stream.
- **Worker pool controller** (`custom_agents/worker_pool_controller.py`, trusted
  code in the team host loop). For each worker type it reads waiting tasks, idle
  workers asking and the oldest wait from NATS, plus running, starting and
  draining workers. A type with tasks and no worker gets one at once; more start
  when tasks waited `agent.worker_pools.scale_up_wait_seconds` with no free
  worker, up to the type's pool size and the team limit. Workers above a lowered
  size leave once they hold no work; idle workers leave after their idle time.
- **Pool sizes.** `agent.worker_pools.default_max_workers_per_type` (4) applies
  until TeamArchitect sets another size, lasting or until a time, with
  `set_worker_pool_limit`; it can never exceed the operator ceilings
  `max_workers_per_type_ceiling` (16) and `max_total_workers_per_team` (32).
  Administrators get an Inbox notice of every change. One size per type covers
  delegated and scheduled work, since a worker does both.
- **Reviews.** When a pool or the team reaches `capacity_warning_ratio` (95%)
  of its limit while tasks wait, when tasks of a type keep hitting model provider
  limits, or when a worker cannot be started, TeamArchitect gets a
  `worker_pool_review_...` task (working for the maintenance owner, an
  administrator) and chooses a larger pool for a while, a larger pool for good,
  no change, or fewer workers. A problem that keeps coming back
  (`recurring_incident_count` times in `recurring_incident_window_hours`) is also
  sent to administrators' Inbox by trusted code
  (`custom_agents/worker_pool_reviews.py`).
- **Provider limits.** A worker whose model provider keeps refusing calls on a
  delegated task does not ask the person: it pauses the task, retries after a
  growing wait and takes no new work meanwhile, and no workers of its type are
  added. After `provider_limit.max_retries` the task fails with a typed
  `TaskFailureV1` (`provider_limited`), which wakes the requester
  (`custom_agents/provider_limits.py`).
- **Lost workers.** A task whose worker died is taken over by a replacement, or
  handed back to the queue after a lost lease; each move to another worker is
  counted from claims, and after `max_retries_after_worker_loss` (3) moves the
  task fails as `worker_lost`.
- **When the requester wakes.** Its model runs again when every delegated task
  ended, at once when one failed with a typed reason, or when a
  `check_back_after_minutes` set on the delegation passed
  (`custom_agents/delegation_wake.py`). Meanwhile trusted code sends the person
  counts ("9 of 24 done, 4 running, 11 waiting for a free worker") at most every
  `progress_notices.interval_minutes`.
- **One named worker.** A delegation that also names a worker
  (`target_agent_name`) goes to that worker's own mailbox
  (`<prefix>.agent.<id>.work`, the id it answers a live status check with),
  never to the shared queue; the type only checks that the worker is of that
  type. Only that worker can take it, so it is checked and timed out while it
  waits. Before 2026-10-05 a type given alongside a name won, so such a task
  went to the shared queue.
- **New types.** The team's worker types are read from the current definition
  catalog on every use (`LocalAgentTeamRuntime.agent_types`), so a specialist
  type TeamArchitect creates is listed in its routing context, accepted for
  delegation and scaled by the controller at once, with no restart.
- TeamArchitect sees all of this with `inspect_worker_pools` and
  `inspect_delegated_work` and stops a delegated task with
  `cancel_delegated_task` (tool `fn:worker_pools`).

Agents can inspect their current user's unfinished queue with `list_task_queue`
and manage another task through `control_task_queue`. Cancellation retains
history and stops attached work; resume refuses unresolved approvals, questions,
delegation and runtime review. Pause notices identify the task and affected
worker. Settings also pages finished task results across agents, including
stopped workers, ten records at a time. Runtime Controls sorts schedules by next
fire time, with undated schedules last.

Self-status, own tool-result inspection and current-task history notes are
part of every agent's standard access. Artifact inspection checks agent,
team and task ownership; older manifests without ownership metadata are rejected.

Universal Agents keeps different kinds of work separate:

- A `Task` is one executable unit of agent work with a terminal lifecycle.
- A `Plan` is manager-owned orchestration state for one strategy or attempt.
- An `Objective` is a durable user-visible outcome that can span several Tasks
  and Plans. It belongs to the person the creating task worked for, or to the
  team when that task worked for no person. All of a team's Objectives sit in
  the team lead's one durable scope, so its Objective tools check each one
  (`access_control/ownership.py`, `objective_access`): a task reaches its
  person's and the team's Objectives, and only work for no person or for an
  administrator changes the team's. A task names another task as a source of
  work, or reads its execution digest, only when both work for the same
  person (`named_task_access`).
  Reading the current task's own digest needs no signed identity; using it
  as a source of new work still requires verification.
  Creation checks the source's existing Objective and Plan, and refuses to
  reuse a Plan already owned by an Objective even for an administrator.
  `spawn_task` creates separate work with its own Plan. Applying a work-profile
  proposal checks current authority again; it grants no lasting authority.
  Missing and inaccessible
  Objective and Plan ids return the same tool result.
  Linked heartbeat items follow the Objective's reading and changing rules,
  including edits that do not supply a replacement Objective id. A shared
  heartbeat launcher keeps its own person's identity and delivery context;
  before changing conversation or run state, it refuses an item whose Objective
  that person cannot change. This prevents private Objective and Plan hydration
  into another person's work without introducing a second signing path.
  A schedule payload uses only its authorized Objective link; rejecting an
  inherited link also clears it from the copied task metadata.
- A manager schedule or `ScheduledMessage` creates agent work later. A model
  interprets that work when it runs.
- An execution job runs one immutable Python or Rust revision on a one-shot,
  interval, or cron schedule. No model reinterprets the code at run time.

For recurring specialist work, the TeamLead can authorize automatic worker startup
within current operator limits. A deterministic controller starts eligible workers;
workers claim schedules from the shared database. Durable occurrence receipts join
task admission in one transaction. Readiness checks the worker type's tools; access
is the worker type's own (given ahead of time with `authorize_scheduled_job_access`)
and every call of a run is decided for the worker type and the owner. A saved worker policy is compared only
with what a team-written agent definition declares
(`custom_agents.schedule_definition_identity`): code releases never pause a
schedule, while an edited team definition pauses it until the owner reviews the
differences and TeamLead re-saves. A schedule's saved fingerprint
(`custom_agents.schedule_execution_identity`) covers its task, owner, target and
delivery but not its run notes, which runs write themselves; paths that change a
schedule with the authority a re-save needs (the owner's own edit, an approved
change, the gateway moving a maintenance schedule to the person's current chat)
move the saved policy along (`follow_authorized_change`). See
[Scheduled workers](user_docs_source/scheduled-workers.md).

Whether a worker start's process is gone is proven, never assumed from a timeout
(`runtime_hosting.process_run`). The supervisor holds an exclusive lock on
`runtime/process_run.lock` and passes the locked file to every agent process, so
the lock lasts while any process of that run lives; a new supervisor starts only
once it gets the lock and gives the run a new id that every start records. A
start from an earlier run is therefore settled at once after a restart or a
replaced container, and new runs never attach to a start whose process cannot be
checked. Before this, a recreated container left such a start "unknown" for good
and it blocked every later run of its worker type (2026-10-03).

Stuck schedules are reported by trusted code, not a model
(`custom_agents.stuck_work`, `custom_agents.stuck_schedules`): on every pass the
team lead's scheduler loop finds schedules that are overdue and not picked up,
and after `agent.schedule_workers.review_interval_seconds` tells the owner (Inbox
notice and one chat message) and, when only they can fix it, administrators;
recovery is reported the same way, and each incident once
(`storage/stuck_work_repository.py`). The Schedules card shows **Not running**
with the same explanation. A missed one-time run is turned off and its owner
told. This replaced a self-addressed TeamLead review task that carried no person
and no chat route, so its findings reached nobody.

Stuck tasks are reported the same way (`custom_agents.stuck_tasks`). The team
lead reads every agent's saved work in its team (read-only) and reports one
incident per request and condition: the agent holding the work stopped reporting
in, a handed-back task nobody picked up, an access decision or a question to the
person still waiting (reminders after a day), or no progress while in progress.
Thresholds live in `agent.schedule_maintenance.stuck_tasks`. The person comes
from the root task's signed context (read, never used to authorize); without one
only administrators are told, and their notices never quote the request. The
task monitor also observes its own scheduler cadence
(`custom_agents.liveness_observation`). After a pause longer than the normal
60-second scheduler wait plus one 20-second heartbeat interval, it gives
workers 60 active seconds to refresh their heartbeats. During that window,
heartbeat-only takeover waits and task incidents remain unchanged; launch
records, process evidence and explicit exits still decide. Host sleep cannot
make a live startup agent look dead in any case: it is judged by its process,
not its heartbeat (see below). Signed
work for no person is labelled as system work or maintenance. A cleared warning
reports that the condition ended, without claiming new task progress.
The same pass also frees a work slot held by a task whose turn is gone
(`turn_failure_runtime.release_orphaned_slot_when_idle`), which previously
waited for new work to arrive.

Four repairs keep work from getting stuck in the first place:

- The team lead starts a replacement worker for a dead agent that still holds
  unfinished work (`custom_agents.orphaned_work_takeover`); the replacement's
  startup takeover adopts it in one transaction. One rule
  (`custom_agents.agent_liveness`) decides whether an agent is gone, for
  takeover, replacements, schedule coalescing and stuck-task notices alike. A
  worker started inside another process is gone when its start record ended or
  its process is proven gone. A startup agent or the team lead reports its own
  process in its status (`AgentStatus.process_identity`) and is gone only when
  that process is proven gone (`runtime_hosting.process_run`); while it runs it
  keeps its work, however old its heartbeat. An agent that reports no process,
  or whose process cannot be checked from here (logged as
  `owner_liveness_unknown`), is judged by its heartbeat, after
  `agent.schedule_maintenance.stuck_tasks.owner_gone_after_seconds`. The proof
  assumes every agent process of a team is a child of one supervisor run on one
  host: a process from another run counts as gone whatever host it names, so
  agent processes of a second runtime sharing the database are not supported.
  Each agent writes only its own status row as it stops (marked exiting from
  the moment its exit begins); the team lead stores other agents' presence only
  when it is newer, never later than its own clock, and removes rows of agents
  that are gone on its own timer, so a manager-only restart no longer makes
  running startup agents look gone.
- A worker stays up while it holds any unfinished task, waiting ones included
  (`strands_team.worker_holds_work`); replies to a waiting task are addressed to
  it.
- An access decision follows its task: any agent of the request's type whose
  task waits on it accepts the notice, and each agent periodically resumes tasks
  whose requests were decided while the notice went to a previous agent
  (`access_request_runtime.resume_decided_access_waits`).
- A task that ends with delegated work still running stops it
  (`custom_agents.orphaned_subtasks`), and a worker still reporting on work whose
  request ended is told to stop.

A cron schedule carries its own `cron_timezone`, and its next run is computed in
that zone (`custom_agents.schedule_store.compute_next_fire_at`; a cron schedule
without one is refused). The store fills it when a schedule is added: the
owner's request time zone from the schedule's metadata (the gateway sets it from
the person's profile), else the instance time zone (`user.timezone`). The owner's
zone is the default because the team's people may live in different zones and
"every morning at 9" means their morning (owner's decision 2026-09-27). The zone
counts as control state, so changing a system-managed schedule's zone needs the
same permit as changing its cron. Callers that bind a permit before saving set
it themselves (`with_cron_start`). Cron schedules saved before this ran in UTC;
each scheduler start fills any still missing (`fill_missing_cron_timezones`,
logged per schedule). The launcher also sets `TZ` from `user.timezone` for
itself and every process, so log times are local; an ambient `TZ` wins.

Each agent's scheduler loop renews its launch lease, refreshes its status,
recovers stalled subtasks and fires due schedules in repeated passes. One failed
pass does not stop the loop: it is logged as `scheduler_pass_failed` with a
traceback and retried after a delay that doubles from one second up to 60
seconds. A stale launch lease (`scheduler_launch_lease_lost`) is different:
another owner holds that launch, so the loop stops claiming scheduled work and
the worker winds down (`custom_agents/lease_loss_wind_down.py`):

1. It leaves the shared queue for its agent type, so no new delegated work arrives.
2. It returns each task it has not started, and the task it is executing, to the
   TeamLead as a `WorkerTaskHandbackV1` message, one task per message with a
   deterministic handback id. The running turn is interrupted without a
   cancellation record, so the task can run elsewhere. New requests that still
   reach the worker go to the TeamLead the same way.
3. It keeps tasks that already started and now wait on a person or subtask,
   because replies are addressed to this worker's id. They may finish until
   `agent.schedule_workers.lease_loss_wait_timeout_seconds` (default 30 minutes)
   after the lease was lost; then they fail with an explanation to the requester,
   their pending subtasks are asked to stop, and an access request they were
   waiting on is withdrawn so the reviewer is not left with an open item.
4. It exits once it holds no unfinished task. Until then it keeps publishing
   presence, so a starting worker of the same type does not adopt its tasks.

The TeamLead accepts a handback only when the sender owns the stated launch, and
saves it (`storage/worker_handback_repository.py`) before acting on it. Its runtime
(`custom_agents/worker_handback_runtime.py`, not the model) then sends the task
out again, unchanged, on the shared queue for that agent type, keeping the
original reply route so the result reaches the original requester:

- A cancelled task, or a lead-delegated subtask that already finished, is not
  sent out again.
- An interrupted scheduled run is not rerun, and its occurrence receipt is
  closed as `superseded`, when the schedule already ran again as its own task,
  is paused or disabled, or has its next run due within
  `interrupted_run_skip_window_seconds` (default 15 minutes). A one-time
  schedule has no next run, so its run is always rerun.
- The task waits in the queue until a free worker takes it; the worker pool
  controller starts a worker if none is free. A winding-down worker stops
  asking for work, so it never takes it.
- An interrupted task carries a typed `redispatch_notice` in `MessageMeta`. The
  worker that accepts it turns the notice into a one-time instruction for the
  task's first turn: which actions the interrupted run already sent, and to
  check them before repeating anything.
- For a subtask the lead delegated itself, its record returns to "waiting in the
  queue", so it is not probed until a worker takes it.
- The worker that accepts a rerun takes over its occurrence receipt, fenced on
  the previous owner, so the schedule execution scope and completion follow the
  run. If the schedule ran again while the rerun was in flight, the worker
  closes the run as superseded instead of running it.
- Processing runs on receipt and on every lead scheduler pass, so a publish
  failure or a lead restart only delays the re-dispatch.
- After `agent.schedule_workers.max_consecutive_interruptions` (default 3)
  interruptions in a row, the runtime stops retrying
  (`custom_agents/worker_interruption_escalation.py`). Ordinary work is counted
  per task id and cancelled with the usual reply to its requester (a subtask the
  lead delegated itself fails, so its parent resumes). Scheduled work is counted
  per schedule since its last finished run, and the interrupted run is closed.
  The runtime then admits an `interruption_review_...` task to the lead with the
  evidence of each interruption. The lead investigates, tells the owner, and may
  pause the schedule with `pause_scheduled_job`. Reviews repeat every
  `max_consecutive_interruptions` further interruptions of the same streak.

Runs of one schedule never overlap across workers. When a schedule comes due
while an earlier run of it is unfinished on this agent, or on another worker
whose presence is still live, the new occurrence is recorded as coalesced into
that run instead of starting a second task. A crashed worker's leftover receipt
does not count, so it cannot block the schedule.

Known limits of the lease-loss handling:

- If a worker exits before the lead re-dispatches its interrupted scheduled run,
  and the schedule fires in that gap, the new fire no longer sees a live owner and
  runs. The rerun then notices the newer run when a worker accepts it and closes
  itself, but a rerun already accepted before that fire can briefly overlap it.
- Replies addressed to a worker's id after that worker exits (for example a
  subtask result for a task it failed at the wind-down deadline) are not
  delivered anywhere; the requester already received a failed result.

Abandoned memory-review claims become inactive review records after a grace period
and checks for live or saved work. InternalImprovementAgent reviews their metadata
in its maintenance runs and uses bounded archive/purge tools, which are its fixed
duties as a built-in system agent. Memory review jobs are made for one person, so
the review tools show and resolve only jobs made for the person the task works
for. Cleanup retains an audit
history and never treats expiry as proof of completion or a reason to replay work.

SQLite is the default local store. PostgreSQL supports shared deployments.
Repositories preserve state that must survive a process restart or remain
inspectable, including Tasks, runtime snapshots, schedules, Plans, Objectives,
conversations, memory, authorization records, gateway sessions, and execution
attempts. Agents still coordinate through NATS rather than polling one
another's tables.

An Objective change that a tool call or a person's command asked for is
reported as not saved when the write fails (`ObjectiveSaveFailed` in
`custom_agents/task_manager_objectives.py`), so the caller never gets back a
changed record the store does not hold. Steps nobody waits on (a task starting
or ending, a plan following its task, a work session recording progress) keep
their own records elsewhere and go on without the Objective's copy, logging
`event=objective_save_skipped` with the step. A schedule or heartbeat item
whose Objective could not record it stays, and the reply warns that pausing
the Objective will not reach it.
The final deletion status, completion timestamp and summary are saved together.
A failed completion therefore leaves the Objective deleting and retryable;
there is no later metadata write after the record becomes deleted, because
repeated deletion commands return immediately for a completed deletion.

The memory system has the five stores the README lists, which agents recall
from (conversation history, user memory, team memory, the memory graph and the
knowledge base), plus task history:

- Conversation history stores durable threads and compaction boundaries. A
  conversation and each of its turns belong to the person the task's signed
  context names, or, for a scheduled run, to the schedule's owner. A person id
  in the message fields alone never makes someone the owner. A new task
  continues an existing conversation only when it works for that
  conversation's owner, and work for no person continues only an ownerless one
  (`access_control/ownership.py`, `conversation_continuation`), because the
  earlier turns go into the task's prompt. Any other task that names the
  conversation is refused and logged (`conversation_owner_mismatch_refused`).
  So trusted code never hands work someone else's conversation: runs for no
  person start their own; a team schedule moved from a departed person, and
  each item of a moved heartbeat launcher, continue in a new conversation; a
  gateway refresh gives a system schedule the session conversation of the
  person it names, which also moves a schedule an older release handed over;
  and a heartbeat item whose conversation belongs to someone other than its
  run's person gets a new one before the run starts. A departing heartbeat
  launcher's item conversations change before its owner changes. If any item
  update fails, the launcher keeps its inactive owner and runs stay stopped;
  the next departure retry resets every item again before moving the launcher.
  This order also protects removal and restore after a partial write or restart.
  The owner decides
  who may read the conversation, and it leaves the team's records with them.
  Work without a verified person stays ownerless through restarts: the
  single-user owner assignment at gateway startup changes only gateway rows,
  and the manager never gives its conversations, archived tasks or schedules
  with no owner to anyone, because a missing owner means work for no person.
  Schedule loading treats an empty database owner as authoritative too: it
  clears stale personal identity, chat routing and conversation references
  from executable metadata without assigning an owner or rewriting the row.
- Task history and execution digests store progress, delegation, failures, and
  recovery decisions. A task record belongs to the person its task verifiably
  works for (`owner_person_id` in `custom_agents/task_records.py`), decided
  once when the task is admitted from its signed context or schedule run
  identity and kept in the runtime snapshot; work for no person has no owner.
  Subtasks and the plan step, recovery and Objective tasks the manager makes
  from a task inherit that task's owner. The archived task row, a person's
  task list and task controls, Objectives, solution variations and a
  person's departure all read this owner; a person id in the message fields
  is never used. Rationale: deciding at admission keeps the owner when the
  signed context later expires (after 7 days) or the runtime restarts, and one
  recorded owner keeps a task's active and archived views in agreement.
- Pinned team and user memory stores reviewed facts and preferences with
  provenance and validity periods. Provenance (source chat, session,
  conversation, task, creator and the fact's internal UUID) stays in storage:
  agent memory tools and memory reviews get a public view built field by field
  (`custom_agents/pinned_memory_runtime.py`, `public_pinned_memory_fact`: text,
  validity, tags, metadata, the importer key trusted imports use, timestamps,
  revision digest). The reserved `metadata.imported_from` record also stays in
  storage; tool and review views and context hydration keep ordinary skill
  metadata, including onboarding progress. The Settings
  memory controls get the same view without metadata (`pinned_memory_reply`).
  A Signal chat id is a phone number, so it never reaches a model or a reply.
- The memory graph stores episodes, entities, claims, evidence, handoffs, and
  semantic retrieval data.
- The knowledge base (`knowledge_base/`, `storage/knowledge_repository.py`) stores
  pages agents and people write together: how-tos, decisions, lessons and
  references, in the team space or one person's own space, with every revision,
  notes and `[[links]]`, searched with each database's full-text index. Agents use
  it through `fn:knowledge` (`custom_tools/providers/knowledge.py`). Three rules
  are trusted code, not prompts: an agent working for a person only proposes team
  changes, which stay private to that person until reviewed (pending
  contributions); credentials and identity numbers are never stored, and contact
  details or text from the person's own memories are flagged for review
  (`knowledge_base/sensitive_content.py`). The service scans page names and tags
  before and after normalization, as well as page text, change descriptions
  and decision reasons, so metadata cannot bypass these rules. Published pages
  use the reviewed title to form a name unless the reviewer names a page
  explicitly; a slash in a title is ordinary text. The store enforces per-agent write
  limits inside its write transaction (`agent.knowledge.limits`), so no agent
  dominates the knowledge base or rewrites a page in a loop. Direct and proposed
  archives share one hourly budget, and revision fingerprints include the page
  type so agents can correct classification without changing the text. KnowledgeCuratorAgent
  (a built-in system agent started on demand) reviews each person's pending
  contributions in a run signed for that person and tidies team pages in a
  run for no person (system schedule kinds `knowledge_curation_person` and
  `knowledge_curation_team`; `custom_agents/knowledge_curation_runtime.py`
  skips a run with nothing to review). Its tools (`fn:knowledge_curation`) are
  in the `knowledge_review` tool set that the curator and people hold, never
  the team lead. The `knowledge_recall` memory step adds matching page titles
  to the team lead's turns. Exact duplicate active team pages, found from the
  existing content fingerprints, trigger cleanup even when nothing else is
  wrong. Frozen notes stay open until a later run can merge them after the
  freeze expires, or until the curator lifts the freeze (`unfreeze_knowledge_page`;
  a freeze always lasts at least a minute). Direct team reverts and merges of
  team pages are refused in agents' personal work, and team revert history
  records only the restored revision. Merging two pages
  (`KnowledgeService.merge`) writes the merged text as the target's next
  revision and archives the source in one transaction; the source's name
  becomes a redirect (`knowledge_redirects`), so reading, noting, editing and
  `[[links]]` by the old name reach the target and a new page never takes that
  name. Both histories get fixed text from trusted code, open notes move to
  the target, and pending contributions are pointed at it against its
  pre-merge revision, so their reviewer merges them again. Note and contribution
  insertion rechecks redirects under the same transaction lock, including
  writes prepared before the merge. Backlinks return each referring page once
  before applying the result limit. The team run's
  preparation sends administrators one Inbox notice a day (kind
  `knowledge_needs_attention`) when team pages froze in the last day or an
  agent type had many refused writes (`NOTICE_REFUSED_WRITES`), with page
  names and counts only. Freeze notices include events whose freeze has already
  expired or been lifted, and describe the current state. Preparation sends
  these notices even when no model run is needed. An after-action report review
  can save its lesson as a page (`saved_to_knowledge`,
  `custom_agents/after_action_report_knowledge.py`):
  a review of a person's work proposes it for the curator, a review of work
  for nobody publishes it. The knowledge transaction also stores a receipt
  keyed by the review, its verified person and an exact digest of the lesson
  fields. If review resolution fails, a retry after restart reuses that receipt
  without another contribution, revision or budget charge; changed lesson
  fields are refused. Receipts contain references, not lesson text, and are
  deleted with the person's reviews on departure. They are runtime control
  state and are not transferred, because pending reviews are not transferred.
  The team transfer `knowledge` section carries redirects from version 2.
  People use it from the Web Chat Knowledge page (see "Knowledge page" below).
  Live privacy scenarios (`tests/test_live_knowledge_privacy_scenarios.py`,
  opt-in with `RUN_LIVE_KNOWLEDGE_PRIVACY=1`) run real agents and the curator
  with the configured model and check that a person's planted private details
  never reach published team pages or another person's model prompt, answers
  or tool calls. They require completed reviews and successful scenario tool
  calls, so an idle model or unfinished review cannot count as a privacy pass.
  Their model-free check of published team content is
  `tests/knowledge_privacy_checks.py`. Rationale and the storage benchmark:
  architecture repository, memory and context architecture.

Before a model call, the runtime assembles context from the current request,
system and role instructions, task state, conversation history, relevant
memory, routing context, and bounded artifact references. A provider buffer may
retain inner-loop state, but durable continuity never depends on an opaque
provider session. Personal memory (a person's facts and hints to their earlier
threads) is added only for the person that the task's signed context or
scheduled-run identity names. A person id in the message fields alone never
selects whose memory loads, and work for no person gets none. The memory
review job for a turn is made for that same person. Before copying conversation
turns into a review job, the runtime checks their stored conversation owner
through `access_control.ownership`; a conversation owned by someone else or
without a recorded owner is refused and its turns remain unqueued. The working
folder that the prompt names is that person's folder too, the same one the file
tools use. Plan-step and recovery prompts hide gateway details, but their
working-folder selection verifies the original task metadata separately from
the sanitized prompt metadata.

Runtime snapshots and idempotent admission let processes recover queued work,
waiting subtasks, deferred messages, and recent in-flight Tasks. Outboxes record
ordered actions before external effects. Execution attempts and worker starts
use leases and fencing, so a stale holder cannot commit after another takes
over. Saved runtime snapshots and deferred messages move only from an agent
that counts as gone, in one transaction that deletes its row. A previous owner
that is still running finds its row gone at its next save, which is refused
(`RuntimeCustodyLost`) before anything is written; it then drops that work from
memory without replying and keeps running (`custom_agents.custody_loss`). A
deferred message leaves the deferred row in the same transaction as the first
task save its handling makes, and only while the agent still holds that row
(`deferred_message_runtime.DeferredHandoff`); when its handling saves nothing,
a checked removal follows. So the saved effects of a task or deferred message
are written by one agent only, and a crash keeps the message either in the
deferred row or, through its effect, in the task row. What is not covered: work
a previous owner does before its first save after the takeover (a model turn in
progress until it is stopped) and handling that saves nothing (such as a reply
about a task it no longer holds) may still happen on both agents. Repairs and
fallback paths are logged rather than hidden.

Transport contracts, workflow records, and snapshot codecs have separate owners.
Shared subsystem configuration lives in `runtime_settings/`; the gateway composes
those settings. Async task and context operations use serialized storage execution,
with detached records at the caller boundary. SQLite connections close at the end
of their transaction context, and PostgreSQL uses native SQL and connection owners.

Operator tool allow and block lists name exact built-in functions from the release
catalog. Both configuration formats validate all such lists at file load, including
unused agent types and defaults that an override replaces. The generic loader
selects only the list fields and reuses the shared setting models, retaining full
paths in errors without changing configuration values or presence. Checking before
override resolution prevents configuration errors from becoming delayed worker
startup failures. MCP tools follow their separate registration path and cannot be
named in these lists.

The topology owner establishes JetStream streams before consumers validate them.
Each agent owns one model execution slot across turn preparation, invocation,
and completion. Parent recovery waits for that owner; queued tasks cannot claim
the slot during an active turn. Control and status messages remain responsive.
Because a model call holds the slot, every model call is time-bounded:
`build_strands_model` wraps each provider stream with
`custom_agents.model_stream_watchdog`, which ends a call that sends no output
for `llm.stream_idle_timeout_seconds` or runs past
`llm.model_call_timeout_seconds`. The HTTP client timeout alone is not enough,
since proxy keep-alive pings keep an idle stream's bytes flowing. A stopped call
is a provider timeout: failover, or a failed task with a plain timeout message.
A task claims the task manager's single execution slot by moving to
IN_PROGRESS before its turn, so every path that runs a turn (the queue, a turn
resumed after a sign-in or access decision, a resume with a directive) ends a
failed turn through `custom_agents.turn_failure_runtime`. As a safety net, the
queue logs `event=execution_slot_orphaned` and ends any IN_PROGRESS task that
still holds the slot when no turn is running. RETRYING keeps the slot on purpose
while the turn-action outbox waits to retry.
Busy or paused runtimes admit messages up to `agent.max_deferred_messages` (100 by
default); excess messages remain with the broker for redelivery. The active task
snapshot layout has changed without a migration. Finish or explicitly abandon
in-flight work before upgrading; see the [configuration operating notes](user_docs_source/configuration.md#active-runtime-state-and-admission).

The gateway presentation transcript and manager conversation history are
different records. The first reproduces what a user-facing channel was allowed
to display. The second supports reasoning context and task continuity.

## Authorization and credentials

Every trust boundary keeps its own deterministic checks. These include gateway
authentication, owner and team scope, repository constraints, path and shell
policy, transport identity, broker policy, and workload validation. Tool
visibility, model prose, task assignment, or a self-reported role does not grant
authority.

Every file an instance keeps sits under one instance root, split by who may
touch it (`runtime_settings/instance_layout.py`): `control/` for team control
files and skills, `dbs/` for databases, `state/` for runtime records, and
`workspaces/` as the only area agents work in (`users/<person>`, `team`,
`agents`). Only the root is configurable; every other path is derived, so no
setting can place an agent-reachable folder over control or state files. One
process runs work for different people, so each tool call finds its folders
from the person its task works for (`custom_tools/workspace_scope.py`): that
person's folder plus `team/` and `agents/`, or only the shared folders for work
with no person. File tools, `notify_user` attachments, research files, Python
environments, and the code sandbox all use this one resolver. The gateway's
workspace browser (`assistant_gateway/application/workspace_browser.py`) opens
for every signed-in person, with roles from the real session: members see only
their own folder, administrators also the shared folders, and nobody sees
another person's folder. Folders outside the instance reach agents only as data
folders, which administrators register in the access store
(`access_control/data_folders.py`, decision D16: `/access folders`,
`assistant_cli access folders`, the Access page, or an approved proposal).
Every process reads the current list when it uses a folder, so registering one
needs no restart, and the rule that a data folder may not hold or sit inside
the instance, its configuration or the broker's credentials (or overlap another
data folder, or look like a credential store) is checked at registration and
again at every use. `tool_policy.data_folders` is removed and refused by name:
a configuration file is read only at start, and keeping the list where
administrators decide access lets them change it while agents run. Agents read and
change control files only through typed services (settings, roster,
provisioning, reviewed skill installation). Team skills are the one readable
exception, because agents must read a skill's files and run its scripts; they
are never writable, and new or changed skills go through reviewed installation.
Startup refuses an instance still in the old single-`workspace/` layout and
prints the move commands rather than moving files itself.

Web research tools save files under `research/<task folder>/<result folder>/`
in the task's workspace. Every task folder combines a readable prefix with a
digest of the full task id. Hashing all ids prevents a short, unchanged id
from matching another task's generated folder name. Each call adds a random
suffix to its result folder so repeated or parallel searches keep separate
files. New writes use these names; saved paths remain readable because
inspection checks the person's research root rather than rebuilding names.

Agents' code can create links in the folders it writes, and it runs at the
same time as trusted code, so a tool that checks a path and then opens it by
name can be sent elsewhere. Two real escapes worked this way: a planted
`research` link let `web_artifact_inspect` read control files and redirected
research writes into `control/`. Every agent file open therefore goes through
one library (`custom_tools/file_access.py`): it makes the path absolute against
the task's default folder, resolves links once to judge where the path really
points (`custom_tools/path_policy.py`: the task's folders, data folders, what
another person shared with the task's person, the read-only skill folders,
never control files, runtime records, another person's unshared folder or a
sensitive name), asks the access service for a folder
decision when authorization is on, and then opens that real location through
`access_control/safe_open.py`. `safe_open` walks from a trusted root one
component at a time with `O_NOFOLLOW`, accepts only regular files with one
link, creates files with `O_EXCL`, and replaces existing ones in place through
the checked descriptor, so a link planted beforehand or swapped in after the
check makes the open fail. The broker's staged CLI files
(`secure_tool_broker/task_files.py`) and the gateway workspace browser use
`safe_open` too. Sensitive names (env files such as `team.env`, SSH private
keys, `.pem` files, credential folders such as `.ssh`) come from one list,
`access_control/sensitive_paths.py`, used by the file tools, the shell tool's
path arguments, attachments in both directions and the workspace browser, so
no check refuses a name another one lets through. `notify_user` attachments
(from the person's folder, `team/`, `agents/`, a data folder, or something
shared with the person, when the agent-side decision allowed reading it) are
checked again by the gateway for the person and copied once, without following
links, into that person's
own folder (`users/<person>/scratch/sent/<id>/`, or `agents/scratch/sent/` for
work for no person), with the copy's SHA-256 recorded
(`assistant_gateway/attachments.py`). Files a person receives stay with that
person's files. Agents' code can write that folder, so every reader (chat
channels, the external adapter host, the Web Chat download) goes through one
reader, `StagedAttachmentReader`: it opens the copy with `safe_open`, requires a
one-link regular file of bounded size, and checks the digest. A link or changed
bytes swapped in after staging are refused (Web Chat answers "no longer
available"), so the file delivered is the file that was checked. The Photon
sidecar reads a path, so it gets a private 0600 copy under `state/` that is
removed after the call.

Published pages (how-tos, articles, apps from `publish_single_page_site`) are
deliverables too, so they are written into the person's folder
(`users/<person>/sites/<site_id>/`) rather than `state/`. The tool also writes a
trusted record, `state/published_sites/<site_id>.json`, naming the page's
folder, the person it was made for, and the SHA-256 of each file
(`user_docs/published_sites.py`). The site host finds a page only through its
record and serves it only while the digests match, so agent code cannot change
what the Web Chat origin serves. Anyone with a page link can open it, which is
why site ids end with a random part; the `/docs/sites/` index lists only the
signed-in person's pages, and a page's chat works only in the page owner's
signed-in Web Chat session and sends each message as that person.

Agents keep temporary files in `scratch/` of their task folder. When the
internal-improvement maintenance schedule fires, trusted code in the manager
deletes files there older than `tool_policy.scratch_retention_days` (14 by
default, 0 turns it off) from every person's and the agents' `scratch/`,
walking folders without following links, and gives InternalImprovementAgent
the counts (`custom_agents/scratch_retention.py`). The maintenance run itself
is system work for no person and cannot open people's folders, which is why
trusted code does this part. The run may use the shared `team/` and `agents/`
folders, sandboxed code there, the memory-review cleanup tools and model
lookup, and it may read and record only its own schedule
(`access_control/ownership.py`, `system_work_schedule_access`): people's
schedules are private and a change to a system schedule needs a person. So
trusted manager code checks every schedule before each run
(`custom_agents/stale_schedules.py`): it deletes the own schedules of people
removed from the team and tells owners and administrators about unused ones
through the Inbox, once per list; the run gets counts only. So that the run's
findings reach a person, the manager signs TeamArchitect's task for its
escalation for the owner stored on the maintenance schedule (the first user),
with the scheduler key; a person named in the escalation itself is ignored
(`custom_agents/task_context_runtime.py`, `sign_system_escalation`).

Every scheduled run takes its owner's standing and roles at the moment it runs
from the gateway's copy of the team's people in the access store
(`access_control/people_directory.py`, written by the gateway whenever a person
is added, changes role, is disabled, enabled or removed, and in full at each
start). Why a copy: the manager signs scheduled work but cannot read the
gateway's records; before, runs used roles saved on the schedule, so a demoted
administrator's schedules kept administrator roles and a disabled person's kept
running. A disabled or removed owner's schedules do not run. Roles are given
only when the owner is vouched for: a system-managed schedule, or a context the
gateway signed for that same person saved with the schedule. The gateway still
keeps where to reach each maintenance schedule's owner current: at each start,
and whenever the manager becomes available again, it sends a `refresh`, and the
manager replaces only the stored person, chat session and that session's
conversation under the trusted-runtime permit, keeping pause, enabled state,
cadence and next fire time (`custom_agents/schedule_context_refresh.py`). A refresh never creates a
schedule. Memory-review schedules are installed the
same way: once (`ensure`), then refreshed, so a gateway restart no longer
resets an operator's pause or cadence. The core-facts audit is one team
schedule; the personal-facts audit and stale-conversation review are one pair
per active person, owned by them and delivered to their own chat
(`assistant_gateway/application/person_maintenance.py`), because a run reviews
only its owner's memory and conversations
(`custom_agents/memory_review_runtime.py`).

What the runtime does with each kind of schedule it manages is listed once, in
the system schedule table (`custom_messages/system_schedules.py`), which the
gateway and the manager both read: whether a kind belongs to one person
(deleted when they leave) or to the team (moved to another administrator),
whether its runs work for no person, which agent type runs it on a worker
started on demand, its delivery mode, and whether it is internal maintenance
that never gets its own conversation. Adding a kind adds a row there, and a
hook in `custom_agents` when its runs need preparing, instead of a check in
every place. A kind that works for no person also needs a fire path that calls
`custom_agents.schedule_permits.sign_system_run`; the ordinary path still signs
for the schedule's owner. The table decides no-person permit issuance and
identity verification, but does not select the signing path.

Every callable operation has an access level (`read` < `write` < `full`) in one
tool catalog (`access_catalog/`, definitions in `ua_definitions/access`):
built-in functions, code tools, broker actions, `gh`/`gog` commands, web API
routes, confirmed MCP operations, and installed procedures. Unclassified
operations need `full`, and startup refuses a build whose tools lack a level.
Related functions that act on one kind of record are one tool with an `action`
argument (`memory` and `topic_tags`; `mcp_connections`, `hosted_mcp_servers`,
`installed_permissions` and `specialist_definitions` in `fn:capabilities`;
`objectives`, `objective_work_sessions` and `objective_work_profiles` in
`fn:objectives`): each action is its own operation
(`memory.delete`) with its own level, an unlisted action needs `full`, and the
tool checks which inputs each action takes (`custom_tools/action_inputs.py`).
A request then carries fewer tool definitions while access stays per action,
and a block can still stop one action.
The broker and the runtime share the catalog package, which imports only
contracts, because the broker never imports agent code. The broker checks
every `gh`/`gog` command line with one unambiguous parser and runs exactly the
checked arguments; local files named by a command are copied through a private
folder and limited to the requesting person's folders.

Agent-run shell and Python commands execute in a Linux Landlock sandbox outside
OpenShell mode. Path checks on command text cannot contain code that builds
paths at run time, so the operating system limits each command. The launcher
is an enforcement point: for each command, `custom_agents/code_access.py` asks
the access service which folders the agent and its person may use (the task's
folders, data folders, what was shared with the person, and folders in the
agent's grants for this person's work) and grants each one
read-write where both may write, read-only where they may only read, or not at
all; the command also gets read-only skill folders, a private scratch home, and
read-only system files. Landlock rules only add access, so a folder with a
blocked folder below it is left out entirely and the result says so, rather
than exposing the blocked part. Control files, databases, secrets, instance
configuration, other people's folders, and other processes stay out of reach.
TCP connect and listen are open only when the agent and its person hold
internet access (`fn:internet`, operation `connect`, decision D14). It sits
with the web research tools in the `internet` tool set, which every role
holds as standard access, so code reaches the web by default and a block on
the set or on `fn:internet` (for an agent type, a role or the whole team)
closes it. Internet access is its own grant rather than a side effect of
`full` on a code tool because the two answer different questions: whether code
may run at all (and write which folders) is not whether it may talk to the
outside world, and tying the network to `full` made people grant far more
than they meant just so a package could download. Closing TCP needs Landlock
ABI 4 (Linux 6.7); on older kernels a command without internet access is
refused rather than run with the network open. Landlock does not cover UDP,
so internal services such as the local NATS server still require credentials
that sandboxed code cannot read. Writable folders get every right except making device files;
links may be made (Python environments need them) because trusted code never
follows them.

Running a command outside the sandbox is its own catalog tool,
`code:unconfined`, so network access never implies host access:
`shell(..., unconfined=true)` and `python_env(..., unconfined=true)` run that
one command unconfined only when the agent and its person hold `full` on it,
after a person approved the agent's `request_access`, and each run is logged
(`event=code_unconfined_run`). Deterministic execution jobs on a profile that
runs on the controller host (`local` backend) need the same access, lasting
beyond one task, at activation and before every run
(`runtime_hosting/authorization.py` `require_host_execution`). With
authorization off, the task's folders, data folders and the network are open,
and only the operator's `security_layer.code_execution.sandbox: off` runs code
unconfined.

OpenShell mode runs code in OpenShell sandboxes instead
(`custom_tools/openshell_sandboxes.py`). OpenShell copies files in when a
sandbox is created rather than sharing a folder, so one shared sandbox would
hold every person's folder. Each person therefore gets their own sandbox,
created on first use with copies of only their folder, `team/` and `agents/`;
work for no person uses a shared one. The operator's OpenShell policy decides
the network and anything else there; folder grants and the network rule above
do not apply.

Who a task works for is signed, not claimed. The gateway signs a task context
(Ed25519) naming the person and their current team roles when it admits a
request; the manager's scheduler signs one for each scheduled run's owner
(`access_control/identity.py`), except runs of the runtime's own maintenance
schedule for InternalImprovementAgent, which it signs as system work for no
person (`custom_agents/task_context_runtime.py`). The context travels unchanged on every
delegated task, so a later hop cannot widen it. Private keys live only in the
gateway and manager processes; every checker holds the public keys.

Scheduled workers claim their own due schedules from the shared database, so
the claiming agent is not always in the manager process. An agent inside it
(the team lead, or a worker the manager started on demand) signs the run
itself. A worker started from team composition runs as its own process
without the scheduler key. Why not give every worker the key: a key in every
agent process would let any one of them sign for anyone.

Instead the manager signs one permit per schedule (`SchedulePermitV1`,
`custom_agents/schedule_permits.py`; owner's decision 2026-09-27). The team
lead's scheduler pass, in the process that holds the key, issues a permit for
each turned-on schedule whose owner is vouched for by the same rule as a
locally signed run (the schedule is runtime-managed, or the context saved
with it was signed by the gateway for that person) and is active, and a
permit for no person for the maintenance schedule of InternalImprovementAgent.
A permit lasts 30 days and is renewed in its last 7, and re-checked when the
schedule's owner changes; the pass is one query, at most every 30 seconds.
The permit is kept on the schedule row (`run_permit_json`), not in the
schedule, and needs no schedule permit to set, like the next run time. A
worker that claims the schedule puts the permit, with a fresh context id, the
conversation and the start time, in the run's `GatewayContext.schedule_run`
and starts the run without contacting the manager. Why one signature per
schedule: asking the manager for every run of every worker puts the manager
on the path of all scheduled work and does not scale.

A permit names no roles, so it cannot carry stale ones. Every check of a run
identity (`access_control/task_identity.py`, `resolve_task_identity`: the
access hook, the broker's gate and the schedule tools all use it) verifies
the permit, then checks live that the schedule still exists, is turned on and
still belongs to the permit's person, and reads that person's current
standing and roles from the gateway's copy of the team's people. These live
facts are cached for 30 seconds per schedule and permit (bounded), so a role
change, a disable or removal, or a deleted or turned-off schedule applies to
the run's next check within 30 seconds. A permit and a task context are
signed under different domains and name different record kinds, so neither
can pass as the other.

Only when a schedule has no usable permit yet (the manager has not looked at
a new schedule, or issued none for an unvouched owner) does the worker ask
the manager over NATS (`ScheduledRunContextRequestV1` on the Core NATS subject
`<prefix>.lead.run_context`, answered on `<prefix>.agent.<id>.run_context`),
showing its claim. The manager signs only when the schedule row's claim holder
and claim token match the request and the sender, takes the owner from the
row with the same rules as above, and answers with the signed context or a
refusal reason (`custom_agents/scheduled_run_contexts.py`). If the manager
refuses or does not answer within 20 seconds, the worker does not start the
run and releases the claim, so the schedule is retried later; a run without a
signed context or permit would have every tool call refused.

The secure tool broker checks every agent request before it runs anything
(`secure_tool_broker/access_gate.py`), in both authorization modes: the calling
process's own broker token (the supervisor derives one per process from the
broker's secret, so a separately started agent can act only as its own agent
type and only the gateway reaches sign-in and credential forms), the signed task
context or scheduled run identity (the same resolver as the agents' access
hook) and the person it names, the operation and level from the tool catalog,
and, for web requests, a web API tool (`http:*`) that covers the host; other
hosts are refused. With authorization enabled, the access service
(`access_control/decisions.py`) then decides with Cedar whether both the agent
and the person it works for hold that level through grants, which live in the
manager storage (`storage/access_repository.py`).

Every function tool call is decided the same way by one Strands
`BeforeToolCallEvent` hook that runs last (`custom_agents/access_runtime.py`),
so an earlier cancellation cannot use up a one-time grant. It skips calls the
broker decides, Strands' structured-output tool (the turn answer), and names the
agent has no tool for: nothing runs for those, and Strands answers "Unknown
tool", which the runtime guardrails extend with a hint when the name is a turn
action such as `delegate_task` (an access refusal there once made TeamLead
report delegation as broken). It verifies the signed task context in-process
and asks the access service. Roles hold standard access without grant rows
(`access_control/standard_grants.py`): the `agent_runtime` tool set for every
agent (its own work, including the access tools and the SymPy `calculator`),
`team_lead_runtime` for the team lead, `system_duties` and `sandboxed_code` for built-in system agents
(MemReviewAgent and InternalImprovementAgent, marked `access_role: system` in
their shipped definitions), which alone may work with no person, and
`internet` for every agent and person. Sandboxed code is a system duty because
building team CLIs and skills is InternalImprovementAgent's job; with no person
behind a task the sandbox reaches only `agents/` and `team/`. Members also hold
the `standard` set (everyday tools, never code or platform controls), so they
can authorize agents for their own work without an administrator deciding
routine requests; an administrator restricts a member with a block. With
authorization off the hook allows every catalog tool (trusted single-person
mode); the broker, sandbox and folder rules still apply.

The calculator is standard for every agent because it only computes
(`custom_tools/calculator.py`, level `read`): it replaces the
strands-agents-tools calculator, keeping its checks that stop an expression
from reaching Python code through SymPy's parser and checking the code the
parser generates as well (`custom_tools/calculator_parsing.py`). Tools run
inside agent processes, which must not start processes for them, so a long
calculation is stopped in-process: it runs in its own thread and is
interrupted between Python steps after 10 seconds
(`custom_tools/calculator_time_limit.py`). A single giant exact-number step
cannot be interrupted that way, so inputs that ask for one (huge exact powers,
factorials, precision) are refused before they run.

Authority is per person (decision D13). An agent grant carries
`for_person_id`, the person whose work it serves, and every validity check in
the access service (`valid_grants`, `holds`, `may_give`, `hand_down`, the
Cedar decision) takes the task's person, so an agent working for Alice has what
Alice authorized and nothing Bob authorized; a hand-down inherits its parent's
person. `None` means everyone's work and is reserved for standard grants,
administrators' grants given with `--everyone`, and system work for no person.
`AccessService.grant_refusal` lets a person give agents, for their own work,
anything they hold themselves (consent within their own access, not new
authority); agent grants for someone else's work need the authority to give it
and happen only when approving that person's request. `AccessRequests.may_decide`
gives a request for a person who holds the access to that person alone, and
sends access the person lacks, and system work, to whoever could give it
(administrators for team targets). Why per person: in a shared team the same
agent types work for everyone, so grants that served the whole team would let
one member's approval (or TeamLead's delegated authority from one member) act
in another member's tasks; the person side of the check alone could not stop
that, because both members may hold the same tool. The first user is an owner
and administrator but in normal use acts like any other user, escalating only
when a request needs it.

Access follows the work (owner's decision 2026-09-30). People only talk to
TeamLead, workers get work only from TeamLead, scheduled runs or system jobs,
and every tool call runs in a task with a verified identity, so workers never
act on their own account. An agent's access is its role's standard access,
administrators' team defaults (grants for everyone's work) and its current
task's access, plus the lasting access of whom it works for
(`AccessService.agent_grants`): TeamLead's lasting access for a person counts
in all TeamLead's work for that person, scheduled runs included, and a
scheduled job's run adds the schedule's access. A schedule is a grant holder
(`SubjectKind.SCHEDULE`, id `<team>/<schedule>`): the agent in one of a job's
runs is in it (`AgentPrincipal.schedule_id`, an extra parent entity in Cedar),
taken only from a scheduler-signed identity (`task_identity.job_schedule_id`).
A job run is a person's schedule that a worker type runs; its workers work for
the schedule and get nothing handed on. Schedules TeamLead runs and the
runtime's timers (heartbeat items, objective work sessions, after-action
reviews) work like the person's request. An after-action review works for the
person whose work the reports describe, taken from the original signed task
context or schedule permit when a report arrives, even after that context
expires or the source schedule ends. This verifies historical ownership and
grants no current access; the scheduler signs the review with current identity
and each tool call still needs access. Reports and report IDs are kept per
person, the review schedule is owned by that person, and the review tools see
only that person's reports
(`custom_agents/after_action_report_identity.py`). Built-in system agent types come from
the release definitions alone (`access_control.system_agents`), so every
access service agrees on them. Lasting
access for a person's work never goes to a worker
(`AccessService.worker_lasting_refusal`, checked by every grant path), and older
such grants simply stop counting, with no migration. A worker's missing access
is added to its task from what TeamLead may hand on without asking, inside the
decision itself (`AccessService.hand_on_for_task`, used by the function-tool
hook, folder decisions, code access and the broker gate), so there is no
`give_access` tool and no extra turn; its tool list shows those tools
(`hand_on_sources`). "For one task" means the person's request: task-bound
grants are bound to the signed context id every task of the request carries
(`AgentPrincipal.work_id`, `AccessRequestV1.work_id`, never an agent-supplied
task id), end when the request's first task ends (`access_runtime._clean_up_ended_task`)
and, as a backstop, when older than any context can live. Why one source: two
sources that override each other per tool need precedence rules (tool set
against tool against operation against folder subtree, narrowing against
widening, unnamed tools falling back), each an edge case; with one source there
is nothing to resolve. Access is always a level on a whole tool: grants and
requests naming one operation are refused (`AccessService.operation_refusal`),
because operation names are fragile (sending mail is both `gmail_send` and
`command:gmail.send`); blocks may still name one.

One entry per tool (`access_control/grant_entries.py`): an agent's access to one
tool for one person's work is one entry. A new grant that covers an existing
one replaces it in the same transaction, and what was handed on from the old
grant moves to the new one (`AccessRepository._replace_in`, a `grant_moved`
event), so replacing never cuts off a worker; this relaxes "grants are
immutable" for `parent_grant_id` only. A grant an existing one covers is not
stored. Grants neither covers stay side by side (lasting access plus a step-up
for one task). Removing access removes the whole entry
(`AccessService.revoke_by_person`, `AccessRequests.revoke`, proposals'
removals). Why: people saw several independent grants for one tool, assumed the
broadest replaced the others, and removing one could leave another working.

Folders are access targets too (`access_control/folders.py`). A folder is named
by an id below a registered root, never by a host path, so grants survive a
move: `team/…` and `agents/…` (every role holds `write` on both as a standard
grant), `data/<name>/…` for registered data folders (these need grants),
and `users/<folder>` for a person's own folder. A person's folder is owned by
that person: the standard Cedar policy `std:task-person-folder` lets an agent
use, up to `write`, the folder of the person its task works for
(`context.person` comes from the signed task context). Only the owner may give
access to it or to a single file in it (`TargetKind.FILE`), through
`std:owner` and only to other people: a share (decision D15). Administrators
cannot open or share it, and agent grants on a person's folder are refused.
An agent working for the recipient uses what was shared with that person at
the shared level, for file tools and sandbox mounts, through per-decision
copies of the person's share grants (`AccessService.shared_with`); blocks on
the agent still apply. In Cedar a file is inside each folder
up to its root, so a grant or block on a folder covers everything below it.
`AccessService.decide_folder` decides each file use (agent side, then person
side), and `custom_agents/folder_access.py` keeps allowed answers for 10
seconds per folder, keyed by the access store version, so a crawl or a Python
environment does not ask for every file while every grant, revocation or block
still applies to the next file. `request_access`, `restrict_access`, `/access`
and `assistant_cli access grant|block --folder` accept folders, and a worker's
task gets a folder from TeamLead's access like a tool.

People share and send files through `assistant_gateway/file_sharing.py`
(`/share`, `/unshare`, `/shares`, `/send`, and the workspace browser's Share
and Send). Sending into another person's folder is a change request made by a
person (`AccessRequestV1.requested_by_person_id`, kind `file_delivery`) that
the recipient or an administrator decides. The file's SHA-256 is recorded when
the request is made, and at the decision the gateway copies exactly that
content, without following links, into `users/<recipient>/received/<sender>/`
(a move removes the original only while it still matches). Copying to the
shared `team/` folder needs no approval, and agents move files between folders
they may write with `file_move`. Why the digest: the sender's folder is
writable by agents' code, so without it the recipient could approve one file
and receive another.

Access has four settings: `authorization.enabled`, `cedar_binary_path`,
`starter_profile` and `presentation.mode`. Every check always applies; there
are no switches that turn individual checks off or into observation only, and
the Cedar policies ship with the release (`ua_definitions/access/cedar/`)
instead of being configurable. A configuration file therefore cannot weaken a
check. `starter_profile` is the first user's default for TeamLead's standing
access to their work, applied by the gateway at start until that person
chooses their own; every other person chooses theirs (`/access profile`, the
Access page, or `assistant_cli access starter-profile --by`), and it serves
only their work. Each choice is recorded with whether the person made it
(`access_profile_choices`), because "manual" leaves no grants to recognize and
a restart must not undo a person's decision.
The loader refuses the settings of the earlier permission system, the removed
`tool_policy` switches and `tool_policy.data_folders` by name
(`runtime_settings/governance.py`)
rather than ignoring them, and startup refuses the removed file-path
environment variables (`TOOL_RESTRICT_TO_WORKSPACE`, `TOOL_DENY_PATHS`,
`TOOL_DENIED_PATHS`), so an operator learns that they no longer do anything.

Agents ask for access with `request_access` (`access_control/requests.py`). A
worker's request is handed down by trusted code at once when the team lead
holds an automatic, delegable grant covering it; the team lead's model does
not decide. Otherwise the request is stored, the task pauses with a Strands
interrupt that waits in the agent's own process (it is never sent to the
requester as a question), and the gateway's access Inbox
(`assistant_gateway/access_inbox.py`) announces it once and lets a person who
could give the access decide. The gateway then sends `AccessRequestResolvedV1`
(ids only) to the agent, which reads the decision from the store and resumes
the interrupted call, or the task after a restart
(`custom_agents/access_request_runtime.py`). Notices for automatic hand-downs,
team skill changes, release changes, shares, received files and credentials to
choose are stored for people and never pushed to a chat. Each review card lists
every row to approve (`ChangeProposalV1.items`, `ReviewItemV1`); grant rows
carry "Keep for later tasks" (off: this task only) with a "How long" choice,
and TeamLead's own requests a box that lets it hand the access on to workers
without asking. Chat buttons, which have no checkboxes, offer the same choices
as separate buttons (`assistant_gateway/application/access_reviews.py`).

When nobody can hand the access on, or a service is missing from the tool
catalog, TeamLead proposes the whole change with `propose_access_change`
(`custom_tools/providers/access_proposals.py`, catalog `fn:access_proposals`):
typed items for new web API tools, grants for agent types, restrictions, data
folders, account links and revocations (`AccessProposalV1`), never code or
policy text. `access_control/proposals.py` (`ProposalCompiler`) checks every
item against the catalog as it would be with the new tools and writes the
review rows in trusted code's words, flagging items that widen access. The
proposal is one change request on a platform tool at `full`, so an
administrator decides it and the Inbox, chat buttons, workflow linking,
withdrawal and resume work as for any change.

Some changes are applied by the gateway at the decision itself rather than
later by the agent that asked: approved proposals (`ProposalApplier`) and file
deliveries (`GatewayFileSharing`), registered as `AccessRequests.appliers`
(a `ChangeApplier` port, like `RequestWatcher`, so `access_control` never
imports gateway code); deciding such a request without its applier, for
example from `assistant_cli access decide`, is refused. Why at the decision:
only the gateway holds the reviewer's verified roles at that moment, so it can
check each item against the reviewer's own authority, and nobody would be
waiting to apply a file a person sent. The proposal applier re-reads the stored
plan, compares its digest with the one reviewed, writes any new tool file to
`control/access/tools/` from the typed item (`access_catalog/instance_tools.py`,
only `http:*` tools), reloads the catalog, and stores grants, blocks, data
folders, revocations and the request outcome in one transaction
(`AccessRepository.apply_together`); if that fails the tool files are removed,
so nothing is half applied. A tool with no grants gives nobody anything, which
is why writing its file first is safe. Afterwards the gateway opens the broker's
account link form for the approver, watches it with the tool setup watcher
(`GatewayToolSetups.after_proposal_links`), and holds the decision message to
the waiting task until every form ended; the resumed `propose_access_change`
then reports which links exist. Rationale: the task resumed before the link
existed and called a tool still refused for a missing account. The hold lives
in gateway memory; after a restart the Inbox loop sends the message and the
tool reports the links as they are.

People hold one team role each (`owner`, `admin` or `member`) in the gateway's
membership records, read again at every decision, so a role change applies at
the next one. An owner is also an administrator everywhere roles are checked;
only owners change who is an owner, and the store refuses a change that would
leave the team without an owner or an administrator. These rules, and the
gateway's checks for adding, listing, inviting, disabling and removing people,
live in one place (`access_control/people.py`); the instance's user is created
as the first owner. Disabling a person is reversible: they keep their
membership, folder, memory, conversations, grants and linked accounts, and
their work ends: the manager cancels their running and queued task trees,
stops their objectives and deletes their schedules, and the gateway withdraws
access requests waiting on them. Removing a person (`/user remove`) marks their
membership removed (the row stays for history) and also deletes at once their
task records, objectives, chat sessions, sign-ins, grants for their work and
grants they gave, personal account links with their stored credentials,
their GitHub, Google and X sign-ins and the broker's credentials folder for
them, requests, notices and sandbox; their folder, personal memory, own knowledge pages and conversations
are held out of every agent's reach for `gateway.users.removed_data_retention_days`
(the folder under `state/held_people/`, the rows in `person_held_rows`) and
then purged by a maintenance run, unless `/user restore` (or adding them back
the usual way while held) brings them back. The gateway does its part after the
identity change commits (`assistant_gateway/application/person_lifecycle.py`,
`assistant_gateway/person_departure.py`) and sends the manager a typed
`PersonStandingCommandV1`, which only a gateway-signed administrator context
can send; the manager acts on the standing in the people copy
(`custom_agents/person_departure_runtime.py`) and runs the same idempotent step
for every disabled or removed person at start and before each maintenance run.
Database cleanup resolves PostgreSQL tables and columns through the connection's
schema search path, just as its deletion and restoration queries do. Tables in
unrelated schemas therefore cannot change which records it finds or restores.
The runtime's own team schedules never leave with a person: they move to
another active administrator (`access_control.people.team_work_owner`). Their
per-person system schedules (memory review, objective work sessions, recovery
checkpoints, retries, after-action reviews) are deleted with them instead. Only a person who is active and holds an active
membership in the gateway's team resolves at any entry point (chat channels,
Web Chat, invites), and the gateway copies each change to the access store for
the scheduler (`assistant_gateway/people_directory_sync.py`). People block an
agent type, an agent, a person, a role or the whole team (the last three owners and administrators only) from a tool set, a
tool, one operation or a folder with a block grant (`AccessRequests.block`,
Cedar `tpl:block`), which wins over every grant, standard ones included, from
the next decision and is recorded in the access event log, so an administrator
can stop access at once; `/access block team internet` turns internet access
off. A block naming an operation the catalog no longer has (an update renamed
or merged it) blocks its whole tool (`access_control.stale_blocks`, applied in
`AccessService._evaluate`), and the gateway tells administrators about each
one at start, so a rename never lifts a block. Standard catalog functions are
withdrawn from Strands' startup registry as well as its dynamic registry;
unblocking restores them before the next model call. A person who may remove
the stale block can narrow it with `/access block ... --replace <block id>`:
the service checks the same subject and tool, keeps the expiry, and the store
inserts the replacement and revokes the old block in one guarded transaction.
Removal authority applies even while the old block stops the administrator's
own access. The team transfer converts the names its release renamed
(`team_transfer.access.RENAMED_OPERATIONS`) in both supported access formats
during review and application: version 3 exports existed before the action
tool merges, so a schema conversion alone would miss their old names. It reviews stale blocks against
accepted team-tool and active MCP definitions before writing the destination.
The `/access` chat command (`assistant_gateway/access_commands.py`) and
`assistant_cli access` share the same helpers (`access_control/operator_commands.py`)
and act within the person's own authority. The Web Chat **Access** page
(`assistant_gateway/access_map.py`, `onboarding_web/assets/access_map.mjs`) is
the access map: by agent type (TeamLead's standing access for the signed-in
person's work, then per type, and per scheduled job holding access, the
standard access by role, one row per tool entry grouped by whose work it
serves with its chain from `AccessService.chain`, and blocks; members see
their own work and everyone's, administrators all of it; a worker's lasting
access that no longer counts is not listed), by
person (members see only their own), by tool (operation levels and account
links, never credential keys), and files (shares by and with the person, and
data folders), each paged at 20 on the server. Its buttons call
`revoke_by_person`, `AccessRequests.block` and `unblock`, and the starter
profile, share and data folder services, as the signed-in person; the gateway
only chooses which buttons to show, and each action is decided again when it
runs. It is built by the access Inbox on the same access service, so the
gateway keeps one Cedar evaluator for people.

Platform changes (runtime settings, restarts, new specialists,
team skills, hosted MCP servers, managed execution services,
typing into SSH sessions and changes to schedules the runtime manages) are
staged as exact, digest-checked plans and applied with one tool,
`apply_change` (`access_control/changes.py`, `custom_agents/change_approvals.py`).
Agents never hold `full` access to platform tools (a standard Cedar guardrail),
so with authorization on a person approves the exact plan in the Inbox; the
resumed call then claims the approval once and applies the plan in the agent's
process. With authorization off the person's conversational agreement applies
it. One narrow class applies at once instead: a settings plan that only
switches an agent type's model (provider, model, reasoning, context window
and input settings; `custom_agents/model_choice.py`) is the `model_choice` change on
`fn:model_choice`, a tool only administrators hold (Cedar
`guard:admin-only-tools`), so it applies when TeamLead holds it for an
administrator's work (the Trusted profile gives it) and that administrator
gets a notice. The owner asked for it because model switches are frequent,
bounded and easy to undo, while everything else keeps the review. Staging a
model-affecting plan already tries each target model once
(`RuntimeLLMOverrideApplier.prepare_candidates` and `test_candidates`, owner's
decision 2026-10-10: try once, then ask), including instance-baseline `llm.*`
changes with the baseline's own endpoint and request settings. A failed test
stages nothing. With authorization off, a tested switch requires conversational
agreement (`confirmed=true`); the access-service decision remains authoritative
with authorization on. Model settings in `LLM_OVERRIDES.yaml` (per agent type or
the workspace `default:`) need no restart for any agent type. A plan explicitly
targeting the applying agent's own type switches that agent at once. A default-only
plan also reaches the applying agent at its next turn when it inherits the changed
setting. Every affected running agent reads the file
again at the start of each turn (`UniversalAgent.refresh_persistent_llm_override`,
through `model_failover_runtime.refresh_runtime_llm_selection`) and new agents
read it at start. They used to report a manager restart for other types, and
TeamArchitect asked people to approve restarts that changed nothing; the apply
result now says when the change reaches running agents (`running_agents`), and
`get_runtime_configuration_context(agent_types=[...])` shows the saved values,
because an idle agent's status report keeps the old values until its next turn.
The named-type view resolves the saved instance configuration on each call,
including when the caller omits the full file snapshot. An unreadable file
returns an error instead of startup values. Failover selection keeps explicit
null override fields distinct from missing fields, so disabling a reasoning
summary or clearing another optional model setting also takes effect next turn.
A sparse agent-type entry may clear an inherited setting without naming a model:
the loader preserves explicit nulls and inherits only omitted fields. This keeps
saved values, startup and next-turn selection consistent instead of treating
an intentional clear as an old bootstrap placeholder.
A temporary failover lease on the primary reads that profile too, while a
lease on a fallback keeps the fallback's profile and its existing lifetime.
Settings in `instance_config.yaml` (the `llm.*` baseline, temperature, output
limits, streaming) are read at start and still report a manager restart. Applying tests again before any write, because the
endpoint, key or model may change in between, then checks the source hash again
so an operator edit made during the test is refused. Window notices use the same
target profile that the test used. Settings saves preserve explicit null request
constraints in the model catalog, since null disables an inherited model option.
The services
only describe and apply plans; the approval they record is
the access request that a person decided. An approval never bypasses the
owning component's validation, and installing something grants no access to
use it: every call is still decided by the access service. TeamLead's first
grants for a person's work come from that person's starter profile
(`access_control/starter_profiles.py`).

`apply_change` returns the approver's note from the Inbox with every decided
outcome (applied, denied, or refused because the approved plan no longer
applies), as `request_access` does. A manager restart (scope `manager` or
`all`) ends the process running TeamArchitect, so the task
that asked for it can stop before it reads the result, and the task's model
conversation is kept only in memory. When the task resumed, it saw only the
person's message ("approve the restart") and asked for the same restart again,
in a loop. So the restart receipt names the task that asked
(`source_task_id`) and the approver's note. The agent publishes those details
with its approved request in one atomic file write. The supervisor records
the dispatch receipt before it removes the request or stops a process;
preparing an unpublished request never counts as dispatch. Status is
`requested` while the request waits and `dispatched` after supervisor acceptance.
An accepted request retained across a supervisor interruption is not replayed.
Supervisor request schema 4 requires the task and note for agent requests;
older pending request files are refused rather than converted.
Restart recovery (`custom_agents/runtime_recovery.py`,
`tell_task_about_its_restarts`) reads receipts for every recovered unfinished
TeamArchitect task, including waiting and queued tasks. It adds a resume
directive saying what happened, when, and what the approver wrote, with an
instruction to check the result and report it instead of restarting again.
All runtime continuations append their instructions, preserving unread restart
outcomes until the next model invocation consumes them. Duplicate notes are
matched at their boundaries, so a shorter requester correction is retained.
Interrupt replies include their steering event id, keeping distinct replies
separate even when one ends at a paragraph boundary inside an earlier reply.

An installed MCP server is one team tool, `mcp:<name>`, in the `mcp_servers`
tool set that members hold as a standard set (`access_catalog/catalog.py`,
`access_control/standard_grants.py`), so it follows grants like every other
tool. Installing one is an item of an access proposal
(`AccessProposalV1.mcp_servers`, `access_control/proposals.py`):
`mcp_connections(action="propose")` records the inert connection and proposes it in the
same call, with default rows from `access_control/mcp_servers.py` (the
proposed agent types and TeamLead, which may hand it on, get `full` for the
requesting person's work). An administrator approves once; the gateway's
applier activates the record, as that person's approval with the request id,
and stores the grants in the same store transaction as the rest, putting the
pending record back if that fails. `full` on the tool covers every operation,
including unconfirmed ones (which need `full`) and ones the server adds later,
so agents never come back for each function; confirming lower levels is only
for narrower grants. An administrator can also approve a pending server in
Settings, which gives its target agent types `full` for everyone's work. There
are no per-agent-type attachment records: the broker lists an active server's
tools for any agent type the calling process may act for, decides each call
with the access gate for the agent and the task's person (no rule reserves a
server to the person who proposed it), fills credential slots from the calling
person's own account link, then the team's while the team shares the server, and returns a refusal's reason,
tool, operation, level and next step as the JSON body of its 403, which the
agent's MCP client gives to the model (`custom_agents/sdk/strands/mcp.py`).
Rationale: installing a server is a team decision, like a new web API tool,
while using it is each person's decision for their own work.

Credentials are chosen by account links, not by agents. A link says which
stored credential one tool uses for the team or for one person, and where each
credential field goes (a header, query value or environment variable). The
broker uses the person's own link first, then the team's, and sends the value
only to the tool's hosts or MCP connection. People create links in the broker's
labeled form (`/link <tool>` in chat, or a setup page TeamLead opens); a link
grants no access by itself. The form (`secure_tool_broker/account_links.py`) either links
a stored credential that has every field the tool needs or saves a new one the
person types, through the broker's own store path (`collect_credential`, in the
team's collection for a team link and the person's for their own), and links it
in the same submission, so one form finishes the job. A new credential gets a
key made from the tool and owner and a number when that key is taken, so it
never replaces a stored credential. The form's **Used by** line is read by the
broker from the grant store it already holds (agent-side grants in force that
reach the tool for the owner's work). It is shown for information only, and the
access service still decides every call. The broker reads it itself rather than
taking it from the gateway, so the form request contract stays the same and
every caller sees the same line.

Whether the team's account serves everyone is the team's choice per service
(`access_control.account_sharing`, owner's decisions 2026-10-08). A tool that
takes both a person's and the team's account (`ToolDefinitionV1.sharing_choice`)
is either shared, so the team's account serves anyone without their own (the
rule before the choice existed), or private, so the team's account is never
used, not even for work that serves no person. The choice is one
`AccountSharingV1` row per service in the access store; without one the
tool's `default_sharing` applies (shared when it names none). Services that
act as a person start private (X), services that supply a resource start
shared (search, model downloads). GitHub and Google use only each
person's sign-in and the GitHub API (`http:github`) takes only each person's
own link, so they are always private. Administrators decide
(`access_control.people.account_sharing_refusal`) with `/accounts share|private
<tool_id>` in chat (`assistant_gateway/account_sharing.py`); each change is in
the access event log and sends administrators an Inbox notice. `/accounts`
and the Web Chat's read-only **Settings → Accounts** tab (`GET
/api/web-chat/accounts`, `onboarding_web/assets/accounts_view.mjs`) are built
from the same rows (`ServiceAccounts`, one gateway instance for both), so the
chat list and the tab cannot disagree; changes stay in chat, where the
administrator check and the notices already are. The Accounts route runs the
whole read through `website_handlers.off_loop`, including the person's current
membership, so slow identity or account storage cannot stop the other chats
that share the gateway's event loop. The chat
commands people are told to send (`/link`, `/accounts`, `/setup`, `/auth model
me`) are named once in `secure_tools.chat_commands`, which the gateway, the
broker and the agents' runtime all build their hints from. Everything that
picks or reports an account asks the same `TeamAccountSharing`: the broker's
gate drops the team from a private tool's owners, the link form refuses the
team's account for it (`account_link_team_private`), the setup status reports a
team account only while it is used, TeamLead's setup check offers only the
person's own link, and the Access page says so. A team link that becomes
unused is kept, so sharing the service again needs no new link. Rationale: an
administrator can keep personal accounts (code, mail, posts) to their own work
while still sharing cheap resources such as search keys, and nothing falls
back to someone else's account without that decision.

A person an administrator adds is shown their first-time setup on their first
chat message (`assistant_gateway.personal_setup`): their own model when they
may not use the team's model keys and saved none, and their own account for
every service the team keeps private that someone in the team already uses
(a linked account of any owner, or a sign-in), so a team that never uses
GitHub does not send every new person there. The add reply promises the list
only when the setup was actually recorded as due. Trusted gateway code shows it, because
a person without a model cannot reach an agent. Adding someone records
`personal_setup_due` in `gateway_user_events`, and the message path
(`GatewayInteractiveSubmissionService`, through `GatewayPersonalSetupGate`)
shows the list once (`personal_setup_shown`): as the reply, holding the
message back, when the model is missing; otherwise posted in the chat before
the message goes to the team. Later messages are never held, because a
personal key the person saved for the team's endpoint also works and only
the broker can see it; the broker's refusal names `/setup`, which lists what
is left at any time. People added before this existed have no setup due.
Rationale: the owner wanted people to provide their own credentials first
when the team keeps them private, without agents and without blocking anyone
the broker would let work.

The web search services follow the same rule. Exa, Brave Search and the
instance's Crawl4AI server are web API tools (`http:exa`, `http:brave-search`,
`http:crawl4ai`) in the `internet` set, and their keys are account links, never
environment variables (startup refuses `EXA_API_KEY`, `BRAVE_SEARCH_API_KEY`
and `CRAWL4AI_API_TOKEN`). The research tools (`custom_tools.exa_tools`, the
Brave artifact tools, `capture_web_screenshot`) send each request through the
broker without a key (`custom_tools.broker_web_requests`), and the broker adds
the linked key. Crawl4AI's address is configuration, so its catalog entry takes
its host and port from `web_services.crawl4ai.base_url` and matches only that
exact address. Because it is the team's own server, only the team's token
opens it: the catalog requires `link_owners: [team]` of every tool whose
address is a setting, so the Crawl4AI web tool is always shared and no
sharing choice can stop its screenshots. The Crawl4AI MCP connection
(`mcp:crawl4ai`) is an MCP server like any other and keeps its choice (shared
by default), because the catalog cannot tell the team's own MCP server from
an outside one. Onboarding cannot reach the broker, so it leaves the Exa and
Brave keys it was given in a private file under `state/`, with the Secrets
page's sharing choices; at start the gateway records the choices once (they
leave the file, so a restart never undoes a later `/accounts` change, and the
keys wait if they cannot be recorded) and hands the keys to the broker on a
gateway-only route, each for the team when the team shares the service and as
the onboarded person's own when it is private (the broker checks a team link
like the form does), and then deletes the file
(`assistant_gateway.onboarding_account_keys`). The page's rows come from the
catalog (`onboarding_sharing_services`), and the server refuses a choice for a
service the team cannot choose (`access_control.account_sharing.onboarding_choices`). Rationale: keys in the agent
processes' environment were readable by any code they ran and could not follow
a person's own account or a block.

`/link <tool_id>` is the one chat command for a tool's account. For a sign-in
tool it starts the person's own sign-in in the broker; for a linked-credential
tool it opens the link form; for a tool with no account the broker replies that
there is nothing to link. The sign-in tools are listed in
`secure_tools/auth_methods.py` (`SIGN_IN_TOOLS`: `cli:github`, `cli:google`,
`cli:x`, each with its sign-in methods and whether it also takes a team link).
Rationale: the list sits next to the broker's sign-in method names rather than
in the tool catalog because the broker needs code for each sign-in
(`secure_tool_broker/auth_flows.py`, `secure_tool_broker/x_sign_in.py`);
`tests/test_access_catalog.py` keeps the list and the catalog's `account` values
in step. The gateway watches each page `/link` opens and posts one message in
the chat that ran it when the page ends (linked, closed, expired, or not
confirmable), through its normal delivery path; Web Chat removes the `/link`
card when that message arrives
(`assistant_gateway/application/link_results.py`). It uses the same broker
watcher as forms an agent task waits on
(`assistant_gateway/application/broker_session_watch.py`), so both follow one
rule for how long to wait and what an unreadable page means. Watches live in
gateway memory; after a gateway restart no message follows for a page that was
still open. `/auth` keeps only what is not a tool's account: sign-in status, the
team's sign-in apps (`/auth google team_client`, `/auth x team_client`,
administrators only, granting nothing; watched like `/link` pages), and the
password managers the broker reads saved credentials from (Bitwarden,
1Password, KeePassXC, Vault).

**Only TeamLead asks the person for credentials (2026-10-07).** Specialists get
a turn answer without the credential-form and signup actions
(`custom_agents/structured_llm_messages.py`, `ModelWorkerTurnDecision` and
`CompatibleWorkerTurnDecision`, chosen by `TurnRuntimeSettings.is_team_lead`).
If a model answers in prose, its structured-output recovery instructions name
the selected turn tool and the same role's allowed actions, so recovery does not
ask specialists to use credential actions their schema rejects.
A specialist that needs a credential, sign-in or account escalates
(`manager_escalation`, `needs_user_input`); TeamLead sets it up with
`set_up_tools` or its credential form and answers. TeamLead's credential forms
and signups go to the person's chat even while it works on an escalation
(`message_routing.requester_bound_primary_route`), like setup pages. A request
that still reaches an agent (another release, a legacy output path) is answered
with a non-terminal `SecureCredentialCollectionResult` telling the specialist to
escalate (`task_message_runtime.refuse_credential_request_from_delegate`,
`event=delegate_credential_request_refused`). Before, specialists were offered
these actions, their requests reached the delegating agent, which dropped them as
an unhandled type, and the specialist waited until the watchdog failed its task.

**Effortless tool setup (owner's decision 2026-09-28).** TeamLead sets up the
tools a request needs itself, in one trusted call, instead of telling the
person which command to run or asking whether to start:

1. `set_up_tools` (`custom_tools/providers/tool_setup.py`, `fn:tool_setup` at
   write, standard for the team lead in both authorization modes) is the only
   way to open a setup page. Rationale: on a deployed build TeamLead ran the
   old check and then asked "If you want me to... I can start the access and
   sign-in steps" instead of starting; the approval cards and setup pages are
   the consent points, so asking first is a wasted round trip, and a routine
   of six model steps was followed differently from run to run. The model
   now makes one call; trusted code does the routine the same way every time.
   The `check_tool_setup` tool and the `tool_setup_request` turn action are
   removed: nothing the model writes opens a page any more.
2. The check (`custom_tools/tool_setup_check.py`) reports per tool the access
   of TeamLead, a named worker type and the person (the access service's
   side-effect-free `holds`), and, from the broker's `tool_setup.status` action
   (`secure_tool_broker/tool_setup_status.py`, catalog tool
   `procedure:tool_setup`, reached only through `fn:tool_setup`), the person's
   sign-in with the account name, their own and the team's links, and the
   team's sign-in app. The person always comes from the signed task context.
3. The plan (`custom_agents/tool_setup_runs.py`) turns the gaps and the tool's
   catalog `setup` data into pieces with no per-tool code: access requests,
   the worker's access, sign-ins, account links, the team's sign-in app, and
   steps done outside Universal Agents (`ToolSetupSpecV1.external_steps`),
   ordered by the tool's `prerequisites` and by the rule that a sign-in waits
   for the team's sign-in app and an account link for the tool's outside
   steps. A piece only an administrator can do, for a person who is not one,
   ends the call before anything is asked (`needs_administrator`, plain
   lines). Setup data is catalog metadata: shipped YAML, an operator's
   instance file, or an approved access proposal (`ProposedHttpToolV1.setup`),
   checked strictly (bounded text, https links without sign-in details or
   key-like query values, nothing that looks like a secret) and shown on the
   review card in the exact words people will see.
4. The run (`ToolSetupRunV1`, stored in `tool_setup_runs` in the manager's
   database by `storage/tool_setup_run_repository.py`, compare-and-swap
   versions) holds each piece's state. The call publishes one access workflow
   for two or more pieces (or any outside step, which the person marks done on
   the workflow page), with TeamLead's `extra_steps` and a final work step;
   asks for TeamLead's missing access (found again by the call's id); and
   pauses the turn with a local Strands interrupt (`tool_setup_run`), which
   `task_steering_runtime` keeps local like access requests and records as the
   task's blocker. `custom_agents/tool_setup_run_runtime.py` then opens every
   page that may start (one `ToolSetupRequestV1` per page to the gateway's
   route for the person's chat, all at once), hands the worker its access with
   `AccessRequests.give` once TeamLead holds it, applies each
   `ToolSetupResultV1` and access decision, offers an expired or unwatched
   page again (at most three per tool and kind), stops at a closed page or a
   denial, and resumes the turn: the live interrupt is answered, or after an
   agent restart the task resumes from a directive with the same compact
   result. The call returns per tool `ready` and `what_happened`.
5. The gateway (`assistant_gateway/application/tool_setup.py`) opens the
   matching page through the same broker calls as `/link` and `/auth`, and
   several pages of one task side by side (a sibling page of a task that
   already waits opens too, `credential_handoff`). The run, not the gateway,
   announces its workflow: before the first page opens it sends one
   `UserNotification` to the person's chat with the page's full address
   (`workflow_notice`, `event=tool_setup_workflow_announced`, once per run).
   Rationale (a live run, 2026-09-28): a link added to a setup reply's text was
   never shown, because Web Chat renders a setup card from its description and
   the transcript from its receipt, and channels such as Telegram show only
   text; a notification is shown the same way in every channel. Steps trusted
   code changes carry its own event codes (`access_requested`,
   `access_approved`, `setup_*`), never `agent_reported_*`
   (`AccessWorkflowService.trusted_progress`), because that code means the
   agent reported it. An `external_step` shows the catalog's words
   (`assistant_gateway/tool_setup_guide.py`), opens no broker page, never
   expires, and ends when the person marks it done on the workflow page
   (`POST .../workflows/{id}/setup_done`, owner only). The gateway still
   enforces the page limits (`TOOL_SETUP_OFFER_LIMIT`, no page after a
   declined one, administrator-only steps), because it records every page.
   Each wait is a row (`gateway_tool_setups`, `ToolSetupRecord`), finished with
   a conditional update so exactly one result reaches the task whether the
   page's watcher, the person's own `/link` or `/auth ... team_client`
   (`link_results`), the Mark Done button, or start-up recovery ends it. After
   a gateway restart, pages still waiting are reported as unwatched (and
   offered again); page addresses are never stored.

5. Last known states instead of model re-checks (owner's decision 2026-09-28).
   Trusted code keeps one `ToolSetupStateV1` per team, person and account tool
   (`access_tool_setup_states` in the access store, which the broker, the
   agents' runtime and the gateway all open in both authorization modes;
   `access_control/tool_setup_states.py`): `ready` with how and when it last
   worked, `needs_setup` with the missing step and who can do it, or `unknown`.
   The broker records every account tool call (success means ready, written at
   most hourly unless it changes; a setup error code means needs setup; other
   failures change nothing; `secure_tool_broker/tool_setup_tracking.py`) and
   every setup status check; the gateway records finished setup pages and the
   person's own `/link` and `/auth ... team_client`, and a team-wide fix (the
   team's sign-in app or account) turns other people's matching gaps into
   `unknown`; `set_up_tools` records the person's missing access. TeamLead
   gets the task person's states as one pinned context line
   (`custom_agents/tool_setup_runtime.tool_setup_context`, only for the
   verified person) and uses a ready tool without checking. The broker's own
   loop re-checks ready states nobody used or checked for
   `security_layer.tool_setup_recheck_days` days (default 30, 0 off;
   `secure_tool_broker/tool_setup_recheck.py`) and sends one Inbox notice per
   problem. Rationale: a model re-checking before every use costs a call each
   time and can decide differently from run to run; a state trusted code keeps
   is cheap and deterministic, and the periodic check covers sign-ins that
   expire while nobody uses them. It runs in the broker because only the broker
   can check sign-ins, so it needs no new route or signed task. States are
   deleted with a removed person's access records.
6. One workflow for several pieces (owner's decision 2026-09-28). When a
   request needs two or more pieces resolved, `set_up_tools` publishes one
   access workflow; with one piece it just opens the page. Each step names its
   expected setup (`tool_setup`: tool, kind, owner, and an outside step's
   `step_id`; checked against the catalog when the plan is published) or tool
   access. Trusted code, not the model, keeps the steps current, the same way
   access requests are linked (`custom_agents/access_workflow_setups.py`):
   each page the run opens is linked to the one step that expects it (open,
   or pending with its prerequisites done, because nobody marks a setup step
   running before its page opens), in the task's workflow or one whose task
   carries the same verified signed task context; each result updates it
   (completed marks a pure setup step done once all its pages are finished;
   expired keeps it waiting and fails it after the third page; declined
   blocks it like a denial; the person's own `/link` answers the waiting
   page's id, so it marks the step too), and the run completes an access step
   once its access is approved. A page that matches no single step is kept on
   the workflow as not on a step and logged (`event=workflow_setup_unassigned`).
   Rationale: one plan the person can follow beats several unconnected pages
   in chat, and the model does not have to judge when a plan is worth it or
   remember to update it; a single piece stays a one-click page because a
   workflow would only add a click. The links live on the workflow record in
   the manager's storage, where the verified task context is known; the page
   reads them and polls, so it updates by itself.

**Installed CLIs (owner's decision 2026-09-28).** A service's command-line tool
can be added as `cli:<name>` through an access proposal (`ProposedCliToolV1`,
`custom_messages/contracts/cli_tool_v1.py`), with no code per CLI; TeamLead
prefers it to a web API tool because the model sees one compact provider,
each command has its own level, and the broker keeps the token hidden.

- Provenance: the program lives in the team workspace
  (`team/tools/<name>/<file>`), where agents put it (InternalImprovementAgent
  builds or downloads it). The administrator's approval pins its SHA-256; a
  proposal may also name an https download that the gateway fetches at apply
  time (size and time limited, https-only redirects, one member of a
  `.tar.gz`/`.zip`), checks and writes without following links
  (`access_control/cli_binaries.py`, `access_control/proposal_clis.py`); any
  failure removes what was written, so nothing is applied. Update = new
  proposal.
- Catalog: an instance file carries the entry and its `installed_cli` data;
  the loader accepts `cli:*` instance entries only with it, builds the command
  table (`cli_tables.policy_from_data`), and makes the name a `secure_cli`
  provider with one `exec` action (a name that is already a provider is
  refused). Levels come from the table; unlisted commands need full. Providers
  are resolved from the current catalog in the broker
  (`secure_tools.registry.set_provider_resolver`) and the agent's
  `secure_cli` (`secure_tools/installed_clis.py`).
- Execution (`secure_tool_broker/installed_clis.py`): the argument list passes
  `cli_passthrough.check_installed_command_line` (the gate decided the level of
  the same list); the workspace program is copied with `safe_open` into a new
  private run folder and the copy's digest checked on every run, since the
  workspace is agent-writable (a mismatch or a link is refused and logged,
  `event=installed_cli_program_refused` with both digests); the credential of
  the link the gate chose (`ToolDefinitionV1.link_owners`: person, person then
  team, or team) goes into an environment variable or a file in the run
  folder (`InjectionPlacement.FILE`), never argv, and its values are removed
  from the capped output. Rationale for the sandbox: the broker holds every
  person's credentials, and a pinned digest proves which file runs, not that
  it is harmless, so the copy runs inside the same Landlock launcher agents'
  code uses (`custom_tools.code_sandbox.prepare_sandboxed_command` with the
  run folder as its only writable folder), always required: it reads only
  system folders and its run folder, so the broker's files and every other
  credential stay unreadable. The network is open because the program must
  reach its service; Landlock limits TCP by port, not host, so hosts are shown
  for review but not enforced. Without Landlock the run is refused
  (`event=installed_cli_sandbox_unavailable`).

A credential collection form ends its task with a fixed answer only when storing
or deleting the credential was the whole request; a request that sets
`continue_task` (collect, then link, then use) resumes through the model.

GitHub and Google Workspace (`cli:github`, `cli:google`, account mode
`personal_login`) use each person's own sign-in instead of links:
`/link cli:github` and `/link cli:google` store it in the credential store as
that person's, and the broker runs `gh` or `gog` only for a request that works
for that person. They have no team account. Each
`gog` command runs in a new private folder under the person's broker credentials
folder with an allowlisted environment and is deleted afterwards
(`secure_tool_broker/google_workspace.py`), because gog keeps its accounts and
keyring in its home folder and a shared home let one person's request use
another's Google account. People sign in to Google through the team's OAuth
client, which an administrator stores with `/auth google team_client`, unless
they pasted their own on the sign-in page.

X (`cli:x`, account mode `linked_credential`) uses each person's own sign-in
first and the team's account link as a fallback while the team shares X (it
starts private, `default_sharing: private`, because it posts as the account). `/link cli:x` runs X's OAuth
2.0 authorization code flow with PKCE (`secure_tool_broker/x_sign_in.py`)
through the team's X app, which an administrator stores with
`/auth x team_client` in the team's part of the credential store. The browser
returns to the broker's `/auth/x/callback` page (under the broker's existing
public `/auth/` path, so proxies need no new route) or, with a `localhost`
callback, the person pastes the address. The broker stores the result under the
person in the same shape the X executor reads, and saves every refresh.
`/link cli:x team` links the team's shared account (`oauth_json`); personal X
links are no longer created or used, because a person's own X account is their
sign-in. The access gate hands the X executor only the team's link, and only while the team shares X; the
executor uses the person's sign-in when there is one, then the team's link, and
otherwise fails with `x_account_missing`. Rationale: a person's posts go out
under their own account without anyone pasting a hand-made token bundle, while
a team that wants one shared account keeps it. Neither a sign-in nor a link
grants access; grants still decide every call.

No tool token comes from environment variables; startup
refuses the old ones (`runtime_settings/security.py`), including a Google
service-account key file (`GOOGLE_APPLICATION_CREDENTIALS`), because an
environment credential would be one team-wide identity for every person.


Schedule changes go through one path (`custom_agents/system_schedule_changes.py`,
built on `custom_agents/schedule_changes.py`). A person's own schedule is
changed at once: the access hook decided the tool call (`fn:schedules`) and the
tool checks the schedule belongs to the person in the signed task context
(`access_control/ownership.py`); the schedule store needs no permit for it. A
schedule the runtime manages (memory review, heartbeat, Objective work
sessions) is never changed on an agent's say-so, because the store requires an
exact permit for its control state (`custom_agents/schedule_management.py`).
The tool stages the exact change (`system_schedule` on the platform tool
`fn:system_schedules`), a person approves it in the Inbox (its agreement with
authorization off), and `apply_change` rebuilds the change from the stored
request, checks that the schedule and the change are unchanged, and issues the
permit in the approval's mode. Objective tools issue `held_access` permits for
the Objective's own work-session schedule; gateway commands on schedules the
runtime manages need a person who could approve such a change, decided by the
manager with the person and roles from the command's gateway-signed context
(`manager_control_runtime._may_change_system_schedules`, the same rule as the
gateway's `PersonChangeCheck`, which the gateway keeps for showing controls).
The manager awaits this storage and Cedar lookup on its bounded
`TaskOperationRuntime` lane before checking the record digest or changing the
schedule. A slow lookup leaves the event loop responsive; a cancelled command
joins the lookup and stops before mutation.
Every gateway-to-manager command (schedules, memory, topics, task and objective
controls, person standing) names its person only through that signed context
(`manager_control_runtime.verified_command_person`); the commands carry no
person, roles or authority flag. Until 2026-10-07 the manager trusted a person
and roles written in the command body, checked only against an unsigned copy,
and a "may change system schedules" flag the gateway set. Scheduled
workers get their tools like any agent type: `prepare_scheduled_job_access`
lists what the worker type and the owner lack, `authorize_scheduled_job_access`
hands on what the team lead may give, and each call at run time is decided
per call.

Authority must also last as long as the work. A grant tied to a creating Task
ends with that Task, so recurring jobs, service reconciliation, and later SSH
interaction need authority that remains valid when each effect occurs.

Credentials stay outside agent processes and ordinary gateway storage. The
secure broker owns the credential catalog, secret-free references, provider
sessions, hydration, use, and output redaction. Sealbox or another configured
provider stores secret material. Agents receive structured results, not raw
passwords, tokens, keys, or credential paths. Paging bookmarks such as
`nextPageToken` stay visible: they only ask an API for its next page and are
useless without the caller's sign-in, and hiding them stopped agents from
reading past the first page. Only the final `token` word in a bookmark name
is exempt from secret-field redaction. Other secret markers, such as
`password` in `passwordPageToken`, still hide the field, and bookmark values
still pass the credential-shape check. The broker derives identity from
the signed task context and chooses the credential from the tool's account link
on every use.

## Deterministic execution and managed processes

Agent-produced code crosses a promotion boundary before it can become durable
automation:

1. Source bytes become a content-addressed artifact.
2. A trusted profile compiles or checks the source in a bounded environment.
3. A build attestation binds the source, toolchain, profile, and output.
4. Promotion creates an immutable revision.
5. Jobs and services refer to that revision instead of mutable source files.

Python validation runs in a separate resource-limited compiler process. Rust
compilation runs in an OpenShell sandbox. Execution profiles define runtime,
resource, and placement policy independently of source code.

One-shot, interval, and cron jobs use database time, deterministic occurrence
identifiers, leases, and fencing tokens. A restarted controller can recover or
adopt work without allowing an earlier controller to commit a stale result.
Adapters dispatch finite runs to local processes, OpenShell, or Kubernetes.

Long-running workloads are desired-state Kubernetes services. Managed services
currently require Kubernetes. They receive verified promoted artifacts and can
use a separate writable volume for application data. An agent-owned background
process inside an ephemeral container is not the durability mechanism.

Managed MCP servers use the service lifecycle. A healthy service creates only
a pending connection, which an administrator installs like any MCP server (in
Settings or through an access proposal); calling one of its operations is
still checked on every call.

SSH uses a brokered OpenSSH PTY against an operator-pinned host, user, host key,
and key material (`credential_binding_id`). Agents interact through a session id and receive
bounded output. Local PTY state is lost when the broker restarts. An
operator-configured remote `tmux` session can support explicit reattachment.

Jobs and SSH sessions use the same access model. They are
`write` operations of `fn:execution_control`, decided by the agent's access
decision hook; each activation (`ActivationRecordV2`) records who held access
(agent, verified person and roles, catalog operation) and how it was approved
(`ChangeApprovalRecordV1`). Before every run, service reconcile and SSH call,
`runtime_hosting/authorization.py` rebuilds the exact resource from trusted
records and decides again with the access service: held access must still be
held (only access that outlives the creating task counts for jobs, which run
unattended), so revoking a grant stops the next run. Managed services and
typing into an SSH session are exact changes a person approves
(`managed_execution_service`, `execution_ssh_interaction`, staged in
`custom_agents/execution_changes.py` and applied with `apply_change`); their
approval record is the authority. Typing approvals live in their own insert-once
table because the broker rewrites the session record on every lease renewal.
The controller builds its access service like the broker does, from the manager
storage and the Cedar evaluator, and refuses to start while records saved before
this model remain (`assistant_cli.py execution-retire-old-records`).

## Gateway, tools, and models

The gateway gives people the places to decide; it never decides access itself.
The access Inbox (`assistant_gateway/access_inbox.py`) lists access and change
requests with notices, announces each waiting request once in a chat where the
person can decide it (the delivery event carries the `access_request_id` and the
review choices; a staged change is announced with the one-line summary its
staging service writes from the plan), and records decisions through the access service as the
signed-in person. Telegram and Discord show those choices as buttons
(button contract in `assistant_gateway/application/access_reviews.py`, decisions
in `assistant_gateway/application/access_review_service.py`): a press carries only the
request id and the choice, and the gateway decides with
`GatewayAccessInbox.decide` as the person linked to the pressing chat account.
Adapters running in a separate adapter host send presses with the protocol
command `access.review_decision`. Web Chat shows the Inbox's review card in
the conversation (`assets/chat_access_review.mjs`): it reads the request by id
from the transcript row's `access_request_id` and decides through the Inbox
decide route as the signed-in person. Other channels show the Inbox link. Because
the button holds no secret and no state, it keeps working after a restart, and
the person's current roles decide what the press may do.

Every web chat route, chat command and Settings view names its signed-in route
with `GatewayRouteContextV1` (`assistant_gateway/application/route_context.py`);
`SignedInPerson` turns that route into the person it acts for. People's
controls for MCP connections and hosted servers
(`assistant_gateway/mcp_controls.py`) and for installed-procedure proposals
(`assistant_gateway/installed_procedures.py`, listed, added or rejected in
Settings → Team) then check with `PersonChangeCheck` what that person may
change, from their current team roles. `assistant_gateway/team_presence.py`
keeps the live team presence in memory. Every live Web Chat page (the
two-second event stream, `website_handlers/conversation.py`) carries it as
`team_activity` for registered people, and one browser store
(`assets/team_activity.mjs`) hands that copy to the sidebar activity mesh,
Settings → Team and the Tasks view. Each used to read presence on its own
timer (4 and 10 seconds); now an open page reads it once, at the stream's
pace, and the three views always agree.

The Inbox pages notices on the server (`AccessRepository.notice_page`, 20 per
page, unread only by default) and marks them read for one person by id or all
at once (`POST /api/web-chat/access-inbox/notices/read`; only notices that
person can see are marked). The Inbox tab shows one count, requests the
person can decide plus unread notices, from
`GET /api/web-chat/access-inbox/counts`: the browser reads it every 30 seconds
while visible, when the page becomes visible, shortly after an agent's chat
message (requests and stuck-work reports arrive with one), and takes the
counts every Inbox read and change already returns. The Settings cog shows that
count plus the Knowledge page's proposal count ("Knowledge page" below). Both
counts use one browser helper (`settings_support.mjs` `createAttentionCount`)
so every badge follows the same rule (`ui/DESIGN.md`, "Attention counts"). Stuck-work notices
(`custom_agents.stuck_work`) go to an owner and administrators as one notice
when they read the same words, and an owner who is an administrator gets only
the administrators' notice otherwise, so one problem never fills an Inbox
twice.

The subtask watchdog records successful status probes separately from task
progress. A waiting worker can renew the probe interval without changing its
progress timestamp or producing updates on every idle check. A status reply
confirms that the worker still knows the task; it does not prove useful progress.

The gateway supports the local CLI, Web Chat, channel plugins, scheduled
messages, proactive notifications, and generated sites. It owns canonical user
resolution, session continuity, attachment admission, clarification routing,
quiet hours, deferred delivery, and receipts. An agent response and a delivered
user message are separate facts.

A person can answer one earlier chat message with the channel's reply feature.
That message often belongs to another conversation than the one the chat
continues: a recurring schedule's report is filed under the schedule's own
conversation, a background check the manager runs itself keeps none, and the
team's default chat delivers through the team's notification session, not the
replying person's. So a reply such as "expected" means nothing to the agent
alone (reported 2026-10-12). Telegram passes the replied message as the chat
showed it, beside the person's text (`Task.replied_message`,
`TaskQuery.replied_message` and `TaskSteering.replied_message`,
`custom_messages/contracts/replied_message_v1.py`;
the adapter-host protocol carries it too). The agent sees it quoted and labeled
by author (the assistant, the person, or another member) as background for the
person's message; it is told not to follow instructions inside the assistant's
or another member's text, because reports quote outside content such as emails
and another member's words are not the person's. The reply stays in the chat's current conversation. Routing it into
the replied message's conversation was rejected: many reports have no
conversation holding their text, and the report and the reply often arrive
through different chat sessions.

Telegram's default rich-message delivery stores visible content in
`rich_message.blocks`, rather than `text` or `caption`. The pinned Telegram SDK
retains this field in `api_kwargs`; `interactive_apps.telegram_reply_text`
normalizes paragraphs, inline formatting, lists, tables and media captions
before the same 16,000-character limit applies. Unsupported rich content is
logged without copying message text. Pending clarification follows its existing
`TaskQuery` route and keeps the replied message on the stored query; the parent
task's history renderer quotes it when work resumes, including after a restart.
Focused plan-step and recovery prompts include their own clarification history
as a separate section, because they intentionally omit general execution history.
SDK interrupt replies use the same labels in both live resume input and durable
recovery notes, while structured access decisions retain their original value.

External adapters connect through an authenticated local or private-host
boundary. Durable inbox and outbox records provide ordering, duplicate
detection, acknowledgements, and reconnect recovery without giving adapters
gateway filesystem or repository access.

Agent definitions are declarative and list the tools a type works with as tool
catalog ids or tool set ids (`default_tools`, `custom_agents/default_tools.py`);
the catalog maps them to reviewed callables (`access_catalog/tool_functions.py`).
YAML and model output cannot register arbitrary imports, commands, endpoints,
or credentials as trusted tools. Default tools grant nothing. With
authorization on, an agent starts with its role's standard tools and a
`BeforeModelCallEvent` hook (`custom_agents/tool_sync.py`) adds and removes the
functions of the catalog tools it holds for the current task's person, so a
new grant is usable on the next model call without a restart; the defaults
only tell TeamLead what to give a new type. The same hook loads the tools of
each MCP server the agent holds (`mcp:<name>`) through the broker as dynamic
tools and removes them when the access ends (`custom_agents/mcp_server_tools.py`).
With authorization off the role's standard tools and the default tools are the
agent's tools, and a smaller hook loads the MCP servers meant for its type the
same way. In both modes the catalog's `on_request` groups (`functions.yaml`) are
left out until the agent loads them for its task with `use_tools`
(`custom_agents/tool_loading.py`): TeamLead's standard tools alone were 140
functions, OpenAI models refuse more than 128 tools in one request, and the
definitions cost about 30,000 tokens per call, while a one-line-per-group list
in the prompt costs under 1,000. A loaded group leaves after 8 unused model
calls (counted, not timed, because tasks wait days for approvals) and when the
task changes; objective work sessions keep the objectives tools for their
checkpoint pass; a call to a function not loaded loads its group and is still
decided by the access hook. Each model's `max_tools_per_request` (the
`model_capabilities` maps, 128 by default) caps what a request carries: loads
unload the least recently used groups first, and an agent whose always-shown
tools alone are over the cap is reported to administrators. Capacity checks,
eviction and loading share one per-agent lock because Strands can run tool
calls concurrently. Definition defaults and tools required by an objective
work session cannot be evicted. The cap follows the selected primary,
fallback or personal model; override edits refresh between turns without
replacing an active fallback. Workers can load tools TeamLead may
automatically hand on, using `AccessService.may_hand_on_for_task`: it shares
the actual delegation checks but writes nothing. The call's access decision
still rechecks and creates the task grant. A default tool grants no access,
and a successful load reports only functions the agent's definition and
operator settings offer. TeamLead asks for a
higher cap with the runtime setting `model.max_tools_per_request`
(`custom_agents/model_tool_limit.py`), for its own model or, with
`target_agent_type`, a worker type's (workers at their cap report it in their
result, since only TeamLead stages settings). The plan names the exact
provider and model. The cap belongs to the model and changes every agent on
it, so it is not `user_authorized`: administrators approve it in the Inbox,
like the team's model endpoint, and it is never the model-choice fast path.
It only raises (lowering would take tools from every agent on that model) and
refuses a person's own model, whose cap and key are not the team's. One
request with that many tools goes to the model at staging and again before
writing, because the only reliable way to learn a provider's limit is to send
that many. No MCP server loads at startup in either mode, so a server that does
not answer never stops an agent from starting: the loader retries it within
`mcp.load_retry_window_minutes`, then tells administrators, and does not retry a
broker refusal until access or the server changes. Fixed tool groups,
capability attachments and restart-to-attach were removed for this. Skills provide reusable instructions without loading every
procedure into the base prompt. A reviewed managed-skill change commits an
immutable local receipt before TeamArchitect sends an authority-free team
reload hint. Running specialists verify that receipt, refresh their own SDK
cache, and republish presence. Heartbeat fingerprint checks repair missed live
hints without a process restart. External and hosted MCP tools enter through
the same installed registry and the broker's per-call access check.

Model providers sit behind factories and adapters, so task and message
contracts do not depend on one provider. Context budgeting, compaction, and
artifact offloading happen in the runtime instead of relying on provider chat
history as the canonical record. Both places that move a tool result out of
the transcript (the per-call limit, `custom_agents/tool_result_offloader.py`,
and the inner-loop context manager,
`custom_agents/filtered_tool_result_context.py`) save it in one format, a
folder with `manifest.json` naming its agent, team and task, because
`state/tool_results` is outside every agent folder and
`inspect_tool_result_artifact` is the only way an agent reads it back.
Projected context pressure shrinks only results the model has already read; a
result it has not read yet (a staged change id, for example) is shrunk only
after the provider actually rejects the request, and then after older ones.
A result counts as read only after Strands returns a successful model response
that its hooks accept. Failed, cancelled or retried calls leave the attempted
results unread, including a successful response discarded by a retry hook.

### Model provider keys

Model keys belong to an endpoint *address* (scheme, host and port,
`model_providers/endpoints.py`), never to a provider name: well-known
providers have one fixed address each, and the OpenAI-compatible endpoint
(`llm.litellm_api_base`, which can be switched) and an OpenAI-compatible
embedding service have their configured one. A key entered for one address
is never sent to another.

- **Where keys live.** Administrators save the team's keys with `/auth model`,
  a broker page (`secure_tool_broker/model_key_page.py`) that tests each key
  with one real request (`model_providers/connection_test.py`, shared with
  onboarding's provider test) before it stores anything. Keys are kept in the
  broker's credential store, one record per owner
  (`secure_tool_broker/model_keys.py`, `model.endpoint_keys_json`). Only
  processes that run agents (manager and agent processes, by their caller
  tokens) can fetch one (`/v1/model-keys/lookup`); the gateway reads a status
  that never contains a key.
  A Sealbox credential-state read maps only a missing secret or
  `sealbox_tenant_not_provisioned` to an absent record. Reads never create
  tenants; the existing authorized write provisions the first tenant. This
  lets read-modify-write save an owner's first key and lets an empty personal
  store reach the team's key when permitted. Existing tenant files must hold
  a valid matching RSA pair. Missing material, invalid pairs, authentication
  errors and unexpected storage failures remain errors, including with file
  fallback enabled, so damaged tenant state cannot be silently replaced.
- **How processes use them.** `custom_agents/model_keys.py` is the only
  place the runtime gets a model key. An environment variable
  (`LITELLM_API_KEY`, `EMBEDDING_API_KEY`, and so on, with their `_FILE`
  forms) overrides the stored key; the override is logged, reported to the
  broker by name and shown on `/auth model` and in `/auth status`. Otherwise
  the stored key is fetched, cached for a minute, kept when the broker
  briefly does not answer, and fetched again at once after a rejection. Each
  model call looks up its endpoint's key first (`bind_model_keys`), so the
  model objects hold no key, LiteLLM reads it per call
  (`CallKeyedClientArgs`) and the OpenAI client calls a key function.
  Typed `ModelKeyError` failures take precedence over provider status codes
  and text, including through SDK wrappers. They never enter provider
  overload, connectivity, timeout, key-rejection or failover classification.
  Exception traversal also recognizes Strands' `original_exception` field;
  the shared base type lives in the dependency-free `model_key_errors` module
  because model keys already import provider-error helpers.
- **Why.** Keys used to come only from `team.env`: changing one meant a
  container restart, an expired key could only be fixed on the host, and a
  switched endpoint would have received the old endpoint's key. Now a new key
  works on the next model call. Environment variables stay as an operator
  override (owner's decision 2026-09-30), but never silently.
- **Well-known providers.** OpenAI, Anthropic, Gemini and xAI (provider
  `xai`, an API key; `xai-oauth` stays a sign-in) have one fixed address each,
  passed to their client explicitly so `OPENAI_BASE_URL` or
  `ANTHROPIC_BASE_URL` cannot send a key elsewhere. Gemini reads the call's
  key from `CallKeyedClientArgs` (Strands builds a client per call),
  Anthropic from a client subclass whose `X-Api-Key` header is the call's key,
  and the OpenAI clients from a key function. Bedrock keeps the AWS
  credential chain. `anthropic` became a runtime dependency here: Strands'
  Anthropic adapter needs it, and before it was only in the skill scanner's
  environment, so `provider: anthropic` could not start.
- **Personal keys.** A person may save their own key for an endpoint the
  team uses (`/auth model me`, `PersonalModelKeyFlow`), stored in their own
  credential scope and deleted when they are removed. A model call sends its
  task's signed context with the lookup (`task_key_context`); the broker
  verifies it like a tool call (`BrokerAccessGate.verified_person_id`) and
  returns that person's key, else the team's. A rejected personal key stops
  the task and tells the person (no fallback, no administrator notice, not
  part of the team's key status), and its rate limits never trigger the
  agent type's failover (`last_call_used_personal_key`).
- **Team keys per person.** Whether a person's work may use the team's model
  keys is an administrator's decision (`access_control.people.
  team_model_keys_refusal`), stored with the person's other shared settings
  in the access store (`PersonSettingsV1`, `access_control.person_settings`)
  because the gateway, the broker and the agents all read it. New people get
  the administrator's `--team-keys`/`--own-keys` choice or the team's
  model-key sharing (`AccountSharingV1` subject `model_keys`, shared unless an
  administrator ran `/accounts private model`; it replaced the removed
  setting `gateway.users.new_people_use_team_model_keys` so it can change
  from chat without a restart); people added before
  the switch existed have no record and keep the team's keys, so nothing
  changed for them without a decision. The broker enforces it
  (`lookup_handler` answers `team_keys_allowed: false`), and the runtime asks
  the broker before using any environment variable for a person's task,
  because an environment key is the team's too. Embedding lookups carry
  `purpose: embedding` and keep the team's key: memory search is the team's
  service.
- **A person's own model.** On `/auth model me` a person may save their own
  model (`PersonModelProfileV1`: any provider and address, tested first,
  with the address form that answered). Every agent's work for them uses it,
  including agent types pinned to a model (`custom_agents.person_model_runtime`).
  The agent's own model fields are never changed, because failover, the
  settings apply, the reasoning-summary repair and the status views read
  them as the team's. Instead a turn selects a per-person model bundle
  (model, context window, capabilities) once its task is known and before
  context assembly (`select_turn_model`, a turn effect), shows it in the
  turn settings (`turn_settings_with_person_model`), and gives the team's
  model back when the execution context is cleared. A `BeforeModelCallEvent`
  hook (`PersonModelHook`, order -50) sets the right model before every call,
  which covers resumed interrupted tasks, fresh Strands agents and rebuilds
  after failover or live settings changes. Failover skips while a person's
  model is active. Opt-in LLM turn summaries and compaction of a person's
  conversation use their model (`summaries_with_person_model`). Who a task
  works for comes only from its verified identity.
- **Onboarding and voice.** Web onboarding tests the model key, then hands
  it (with the embedding and xAI voice keys) to the broker through the same
  private hand-over file as the Exa and Brave keys
  (`assistant_gateway/onboarding_account_keys.py`, `OnboardingModelKeyV1`);
  the broker saves them as the team's keys when the person who onboarded is
  an administrator. They are never written to `team.env`, where they would
  override every key saved later, and they are no longer listed as missing
  environment secrets. Grok voice looks the xAI key up per request; the
  gateway may fetch that one key and no other (`GATEWAY_KEY_ADDRESSES`). A
  separate adapter host has no broker token, so its voice reads
  `XAI_API_KEY` only.
- **Switching the endpoint.** `llm.litellm_api_base` is a team-wide runtime
  setting (`custom_agents/endpoint_switch.py`), never a model choice:
  staging is refused while the new address has no key or while the
  operator's `LITELLM_API_BASE` would override it (the launcher's override
  list), an administrator approves it in the Inbox (not the person who
  asked: it moves the whole team's model traffic), the Inbox text names the
  host and whose key is used, and applying sends one test request first and
  saves nothing when it fails. It applies without a restart: every agent
  compares the saved endpoint with its own at the start of each turn and
  rebuilds its model when it changed (`custom_agents.endpoint_refresh`). A
  manager restart used to be required, and it restarted only TeamLead's
  process, so startup agents such as MemReviewAgent kept the old endpoint.
  The endpoint stays out of `RuntimeLLMProfile` and `LLM_OVERRIDES.yaml`,
  which model-choice plans write without administrator review. Staging
  also tests the address with and without `/v1`
  (`model_providers.connection_test.check_endpoint_address`) and stages the
  form that answered, because people name an endpoint by its host.
  TeamLead sees the team's saved endpoints (addresses and the form that
  answered their test, never keys) in `get_runtime_configuration_context` →
  `model_endpoints`, served by the broker's `MODEL_KEY_ENDPOINTS_ROUTE`;
  before that it could not tell an endpoint such as Bifrost was already set
  up and asked the person for its address. Live tests wait as long as agents'
  model requests (`llm.request_timeout_seconds`): a 20-second wait refused a
  working endpoint whose reasoning model was silent longer than that.
- **Model settings reach other agents at their next turn.** Every agent
  reads `LLM_OVERRIDES.yaml` again at the start of each turn
  (`model_failover_runtime.refresh_runtime_llm_selection`) and rebuilds its
  model and compactor when its profile changed. So a TeamLead change to
  another agent type's model settings (`current_agent.llm.*` with
  `target_agent_type`) or to the workspace default (`workspace_default.llm.*`)
  needs no restart. A change explicitly targeting TeamLead's own type switches
  it at once; a default-only change reaches it at its next turn too. These
  settings used to be marked "manager restart", carried over from when they
  lived in the instance config. That restart restarts only TeamLead's process
  and never reached the other agents (owner's report 2026-10-11). The plan's
  summary, its Inbox effect lines and the apply result's `running_agents` say
  that running agents switch at their next turn, because an idle agent's
  status shows its previous values until then
  (`runtime_setting_registry.next_turn_targets`). Instance-config model
  settings (`llm.*`, and `current_agent.llm.*` settings stored there, such
  as `temperature` and `max_output_tokens`) are read only at startup and
  still need the reported restart.
- **The `/auth model` pages.** One form per page whose Endpoint list fills in
  the fields; results show just above the form they came from (red for a
  failure, with what it most likely means); pages last 24 hours
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
finding disappears once its condition is gone. The gateway gathers them on
each read from the launcher's list of environment overrides (names only,
`InstanceLayout.environment_overrides_path`, written by
`docker/settings_environment.py`), the broker's model key status and the
configured endpoints, every agent's own findings carried in its status
heartbeat (`AgentStatus.status_findings`, `custom_agents/status_findings.py`:
MCP servers that do not load, degraded memory search, skill refresh errors,
schedules for administrators, receive loops that stopped), whether TeamLead
reports, and its own configuration and receive loops. A source that does not answer is listed and shown as a
problem, because silence must not look like health. Every five minutes the
gateway sends administrators one Inbox notice per new set of problems
(`status_problems`), linking to the page. Why: conditions such as an
environment variable silently overriding a saved setting or key, a rejected
model key, or an MCP server that stopped answering were only in logs or only
known to one process.

### Knowledge page

People read and write the knowledge base from Web Chat (`/webchat?view=knowledge`,
`knowledge_page.mjs`): Team and Personal lists with search and a Current pages /
Archived pages filter, a page with its links
in, open notes and history, an editor, revert and archive, and **My proposals**
(the person's own pending team contributions, published or withdrawn by them).
`GET /api/web-chat/knowledge/counts` answers only how many proposals wait for
the person (`count_contributions`, a count query in the store). It feeds the
badges on **My proposals**, Settings' **Knowledge** link and the cog without
loading the list. On the chat page it asks in the open conversation and skips
the read when none is open, so counting never creates a conversation.
The browser discards older count replies when a list supplies a newer total,
and sign-out detaches pending reads so the next person can read immediately.
After publishing or withdrawing a proposal, the refreshed list supplies the
count. If navigation discards that list or the list read fails, a fresh count
read updates the badge without changing the viewer. Replies from an earlier
sign-in cannot start these reads in the next person's session.
The store lives in the manager's database, so the gateway's routes
(`website_handlers/knowledge.py`) send one `ManagerKnowledgeCommand` per request
through `GatewayManagerCommandService`, like Settings memories. The command names
no person: the manager builds a person actor (`ActorKind.PERSON`) from the
gateway-signed context only (`custom_agents/knowledge_command_runtime.py`,
`verified_command_person`), so `personal/` is always the signed-in person's own
space. Both sides check the same ownership rule
(`access_control/knowledge_access.py` over `access_control.ownership`): every
team member reads and edits team pages, personal pages and proposals are their
owner's. The knowledge service then applies its own rules: a person's team edit
publishes directly without write limits, the never-stored content rules apply,
content held for review needs an explicit confirmation, and saves carry the base
revision so a concurrent change is a conflict, never an overwrite. Refusals and
review flags carry kind, field and position, never the matched text. Archive and
revert reasons are fixed by trusted code because they enter shared history. Pages
show `[[links]]` as buttons: the manager resolves each one with the stored link
rules, following merged names (`KnowledgeService.page_links`), and the browser
replaces link syntax in rendered text nodes, never in code. The gateway replaces
editor person ids with names before answering the browser. Like
other Web Chat routes, these have no tool catalog levels: they are people's own
controls behind the session, the write checks and the ownership rule.

The repository filters and counts pages by status and proposals by their owner
and pending status before applying SQL pagination. Archived lists exclude merged
source pages, which are redirects rather than restorable pages. This keeps
restoration reachable and proposal totals complete beyond 100 entries. An omitted
summary preserves it; an explicit empty summary clears it. Edited note proposals
publish the reviewed note argument, never fall back to the original text. Archive
publication copies the current page fields and changes only its status, with the
same optimistic revision check as other writes. Archive proposals therefore have
no edit option. The browser retains the displayed page's full space and slug for
Refresh, and discards replies from a previous view or signed-in session.

## Deployment and observability

`config.yaml` contains generic runtime settings. Onboarded assistants use the
separate `instance_config.yaml` schema. Durable non-secret settings live in
configuration. Secrets use environment variables or file references. Child
services receive service-specific runtime manifests rather than the whole
instance configuration.

One saved settings file (owner's decision, 2026-09-29). Every process and
every settings writer uses `<instance_root>/instance_config.yaml`. Running
processes load it with `GatewayInstanceConfig.load_for_process`, which applies
what the deployment supplies through the environment (NATS servers and
credentials, the public origin, the website listener) in memory
(`assistant_gateway.deployment_environment`); the Docker bundle's own
PostgreSQL address is applied by the storage resolvers
(`TEAM_BUNDLED_POSTGRES_ADDRESS`). Writers load the file as stored
(`load_saved`), change only their own fields and save, so no deployment detail
or stale in-memory value is ever written back. The bundle used to run every
process from a copy written at container start; settings saved while the
runtime ran then waited for a container restart. For the same reason the
launcher, on each in-app restart, recomputes the environment variables it
derived from the saved file (`docker.settings_environment`), leaving variables
the operator or the bundle set and every secret as they were, so a setting
that "takes effect after a restart" does so after the in-app restart.

Each process the launcher starts receives only the secrets it uses.
`docker/secret_env_vars.yaml` names, for every secret, the processes that
receive it, and `env_secrets.environment_for` removes the rest (with their
`<NAME>_FILE` forms) from each process's environment, including processes
restarted at run time. Third-party programs (`nats-server`, `openshell-gateway`,
`sealbox-server`, Postgres, the Photon sidecar) receive none of the runtime's
other secrets, and no secret is passed on a command line, which every process
can read. The reason is containment of accidental disclosure: a log line, an
error report or a program one process starts cannot reveal another process's
secrets. All processes still run as one Linux user, so the code sandbox, not
this filter, is what keeps agent code from reading other processes.

The list is YAML so each entry can carry a plain description (`about`), from
which `scripts/export_secret_env_docs.py` generates the Docker guide's tables.
Only `env_secrets.py` reads it, through a Pydantic model that refuses unknown
keys, unknown process names and bad `file` values. The container entrypoint
does not read secret files itself: every Python program it starts reads them
at startup (`env_secrets.materialize_secret_file_envs`), so the entrypoint only
runs `env_secrets.py check-container-env`, which refuses a container
environment that sets both `<NAME>` and `<NAME>_FILE`. That check covers the
container's own environment only, because the environments the launchers build
may hold a `team.env` value next to its file form on purpose.

The Docker bundle can supervise NATS, optional PostgreSQL, the manager, startup
specialists, gateway, website, activity collector, secure broker, and execution
controller. A deployment can instead use external NATS, PostgreSQL, credential
stores, reverse proxies, and adapter hosts. Process separation and external
services require operators to supply the corresponding network and workload
identity controls.

Runtime logs, task history, execution digests, schedule telemetry, presence,
and anomaly records support diagnosis. The activity collector consumes curated
events into a separate read model. It does not route Tasks or treat manager
tables as its control plane.

### Moving a team to a new version

Releases may change storage without migrations, so a team moves to a new
version through a transfer file (`team_transfer`, `assistant_cli team export |
inspect | review | import | import-credentials | finish-import`; guide:
`user_docs_source/team-transfer.md`).

- **Two pages, one review.** The operator's loopback page (`team review`,
  `team_transfer.review_web`) and onboarding's **Import a team export**
  (`assistant_gateway.onboarding_web.team_import`, for an export copied into
  `InstanceLayout.import_dir`) both run `team_transfer.review_session`: the
  same selection, edits, schedule confirmation, validated digest and import,
  with readable record names from `team_transfer.titles`. Each page decides who
  may call it: the loopback page by host and printed token, onboarding by the
  export's passphrase, which gives one browser tab a token sent only as a
  header. Onboarding also asks where secrets come from
  (`team_transfer.sections.DestinationChoices`), which wins over the carried
  `launch.secrets_source` in a first import only.
- **Credentials wait for the first start.** Only the running broker stores
  credentials and onboarding runs before it, so onboarding's import leaves the
  still-encrypted credential section, its encryption subkey (never the
  passphrase) and the signed manifest in
  `InstanceLayout.team_import_credentials_path` (`state/`, 0600). The
  handover is written before the unfinished-import marker is removed; a failed
  handover write keeps the instance unstartable. Private writes sync both the
  file and its directory entry. The launcher starts the broker, then waits for
  `team finish-import` (`build_finish_import_task`) before initializing agents
  or the gateway, so their first model or embedding lookup sees restored keys.
  The step stores the selection as `team import-credentials` does and notifies
  people and the exporting owner. Retryable broker record failures are retried
  within the startup window; exhaustion or a whole-request failure keeps the
  handover and stops startup. Completed credentials are skipped through the
  receipt on the next start. Terminal refusals are reported without retrying;
  the handover is deleted only when no retryable failures remain.
  Broker file-backed credentials, imported password-manager sessions and
  template files share an atomic private writer. Imports publish complete
  files exclusively, preserving a destination sign-in that appeared while
  the broker was checking it. Normal saves atomically replace complete files;
  a failed write leaves the old contents or no destination, so retries cannot
  mistake a truncated file for an existing credential. Owner-file exports
  exclude the writer's unpublished temporary files.

- **The file is the only contract between versions.** Each version exports its
  own state with its own repositories; no version reads an older database.
  Sections hold plain, versioned records with checked-in JSON Schemas
  (`data/schemas/team-transfer/v1`, a test fails when they drift). The manifest
  identifies persisted payload models and their owning sections. Frozen
  fingerprints in `data/schemas/team-transfer/section-contracts.json` detect
  payload changes even after schema regeneration: append a new version and
  converter rather than replacing an existing fingerprint. A version
  imports its own section versions and the ones the previous release exported,
  through one conversion step per section that changed since
  (`team_transfer.sections.SECTION_UPGRADES`). This is the one deliberate
  compatibility layer. Steps are pruned by release: when a release ships, the
  steps from older releases' versions are deleted, and files from those
  releases are refused before anything is written. Refusal messages explain
  that moving an older export may require several import/export steps and
  point to the guide's release-specific paths. The guide owns these paths
  because section versions alone do not identify a suitable intermediate
  release.
- **Fresh first batch, same ids.** Import creates the instance (the same first
  steps as onboarding, `assistant_gateway.instance_secrets`) and keeps team,
  person, schedule and grant ids. Person schedules are signed again with the
  new gateway key. Later batches require its receipt and the exact same signed
  export. Imported identities are skipped and existing destination records
  kept. Account links match by team, owner and tool, since a newer link has a
  new ID; all active destination chat bindings, including removed people's,
  participate in collision checks through a separate read-only identity query.
  Noncredential batches hold an import lock and the supervisor's run lock;
  broker batches hold the import lock while the runtime runs. The process config
  loader checks the unfinished marker for every entry point. Failed later
  batches require a pre-batch backup restore: writers do not share a transaction
  across stores.
- **One planner, nothing silently dropped.** Every record is imported, imported
  with a note, or refused with a reason, checked with this version's own
  loaders and models; sections see what earlier sections accept, so a grant on
  a tool that is not imported is refused, not carried dead. `--dry-run` shows
  the same plan. Knowledge contributions that change an existing page require
  that page to be selected or already present. Link rows are checked against
  the selected or retained page's current text using the same link resolver as
  normal knowledge writes, so later batches preserve destination link edits.
- **Settings carry choices only**: the settings that differ from a freshly
  created configuration, so new defaults apply to everything else. Model
  control files likewise carry edits relative to bootstrap's rendered values.
  PostgreSQL writers use the destination's planned DSN; bootstrap preserves it
  for the next launch. Before the first write, validation resolves every actual
  database address through the normal storage resolvers, including the bundle's
  address and a separate activity database, and refuses occupied databases.
- **Private browser review.** `team_transfer.review` compiles selections and
  editable files into the existing section planners, shared by browser and CLI.
  Schedules travel as Markdown containing YAML and validate through the schedule
  model. IDs, ownership, routes and authorization provenance stay fixed.
  Confirmation binds to the exact submitted schedule text, without rereading
  a file an external editor could change; import binds to the
  validated review digest. Settings and memories are editable individually.
  The browser saves in request order, keeps later edits dirty, and discards
  validation responses from an earlier edit revision. Pending saves block import.
  The loopback aiohttp page uses shared appearance assets, a temporary header
  token, origin checks and no caching. It never renders secret values.
- **Read-only validation.** Destination snapshots use current exporters under
  `storage.read_only.read_only_repositories`. Constructors skip initialization
  and connections enforce read-only access, so review cannot add tables,
  backfill records or repair schemas. The shared memory planner rejects blank
  text after trimming, and schedule planning computes a next run with the store's
  timezone rules. Editable YAML errors show their location without repeating
  private input. These checks run before the unfinished-import marker is written.
- **Credentials stay with their owners.** A passphrase-derived key
  authenticates the manifest and encrypts secrets. Operator secrets from
  `team.env` travel encrypted in the file. The broker's stored credentials are
  encrypted and stored again by the broker itself, through two routes only the
  `operator` caller (`access_control.caller_tokens`) may use; the command never
  holds those values.

Transfer confidence relies primarily on integration tests with real repositories
and HTTP routes, plus a process-level CLI/broker round trip. Parser tests and
frozen previous-section fixtures cover conversions and refused edits. PostgreSQL
and Sealbox acceptance require those services; file-store tests do not prove
their deployment behavior. Browser checks cover unsaved edits, confirmation
invalidation, selection changes and the validate/import sequence.

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


### X and Hugging Face tools

Both Docker images include `xurl` and `hf`, with matching agent skills.
Credentialed operations use the secure tool broker and the account linked to
`cli:x` or `cli:huggingface`. X token refresh state follows the configured credential store;
private HF artifacts use temporary broker storage and bounded transfer results.
See the [operator guide](user_docs_source/hub-cli.md) for supported actions and setup.

### Local build infrastructure

CI runs on a labeled self-hosted Linux runner, including the WSL 2 build host shared with desktop development. Test services use unique ownership labels and dynamically allocated loopback ports. Installed runner hooks reclaim job state after action post steps and recover abandoned work before admitting another job. Runtime data is outside that ownership boundary. See [Local CI](docs/LOCAL_CI.md).

## Visual workflow coordination

The [visual workflow guide](ACCESS_WORKFLOWS.md) describes the user and agent
contract. A workflow is a view over access requests: each step names the kinds
of staged change and the tools (with a level) it needs, checked against the
tool catalog. `custom_agents.access_workflows` owns plans, progress and the
link from each access or change request to its step. The link is made when the
request is stored, through the `RequestWatcher` port of
`access_control.requests`, so the access model never imports workflow code; a
delegated worker's request reaches its parent's step only through the shared,
verified signed task context. `storage.access_workflow_repository` persists
plans with versions and events. The gateway's workflow page
(`assistant_gateway.access_workflows_page`) reads each request's current status
from the access store and decides through the access Inbox. A denied request
closes its step, and a new request that only that step covers is refused.
`request_access_workflow_approvals` submits one change request per staged
change and pauses once; `apply_change` finds each approval by its change id.
The graph neither executes commands nor grants access.

Local scheduled tasks use database claims and durable run records. A separate
runtime loop starts needed local workers without a TeamArchitect model turn.
The default startup policy covers the specialist types that ship with
Universal Agents; tool and credential access remain separate. Memory-review timers
target MemReviewAgent after the explicit one-time upgrade described in
[user documentation](user_docs_source/scheduled-workers.md).
