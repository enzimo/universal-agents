# Access and approval

Universal Agents separates a request to do something from the authority to do
it. An agent may propose an operation and explain why it needs access. Trusted
runtime code works out which tool and operation the request is for and checks
the access of the agent and of the person its task works for. When access is
missing, a person decides that exact request.

This separation matters most for tools that change state, use credentials,
install code, connect to external services, or affect another agent.

## Access levels

Every operation an agent can call has a level:

- **read** looks without changing anything, such as searching mail or listing
  issues;
- **write** creates or changes things, such as sending mail, creating an
  issue, or writing a file;
- **full** covers everything else: deleting, administering an external system,
  changing access or platform settings, and any operation nobody has
  classified.

The levels of built-in tools, broker actions, and `gh`/`gog` commands ship with
Universal Agents (`ua_definitions/access`). An operation that is not classified
always needs full. Some built-in tools take an action, such as
`memory(action="delete")`: each action is an operation of its own, named
`<function>.<action>` (`fn:memory#memory.delete`), with its own level, so a
block can stop one action. With authorization enabled, the secure tool broker checks
these levels for every agent request it runs: broker actions, MCP tool calls,
and web requests. Both the agent and the person its task works for need a
grant at that level; see [Tool access and account links](#tool-access-and-account-links).
Tools that run inside the agent runtime, such as file, shell, and schedule
tools, are checked the same way before every call; see
[How agents get access](#how-agents-get-access). Each file and folder an agent
opens is checked too ([Files and folders](#files-and-folders)), and so is what
each shell or Python command can reach ([Shell and Python code](#shell-and-python-code)).

MCP servers describe their operations with hints (for example "read only"),
but a server can claim anything, so every MCP operation needs full until an
administrator confirms a lower level. Confirming is optional: approving an MCP
server normally gives full access to the whole server, which covers every
operation, so agents never come back for each one (see
[MCP permissions](#mcp-permissions)). Confirm levels only when you want to give
someone read-only use of a server:

```bash
assistant_cli.py mcp-operation-levels --config /data/instance_config.yaml
assistant_cli.py mcp-operation-levels --config /data/instance_config.yaml \
  --connection crawl4ai --accept-suggested --set delete_index=full --by alice
```

`--by` must be an active team administrator or owner (default: the instance's
default user); anyone else is refused. An unconfirmed operation, and one whose
definition changed since it was confirmed, needs full.

For a web API of your own (for example a Portainer server), TeamLead can
propose a [web API tool](#web-api-tools) for an administrator to approve.

## Whose work access serves

An agent always works for one person at a time: the person who asked, or the
owner of the schedule that started the run. What the agent may do depends on
that person:

- **Access you give agents serves your work.** When you approve a request or
  give access with `/access grant`, agents use it only while they work for
  you. When the same agent type works for someone else, it has only what that
  person gave it; nothing you gave carries over. TeamLead also cannot hand on
  to a worker what you gave it while it works for someone else.
- **You decide for your own work.** Every team member holds the standard tool
  set (everyday tools, never code execution or platform settings) and can give
  agents any of it for their own work. A request for access you hold is
  decided only by you; an administrator does not decide it for you.
- **Administrators decide the rest.** A request for access the person does not
  hold themselves goes to administrators, and approving it gives the person
  that access too. So does a request from work that runs for nobody, such as
  built-in maintenance. Administrators can also give agents access for
  everyone's work, with `--everyone`.
- **The first user is the administrator** (the team owner) and in normal use
  works like any other user: they decide requests for their own work and act
  as an administrator only when a request needs it.
- **Agents never exceed their person.** Every call is checked for the agent
  and for the person its task works for.
- **You can always remove access that serves your work**, with **Remove** on
  the [Access page](#the-access-page) or `/access revoke`.

The Access page shows each agent type's access grouped by whose work it serves.

## How agents get access

With authorization on, every tool call an agent makes is checked before it
runs: first the access the agent holds for the person its task works for,
then that person's own access.

**Standard access.** Every agent may use the tools it needs for its own work
without a grant: messages, its task queue, memory, skills, its execution plan
and notes, its own status and configuration, the person's schedules and a
record of the scheduled run it is doing, the file tools, and the access tools
below. [Internet access](#internet-access) (web research and the network for
code) is standard too, for every agent and every person, until someone blocks
it. An agent may read and write the folder of the person its task works for
and the shared `team/` and `agents/` folders; other folders need a grant or a
share (see [Files and folders](#files-and-folders)). TeamLead may also create
and steer workers, keep objectives and its heartbeat checklist, stage platform
changes for you to approve, and [propose access changes](#access-proposals).
Built-in maintenance agents (the memory reviewer and the internal improvement
agent) may do their fixed duties, including sandboxed shell and Python, and only
they may run with no person behind the task. The internal improvement agent's
timed maintenance runs work for no person: they reach only the shared `team/`
and `agents/` folders and their own schedule, never people's folders or
schedules. Their findings go to TeamLead, which handles them for the person the
maintenance schedule belongs to (the first user), with that person's access.
When a maintenance agent works for you (for example your memory
review), it is checked against your access like any other agent, and it sees
only your own review jobs.

**Access follows the work.** Only TeamLead keeps access between tasks, one set
per person: what you let it do for you, and what it may pass on. Workers keep
nothing of their own. Each task TeamLead gives a worker gets the access that task
needs from what TeamLead may pass on for you, the moment the worker first needs
it, with no question to you. That access ends when your request ends. A
scheduled job that a worker runs for you holds its own access, which every agent
in its runs uses, and the job's worker uses only that. So an agent inherits the
access of whom it works for: TeamLead uses its access for you in all its work
for you, scheduled runs included; a worker uses what TeamLead passes on to its
task, or, running your scheduled job, that job's access. Schedules TeamLead runs
itself and the runtime's own timers (heartbeat checks, objective work sessions,
after-action reviews) are not jobs: they work like one of your requests. Every
agent also has the standard access of its role and any team default an
administrator gave for everyone's work. Access is always a level
(read, read and change, or full) on a whole tool, never one operation.

**One entry per tool.** An agent's access to one tool, for one person's work, is
one entry. Approving more for a tool replaces what it had when the new access
covers it (same or higher level, same or longer time, same or more handing on),
and what was handed on from the old access moves to the new one. Access for one
task can sit beside lasting access as a step-up, for example "read and change
until removed, and full while one task runs". Removing the entry removes all of
it, and what was handed on from it.

**The tools an agent sees follow its access.** An agent starts with the
standard tools of its role. Before each step (each call to its model) it also
gets the tools it holds for the person it works for now, and loses the ones it
no longer holds. So a grant given during a task can be used on the agent's
next step, without a restart; a removed grant or a new block takes the tool
away on the next step; and the same agent shows different tools when it works
for different people. The agent finds tools it does not hold with `find_tools`
and asks for them with `request_access`. An installed MCP server is a tool
like the others (`mcp:<name>`): its tools appear on the next step once the
agent holds access to it for the person it works for, and go away when it no
longer does (see [MCP permissions](#mcp-permissions)); each call is still
checked. With authorization off, an agent has the tools its definition lists
and the MCP servers installed for its type.

**TeamLead's standing access for your work.** Each person chooses how much
TeamLead may do for their own work without asking each time, and whether it
may pass that on to workers:

| Profile | For your work, TeamLead receives |
| --- | --- |
| `trusted` (Trusted team) | Full access to the standard tool set, plus shell commands and Python in the code sandbox, and may give both to workers without asking you; you get a notice each time. If you are an administrator, also model choice: TeamLead may switch agents' models without asking you (see below). It may also look up your saved credentials without asking (search Sealbox and read a credential's name and username, never the secret); using or saving a credential still asks you, and TeamLead cannot pass this lookup on to workers. |
| `careful` (Careful team) | Read access to the standard tool set, and may give that to workers; changes need your approval. |
| `manual` (Manual) | Nothing; every access is asked for. |

Choose with `/access profile trusted`, `careful` or `manual` in chat (without a
name it shows your current choice), with **TeamLead's standing access for your
work** at the top of the Access page's **By Agent Type** tab, or with
`assistant_cli.py access starter-profile <name> --by <user id>`. The choice
applies only to your own work. Switching removes the grants your earlier
choice made, and with them anything handed down from them; grants given by
hand stay. A person who has not chosen has no profile, so every access for
their work is asked for.

The setting `authorization.starter_profile` is only the first user's default
(the instance's user). The gateway applies it when it starts until that person
chooses a profile themselves with `/access profile`, on the Access page or with
`assistant_cli.py access starter-profile`; from then on their own choice stays,
and changing the setting no longer affects it.

Shell commands and Python in the sandbox (`code:shell`, `code:python`) form
their own tool set, **Sandboxed code**, which only the Trusted profile gives.
Sandboxed code reaches only your own folders and the shared `team/` and
`agents/` folders, and the network only with internet access. Running outside
the sandbox (`code:unconfined`) and platform administration are in no profile;
people give them with a grant. Model choice is in the Trusted profile only for
administrators. The profile's credential lookup (`cli:sealbox` and
`procedure:credentials` at read) is given only to people who may give it
themselves, which today means administrators; a member's Trusted profile leaves
it out. After an update adds grants to a profile, choose the profile again with
`/access profile trusted` to receive them.

**When an agent lacks access**, the refused call names the tool, the level,
and the next step:

1. A worker working on your request gets what TeamLead may pass on for you at
   once, on its first call, for that request only. It does not ask, and you
   get no notice: TeamLead's access is what you already agreed to. Otherwise
   the agent calls `request_access` with a reason.
2. Otherwise the request waits in the **Inbox**, and the agent's task pauses.
   If the person the task works for holds the access, only that person
   decides. If they do not hold it, or the task works for nobody, an
   administrator decides, and approving gives the person the access as well.
3. The request is announced once in the task's conversation when the person
   there can decide it; otherwise in the chat that receives proactive
   messages, when its owner can decide it. Decide in the Inbox
   ([The review card](#the-review-card)) or with the buttons in Telegram and
   Discord ([Decide requests in chat](#decide-requests-in-chat)). The task
   continues with the decision.

Workers ask the same way. A worker's request goes to the person directly;
TeamLead does not pass the question along in its own conversation. When you
keep what a worker asked for, TeamLead keeps it for you and gives it to its
workers' later tasks; workers never keep access themselves. Scheduled work asks
the same way too; the run shows "waiting for access" until someone decides, and
access you keep then belongs to that scheduled job only. A request whose task
ends first is withdrawn from the Inbox.

When nobody can hand the access on, for example because the service is not in
the tool catalog yet, TeamLead can instead propose the whole change with
`propose_access_change` ([Access proposals](#access-proposals)).

### The review card

Each Inbox card lists every item you would approve, described by Universal
Agents itself; the agent's reason is shown separately, labeled as the agent's
words. On an access request:

- **Keep for later tasks** appears when the agent asked to keep the access,
  and starts ticked, so approving as shown gives what was asked. Off gives the access for this
  task only: for your whole request, including the workers helping with it,
  until the request ends. On keeps it: for TeamLead's own request, TeamLead
  keeps it for the person named on the card; for a worker's request, TeamLead
  keeps it and gives it to its workers' later tasks (the worker has it now);
  for a worker's request in a scheduled job's run, that job keeps it. **How long** then offers
  **Until I remove it**, **For 1 day**, **For 7 days** or **For 30 days**. A
  request that belongs to no task has no checkbox and lasts as asked.
- On TeamLead's own requests, **TeamLead may hand this on to workers without
  asking you** starts ticked, because workers get access only from TeamLead.
  Untick it and workers must ask you for it each time.
- **What each choice does** lists, under the buttons, what **Approve**,
  **Approve reading only** and **Deny** would do right now, in plain words, and
  changes as you tick the boxes. Pointing at a button marks its line. The card
  also says what approving does to access the agent already has on the tool,
  for example "TeamArchitect now has read access to Schedules until someone
  removes it. Approving replaces it."
- **Approve**, **Approve reading only** (shown when more than read was asked)
  and **Deny** decide, with an optional note the agent sees.

A proposal, a file delivery or a staged change lists its rows the same way,
with **Approve** and **Deny**; each proposal row that gives access has its own
**Keep for later tasks** checkbox. Chat buttons have no checkboxes, so they
offer the same choices as separate buttons (see
[Decide requests in chat](#decide-requests-in-chat)).

**Handing on access.** Workers get access from TeamLead for each task, as
described above; there is no tool for an agent to give access. TeamLead asks
for access it will pass on with `request_access` and `hand_on=true`.
`restrict_access` blocks a worker from one tool, operation or folder, and
`revoke_access` takes back what TeamLead gave. Removing access also ends
everything handed down from it.

**Notices.** The Inbox lists notices that need no decision: lasting access
TeamLead handed on without asking, such as a scheduled job's access (with a
**Revoke** button; access added to one task is not announced), changes to team skills
(sent to administrators), updates that changed an agent one of your schedules
runs, files and folders someone shared with you, files that arrived in your
`received/` folder, and credentials to choose for tools you approved in a
proposal. Notices never interrupt a chat. The Settings cog shows how many
requests you can decide plus unread notices; the Notices list opens on unread
ones and can mark several, or all, read at once.

**Changes to the platform.** TeamLead never changes runtime settings (including
its own profile and the startup roster), restarts services, creates or removes
specialists, starts, stops or removes managed execution services, changes a
schedule the runtime manages (such as memory review or the heartbeat), or
types into an SSH session on its own. It stages an
exact plan and calls `apply_change`; you see exactly what will change (old and
new values, the processes a restart affects, the specialist or the schedule)
in the Inbox and choose **Approve** or **Deny**. In chat the change is
announced in one line that Universal Agents writes from the plan itself, such
as "Switch TeamArchitect to deepseek/deepseek-v4.1-flash (images on). Takes
effect now.": only values that change are named, and it says when the change
takes effect (now, or after a restart). Approving
applies that plan once; if the file or the running work changed meanwhile,
TeamLead must stage it again. Administrators approve these changes; a setting
that belongs to one person, such as their own delivery target, can also be
approved by that person. Team skills are the team's own: an administrator can
let TeamLead install and remove them itself (full access to `fn:team_skills`),
and then every change sends administrators a notice; without that, you approve
each change the same way. With authorization off, TeamLead asks for your
agreement in the chat instead.

**Switching models (model choice).** Switching an agent's model is the one
settings change TeamLead may make without asking each time. A plan that
changes only these settings, for TeamLead itself or for named agent types,
counts as model choice: the provider (only one this instance is already set up
for, such as the team's configured provider, a failover provider or one an
agent type already uses; TeamLead refuses any other), the model, the reasoning
effort and reasoning summary, the context window, and whether the model takes
images, audio and files. Because model settings apply to the whole team, only administrators
hold model choice (`fn:model_choice`); the Trusted profile gives it to
TeamLead for an administrator's work. TeamLead first tries the new model once
and asks you to confirm in the chat; when you confirm, the change applies at
once without an Inbox review. TeamLead tells you in the chat, for example "Switched TeamArchitect to
deepseek/deepseek-v4.1-flash (images on, files off). It takes effect now."
(for another agent type, its running agents switch at their next turn, with
no restart), and your Inbox gets a
notice with the previous values and how to switch back. Everything else still
waits for an administrator in the Inbox: the same change asked for by a
member, a plan that also changes another setting, team-wide model defaults,
and the request API mode (`litellm_api_mode`), which switches the endpoint the
model is called through. Endpoints and keys are never settings agents can
change. A restart the change needs is still a separate approval. To be asked
each time again, block TeamLead from **Model choice** on the Access page.

**Schedules.** Creating and changing your own schedules needs access to the
schedule tools (`fn:schedules`), and an agent only ever sees the schedules of the
person its task works for. Each scheduled job holds its own access, which every
agent in its runs uses and nothing else does, so what one job needs never
widens another job of the same worker type. Deleting the job ends its access;
see [Give a scheduled worker its access](scheduled-workers.md#give-a-scheduled-worker-its-access).

**Agents' own view.** `list_my_access` shows an agent what it holds for the
person it works for now, where it came from, such as "alice → TeamArchitect →
WebResearchAgent", and what other people shared with that person. `find_tools`
finds tools it does not hold yet.

With authorization off (`authorization.enabled: false`, see
[Configuration](configuration.md#authorization)), every tool is available
without asking, the access tools are not given to agents, and the sandbox, the
broker, and the protected folders still apply.

## Access proposals

Some access cannot be handed on or asked for one tool at a time: a service that
is not in the tool catalog yet (such as a Portainer server's web API), or
access that nobody can hand on to TeamLead. TeamLead then proposes the whole
change at once with `propose_access_change`, and an administrator reviews it
once. A proposal can contain:

- new [web API tools](#web-api-tools) (`http:<name>`): their exact hosts, and
  optional named routes with the level each one needs;
- access for TeamLead to tools, tool sets or folders (a level on a whole tool,
  never one operation), optionally with the right to hand it on to workers'
  tasks; a worker type may get access only for this task, since workers keep
  nothing between tasks;
- restrictions (blocks) on agent types;
- [data folders](#data-folders) to register;
- [account links](#account-links) to set up for the tools;
- grants to remove.

Before anyone sees it, Universal Agents checks every item against the tool
catalog as it would be with the new tools, and writes each row of the review
card in its own words; the agent's description is never used for the effect.
Items that widen access carry a warning: **New host**, **Private address** (a
host on a local or private network), **Full access**, **Can hand on**, **Runs
code** or **New folder**. The access in a proposal serves the work of the
person the proposal's task works for. When that person lacks the access, the
proposal gives it to them too, on its own row. A person's own folder is never
part of a proposal; its owner shares it instead.

An administrator approves or denies the proposal as a whole: in the Inbox,
where each row that gives access has its own **Keep for later tasks** box
(unticked rows last for the task), or with **Approve all as proposed** in chat,
which keeps every access row as proposed. The gateway applies an approved
proposal at that moment, all together. It checks that the stored proposal is
exactly the one reviewed and that the administrator may give each item, adds
the new tools, and stores the access, blocks, data folders and removals in one
step. If any part fails, nothing is applied and the reason is shown. TeamLead's
waiting task then continues and learns what was applied.

When the proposal asked for an account link, the administrator who approved it
chooses the credential straight away: the approved card shows a link such as
**Choose the credential for Portainer**, and a notice with the same link waits
in the Inbox, so
a decision made with a chat button can be finished there. The broker's form
can also take a new credential in the same step
([Account links](#account-links)). TeamLead's task keeps waiting until those
forms are used, closed or expired, and then learns which accounts were linked,
so it does not call a tool whose account is still missing. Until someone links
an account, calls that need it are refused with `account_link_missing`;
TeamLead can then offer the form in the requester's chat, or `/link <tool_id>`
opens it again.

Proposals are decided in the gateway (the Inbox or chat), not with
`assistant_cli access decide`, because the gateway applies them. With
authorization off there are no grants: TeamLead explains the new tools and data
folders and adds them after you agree in the conversation.

## Internet access

Internet access is its own tool set, `internet` (**Internet access**): the web
research tools (web search, page contents, screenshots) and `fn:internet`,
which lets shell and Python commands open network connections. Every agent and
every person holds it as standard access, so agents can research the web, and
code can download data or install packages, without a grant.

The set also holds the web services the research tools call: `http:exa` (Exa
web search), `http:brave-search` (Brave Search) and `http:crawl4ai` (this
team's Crawl4AI server, used for screenshots). Their keys are not environment
variables: each is an [account link](#account-links) in the credential store,
and the tools send their requests through the secure tool broker, which adds
the key. The person's own key is used first, then the team's while the team
shares the service, which it does until an administrator makes it private
([Shared or private](#shared-or-private)). Onboarding saves
the Exa and Brave keys typed there when the assistant starts: as the team's
keys for a service the Secrets page left shared (administrators get an Inbox
notice), and as the administrator's own for one they made private. To add or change a key, send
`/link http:exa team` (or `http:brave-search`, `http:crawl4ai`), or
`/link http:exa` for your own. Until a key is linked, the tool tells the agent
exactly that and which command to use.

Full access to a code tool no longer opens the network, and a code tool no
longer needs full access to reach it: the network follows internet access (see
[Shell and Python code](#shell-and-python-code)).

Administrators turn internet access off with a [block](#blocking-access):

- for everyone in the team: `/access block team internet --reason "Offline
  week"`, or on the Access page **Add a block** with **Who**: **Everyone in the
  team** and **Tool**: **Internet access (all of it)**;
- for one agent type: `/access block agent-type:CodeWritingAgent internet
  --reason "Works offline"`, or **Add a block** on that agent type's card;
- for code only, keeping web research: block `fn:internet` instead of the
  whole `internet` set.

The block applies from the next action; `/access unblock <block id>` turns
internet access on again.

## Files and folders

Agents work with files in a few places, and nowhere else:

- the private folder of the person the task works for
  (`workspaces/users/<person>`), which is the default folder for relative
  paths;
- the shared `workspaces/team/` and `workspaces/agents/` folders (work that
  runs for no person uses `agents/` as its default folder);
- files and folders that other people [shared](#sharing-files-with-people) with
  the person the task works for;
- [data folders](#data-folders) that an administrator registered outside the
  instance, such as a reports share;
- the team's skill folders, read only.

Control files, databases, runtime records, credentials, the instance
configuration and other people's folders (except what they shared) are never
reachable, whatever the grants say.

**Folder names.** Grants, requests, shares and blocks name a folder by where
it sits, never by its path on the host: `team/…`, `agents/…`, `data/<name>/…`,
or `users/<folder>/…` for a person's folder, for example `data/reports/2026`
or `team/finance`. Access to a folder covers everything inside it. Reading a
file needs `read`, and creating or changing one needs `write`.

**Standard access.** Every agent and every person holds `write` on `team/` and
`agents/` without a grant. An agent may also use the folder of the person its
task works for, up to `write`. Nobody else reaches a person's folder, not even
an administrator, unless its owner shares part of it with them; agents are
never given a person's folder with a grant. Data folders need a grant; owners
and administrators hold them already. A data folder registered as read-only
refuses writes whatever the grants say.

**When an agent needs a folder**, the refused call names the folder and the
level, and the agent asks:

```text
request_access(folder='data/reports/2026', level='read', reason='Summarize the 2026 reports')
```

The request goes through the same steps as a request for a tool (see
[How agents get access](#how-agents-get-access)). A worker's task gets a
folder from TeamLead's access the same way as a tool, only a folder inside the
one TeamLead holds, and TeamLead can block a worker from one with
`restrict_access(agent_type=..., folder=..., reason=...)`. People give and
block folders with `/access` and `assistant_cli access` (examples below).

A grant, revocation or block applies to the next file an agent opens. An
allowed answer is kept for up to 10 seconds per folder, so a grant that ends
at a set time can still be used for those few seconds.

**Links.** File tools look at where a path really points. A link inside the
person's folder that points at their own files works; a link that points
anywhere else is judged by where it points, so it cannot reach a control file.
Files are then opened one folder at a time without following links, so a link
swapped in at the last moment makes the open fail rather than reach another
file. A file with more than one hard link is refused; copy it to a new file
first.

**Moving files.** Agents copy or move one file with `file_move` into any
folder they may write, for example to hand a finished report to `team/`
without shell access, or into a folder someone shared with their person for
changing. An existing file is never overwritten. To put a file into another
person's own folder, that person shares a folder, or the person the agent
works for sends it with [`/send`](#sending-files-to-a-person-or-the-team).

**Files agents send to people.** `notify_user` attaches files from the
person's folder, `team/`, `agents/`, a data folder, or something another person
shared with the person, when the agent may read it. When the gateway receives
the message, it checks each file again for the person the message is for and
copies it once into `scratch/sent/` of that person's folder, recording the
copy's fingerprint (SHA-256). Every chat and Web Chat send that copy only while
it still matches, so the file the person receives is the file that was
checked; a copy that was changed or replaced by a link afterwards is not sent,
and Web Chat shows it as no longer available. Copies are deleted with the rest
of `scratch/` ([Scratch retention](configuration.md#scratch-retention)).

With authorization off there are no folder decisions: agents use the task's
folders and every data folder, read-only where it was registered so.

### Sharing files with people

Your own folder is private, but you can share a file or a folder in it with
another team member, to read or to read and change:

```text
/share reports/q3.pdf with Bob
/share projects/website with user:usr_3f9a1b2c4d5e write --for 7d
/shares
/unshare grant_0123abcd
```

- The path is relative to your folder. Sharing a folder shares everything in
  it. Links, and files that look like credentials (such as `.env` files and
  SSH keys), are never shared.
- Name the person by display name or as `user:<id>`. The level is `read` (the
  default) or `write` (read and change). `--for 12h` or `--for 7d` ends the
  share by itself; otherwise it lasts until you stop it.
- The person gets a notice. They, and agents working for them, can then use
  the item at the shared level. Agents reach it at `users/<your folder>/<path>`,
  in file tools and in shell and Python commands, and `list_my_access` lists
  it. Blocks on an agent still apply, and agents working for anyone else gain
  nothing.
- `/shares` lists what you share and what others share with you;
  `/unshare <share id>` stops a share from the next action.

In Web Chat, **Workspace Files** (in Settings, or `/webchat?view=files`) shows
your own folder; administrators also see `team/` and `agents/`. Select a file
or folder in your own folder, and **Share…** and **Send…** (files only) appear
at the top right of the preview, next to its name. **Share…**
offers the same choices: a person, **Read** or **Read and
change**, and **Until I stop sharing**, **1 day** or **7 days**. The Access
page's **Files** tab lists **Shared by you**, with **Stop sharing**, and
**Shared with you**.

Only the owner shares their folder; administrators cannot share or open other
people's folders. Sharing needs authorization on: with it off there is one
person and nobody to share with.

### Sending files to a person or the team

`/send` puts a copy of one of your files into another folder:

```text
/send reports/q3.pdf to Bob
/send reports/q3.pdf to team/finance --move
```

- **To a person**, it asks first. The file arrives in their
  `received/<your folder>/` folder only after they, or an administrator,
  approve the delivery request in their Inbox (or with a chat button). The
  file's content is recorded when you send it; if it changes before the
  approval, the delivery is refused and you send it again.
- **To `team/`** (or a folder inside it), the copy is made at once.
- `--move` removes your file after the copy arrives; it is kept if it changed
  in the meantime.
- An existing file is never overwritten; the copy gets a number instead, such
  as `q3 (2).pdf`.

**Send…** in Workspace Files does the same, with **To a person** or **To the
team folder**, and **Copy** or **Move**. Send one file of at most 256 MiB at a
time; share a folder instead of sending many files. Deliveries are decided in
the gateway, not with `assistant_cli access decide`, because the gateway makes
the copy when the delivery is approved. Sending needs authorization on.

### Data folders

A data folder is a folder outside the instance, such as a shared reports
folder or a read-only reference library, that agents can be given access to.
Owners and administrators register and remove data folders while Universal
Agents runs; every process reads the current list when it uses a folder, so no
restart is needed:

```text
/access folders
/access folders add reports /srv/team-reports --reason "Quarterly reports"
/access folders add reference /mnt/reference --read-only
/access folders remove reports
```

```bash
assistant_cli.py access folders list --config /data/instance_config.yaml
assistant_cli.py access folders add reference /mnt/reference --read-only \
  --config /data/instance_config.yaml --by alice
assistant_cli.py access folders remove reference --config /data/instance_config.yaml --by alice
```

The Access page's **Files** tab lists data folders for everyone and gives
administrators **Add folder** (its **Read-only** toggle starts on) and
**Remove**. TeamLead can also include a data folder in an
[access proposal](#access-proposals).

- The name uses lowercase letters, digits, `-` and `_` (at most 64
  characters). Agents, grants and blocks call the folder `data/<name>`, never
  by its host path.
- The path must be an existing absolute folder, not the filesystem root. In
  Docker it is the path inside the container, so mount the host folder there
  first.
- `--read-only` refuses writes whatever the grants say.
- A folder that holds or sits inside the instance folder, the instance
  configuration or the broker's credentials, that looks like a credential
  store, or that overlaps another data folder is refused. The same check runs
  again each time a process reads the list; a folder that fails it is left out
  and logged (`event=data_folder_refused`).

Registering a folder gives nobody access to it. With authorization on, agents
need a folder grant, such as
`/access grant agent-type:WebResearchAgent data/reports read`; owners and
administrators hold access already. With authorization off, every registered
data folder is open to agents at its writability, and `/access folders` still
works. In OpenShell mode, sandboxes hold copies of only the person's folder,
`team/` and `agents/`, so code there does not see data folders.

Data folders used to be listed in `tool_policy.data_folders`; that setting is
removed (see [Removed settings](#removed-settings)).

## Shell and Python code

Shell commands (`shell`) and managed Python environments (`python_env`) are
the tools `code:shell` and `code:python`. Each command runs in the code
sandbox, which the operating system enforces
([Code Execution Sandbox](configuration.md#code-execution-sandbox)). What one
command can reach comes from the access of the agent and of the person it
works for, checked for every command:

- **Folders.** The task's folders, the data folders, what other people shared
  with the person, and every folder in the agent's grants for this person's
  work are made available read-write where both may write, read-only where
  they may only read, and not at all otherwise. One-time grants do not count
  here, because a command keeps its folders until it ends. The sandbox cannot
  leave out part of a folder, so a folder with a blocked folder inside it is
  left out entirely; the command result's sandbox note says so, and file tools
  still reach the rest.
- **Network.** Commands can open network connections when the agent and its
  person hold [internet access](#internet-access), which everyone has until
  someone blocks it. The level on `code:shell` or `code:python` has nothing to
  do with the network. Installing packages with `python_env` needs `write` on
  `code:python`, plus internet access to download them. The sandbox closes TCP
  only; UDP is not covered. Keeping code off the network needs Linux 6.7 or
  later: on an older kernel, a command whose agent or person has internet
  access blocked is refused rather than run with the network open.

Running shell commands (`shell`) and changing Python environments need `write`
on the code tool. For example, to let CodeWritingAgent run shell commands for
your work for one day (you need that access yourself; administrators have it):

```text
/access grant agent-type:CodeWritingAgent code:shell write --for 1d --reason "Build the report tool"
```

**Running a command outside the sandbox.** Some work needs the whole host,
such as a system package manager. That is a separate tool, `code:unconfined`
(**Unconfined host execution**), because such code can read and change
everything the runtime can, including control files and the access store.
Full access to shell commands never includes it, and no tool set or starter
profile contains it. The agent asks with
`request_access(tool_id='code:unconfined', level='full', reason=...)`,
preferably with `lifetime='task'`, and says exactly what the command needs. Once
a person approves it in the Inbox, the agent passes `unconfined=true` to
`shell` (or to `python_env` for `install` and `run`), and that command runs
outside the sandbox. Every such command is logged
(`event=code_unconfined_run`) with the agent, person, task and grant.

Deterministic execution jobs whose profile runs code on the controller host
(backend `local`) also run outside any sandbox. The agent type and the person
the job works for need `full` on `code:unconfined` that lasts beyond one task
("for this task only" and one-time approvals do not count). It is checked when
the job is turned on and again before every run, so revoking it stops the next
run.

In OpenShell mode (`security_layer.mode: openshell`) commands run in the
person's own OpenShell sandbox instead; the operator's OpenShell policy decides
the network and what else it can reach, and folder grants do not change it
(see [Code Execution Sandbox](configuration.md#code-execution-sandbox)).

With authorization off there are no requests: commands reach the task's
folders and the data folders, the network is open, and only the operator can
run agent code outside the sandbox, for every agent, with
`security_layer.code_execution.sandbox: off`.

## Tool access and account links

The secure tool broker runs credentialed tools for agents: `gh`, `gog`, X and
Hugging Face actions, MCP tool calls, and web requests. Before it runs one, it
checks the following, whether authorization is on or off:

1. **Which process is calling.** `SECURE_TOOL_BROKER_TOKEN` in `team.env` is
   the broker's secret. The supervisor gives the broker that secret and every
   other process its own token made from it: the gateway, the manager
   (TeamLead and the workers that run inside it), and each separately started
   agent. A separately started agent can act only as its own agent type, and
   only the gateway can open sign-in, credential, and account link forms. The
   broker never accepts the secret itself. A gateway or agent started by hand
   with the `team.env` value makes its own token from it at start.
2. **Who the task works for.** When the gateway accepts a request, it signs a
   record of the person and their current team roles (a *signed task
   context*). The manager's scheduler signs one for each scheduled run's
   owner. Delegated tasks carry the record unchanged, so a later step cannot
   change whom the work is for. The broker refuses a request whose record is
   missing, forged, or names a different person. The signing keys
   (`ACCESS_GATEWAY_SIGNING_KEY`, `ACCESS_SCHEDULER_SIGNING_KEY`,
   `ACCESS_CONTEXT_VERIFY_KEYS`) are generated into `team.env` with the other
   internal credentials; do not edit them.
3. **What the operation is and its level**, from the tool catalog.
4. **For web requests, the web API tool that covers the host.** Requests to
   any other host are refused (`access_unlisted_host`). Public pages remain
   available through the web research tools.

With authorization enabled, the broker then checks that the agent holds the
needed level for the work of the person it works for, and that the person
holds it too. An agent never gets more access than its person. Team owners and
administrators hold full access to team tools, members hold the standard tool
set and sandboxed code, and anything else needs a grant. For a personal login, such as a person's
own Google or GitHub sign-in, the person counts as its owner, so only the
agent needs a grant.

Last, if the tool uses a stored credential, the broker picks it through an
[account link](#account-links).

### Giving access with `assistant_cli access`

People can also give access directly with the `access` command, or with
[`/access` in chat](#access-commands-in-chat). The command acts for the person
named by `--by` (default: the instance's default user) with that person's
current team roles. It refuses to give more than that person's own authority:
anyone can give agents, for their own work, access they hold themselves
(including their own logins), and owners and administrators can give access
to every team tool. Access given to agents serves the giver's own work;
administrators add `--everyone` to give it for everyone's work.

```bash
# List grants and blocks; --all includes revoked and refused ones. A name
# (agent type, agent, person, tool or tool set) shows only what concerns it.
assistant_cli.py access list --config /data/instance_config.yaml
assistant_cli.py access list WebResearchAgent --config /data/instance_config.yaml

# Let WebResearchAgent read GitHub for alice's work for seven days.
assistant_cli.py access grant --config /data/instance_config.yaml \
  --by alice --to agent-type:WebResearchAgent --tool cli:github --level read --for 7d \
  --reason "Research open issues"

# An administrator lets WebResearchAgent read GitHub for everyone's work.
assistant_cli.py access grant --config /data/instance_config.yaml \
  --by alice --to agent-type:WebResearchAgent --tool cli:github --level read --everyone

# Let every team member write with the GitHub tools, until revoked.
assistant_cli.py access grant --config /data/instance_config.yaml \
  --by alice --to role:member --tool-set github --level write

# Let WebResearchAgent read one year of the reports data folder.
assistant_cli.py access grant --config /data/instance_config.yaml \
  --to agent-type:WebResearchAgent --folder data/reports/2026 --level read

# Revoke a grant and everything handed down from it.
assistant_cli.py access revoke grant_0123abcd --config /data/instance_config.yaml

# Choose TeamLead's standing access for bob's own work.
assistant_cli.py access starter-profile careful --config /data/instance_config.yaml --by bob

# List agents' requests waiting for a decision, and decide one without the web chat.
assistant_cli.py access requests --config /data/instance_config.yaml
assistant_cli.py access decide access_req_0123abcd approve_read \
  --config /data/instance_config.yaml --by bob --note "Read is enough for this"
```

`decide` takes `approve` (the access lasts as asked, normally until removed),
`approve_read`, `approve_task` (for the waiting task only), or `deny`, and
follows the same rule as the Inbox: a request for work for a person who holds
the access is decided only by that person. Proposals and file deliveries are
decided in the gateway, because the gateway applies them. While the gateway
runs, the waiting agent learns the decision within a few seconds. Data folders
have their own subcommand, `access folders` ([Data folders](#data-folders)).

| Option | Meaning |
| --- | --- |
| `--by` | The person acting (default: the instance's default user) |
| `--to` | Who receives access: `agent-type:<Type>`, `agent:<id>`, `role:<name>` (`member`, `admin`, `owner`, or the agent roles `team_lead` and `worker`), `user:<id>`, or `team` |
| `--tool` | A tool id such as `cli:github`; access is a level on the whole tool, never one operation |
| `--tool-set` | A tool set such as `github` or `google_workspace`, instead of `--tool` |
| `--folder` | A folder below `team/`, `agents/` or `data/<name>/`, such as `data/reports/2026`, instead of `--tool` ([Files and folders](#files-and-folders)) |
| `--level` | `read`, `write`, or `full` |
| `--for` | Expiry such as `30m`, `12h`, or `7d`; without it the grant lasts until revoked |
| `--everyone` | Administrators only: agents may use it for everyone's work, not only the giver's |
| `--delegate-depth` | How many times the access may be handed on, `0` to `2` (default `0`) |
| `--delegate-mode` | `automatic` hand-downs take effect at once; `ask_owner` hand-downs wait for approval |
| `--reason` | Why, for the record |

With authorization off, the command records grants but says they are not
enforced until authorization is turned on.

### Access commands in chat

`/access` does the same from any chat, including Web Chat, as the signed-in
person with their current role:

```text
/access list
/access list WebResearchAgent
/access grant agent-type:TeamArchitect cli:github read --delegate-depth 1 --for 7d --reason "Research open issues"
/access grant agent-type:TeamArchitect data/reports/2026 read --delegate-depth 1
/access grant agent-type:WebResearchAgent cli:github read --everyone
/access revoke grant_0123abcd
/access block agent-type:BackgroundOperationsAgent cli:google --reason "Sending too much mail"
/access block team internet --reason "Offline week"
/access unblock grant_4567efgh
/access requests
/access profile careful
/access folders
```

`/access grant` takes the same `--for`, `--delegate-depth`, `--delegate-mode`,
`--everyone` and `--reason` options as the command line; what you give agents
serves your own work unless an administrator adds `--everyone`. For your own
work, give access to TeamArchitect with `--delegate-depth 1`: its workers get it
for each task. Only a team default (`--everyone`, administrators) may go to a
worker type directly. Name the target as a tool (`cli:github`), a tool set
(`github`), or a folder: a name with `/` (`data/reports/2026`), or
`folder:team` for a whole shared folder. `/access revoke` removes all of that
agent's access to the tool, and what was handed on from it. `/access requests` lists agents'
requests you can decide, with a link to the Inbox where you decide them.
`/access profile` shows or sets [TeamLead's standing access for your
work](#how-agents-get-access), and `/access folders` lists, adds and removes
[data folders](#data-folders). Owners and administrators see everyone's grants
in `/access list`; members see agents' access and their own, not other
people's. Each answer says what changed; a refusal says why, and nothing
changes. `/access help` lists every form; sharing files has its own commands
(`/share`, `/unshare`, `/shares`, `/send`, see
[Sharing files with people](#sharing-files-with-people)).

With authorization off, `/access` answers that access control is off (trusted
single-person mode) and that nothing needs to be given or blocked;
`/access folders` still works.

### Blocking access

A block stops an agent type, one agent, a person, everyone with a role, or the
whole team from using a tool set, a tool, one of its operations, or a folder,
whatever grants exist. It applies from the next action, and blocks always win
over grants, including standard access such as [internet
access](#internet-access). An administrator's block is immediate and recorded
in the access log. Use blocks to stop access now.

```bash
# Stop BackgroundOperationsAgent from using Google Workspace.
assistant_cli.py access block --config /data/instance_config.yaml \
  --agent-type BackgroundOperationsAgent --tool cli:google --reason "Sending too much mail"

# Stop one operation for a day; the block ends by itself after 24 hours.
assistant_cli.py access block --config /data/instance_config.yaml \
  --agent-type WebResearchAgent --operation fn:web_research#exa_search --hours 24 \
  --reason "Use Brave search this week"

# Keep ResearchComparisonAgent out of one shared folder.
assistant_cli.py access block --config /data/instance_config.yaml \
  --agent-type ResearchComparisonAgent --folder team/finance --reason "Payroll files"

# Turn internet access off for everyone in the team (--tool also takes a tool set).
assistant_cli.py access block --config /data/instance_config.yaml \
  --team --tool internet --reason "Offline week"

# Remove a block; the grants it stopped apply again.
assistant_cli.py access unblock grant_4567efgh --config /data/instance_config.yaml
```

| Option | Meaning |
| --- | --- |
| `--agent-type`, `--agent`, `--person`, `--role`, `--team` | Who is blocked: every agent of a type, one agent by id, a person and every agent working for them, everyone holding a role (such as `worker` or `member`), or the whole team (exactly one) |
| `--tool`, `--operation`, `--folder` | What is blocked: a tool id or tool set id (such as `internet`), one operation as `tool#operation`, or a folder below `team/`, `agents/` or `data/<name>/` (exactly one) |
| `--reason` | Why; required, and shown wherever the block is listed |
| `--hours` | End the block after this many hours; without it the block lasts until removed |
| `--replace <block id>` | Replace a block on a removed operation with a block on a current operation of the same tool, for the same subject. Keeps the old expiry; cannot be combined with `--hours` |

In chat, `/access block <who> <target> --reason "why" [--hours 24]` names who
as `agent-type:<Type>`, `agent:<id>`, `user:<id>`, `role:<name>` or `team`.

Who may block and unblock:

- You can block access you could give: owners and administrators for every
  team tool, and anyone for a tool they hold access to that they may hand on.
- Only owners and administrators block a person, a role or the whole team.
  Blocking a person also stops every agent working for them, because an agent
  never exceeds its person. You cannot block yourself.
- The person who added a block, or any owner or administrator, removes it.
  Holding access you could give is not enough to remove someone else's block.
- `revoke` does not remove blocks; use `unblock`.
- An agent that is blocked is told so when it tries the tool, and asking for
  access is refused, because no grant can override a block.
- A folder block covers everything inside the folder, including the standard
  `team/` and `agents/` access. Shell and Python commands then do not get the
  folder that contains the blocked one at all (see
  [Shell and Python code](#shell-and-python-code)).

TeamLead's `restrict_access` creates the same kind of block on a worker.

When an update renames or removes an operation that a block names, the block
stops the whole tool instead, so it never quietly allows that operation again.
Administrators get one Inbox notice for each such block, naming whom it stops
and the tool's operations now. To block one operation again, replace the old
block in one command, using the current operation you want to stop:

```text
/access block team fn:web_research#exa_search --reason "Keep Exa blocked" --replace <block id>
```

The person who added the block, or an owner or administrator, may replace it
even while that block stops their own access. The replacement keeps the same
subject, tool and expiry. Both changes commit together, so a failure leaves
the old block in force. The CLI accepts the same `--replace` flag with
`--operation`. To remove the restriction entirely, use `/access unblock <block id>`;
to keep the whole tool blocked, do nothing. A [team transfer](team-transfer.md) converts the
operations its release renamed, so imported blocks keep their exact scope.

### The Access page

In Web Chat, open **Settings → Access** (next to Inbox) to see who can use
what. A link such as `/webchat?view=access#agent=WebResearchAgent` opens it at
one agent type. Opening the page never changes access. It has four tabs, each
showing 20 items per page:

- **By Agent Type** starts with **TeamLead's standing access for your work**:
  the three [profiles](#how-agents-get-access), the one in use marked **In
  use**, and **Save**, which changes only your own work. Below it, one card per
  agent type, and one per scheduled job that holds access (**Scheduled job
  <name>**), shows its access in groups, with one row per tool:
    - **Standard for its role**: access that comes with the role, such as the
      tools every agent needs, internet access, and write access to `team/`
      and `agents/`. It cannot be removed, but a block can stop it.
    - **For your work**: access that serves your tasks, one row per tool, with
      its level (and any step-up for one task, such as "read and change, and
      full while one task runs"), **How long**, **Handing on**, **Came from**
      (such as "You → TeamArchitect → WebResearchAgent") and **Why**, and a
      **Remove** button that removes all of it. A worker's rows are access its
      running tasks got from TeamLead. When an administrator also gave the
      same tool for everyone's work, a **Note** says that removing your row
      leaves that.
    - Administrators also see one group per other person, such as **For
      Bob's work**, and **For everyone's work**, each with **Remove**. Members
      see their own work and everyone's work, never what serves another
      person.
    - **Blocks**, with who added each one, when, why and until when, and a
      **Remove block** button.

    Rows still waiting for a decision show **Waiting for approval**. The team
    lead's type is always listed.

- **By Person**: what each person has given, and to whom, including the blocks
  they added. Members see only their own; owners and administrators see
  everyone.
- **By Tool**: every tool with its tool sets, the access each of its operations
  needs (**Show operations**), and whose accounts it uses (the team's, yours,
  or, for administrators, other people's). Account details such as credential
  names are never shown.
- **Files**: **Shared by you** (with **Stop sharing**), **Shared with you**,
  and the team's **Data folders**. Everyone sees the data folders;
  administrators can **Add folder** and **Remove** them
  ([Data folders](#data-folders)).

The buttons follow the same rules as `/access`, as the signed-in person.
**Remove** also removes everything handed on from that access. **Add a block**
opens a form on the card: **Who** (this agent type, or for administrators
**Everyone in the team**), **Tool** (for administrators it starts with whole
tool sets such as **Internet access (all of it)**), **Operation (optional)**,
**How long** and **Why**; block a folder with `/access block` or the command
line. **Remove**, **Remove block** and **Stop sharing** ask you to confirm
first. The page shows only the buttons you may use, and if something changed
meanwhile the refusal explains why and nothing changes. Changes apply from the
next action. With authorization off, the page says that access control is off
and still lists the data folders, which every agent then uses at the access
shown; administrators add and remove them there, with `/access folders`, or
with `assistant_cli access folders`.

### Web API tools

Every web request an agent sends through the broker (`secure_requests` or
`credential.api_request`) belongs to the web API tool whose hosts include the
request's host. The built-in `http:github` covers `api.github.com` and
`github.com`. For another service, TeamLead proposes a web API tool in an
[access proposal](#access-proposals); when an administrator approves it, the
gateway writes the tool's file and the tool can be used at once, by the agents
the proposal gave access to. An operator can also describe a service by hand
in `<instance_root>/control/access/tools/<name>.yaml`. Only web API tools
(`http:*`) can be defined there, agents cannot create or change these files,
and a new tool gives nobody access until access is given.

```yaml
# <instance_root>/control/access/tools/portainer.yaml
tools:
  - tool_id: http:portainer
    display_name: Portainer
    hosts: [portainer.example.com]
    routes:
      - {name: stop_stack, method: POST, path: "/api/stacks/{id}/stop", level: full}
    account: linked_credential
    credential_injections:
      - {placement: header, name: X-API-Key, field: api_key}
    setup:
      external_steps:
        - step_id: create_key
          title: Create a Portainer access token
          instructions: In Portainer, open My account, then Access tokens, and add a token.
          url: https://portainer.example.com/#!/account
          verify: Portainer shows the new token once; paste it into the account link form next.
```

- `hosts` are exact host names without wildcards or ports. Each host belongs
  to one tool.
- `routes` are optional. Each gives one method and path its own level;
  `{id}` matches one path segment. Other requests use the method's level:
  GET, HEAD, and OPTIONS need read, POST, PUT, and PATCH need write, and
  DELETE or any other method needs full. A path with `..`, empty segments, or
  encoded slashes always needs full.
- `account: linked_credential` means the tool uses a stored credential chosen
  by an account link. Leave it out for a public API.
- `setup` is optional: what `set_up_tools` needs besides a sign-in or
  account link. `external_steps` are up to six steps the person does outside
  Universal Agents before the account can be linked, each with a `step_id`,
  a `title`, plain `instructions`, an optional `url` (https only, without
  sign-in details or tokens in the address), an optional `verify` sentence
  ("how to tell it worked") and `who` (`person`, or `administrator` for a
  step only an administrator can do). `prerequisites` are up to four tools to
  set up first. The text is checked: no secrets, and bounded lengths.
  TeamLead may include it in an access proposal; the review card shows each
  step in the exact words people will see (flagged "Steps for people").
- `credential_injections` say where each credential field goes. `placement`
  is `header` or `query` (or `env`, used only for
  [OpenShell credential leases](configuration.md#secure-broker)), `name` is
  the header, query parameter, or variable, and `field` is the stored
  credential's field. An optional `template` places the value, such as
  `"Bearer {value}"`.

The broker sends the credential only to the tool's hosts, and a request cannot
set a header or query value that the tool's account sets. Requests carry no
credentials and no `${...}` placeholders. Redirects are returned to the agent,
not followed, and the next request is checked again. The broker reads these
files again when they change. If a changed file is invalid, it keeps the
previous definitions and logs `tool_catalog_reload_failed`; an invalid file at
startup stops the broker.

### Command-line tools (CLIs)

When a service has a command-line tool, TeamLead prefers it to a web API tool:
the agents see one compact tool, each command has its own access level, and
the broker keeps the token away from them. TeamLead chooses, and says why, in
this order: a tool already in the catalog, a CLI, a web API tool for a simple
REST service with a key or token, and an MCP server when the service offers
one. A CLI is added with an [access proposal](#access-proposals) (a `clis`
item), and nothing about it is code:

- **The program** is one file at `team/tools/<name>/<file>` in the team's shared
  folder. InternalImprovementAgent builds it from source in the sandbox or
  downloads the vendor's release there, and reports its SHA-256. The proposal
  pins that digest; it can also name the https download and version, which the
  gateway fetches into that path when the proposal is approved (a download
  that does not match the digest applies nothing). Updating the CLI is a new
  proposal with the new digest.
- **Every run** copies the program into a private folder without following
  links, checks the copy's SHA-256 against the approved one, and runs only
  that copy. A program changed after approval is refused ("the program at
  team/tools/tea/tea is not the approved one ... Propose the update with its new
  digest"), and the broker logs `event=installed_cli_program_refused` with both
  digests.
- **The sandbox**: the copy runs inside the same Linux Landlock sandbox as
  agents' shell and Python commands, always, whatever the agents' sandbox
  setting. It reads only system folders and its private run folder and writes
  only that folder, so the broker's files and every other credential stay out
  of reach. The network is open, because the program must reach its service;
  Landlock can limit connections by port but not by host, so the hosts a CLI
  talks to are shown on the review card but not enforced. On a host without
  Landlock, installed CLIs do not run: the call is refused with a plain
  message and `event=installed_cli_sandbox_unavailable`.
- **The account**: `link_owners` says whose [account link](#account-links) it
  uses: each person's own (`[person]`, the default), the person's own or else
  the team's while the team shares the CLI (`[person, team]`; see
  [Shared or private](#shared-or-private)), or only the team's (`[team]`).
  The credential reaches the program as an environment variable or as a file
  in its private home folder
  (`credential_injections` with `placement: env` or `file`), never on its
  command line, and its values are removed from the output.
- **Commands** map to levels (`repo.list: read`, `issue.create: write`,
  `repo.delete: full`); any command not listed needs full, and blocked
  commands and options are refused whatever access is held. Deleting, paying
  and trading belong at full, so they are asked for each time.
- Agents call it through `secure_cli` with the CLI's name as the provider:
  `provider="tea", action="exec", params={"argv": ["repo", "list"]}`.

For example, Gitea's `tea` with each person's own token:

```json
{
  "tool_id": "cli:tea",
  "display_name": "Gitea CLI (tea)",
  "installed_cli": {
    "binary_path": "team/tools/tea/tea",
    "sha256": "<the program's SHA-256>",
    "version": "0.9.2",
    "source_url": "https://dl.gitea.com/tea/0.9.2/tea-0.9.2-linux-amd64",
    "commands": {"repo.list": "read", "issue.list": "read", "issue.create": "write", "repo.delete": "full"},
    "blocked_commands": {"login.add": "stores credentials outside the broker"},
    "blocked_flags": ["--token"],
    "allowed_hosts": ["gitea.example.com"]
  },
  "credential_injections": [{"placement": "env", "name": "TEA_TOKEN", "field": "token"}],
  "link_owners": ["person"],
  "setup": {"external_steps": [{
    "step_id": "create_token",
    "title": "Create a Gitea token",
    "instructions": "In Gitea, open Settings, then Applications, and generate a token that can read repositories.",
    "url": "https://gitea.example.com/user/settings/applications"
  }]}
}
```

The review card then reads, flagged "New program", "Download", "Full access"
and "Steps for people":

- *New command-line tool Gitea CLI (tea) (cli:tea): runs the program
  team/tools/tea/tea with SHA-256 ..., version 0.9.2 downloaded from
  dl.gitea.com, checked before every run and run inside the sandbox (only its
  own private folder; network open). It uses each person's own account; the
  broker gives it the credential in the environment variable TEA_TOKEN, never
  on its command line, and removes it from its output. It talks to
  gitea.example.com (not enforced: the sandbox cannot limit hosts).*
- *Gitea CLI (tea) commands. Reading (read): issue list, repo list. Changes
  (read-and-change): issue create. Full (needs full access; asked for each
  time otherwise): repo delete. Any other command needs full. Never run: login
  add. Refused options: --token.*
- *Setup step 1 for Gitea CLI (tea), done by the person at
  https://gitea.example.com/user/settings/applications: "Create a Gitea token".
  ...*

After approval, `set_up_tools` shows each person the token step and their
account link form the first time a request needs Gitea.

### Account links

An account link says which stored credential one tool uses, either for the
whole team or for one person, and where each credential field goes. The link
form either chooses a credential that is already stored or saves a new one you
type into it and links it in the same step. A link gives nobody access to the
tool; grants still decide that.

The broker uses the person's own link first, then the team's while the team
shares the service (see [Shared or private](#shared-or-private)), for MCP
servers too. (Only listing an MCP server's tools, which happens once, without
a person, uses the link of the person who proposed the server, then the
team's.) Tools that need a link include `cli:huggingface`,
`http:github` (each person's own account only), `http:exa`,
`http:brave-search`, `http:crawl4ai`, web API
tools you define with `account: linked_credential`,
and MCP connections that declare `credential_injections`
(`mcp:<connection_name>`).

GitHub, Google and X use each person's own sign-in instead, and `/link` is the
command for those too: `/link cli:github`, `/link cli:google` and `/link cli:x`
start your sign-in (see [Authentication](auth/index.md)). X alone also takes a
team account link, `/link cli:x team`, which the broker uses for people who have
not signed in to X, but only after an administrator shares X. A tool that uses
no account replies that there is nothing to link.

Links are made in the broker's form, never in chat. There are three ways to
open it:

- Send `/link <tool_id>` in chat to link your own account, or
  `/link <tool_id> team` to link the team's account. Only team owners and
  administrators can link the team's account, and only for a service the team
  shares. When you finish the form, the
  same chat gets one message saying whether it linked, was closed, expired or
  could not be confirmed (see
  [How you know `/link` worked](features.md#how-you-know-link-worked)).
- Before using such a tool, or when an agent reports that no account is
  linked (`account_link_missing`), TeamLead checks the setup and offers the
  form in the chat of the person the task works for (see
  [Setup pages in chat](#setup-pages-in-chat)).
- When an administrator approves an [access proposal](#access-proposals) that
  asks for an account link, the form opens for that administrator: a link such
  as **Choose the credential for Portainer** on the approved Inbox card, and
  in an Inbox notice.

The form shows whose account it is, which agents use it (**Used by**: the agent
types holding access to the tool for your work, or for anyone's work for a team
link), where the credential goes (the tool's hosts or MCP connection), and how
it is sent (for example the `Authorization` header as `Bearer ‹token›`). Then
you do one of two things and confirm:

- **Use a stored credential** lists the stored credentials that have every
  field the tool needs: yours for your own link, the team's for a team link.
- **Add a new credential** has one masked field for each value the tool needs.
  The broker saves the values in your own credentials, or in the team's for a
  team link, and links them in the same step. The new credential gets a name
  made from the tool and owner, such as `http-portainer-team`; if that name is
  taken it gets a number (`http-portainer-team-2`), so nothing already stored
  is replaced. The values go only to the broker's credential store, never into
  chat, logs or the result page.

A new link replaces the earlier link for the same tool and owner. A
link keeps the placement you confirmed, so after you change a tool's
`credential_injections`, link the account again.

Operators can list and remove links:

```bash
assistant_cli.py access links --config /data/instance_config.yaml
assistant_cli.py access unlink account_link_0123abcd --config /data/instance_config.yaml
```

A person can remove their own links; a team administrator can remove any link
in the team.

For example, X account checks and text posts are `cli:x` actions: the broker
posts with the person's own X sign-in, or else, when the team shares X, the
team's X account linked to `cli:x`, and each post is decided like any other
call, for the agent and the person it works for. Signing in or linking the
account does not approve a post.

#### Shared or private

For each service that takes both a person's own account and the team's, the
team chooses whose account agents use. The first administrator makes these
choices on onboarding's Secrets page (see [Onboarding](onboarding.md)); after
that, administrators change them with `/accounts`:

- **Shared**: the team's account serves the work of anyone who has not linked
  their own. A person's own account still comes first.
- **Private**: the team's account is never used for that service, not even
  for work that serves no person, such as the team's background jobs. Each
  person's work uses only their own account (`/link <tool_id>`).

Until an administrator chooses:

| Service | Sharing |
| --- | --- |
| Exa (`http:exa`), Brave Search (`http:brave-search`), Hugging Face (`cli:huggingface`), and web API tools and MCP servers added later | Shared |
| X (`cli:x`) | Private; administrators can share it |
| Crawl4AI web (`http:crawl4ai`): the team's own server, which only the team's token opens | Always shared |
| GitHub (`cli:github`), Google Workspace (`cli:google`), the GitHub API (`http:github`) and CLIs that take only each person's account | Always private |

A team account that is already linked is kept, unused, while the service is
private; sharing the service again uses it without a new link. Making a
service private stops the team's account for everyone's work, the
administrator's own included; the reply says so and names the command to link
or sign in with your own account (the key typed for the team stays the team's
and is not moved to you). During onboarding, by contrast, a key typed for a
private service is saved as your own from the start. While a
service is private, `/link <tool_id> team` is refused: "Exa web search is
private in this team: agents use only each person's own account, so the
team's account would not be used. Link your own with /link http:exa, or an
administrator can share the service with /accounts share http:exa." When a
call finds no account for a private service, the agent is told: "The team
keeps this service private, so each person links their own account with
/link http:exa." TeamLead cannot ask for the team's account for a private
service either: a proposal (`propose_access_change`) or an access workflow
step that names one is refused with the same reason before anyone reviews it.

`/accounts` shows the team's choices to any team member, in any chat app: the
model keys' sharing, then every service that uses an account (shared,
private, always private or always shared), whether the team's account is
linked, and whether you linked your own. For example:

```text
- Model keys: shared: people added from now on may use the team's model keys.
- Exa web search (http:exa): private. Team account: linked, not used while private. Your own: not linked; link it with /link http:exa.
```

In the web chat, Telegram and Discord, each service's name is bold, tool ids
and commands show as code, and the "Team account" and "Your own" labels are
underlined. Signal and WhatsApp show the same text with its formatting marks.
Code keeps its formatting marks as typed, and underscores inside names such
as `café__cache__` stay as typed too.
The web chat shows the same list in **Settings → Accounts**, as a table you
can refresh; changing it still happens in chat.

Owners and administrators change it:

```text
/accounts share cli:x        the team's X account serves people who have not signed in to X
/accounts private http:exa   each person's work uses only their own Exa key
/accounts share model        people added from now on may use the team's model keys
/accounts private model      people added from now on use their own model (/auth model me)
```

Each change is recorded in the access event log, and every administrator gets
an Inbox notice. Choosing what is already in force changes nothing. The model
keys' sharing decides only what people added from now on start with (see
[Model Keys](model-providers.md#model-keys)). A choice picks whose account
agents use; it never gives anyone access to a tool. On the Access page, the
tool's **Account** line on **By Tool** says when the team keeps a service
private, and a team account that is not used says why: "(not used: the team
keeps this service private)", or, for a link left from before on a service that
takes only each person's own account, "(not used: this service uses only each
person's own account)".

## MCP permissions

An installed MCP server is one team tool, `mcp:<connection name>`. Installing
it is an administrator's decision; using it follows access like any other
tool. One review does both, so the agents it names never come back for each of
its functions:

| Step | Who decides |
| --- | --- |
| TeamArchitect proposes a server (`mcp_connections(action="propose")`) | Nothing is installed and nothing connects yet. TeamArchitect's task waits for the review. |
| Installing it, and who may use it | An administrator, on one Inbox card: it installs the server and lets TeamArchitect use **all** of its tools for the requesting person's work and hand it on to its workers' tasks. The access row has **Keep for later tasks**. The card shows the endpoint, any local command, and whether credentials come from the operator's configuration or a linked account; any credential form opens for the approver afterwards. |
| Listing its tools | Nobody further: an agent that holds access lists them through the broker. |
| Calling one tool | The access check on every call, for the agent and the person the task is for (`mcp:<connection>`). |

Full access to the server covers every function it has: ones whose level
nobody confirmed and ones the server adds later. A server's tools appear on
an agent's next step once it holds access to the server for the person it
works for, and disappear when that access ends. If the server does not answer,
agents keep trying for a limited time and then tell administrators; `find_tools`
shows the agent why the tools are missing (see [MCP](configuration.md#mcp)).

Other ways to install a server:

- An administrator approves a pending server in **Settings → Team → MCP
  Connections** (servers from the operator's configuration, managed services,
  or TeamArchitect's proposals). With authorization on, that also lets the
  agent types the server names use all of it, for everyone's work.
- TeamLead can include a pending server in any
  [access proposal](#access-proposals) (`mcp_servers`) with its own access
  rows, for example read-only use for one worker.
- Servers listed in the operator's configuration when the MCP registry was
  first created are recorded as approved by that configuration; with
  authorization on, give agents access to them on the Access page or with
  `assistant_cli.py access grant`.

Each person authorizes agents for their own work: members hold every
installed server themselves, so when TeamArchitect works for someone else, that
person approves its use for their work (or gives it on the Access page). The
broker uses that person's own account link first, then the team's while the
team shares the server ([Shared or private](#shared-or-private)). There is no
rule that only the person who proposed a server may use it.

When a call is refused, the agent sees the reason, the tool, the operation,
the level and the next step, so it asks for exactly what is missing
(`request_access`, or an account link for `account_link_missing`).

Hosted MCP servers (declarative servers the runtime runs itself) are installed
or removed as an approved change (`hosted_mcp_servers` with `plan_install` or `plan_remove`, or the MCP
controls); installing also lets the agent types it names use all its tools.
Starting and stopping an installed server are ordinary tool calls. Publishing
a hosted server outside the instance is not available.

With authorization off, the person's agreement in the conversation installs a
server (`confirmed=true`), every signed-in team member may use the MCP
controls, and agents load the installed servers that name their type on their
next step, without a restart.

### Let TeamArchitect and WebResearchAgent use Crawl4AI

You can ask TeamArchitect:

> Add the Crawl4AI MCP server so you and WebResearchAgent can read pages with it.

1. TeamArchitect calls `mcp_connections(action="propose")` once. You get one card:
   install Crawl4AI, and let TeamArchitect use all of its tools for your work,
   with permission to hand them on to workers. Approve it (tick **Keep for
   later tasks** if it should last). If the server needs a credential, the
   form to choose it opens for you.
2. Its tools appear on TeamArchitect's next step, and it uses every one of
   them without asking again.
3. When WebResearchAgent needs Crawl4AI, it calls `request_access`;
   TeamArchitect hands the server on at once and you get a notice.

## Installed procedures

An installed procedure is a fixed operation a developer compiled into trusted
runtime code, such as invalidating one named cache with a schema-validated key.
Agents cannot supply Python, a shell command, a URL, credentials, an input
schema or a risk label. This repository currently ships no procedure handlers;
the checked-in handler registry is empty.

1. A developer implements the executor in trusted runtime code and registers
   the exact handler ID in `TRUSTED_INSTALLED_PERMISSION_EXECUTORS`.
2. The same release adds matching metadata to
   `ua_definitions/installed_permissions/handlers.yaml` (a JSON Schema object
   with `additionalProperties: false`, impact and risk). Tests require the code
   and metadata registries to match exactly.
3. An agent may propose using a registered handler; the proposal is an inert
   description.
4. An administrator adds or rejects the proposal in Web Chat under
   **Settings → Team → Installed procedures**. Adding it installs the
   definition only.
5. Using it is the catalog tool `procedure:<handler>`: each
   `invoke_installed_permission` call is checked like any other tool, for the
   agent type and the person, so an administrator grants access to it (or an
   agent asks with `request_access`).

## Decide requests in chat

When an agent's request waits for a person, the gateway announces it once in a
chat where it can be decided: the task's own conversation when the person there
can decide it, otherwise the chat that receives proactive messages such as
heartbeat updates, when its owner can. If neither can decide it, the request
waits in the Inbox without an announcement. A staged change is announced in one
line that Universal Agents writes from the plan (what changes, and when it
takes effect); the full detail stays in the Inbox.

In **Web Chat**, the announcement is a System message with the Inbox's own
review card:

- the one-line summary, with **Needs your decision** and the same buttons as
  the Inbox (**Approve**, **Approve reading only** when more than read was
  asked, **Deny**);
- warnings such as **Full access** or **Runs code**, which always stay visible;
- **Details**, which opens the full title, the rows with their **Keep for
  later tasks** boxes, what the change does, the agent's reason and an
  optional note.

The card decides through the Inbox as the signed-in person, with the same
rules. Once decided, it shows the outcome and who decided, also after a reload
or when someone decided on another device. If you may not decide the request,
the card says **Waiting for an administrator** and has no buttons.

In **Telegram** and **Discord**, the announcement has buttons. Chat has no
checkboxes, so the Inbox card's **Keep for later tasks** choice becomes
separate buttons:

- for an access request: **Allow for this task** (only when the request
  belongs to a task), **Allow and keep** (the access lasts as asked, normally
  until someone removes it), **Allow reading only** (only when more than read
  was asked), and **Deny**;
- for a proposal, a file delivery or a staged change: **Approve all as
  proposed** (every access row kept as proposed) and **Deny**;
- **Open Inbox**, a link to the review in Web Chat, when Web Chat has an HTTPS
  address. Use the Inbox to keep access for a limited time, to keep only some
  rows of a proposal, or to let TeamLead hand access on.

Pressing a button decides as the person whose gateway user is linked to that
Telegram or Discord account, with that person's current role. The same rules
apply as in the Inbox and with `/access`: a request for work for a person who
holds the access is decided only by that person, and otherwise you can decide
only what you could give yourself. Link your chat account to your gateway user
first (see
[Link the same person to another platform](newuser/index.md#onboarding-path-4-link-the-same-person-to-another-platform));
an account that is not linked is told to link it or to decide in the Inbox.

After a decision the message shows the outcome, for example "Allowed for this
task by Alice." or "Denied by Alice.", and the buttons disappear. If the request was
already decided or its task ended, the message says so. If you may not decide
it, you see the reason and the buttons stay for someone who can. The buttons
keep working after the gateway restarts.

WhatsApp, Signal and Photon/iMessage have no buttons; their announcement
includes the Inbox link. A decision in chat wakes the waiting
agent exactly like a decision in the Inbox. Opening a review never decides it,
and a reply such as "yes" in the conversation is not a decision.

### Setup pages in chat

When a request needs a tool that acts with an account and TeamLead's context
does not list it as ready, TeamLead calls `set_up_tools` at once, without
asking the person whether to start (owner's decision 2026-09-28). Trusted code
checks, for the person the task works for, access for TeamLead, the worker
and the person, the person's sign-in or a linked account (theirs, or the
team's while the team shares the service), the team's sign-in app for Google
and X, and the tool's setup data (steps outside Universal Agents and tools to set up first; see
[Web API tools](#web-api-tools)). Then it:

- asks for the access TeamLead lacks (a person decides in the Inbox) and hands
  the worker its access when TeamLead may;
- opens, in the person's chat, every page the person can do: a sign-in page
  (**Sign in to GitHub**), an [account link](#account-links) form (**Choose
  your account for Portainer**), the page that stores the team's sign-in app
  (administrators only), and steps done outside Universal Agents, which the
  person marks done on the workflow page. Pages that need another first wait
  for it;
- shows one workflow page when there are two or more things to do.

The task waits until everything is done, then continues on its own:

- **Finished**: the task goes on with the work. Finishing the same step
  yourself with `/link` or `/auth ... team_client` counts too.
- **Expired**: a fresh page opens by itself, at most three pages of one kind
  per tool in a request; the gateway refuses a fourth.
- **Closed without finishing**, or **access denied**: the setup stops at once,
  TeamLead says what cannot be done, and nothing is asked again in the request.
- **An administrator's step** (the team's sign-in app, the team's account,
  access the person lacks, a setup step marked for an administrator) for
  someone who is not an administrator: nothing is asked or opened; TeamLead
  explains which step is missing and who can do it, and ends the task.

No access request or page is opened by the model itself: `set_up_tools` is the
only way, and setting a tool up grants no access.

TeamLead does not check again each time: trusted code keeps each person's last
known setup state per tool (from every tool call, setup page, `/link` and check,
and a periodic re-check; see
[Tool setup checks](configuration.md#tool-setup-checks)), shows it to TeamLead,
and TeamLead uses a tool it lists as ready without checking. The state covers
the person's account and the person's own access, never an agent's access,
which is decided at every call.

The gateway records each waiting page, so if it restarts while a page is open,
the task is told that nobody could see how the page ended, and a fresh page
opens (it counts toward the three). A step done outside Universal Agents does
not expire and still waits after a restart. The agents' side keeps the setup
run too, so an agent restart during the wait resumes the task once the pages
end. Refreshing chat can recover a live page while the gateway is running; a
restart does not recover an already-issued page.

## Complete workflow reviews

When a goal needs two or more things set up or approved, the team lead shows it
as a map first: a **Your Access Plan** card in chat, and the full workflow page.
With only one thing missing there is no map; the team lead just asks for it
(`set_up_tools` applies this rule and publishes the steps: one per sign-in,
account link, team sign-in app, access request or step outside Universal
Agents, and a final work step). You can also open
**Inbox → Open Workflows**; the list shows 20 items per page. The map is a view
over the ordinary requests you decide in the Inbox, and a plan grants nothing.

Each step shows the requests the agent made for it and their current status:
waiting for a decision, approved, approved and applied, denied, or withdrawn. A
waiting request links to its Inbox review. A request made by a worker for its
team lead's plan shows on the team lead's step with the worker's name. Requests
that matched no single step are listed separately; so are folder requests,
because steps name tools and changes, not folders.

A step can also wait for [setup pages in chat](#setup-pages-in-chat). Each page
shows on its step with its status in plain words, and trusted code, not the
agent, changes the step from how the page ended: **Signed in as ...** (the step
is done once all its pages are finished), the page expired (the step keeps
waiting; after the third page it fails and says to use `/link <tool>` later),
or closed without finishing (the step is blocked, like a denied request).
Finishing the setup yourself with `/link` while the task waits counts the same.
A step you do outside Universal Agents (for example, creating a token on the
service's website) shows its instructions and a **Mark Done** button; only
you, the person the workflow belongs to, can mark it done, and the waiting
task continues. A setup page that matched no single step is listed with the
other requests not on a step.

**Review Ready Requests** shows the plan's waiting requests as the same review
cards the Inbox shows ([The review card](#the-review-card)): for access, each
grant's **Keep for later tasks** checkbox with **Approve**, **Approve reading
only** or **Deny**; for a proposal or a staged change, **Approve** or **Deny**.
**Approve All** and **Deny All** decide every request you can decide. A
decision here is the same as in the Inbox, and the waiting agent is told.
Denying a request closes its step: the agent cannot ask for anything that step
covers again in this plan.

The team lead can ask for several staged changes together: one request per change,
and its task pauses once until you decided all of them. It then applies each
approved change. Steps that need a later result, such as a linked account or a
draft, get their requests later on the same page. Account links use the trusted
broker form and grant no access by themselves.

The page separates **Agent reported** progress from request status. An approval
means the exact request was approved; the agent still has to do and check the
work, and it reports a step complete only after its changes are applied. A task
cannot finish while its plan has unfinished steps; a repeated attempt without
progress becomes an explicit error.

Workflows saved by earlier releases are no longer shown and cannot be continued.
Ask the team lead to publish a new plan for any unfinished work; completed results
stay useful, and uncertain external effects must be checked before retrying.

**Download Diagnostic Logs** saves the steps, missing prerequisites, each request's
outcome, explanatory codes, and task and request references. For ended tasks it
includes the end time and known system events. It leaves out credentials, form
links, chat, request reasons, private work content, and raw provider responses.
Event limits (`access_workflows.diagnostic_event_limit`) and truncation are
explicit.

## Diagnosing a denied or failed operation

Check the stages in order:

1. Does the agent see the tool at all? With authorization on, an agent sees a
   tool only while it holds access for the person it works for now
   (`find_tools` finds the others). For an MCP tool: is the server installed
   (active), and does the agent hold access to `mcp:<name>` for this person?
2. What did the refusal say? It names the tool, the operation, the level, and
   whether the agent or the person lacks access.
3. Does the agent hold that level for this person's work? Ask it to run
   `list_my_access`, or run `assistant_cli.py access list`. Access given for
   another person's work does not count.
4. Does the person the task is for hold it too? Agents never exceed their
   person.
5. Is a block in place for that agent type, its role, the person, or the whole
   team?
6. For broker tools: is an account link needed (`account_link_missing`)?
7. For files: is the path in the task's folders, a data folder, or something
   shared with the person, and is it a link to somewhere else or a file with
   more than one hard link ([Files and folders](#files-and-folders))?
8. For shell and Python commands: does the result have a sandbox note (a
   folder left out), a `Permission denied` for a folder the command did not
   get, or a failed network connection because internet access is blocked
   ([Shell and Python code](#shell-and-python-code))?

Every decision is recorded in the access store with the grants that allowed
it. An approval does not prove the external operation succeeded; check the
tool's own result.

See [Configuration](configuration.md#authorization) for the complete settings
reference and [Agent features](features.md#access-requests-and-reviews) for the
Inbox and chat commands.

## Upgrading

The sections below apply once, when you upgrade an instance that used the
earlier permission system, tool attachments or the earlier account rules. A
new instance needs none of them.

### Upgrading to shared or private accounts

Each team now chooses whether its account for a service is shared or private
([Shared or private](#shared-or-private)). This breaks compatibility in three
places; nothing is converted for you:

- X starts private, so the team's linked X account is no longer used for
  people who have not signed in to X. To keep using it, an administrator
  sends `/accounts share cli:x`; the existing link is used again without a
  new one.
- The GitHub API (`http:github`) takes only each person's own account. A team
  account linked to it is no longer used; each person links their own with
  `/link http:github`. To remove the unused team link, find its id with
  `assistant_cli.py access links --config /data/instance_config.yaml` and run
  `assistant_cli.py access unlink <link_id> --config /data/instance_config.yaml`.
- Crawl4AI (`http:crawl4ai`) takes only the team's token, because it is the
  team's own server. A person's own Crawl4AI link is no longer used; remove it
  the same way.
- A [team transfer](team-transfer.md) refuses a link this version would never
  use (a team GitHub API link, a person's Crawl4AI link) with the reason, and
  imports a team link for a service that is private after the import (such
  as X) marked for attention, with the `/accounts share` command that uses it.
- `gateway.users.new_people_use_team_model_keys` is removed. Startup refuses
  a configuration that still has it, with a message saying to delete it. The
  team's model-key sharing replaces it and is shared until someone chooses,
  so people added from now on may use the team's model keys (the old setting
  was off by default). To keep the old behavior, an administrator sends
  `/accounts private model`. People already in the team keep their own
  setting.

### Upgrading to access that follows the work

Access now follows the work ([How agents get access](#how-agents-get-access)).
This breaks compatibility; nothing is moved for you:

- Lasting access a person gave a worker type for their own work no longer
  counts and is no longer listed. Workers get access for each task from
  TeamLead. Give TeamLead the access with handing on (the review card now
  starts with **TeamLead may hand this on to workers** ticked), or let the
  worker ask once and approve it with **Keep for later tasks**, which gives it
  to TeamLead.
- Scheduled jobs that a worker runs hold their own access now. Access an
  earlier setup gave a job's worker type no longer counts, so a job's next run
  asks for what it needs. Ask TeamLead to run `authorize_scheduled_job_access`
  for each such job to give it its access again at once. Schedules TeamLead
  runs and the runtime's timers need nothing: TeamLead keeps its access in
  them and passes it on to its workers there.
- The built-in maintenance agents (MemReviewAgent, InternalImprovementAgent)
  are now treated as system agents by the tool broker, the Inbox and the
  operator commands too, not only by the agents themselves.
- Access is a level on a whole tool. Single-operation grants (such as
  `cli:github#issue.create`) are refused when given; existing ones keep
  working until removed. Blocks may still name one operation.
- TeamLead's `give_access` tool is gone, and `request_access` no longer takes
  `operation`. A proposal can no longer give a worker type lasting access.
- Access approved for one task now covers your whole request, including the
  workers TeamLead hands parts of it to, and ends when the request ends.
- "While one task runs" rows from before the upgrade whose task is over are
  ended when TeamLead starts. Requests for one task made before the upgrade
  still show on their chat cards but can only be denied (their task has
  ended); TeamLead withdraws any still waiting when it starts.

### Upgrading from credential bindings

Account links replace credential bindings, OpenShell bindings, and
stored-credential consent for MCP connections. Stored credentials stay where
they are; each needs to be linked to its tool once.

1. Stop Universal Agents and add the task-context signing keys to `team.env`.
    Rerunning onboarding adds them, or run the following. It adds only missing
    values and keeps existing ones:

    ```bash
    .venv/bin/python scripts/generate_internal_credentials.py \
      --instance-name <name> --env-file <instance_root>/team.env --write
    ```

    To use Docker secrets instead, set `ACCESS_GATEWAY_SIGNING_KEY_FILE` and
    `ACCESS_SCHEDULER_SIGNING_KEY_FILE` to files holding the values that the
    same script prints with `--print` in place of `--env-file ... --write`
    (see [Docker](docker.md#useful-environment-variables)). Startup refuses
    while either key is missing (`task_context_keys_missing`) and prints this
    command.

2. Start Universal Agents. If installed MCP connection records still carry
    credential binding data (every install from before this version that has
    MCP connections), startup stops with
    `obsolete_credential_binding_data_present` and prints the command that
    moves the records aside (`mv <registry> <registry>.before-account-links`).
    Connections from the configuration are recreated at the next start and
    wait for approval; TeamLead must propose again any connection it had
    proposed. Two other old records stop startup with their own messages: an
    `approved_attachments` section in `control/TEAM_AGENTS.yaml` (delete the
    section) and specialists created by an earlier version (move them aside
    as the message says and have TeamLead create them again).

    Stop again, run the commands, and start.

3. Link accounts with `/link <tool_id>` or `/link <tool_id> team`, for example
   `/link cli:huggingface`, `/link cli:x team` (after `/accounts share cli:x`), or
   `/link mcp:<connection_name> team`. People sign in to GitHub, Google and X
   with `/link cli:github`, `/link cli:google` and `/link cli:x`; the old
   `/auth github` and `/auth google` sign-ins moved to `/link`.
4. Add a web API tool for every service that agents call through
   `secure_requests` or `credential.api_request`, other than GitHub: TeamLead
   can propose one ([Access proposals](#access-proposals)), or you write its
   file ([Web API tools](#web-api-tools)). Requests to other hosts are now
   refused, even for public APIs, and requests that contain `credential_key`
   or `${VAR}` placeholders are refused (`api_request_placeholders_unsupported`).
5. For an MCP connection that used a stored credential, add
   `credential_injections` to its configuration (see
   [MCP](configuration.md#mcp)) and link an account. Connections whose
   operator configuration uses `${VAR}` headers keep working as before.
6. With authorization enabled, give agents access with `/access grant` or
   `assistant_cli.py access grant`, or choose TeamLead's standing access for
   your work and let TeamLead hand it on; the broker refuses agent requests
   that no grant covers.
7. Start Universal Agents. The supervisor refuses to start while the signing
   keys from step 1 are missing, and prints the command that adds them.

### Upgrading saved approvals

Installed MCP connection records (in the MCP registry) and
saved scheduled-worker policies (in the manager database) record how they were
approved. Versions before this one recorded decision ids or approval kinds from
the removed permission system, so startup refuses them and lists them. To keep
them, an administrator re-approves them as themselves, with Universal Agents
stopped:

```bash
assistant_cli.py access approve-existing --config /data/instance_config.yaml --by <your user id>
```

The command rewrites only those records, naming you as the approver, and
checks that they load. Scheduled-worker policies keep their settings; the old
approval records are dropped and a policy saved under authorization is recorded
as allowed by held access. MCP servers the registry first recorded from the
configuration file keep that origin instead of your name. To drop an MCP
connection instead, move its record out of the MCP registry folder
(`<instance_root>/runtime/mcp-connections/records/`).

### Upgrading from tool attachments

Tools are no longer attached to agent types: an agent's tools now follow the
access it holds ([How agents get access](#how-agents-get-access)), so attaching
a tool and restarting an agent to get it are gone. This breaks compatibility:

- Startup refuses a `control/TEAM_AGENTS.yaml` that still has an
  `approved_attachments` section, even an empty one. Delete the section and give
  the access with grants instead: workers get access for each task from what
  TeamLead may hand on, and a person approves the rest in the Inbox.
- An agent definition that still uses `tool_capabilities` or
  `capability_registry_ids` is refused when it loads; list its tools under
  `default_tools` as tool catalog ids or tool set ids instead. A specialist
  TeamLead generated earlier cannot be carried over: move its folder out of
  `control/agent_bundles/team_lead_generated` and ask TeamLead to create it
  again.

### Upgrading from MCP attachments

MCP servers are no longer attached to agent types; access to the server's tool
(`mcp:<name>`) decides who may use it. This breaks compatibility:

- With authorization on, a server installed before this version gives no agent
  access until someone grants it. Give the agent types that used it `full`
  access to `mcp:<name>` on the Access page, or run
  `assistant_cli.py access grant --config /data/instance_config.yaml --to agent-type:TeamArchitect --tool mcp:<name> --level full --everyone`
  (for everyone's work; add `--delegate-depth 1` to let TeamArchitect hand it
  on), or leave out `--everyone` to give it for the default user's work only.
- The broker no longer limits a server to the person who proposed it; people
  who have not authorized it for their own work are still refused.
- The `attachments/` and `removed-attachments/` folders in the MCP registry
  are no longer read; you can delete them.
- `plan_mcp_connection_activation`, `load_mcp_connection_tools` and
  `/mcp connection detach` are gone. TeamArchitect proposes a server with
  `mcp_connections(action="propose")` (one review), and people remove access on the
  Access page.
- `assistant_cli.py mcp-operation-levels --by` must name an active
  administrator.

### Upgrading from the Permissions page

The earlier approval system in the gateway is removed:

- The `/permissions` chat command and all its subcommands are gone. Use
  `/access` to list, give, remove and block access and to list requests
  waiting for you ([Access commands in chat](#access-commands-in-chat)).
- **Settings → Permissions** (Needs Approval, Approved Access, Custom
  Permissions and Administration) is gone. Decide requests in **Settings →
  Inbox**, see and change who holds what on the **Access** page
  ([The Access page](#the-access-page)), and review installed-procedure
  proposals under **Settings → Team → Installed procedures**.
- The Inbox no longer lists the older reviews with countdowns and comments,
  and links to them (`#interaction=` and `#permission=`) no longer open
  anything. Chat no longer accepts numbered replies as approvals, and the older
  Telegram **Review request** and **Request status** buttons and the older
  Telegram and Discord approval buttons no longer do anything. Requests made
  through the older system are not carried over; ask the agent to try again,
  and it asks with `request_access`.
- Emergency deny, which needed an offline signature, is replaced by blocks:
  `/access block`, `assistant_cli.py access block`, or **Add a block** on the
  Access page ([Blocking access](#blocking-access)).
- Temporary grants, saved permission policies, role policies, delegation
  limits for the lead, and administrator elevation are gone. Grants replace
  all of them: each person's choice of TeamLead's standing access gives
  TeamLead its first access for that person's work, and people give more with
  `/access grant`, `assistant_cli.py access grant` or an Inbox decision.
- A task that was waiting for an approval from the older system when you
  upgraded keeps waiting, because nothing can answer that approval any more.
  Stop it (`/stop` lists active work with the exact command to stop each
  task) and ask again.
- Agents no longer pass a worker's questions up to you through the lead. A
  worker that needs access asks with `request_access` and waits for your
  decision in the Inbox.

Update the gateway, the agents and any separately hosted Telegram or Discord
adapter together, so the new review buttons work and no part sends the older
messages.

### Removing the older approval data

The upgrade leaves the older system's database tables in place, unused, so you
can look at them first. Nothing reads them. To see which remain, and how many
rows each holds:

```bash
assistant_cli.py access drop-old-tables --config /data/instance_config.yaml
```

It checks the manager database, the gateway database and the broker's
credential catalog, and changes nothing. To delete those tables:

```bash
assistant_cli.py access drop-old-tables --config /data/instance_config.yaml --yes
```

Deleting cannot be undone, so back up the databases first if you may want the
old records. The command removes only the listed tables of the older system
(its approvals, reviews, grants, policies, Inbox items, version 1 workflows and
old schedule and credential-binding records); your conversations, tasks,
schedules, memory and current access stay as they are. You can run it while
Universal Agents is running, and running it again reports nothing left.

If authorization was on before this release, your NATS server also keeps the
older approval stream, named after your team id (or `transport.subject_prefix`)
followed by `_AUTHORIZATION`, in capitals, for example `TEAM_1A2B3C_AUTHORIZATION`.
Nothing uses it; remove it with `nats stream rm <name>` when convenient.

People and schedules after upgrading:

- An install from before this release keeps its first user as `admin`; make
  them an owner with `/user role <usr_...> owner`.
- Memory review now runs for each person. The first user deletes the two old
  team-wide schedules with `/delete schedule mem_rev_user_<instance>` and
  `/delete schedule mem_rev_stale_<instance>`; until then they only repeat the
  first user's reviews.
- Disabling a person now stops their work and deletes their schedules, and
  removing a person deletes their personal data after a holding period
  ([Disable or remove](newuser/index.md#disable-or-remove)).
- An `external_mcp_servers` entry may no longer contain `continue_on_error`;
  startup refuses it and names the entry. Deleting the key sends that server
  back for approval in Settings.

### Removed settings

The settings of the earlier permission system, the three `tool_policy`
switches and `tool_policy.data_folders` are gone. Universal Agents refuses to
start while `config.yaml` or `instance_config.yaml` still contains one of
them, or a `human_interactions` block inside an `agent_types.<name>` override
in `config.yaml`. The refusal names every removed key it finds in one message
and points to `authorization.enabled`, `authorization.starter_profile`,
`/access` and `assistant_cli access`. If `human_interactions` still holds
`workflow_max_steps` or `workflow_diagnostic_event_limit`, the message also
says that they moved to `access_workflows.max_steps` and
`access_workflows.diagnostic_event_limit`. If `tool_policy` still holds one of
these keys, the message also says that an administrator registers folders
outside the workspace with `/access folders add <name> <path>` or
`assistant_cli access folders add`.

To fix it, delete the listed keys. Nothing is converted for you; only the two
workflow limits carry over, and only if you copy their values to the
`access_workflows` block. Earlier versions often saved the whole
`authorization` block with its defaults, so expect to delete several keys at
once.

| Removed setting | What replaces it |
| --- | --- |
| `authorization.engine`, `authorization.fail_closed`, `authorization.policy_path`, `authorization.schema_path`, `authorization.cedar_request_timeout_seconds` | Nothing to set. Every decision uses the Cedar evaluator with the policies that ship in `ua_definitions/access/cedar/`, and an evaluator error always refuses. `authorization.cedar_binary_path` still chooses the evaluator program. |
| `authorization.user_approval` | Requests wait in the Inbox until a person decides; access approved with **Keep for later tasks** off (or **Allow for this task** in chat) ends with the request. |
| `authorization.admin_elevation` | A request that needs an administrator goes to administrators in the Inbox. |
| `authorization.emergency_deny` | [Blocks](#blocking-access). |
| `authorization.permission_delegation` (`mode`, `enforced_actions`, capability and target selectors, envelopes, lifetimes and use limits) | Every check always applies. What TeamLead may hand on comes from its grants: the starter profile and grants given with `--delegate-depth` and `--delegate-mode`. |
| `authorization.shell_command_profiles` | Access to the shell and Python tools (`code:shell`, `code:python`) is a grant like any other; commands run in the code sandbox, with folders and the network following access ([Shell and Python code](#shell-and-python-code)). |
| `human_interactions` (the whole top-level block, and the same block inside `agent_types.<name>`) | Nothing to set for the Inbox. `workflow_max_steps` and `workflow_diagnostic_event_limit` moved to `access_workflows.max_steps` and `access_workflows.diagnostic_event_limit`. |
| `tool_policy.restrict_to_workspace` | Agents reach only the task's folders under `workspaces/`, what other people shared with the task's person, and the [data folders](#data-folders) an administrator registered; with authorization on, a data folder needs a [folder grant](#files-and-folders). No setting opens other paths on the host. |
| `tool_policy.data_folders` | Data folders are kept in the access store. An administrator registers each one with `/access folders add <name> <path> [--read-only]`, `assistant_cli.py access folders add`, the Access page's **Files** tab, or by approving TeamLead's [proposal](#access-proposals); no restart is needed. Nothing is converted: register each folder again under the same name, and grants that name `data/<name>` apply to it. |
| `tool_policy.allow_web_search`, `tool_policy.allow_shell_commands` | Nothing to set; they never changed what agents could do. Web research is part of [internet access](#internet-access), which everyone holds until someone blocks it, and shell tools are given with grants like any other tool. |

The environment variables `TOOL_RESTRICT_TO_WORKSPACE`, `TOOL_DENY_PATHS` and
`TOOL_DENIED_PATHS` are removed too, because file tools and code reach only the
folders above and there is nothing left to deny. Universal Agents refuses to
start while one of them is set, in `team.env`, the environment, or your
container settings, and names each one; remove them and start again.

The web search and crawling keys `EXA_API_KEY`, `BRAVE_SEARCH_API_KEY` and
`CRAWL4AI_API_TOKEN` (and their `*_FILE` forms) are no longer environment
variables; they are [account links](#account-links) of `http:exa`,
`http:brave-search`, `http:crawl4ai` and `mcp:crawl4ai`. Universal Agents
refuses to start while one of them is set, names it, and gives the
`/link ... team` command that replaces it. An MCP server header that still
reads one (the Crawl4AI connection used `Bearer ${CRAWL4AI_API_TOKEN}`) is
refused too: replace the header with a `credential_injections` slot (see
`config.example.yaml`), approve the changed server in Settings, and link the
token with `/link mcp:crawl4ai team`.

The Crawl4AI settings `web_services.crawl4ai.verify_tls`, `expected_version`,
`allow_execute_js`, `job_poll_seconds` and `job_timeout_seconds`, and the
variables `CRAWL4AI_VERIFY_TLS` and `CRAWL4AI_EXECUTE_JS_ENABLED`, are removed:
the broker sends Crawl4AI requests and always verifies TLS certificates, and
crawling uses the Crawl4AI MCP connection. A saved setting that still holds
its old default is dropped when the file is read; any other value, and either
variable, stops the start with a message that names it.
