"""A fresh import refuses a team whose ids protocol 1 cannot carry, before it writes anything (G1 of PR 152's review).

team_transfer.protocol1_ids, called by every plan for a fresh root (the command-line import,
the review pages) and by onboarding's Import when an export is opened, before its review is
prepared. The launcher would otherwise refuse such a team at its first start, once everything
was written, with advice meant for an instance upgraded in place.
"""

from __future__ import annotations

import shutil
from pathlib import Path

import pytest
from aiohttp.test_utils import TestClient, TestServer

from team_transfer.container import TransferReader
from team_transfer.control import ControlSection
from team_transfer.crypto import new_key_derivation
from team_transfer.errors import TransferError
from team_transfer.service import export_source, export_team, import_team
from team_transfer.settings import SettingsSection
from tests.pinned_memory_support import new_instance
from tests.test_onboarding_team_import import _onboarding
from tests.test_team_transfer_control import GOOD_BUNDLE

PASSPHRASE = "correct horse battery staple"
SECTIONS = (SettingsSection(), ControlSection())
FITS = "team_" + "a" * 19  # 24 characters, protocol 1's cap
TOO_LONG = "team_" + "a" * 20  # 25
LONG_TYPE = "TravelPlannerForTheWholeTeam"  # 28


def _export(tmp_path: Path, team_id: str, *, agent_type: str | None = None) -> Path:
    """An export of a team, with a bundle defining ``agent_type`` when given."""

    from assistant_gateway.bootstrap import bootstrap_instance

    path, config = new_instance(tmp_path, "old", team_id)
    if agent_type is not None:
        bootstrap_instance(config)
        bundle = config.layout.control_dir / "agent_bundles" / "long"
        bundle.mkdir(parents=True)
        (bundle / "agent.yaml").write_text(GOOD_BUNDLE.replace("TravelPlanner", agent_type))
        (bundle / "prompt.md").write_text("You plan trips.\n")
    out = tmp_path / "exports" / "team.uatransfer"
    out.parent.mkdir(exist_ok=True)
    export_team(export_source(path), out, PASSPHRASE, owner_person_id="usr_owner", sections=SECTIONS,
                key_derivation=new_key_derivation(opslimit=1, memlimit=8192))
    return out


def _import(export: Path, target: Path, *, dry_run: bool = True) -> None:
    with TransferReader.open(export, PASSPHRASE) as reader:
        import_team(reader, target, dry_run=dry_run, sections=SECTIONS)


def test_a_team_id_over_24_characters_is_refused_before_the_import_writes_anything(tmp_path: Path, caplog) -> None:
    target = tmp_path / "new"

    with pytest.raises(TransferError) as refused:
        _import(_export(tmp_path, TOO_LONG), target, dry_run=False)

    assert str(refused.value) == (
        "This export's team cannot be imported into this version: protocol 1 carries these ids in message addresses, "
        "and they do not fit (event=team_import_ids_over_caps). Nothing was written.\n"
        f"The team id {TOO_LONG!r} is 25 characters long; protocol 1 allows at most 24. A team keeps its id when it "
        "moves, and this version has no way yet to give a team a shorter one, so this export cannot be imported into "
        "it.")
    assert "0.9.x" not in str(refused.value), "a fresh 0.10 import is not told to keep running 0.9.x"
    assert not target.exists() or not any(target.iterdir())
    assert "event=team_import_ids_over_caps" in caplog.text


def test_a_team_id_of_24_characters_is_imported(tmp_path: Path) -> None:
    target = tmp_path / "new"

    _import(_export(tmp_path, FITS), target, dry_run=False)

    assert (target / "instance_config.yaml").is_file()


def test_an_agent_type_over_24_characters_is_refused_naming_where_it_is_defined(tmp_path: Path) -> None:
    with pytest.raises(TransferError) as refused:
        _import(_export(tmp_path, FITS, agent_type=LONG_TYPE), tmp_path / "new")

    message = str(refused.value)
    assert (f"The agent type {LONG_TYPE!r} (defined in control/agent_bundles/long) is 28 characters long; protocol 1 "
            "allows at most 24. On the instance this export came from, rename it to at most 24 letters") in message
    assert message.endswith("Then export the team again and import the new export.")
    assert "team id" not in message.split("\n", 1)[1], "the team id fits"


@pytest.mark.asyncio
@pytest.mark.parametrize(("team_id", "agent_type", "refused"), [
    (TOO_LONG, None, f"The team id {TOO_LONG!r} is 25 characters long"),
    (FITS, LONG_TYPE, f"The agent type {LONG_TYPE!r} (defined in control/agent_bundles/long) is 28 characters long"),
    (FITS, None, None),
])
async def test_onboarding_refuses_such_an_export_when_it_is_opened_before_preparing_its_review(
    tmp_path: Path, team_id: str, agent_type: str | None, refused: str | None
) -> None:
    root = tmp_path / "new"
    (root / "import").mkdir(parents=True)
    shutil.copy(_export(tmp_path, team_id, agent_type=agent_type), root / "import" / "team.uatransfer")

    async with TestClient(TestServer(_onboarding(root).build_app())) as client:
        opened = await client.post("/api/team-import/open",
                                   json={"file_name": "team.uatransfer", "passphrase": PASSPHRASE})
        body = await opened.json()

    if refused is None:
        assert opened.status == 200 and body["token"]
        return
    assert opened.status == 400 and refused in body["error"]
    assert body["error"].startswith("This export's team cannot be imported into this version")
    assert sorted(path.name for path in root.iterdir()) == ["import"], "no review folder, nothing written"
