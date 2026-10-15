# Multi-User Onboarding

`/user` creates and manages canonical users. The former `/newuser` token is
not an alias and is not accepted. Every person gets a stable
`usr_...` identity, and that identity owns their conversations, memory,
schedules, attachments, and Web Chat history.

All users open the same `/webchat` page. Do not create per-user URLs and do not
put a token or invitation code in a URL.

Authenticated Web Chat supports the browser-relevant `/user` forms:
`help`, `status`, `list`, `enable`, `disable`, `remove`, `role`, `token`,
`invite`, and `code`.
The signed Web Chat session determines who runs the command; a browser request
cannot supply or impersonate another canonical user.

Commands that bind an external platform identity must be run on that platform.
That includes `/user <external_user_id> "Display Name"`, `/user
--replace ...`, and `/user link <code>`. Running one of those forms in Web
Chat returns guidance instead of creating a synthetic browser-channel binding.

## What `<external_user_id>` Means

`<external_user_id>` is the stable sender identifier reported by the chat
platform. It is not the person's display name and usually is not their
username. Use the exact `newuser_target=...` value from the rejected-message
gateway log when possible.

| Platform | Example external user id | Admin command |
| --- | --- | --- |
| Telegram | `123456789` (`from_user.id`) | `/user 123456789 "Alice"` |
| Discord | `987654321012345678` (numeric user ID) | `/user 987654321012345678 "Alice"` |
| WhatsApp | `+15551234567` (normalized E.164 sender) | `/user +15551234567 "Alice"` |
| Signal | `550e8400-e29b-41d4-a716-446655440000` (`sourceUuid`) | `/user 550e8400-e29b-41d4-a716-446655440000 "Alice"` |
| Photon/iMessage | `imessage:+15551234567` (emitted sender ID) | `/user imessage:+15551234567 "Alice"` |

See the platform pages for how to find each value:
[Telegram](telegram.md), [Discord](discord.md),
[WhatsApp](whatsapp.md), [Signal](signal.md), and
[Photon/iMessage](photon.md).

## Onboarding Path 1: Chat Platform, Then Self-Service Web Chat

An already bound admin adds Alice from the same chat system Alice will use:

```text
/user 123456789 "Alice"
```

The first value is Alice's external platform ID. `"Alice"` is only her display
name. The response includes Alice's new canonical ID, for example
`usr_a1b2c3d4e5f6`.

Alice can use the chat platform immediately. If she also wants Web Chat, Alice
runs this herself from her newly registered chat identity:

```text
/user token "Alice laptop"
```

`"Alice laptop"` is a device label. It is not a username or canonical user ID.
The command always creates a token for the identity that runs it; it does not
select a user named Alice. If an admin runs this command, the token belongs to
the admin. The returned `uauser_...` token is immediately active and shown
once. Alice stores it securely, opens `https://agents.example.com/webchat`, and
pastes the token into the form. Its lifetime follows
`gateway.users.web_user_token_ttl_days`; the default `0` means it does not
expire automatically.

## Onboarding Path 2: Admin Invite for an Existing User

An admin can enroll an existing canonical user into permanent Web Chat access:

```text
/user list
/user invite usr_a1b2c3d4e5f6 --label "Alice laptop"
```

The `usr_...` value comes from `/user list`. The label describes the intended
browser or device. The response returns:

- the shared `/webchat` address;
- a single-use `uainvite_...` code;
- the invitation lifetime.

Send the address and code through a trusted channel. Alice opens `/webchat` and
pastes the code into the form. The invitation is consumed and Alice sees a
blocking **Finish Setting Up Web Chat** popup. She selects **Generate**, copies
the new `uauser_...` token, stores it somewhere safe, and selects **I've Saved
My Token**. Only that final confirmation activates the permanent token and
unlocks Chat.

## Onboarding Path 3: Web-Only User

When Alice will use only Web Chat and has no Telegram, Discord, WhatsApp,
Signal, or Photon identity, an admin creates the canonical user and initial
invite together. The admin may run this directly in authenticated Web Chat:

```text
/user invite --new "Alice" --label "Browser Onboarding"
```

`"Alice"` is the display name. `"Browser Onboarding"` is the invitation label.
The new user receives a `usr_...` identity and active team membership but no
channel binding. Give Alice the returned `/webchat` address and one-time
`uainvite_...` code. Alice follows the permanent-token popup described above.
The generated token inherits the `"Browser Onboarding"` label.

Alice is a durable team member as soon as the command succeeds. The invitation
is only an enrollment credential; the generated `uauser_...` token is her
reusable Web Chat credential.

## Settings When Adding Someone

Both ways of adding a person (`/user <external_user_id> "Name"` and
`/user invite --new "Name"`) take these options:

- `--team-keys` or `--own-keys`: whether their work may use the team's model
  keys. With `--own-keys`, agents use only the model they set up themselves
  with `/auth model me`, and their requests do not run until they do. Without
  either, the team's model-key sharing decides: shared (the default) lets them
  use the team's keys, private does not. Owners and administrators change it
  with `/accounts share model` or `/accounts private model` (see
  [Shared or private](../permissions.md#shared-or-private)); it applies only to
  people added afterwards. Change one person later with
  `/user team-keys <usr_...> on|off` (owners and administrators only).
- `--shared <name>`: a shared working folder `team/<name>` for their work,
  for example `--shared research-notes`. Everyone also has
  `team/team-shared`. Folders under `team/` are open to the whole team: their
  agents, your agents and the team's own agents can all read and write them.
  Use a person's own folder (`users/...`) for anything private.

The reply ends with a report of how the person is set up:

```text
Settings for Alice (usr_a1b2c3d4e5f6):
- Role: member
- Added through: telegram identity 123456789
- Timezone: America/Chicago
- May use the team's model keys: no (change with /user team-keys usr_a1b2c3d4e5f6 on)
- Own model: not set up yet. Their requests do not run until they set it up with /auth model me
- Personal folder: users/usr_a1b2c3d4e5f6
- Shared folders: team/team-shared, team/research-notes (open to the whole team: ...)
```

`/user status` shows the same model and folder lines for the person who runs
it. See [Model Keys](../model-providers.md#model-keys) for how people set up
their own model.

## First-Time Setup

When the team keeps something private, the person you add brings their own.
The report above ends with what they still have to set up, for example:

```text
- Still to set up: their own model (needed) and optionally their own GitHub (gh), X (xurl) (/setup lists them)
- First message: they are shown what is still to set up for themselves.
```

The **First message** line appears only when the first-time setup was
recorded for them; a person who is restored or added again gets none. The
person sees that list on their first message:

- **Their own model**, when they may not use the team's model keys and have
  not saved one. It comes first, because their requests cannot run without
  it. Their first message is held back and the list is the reply; they send
  `/auth model me`, keep **Use this as my model** ticked, and send their
  message again.
- **Their own account for each private service someone in the team uses**:
  every service the team keeps private
  ([Shared or private](../permissions.md#shared-or-private)), GitHub and
  Google included, that the team or another person already has an account
  for. A service nobody in the team uses is left out; `/link <tool_id>` still
  works for it, and a task that needs it says so. These are optional. Each line names the command
  that opens the secure page, such as `/link http:exa` or `/link cli:github`,
  and how the page ended comes back in the same chat.

When the model is not needed, the list arrives as a separate message that
says their message is on its way, and their first message goes to the team as
usual. When there is nothing to list, they see no list at all. The list is shown once; `/setup`
shows what is still left at any time. Later messages are never held: if the
model is still missing, the reply says so and names `/setup`. People who were
in the team before this existed are not shown a first-time setup, but `/setup`
works for them too.

## Returning to Web Chat

The browser uses a 12-hour HTTP-only session cookie. Alice does not need to
re-enter her token while that session remains valid. After logout, session
expiry, a gateway restart, or moving to another browser, she opens `/webchat`
and enters her saved `uauser_...` token. Her existing owner-scoped conversations
and gateway-owned Web Chat transcripts return after authentication.

If Alice refreshes after generating a token but before confirming that she
saved it, the raw token cannot be shown again. The popup offers **Generate
Replacement**; the abandoned pending token is revoked and never becomes usable.
If she loses an already activated token, an admin can issue another enrollment
invitation for her existing canonical identity:

```text
/user invite usr_a1b2c3d4e5f6 --label "Replacement browser token"
```

## Onboarding Path 4: Link the Same Person to Another Platform

Link codes attach an additional platform identity to an existing canonical
user. They do not create a second person.

Alice runs this from a channel already linked to her account or from her
authenticated Web Chat session:

```text
/user code
```

The gateway returns a value similar to:

```text
Link code for usr_a1b2c3d4e5f6: AbCdEf1234
Expires at UTC: 2026-07-30T18:30:00+00:00
```

`AbCdEf1234` is a short-lived, one-time proof that the new platform identity
should belong to Alice's existing `usr_...` user. It is stored only as a hash,
expires after `gateway.users.link_code_ttl_seconds` (15 minutes by default),
and is consumed by the first successful link. It is not a Web Chat token and
cannot authenticate at `/webchat`.

Alice then sends the returned code from her own identity on the other platform.
This second command cannot be run in Web Chat because the destination
platform's sender identity is what must be bound:

```text
/user link AbCdEf1234
```

The gateway binds the sender ID supplied by that second platform to the same
canonical user that created the code. Alice's conversations from both
platforms therefore belong to Alice while remaining isolated from every other
team member.

The person who owns the existing identity must generate the code. Do not use
an admin's own link code to onboard a new person; it would attach the new
platform identity to the admin's canonical user. If the destination identity
is already linked, `/user link AbCdEf1234 --replace` is required to rebind
it.

## Roles

Every person has one role in the team:

| Role | Who | What it allows |
| --- | --- | --- |
| `owner` | Whoever set up the instance; there can be more than one | Everything an administrator can do, and making someone an owner or changing an owner's role |
| `admin` | Team administrators | Adding, disabling, removing, and inviting people, changing roles other than owner, and giving, removing, and blocking agents' access to the team's tools |
| `member` | Everyone else | Their own conversations, memory, schedules, and logins |

The instance's user (`user.canonical_user_id` in `instance_config.yaml`)
becomes the first owner when the gateway creates their membership. Owners and
administrators change a role with `/user role`:

```text
/user role usr_a1b2c3d4e5f6 admin
/user role usr_a1b2c3d4e5f6 member
/user role usr_a1b2c3d4e5f6 owner
```

- Only an owner makes someone an owner or changes an owner's role.
- The team always keeps at least one owner and at least one administrator; a
  change that would remove the last one is refused.
- The new role applies from the person's next action, because roles are read
  again at every access decision.
- Every change is recorded with who made it (`team_role_change` in
  `gateway_user_events`).

**Instances set up before owners existed.** The gateway does not change an
existing membership, so the instance's user stays an `admin`. While a team
has no owner, an administrator may name one, for example themselves:
`/user role <your usr_... id> owner`.

## Administration and Verification

Run these from an already bound owner or admin identity, or an authenticated
owner or admin Web Chat session:

```text
/user status
/user list
/user list active
/user list disabled
/user list removed
/user disable usr_a1b2c3d4e5f6
/user enable usr_a1b2c3d4e5f6
/user remove usr_a1b2c3d4e5f6
/user role usr_a1b2c3d4e5f6 admin
```

`/user list` shows canonical IDs, where each person stands in the team
(`active`, `disabled`, `removed from the team`, or `not a member of this
team`), memberships with their roles, bound channel identities, default
notification targets, and last-seen times. `/user list active`, `disabled`,
and `removed` show only the people who stand that way. User-facing times are
converted from canonical UTC storage into the authenticated requester's
configured IANA timezone and rendered with a readable date, clock time, and
timezone abbreviation.

### Disable or remove

**Disable** is for a pause you intend to undo:

- It immediately prevents new chat-platform and Web Chat requests, including
  requests from an existing browser session.
- Their running and queued work is cancelled, their objectives stop (they are
  kept, paused), their schedules are deleted, and access requests waiting for
  them or made for their work are withdrawn.
- The person stays on the team. Their folder, memory, conversations, access
  grants, linked accounts, their GitHub, Google and X sign-ins and channel
  bindings are kept.
- `/user enable` restores access. The person must sign in to Web Chat again,
  and their own memory review and knowledge review schedules are installed
  again; the schedules deleted
  when they were disabled do not come back.
- Only an owner can disable or enable an owner, and nobody can disable
  themselves.

**Remove** is for someone who left:

- Everything that disabling does happens, and they are taken off the team:
  they can no longer use it from any chat app or Web Chat.
- Deleted at once: their task records (running, stopped, paused and
  finished) and objectives, their chat sessions and Web Chat history, their
  sign-ins, the access grants for their work and the access grants they gave
  (including what they shared from their folder), their personal account
  links and the credentials stored under them, their own GitHub, Google and X
  sign-ins and the broker's private credentials folder for them, their
  requests and notices, their starter-profile choice, their memory-review
  jobs and their personal OpenShell sandbox. The reply names the sign-ins
  that were deleted. The team's credentials and sign-ins are not touched.
- Held, then deleted: their private folder (including their Python
  environments), their personal memory, their own knowledge pages and their conversations move out of
  every agent's reach for `gateway.users.removed_data_retention_days` (7 days
  by default). The reply to `/user remove` says until when. After that time a
  maintenance run deletes them.
- Kept: audit records (`gateway_user_events` and the access event log), what
  they put in the shared `team/` and `agents/` folders, and files they sent to
  other people.
- The runtime's own team schedules (for example the maintenance schedule and
  the team memory audit) are never deleted with a person. When their owner is
  disabled or removed they move to another active administrator, and
  administrators get an Inbox notice. They continue in new conversations, as
  do the heartbeat checks: the earlier conversation history stays with the
  person who left and comes back with them if they are restored.
  If preparing the heartbeat checks for their new owner fails, they stay
  stopped until the cleanup succeeds on a retry.
- Only an owner can remove an owner, nobody can remove themselves, and the
  team always keeps at least one owner and one administrator.
- Running `/user disable` or `/user remove` again for the same person checks
  their work and records again; use it when the reply says something could
  not be done.

**Restore** brings a removed person back with their held data:
`/user restore usr_...` (owners and administrators) adds them back as a
member, moves their folder, memory and conversations back, and installs their
memory-review schedules. Their tasks, schedules and objectives do not come
back, and neither do their stored credentials and sign-ins, which were
deleted at once: they sign in again with `/link`. After the hold ends,
`/user restore` explains that the data is gone.

`/user enable` does not apply to a removed person. Adding them back the usual
way, `/user <external_user_id> "Display Name"` from the chat app they used or
`/user invite usr_...` for Web Chat, makes them the same person with the
`member` role again (and enabled, if they had also been disabled). While their
data is still held, it comes back as with `/user restore`; after that, they
start fresh.

## Web Chat Security and Separation

- A reusable `uauser_...` token and a one-time `uainvite_...` code are secrets.
  Neither is stored in browser local storage or placed in a URL.
- Invitation codes are stored only as hashes, expire after
  `gateway.users.web_user_invite_ttl_seconds` (15 minutes by default), and can
  be used once.
- Permanent tokens are also stored only as hashes and are shown once. During
  the popup they remain `pending` and cannot authenticate until the user
  confirms that the token was saved.
- Pending tokens expire with the enrollment session. Generating a replacement
  revokes the prior pending token.
- Permanent tokens do not expire by default. Operators can set
  `gateway.users.web_user_token_ttl_days` to impose an expiration policy.
- The browser uses an HTTP-only, same-site session cookie. It is marked
  `Secure` when `network.public_origin` uses HTTPS.
- Server sessions, gateway transcript rows, and attachment references are
  scoped by canonical user. Browser storage contains only disposable UI state,
  not the authoritative message list. Logging out and signing in as another
  user switches to that user's isolated history.
- Each person's files live in their own folder under `workspaces/users/`.
  Agents working for one person, and the shell and Python commands they run,
  cannot open another person's folder, and nobody can give access to it. In
  OpenShell mode each person's code runs in their own sandbox. See
  [Files and folders](../permissions.md#files-and-folders).

## Storage and Restart Behavior

Users are durable gateway state, not static `instance_config.yaml` entries. The
gateway stores:

- `gateway_users`
- `gateway_user_channel_bindings`
- `gateway_user_team_memberships`
- `gateway_user_web_tokens`
- `gateway_user_web_invites`
- `gateway_user_link_codes`
- `gateway_user_events`
- `gateway_transcript_streams`
- `gateway_transcript_session_sequences`
- `gateway_transcript_messages`

Direct-user registration, token creation, invitations, role changes,
disabling, and enabling take effect immediately. No restart is required. Static adapter changes—such
as enabling a platform, changing credentials, or changing a group/channel
allowlist—still require config reload or restart.
