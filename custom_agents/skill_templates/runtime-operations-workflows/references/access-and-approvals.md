# Access, approvals and tools: how they work here

Use this to answer people's questions in plain words and to choose the right
action. Check the person's actual situation first (`list_my_access`,
`find_tools`); explain what applies to them, not the whole model. Then act:
hand the access on, ask for it, propose it, or name the one step only they can
take. When the design here does not fit what they want, say so plainly and
offer the nearest workable option or who can help (an administrator or the
operator).

## Two modes

- **Access control on** (`authorization.enabled: true`): everything below
  applies.
- **Access control off** (personal use): there are no grants, requests or
  Inbox reviews. Each agent has its role's standard tools plus the default
  tools in its definition. Platform changes (settings, restarts, specialists,
  skills, MCP servers) still need the person's agreement in the conversation,
  and the sandbox and broker still apply.

## Whose work, and the two checks

- Every tool call is checked twice: the agent must hold access for **the
  person its task works for**, and that person must hold it too. An agent
  never gets more than its person.
- Access is per person. What Ann approved lets agents work for Ann; the same
  agent working for Ben has only what Ben approved.
- Internal maintenance (InternalImprovementAgent's timer runs) works for no
  person: it uses only its fixed duties, reaches only `team/`, `agents/` and
  its own schedule, and its findings go to the maintenance schedule's owner.
  Memory review works for the person whose conversation it reviews. Each
  active person has their own personal-facts audit and stale-conversation
  review timers, which work for that person only; the core-facts audit is one
  team timer.
- A person's schedules run for them with their role at the moment each run
  starts, so a role change applies from the next run. Disabling or removing
  a person deletes their own schedules at once; the team's timers they owned
  move to another administrator. Before each maintenance
  run the runtime also tells owners, in the Inbox, about schedules that look
  unused; they remove one with `/delete schedule <id>` or keep it with
  `/resume schedule <id>`.

## Where access comes from

- **Standard tools** come with a role: every agent can manage its own work,
  use files in its task's folders, and use the internet; TeamLead also has
  the team tools. They need no grant and cannot be removed, only blocked.
- **Starter profile**: each person chooses TeamLead's standing access for
  their own work with `/access profile` or on the Access page.
  - Trusted: the standard tool set plus shell and Python in the sandbox, which
    TeamLead may hand on to workers without asking (the person gets a notice).
    For an administrator it also gives TeamLead model choice (below) and
    read access to their saved credentials (search Sealbox, read a credential's
    name and username; never the secret), kept to TeamLead: to find a
    credential, look it up directly. Using or saving one still asks.
  - Careful: read access to the standard set; changes are asked for.
  - Manual: nothing; everything is asked for.
- **Grants** a person approves. Each can last for this task (the whole
  request, including workers helping with it), for 1, 7 or 30 days, or until
  removed ("Keep for later tasks"). Access is a level on a whole tool, never
  one operation, and one entry per tool: approving more replaces what was
  there, and Remove takes the whole entry.
- **Access follows the work.** Only TeamLead keeps access between tasks, per
  person. Workers keep nothing: each task gets what TeamLead may hand on
  without asking, when it first needs it, with no request and no notice.
  "Keep" on a worker's request gives it to TeamLead (the worker has it now).
  The card's "TeamLead may hand this on to workers" box starts ticked.
  TeamLead's access for a person counts in all its work for them, its
  scheduled runs too. A person's scheduled job that a worker runs holds its
  own access, and the job's worker uses only that; TeamLead's own schedules
  and the runtime's timers work like a person's request.
- **Blocks** stop access for an agent type, an agent, a person, a role or the
  whole team, even where a grant or a standard tool would allow it.
  A block on an operation an update removed stops the whole tool. Its Inbox
  notice lists the current operations. The person who added it, or an owner
  or administrator, can narrow it with
  `/access block <who> <tool>#<current operation> --reason "why" --replace <block id>`.
  That command swaps the blocks together, keeping the same subject, tool and
  expiry; a failure leaves the old block in force. Agents cannot replace it
  or override it by requesting access. To keep the whole tool blocked, the
  person does nothing.

## Asking for access

- A worker that lacks a tool its task needs gets it at once when TeamLead holds
  it for that person and may hand it on; otherwise it calls `request_access`,
  the task pauses and the person decides in the Inbox. Nobody is asked in chat.
  In a scheduled job's run a person always decides, and "Keep" gives the
  worker's request to that job.
- Who decides: the person the work is for, when they hold that access
  themselves; otherwise an administrator (also for system work).
- A grant takes effect on the agent's next step; no restart. The agent's tool
  list shows only what it holds for the current person, and `find_tools`
  finds the rest. Some groups the agent holds (TeamLead's objectives,
  capabilities, runtime settings and others) load on request: `use_tools`
  adds them for the task, because one model request may carry only so many
  tools (`max_tools_per_request` per model, 128 by default). Loading never
  gives access.
- `propose_access_change` is for what nobody can hand on: a new web API tool,
  a new command-line tool (CLI), a new MCP server, a data folder, several
  grants and blocks together, or an account link. An administrator reviews one
  card listing every item; it is applied all together, then any credential
  forms open for the approver, and the proposal's task resumes once those forms
  end, told which links exist.
- **Adding a service**: prefer, in order, an existing catalog tool, a CLI the
  broker runs (one compact `secure_cli` provider, a level per command, the
  token hidden), a web API tool for simple REST with a key or token, and an MCP
  server when the vendor provides one; say why in one line. A CLI's program
  goes to `team/tools/<name>/<file>` (InternalImprovementAgent can build or
  download it there) and the proposal pins its SHA-256; the approval can also
  download it from an https address. Every run uses a private copy checked
  against that digest, inside the same sandbox as agents' code: it reads only
  system files and its own run folder, where the person's credential is put in
  an environment variable or a file, never on its command line. The network is
  open (the sandbox cannot limit hosts). A changed program is refused; an
  update is a new proposal. Commands without a level need full; classify
  deleting, trading and paying as full. After approval, `set_up_tools` opens
  the account link and any setup steps.

## Tools and services

- **Web API and command-line tools** (Google Workspace, GitHub, X, web APIs)
  work through the broker. Access decides whether an agent may use them; the
  account decides whose credential is used. GitHub and Google use only the
  person's own sign-in; X uses the person's own sign-in first, then the team's
  linked X account while the team shares X; other tools use an **account
  link** (the person's own link first, then the team's while the team shares
  the service). Each team chooses per service whether its account is shared or
  private (`/accounts` shows it; administrators change it): X starts private,
  Exa, Brave Search, Hugging Face and new web API tools and MCP servers start
  shared, the team's own Crawl4AI server is always shared, and GitHub, Google
  and the GitHub API (`http:github`) are always private. A private service never uses the team's account, not
  even for work that serves no person. Google and X sign people in through
  the team's sign-in app, which an administrator sets up once (`/auth google
  team_client`, `/auth x team_client`). The runtime keeps each person's last
  known setup state per tool (ready, needs setup, or unknown) and shows it to
  TeamLead; a ready tool is used without a check. Otherwise TeamLead calls
  `set_up_tools` straight away, without asking whether to start (the
  approval cards and setup pages are where the person agrees). Trusted code
  asks for missing access, opens the sign-in pages, account link forms, team
  sign-in app pages and steps done outside Universal Agents (from the tool's
  catalog setup data) in the person's chat at once, in order where one needs
  another, offers a fresh page after an expired one (at most three per tool
  and kind), and resumes the task when everything is done or the person
  closes a page or denies a request. Two or more things to do get one access
  workflow page with a step for each and a final work step; its steps update
  by themselves, and the person marks outside steps done there. An
  administrator's step for a person who is not one is explained, nothing is
  opened, and the task ends. People can also set up every tool with
  `/link <tool id>`, or `/link <tool id> team` for the team's account
  (administrators only, refused for a private service); the chat where they
  ran it then gets one message saying whether it worked, and a task of
  theirs waiting on that setup resumes. Neither a sign-in nor a link grants
  access.
- **MCP servers**: TeamLead proposes one with a single `mcp_connections(action="propose")`
  call; an administrator approves installing it on one card that also lets
  TeamLead use all of its tools for the requester's work and hand it on to its
  workers' tasks. After that, the agents use every tool the server offers
  without asking again, including tools it adds later; other people authorize
  it for their own work like any team tool. An administrator who approves a
  server in Settings lets the agent types it names use it for everyone's work.
  If a server does not answer, agents keep trying for up to an hour (the
  operator's `mcp.load_retry_window_minutes`) and then tell administrators in
  the Inbox; `find_tools` and `mcp_connections(action="list")` show the reason
  (`load_status`), so tell the person instead of asking for a restart.
- **Code**: shell commands and Python run in a sandbox that reaches the
  person's own folder and the shared `team/` and `agents/` folders, and the
  network through internet access. Running outside the sandbox
  (`code:unconfined`) is a separate grant for a stated need.

## Files and folders

- Each person has a private folder; agents working for them use it by
  default. `team/` and `agents/` are shared.
- People share their own files or folders with another person (`/share`, or
  Share in the workspace browser), and send a file to someone else's folder
  (`/send`, which waits for that person's or an administrator's approval).
  Agents cannot share for them.
- Folders outside the workspace reach agents only as **data folders** that an
  administrator registers (`/access folders`, the Access page's Files tab, or
  a proposal); agents still need access to each one when access control is on.

## Platform changes

- Settings (including SOUL.md and the roster), restarts, new specialists,
  team skills and hosted MCP servers are staged as exact plans and applied
  with `apply_change` after a person approves that exact plan in the Inbox
  (with access control off, after the person agrees in the conversation).
  Remote MCP servers are installed through a proposal (above).
- A waiting change is announced in the person's chat with a one-line summary
  that trusted code writes from the plan (what changes, and when it takes
  effect), with Approve and Deny: buttons in Telegram and Discord, a card in
  Web Chat. The full detail stays in the Inbox. Agent wording never becomes
  that summary.
- Stage only the settings the person asked for. When switching a model, keep
  the current provider unless they ask to change it; a routing prefix in a
  model id (`openrouter/…`, `anthropic/…`) is a route on the configured
  LiteLLM endpoint, not a reason to change the provider.
- **Model choice** is the exception for switching models: a plan that changes
  only an agent type's provider (one this instance is already set up for),
  model, reasoning effort or summary, context window, or image, audio and file input applies
  at once when TeamLead and the person it works for both hold model choice
  (`fn:model_choice`). Model settings apply to the whole team, so only
  administrators hold it; the Trusted profile gives it to TeamLead for their
  work, and each change sends them a notice with the previous values. For a
  member, or a plan that also changes anything else (team-wide defaults, the
  request API mode, other settings), an administrator approves it in the
  Inbox. Blocking TeamLead from Model choice brings the review back. After a
  model change applies, the result lists every setting that agent type now
  runs with (`model_settings`); show that table as it is.
- Model-affecting plans test each target during staging and again before saving,
  including instance-baseline `llm.*` settings with their own endpoint. A failed
  staging test creates no plan. Tell the person when the model answers and ask
  them to confirm. With access control off, a tested switch requires
  `apply_change(..., confirmed=true)`; with access control on, `confirmed`
  grants no authority and the access-service decision still applies.

## Where people see and change it

- **Inbox**: requests waiting for them, each card saying what each choice does,
  and notices of lasting hand-downs.
- **Access page**: by agent type and scheduled job (including TeamLead's
  standing access for their work; one row per tool with a Remove button that
  removes all of it), by person, by tool, and Files (shares and data folders).
- Chat: `/access`, `/access profile`, `/share`, `/send`, `/access folders`.
- People (owners and administrators, in chat): `/user disable` is a pause
  they can undo with `/user enable`: the person stays on the team, their
  running work is cancelled, their objectives stop and their schedules are
  deleted; their folder, memory, conversations, access and linked accounts
  are kept. `/user remove` is for someone who left: their work, task records,
  objectives, sessions, grants, personal account links and credentials are
  deleted at once, and their folder, personal memory, own knowledge pages and conversations are
  held for `gateway.users.removed_data_retention_days` (7 by default) before
  a maintenance run deletes them. `/user restore <usr_...>` brings a removed
  person back with the held data while it is held. The team's own schedules
  they owned move to another administrator in new conversations, and heartbeat
  checks get new conversations before their launcher changes owner. If that
  preparation fails, the launcher stays stopped until a retry succeeds.
  When cleanup reports a failure, direct an administrator to repeat the same
  `/user disable` or `/user remove` command; never recreate the launcher or
  reuse the departed person's conversation to make it run.
  Agents cannot do any of this;
  tell the person to ask an owner or administrator.
