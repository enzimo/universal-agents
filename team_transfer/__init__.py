"""Move a team to a fresh instance of a newer version (``assistant_cli team``).

Before a breaking framework change, an operator exports the team's
configuration from the running instance into one transfer file, installs the
new version into a fresh instance and imports the file there. Everything the
new version cannot accept is listed record by record with the reason; nothing
is dropped silently. The plan and the owner's decisions are in the
architecture repository (``plans/team_transfer_plan.md``).

Design notes, so a later reader can map the structure to its reasons:

- Each version exports its own state with its own repositories and services.
  No version ever reads an older version's database layout; the transfer file
  is the only contract between versions.
- The file holds plain records, not database rows. Each section has its own
  ``schema_version`` and its own export models in this package, separate from
  internal models such as ``GrantV1``, so an internal change cannot silently
  change the format. Schemas include persisted payload models and identify their owning section.
  Frozen contract fingerprints require version changes even after schema
  regeneration. JSON Schemas of every model are checked in under
  ``data/schemas/team-transfer/v1`` and a test fails when they drift, which
  makes every format change deliberate.
- The file format is the one deliberate exception to "no compatibility
  layers" (owner's decision 2026-10-05): a version imports its own section
  versions and the ones the previous release exported, through one pure
  conversion step per section that changed since (``sections.SECTION_UPGRADES``).
  Steps from older releases' versions are deleted, and those files are refused
  with import/export instructions and the guide's release-specific paths.
- The first batch creates a fresh instance; later reviewed batches from the
  exact same signed backup skip imported identities. Import keeps the original identities
  (team id, instance name, person, schedule and grant ids), which removes
  nearly all remapping.
- One planner decides every record's outcome and both ``--dry-run`` and the
  real import use it, as ``custom_agents.pinned_memory_transfer`` does.
- A passphrase protects the file: a key derived from it authenticates the
  manifest, which lists the SHA-256 of every member, so an edited file is
  refused; the same key family encrypts the credentials sections.

Operator commands run on the host are not agent-callable operations, so they
have no tool-catalog level (as with ``access drop-old-tables``).
"""
