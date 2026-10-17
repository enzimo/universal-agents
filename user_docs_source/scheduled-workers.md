# Automatic workers for scheduled tasks

The local scheduler starts a registered specialist when a job is due and no local
worker claims it. It runs independently of TeamArchitect's conversation and keeps
working while the lead is paused or busy. A worker may retire after finishing;
the next occurrence can start another worker without another lead decision.

The default startup policy covers the specialist types that ship with Universal
Agents. Ordinary owner-scoped schedules for those types that have no saved startup
policy receive default startup permission, and a type added by a later release is
covered too. Team-written types, such as a specialist TeamLead created or a
definition in `control/agent_definitions`, need an explicit entry in
`agent.schedule_workers.eligible_agent_types`. Existing policies, including
disabled ones, are preserved. The default grants permission to start workers only:
current tool, credential, owner and system-schedule protections still apply.

New memory-review timers target **MemReviewAgent** directly. The local worker
prepares saved review jobs and executes the scheduled task using the same database
claim and run records as other local schedules. TeamArchitect handles exceptional
repairs. Conversation-triggered memory reviews retain their existing dispatch path.
Existing timers require the one-time upgrade below; do not create replacements.

The team's core facts are audited by one team timer. Personal facts and stale
conversations are reviewed per person: every active person has their own pair
of timers (`mem_rev_user_<instance>_<person>` and
`mem_rev_stale_<instance>_<person>`), which review only that person's facts and
conversations and send anything for them to their own chat (their default chat,
otherwise the chat they used last). The gateway adds the pair at start for
everyone active, including people added while it was stopped. A disabled or
removed person gets none.

Instances set up before per-person reviews also keep an older team-wide pair,
`mem_rev_user_<instance>` and `mem_rev_stale_<instance>`, owned by the first
user. It only repeats the first user's reviews. The first user (an
administrator) can delete it from their chat with `/delete schedule
mem_rev_user_<instance>` and `/delete schedule mem_rev_stale_<instance>`.

### When an agent definition changes

A schedule's worker policy is saved against its target agent definition. What
happens when that definition changes depends on where it comes from:

- **Shipped definitions** change only when you deploy a new version. The schedule
  keeps running, and the access its worker type holds stays in place. The scheduler
  logs `schedule_worker_definition_refreshed` with the tools added or removed and
  whether the prompt changed, and `inspect_scheduled_worker` shows the same
  details under `definition.last_release_change`.
- **Team-written definitions** (a specialist TeamLead created, a definition file
  in `control/`, or a team file that redefines a shipped type) pause the
  schedule with `agent_definition_changed_review_required` when their role, own
  prompt text, default tools, skills setting or backstory change. A release
  that only changes which tools a tool set contains, or the shared base prompt,
  does not pause them. A team definition moved from tool groups to
  `default_tools` counts as a change, so its schedules pause once for review.

For a paused schedule, TeamLead's `inspect_scheduled_worker` lists the
differences under `definition.changes` and offers `suggested_refresh`. TeamLead
shows you the differences; after you agree, it re-saves the policy with the same
target, which clears the block. Access belongs to the worker type, so re-saving
keeps it; TeamLead runs `prepare_scheduled_job_access` to see whether the changed
definition needs more. Idle workers must pass the same check.
Verify admission and a completed run afterwards. A scheduler claim is only an
attempt to admit work.

Upgrading from a version before this behavior: schedules for shipped types resume
on their own. Schedules for team-written types pause once and need one re-save
through TeamLead.

An unresolved occurrence keeps its review reminder deadline even when different
checks report different blockers. Repeated checks do not create a new review on
every poll. A full TeamLead queue can still prevent review admission. Use
**Settings → Tasks** to inspect and remove stale queued work; preserve valid work
and repair the schedule blocker. Increasing the task limit does not fix that blocker.

## Give a scheduled worker its access

A scheduled job a worker runs for a person holds its own access ("access
follows the work"): every agent in one of its runs uses the job's access, and
only there, so what one job needs never widens another job of the same worker
type, and retargeting the job to another worker type keeps it. The job's worker
uses only its job's access (with its role's standard access and team defaults),
not what TeamLead could pass on for the owner; TeamLead keeps its own access
for the owner if it takes part in the run. A schedule TeamLead runs itself, and
the runtime's own timers (heartbeat items, objective work sessions,
after-action reviews), are not jobs: they work like the person's request, so
TeamLead uses its access for the person and passes it on to the workers it
delegates to. Every tool call of the run is decided for the agent and for the owner,
because agents never exceed their person. Access is a level on a whole tool:
the job gets each tool it declares at the highest level its requirements need.

1. `prepare_scheduled_job_access` lists under `missing_access` each declared tool
   that the job or the owner does not hold yet.
2. `authorize_scheduled_job_access` gives the job what TeamLead may pass on (for
   example with the **Trusted team** starter profile), one grant per tool; you get
   a notice for each. If TeamLead's own access says "ask me each time", it waits
   for your approval in the Inbox. Setup access the requirements no longer need,
   or that is higher than they need now, ends first; access a person approved
   during a run stays.
3. What TeamLead cannot give is listed under `ask_a_person`, with the exact
   `request_access` call. The worker asks for it the first time a run needs it;
   that run then waits until the owner decides, and **Keep for later tasks**
   gives it to this job only. When the owner lacks the tool too, only a team
   administrator can give it to both.

The job's access serves its owner's work only (see
[Whose work access serves](permissions.md#whose-work-access-serves)). Deleting
the job ends its access, and the Access page shows it as a **Scheduled job**
card.

With authorization off every tool is available, so there is nothing to give.

## Who signs a scheduled run

Every scheduled run must show whom it works for, or the access checks refuse
every tool call ("Nobody verifiably asked for this task"). Only the TeamLead
process holds the scheduler's signing key.

TeamLead signs one permit per schedule. It says the schedule's runs work for
its owner, and it lasts 30 days; TeamLead renews it a week before it runs out
and checks it again whenever the schedule gets a new owner. A worker listed
under `startup_agents` runs as its own process: when it takes a due schedule,
it attaches that permit to the run and starts it without contacting TeamLead.
Workers that TeamLead starts itself sign their runs directly.

The permit does not record the owner's roles. Each time a run's tool call is
checked, the owner's current roles and standing are read (at most 30 seconds
old), so a role change, a disabled or removed owner, or a deleted or turned-off
schedule takes effect on the run within 30 seconds.

TeamLead gives a person's schedule a permit only when the person is active and
the schedule was saved from that person's own request (or is managed by
Universal Agents itself). A schedule without a permit still runs: the worker
then asks TeamLead to sign that one run, as for a schedule TeamLead has not
looked at yet (it looks at new schedules within about a minute). TeamLead
signs only a run the worker has actually taken, for the schedule's owner. If
TeamLead is stopped, refuses, or does not answer within 20 seconds, the worker
does not start the run. The schedule stays due and is tried again after the
retry wait (`agent.schedule_workers.defaults.retry_backoff_seconds`, 30 seconds
by default), so a run can start late while TeamLead restarts, but it never runs
without its signature.

To follow this in the logs, look for `schedule_permit_issued`,
`schedule_permit_renewed` and `schedule_permit_refused` (TeamLead, with the
reason), `scheduled_run_permit_attached` (the worker used the permit),
`scheduled_run_context_signed` and `scheduled_run_context_refused` (TeamLead
signed or refused one run), and `scheduled_run_not_started_unsigned` (the
worker, with the reason).

## Upgrade and existing work

New tables initialize automatically, and omitted settings receive defaults.

Saved worker policies from before version 2 of their format (which kept records
of the removed permission system) are not read. Startup stops and names them;
while Universal Agents is stopped, a team administrator runs
`assistant_cli.py access approve-existing --config <instance_config.yaml> --by <your user id>`,
which rewrites them in place: the old approval records are dropped and a policy
saved under authorization is recorded as allowed by held access. Access that
earlier approvals gave to one schedule is not carried over; give the worker type
its access again (see [Give a scheduled worker its access](#give-a-scheduled-worker-its-access)).
Worker limits live in `agent.worker_pools` (see the configuration guide's
Worker pools section): each worker type's pool size (4 by default) covers
scheduled and delegated work, the team allows 32 workers, and each type 60
starts per hour. Manual and delegated starts count toward these limits. Deploy the manager, gateway, browser assets, and the Rust
Cedar evaluator (`ua-cedar-authorizer`) together. Follow the [snapshot preflight and retirement procedure](configuration.md#repair-an-active-runtime-snapshot-upgrade)
before restarting an instance with saved work. There is no automatic conversion
or abandonment of incompatible tasks.

Existing typed schedules remain claimable by a replacement of the same type.
An exact agent-ID target needs explicit conversion before another identity can
claim it.

### When a schedule is stuck

An enabled, unpaused, unexpired schedule that is overdue and that no worker has
picked up is **stuck**. You are told about it, without asking:

- Once it has been stuck for `agent.schedule_workers.review_interval_seconds`
  (one hour by default), its owner gets an Inbox notice and a chat message
  saying since when it has not run, what is wrong and what to do.
  Administrators also get an Inbox notice when only they can fix it, and for
  schedules the runtime manages. An owner who is also an administrator gets
  one notice, not two.
- When it runs again, everyone who was told gets a "running again" notice and
  message. Each problem is reported once.
- Its card under **Runtime > Schedules** shows **Not running**, with the same
  explanation, as soon as it is stuck.
- A one-time schedule (or the last run of a counted series) that is more than
  `agent.schedule_workers.missed_one_time_after_seconds` late (one hour by
  default) is turned off instead, and its owner is told it was missed, so a
  reminder never arrives days late. Ask your assistant to set it up again if
  you still need it. A run a worker has just started is not turned off.

To fix a stuck schedule, ask your assistant about it in your normal
conversation; it inspects the schedule and repairs what it can, keeping the
schedule's ID, target and pending run. For `agent_definition_changed_review_required`,
a team-written definition changed after the policy was saved: the assistant
shows the differences and re-saves the existing policy once you agree.
Recreating the timer is not the normal repair. When you change a schedule
yourself (through your assistant), or a person approves a change to a schedule
the runtime manages, its worker policy follows automatically; the schedule
does not wait for a second confirmation.

A change never overwrites a newer one. If the schedule was paused, turned off,
changed or deleted after your assistant read it, the change is refused and the
assistant reads the schedule again. The controls on the Schedules page are
refused the same way when someone changed the schedule after the page showed
it; a run that only moved its next run time or wrote its notes does not count,
so a schedule that runs often can still be paused. A run that writes its notes
never undoes your change. Pausing or turning off a schedule just before a run
starts stops that run, and pausing it while it runs does not make that run
happen again when you resume it. A one-time schedule always runs once: asking
for more runs needs an interval or a cron time.

The runtime notices and reports; it does not guarantee a repair. Disallowed
types, missing tools or credentials, or a stopped team lead can still need an
administrator. The team lead does the checking, so while it is down nobody is
told; administrators see that on the Status page.

Earlier versions gave the team lead an hourly `sched_review_...` task instead,
which could not reach anyone. Such tasks left in **Settings > Tasks** can be
removed there.

Routine claim/admission checks and claim releases are DEBUG logs. Two workers
checking an overdue schedule do not mean two successful runs occurred. Read the
run history to verify admission and completion; unresolved blockers remain
visible as warnings.

Rows without matching team provenance are outside the stuck-schedule check.

Objective records and links retain their normal lifecycle. System-managed
Objective work-session schedules and child-task timers remain outside automatic
specialist-worker configuration. A same-team overdue system timer can receive
the general review, but its repair must use the protected Objective/system
workflow. A user-owned root schedule linked to an Objective can use automatic
workers; the linkage alone does not make it a system timer. Retiring an
incompatible task snapshot does not complete its Objective or gateway request.

Each bounded Objective session closes its own Task after checkpointing. Remaining
plan work belongs to the durable Objective and its next scheduled session. A plan
that cannot admit a step leaves the session Task runnable so it can report the
blocker and finish. A checkpointed plan step returns its root Task to completion
without another model invocation; it does not leave a waiting Task for tomorrow.
Its saved checkpoint determines the recorded outcome, and the existing `always`,
`on_change`, `never` and failure-notification settings still control delivery.

Completion uses the recurring schedule linked in the saved Objective work policy;
it does not require a gateway delivery job ID. If the policy, checkpoint, schedule,
team or owner cannot be verified, the Task fails with a concise warning and reply.
Saved checkpoints remain available and the task slot is freed. Normal Objective
failure rules still apply and can mark the Objective blocked.

If an earlier version failed with "The Objective session's schedule and owner
could not be verified", install the fix and inspect the existing policy, schedule
and checkpoint. Resume the existing Objective if it was marked blocked after that
failure. Do not recreate a valid schedule or discard the saved checkpoint.

## See assignments in Web Chat

Open **Runtime Controls > Schedules**. Each card shows the assigned agent type,
any exact agent identity, and **Automatic creation: Enabled by TeamLead**,
**Disabled by TeamLead**, or **Not configured**. A pending retarget shows both
the current and desired types until the current claim ends. An unknown type
is labeled **Not specified**.

Automatic creation describes the saved lead policy. It is separate from the
schedule's Enabled/Paused/Disabled state and remains subject to current access,
current eligibility, operator denies, and launch limits. The card does not certify tool readiness
or report that a worker is already running.

Default policies are shown as **Automatic creation: Allowed by default policy**.
Memory-review cards show **Review agent: MemReviewAgent**, **Managed by: Local
scheduler**, and the same saved startup permission. Old timers show **Upgrade
required: Move this timer to the local scheduler** until upgraded. These labels
describe permission and assignment, not proof of a running or successful worker.

## Upgrade existing memory-review timers once

This changes their execution path. Use the new release's upgrade command with the
same configuration, environment and persistent database as your running instance.
The script defaults to preview and never asks TeamArchitect to reapprove current
types. It preserves schedule IDs, due times, recurrence, run counts, pause/disabled
state, owner, conversation, memory and past results. It is safe to repeat: existing
startup policies are preserved.

1. Preview with the new code. Preview does not change the database and can run
   while the instance is running:

   ```bash
   python scripts/upgrade_local_schedules.py --config /path/to/instance_config.yaml --team-id YOUR_TEAM
   ```

   The report separates recurring schedules from saved review jobs, which are
   individual batches of memory or conversation content awaiting review. Queued
   jobs alone do not block the upgrade and are kept unchanged. The report lists
   claimed job IDs, their worker IDs and claim expiry times, and saved unfinished
   MemReviewAgent task IDs. Unsupported saved runtime formats are reported by
   worker ID and require the [snapshot upgrade procedure](configuration.md#repair-an-active-runtime-snapshot-upgrade).
   The report excludes review content and task text.
2. Let active reviews and already-delivered memory-review tasks finish, then stop
   all team runtimes. The database queue does not have to be empty. A queued
   database record can also have an old NATS delivery, so finish those deliveries
   before applying; stopping a worker does not remove its pending messages.
   Do not delete tasks or broker messages to force the upgrade through. Take a
   normal full database backup for rollback.
3. Apply after resolving any reported blockers:

   ```bash
   python scripts/upgrade_local_schedules.py --config /path/to/instance_config.yaml --team-id YOUR_TEAM --apply --runtime-stopped --backup /path/to/local-schedules-before-upgrade.json
   ```

   Supply `--env /path/to/debug.env` if the configuration needs environment values.
   The command writes a new private schedule/policy backup before editing anything.
   It refuses active schedule claims, unfinished or unsupported saved MemReviewAgent runtimes, and
   claimed review jobs. For abandoned expired claims, preview and apply with
   `--retire-expired-review-claims` as described below. The script never resets a
   claim for retry or declares a job complete. These execution
   blockers stop the command without changing the database or creating the JSON
   backup. Missing owners, unusual exact assignments,
   disallowed types and changed definitions are reported for review, not guessed.
   The command cannot verify broker drainage; `--runtime-stopped` confirms it.
4. Restart the new runtime. Inspect the existing schedules and verify a new
   admitted occurrence and completed review. No duplicate schedules are needed.
   Local reviews reuse queued jobs with the matching review kind and owner;
   core-memory audits are team-owned. Other queued records remain unchanged.
   Legacy user-memory audits without a canonical owner are not assigned to a
   guessed user and need a separate review of their ownership and outcome.

Each timer's target and policy are saved together. If interrupted between timers,
rerun the command with a new backup filename. For rollback, stop the runtime and
restore your full database backup and matching application version together;
the JSON file is an audit backup, not an automatic database restore.

### Read a large upgrade report

Capture the full preview, including stderr, then summarize it with this standalone
Python script. Run the second command even if preview exits with status 1:

```bash
python scripts/upgrade_local_schedules.py --config /data/instance_config.yaml --team-id team_ua > /tmp/local-schedule-upgrade.log 2>&1
python scripts/summarize_local_schedule_upgrade.py /tmp/local-schedule-upgrade.log
```

Read `look_at_first` for unexpired claims, missing or invalid expiry dates, saved
unfinished tasks, unsupported runtimes, and blocked schedules. Read
`expired_claims_by_worker` for counts, oldest/newest expiry dates, and example job
IDs. Each section shows at most 10 rows and reports how many were omitted.

To see individual expired jobs for one worker, add `--worker` with its exact ID.
Use `--show-expired` for all workers' individual expired jobs, and `--limit 50` to
increase the rows shown per section. Counts always cover the full report.

An expired claim does not establish whether work completed or changed memory.
The default upgrade still blocks claimed records. If all the claims are abandoned,
use the explicit retirement option; the summary script only reads the report.
A report without a saved runtime is not a check of live processes or pending NATS
deliveries.

### Retire abandoned claims during the upgrade

With the updated code, preview the additional operation:

```bash
python scripts/upgrade_local_schedules.py --config /data/instance_config.yaml --team-id team_ua --retire-expired-review-claims
```

Check `expired_claim_retirement.eligible_count` and that no other blockers remain.
After stopping all team runtimes, finishing old NATS deliveries, and taking a full
database backup, apply with a new backup filename:

```bash
python scripts/upgrade_local_schedules.py --config /data/instance_config.yaml --team-id team_ua --retire-expired-review-claims --apply --runtime-stopped --backup /data/local-schedules-before-upgrade.json
```

This changes eligible claims from `claimed` to inactive `expired`, preserves their
content, worker identity, and expiry, and records that the outcome remains unknown.
It also copies their original rows into the private backup. It neither retries nor
deletes them. Expiry must be older than the configured grace period, with no saved
unfinished or unsupported worker runtime. Other execution blockers still stop the whole apply
before any write. Queued jobs remain unchanged. Repeat preview after apply to see
`awaiting_cleanup_review`; these inactive records no longer block the timer upgrade.

### Ongoing cleanup

The local scheduler checks abandoned claims every five minutes by default. It waits
one hour past expiry and excludes live local workers, known NATS peers, unfinished
or uncertain recorded launches, and workers with saved tasks or unsupported state. It retires at most 100 claims per pass. Expired claims are
no longer automatically reclaimed for execution: a timeout cannot prove that a
memory update never happened.

Pending cleanup ensures the team's existing InternalImprovementAgent maintenance
schedule exists. A new schedule is due immediately; an existing schedule keeps its
cadence, pause, and disabled state. Set
`agent.schedule_maintenance.memory_review_cleanup` in either `config.yaml` or
`instance_config.yaml`. Its settings are `enabled`, `scan_interval_seconds`,
`expired_claim_grace_seconds`, `batch_size`, and `archive_retention_days` (90 by
default). Disabling automatic scans does not prevent explicit offline retirement.

InternalImprovementAgent uses `inspect_memory_review_cleanup` to read job IDs,
states, worker IDs and times, without private memory content. It uses
`maintain_memory_review_jobs` to archive exact expired IDs. Archiving retains their
content and unknown outcome. Purging removes only previously archived content
after the retention period and keeps the cleanup audit history. Neither operation
changes memory facts or recurring schedules.

Cleanup is one of InternalImprovementAgent's fixed duties as a built-in
maintenance agent, so it needs no grant. Its maintenance runs work for no
person: they can use the shared `team/` and `agents/` folders and their own
schedule's record, but not people's folders or schedules. TeamArchitect handles
their findings for the person the maintenance schedule belongs to (the first
user) and tells them.

Before each maintenance run, the runtime itself checks every schedule
(settings under `agent.schedule_maintenance.stale_schedules`):

- It ends the work of anyone an administrator disabled or removed
  (`/user disable`, `/user remove`), in case that did not finish when it
  happened: their schedules are deleted, and the team's own schedules they
  owned move to another active administrator, who is told in the Inbox. It
  also deletes the held folder, memory, own knowledge pages and conversations of removed people
  once `gateway.users.removed_data_retention_days` has passed.
- Owners get one Inbox notice listing their schedules that look unused: turned
  off for at least 30 days, paused more than 90 days ahead, set up for an agent
  type that no longer exists, or tied to a closed objective. Nothing changes
  if they do nothing; they remove one with `/delete schedule <id>` or run it
  again with `/resume schedule <id>`. They are told again only if the list
  changes.
- Administrators also hear about schedules whose owner the gateway does not
  know, and about any schedule of a disabled or removed person that could not
  be deleted or moved.

Every scheduled run uses its owner's current role, so changing someone's role
applies to their schedules from the next run. Any other agent doing
cleanup needs access to the memory review maintenance tool and asks for it with
`request_access`; see
[How agents get access](permissions.md#how-agents-get-access). In
authorization-disabled mode the bounded operations remain available. No raw
database or shell cleanup is needed.

## Prepare a schedule

1. Select the named specialist type and declare the exact tools the job is known
   to need. Mark tools used only on some branches as conditional.
2. Create the job with `create_scheduled_job`, `target_agent_type`,
   `worker_policy_json`, and `requirements_json`. An automatic job starts as an
   inactive draft while its known requirements are checked.
3. Inspect the returned readiness result. A missing required tool, or access the
   worker type or the owner lacks (`missing_access`), keeps the draft disabled.
   Open-ended work can be provisional because future resource choices and
   external credentials are checked when the tools run.
4. When access is missing, use `authorize_scheduled_job_access` with the returned
   policy revision (see [Give a scheduled worker its access](#give-a-scheduled-worker-its-access)).
   A missing account needs an [account link](permissions.md#account-links).
5. Recheck with `prepare_scheduled_job_access`, then activate through
   `configure_scheduled_worker` using the current revision.

To correct a job's requirements later, the lead calls `configure_scheduled_worker`
with the current revision and the complete new `requirements_json`. The new list
replaces the saved one, whether automatic worker startup is on or off. The
result shows the saved requirements and a one-line summary such as "Saved
requirements: cli:google gmail_search, cli:google gmail_read. Ready: yes …", so
you can confirm the change took effect. A `requirements_json` value in the
wrong shape is refused and nothing is saved.

For example, a WebResearchAgent job can declare:

```json
{
  "requirements": [
    {"capability_id": "fn:web_research", "operation": "brave_web_search_artifact", "necessity": "required"}
  ],
  "runtime_discovery": true
}
```

A job that checks Gmail through Google Workspace declares the two actions it
uses:

```json
{
  "requirements": [
    {"capability_id": "cli:google", "operation": "gmail_search", "necessity": "required"},
    {"capability_id": "cli:google", "operation": "gmail_read", "necessity": "required"}
  ]
}
```

`capability_id` holds a tool catalog id (such as `fn:web_research`,
`cli:google` or `mcp:<connection>`) and `operation` one of that tool's
operations; `find_tools` lists them. An operation is what the tool does, not the
function an agent calls to use it: `secure_cli` runs every command-line tool and
is not an operation. A requirement the catalog does not know is blocked with
`not_in_tool_catalog`; the check's `detail` explains the mistake and its
`suggestions` list valid operations (a tool's named operations such as
`gmail_search` before its `command:<path>` entries). The older ids such as
`builtin:web_research` are removed: a saved schedule that still names one is
blocked with `requirement_not_a_catalog_tool` (with close tool ids in
`suggestions`) until TeamLead saves its requirements again. Requirements name
operations so the job's check is exact, but the job gets each tool at the
highest level its operations need, never single operations: an operation name
can differ from the one a call uses (sending mail is both `gmail_send` and
`command:gmail.send`).

An empty requirements list with `runtime_discovery: true` permits discovery during
execution. It does not certify that every possible tool is available. Resource
constraints without a registered enforcement adapter are rejected.

### Time zones

A cron schedule runs in its owner's time zone: `0 9 * * *` is 9:00 where that
person lives, also after a daylight saving change. The zone is saved with the
schedule (`cron_timezone`) when it is created:

- A person's schedule, whether they asked an agent or used a chat command,
  takes the time zone of their profile.
- A schedule the runtime creates without a person uses the instance time zone,
  `user.timezone` in `instance_config.yaml` (`DEFAULT_REQUEST_TIMEZONE`
  overrides it).
- An agent passes `cron_timezone` (an IANA name such as `Europe/Berlin`) only
  when the person asks for another zone. `update_scheduled_job` changes it the
  same way, and schedule listings show it.

Daylight saving changes follow the classic cron rule. When clocks go back, the
times of the repeated hour happen twice: a schedule at fixed times of day (01:30
in New York, 02:30 in Berlin, or `0,30 1 * * *`) runs once at each of its times
that night, while an hourly or every-few-minutes schedule (`0 * * * *`,
`*/30 * * * *`) runs in both copies of the hour, as in every other real hour.
When clocks go forward, a time in the skipped hour runs at the first time after
it: a 02:30 schedule runs at 03:00 that day.

Interval schedules ("every 6 hours") do not depend on a time zone. Objective
work plans use their `cadence_timezone`, which is `owner` by default (see
[Configuration](configuration.md)).

Cron schedules saved before time zones existed ran in UTC. The first time the
manager starts after the upgrade, each one gets its owner's time zone (else the
instance's), and its next run moves to that local time. Every change is logged
with `event=schedule_cron_timezone_filled`.

## Resolve access discovered during a run

When a scheduled job's worker lacks access, it calls `request_access`. The
request waits in the owner's Inbox and the run shows "waiting for access" until
someone decides; TeamLead's access does not pass to the job's worker, because
the worker works for the job and the job's own access is its source. **Keep for later tasks** keeps it for this job's
later runs only. Access a job needs every time is best given at setup (see
[Give a scheduled worker its access](#give-a-scheduled-worker-its-access)).
The scheduler does not check access before a run starts; each tool call is
decided when it happens.

For a missing credential or another problem it cannot solve by asking for
access, the worker uses the structured `manager_escalation` action. Include the
schedule and the tool catalog id.
The existing delegated-task workflow waits for the lead's repair. A review message
does not itself give access; the concrete operation must pass its normal checks
when execution resumes. Never put credentials in schedule requirements or messages.

## Change the worker or launch rules

### Custom agent types

`eligible_agent_types` is a candidate list for automatic startup. Its two shipped
entries are additional candidates, not the complete list. Types already covered
by the saved default policy need no eligibility edit or new approval. The TeamLead
can add your registered custom type through the normal runtime settings workflow:

1. Check `get_available_agent_types` and the schedule's
   `inspect_scheduled_worker` result. Inspection reports registration, candidate
   membership, explicit denial and the kill switch even before a policy exists.
2. For a missing registered type, pass `eligibility.suggested_settings_change` to
   `plan_runtime_setting_change`, then call `apply_change` with
   the returned `change_id`. The proposal preserves existing candidates and adds
   the exact type at `agent.schedule_workers.eligible_agent_types`, with team scope.
   No separate conversational confirmation or restart is required for this setting.
3. Preview access. For an unbound inactive draft, save the target and requirements
   with `configure_scheduled_worker(expected_revision=0, activate_schedule=false)`.
   Give missing access for its returned revision with
   `authorize_scheduled_job_access`, recheck readiness, then activate.
   Verify actual admission and completion. Adding eligibility alone does none of
   these things. On retarget, save the new target before authorizing its operations.

Before creating a schedule, the lead can read the list the launcher enforces
from `get_runtime_configuration_context` under
`schedule_workers.limits.eligible_agent_types`. A custom specialist created
through TeamLead's reviewed workflow is listed and can be scheduled as soon as it
is created; it needs no restart.
If using generic configuration without an instance file, edit that configuration
through its operator workflow; the lead's settings tools persist to instance config.

The runtime rechecks additions at plan and apply time. Unknown types and automatic
manager creation are rejected. Explicit operator denies win over eligibility at
every launch. The lead must preserve the rest of the list when adding a type;
removing a candidate removes that route to startup permission. A type still
covered by the default policy remains eligible. Use an explicit deny to forbid it.

This changes ownership of the existing eligibility setting. Operators who need a
type to remain forbidden must use `denied_agent_types`; omission from the candidate
list can now be repaired by an authorized TeamLead. The kill switch, explicit denies
and launch ceilings remain operator-owned. Existing files need no schema conversion.

### Assignment and timing

Use `configure_scheduled_worker` with `expected_revision`. An exact agent-ID target
requires `convert_exact_target=true` before automation can use a named type.
Retargeting preserves recurrence, run counts, owner, conversation, and schedule
memory. A live claim keeps its prior target until the claim finishes or expires.
Already admitted work follows the task lifecycle; use task cancellation to stop it.
When editing a binding, preserve the inspected policy's expiry and rules that should
remain. `worker_policy_json` replaces the policy rather than patching individual fields.

Rules can be set per schedule or inherited from team and type settings:

| Rule | Default | Meaning |
| --- | --- | --- |
| `claim_wait_seconds` | 30 | Time allowed for an existing worker to claim the occurrence |
| `startup_timeout_seconds` | 120 | Time allowed for a new worker to become ready |
| `admission_timeout_seconds` | 60 | Time allowed after readiness for task admission |
| `max_launch_attempts_per_occurrence` | 2 | Maximum new starts for one due occurrence |
| `retry_backoff_seconds` | 30 | Initial delay; doubles with attempts, capped at 300 seconds |
| `launch_when_busy` | false | Whether busy existing workers can justify another start |
| `idle_ttl_seconds` | inherited | Idle lifetime from team composition when omitted |

The lead changes team defaults through runtime settings at
`agent.schedule_workers.defaults.<rule>` and type overrides at
`agent.schedule_worker_rules.<rule>` with agent-type target scope. Per-schedule
values take precedence. Every value remains subject to current operator ceilings.

Set the policy's `enabled` field to false to suspend automatic startup. Schedule
work can still be claimed by an already ready, authorized worker. Schedule
pause, disable, deletion, and task cancellation remain distinct operations.
Protected memory-review timers use this worker policy but remain restricted to
MemReviewAgent. The runtime manages these timers, so a lead-authored change to
their policy is staged and applied only after a person approves the exact change
(see [Schedules the runtime manages](#schedules-the-runtime-manages)). Other
protected system timers keep their purpose-specific workflows.

## Schedules the runtime manages

Some schedules belong to the runtime rather than to a person's request: memory
review timers, the heartbeat launcher, Objective work sessions and similar
system timers. Their timing and targets are protected:

- **Your own schedules** change at once when an agent that may use the schedule
  tools updates, pauses, resumes, turns off or removes them. Another person's
  schedule is reported as not found.
- **A schedule the runtime manages** is never changed on an agent's say-so. The
  tool returns `approval_required` with a `change_id`, and the agent calls
  `apply_change`. With authorization on, a team administrator approves the exact
  change in the Inbox (what it changes, on which schedule); the agent's task
  waits and then applies it once. With authorization off, the agent explains the
  change and applies it after you agree in the conversation.
- If the schedule's settings change before the approval, the change is refused
  and the agent stages it again. Its regular runs in the meantime do not count.
- In the gateway (`/schedules` commands and **Runtime Controls > Schedules**),
  only people who could approve such a change may pause, resume, turn off or
  remove these schedules: team administrators with authorization on, any active
  member with it off. Other people see the schedule without its controls.
- Restarting the gateway keeps a memory-review or maintenance schedule's pause,
  off state and cadence. At each start the gateway only refreshes the person
  the schedule belongs to and their current roles. Memory-review intervals in
  the configuration apply when the schedules are first installed.
- An Objective's own work-session schedule changes with the Objective's tools,
  which you already confirm; ending or pausing an Objective does not change
  linked system schedules.

## Operator controls and diagnosis

Both `config.yaml` and `instance_config.yaml` use `agent.schedule_workers` for
lead-managed eligibility and timing defaults, plus the operator-owned
`default_startup_policy_enabled` switch (true by default), kill switch, explicit
denies, capacity/rate limits, timing ceilings,
review interval, and evidence retention. Type overrides live under
`agent_types.<Type>.agent.schedule_worker_rules`. The lead cannot change the
operator controls. These settings are read from the saved
`<instance_root>/instance_config.yaml` on every check, so a saved change applies
without a restart, in every deployment. Removed definitions and newly
disallowed types block later work.

Use `inspect_scheduled_worker` to see the active and desired target, effective
rules, readiness evidence, launch state, attempts, next deadline, occurrence
custody, terminal outcome, and timing. A starting worker has not yet accepted the
task. A ready worker has completed transport setup and recovery. Admission means
the task and occurrence receipt committed to storage together. Completion means
the task produced a terminal result.

The default policy is evaluated against the current shipped catalog on every
check; nothing is saved per team. Team-written types never gain default
permission. Turning the default switch off prevents default-policy starts,
including workers still initializing. Explicit lead-configured policies continue
under their own rules.

Schedules continue to use the shared database claim path. The startup service
counts only workers registered in the local process; a remote NATS listener cannot
suppress a needed local start. NATS carries worker
presence, notices that an access request was decided, and the chat messages about
stuck schedules. The scheduler does
not publish a second copy of the scheduled payload to a worker queue.

A worker renews its launch lease on every scheduler pass. If it cannot renew for
the whole lease (120 seconds by default), for example during a long database
outage, the lease is lost and the TeamLead may treat that launch as gone. The
worker then winds down instead of continuing without custody:

- It stops taking new work and returns tasks it has not started, and the task it
  was running, to the TeamLead.
- Tasks that were waiting for an answer from a person or a delegated subtask may
  still finish on that worker for up to `lease_loss_wait_timeout_seconds`
  (30 minutes by default). After that, the requester receives a failed result
  explaining that no reply arrived in time.
- The worker exits once it holds no unfinished task.

The TeamLead sends returned tasks to another worker of the same type, starting
one when none is available, and the result still goes to whoever asked. A task
that was interrupted mid-run starts again with a note listing the actions the
first run had already sent, so the new run checks them before repeating anything.

An interrupted scheduled run is only rerun when that cannot collide with the
schedule itself. It is skipped when the schedule already ran again, is paused
or disabled, or its next run is due within `interrupted_run_skip_window_seconds`
(15 minutes by default, editable by the TeamLead under
`agent.schedule_workers.defaults` or per agent type). A schedule never runs
twice at the same time: if it comes due while its previous run is still going on
any live worker, the new occurrence merges into that run, and the owner is told
once when this happens on the same worker.

If the same work is interrupted this way three times in a row
(`max_consecutive_interruptions`), the runtime stops retrying it. A task is
cancelled and its requester told; a scheduled run is closed, counting all runs of
that schedule since its last finished run. The TeamLead then receives a review
task with the evidence, tells you what happened and what it recommends, and may
pause the schedule if further runs would likely fail the same way.

Look for `worker_wind_down_started`, `worker_task_handed_back`, and
`worker_wind_down_exit` in the worker's log, and `worker_task_handback_received`
and `worker_task_redispatched` or `scheduled_rerun_skipped` in the TeamLead's
log. `scheduled_fire_coalesced_elsewhere` marks a fire merged into a run on
another worker, and `worker_interruption_escalated` marks work the runtime stopped
retrying.

A worker start whose process cannot be checked keeps its capacity slot until
the process is proven gone, and new runs never attach to it. Every runtime run
(each start of the supervisor) holds a lock file, `runtime/process_run.lock` in
the instance folder, for as long as any of its agent processes lives, and each
start records the run it belongs to. After a restart, or after the container is
replaced, the supervisor can only start once every process of the earlier run
is gone, so starts left by the earlier run are settled on the next scan and
their schedules run again. If a process of an earlier run is still alive, the
supervisor refuses to start and says so; stop it first. Starting a worker never
consumes an occurrence. Stable occurrence receipts prevent duplicate
admission across a crash before schedule advancement; external tool effects
retain their own retry and reconciliation requirements.


### Quiet checks and conversation history

A recurring worker can keep results in its schedule's conversation and finish
without sending a message when nothing has changed. Prior messages in that
conversation do not make the new run a follow-up request. An explicit follow-up
or delegated subtask still returns a response to its requester.

`task_response_suppression_cleared` means the runtime refused a request to suppress
delivery. Its diagnostic includes the current task, conversation, scheduled job,
and follow-up task IDs. `followup_work` means the incoming task had a follow-up
reference; it does not indicate a Gmail or permission failure. New recurring
runs no longer acquire that reference just from sharing conversation history.

## Bounded inspection results

Agents receive up to 20 schedule or pending-review summaries per page. They can
follow `next_offset` or inspect a supplied schedule/review ID directly. Pending
review filters apply before the database limit, so old completed or expired
records cannot hide queued work. Listing does not claim or retry a review.

`get_schedule_telemetry()` now returns counts by default. Request
`include_schedules=true` for a metadata page, optionally filtered by `schedule_id`.
`list_scheduled_jobs(schedule_id=...)` and `get_memory_review_job(review_job_id=...)`
return exact details in excerpts of at most 4,000 characters, with a digest to
check that subsequent pages still refer to the same content. Full instructions,
review sources and run histories are no longer included in default listings.
Existing custom tooling that reads those fields must use the exact-detail calls.
This changes tool responses only; saved schedules and reviews need no migration.

New scheduled memory-review tasks also carry saved job IDs instead of copying
all review sources into the initial prompt. Workers read current status and
source excerpts through the exact-job tool.
