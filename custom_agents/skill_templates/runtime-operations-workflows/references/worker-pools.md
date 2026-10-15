# Worker pools: delegated work, scaling and reviews

Read this when you hand out many tasks at once, when delegated work seems slow
or stuck, and for every `worker_pool_review_...` task.

## How delegated work runs

1. Each `delegate_task` to a worker type puts the task in that type's shared
   queue. Nothing picks a worker for it.
2. A worker of the type asks for one task only when it is free, takes it, and
   tells you it took it (you see "Worker X took delegated task Y" in the task
   history). Tasks still in the queue are not checked or timed out: the queue
   holds them.
3. The worker pool controller (runtime code, not you) watches every queue. When
   tasks wait and no worker is free it starts more workers, up to the type's
   pool size and the team limit. A type with no worker gets one at once; more
   start after a task waited `scale_up_wait_seconds` (20 by default). Idle
   workers leave after their idle time, so pools shrink on their own.
4. Your request waits. You are woken when every delegated task has ended, at
   once when one fails for a reason the runtime reports (`provider_limited`:
   the model provider kept refusing calls; `worker_lost`: its workers kept
   being lost), or when a `check_back_after_minutes` you set on a delegation
   passes. Trusted code sends the person counts meanwhile ("9 of 24 done, 4
   running, 11 waiting for a free worker").
5. If a worker dies or loses its launch lease, its task moves to another worker
   on its own, up to `max_retries_after_worker_loss` (3) times; then it fails as
   `worker_lost` and you decide.

To run many tasks in parallel, put one `delegate_task` per task in one turn. You
never need to start workers for delegated tasks. `instantiate_agent` stays for
special cases (for example a worker that must exist before work arrives).

A specialist type you create is a worker type at once: it is listed in your
routing context, its queue and pool work like any other, and no restart is
needed.

To send a task to one specific running worker (it needs context only that
worker has, or only it is set up for the work), also set `target_agent_name` to
its full name as `inspect_worker_pools` or `inspect_delegated_work` shows it.
The task goes to that worker's own mailbox: it waits behind that worker's
current work, no other worker can take it, the controller adds no workers for
it, and it is refused if that worker is not running. While it waits it is
checked and timed out like running work.

When you are woken early and want to keep waiting, end the turn with a `noop`
action: the request waits again and you are woken when the rest ends. To finish
early, stop what you no longer need with `cancel_delegated_task`, then answer
with what you have.

## Tools

- `inspect_worker_pools(agent_type="")`: per type, tasks waiting and the oldest
  wait, idle workers asking for work, workers running, starting, draining or
  waiting out a provider limit, the pool size in force (`effective`, its
  `source`: temporary, lasting or default, and the operator `ceiling`), the
  controller's last decision, open problems and recent size changes.
- `inspect_delegated_work(task_id="")`: the current request's delegated tasks:
  waiting, running (on which worker), done, failed (and why), moves to other
  workers.
- `set_worker_pool_limit(agent_type, max_workers, reason, for_minutes=0)`: set a
  type's pool size (or `agent_type="team"` for the team limit); `for_minutes`
  makes it temporary. Inside the operator ceilings only; administrators get a
  notice with your reason every time.
- `cancel_delegated_task(task_id, reason)`: stop one delegated task of the
  current request; a waiting one is withdrawn from its queue.

## Worker pool reviews

The runtime gives you a `worker_pool_review_...` task when:

| Kind | Meaning |
|---|---|
| `capacity` | A pool (or the team, scope `*`) reached about 95% of its limit while tasks waited. For small pools that is the limit itself. |
| `provider_limited` | Tasks of a type keep hitting the model provider's limits (rate limit, quota, overload), or a worker waits one out while tasks wait. No workers are added meanwhile. |
| `start_failed` | The runtime could not start a worker it needed; the evidence says why. |

Each review works for the team's maintenance owner (an administrator), so
`notify_user` reaches them. Check `inspect_worker_pools`, then choose:

- **A, a larger pool for a while:** `set_worker_pool_limit` with `for_minutes`.
  Fits a burst of work (many tasks now, normal load later) when the provider is
  not limiting.
- **B, a larger pool for good:** `set_worker_pool_limit` without `for_minutes`.
  Fits capacity reviews that keep coming back.
- **C, change nothing:** tasks wait their turn. Fits when waiting is acceptable,
  and often for provider limits.
- **D, fewer workers:** a smaller size, for a while or for good. Fits when the
  provider's limit or cost is below what the pool would use. Surplus workers
  finish their work and leave; nothing running is interrupted.

Never choose A or B for `provider_limited`: more workers make more calls and
more refusals. A pool size does not fix `start_failed`: check the agent type's
definition, the operator's denied types and start-rate limit, and tell an
administrator what to change. Above an operator ceiling only an administrator
can help (`agent.worker_pools` in the configuration); say so.

When the review says the problem keeps coming back, tell administrators with
`notify_user`: what happens, how often, the likely cause and your
recommendation. They also get an Inbox notice from the runtime.

Finish every review with a short task response saying what you decided and why.
Do not delegate the waiting tasks again; they are still queued.

## Troubleshooting

| What you see | What it means and what to do |
|---|---|
| Tasks wait, `workers_running` 0, `decision` mentions a start problem | A worker cannot start; see the open `start_failed` problem and tell an administrator. |
| Tasks wait, pool at its size, nobody free | Normal under load. Choose A, B or C. |
| `workers_waiting_out_provider_limit` > 0 | The provider refuses calls; workers retry on their own after a growing wait. Choose C or D. |
| `workers_unknown` > 0 | A start whose process cannot be checked holds a slot until it is proven gone; tell an administrator if it stays. |
| A delegated task moved between workers several times | Its workers keep dying or losing their lease; look at the task history and the stuck-work notices, and report a repeating cause. |
| A delegated task runs for hours | Check its worker with `inspect_delegated_work`; stop it with `cancel_delegated_task` if it is no longer needed. The runtime also tells the person about work without progress. |
