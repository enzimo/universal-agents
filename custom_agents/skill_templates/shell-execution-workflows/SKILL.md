---
name: shell-execution-workflows
description: Use before running Universal Agents shell commands, command lists, parallel probes, build or test processes, or commands that need explicit working-directory, timeout, cancellation, RTK, OpenShell, or result-handling behavior.
---

# Shell Execution Workflows

Use this skill before calling the Universal Agents `shell` tool.

## Call Shape

- Pass the underlying command as a string. Do not prefix it with `rtk`; the
  shell boundary applies RTK automatically.
- Pass a list to run multiple commands. Sequential lists are fail-fast and
  preserve input order. A successful standalone `cd ...` changes the working
  directory for later sequential entries, but prefer `work_dir` for clarity.
- Use `parallel=true` only for independent commands. Parallel results retain
  input order. With `ignore_errors=false`, the first failure cancels running or
  pending siblings.
- Use `ignore_errors=true` only for deliberate best-effort collection where a
  failed command must not stop the remaining entries.
- Set top-level `timeout` and `work_dir` for common defaults. Override either
  value for one entry with
  `{"command":"...","timeout":30,"work_dir":"relative/or/absolute/path"}`.
- Keep `work_dir` and path arguments inside this task's folders. The default
  working directory is the person's folder (or `agents/` for work with no
  person). Relative paths resolve from the active command working directory.

## Non-Interactive Execution

- Expect closed stdin and no PTY. Never run editors, pagers, TUI selectors,
  password prompts, confirmation prompts, or commands that wait for manual
  input.
- Add supported non-interactive flags such as `--yes`, `--no-input`,
  `--batch`, or `--no-pager`.
- Set an explicit timeout for network, package-manager, build, test, and
  long-running service probes. The process supervisor terminates the complete
  child process tree on timeout or cancellation.
- Do not start a persistent server directly. Use a bounded health probe,
  purpose-built harness, or separately supervised service.

## RTK Behavior

- Expect supported commands to run in `rtk_mode=rewrite` through RTK's
  token-optimized wrapper.
- Expect unsupported commands to run in `rtk_mode=proxy` through the tracked,
  unfiltered `rtk proxy` path.
- Treat `rtk_mode=missing` and its warning as an observable degraded path: the
  original command ran without RTK filtering.
- In OpenShell mode, RTK selection happens inside the sandbox. The host-side
  command invokes `openshell sandbox exec`, so `Executed Command` is diagnostic
  metadata rather than the original command text.
- Do not treat an `rtk` or `rtk proxy sh -c` wrapper in `Executed Command` as a
  failure. Judge the result from status, exit code, termination reason, output,
  and error.

## Result Handling

- Read `Output` as stdout and `Error` as stderr; they are captured separately.
- Check `Status`, `Exit Code`, `RTK Mode`, `Termination Reason`, `Duration
  Seconds`, and `Warning` before deciding whether to retry.
- Interpret termination reasons explicitly: `exited` means the child returned,
  `timeout` uses exit code 124, `cancelled` uses exit code 130, and
  `spawn_error` means the executable could not be started.
- Preserve useful partial stdout/stderr from timed-out or cancelled commands
  when diagnosing a failure.
- Change the command, inputs, working directory, timeout, or hypothesis before
  retrying. Do not repeat an identical failing shell call.

## Sandbox

- Outside OpenShell mode, every command runs in a Linux Landlock sandbox. It
  reaches the folders you and the person you work for may use: normally the
  person's own folder (the default working folder), the shared `team/` folder
  and the shared `agents/` folder, plus any folder you were given, read-only
  where you may only read. It also gets a private temporary `HOME`/`TMPDIR`
  that is deleted after the command, and reads operating-system and
  interpreter files. A folder with a blocked subfolder inside it is left out
  entirely; the result's sandbox note says so.
- The network is open: every agent has internet access (`fn:internet`) unless
  a person blocked it for the agent type, the agent, themselves or the team.
  With a block, TCP connections and listening sockets fail with a permission
  error. A request cannot lift a block, so tell the person what the command
  needed the network for.
- Team control files, instance configuration, `team.env`, credentials,
  databases, runtime records, other people's folders, and other processes are
  not reachable. `Permission denied` for a path outside these folders is the
  sandbox, not a bug to work around.
- Files written under `HOME` or `TMPDIR` do not survive the command. Put
  anything you need later in the person's folder (personal results) or
  `agents/` (non-personal working material).
- Put temporary and intermediate files that must outlive one command
  (downloads, conversions, drafts) in `scratch/` of the task's default folder.
  Maintenance deletes old files there automatically, so keep anything the
  person should keep outside `scratch/`.
- A result with `reason_code=code_sandbox_unavailable` means the operator's
  system cannot apply the required sandbox; report it instead of retrying.
- If the task genuinely needs more, ask with `request_access` and say exactly
  why: `folder='<folder>'` (below `team/`, `agents/` or `data/<name>/`) for a
  folder, or `tool_id='code:unconfined'` with `level='full'` to run commands
  outside the sandbox. Unconfined code can read and change everything the
  runtime can, so ask for it only for a stated need and preferably for this
  task (`lifetime='task'`). With it approved, pass `unconfined=true` on the
  commands that need it; every such command is logged.
- With authorization off there are no requests: only the operator can turn the
  sandbox off. Tell the person exactly what the command needs and why.
- In OpenShell mode, commands run in the person's own OpenShell sandbox
  instead. It holds copies of the person's folder, `team/` and `agents/` made
  when the sandbox was created; files written on the host later are not in
  it, and files a command writes there are not visible to the file tools. The
  operator's OpenShell policy decides the network, and folder grants do not
  change what it reaches. A command run with `unconfined=true` runs on the
  host instead.

## Authorization Denials

- A refused command returns `status: "access_denied"` with `tool_id`
  (`code:shell` for shell commands, `code:python` for managed Python),
  `operation`, `level`, `reason`, and `next_step`. Call `request_access` with
  that `tool_id`, `operation`, and `level` and a plain reason people will read.
  Ask for the smallest `lifetime` that fits: `one_time` for one command, `task`
  for this task, or the default `until_revoked` when the work recurs. A
  one-time `code:unconfined` grant covers one unconfined command.
- When TeamLead may hand the access on, you get it at once. Otherwise your task
  pauses until a person decides in the Inbox, and the call returns the
  decision. Never ask people to grant access in chat.
- Retry the exact command only when the result says `granted`. An approval for
  reading only does not cover a command that needs write access, and an
  approval for this task only ends with the task.
- After `denied`, do not retry, and do not try other command spellings,
  programs, or tools to get around it. Find another way or tell the person.
- Access never bypasses the sandbox or the policy boundaries below.

## Policy Boundaries

- Expect secret-like environment variables to be stripped unless the operator
  explicitly allowlists them. The public shell tool does not accept arbitrary
  environment overrides.
- Do not call credential-owned CLIs such as `gh`, `gog`, `bw`, `op`, `vault`,
  or `keepassxc` through shell. Use `secure_cli` and the
  `secure-credential-workflows` skill.
- Do not call localhost or loopback services through shell. Use `secure_cli`,
  `secure_requests`, or another broker-owned tool for broker, Sealbox, NATS,
  Postgres, and similar local-service access.
- Treat folder-path, credential-command, loopback-target, and environment
  validation failures as policy decisions, not commands to evade with nested
  shells or alternate executables.
