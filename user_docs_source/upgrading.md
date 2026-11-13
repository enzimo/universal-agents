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
  Web Chat need nothing. For a chat app in the adapter host, first let
  requests that use files sent through it finish. After the upgrade the team
  can no longer open those files with its tools. The chat each such request
  came from is told which files to send again, and the person it works for
  also gets the notice in the Inbox. On a team with gateway users off nobody
  is verified, so the chat gets the message and administrators get an Inbox
  notice with the number of files.
- **From 0.9 with an external NATS server:** prepare the server first. 0.10
  needs NATS Server 2.15.1 or later with two server settings
  ([Surviving a power loss](configuration.md#surviving-a-power-loss)), and more
  from its NATS login ([Message host trust](docker.md#message-host-trust)). The
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
`instance_config.yaml` or `config.yaml` is from an older format, startup offers
to upgrade it or names the `assistant_cli.py migrate-config` command. If
something from the old version cannot carry over, startup says what to do.

A memory, Knowledge page or topic command sent from 0.9 just before the upgrade
that was still waiting when the new version started is not carried out. You get
a chat message that says so; send it again.

Topic tags are limited to 64 characters after normalization. Existing longer
tags stay stored but are left out of lists, and the team logs a warning. New
oversized tags are refused, and each tagging command accepts at most 20 tags.

## Moving to another machine

To move a team to another machine, or to a fresh instance, use the
[team transfer](team-transfer.md).
