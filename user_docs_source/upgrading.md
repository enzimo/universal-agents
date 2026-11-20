# Upgrading

Install the new version over the old one. Your settings, people, access,
memory, knowledge, schedules and history stay where they are.

## Which versions upgrade in place

A release upgrades an instance in place from earlier releases of its own
minor version and of the previous one: 0.10.x opens what 0.9.4 and later
saved. Upgrades only go forward. Going back to an earlier release is not
supported, because it may not read what a newer one saved.

- **From 0.9.3 or earlier, or 0.8 or earlier:** move the team to a fresh
  instance with the [team transfer](team-transfer.md). Upgrades from these
  releases are not checked, and 0.9 itself moved teams only that way.
- **From 0.9.6 or earlier:** approve or decline the schedule changes waiting
  in the Inbox first. A waiting schedule change saved by those releases
  cannot be read after the upgrade.
- **From 0.9.11 or earlier:** first approve or decline a restart waiting in
  the Inbox, or, with authorization off, waiting for your agreement in the
  chat. A restart planned by those releases cannot be approved after the
  upgrade: approving it then only says it was planned by another version, and
  Team Architect has to plan it again and ask you again.
- **From 0.9 with a chat app the adapter host runs:** this applies only to
  chat apps set to `execution: "adapter_host"` under `interactive_apps`. Chat
  apps the gateway runs itself (`execution: "in_process"`, the default) and
  Web Chat need nothing. 0.9 kept the files people sent through such a chat
  app in the adapter host's transfer folder
  (`interactive_apps.adapter_host.upload_dir`, by default
  `runtime/adapter-host-uploads`); 0.10 keeps every file in `state/uploads`.
  At its first start, before the gateway and the agents start, 0.10 moves
  the files that conversations still list into `state/uploads`, so the team
  can open them later in the same conversation as before. It does this once,
  logs the result with counts (`event=adapter_host_files_upgraded`) and
  records that it finished in `state/upgrades/adapter-host-files.json`.
  - **Let requests that use those files finish first.** A request still
    running during the upgrade cannot open its files afterwards. The chat it
    came from is told which files to send again, and the person it works for
    also gets the notice in the Inbox. On a team with gateway users off
    nobody is verified, so the chat gets the message and administrators get
    an Inbox notice with the number of files.
  - **Moved files keep the date they were sent.** Upload retention counts
    from that date, so files older than `retention_days`
    ([Upload retention](configuration.md#upload-retention)) are deleted at
    the gateway's first cleanup after the upgrade.
  - **A file that is no longer there** cannot be moved. Its records are
    removed, and when the person refers to it the team asks them to send it
    again.
  - **A file it refuses** (a link, contents that differ from what was
    received, a file it cannot read) stays where it is and keeps its
    records, but the team cannot open it. The log names it
    (`event=adapter_host_file_not_moved`; its `sources=` field names the
    file in the transfer folder). Fix the file, delete
    `state/upgrades/adapter-host-files.json` and restart to try again.
  - **A file with no record of the chat that sent it** (its gateway record
    was removed, for example by a restore on 0.9) cannot be moved, and
    trying again does not help. Its conversation records are kept while
    the file is there, the team cannot open it, and the person can send it
    again. The log counts these as `no_record`.
  - **Files no record names** stay in the transfer folder: files of people
    removed on 0.9, files whose records were deleted, or files of messages
    the gateway never handled. The team cannot open them. The upgrade finds
    them once, when it finishes. A warning
    (`event=adapter_host_files_without_records`) gives the folder, the
    number and the first ten names, and the full list is in
    `state/upgrades/adapter-host-files.without-records.txt`, one name per
    line. The list leaves out files of messages the gateway took in but had
    not handled yet: at its start 0.10 handles those messages, 0.9's
    included, reads their files and then removes them. When records name
    any of these files in another folder, the upgrade gives no list and no
    command at all (see "The folder was at another path under 0.9" below).
    Unless the folder was at another path under 0.9 (see below: then keep
    every file), you may delete exactly the listed files, but only after
    the gateway has started on 0.10 since
    `state/upgrades/adapter-host-files.json` (the
    upgrade's marker) was written, and only while the instance is stopped.
    The warning first says when not to delete, then gives the command. The
    command deletes nothing, and says so, unless the marker is the one
    written with the list: the upgrade stores an id for the list in the
    marker and the command checks for it, so a command from an earlier
    warning deletes nothing after a later run of the upgrade. The list
    never names a file in the `sources=` field of
    an `adapter_host_file_not_moved` warning, so you take nothing out of it;
    keep those files, because they can still be moved once you fix them, as
    above. (Its `file=` field may name the copy in `state/uploads` that
    stood in the way instead.) Wait for the gateway because the 0.10
    adapter host passes new files through the same folder under the same
    names, so a list taken while the team runs (when you run
    `assistant_cli upgrade adapter-host-files` yourself, with or without
    `--files-gone`) may name them: after such a run, restart the instance
    once before deleting. Before you stop the instance to delete, let the
    team run a few minutes after the gateway's start, so messages a remote
    adapter host is still sending arrive. If the adapter host is turned
    off, keep the files until it has run again.
  - **Deleting the listed files in a container.** The paths in the warning
    are the container's (`/data/…`), and the instance must be stopped, so
    `docker exec` cannot run the command, and on the host the command finds
    no marker at that path and deletes nothing. Either run it in a one-off
    container that mounts the instance's folder at `/data` and paste the
    command there, or replace `/data` in all three of its paths with the
    folder the container mounts there. For the installer, that folder is
    the one it keeps data in (by default `~/.universal-agents/data`, or the
    one you gave with `--data-dir`):
    `docker run --rm -it -v ~/.universal-agents/data:/data --entrypoint sh <image>`,
    with your folder in place of the default.
  - **Names the upgrade does not show.** A file whose name is not one the
    gateway gives (`adapter_upload_` and 12 lowercase hex digits) is counted
    (`unlisted=` in the warning) but not listed or shown: something else put
    it there, and its name may be text someone chose. The upgrade gives no
    advice for such files.
  - **The list could not be written**
    (`event=adapter_host_files_without_records_list_failed`): the warning
    names at most ten files and gives no command. Only the files it names
    may go, under the same conditions. Fix `state/upgrades`, delete
    `state/upgrades/adapter-host-files.json` and restart to list them all.
  - **The folder was at another path under 0.9** (moved, mounted elsewhere
    or reached through another link): its records name that path, so every
    file here looks like one no record names. The upgrade notices this when
    a record names one of these files in another folder. It then lists
    nothing, gives no delete command (a command from an earlier warning
    deletes nothing either) and logs one error instead
    (`event=adapter_host_files_folder_moved`): how many files records name
    in another folder, the folders it may show (from gateway records and
    held guidance, never from conversation records, which agents write) and
    what to do. Do not delete any of the files. They were not moved, so the
    team cannot open them. The upgrade moves a file only with its gateway
    record. For the files a gateway record names, move or mount the folder
    back at the path the error names (a link does not work), set
    `upload_dir` to that path, delete
    `state/upgrades/adapter-host-files.json` and restart; that start moves
    the files whose gateway records name that path. Putting the folder back
    without changing `upload_dir` moves nothing: the upgrade looks only
    where `upload_dir` points. When the error names several paths, do this
    for each in turn: each start moves only the files whose gateway records
    name the path the folder is at, and reports the others again. A file
    the upgrade refused at the other path keeps records naming that path,
    so after the move back the error names that path for it: fix the file
    (see "A file it refuses" above), then do the same. Files only a
    conversation record or held guidance names cannot be moved at any path,
    and the error says so: keep them, and the people who sent them can send
    them again. The error is logged once and its count stays in the marker
    (`named_elsewhere`); to see the error again, delete
    `state/upgrades/adapter-host-files.json` and restart.
  - **Files named in another folder that cannot be moved** stay in the
    transfer folder. The upgrade never gives a command that deletes them,
    because they are files people sent and it cannot tell which are still
    needed. Keep them while anyone may need them. If you
    decide to remove them later, that is your decision: back the folder up
    first, stop the instance, and remove only files sent before the upgrade
    (a file's modification time is when it was sent), because the 0.10
    adapter host passes new files through the same folder.
  - **When the upgrade cannot tell that the folder moved.** If no record
    names any of the files (their records were all deleted, for example),
    the upgrade lists them as above. The warning then says not to delete
    them if the folder was at another path under 0.9, before it gives the
    command.
  - **The files could not be counted**: the warning
    `event=adapter_host_files_without_records_uncounted` gives the reason
    (a folder that cannot be listed, or one replaced during the upgrade).
    Counting them changed nothing and startup goes on. When the files were
    counted but the check for records naming them in another folder did
    not finish (`reason=other_folders_unchecked`), nothing is listed and
    no command is given: do not delete any of them; delete
    `state/upgrades/adapter-host-files.json` and restart to check again.
  - **The transfer folder is not here.** It belongs to the gateway: 0.9
    wrote it on the gateway's machine, also when the adapter host ran
    elsewhere. It is missing when it was not copied or mounted with the
    instance (for example a container started without the volume that
    held it). Nothing changes, startup goes on and an error in the log
    says so. Copy or mount the folder where `upload_dir` points and
    restart. If the files are really gone, run
    `docker exec <container> python -m assistant_cli upgrade adapter-host-files --config /data/instance_config.yaml --files-gone`
    while the team runs; their records are then removed as above.
    The same error comes when a start after an interrupted upgrade finds
    another folder in its place. If the log says the device number changed
    and the folder is the same one mounted again (some file systems give it
    a new device number at each mount), the files that start did not move
    are not in it, and `--files-gone` finishes the upgrade safely. If the log
    says it is probably another folder on the same disk, the earlier folder was
    probably moved or renamed: put it back where `upload_dir` points and
    restart, because the files that start did not move are still in it.
  - **A folder it cannot use** (a read-only or full `state/uploads`, a
    folder it has no permission to read): startup stops with a message
    naming the folder. Fix it and restart; the step goes on where it
    stopped.
- **From 0.9: ids that fit message addresses.** 0.10 carries the team id,
  each agent type and each agent's id in its message addresses, which allow at
  most 24 characters for a team id or an agent type and 32 for an agent id
  (letters, digits, `-` and `_`, starting with a letter or digit). At its first
  start, before it changes anything, 0.10 checks the team id
  (`gateway.team_id`), the agent types and the ids of agents that still have
  running tasks, waiting messages or handed-back tasks. If one does not fit, it
  stops with a message naming each id and what to do
  (`event=protocol1_ids_over_caps`). Nothing has changed then, so you can start
  0.9 again:
  - **A team id over 24 characters, or with other characters:** an instance
    keeps its team id, so this instance cannot run 0.10. Its team can move to
    a fresh 0.10 instance under a new team id:
    1. Start 0.9 again and update it to the latest 0.9.x release.
    2. Export the team with
       [`/upgrade`](team-transfer.md#export-from-chat-with-upgrade).
    3. Set up a fresh 0.10 instance and choose
       [Import in onboarding](team-transfer.md#import-during-onboarding). When
       the export's team id does not fit, the page says why and asks for a new
       team id, filled in with the old one cut to fit; you can change it.
       The team is imported under the new id; text you wrote, such as a
       schedule's instructions, is kept as written, even where it mentions
       the old id. The export file stays as it is. On the command line, `assistant_cli team review` takes the new id
       with `--new-team-id`, and `assistant_cli team import --review` then uses
       the id the review recorded.

    Without a new id, an import refuses such an export before it writes
    anything (`event=team_import_ids_over_caps`). An export with an agent type
    over 24 characters is refused the same way, also under a new team id:
    rename the type on 0.9 as below and export again. If the instance uses an
    external NATS server whose permissions name the old team id, update them
    for the new one.
  - **An agent type over 24 characters:** on 0.9, rename it in the file the
    message names and everywhere the instance names it:
    `gateway.startup_agents` in `instance_config.yaml` and the team roster
    (`control/TEAM_AGENTS.yaml`). For a specialist the team lead created,
    remove it and have it created again under a shorter name. Then start 0.10.
  - **An agent id over 32 characters:** on 0.9, let that agent's tasks finish
    or stop them, and let the team lead deal with tasks it handed back. Then
    start 0.10.

  When every id fits, 0.10 records it in `state/upgrades/protocol1-ids.json`
  and does not check again for that team id. Agents of a type added later with
  a name that is too long cannot join the team's messages, and the Status page
  says why. A startup agent (`gateway.startup_agents`) with such a type stops
  the start instead, with a message naming that entry
  (`event=protocol1_startup_agent_types_over_cap`): remove the entry or change
  it to a shorter type, then start again. Do not go back to 0.9 then, since
  0.10 has already run on the instance. The record is written before 0.10
  creates its message streams, so if 0.10 then refuses to start (for example
  for room, below) and you go back to 0.9, delete
  `state/upgrades/protocol1-ids.json` too, so that the next 0.10 start checks
  the ids again.

  Upgrade from 0.9 by starting 0.10, not with `assistant_cli.py
  migrate-config`: the start checks the ids before it upgrades
  `instance_config.yaml`, and `migrate-config` does not. 0.9 cannot read an
  upgraded file. If you ran `migrate-config` first and the start then stops at
  this check, the message names the backup to put back
  (`instance_config.yaml.bak.v12`) before you start 0.9 again. Services you
  start yourself with the `assistant_cli.py run-*` commands run no id check:
  start the instance once with the launcher first (the Docker image, or
  `docker/launch_headless.py`), which checks the ids and then upgrades the
  file.
- **From 0.9: room for the message streams.** At its first start 0.10 creates
  each team's new message streams, and the NATS server sets each stream's
  whole size limit aside on its storage at once: about 2.1 GiB per team at the
  default `transport.queues.max_bytes`
  ([Storage notices](configuration.md#storage-notices)). The included NATS
  server gives its streams three quarters of the disk's free space, measured
  at each start, so keep about 3 GiB free on the instance's disk for each
  team, at this start and every later one. When the room is not there, the
  start stops before anything is served and names the stream, the limit it
  asked for and the `transport.queues.max_bytes` entry. Do what the message
  says (free disk space, or lower that limit), then start again; streams
  already made stay and the next start finishes the rest.
- **From 0.9 with an external NATS server:** prepare the server first. 0.10
  needs NATS Server 2.15.1 or later with two server settings
  ([Surviving a power loss](configuration.md#surviving-a-power-loss)), and more
  from its NATS login ([Message host trust](docker.md#message-host-trust)): the
  partition mappings ([Message Broker](configuration.md#message-broker)) and
  room for each team's streams, as above. The
  runtime reports a server that is too old only for the NATS server it
  includes, so check an external server yourself.
- **PostgreSQL:** an instance that keeps its data in PostgreSQL gets the same
  upgrade steps, but release tests check them on SQLite only. Back up the
  database first.

## Before you upgrade

1. **Read the release notes of every release you skip**, starting with
   **Breaking Changes**. Each entry there says what changes and what to do
   first. The desktop app shows these notes and asks you to confirm before it
   switches versions.
2. **Finish or stop work in progress** when the notes ask you to.
3. **Back up the data folder**: the one you installed with
   (`--data-dir`), `~/.universal-agents/data` by default. If the instance
   keeps its data in PostgreSQL, back up that database too.

## Upgrade

- **Desktop app:** open **Updates**, read the notes, confirm and choose
  **Switch and restart**.
- **Installer:** download the new version's `install.sh` as the
  [Quickstart](quickstart.md) shows, then run it with every option you first
  installed with (such as `--data-dir`, `--container-name`, `--bind-host`,
  `--web-port`, `--broker-port` or `--public-origin`), the new version's
  `--image`, and `--replace`. The installer does not remember earlier options:
  without `--data-dir` it uses `~/.universal-agents/data`, so an instance kept
  in another folder would start empty, as a new install, on the default one.
  Your data stays in its folder; run the command again with its options. If
  you installed with the HTML setup guide, enter the same choices there to get
  your command.
- **Other Docker setups:** start the new image with the same data folder and
  configuration.

On its first start, the new version adds what it needs to the tables the old
one created and logs each addition (`event=storage_column_added`). If your
`instance_config.yaml` or `config.yaml` is from an older format, the launcher
upgrades it; services you start yourself offer to upgrade it or name the
`assistant_cli.py migrate-config` command. From 0.9, let the launcher do it,
after its id check (above). If something from the old version cannot carry
over, startup says what to do.

A memory, Knowledge page or topic command sent from 0.9 just before the upgrade
that was still waiting when the new version started is not carried out. You get
a chat message that says so; send it again.

Topic tags are limited to 64 characters after normalization. Existing longer
tags stay stored but are left out of lists, and the team logs a warning. New
oversized tags are refused, and each tagging command accepts at most 20 tags.

## Moving to another machine

To move a team to another machine, or to a fresh instance, use the
[team transfer](team-transfer.md).
