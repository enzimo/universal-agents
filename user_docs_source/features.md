# Agent Features

This page summarizes the user-facing features that are useful after the agent
is running and gives examples you can try from Web Chat, CLI, Telegram,
WhatsApp, Discord, Signal, Photon/iMessage, or an embedded generated-site chat.

## Chat and Sessions

### Chat alerts and Settings navigation

In **Settings**, turn on the **Chat Alerts** switch and allow notifications in your
browser. Alerts appear for new messages while the Web Chat tab is hidden or another
window has focus. They contain no conversation text. Click an alert to open its chat.
This requires HTTPS and a supported desktop browser; the page must remain open.
The tab title also shows unread conversations and requests needing attention,
even without browser notification permission. Turn the **Chat Alerts** switch off
to stop desktop alerts for this user in this browser.

Reopening Settings returns to the tab you last used until you sign out or reload
the page. **Team** lists running agents and all defined agent classes with their
descriptions, including specialists that are not currently running.

**Access**, next to **Inbox**, is the access map: what each agent type can use
and where that access came from, what each person has given, and every tool's
operations and linked accounts. You can remove access, add a block and unblock
there, within your own authority. `/webchat?view=access` opens it directly, and
`/webchat?view=access#agent=<type>` opens it at one agent type. See
[The Access page](permissions.md#the-access-page).

### Inspect and clear tasks across agents

Open **Settings → Tasks** in Web Chat to see your current, queued and waiting
tasks across conversations, grouped by agent. Each section includes task descriptions,
queue positions and waiting reasons. It refreshes every 10 seconds while open and shows
20 tasks per agent per page. Other users' tasks remain private. If one agent cannot
respond, its section reports that problem while the other sections remain usable.

TeamArchitect appears first. Long descriptions, waiting reasons, and activity
notes have compact previews; expand them to read the full available text.
**Finished tasks** starts collapsed below active work. Expand it to browse
recent results from all agents, including agents that have stopped.

Refresh buttons show a rotating circle while lists update, without a temporary
loading message. Errors remain visible if an update fails.

Choose **Remove** to cancel a queued task, or **Stop and remove** for current work.
Choose **Purge this queue** to cancel your queued and waiting tasks on that agent while preserving
current work. Review the inline confirmation before applying either action. If
the queue changes, refresh and confirm the new list. Cancellation history remains
available; removing a task does not erase previous results or undo external actions.
Remote worker stop confirmations can arrive later and are reported separately.

These controls bypass the model queue, so they can free capacity when an agent's
task limit is full. From chat, `/stop` lists your unfinished work and
`/stop task <task_id>` cancels one execution and its attached work. `/stop all`
cancels all your unfinished work, including current work. It is broader than
**Purge this queue**. A full queue alone does not mean every task is invalid.

At 90% of the task limit, TeamLead sends a queue warning through normal notification
delivery. With the default limit, this is 45 of 50 tasks. Current, queued and waiting
tasks all count. Each owner with a retained reply route receives one warning until
usage falls below 90%; a restart can send a fresh warning. This warning does not
complete your chat request or create another task. Internally created and restored
tasks are checked on the scheduler's next pass.
For internal repair checks with no chat reply route, TeamLead uses the instance's
configured notification destination only when its configured user matches the task
owner.

The runtime also checks for tasks that cannot run but remain in the execution
queue. It removes those queue entries while keeping the tasks, their waiting
requests and history. Other ready work can then proceed. TeamLead sends the requester a
separate safety notice through normal notification delivery; it does not require
a model turn or consume the pending final answer. Delivered safety notices remain
deduplicated after restart.

If a final answer is held for review, the runtime reports a remaining decision or
unfinished work. If no reason to wait is recorded, it pauses the task and sends a
notice instead of retrying it. Use **Settings → Tasks** or `/stop` to inspect the
task. These checks prevent invalid queue entries from spinning; they do not detect
every possible repetition inside a model turn.

If the task queue or incoming-message buffer is full, a new request receives a
"not queued" reply, including while the agent is busy. Remove unwanted work and
send the message again. Already admitted work is retained. Delivery failures keep
the request eligible for broker retry instead of acknowledging a silent rejection.

A control timeout means the result is unconfirmed; it does not prove the agent
disconnected or the cancellation failed. The Tasks page refreshes after an
unconfirmed removal without repeating it. Check the refreshed list, or run
`/stop`, before submitting another cancellation. Task controls remain available
while a model request is running. A model timeout with no available fallback
fails that task, returns a retry message, and frees its task slot for other work.
Failed and cancelled turns save a local summary without making another model
request. Optional conversation compaction waits for a normal turn.

Task removal does not disable recurring schedules. An access request whose task
ends is withdrawn from the Inbox. Repair the underlying schedule blocker as well,
or later occurrences can create more work. For a changed team-written agent
definition, let TeamLead show you the differences and re-save the schedule's
worker policy, then verify a completed run (see
[When an agent definition changes](scheduled-workers.md#when-an-agent-definition-changes)).

### Personal and team memories

In **Settings → Memories**, select **Personal** or **Team** and click **New**
beside Refresh. Enter the memory, optional comma-separated tags, and an optional
expiration, then Save. Personal memories belong to your account; team memories
are shared with the team. Only administrators can create or change team memories.
Both lists show the most recently updated memories first, so a new or edited
memory appears at the top of the first page.

In chat apps other than Web Chat, `/usermem list` shows your personal memories
and `/coremem list` the team's; `/help` lists their add, replace and delete
forms, and the same rules apply as in Settings. You can also just ask the
assistant "What do you remember about me?" or "Forget that".

The assistant changes or deletes a memory only as it last read it. If the
memory changed in the meantime, for example from another chat or in Settings,
the assistant's change is refused and nothing changes; the assistant reads
the memory again before it decides. In the same way, undoing a change works
only while it is the memory's latest change.

### Knowledge base

Agents keep a shared knowledge base: pages of how-tos, decisions, lessons and
references they search before researching something again, and add to when they
learn something useful. Each page keeps every earlier version, so a change can
always be traced and undone.

- **Team pages** are shared with everyone on the team. **Personal pages** belong
  to you: only work done for you can read or change them.
- When an agent working for you wants to change a team page, the change waits
  for review and only you (and work done for you) can see it until it is
  published. This keeps your private details out of pages the whole team reads.
- The knowledge base never stores passwords, keys, tokens, social security
  numbers, card numbers or bank account numbers. Checks include page names,
  tags, text, change descriptions and review decision reasons. Administrators
  can add their own never-stored patterns
  (`agent.knowledge.sensitive_content.blocked_patterns`).
- Agents are limited in how often they may change the knowledge base and the
  same page (`agent.knowledge.limits`), so no agent can take it over or keep
  rewriting a page. A page that several agents keep changing is frozen for a
  while, and agents add notes to it instead. Proposed team archives count
  toward the same hourly limit as directly archived pages.
- The **Knowledge Curator** reviews the changes waiting for you once a day
  (`agent.knowledge.curation`): it publishes what helps the whole team after
  removing anything private to you, or rejects it with a reason. Once a day it
  also tidies the team pages: it works notes into their pages, fixes broken
  links, merges pages about the same topic, and freezes or reverts a page that
  agents keep fighting over. It starts only when there is something to review.
  Notes on a frozen page stay open until the curator can merge them, after the
  freeze expires or once it lifts the freeze. Shared revert history names only
  the restored revision.
- When two pages are merged, the page that was merged away is archived with
  all its versions, and its name leads to the page that was kept, so links to
  it keep working. That name is not used for a new page again.
- Administrators get one Inbox notice a day when team pages were frozen or an
  agent had many knowledge writes refused. It names the pages and counts, never
  their text. A freeze is reported even if it ended before the daily check;
  the notice says whether it is still active, expired or was lifted.
- The team lead can save a lesson from an agent's after-action report as a
  knowledge page. A lesson from work done for you goes to the Knowledge Curator
  for review like any other team change. If saving succeeds but closing the
  report review fails, retrying with the same lesson finishes the review without
  saving a second copy, including after a restart.
- When the team lead works on a request, it is shown the titles of knowledge
  pages that match it, so earlier findings are reused instead of researched
  again.

#### The Knowledge page

Open **Settings → Knowledge** in Web Chat (or `/webchat?view=knowledge`) to read
and write knowledge pages yourself. The link appears while the knowledge base is
on (`agent.knowledge.enabled`).

- **Team** and **Personal** open that space's **Home** article. Edit it to
  organize links to topic pages, using `[[page-name]]`. If it does not exist,
  select **Create Home page** to write it. This creates an ordinary page named
  `home`; existing pages stay available through **All pages**.
- **All pages** opens a searchable directory beside the article, 20 pages at
  a time, the most recently changed first. Scroll its rows to browse; the page
  controls and total stay visible below them. Select **All pages** again to
  close the directory, or **Home** to return to the landing article.
  Type words in the search box to find pages by their
  text instead. **Current pages** is the default. Select **Archived pages**
  below the search box to find a page you archived, then open it and select
  **Restore**. Pages merged into another stay out of both lists, though a
  merged page's name still opens the page it was merged into.
- Select a page to read it. It shows its summary, tags and text, which pages
  link to it (**Linked from**), its open notes, who changed it last and when,
  and whether it is archived or frozen for agents. **Add note** records
  something to fold into the page later.
- Links in a page's text (`[[page-name]]`, or `[[team/page-name]]` from a
  personal page) open that page. A link to a page that does not exist yet stays
  plain text marked "no page yet"; links inside code stay as written. Opening a
  merged page's name shows the page it was merged into, with a note saying so.
  Each article has its own browser address. You can bookmark it, refresh it,
  or use browser Back and Forward to retrace article links. Sharing an address
  does not grant access; a personal address always opens the signed-in person's
  own pages.
  A new page cannot take a merged page's name: edit the page it leads to, or
  choose another name.
- **Edit** and **New page** open an editor for the title, summary, tags and text.
  Clear the summary field to remove the saved summary.
  Your changes to team pages are published at once; every team member may edit
  team pages. If someone else changed the page while you were editing,
  nothing is saved and your text stays in the editor: select **Show the latest
  version**, merge what you need, and save again.
- The knowledge base never stores passwords, keys, tokens or identity numbers;
  the editor says where they are (field, line and column) without repeating them.
  Email addresses, phone numbers, street addresses, birth dates and text copied
  from your own memories are held back on team pages until you select **Save
  anyway**, because everyone on the team will see them.
- **History** lists the latest 50 revisions with who made it and when, including
  merges ("Merged"). **View** shows an
  earlier revision, and **Revert** (select it twice to confirm) makes it current
  again as a new revision. **Archive** (also twice) takes a page out of current
  lists and searches; every revision is kept and **Restore** brings it back.
- **My proposals** lists the team changes agents working for you have proposed.
  A number on the tab, on the **Knowledge** link in Settings and on the Settings
  cog shows how many wait for you.
  Each shows what would change, line by line, and the agent's reason.
  **Publish** publishes it as proposed, **Edit, then publish** lets you change
  it first, and **Withdraw** drops it. Publishing an archive proposal archives
  the current page without changing its text, summary or tags. It has no edit
  option. Only you (or the knowledge curator
  working for you) can publish your proposals; a proposal with private details
  asks for a second click before it reaches the team.

### Conversations

Use the agent from the browser Web Chat, local CLI, Telegram, WhatsApp,
Discord, Signal, Photon/iMessage, or an embedded generated-site chat. Replies
return to the originating channel and conversation.

If you are new, ask "what can you do?" or "give me the tour", or run `/tour`
from a chat surface that supports slash commands. An explicit tour request
starts the guided walkthrough immediately. The assistant sends one short tour
step, pauses for your next message, and continues one step at a time. You can
say "finish" to pause the guide so it can resume later.

The core tour covers the team, a map of the Web Chat screen, tools and secure
sign-ins, the Inbox where agents ask before they act, reminders, objectives,
generated pages, memory, files, and watching or stopping work. It then offers
a menu of deeper topics: the Access page, using more than one chat app, your
own model, the Workflows page, appearance and voice messages. Owners and
administrators also get topics on adding people, the team's model keys,
growing the team, team-wide access and the Status page. The tour fits itself
to the chat app you use (Web Chat names its buttons and tabs; other apps get
links) and to your role, which TeamLead reads from your signed request; the
role only chooses what to explain and grants nothing.

Good first requests:

- "Give me the tour, then show me three things you can help with this week."
- "Help me compare these two options and tell me what information is missing."
- "Research the current setup steps for this service and cite the sources."
- "Turn this plan into a checklist I can keep open in the browser."
- "Remind me every Friday to review open bills and renewal dates."

The browser Web Chat at `/webchat` keeps gateway-owned thread history. Opening it
restores existing conversations without creating a new one; with none yet, it
shows a single **New Chat** row. Click **New Chat**, or send your first message
when no conversation is selected, to start one. A thread is named after the
first message you type in it (slash commands do not count) and keeps that name
unless you rename it. Updates the team sends you and notes from memory reviews
appear as their own threads, **Updates** and **Memory review**, once there is
something in them.
It loads the active thread's newest 50 messages; other threads load when selected. Scroll
up to load earlier pages, or use **Load earlier messages**. Loading older history
keeps your reading position, and background replies do not scroll you away.
Chat keeps a live server-sent event connection for new messages and access
review notices. The account area shows whether updates are connected or retrying.
After a temporary disconnection, Chat reconnects and retrieves missed messages
without reloading the page. A missing heartbeat triggers recovery within 15
seconds. User actions still use authenticated HTTP requests.
Messages accepted on one device reappear on another device signed in as the same
canonical user, and Website process restarts do not erase them. The thread list
also stays in step across devices within about two seconds: a thread renamed,
archived or started on one device updates on the others, and the pulsing dot
shows on every device while the agent is working on a reply. Use the thread
row edit icon to rename a saved thread, the archive icon to hide it from the
active list, and a message bubble copy icon to copy that message's raw markdown
or text. Each canonical user sees only their own server sessions and durable
transcript. Use Log out, at the bottom of Settings' left column, before
another user signs in from the same browser profile. The small triangle mesh
above your name shows the team at work: the middle triangle glows gold while
the team lead works, and each busy worker lights a triangle in its own color
(hover one to see which kind of agent it is, such as WebResearchAgent). It
follows the team's status that arrives with each live update, about every two seconds, the same copy **Settings → Team**
and **Settings → Tasks** show. A number on the Settings cog counts what waits
for you: requests you can decide and unread notices in your Inbox (the number on
the **Inbox** tab), plus team changes your agents proposed (the number on the
**Knowledge** link). Popups break into
glass triangles when they close, and form from them when they open.
Settings > Appearance > Interface Animation
turns interface motion off; your system's reduced-motion setting always does.
In Customize Theme, changes only preview until you choose Done; Cancel puts the
theme back, and Share settings copies the theme as text for
`instance_config.yaml`. The sign-in page greets you for the time of day ("Good
Morning!") and, once you have signed in on that browser, by name ("Good
Morning, Alice!"); the sign-in window appears under the greeting. Press any key
or click to skip ahead. Only your browser remembers the name: signing out keeps
it, and **Not Alice?** in the sign-in window makes the browser forget it. A
one-time admin invitation opens a required
token-enrollment popup: the user generates, reveals, and copies a reusable
`uauser_...` token before Chat unlocks.

Upgrade note: older releases kept message lists only in browser local storage.
The durable implementation does not upload those untrusted lists or infer them
from manager history. Export important browser-only threads before upgrading;
only messages accepted after the upgraded gateway starts are durable.

When an agent needs a decision from you, the request appears in **Settings →
Inbox** and is announced once in a chat where you can decide it. Web Chat shows
that announcement as a card with a one-line summary, the decision buttons and
the full details under **Details**; Telegram and Discord add decision buttons. See
[Access requests and reviews](#access-requests-and-reviews).

### Web Chat Commands

The guide above the Web Chat transcript is a short reference. It links each
command to the full documentation and shows only a few representative options.
Use `/help` for the same compact list in the transcript.

| Command | Use |
| --- | --- |
| `/help` | Show available interactive commands |
| `/docs` | Return the browser documentation URL |
| `/tour` | Start the onboarding tour |
| `/auth ...` | Check your sign-in status, save model provider keys (`/auth model`), set up password managers, and store the team's sign-in apps; see [Secure Tool Authentication](#secure-tool-authentication) |
| `/link ...` | Sign in to a tool or link its account; see [Account links](#account-links) |
| `/accounts ...` | See whose account agents use for each service (also in **Settings → Accounts**); owners and administrators share a service's team account or keep it private, and decide whether people added from now on may use the team's model keys; see [Shared or private](permissions.md#shared-or-private) |
| `/setup` | See what you still set up for your own work: your own model and your own accounts for the private services someone in the team uses; see [First-Time Setup](newuser/index.md#first-time-setup) |
| `/access ...` | See, give, remove, and block agents' and people's access to tools, and list requests waiting for you; see [Access commands in chat](permissions.md#access-commands-in-chat) |
| `/user ...` | Manage users, their roles (owner, admin, member), and Web Chat access; see [Multi-User Onboarding](newuser/index.md) |
| `/new` | Start a new session in the current channel |
| `/objectives` | List Objectives and their available controls; see [Runtime Controls](#runtime-controls) |
| `/schedules` | List scheduled work and its available controls; see [Runtime Controls](#runtime-controls) |
| `/mcp ...` | Review MCP connections and hosted servers; see [MCP Connections and Hosted Tools](#mcp-connections-and-hosted-tools) |
| `/stop ...` | List or cancel active work; see [Stopping Active Work](#stopping-active-work) |

Use the thread-history controls in browser Web Chat to open, rename, or archive
saved threads. Archive does not permanently delete transcript messages. Direct
external-platform registration and `/user link` must
be run on the platform whose identity is being linked. Broader session, memory,
and topic commands are kept out of the browser command list.

The local CLI also supports `/reply <message-ref> <text>` and prints local
`[msg_n]` references for that purpose. Other chat channels show plain replies
without message-reference prefixes.

### When work gets stuck

You are told when one of your requests stops moving, without having to ask.
The team lead checks every agent's unfinished work on each pass of its
scheduler, and after a condition has lasted its threshold
(`agent.schedule_maintenance.stuck_tasks`) you get an Inbox notice and a
message in the chat the request came from. You hear once per problem, and
again when that warning clears. A cleared warning means the reported condition
ended; check the task history for its actual progress.

| Condition | Told after (default) | Also told |
|---|---|---|
| The agent working on it stopped responding, and no agent took the work over | 10 minutes | Administrators |
| It was handed back by a worker that stopped and no agent picked it up | 30 minutes | Administrators |
| It is waiting for an access decision in the Inbox | 1 day (a reminder) | Administrators |
| It asked you a question that is still unanswered | 1 day (a reminder) | — |
| It is in progress but has shown no progress at all | 2 hours | — |

Some of these are repaired automatically before you would be told: when the
agent holding your request stops, the team lead starts another agent of the same
kind that takes the work over; an agent waiting on your answer or on an access
decision stays up until it gets one; an access decision made while the agent
restarted still reaches the task; and when a request ends, any work it handed to
other agents stops too.

A request that involves several agents is reported once, as your request.
Administrators' notices name the kind of agent and what to check, never the
text of your request. Stuck schedules are reported the same way (see
[Scheduled workers](scheduled-workers.md#when-a-schedule-is-stuck)).

After the host wakes from sleep, the task monitor gives workers one minute to
refresh their status before treating old heartbeats as a stopped worker. A
confirmed worker exit can still trigger replacement during that wait. Automatic
maintenance runs belong to the team and their notices identify system maintenance,
rather than a request you assigned.

### Stopping Active Work

Bare `/stop` lists your current nonterminal task graphs. It does not cancel
anything until you choose an explicit scope:

- `/stop task <task_id>` cancels the complete graph containing that task,
  including delegated agent work.
- `/stop conversation` cancels every active graph in the current conversation.
- `/stop session` cancels every active graph in the current session, including
  work in its other conversation lanes.
- `/stop all` cancels all current nonterminal work owned by you across sessions
  and conversations.

Your own stops apply at once, whatever an agent is doing. Agents cannot stop
your other requests: while TeamLead works on one request it may pause, resume
or stop only that request's own tasks. If you ask it about another request, it
tells you to ask in that request's thread, or to stop it yourself with
`/stop task <task_id>` or **Settings → Tasks**.

Stopping work also ends the commands an agent is running for it, such as a
build, a test run or a script, and the background processes those commands
started, such as a development server. Each is asked to stop and is ended a
second later if it keeps going, and no further command of that work starts.
Not ended: a process that detaches itself completely (for example with
`setsid` or by turning itself into a daemon), and a background process started
by an earlier step of the work that had already finished. Each agent marks its
part of the work as stopped only after its commands have ended, which can take
a few seconds.

Future schedules remain separate from every stop scope. Pause an Objective or
use its schedule controls when you also need to prevent later runs.
The chat receipt summarizes scheduled items and distinct scheduled objectives
with counts. In Web Chat, **Review schedules and objectives** opens the dashboard
with the full lists and their controls.

## Secure Tool Authentication

Credentialed tools use the secure broker. The agent can ask the broker to
run an allowlisted action, but raw tokens, passwords, session keys, and secret
values stay outside the agent process.

The broker takes the calling agent from that process's own token and the person
and their roles from a signed record of whom each task works for. Role names
sent in a request are never accepted as authority. It also checks the level
each operation needs; see
[Tool access and account links](permissions.md#tool-access-and-account-links).

`/link <tool_id>` is the one command for a tool's account. For GitHub, Google
and X it starts your own sign-in:

```text
/link cli:github
/link cli:github token
/link cli:google
/link cli:google json_token
/link cli:x
```

`/link cli:github` starts the GitHub browser sign-in with MFA (open GitHub's
device page and enter the one-time code; no token to create), `/link cli:github
token` opens the page to paste a personal access token instead, and `/link
cli:github web-all` requests the full high-privilege scope list. The setup page
TeamLead opens for you is the browser sign-in too. `/link cli:google json_token` imports a gog
token.

`/auth` keeps what is not a tool's account:

```text
/auth status
/auth model
/auth model me
/auth google team_client
/auth x team_client
/auth bitwarden
/auth onepassword
/auth keepass
/auth vault
```

`/auth status` shows your sign-in status for GitHub, Google, X and the password
managers, then each model endpoint's key: saved, overridden by an environment
variable, or recently rejected (never the key itself). `/auth model`
(administrators and owners) opens a secure page that tests and saves the
team's model provider keys, one per endpoint address; others get their own
page, which `/auth model me` opens for anyone. There a person saves their own
key for an endpoint the team uses, or their own model for all their work;
either is used for that person's work only. See
[Model Keys](model-providers.md#model-keys).
`/auth google team_client` and `/auth x team_client` (administrators
and owners only) store the team's sign-in app that people sign in through with
their own accounts; storing one gives nobody access. Bitwarden, 1Password,
KeePassXC and Vault are password managers the broker reads saved credentials
from, not accounts a tool acts with.

GitHub, Google, X and Bitwarden can return broker-owned browser forms. Submit
sensitive values only through those broker forms or through configured env /
secret files, not in normal chat. GitHub, Google and X sign-ins belong to the
person who made them: agents use them only for that person's work.

When an agent asks you to store or delete a credential, Web Chat shows a
dismissible **Open secure credential form** action with a live expiry countdown.
A setup page an agent asks for (see
[When an agent needs a tool set up](#when-an-agent-needs-a-tool-set-up)) shows
its own **Open** action the same way.
The action is available only in the browser response that starts the workflow;
it stays at its original position in the chat timeline and disappears as soon
as the form ends: the chat then shows one line saying how it ended, such as
"The secure form for `bifrost_virtual_key` is done: the credential was
stored.", while the agent goes on with its task.
If the gateway loses the form's status, it closes the card and says it cannot
confirm whether anything was stored or changed. A delayed or failed send to
the agent does not keep the card open. The action is intentionally absent after
refresh and from saved transcript history. At expiry, Web Chat replaces the Open action
and countdown with an inert expiry notice while it waits for the matching result.
Dismiss remains available. If the form expired before submission, start the
credential request again. If its result is unknown, check the stored credential
before trying again. Manual account-signup forms include
a separate validated link to create the account before returning to store its
credential.

You can keep chatting while a credential form is open. Each later message starts
a normal conversation turn and does not steer or resume the waiting credential
request; only the broker result resumes that request.

Secure credential forms start with sensitive fields masked. Use **Show** to check
the entered value and **Hide** to mask it again before submitting.

`/link cli:google` is the main Google sign-in. For an
External Google OAuth app, publish the consent screen to In Production before
authorizing; Testing-mode refresh tokens expire after 7 days.

See [Tools and Authentication](tools/index.md) for provider setup.

### Adding a service

When you ask for something the team's tools cannot reach yet, TeamLead picks
the simplest safe way to add the service and tells you why in one line: a tool
the team already has, the service's command-line tool (run by the secure
broker in a sandbox, with a level per command and your token kept from the
agents), a web API tool, or the service's MCP server. It proposes the tool,
the access the work needs and any steps you do on the service's website in
one card for an administrator, and after approval sets it up with you. A
service that needs a browser sign-in Universal Agents does not support yet
needs operator work; TeamLead says so and offers the nearest option, such as
a token. See [Command-line tools](permissions.md#command-line-tools-clis).

### When an agent needs a tool set up

You do not need to know these commands in advance, and TeamLead does not ask
whether it should start. When you ask for something that needs a tool with an
account, such as "What repos can you access in GitHub?", TeamLead sets the
tool up straight away: the approval cards and setup pages are where you
agree. It finds what is missing (access for itself and the agent doing the
work, your access, your sign-in or a linked account, the team's sign-in app,
and any steps the service needs outside Universal Agents), asks for the access
it lacks, and opens every page in your chat at once:

> I need you to sign in to GitHub so I can list the repos you can access in
> GitHub. **Sign in to GitHub**

The page opens in a new tab; what you enter goes straight to the secure broker,
never to the agent. When everything is done, the task carries on by itself,
for example "Signed in to GitHub as exampleuser. Checking your repositories now."
You can also finish the same step yourself with `/link <tool_id>` (or
`/auth google team_client`) while the task waits; the task hears about it the
same way.

- Pages open in order where one needs another: the team's sign-in app before
  your sign-in, a step outside Universal Agents (such as creating a token on
  the service's website) before the account link that stores it, and a tool
  the service depends on before the service.
- If a page expires before you finish, a fresh one opens by itself, up to
  three times in the same request; after that TeamLead tells you what is
  still needed and that you can do it any time with `/link <tool_id>`.
- If you close a page without finishing, or deny the access request, the
  setup stops, TeamLead tells you what cannot be done, and it does not ask
  again in this request.
- If the missing step is an administrator's, such as the team's Google or X
  sign-in app or access you do not have, and you are not an administrator,
  nothing opens: TeamLead explains which step is missing and who can do it,
  and ends the task.
- If the gateway restarts while a page is open, a fresh page opens.

When the request needs two or more things, one chat message says so first
and links the workflow page that tracks them all, for example "I need 2
things before I can list the private repos you can access in GitHub: Access to
GitHub; Sign in to GitHub. I'll ask for each one here and carry on by myself
when they're done. Follow along here: <link>". The message reads the same in
Web Chat, Telegram and Discord. For "What repos can you access in
GitHub?" when TeamLead has no GitHub access and you are not signed in, the
workflow has three steps: **Access to GitHub** (waiting for your decision in
the Inbox), **Sign in to GitHub** ("Waiting for the person to finish the
page", then "Signed in as exampleuser"), and **Do the requested work** after both.
The steps update by themselves. A step you do outside Universal Agents, such
as creating a token, shows its instructions in chat and a **Mark Done** button
on the workflow page. When only one thing is missing there is no workflow;
the page just opens. See
[Complete workflow reviews](permissions.md#complete-workflow-reviews).

TeamLead remembers what worked. The runtime keeps, for you and each tool, its
last known state: ready (for example signed in as exampleuser, used today), what it
still needs, or unknown. TeamLead uses a ready tool straight away without
checking; if it stops working because a sign-in expired, the state changes by
itself and TeamLead walks you through setting it up again. For a tool nobody
used for a while, the system checks again every 30 days by default
([Tool setup checks](configuration.md#tool-setup-checks)) and sends you one
Inbox notice if something needs doing.

Missing access is asked for the usual way (see
[Access Requests and Reviews](#access-requests-and-reviews)). Setting a tool up
gives nobody access to it by itself.

### Account links

Some tools use a stored credential instead of a sign-in: Hugging Face
(`cli:huggingface`), web APIs such as `http:github`, and MCP connections that
declare credential slots. An account link tells the broker which of your stored
credentials such a tool uses. Send:

```text
/link cli:huggingface
/link http:portainer team
```

`/link <tool_id>` (or `/link <tool_id> me`) links your own account;
`/link <tool_id> team` links the team's account and needs a team owner or
administrator. Chat shows **Open account link form**. The broker's form shows
whose account it is, which agents use it, where the credential goes, and how it
is sent. Pick one of the stored credentials that fit, or type a new one under
**Add a new credential**; the broker saves it and links it in the same step.
Then confirm. The broker uses your own link first, then the team's while the
team shares the service; for a service the team keeps private, `/link
<tool_id> team` is refused and each person links their own. `/accounts` shows
which services are shared (see
[Shared or private](permissions.md#shared-or-private)). A link gives nobody
access to the tool by itself.

For sign-in tools, `/link <tool_id>` starts your sign-in instead (see above).
X also takes a team account once an administrator shares X with
`/accounts share cli:x`: `/link cli:x team` then links it (administrators
only), and the broker uses it for people who have not signed in to X. GitHub and
Google sign-ins are personal, so `/link cli:github team` and
`/link cli:google team` only say so. A tool that uses no account replies that
there is nothing to link.
See [Account links](permissions.md#account-links) for details and the operator
commands.

#### How you know `/link` worked

After you use the form or sign-in page, the chat where you ran `/link` gets one
message saying how it ended, in Web Chat, Telegram, Discord and the other chats:

- **Linked**: for example "Linked the team's account to Crawl4AI
  (mcp:crawl4ai). Settings → Access → By Tool lists it. Linking an account
  gives nobody access by itself." For a sign-in: "Signed in to GitHub."
- **Closed or refused**: "The account link form for Crawl4AI (mcp:crawl4ai)
  was closed without linking anything.", or "Nothing was linked to …" with the
  broker's reason.
- **Expired**: the page's time ran out; the message names the `/link` command
  to run again.
- **Couldn't confirm**: the gateway could not read the result from the broker.
  Check **Settings → Access → By Tool** (its **Accounts** row names who owns
  each linked account and who linked it) or `/auth status` for sign-ins.

In Web Chat the **Open account link form** or **Open** card disappears when
that message arrives. While the page is open, chat history keeps only a line
such as "Account link form opened for mcp:crawl4ai (the team's account)."; the
page's address is never saved. The card lives only in the browser tab that ran
`/link`, so refreshing the page removes it, and the result message still
arrives. If the gateway restarts while a page is open, the page still works but
no message follows; check **Settings → Access → By Tool** or `/auth status`.

`/auth google team_client` and `/auth x team_client` pages get the same kind of
message when they end ("The team's Google sign-in app is stored; people can now
sign in with it.").

## Access Requests and Reviews

With authorization on, every tool call an agent makes is checked before it
runs, for the agent and for the person its task works for. Access you give
agents serves your own work: when the same agent works for someone else, it has
only what that person gave it. A refused call names the tool, the level, and
the next step. The agent asks with `request_access` and a reason. TeamLead
hands the access on when it may; otherwise the request waits for a person, and
the agent's task pauses until someone decides. You decide requests for access
you hold yourself; an administrator decides the rest. Access goes to the agent
type, so a restarted agent keeps it, and an agent's tool list follows its
access: a new grant can be used on its next step without a restart. See
[Whose work access serves](permissions.md#whose-work-access-serves) and
[How agents get access](permissions.md#how-agents-get-access).

Each person chooses how much TeamLead may do for their own work without asking
(`/access profile trusted|careful|manual`, or the Access page). When a service
is missing from the tool catalog, TeamLead proposes the whole change (a new web
API tool, access, restrictions, data folders, account links) for one
administrator review; see [Access proposals](permissions.md#access-proposals).

Agents ask for folders the same way, for example read access to a data folder
an administrator registered with `/access folders add` (`data/reports/2026`),
and a shell or Python command reaches only the folders the agent and its
person may use. Web research and the network for code are on for everyone
until an administrator blocks them (`/access block team internet --reason
"..."`); full access to a code tool no longer opens the network. A single
command runs outside the sandbox only after a person approves
`code:unconfined`. See [Internet access](permissions.md#internet-access),
[Files and folders](permissions.md#files-and-folders) and
[Shell and Python code](permissions.md#shell-and-python-code).

People share files and folders from their own folder with each other
(`/share`, `/unshare`, `/shares`, or **Share…** above the preview in Workspace Files), and send a
file into someone else's folder with `/send`, which that person or an
administrator approves first. See
[Sharing files with people](permissions.md#sharing-files-with-people).

Platform changes wait the same way. TeamLead stages the exact change, such as a
setting, a restart, a new specialist, a team skill, or a hosted MCP server, and
a person approves or denies exactly that change. A new remote MCP server is an
access proposal: one card installs it and says who may use it. With authorization off,
TeamLead asks for your agreement in the chat instead.

When an agent updates, pauses, resumes, turns off or removes a schedule the
runtime manages, such as the heartbeat launcher or memory-review maintenance,
it stages the exact change and a team administrator approves it in the Inbox
before it happens (with authorization off, you agree in the conversation).
The approval covers only that change to that schedule as it was when staged;
if the schedule's settings change first, the agent stages it again (its regular
runs do not count). Your own reminders change at once, and only you and agents
working for you see them. An agent changes a schedule only as it last read it:
if another of your requests changed the schedule meanwhile, nothing is changed
and the agent reads it again first. See
[How agents create and change schedules](scheduled-workers.md#how-agents-create-and-change-schedules).
Pausing or cancelling an Objective does not provide an indirect bypass: its
lifecycle cascade skips linked system schedules. See
[Schedules the runtime manages](scheduled-workers.md#schedules-the-runtime-manages).

### The Inbox

Open **Settings → Inbox** in Web Chat to decide requests:

- An **access request** says which agent asks, for which tool and level, for
  whose work, for how long, and why. If the person the task works for lacks
  the access too, it says so, and only an administrator can approve it. Tick
  **Keep for later tasks** (ticked when the agent asked to keep it; off: this
  task only) and choose **How long**
  (until you remove it, or 1, 7 or 30 days); on TeamLead's own requests, tick
  **TeamLead may hand this on to workers without asking you** to let it pass
  the access on. Then choose **Approve**, **Approve reading only** (when more
  than read was asked), or **Deny**.
- A **proposal**, a **file delivery** or a **change request** lists every row
  it would change. Choose **Approve** or **Deny**; proposal rows that give
  access each have their own **Keep for later tasks** box.
- **Notices** need no decision: access TeamLead handed on without asking (with
  **Revoke**), team skill changes, updates that changed an agent one of your
  schedules runs, shares and files you received, credentials to choose
  for tools you approved, schedules and tasks that are stuck, and (for
  administrators) MCP servers that stopped answering, after agents gave up
  retrying them. The list opens on **Unread** notices (**All** shows read
  ones too), 20 to a page. Mark one read with its **Mark as read** button,
  tick several and choose **Mark selected as read**, or choose **Mark all as
  read** (it asks once more) to clear every page at once.

You see the requests you could decide and the requests made for your own
tasks. Deciding wakes the waiting agent, which continues with your decision. A
request whose task ends first is withdrawn. From any chat, `/access requests`
lists the requests you can decide, with a link to the Inbox. Paged Settings and
workflow lists show **Page X of Y** for the current visible results.

### Decide requests in chat

Each waiting request is announced once in a chat: the task's own conversation
when the person there can decide it, otherwise the chat that receives proactive
messages such as heartbeat updates. In Telegram and Discord the announcement has
buttons: **Allow for this task**, **Allow and keep**, **Allow reading only** and
**Deny** for access, and **Approve all as proposed** and **Deny** for proposals
and changes. Pressing one decides as the person linked to that chat account.
Web Chat shows the Inbox's review card in the conversation, with the same
buttons and the full details under **Details**; it keeps showing the decision
after a reload. A staged change reads as one line, such as "Switch
TeamArchitect to deepseek/deepseek-v4.1-flash. Takes effect now.". Other
channels show a link to the Inbox. See
[Decide requests in chat](permissions.md#decide-requests-in-chat).

### Who holds what

The **Access** page next to the Inbox and the `/access` chat command show what
each agent type, scheduled job and person holds, one row per tool, for whose
work, who gave it, and until when. Access follows the work: TeamLead keeps
access for each person, workers get it for each task from TeamLead, and each
scheduled job holds its own.
Its four tabs are **By Agent Type** (starting with TeamLead's standing access
for your work), **By Person**, **By Tool** and **Files** (shares and data
folders). You can remove access, add and remove blocks, and stop sharing there,
within your own authority. See
[The Access page](permissions.md#the-access-page) and
[Access commands in chat](permissions.md#access-commands-in-chat).

Administrators review proposals for installed procedures (fixed operations
built into the runtime) in **Settings → Team → Installed procedures**; see
[Installed procedures](permissions.md#installed-procedures).

### Reviewed GitHub skill installation

Ask Team Architect to install a skill by giving it a public GitHub repository
URL and, when needed, a repository-relative path. Universal Agents searches for
`SKILL.md` below directories named `skills` anywhere in that repository; it
does not depend on `skills.sh` or a repository-specific catalog. If more than
one candidate matches, Team Architect will show the candidates and resolve a
fresh plan from the exact path you choose.

Before you decide, review the immutable commit, selected path, tree digest,
SkillSpector version, static scan score and recommendation, and every finding
in the generated scan-report artifact. Only a complete `LOW`/`SAFE` report at
or below the configured ceiling can proceed. Static `--no-llm` mode does not
claim semantic/LLM analysis; it fails closed on partial files, unexpected
disabled analyzers, failures, exclusions, malformed output, timeout, or a
score outside the safe band.

Team skills belong to the team. You approve each install, replacement or
removal in the Inbox, unless an administrator let Team Architect manage team
skills (full access to `fn:team_skills`); then it applies them itself and
administrators get a notice for each one. Either way, Universal Agents checks
the tree and report again just before it installs the managed copy, and a
modified managed tree fails its activation check.
The runtime immediately reloads TeamArchitect's AgentSkills cache and publishes
a typed team refresh notice. Each idle skills-enabled specialist verifies the
immutable installation receipt, reloads its own cache, and republishes its live
skill list. A busy specialist defers the reload until its active turn finishes.
A presence-heartbeat fingerprint check repairs that deferred refresh or a
missed live notice. The Team roster may lag until refreshed presence arrives,
but a successful install does not require a manager or system restart. If the
exact semantic name remains unavailable after the next idle presence heartbeat,
report it as a runtime defect rather than approving a restart workaround.
Installing the skill does not activate it and does not approve its tools, shell
commands, MCP calls, credentials, or future repository updates.

### State-bound supervised runtime restart

Restarting a supervised process is high risk because active work can be
interrupted. Team Architect must stage an exact `manager`, `gateway`, or `all`
plan first. The plan reports affected processes, the current task, tracked,
runnable, and waiting counts, top-level task ids/states and delegated subtasks,
plus team-visible active work for an `all` restart. Gateway session count is
not visible to the agent, so the prompt states that connected sessions may
need to reconnect rather than pretending the count is known.

After you review that state, Team Architect asks you to approve the exact
restart in the Inbox (with authorization off, it asks for your agreement in the
chat). The request lists the tasks the restart may interrupt or delay: every
task Team Architect holds, whether running, queued or waiting, and for an `all`
restart also the work other agents are running. Work an agent does on a task
Team Architect handed it counts as that task.
The next plan step or a recovery task for that request is covered too.

Your approval covers those tasks. Applying re-reads the running state first.
If some listed tasks finished or changed state in the meantime, the restart
still goes ahead. If a new task arrived that the restart would interrupt, such
as another message or a scheduled run, the restart is refused and Team
Architect must ask you again with a fresh plan that lists it. Approving a
settings change or a skill never approves a restart.

The supervisor acts only on restart requests that record their approval: your
approved request, your agreement with authorization off, or the authenticated
gateway configuration editor. Other JSON is discarded. `manager`, `gateway`,
and `all` restart supervised child process scopes; none reboots the host or
container.

Tools are not attached to agent types any more, and getting a new tool never
needs a restart: with authorization on, an agent's tools follow the access it
holds for the person it works for, and a new grant can be used on its next
step. Each call is still checked against the agent's and the person's access
and must pass the tool's normal safety or credential-broker checks.

A new-specialist plan contains bounded behavioral text and its default tools
as tool catalog ids or tool set ids only. With authorization on, the default
tools say what the new type should be given; it starts with its role's
standard tools and uses the rest once it holds access. Apply atomically creates
a validated bundle with provisioning status and rollback support; it cannot
introduce imports, installers, commands, endpoints, executable paths, headers,
environment values, or credentials. Creation does not authorize any tool.

Tools that work through the credential broker, such as X posting (`cli:x`),
take the credential from the tool's [account link](#account-links) when they
are used. If none exists, Team Architect asks for one and you choose the
credential in the broker's form. Team Architect never receives the credential
key or value. A link does not approve specialist creation or use, and the
broker checks the person, agent, operation, and link on every use.

### MCP connections and hosted tools

For a plain-language explanation of connection, installation, invocation, and
grants, read [Access and approval](permissions.md#mcp-permissions).

Static MCP configuration, trusted profiles, and agent-proposed remote servers
all use the same installed connection registry. An agent proposal is inert and
secret-free: it cannot perform DNS or connect, and it cannot contain a stdio
command, token, credential value, or executable handler. In **Settings →
Team → MCP Connections**, an authorized user can approve or reject the exact
digest-bound pending definition. Hidden buttons mean the signed-in user does
not have that authority.

Installing a server and saying who may use it is one approval. TeamArchitect
proposes a server with one call (`mcp_connections(action="propose")`); an administrator
approves the Inbox card, which installs it and lets TeamArchitect (and the
agent types it named) use all of its tools for the requesting person's work,
with TeamArchitect allowed to hand it on to workers. An administrator can also
approve a pending server in the MCP controls, which lets the agent types it
names use it for everyone's work. The secure broker then validates the
endpoint and lists and pins the exact original tool schemas. The server's
tools appear on each allowed agent's next step, with no restart. The review
names a local command or credentials from the operator's configuration when
the connection uses them.

The MCP table's **Credentials** column shows **Set by administrator** for
connections whose headers or environment come from the operator's configuration
and **Uses a linked account** for connections that declare credential slots.
Those use the stored credential linked to `mcp:<connection_name>` (the
person's own link first, then the team's while the team shares the server);
link one with `/link
mcp:<connection_name>` or `/link mcp:<connection_name> team`. The server's
tools load by themselves once a link exists. See [MCP](configuration.md#mcp).

Every call is checked against the access of the agent and of the person the
task is for (`mcp:<connection>`); access to the whole server covers all of its
tools. Every call also passes through the broker's credential, endpoint, team,
agent, and schema checks. When a call is refused, the agent is told the tool,
the level and the next step, and asks for exactly that.

The broker reuses healthy MCP sessions across requests. A transport failure or
timeout closes that session, and a later explicit call opens a fresh one. The
broker does not replay the failed tool call: a timeout does not establish whether
the server already performed the operation. For an upstream HTTP 504, inspect
the MCP server and reverse-proxy logs at the same timestamp. Increasing a client
timeout alone does not change a proxy's timeout limit.

To let a specialist type use a server, TeamArchitect holds it with handing on:
each task it gives the specialist then gets the server from TeamArchitect's access
when the task first needs it.

Team Architect can also propose a local declarative MCP server using reviewed
JSON schemas and fixed trusted behaviors. Installing and removing it are
approved changes (`hosted_mcp_servers` with `plan_install` or `plan_remove`, or the MCP controls);
installing also lets the agent types it names use its tools. Starting and
stopping it are ordinary tool calls. The broker owns the bearer-protected
sidecar and its private logs. Publishing it outside the instance is not
available.

Open **Settings → Runtime** to review MCP connections and hosted
servers alongside Objectives and schedules. Only administrators see the
approve, reject, disable, remove, install, start and stop buttons (with
authorization off, every signed-in team member does); each applies to the exact
displayed digest and lifecycle and uses the same same-origin cookie and CSRF
boundary as other trusted Web Chat controls. Connection and hosted-server
removal is recoverable; credentials, endpoint headers, broker tokens, and
sidecar logs never enter the page. The equivalent `/mcp` command lists the
current resources and available actions.

User credentials stay private. A team account link is only for an
intentionally shared service or organization credential, and is used only
while the team shares the service; the team's stored
credentials live in their own Sealbox tenant, and only a team owner or
administrator can link one. The broker checks the person, agent, operation, and
link on every use.

If the operator sets `authorization.enabled: false`, these bounded settings,
reviewed generated-specialist features, the new web API tools and data folders
of an access proposal, and account links still work.
Team Architect explains the exact change and asks for conversational agreement,
then applies an immutable agreement-backed plan. Agents do not ask for access,
and nothing waits in the Inbox.
Account links still use the broker's form for your explicit choice; ordinary
chat agreement never chooses a credential. The broker still checks the calling
process, the signed record of whom the task works for, and web API hosts;
grants are recorded but not enforced.
Operator-only settings and every normal ownership, broker, path, shell, storage,
transport, and credential guard remain enforced.

## Generated Sites

For content that benefits from a browser layout, ask the agent to create a
generated site. Sites can be articles, comparisons, procedural how-tos, or
interactive apps with browser-local progress tracking. Every type can include
an embedded chat panel that continues the task from the browser for the person
the page was made for, signed in to Web Chat. Pages are kept in `sites/` of that
person's folder.

Good requests include:

- "Create a comparison site for these two cars."
- "Turn this research into a clean reference article."
- "Create a walkthrough site for setting up my GitHub integration."
- "Make an app site for tracking my workout goals."

See [Generated Sites](generated-sites.md) for how they work.

## Durable Objectives And Replaceable Tasks

Team Architect can promote long-running, recurring, or separately tracked work
to an Objective. The Objective owns the outcome, its Plan owns the current
strategy, and each Task is one replaceable execution attempt.

To start one yourself, ask for it in plain words, for example "Make this an
ongoing objective: get my home office set up by the end of the month." Simple
requests stay ordinary tasks.

After promotion succeeds, completing or failing one Task does not discard the
Objective or its active Plan. A failed attempt may leave the Objective blocked
until Team Architect starts a fresh continuation Task; that Task resumes the
same Plan. If the strategy itself is ended, Team Architect can close that Plan
attempt and start a revised Plan under the same non-terminal Objective. You do
not need to preserve or supply internal Objective, Plan, or Task identifiers.

An Objective belongs to the person whose request created it. While Team
Architect works for you, it sees your Objectives and the team's Objectives
(ones created by work that runs for no person, such as a system timer), and
never another person's. The team's Objectives change only in work for an
administrator or in work that runs for no person.

Two of your requests can work on the same Objective at once. When one of them
changes what the Objective asks for (for example marks a success criterion
met), Team Architect's other request can't overwrite that change with a
decision made from what it read earlier: its change is refused, it reads the
Objective again and decides on what it says now. Recording progress or linking
work preserves unrelated fields and does not make that version stale. Progress
text, current focus and lifecycle status follow their own current-state rules;
the version check does not reject two changes to the same progress field.

Heartbeat items linked to an Objective follow the same ownership rules. A
shared heartbeat runs for its launcher's person and skips an Objective that
person cannot change, with a warning in the runtime log. Use a schedule owned
by the Objective's person for private recurring work. A saved work-profile
proposal also requires current authority when you apply it; an earlier
administrator role does not keep that authority after a role change.

### Runtime controls

Open **Settings → Runtime** to see authoritative state owned by
your canonical user. The page has separate Objectives, Schedules, and MCP tabs
and refreshes without treating browser or Activity Collector state as
authority.

Objective controls have distinct meanings:

- **Pause** prevents future managed runs but leaves current work alone. When
  that work finishes, the Objective stays paused until you resume it.
- **Stop** pauses future runs and asks current Objective work to halt at a safe
  boundary.
- **Resume** re-enables only links and work sessions that an Objective control
  previously disabled. A work-session setting you turned off yourself stays off.
- **Abandon** stops work and retains a terminal Objective record.
- **Delete** runs a recoverable state machine: it disables linked policies,
  schedules, and heartbeat items, stops active work, and then retains a
  `deleted` tombstone with Plans, evidence, checkpoints, and audit history.
  Only you can delete your Objective; the team lead cannot.

Each Objective shows only the controls its current status allows, and these
controls refuse any other: for example, a satisfied Objective can only be
deleted. Repeating the control that set the current status is allowed: if a
pause or abandon reports that a schedule did not switch, send the same command
in chat again (for example `/pause objective <id>`) to retry the parts that did
not finish. An unfinished deletion is retried with Delete or
`/delete objective <id>`.

Asking the team lead in chat to pause or resume an Objective also pauses or
resumes its work sessions and the schedules and heartbeat items it manages,
except schedules the system manages: the team lead leaves those as they are and
says so. Unlike these controls, the team lead can still set a finished
Objective back to active when you ask it to.

Delete and abandon require a second confirmation click. Every Web mutation is
bound to the signed-in browser, same origin, an in-memory CSRF token, canonical
user ownership, and the exact record digest shown on the page. A stale page is
rejected and must be refreshed.

The same controls are available as `/objectives`, `/pause objective <id>`,
`/resume objective <id>`, `/stop objective <id>`, `/abandon objective <id>`,
and `/delete objective <id>`. Use `/schedules` and the schedule pause, resume,
disable, and delete commands for owner-scoped scheduled work. A replacement
same-team TeamArchitect adopts eligible legacy custody for the canonical user,
so a vanished manager process cannot strand the Objective.

### Status page

Administrators and owners open **Settings → Status** to see what needs
attention now. Every finding says what it affects and what to do, and
disappears once it is fixed:

- **Problem**: something does not work now, for example a model provider
  rejected a key, an endpoint in the configuration has no key, an MCP server
  is not answering, the secure tool broker does not answer, or TeamLead
  stopped reporting.
- **Warning**: something works, but not as configured, for example a
  variable in `team.env` or the container environment that overrides a saved
  setting or a key saved with `/auth model`, memory search that stopped
  working, schedules an administrator needs to check, or people who may not
  use the team's model keys and have no model of their own yet.
- **Note**: a deliberate choice worth knowing, such as access checks turned
  off, or people whose work goes to their own model provider.

When new problems appear, administrators also get one Inbox notice that
links to the page. The page only reads; it never changes anything.

## Deterministic code and managed services

Agents can promote fixed Python or single-file Rust code into an immutable
revision. A trusted profile records the exact language, toolchain, isolation
backend, resource limits, network posture, and runtime placement. Once enabled,
a job always runs the promoted bytes. Editing source creates a new revision.

Jobs support one-shot, interval, and timezone-aware cron rules. Missed-run and
overlap behavior is explicit, and every occurrence has its own durable attempt
record. The execution controller runs this path without asking a model to
reinterpret the code.

Long-running code and managed MCP servers run as desired-state Kubernetes
services so they outlive an agent turn and controller restart. A separate data
PVC can preserve files at `UA_DATA_DIR=/ua/data`. A managed MCP service becomes
a pending MCP server that an administrator installs (in Settings or through an
access proposal); each call is still checked against access to its `mcp:` tool.

Brokered SSH sessions use only operator-registered hosts with pinned host keys.
The agent can open, read, write, resize, and close a PTY by session id, but it
cannot receive the identity key or substitute a host. A remote `tmux` name can
preserve work on the server when the local broker restarts.

Who decides, with authorization on:

- **Jobs and SSH sessions.** TeamLead needs write access to "Deterministic
  execution and SSH" (`fn:execution_control`), checked on every call. The
  execution controller checks that access again before every run and every SSH
  use, so revoking the grant stops the next run. Because a job runs later,
  outside the chat that created it, TeamLead needs access that lasts (not
  "for this task only") to turn a job on.
- **Jobs that run on the host.** A job whose profile runs code on the
  controller host (`backend: local`) runs outside any sandbox. It also needs
  lasting full access to **Unconfined host execution** (`code:unconfined`) for
  the agent type that turned it on and the person it works for, checked when
  the job is turned on and before every run.
- **Managed services and typing into SSH.** TeamLead stages the exact change
  and you (an administrator) approve it in the Inbox: creating a service,
  starting, stopping or removing it, or letting TeamLead type into one SSH
  session. Commands typed into a session run on that host, so each session
  needs its own approval.

With authorization off, TeamLead asks for your agreement in the chat instead.

## Specialist Agents

Team Architect is the permanent coordinator. It can answer directly, plan work,
or bring in specialists when the request benefits from a narrower role.

Specialists you may see:

| Specialist | Helps With | Example Request |
| --- | --- | --- |
| Web Research Agent | Current external facts, source reading, citations, and conflict resolution | "Find current docs for this provider change and summarize what changed." |
| Research & Comparison Agent | Practical comparisons, tradeoffs, recommendations, trips, services, products, and plans | "Compare these three health plans and flag the decision points." |
| Personal Admin Agent | Scheduling, paperwork, reminders, renewals, message drafts, and follow-ups | "Draft a polite reply and remind me if I have not heard back by Tuesday." |
| Home & Errands Agent | Groceries, meals, repairs, warranties, contractors, and household projects | "Make a weekend repair checklist and a shopping list." |
| Learning & Coaching Agent | Study plans, lessons, practice drills, and plain-language explanations | "Build a two-week practice plan for SQL joins." |
| Internal Improvement Agent | Runtime housekeeping, reusable skills, templates, small automations, and system health | "Create a repeatable checklist for this monthly maintenance task." |
| Memory Review Agent | Durable pinned memory, topic tags, and remembered preferences | "Remember that I prefer concise status updates for long tasks." |

The Web Research Agent and Research & Comparison Agent are separate roles. Use
Web Research when you mainly need current sourced information. Use Research &
Comparison when you need a decision-ready comparison, ranking, itinerary, or
tradeoff analysis.

### Many tasks at once

Team Architect can split a request into many tasks, for example researching
twenty products at the same time. The tasks wait in a queue for their
specialist type, and the system starts more specialists while tasks wait, up to
the team's limits. While they run you get short progress notes, such as "9 of
24 done, 4 running, 11 waiting for a free worker". If a model provider limits
how fast the team may work, the specialists wait and retry on their own instead
of asking you. Team Architect looks at the results when all tasks are done, or
earlier if one fails in a way it must decide about.

Administrators get an Inbox notice when Team Architect changes how many
specialists may run, and when a capacity or provider problem keeps coming back.

## Channels and Proactive Messages

The gateway can receive messages from CLI, Telegram, WhatsApp, Discord, Signal,
and Photon/iMessage. Each channel uses the same session model, so the agent can
preserve context per chat, topic, thread, or sender.

The configured default delivery target is used for proactive messages such as
scheduled reminders and heartbeat updates. You can ask in plain English for
simple reminders, recurring checks, and pause/resume changes.

If two of your requests change the same heartbeat item at the same time, both
changes are kept. A change Team Architect decided on from an older look at the
item (for example turning off an item whose instruction another request just
rewrote) is not made: it looks at the item again and decides on what it says
now.

If an enabled job remains overdue and unclaimed for two minutes, the runtime
queues a review for the TeamLead, including the schedule ID and required agent
type or exact worker. This covers jobs whose last worker has exited. The review
waits durably if the TeamLead is busy. The TeamLead can start the registered
agent type or investigate another repair. The original occurrence stays pending;
the review does not count as a run. Paused jobs, expired jobs, and jobs with an
active claim do not trigger this review.

See [Channels](channels/index.md) for channel setup and verification.

Owners and admins can add, list, disable, restore, and remove users, and
change their roles, with `/user`. Disabling is reversible; removing takes a
person off the team and deletes their schedules. See
[Multi-User Onboarding](newuser/index.md#disable-or-remove) for roles,
platform-specific identity formats, and restart rules.

## Voice

When voice is enabled, Telegram and Discord voice/audio messages can be
transcribed and sent to the agent. Outbound text-to-speech is controlled
separately; text replies are still sent even when audio output is disabled.

Discord voice here means audio messages and attachments, processed by the
configured speech service. The app does not join live Discord voice channels.
A missing `davey` or `PyNaCl` warning concerns that separate live-channel feature;
it does not disable audio attachments or text messages.

Voice configuration is covered in [Configuration](configuration.md).

## Skills and Help Pages

Installed skills are exposed through generated browser docs at `/docs/skills/`.
These pages help the agent and the user inspect available workflows,
provider guidance, and tool-specific procedures without rebuilding the static
docs site.

Open [Skills](skills.md) for details.

## Internal Team Work

For larger tasks, the agent may coordinate specialist runtime agents
internally. User-facing conversations should stay focused on the work result:
what was done, what needs input, what was verified, and where artifacts were
written.


When upgrading, update the gateway and any separately hosted Telegram or
Discord adapter together so review buttons work. The earlier `/permissions`
command and the **Settings → Permissions** page are removed, and review buttons
on notifications sent by earlier versions no longer do anything; decide those
requests in the Inbox. See
[Upgrading from the Permissions page](permissions.md#upgrading-from-the-permissions-page).
