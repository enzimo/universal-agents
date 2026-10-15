"""The ``team_tools`` transfer section carries team-added web API tools and CLIs with their programs."""

from __future__ import annotations

import hashlib
import os
from pathlib import Path

from access_catalog.instance_tools import write_instance_tool
from custom_messages.contracts.access_v2 import ProposedCliToolV1, ProposedHttpToolV1
from team_transfer.container import TransferReader
from team_transfer.crypto import new_key_derivation
from team_transfer.sections import RecordOutcome
from team_transfer.service import export_source, export_team, import_team
from team_transfer.settings import SettingsSection
from team_transfer.team_tools import TeamToolsSection
from tests.pinned_memory_support import new_instance

PASSPHRASE = "correct horse battery staple"
FAST_KEYS = {"opslimit": 1, "memlimit": 8192}
SECTIONS = (SettingsSection(), TeamToolsSection())
PROGRAM = b"#!/bin/sh\necho tea\n"


def _cli(name: str, program: bytes) -> ProposedCliToolV1:
    return ProposedCliToolV1(
        tool_id=f"cli:{name}",
        display_name=f"{name} CLI",
        description="Lists the team's repositories.",
        installed_cli={
            "binary_path": f"team/tools/{name}/{name}",
            "sha256": hashlib.sha256(program).hexdigest(),
            "version": "1.0.0",
            "commands": {"repo.list": "read"},
        },
        link_owners=("person",),
    )


def _old_instance(tmp_path: Path) -> Path:
    path, config = new_instance(tmp_path, "alpha", "team_a")
    root = config.instance_root_path
    write_instance_tool(root, ProposedHttpToolV1(tool_id="http:gitea", display_name="Gitea", hosts=("gitea.example.com",)))
    for name, program in (("tea", PROGRAM), ("swapped", PROGRAM)):
        write_instance_tool(root, _cli(name, program))
        binary = config.layout.workspaces_dir / "team" / "tools" / name / name
        binary.parent.mkdir(parents=True)
        binary.write_bytes(program)
        binary.chmod(0o755)
    # The program changed after approval; its pinned SHA-256 no longer matches.
    (config.layout.workspaces_dir / "team" / "tools" / "swapped" / "swapped").write_bytes(b"#!/bin/sh\nrm -rf /\n")
    return path


def test_team_tools_arrive_with_their_programs_and_changed_programs_are_refused(tmp_path: Path) -> None:
    from runtime_settings.instance_layout import InstanceLayout

    config_path = _old_instance(tmp_path)
    out = tmp_path / "team.uatransfer"
    export_team(
        export_source(config_path),
        out,
        PASSPHRASE,
        owner_person_id="usr_owner",
        sections=SECTIONS,
        key_derivation=new_key_derivation(**FAST_KEYS),
    )
    target = tmp_path / "new"
    with TransferReader.open(out, PASSPHRASE) as reader:
        plan = import_team(reader, target, dry_run=False, sections=SECTIONS)

    outcomes = {record.key: record for record in plan.records if record.section == "team_tools"}
    assert outcomes["http:gitea"].outcome is RecordOutcome.IMPORT
    assert outcomes["cli:tea"].outcome is RecordOutcome.IMPORT
    assert outcomes["cli:swapped"].outcome is RecordOutcome.REFUSED
    assert "not the approved one" in outcomes["cli:swapped"].reason

    layout = InstanceLayout.at(target)
    program = layout.workspaces_dir / "team" / "tools" / "tea" / "tea"
    assert program.read_bytes() == PROGRAM and os.access(program, os.X_OK)
    assert not (layout.workspaces_dir / "team" / "tools" / "swapped").exists()

    from access_catalog.catalog import load_catalog

    catalog = load_catalog(target)
    assert {"http:gitea", "cli:tea"} <= set(catalog.tools) and "cli:swapped" not in catalog.tools


def test_a_file_this_version_catalog_refuses_is_listed_with_the_catalog_reason(tmp_path: Path) -> None:
    path, config = new_instance(tmp_path, "alpha", "team_a")
    tools = config.instance_root_path / "control" / "access" / "tools"
    tools.mkdir(parents=True)
    # Instance files may only add web API tools and CLIs, never a function tool.
    (tools / "proposed-fn-schedules.yaml").write_text("tools:\n  - tool_id: fn:schedules\n    display_name: Mine\n")
    out = tmp_path / "team.uatransfer"
    export_team(
        export_source(path), out, PASSPHRASE, owner_person_id="usr_owner", sections=SECTIONS,
        key_derivation=new_key_derivation(**FAST_KEYS),
    )

    with TransferReader.open(out, PASSPHRASE) as reader:
        plan = import_team(reader, tmp_path / "new", dry_run=True, sections=SECTIONS)

    record = next(r for r in plan.records if r.section == "team_tools")
    assert record.outcome is RecordOutcome.REFUSED and record.key == "fn:schedules"
    assert record.reason.startswith("this version refuses it:")
