# Upgrading

Install the new version over the old one. Your settings, people, access,
memory, knowledge, schedules and history stay where they are.

## Which versions upgrade in place

A release upgrades an instance from any earlier release of its own minor
version and of the previous one: 0.10.x opens what any 0.9.x saved. Upgrades
only go forward. Going back to an earlier release is not supported, because it
may not read what a newer one saved.

For a longer jump, upgrade to a release in between first, or move the team to
a fresh instance with the [team transfer](team-transfer.md).

## Before you upgrade

1. **Read the release notes of every release you skip**, starting with
   **Breaking Changes**. Each entry there says what changes and what to do
   first. The desktop app shows these notes and asks you to confirm before it
   switches versions.
2. **Finish or stop work in progress** when the notes ask you to.
3. **Back up the data folder** (`~/.universal-agents/data` with the
   installer).

## Upgrade

- **Desktop app:** open **Updates**, read the notes, confirm and choose
  **Switch and restart**.
- **Installer:** run the new version's install command from the
  [Quickstart](quickstart.md) with `--replace` added, which recreates the
  container on the same data folder.
- **Other Docker setups:** start the new image with the same data folder and
  configuration.

On its first start, the new version adds what it needs to the tables the old
one created and logs each addition (`event=storage_column_added`). If your
`instance_config.yaml` or `config.yaml` is from an older format, startup offers
to upgrade it or names the `assistant_cli.py migrate-config` command. If
something from the old version cannot carry over, startup says what to do.

## Moving to another machine

To move a team to another machine, or to a fresh instance, use the
[team transfer](team-transfer.md).
