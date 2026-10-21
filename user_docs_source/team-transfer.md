# Moving a team to a new version

Export the old instance's setup, then import it into a fresh instance of the
new version. The simplest way is onboarding's **Import a team export** choice:
copy the export into the new data folder, open it in onboarding, choose what
to bring, and the team starts
([Import during onboarding](#import-during-onboarding)). Host commands do the
same and can also bring more records from the same backup later
([Export and review](#export-and-review)). Neither changes the signed export.

## What moves

- **Settings** you changed in `instance_config.yaml`, editable individually in
  YAML. Unchanged settings use the new release's defaults.
- **Control files**: `SOUL.md`, chosen model overrides, the roster, templates,
  agent definitions, specialist bundles, and Objective profiles.
- **Skills and tools**: reviewed skills, added web API and CLI tools with their
  programs, MCP connections, and installed procedures.
- **People and access**: roles, channel bindings, Web Chat sign-ins, lasting
  grants, blocks (not TeamLead's restrictions for one request, which end with
  their request), shares, data folders, account links, person settings, and
  the team's choices of which services' accounts and model keys are shared
  ([Shared or private](permissions.md#shared-or-private)). Disabled people
  stay disabled. Removed people do not move.
- **Schedules and work setup**: timing, instructions, enabled state, delivery
  routes, heartbeats, worker policies, pool limits, objectives and their
  policies. Each schedule has editable Markdown containing fenced YAML.
- **Chat sessions**: the sessions schedules deliver through and each person's
  current session in each chat, with their names but not their conversations.
  After the move, a person's next message continues in the session that was
  current in the old instance, even when an older session of the same chat
  moves too.
- **Pinned memories**, selected and editable one fact at a time.
- **Knowledge pages**: the team's pages and each imported person's own pages,
  with every revision, notes, links, the names of merged pages and pending
  contributions. Pages of people who are not imported stay behind. Selected
  notes, revisions, links and contributions that change a page need that page
  selected or already present. Contributions proposing a new page need no
  existing page. Later link selections must still appear in the destination
  page's current text; validation refuses links removed by destination edits.
- **Credentials**: operator secrets from `team.env`, model keys, stored
  sign-ins, catalog entries, and password-manager sessions. The page shows
  names and owners, never secret values.

Agent-type grants and blocks keep their `<team>/<type>` IDs. Import checks the
type against this release's agents and the specialist definitions selected for
import. Unknown types, bare type names, and another team's subjects are refused.
The team lead's access for one person remains limited to that person's work.

Conversation and task history, the learned memory graph, workspace files, audit
logs, queued tasks, and waiting approvals do not move. Automation jobs and managed
services are listed for manual setup rather than imported. Missed one-time runs
are imported turned off; recurring schedules resume counting from import.

## Import during onboarding

1. **Export the old team.** Run `team export` on the old instance as shown in
   [Export and review](#export-and-review), then stop the old instance.
2. **Start the new version with an empty data folder.** Onboarding opens, as for
   a new team. Import works only on a fresh data folder and, for PostgreSQL, an
   empty database named by `POSTGRES_DSN` in the container's environment.
3. **Copy the export into the new data folder's `import/` folder**, for example
   `/srv/new-team/import/team.uatransfer` when `/srv/new-team` is mounted as
   `/data`. Export files can be large, so there is no browser upload.
4. **On Let's Begin, choose Import a team export.** Choose the file, type its
   passphrase and select **Open export**. A wrong passphrase is refused; try
   again.
5. **Choose what to import.** Records are grouped by kind and shown by name:
   people, schedules, memories, knowledge pages and so on. Everything starts
   selected; unselect what you don't want. Settings, memories and schedules can
   be edited as in the host-command review below. Open each selected schedule,
   check it, and confirm it.
6. **Choose where secrets come from.** It starts with the old instance's choice.
   Turn on **Secrets come from the environment** when every secret reaches the
   container as an environment variable or Docker secret, as in Docker Swarm.
7. **Validate, then select Import and start the team.** Onboarding writes the
   instance and the container starts the team. The page opens Web Chat when it
   is ready.

The copied export and the review's private files are deleted after the import;
the original stays where you exported it. Stored sign-ins and keys are imported
by the new instance's broker when the team first starts. Each person whose
sign-ins moved gets an Inbox notice, and the owner who exported the team gets
one listing any the broker did not store. Until then they wait, still encrypted,
in a private file under `state/`; the passphrase is never saved. The broker
restores them before agents and Web Chat start. Temporary storage failures are
retried during startup. If the broker cannot be reached or those failures
persist, startup stops and keeps the encrypted handover for the next start;
`python -m assistant_cli team finish-import --config /data/instance_config.yaml`
runs the same step by hand.

An import is marked complete only after this handover has been saved. If its
write fails, the instance stays unfinished and cannot start. Use a fresh data
folder and empty databases to repeat that first import. A failure opening an
export leaves completed review edits intact and can be retried.

When secrets come from the environment, the import lists the secrets the
environment is missing, including those the old `team.env` held. Add them as
Docker secrets and restart the container; the team then starts.

## Export and review

Choose a passphrase of at least 12 characters and an active owner's person id
(`/user list` shows ids). Keep the passphrase and export private. Run these
operator commands with the release that owns each instance; agents cannot run
them.

```bash
.venv/bin/python -m assistant_cli team export \
  --config <old-root>/instance_config.yaml --owner <owner-person-id> \
  --out team.uatransfer
```

The command prompts for the passphrase twice. `--passphrase-file <file>` reads
it from a private file instead; the passphrase is never a command-line value.
`--skip-credentials` omits broker credentials if the old broker cannot run.
Stop the old instance immediately after export: bot tokens and rotating sign-ins
must not serve two running instances.

The first import needs a fresh root without existing configuration, control
files, or databases. For PostgreSQL, create empty destination databases and set
`POSTGRES_DSN` or `POSTGRES_DSN_FILE` in the destination environment or planned
`team.env`. `--env <file>` loads a destination environment file. All import
writers and the next launch resolve those settings the same way. The first import
checks each effective destination database, including the bundled PostgreSQL
server and a separate activity database, and refuses any that already hold
tables. Pick database locations
and the secret source in the first batch; later batches cannot move existing
data or keys to another location.

Open the browser review using the new release:

```bash
.venv/bin/python -m assistant_cli team review \
  --in team.uatransfer --instance-root <new-root> --review-dir <private-review-folder>
```

The command opens a private loopback page and prints its URL. Keep the command
running while reviewing; Ctrl+C closes it. Run it on the destination host. For
a remote host, forward the printed port through SSH and open the same loopback
URL locally, preserving its token fragment. This is an operator page, separate
from the gateway.

1. Filter and select individual records. Include at least one active owner in
   the first batch. Dependencies must be selected or already present, such as a
   schedule's person, chat session, objective, delivery target, worker binding,
   and worker definition.
2. Open each selected schedule. Check timing, timezone, instructions, enabled
   state, owner, and delivery route. Edit and save its Markdown. The one fenced
   YAML block is the imported configuration; surrounding prose is review notes.
   IDs, ownership, routes, and approval provenance cannot be changed this way.
   Leave a wrong-owner or wrong-route schedule unselected and recreate it using
   the normal schedule controls.
   The page shows the backup's remaining run count. Keep `repeat_count` unchanged
   to preserve that progress; changing it sets the number of runs remaining after
   import (`null` means unlimited). Worker setup must match the selected worker
   type. Edited worker instructions may need the normal worker review after startup.
3. Confirm each selected schedule after its final edit. Editing again clears
   confirmation. Settings and memories are editable too, in the page or with
   your own text editor.
4. Choose **Validate selected records**. Review attention notes; correct or
   unselect refused records. Validation reads destination stores without
   initializing or repairing them. Blank memories and schedules that cannot
   produce a next run are refused before anything is written. Invalid YAML
   reports its line and column so you can correct the saved edit and retry.
5. Choose **Import selected records**. The server checks the exact saved review
   you validated. Noncredential batches require a stopped destination runtime.
   Pending saves and unsaved edits keep import disabled. If you edit during
   validation, save the current text and validate again.

The private review folder contains `review.json`, schedule Markdown in
`schedules/`, setting YAML in `settings/`, and memory text in `memories/`. It is
bound to this exact signed backup and destination root. It can contain private
instructions and memories, but no decrypted broker credentials.

## Credentials and later batches

Start the new instance after the first noncredential batch. Stored credentials
go through the running broker. Reopen the page, select a credential batch, then
validate and import. Unselect pending noncredential records for a broker-only
batch while the runtime runs. If credentials were attempted before the broker
started, the page reports that they were not stored; start it and retry them.
Each person whose credentials move receives an Inbox notice.

Keep the original backup and review folder until all wanted records have moved.
Reopen `team review` with the same arguments to select more records. Later
batches require the exact backup used for the first batch. Already imported
records are skipped, preserving destination edits. Existing records with the
same identity, including credentials stored after startup, are kept too.
A chat that already continues in a session in the new instance stays in it.
If a later batch brings the session that was that chat's current one in the
old instance, the session is imported and marked for attention, and people's
next messages still go to the session they use now.
Distinct credential and field names keep separate secret values, including
names that differ only in punctuation. Importing a catalog credential does
not overwrite a secret used by another existing catalog entry.
If storing a catalog credential fails, retry the same backup. The broker reuses
that record's stored secret items and saves its complete catalog entry together,
so a partial attempt does not block the retry or create duplicate secret items.
Account links belong to their owner and tool, so an older link cannot replace a
newer destination sign-in even when their link IDs differ. An access decision
whose result is still being confirmed must be reconciled before export. Start
the gateway and let the Inbox check it after the ten-minute claim period,
then stop the runtime and export again. Each tool has one
link per owner; if a backup holds two for the same tool and owner, the review
refuses the older one and names the newer one, which was the one in use. A person's chat
account already linked to someone in the destination is refused before writing,
including accounts retained after that person leaves the team.

Stop the destination before adding more settings, people, schedules, or memories.
Back up its root and databases before each later batch. Receipts and reports
are stored in `runtime/team-import/`. A failed noncredential import leaves a
marker that blocks every runtime entry point. For a failed first import, use a
fresh root and empty databases. For a failed later batch, restore the pre-batch
root and database backups before retrying. Removing the marker does not repair
partially written records.

## CLI and Docker

The CLI also applies saved reviews. Writing imports now require `--review`;
an unreviewed `--dry-run` remains available.

```bash
.venv/bin/python -m assistant_cli team import --in team.uatransfer \
  --instance-root <new-root> --review <private-review-folder> --dry-run
.venv/bin/python -m assistant_cli team import --in team.uatransfer \
  --instance-root <new-root> --review <private-review-folder>
.venv/bin/python -m assistant_cli team import-credentials --in team.uatransfer \
  --config <new-root>/instance_config.yaml --review <private-review-folder>
```

`team inspect --in team.uatransfer` verifies the archive and lists its sections.
Delete private transfer and review files when the move is complete.

With Docker, export and import credentials inside the matching running container:

```bash
docker exec -it <old-container> python -m assistant_cli team export \
  --config /data/instance_config.yaml --owner <owner-person-id> --out /data/team.uatransfer
docker exec -it <new-container> python -m assistant_cli team import-credentials \
  --in /data/team.uatransfer --config /data/instance_config.yaml --review /review
```

For browser review on Linux, use a disposable container with host networking.
Mount the destination and review folders at the **same absolute paths** used by
the normal instance, because those paths are saved. For example:

```bash
docker run --rm -it --network host --entrypoint python \
  -v /srv/new-team:/srv/new-team -v /srv/team-review:/srv/team-review \
  <new-image> -m assistant_cli team review --in /srv/new-team/team.uatransfer \
  --instance-root /srv/new-team --review-dir /srv/team-review/review
```

The example's review subfolder must initially be absent. Docker Desktop does
not expose container loopback this way; use the new release's host installation
to review with access to the same destination files.

## Which versions can read a file

Each section has its own version. A release reads its own section versions
and the ones the release just before it exported, converting the sections
that changed. No release reads an older database directly.

A file from an older release is refused before anything is written. The
message names the release that exported the file. Import it into a newer
release that can read it and export the team again there. Repeat with newer
releases until this release can read the file. Use the release paths below;
one intermediate export may still be too old for this release.

This release reads files exported by v0.9.1 and v0.9.2. Files exported by
v0.8.5 or v0.9.0 are read by v0.9.2: import such a file into v0.9.2, export
the team again there, and import that file here. Later batches from the older
file also need v0.9.2, so finish them before you upgrade.

Files exported by v0.8.1, v0.8.2 or v0.8.3 are read only by v0.8.4: those
releases export the same section versions.

These files have no credential index. After the first noncredential import
into v0.8.4, start its broker, then open `team review` with a **new
`--review-dir` folder**. Select and import the stored credentials before
exporting again. Reopening the first review folder does not discover them.

Files exported by v0.8.4, the last release before shared and private
accounts, are read only by v0.8.5. Import such a file into v0.8.5, export the
team again there, and continue with that file through v0.9.2 as above. Later
batches from a v0.8.4 file also need v0.8.5, so finish them before you upgrade. v0.8.5 keeps how the old team
treated people added later (they use the team's model keys only if
`gateway.users.new_people_use_team_model_keys` was on), and its export carries
that choice as the team's model-key sharing.

v0.8.4 and v0.8.5 refuse agent-type grants and blocks. Unselect refused
records in review, then recreate them with that release's access commands
before starting each intermediate instance. Enabled imported schedules can
otherwise run without their original blocks. The steps below describe how
to restore and check that access.

When a release renames tool operations, review and import convert the blocks
that name them. Backups made before the action tools were merged still need
those renames.
A block on one memory bank's old function covers that action for both banks.
A block naming an operation this release does not have is listed for attention,
including operations of accepted team-added CLIs, web APIs and active MCP
servers. Review checks their imported definitions before writing the new
instance. Such a block stops the whole tool until an administrator replaces it
(see [Blocking access](permissions.md#blocking-access)).

## Instances older than v0.8.1

v0.8.0 and earlier releases have no team export. Choose one of two ways.

**Start fresh and keep pinned memories.** Onboard a new instance of this
release on its own data folder. Enter model keys and chat bot tokens in
onboarding rather than uploading the old `team.env`, which would bring back
the old internal keys and turn model keys into overrides. Move pinned
memories with `memory export` and `memory import`
([Moving memories to a new instance](configuration.md#moving-memories-to-a-new-instance)).
People sign in to their accounts again with `/link`, and administrators link
the team's accounts with `/link <tool> team`. Settings, access, schedules and
added tools stay behind.

**Move everything through v0.8.1.** Each step needs the release named in it.

1. Back up the instance root and its databases, then start the instance once
   on v0.8.1. The Docker start converts `instance_config.yaml` to the v0.8.1
   format; check with
   `python -m assistant_cli migrate-config --config <root>/instance_config.yaml --yes`,
   which also converts it when needed. The v0.8.1 export copies the file as
   saved, so an unconverted file brings old settings the next release refuses.
2. Save the output of `python -m assistant_cli access list --config <root>/instance_config.yaml`.
   v0.8.4 and v0.8.5 refuse grants and blocks given to an agent type, such as
   the team lead's access for one person or a block on a worker type. Unselect
   them in each intermediate review. After the noncredential imports at each
   hop, recreate them with that release's `access grant` and `access block`
   commands and check them with `access list` **before starting the instance**.
   Preserve whose work each grant serves, its level, expiry and delegation
   settings, and each block's subject and target. Imported recurring
   schedules remain enabled; without their blocks, they can perform
   previously blocked actions as soon as the intermediate instance starts.
3. Export on v0.8.1 and stop the old instance.
4. Import all wanted noncredential records into a fresh v0.8.4 instance and
   restore the access recorded in step 2 before starting it. v0.8.3 renamed the
   default assistant from Assistant to Lisa. To keep the old name, edit the
   imported v0.8.4 `instance_config.yaml` and set `assistant.display_name`
   to `Assistant` before the next export. An unchanged old default is absent
   from the export, so the review has no setting for it.
5. Start the v0.8.4 instance. Open `team review` with the same backup and
   destination but a **new `--review-dir` folder** to discover its stored
   credentials. Select only the credential batch, validate and import it.
   Finish all wanted batches, then export on v0.8.4 and stop that instance.
6. Import all wanted noncredential records from that file into a fresh v0.8.5
   instance. Again, unselect refused agent-type grants and blocks in review
   and recreate them before startup.
   Start it, finish its credential import and all wanted batches, then
   export on v0.8.5 and stop it.
7. Import the v0.8.5 file into a fresh v0.9.2 instance. Select the restored
   agent-type grants and blocks; v0.9.2 accepts them. Check the access list
   against the saved one before startup. Start it, finish its credential
   import and all wanted batches, then export on v0.9.2 and stop it.
8. Import the v0.9.2 file into this release, check the access list again
   before startup, then finish the credential import as described above.

For moving only pinned memories, see
[Moving memories to a new instance](configuration.md#moving-memories-to-a-new-instance).
