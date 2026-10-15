"""The ``people`` transfer section carries the team's people, roles, chat accounts and Web Chat sign-ins."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from team_transfer.container import TransferReader
from team_transfer.crypto import new_key_derivation
from team_transfer.people import PeopleSection
from team_transfer.sections import RecordOutcome
from team_transfer.service import TransferError, export_source, export_team, import_team
from team_transfer.settings import SettingsSection
from tests.pinned_memory_support import new_instance

PASSPHRASE = "correct horse battery staple"
FAST_KEYS = {"opslimit": 1, "memlimit": 8192}
SECTIONS = (SettingsSection(), PeopleSection())
TEAM = "team_a"


def _appearance() -> dict[str, Any]:
    from assistant_gateway.application.website_application import WebAppearanceV1
    from runtime_settings.appearance import DarkMetalAppearanceSettings

    return WebAppearanceV1(
        color_mode="dark",
        light_colors=("#112233", "#223344", "#334455", "#445566"),
        light_diffusion=50,
        polygon_opacity_low=10,
        polygon_opacity_high=30,
        polygon_line_color="#112233",
        polygon_line_opacity=40,
        polygon_seed=4147,
        dynamic_background=True,
        dark=DarkMetalAppearanceSettings().model_dump(mode="json"),
    ).model_dump(mode="json")


def _repository(config: Any) -> Any:
    from storage.gateway_state_repository import create_gateway_state_repository

    return create_gateway_state_repository(config.state_storage_settings)


def _old_instance(tmp_path: Path, *, owner_role: str = "owner") -> Path:
    path, config = new_instance(tmp_path, "alpha", TEAM)
    repo = _repository(config)
    repo.create_gateway_user(canonical_user_id="usr_ana", display_name="Ana", timezone="Europe/Lisbon")
    repo.set_gateway_user_default_channel(canonical_user_id="usr_ana", channel_name="telegram", chat_id="42", thread_id=None)
    repo.upsert_gateway_user_appearance(canonical_user_id="usr_ana", appearance=_appearance())
    repo.create_team_membership(canonical_user_id="usr_ana", team_id=TEAM, role=owner_role)
    repo.create_channel_binding(canonical_user_id="usr_ana", channel_name="telegram", external_user_id="tg-42")
    repo.create_channel_binding(canonical_user_id="usr_ana", channel_name="cli", external_user_id="ana", chat_scope="cli:alpha")
    repo.create_web_user_token(token_hash="a" * 64, canonical_user_id="usr_ana", label="laptop")
    repo.create_web_user_token(token_hash="b" * 64, canonical_user_id="usr_ana", label="old", expires_at_utc="2020-01-01T00:00:00+00:00")

    repo.create_gateway_user(canonical_user_id="usr_ben", display_name="Ben", timezone="UTC")
    repo.upsert_gateway_user_appearance(canonical_user_id="usr_ben", appearance={**_appearance(), "retired_field": 1})
    repo.create_team_membership(canonical_user_id="usr_ben", team_id=TEAM, role="member")
    repo.set_gateway_user_status("usr_ben", "disabled")

    repo.create_gateway_user(canonical_user_id="usr_gone", display_name="Gone", timezone="UTC")
    repo.create_team_membership(canonical_user_id="usr_gone", team_id=TEAM, role="member", status="removed")
    return path


def _transfer(tmp_path: Path, config_path: Path, *, dry_run: bool = False):
    out = tmp_path / "team.uatransfer"
    export_team(
        export_source(config_path),
        out,
        PASSPHRASE,
        owner_person_id="usr_ana",
        sections=SECTIONS,
        key_derivation=new_key_derivation(**FAST_KEYS),
    )
    target = tmp_path / "new"
    with TransferReader.open(out, PASSPHRASE) as reader:
        plan = import_team(reader, target, dry_run=dry_run, sections=SECTIONS)
    return plan, target


def test_people_arrive_with_their_roles_accounts_and_sign_ins(tmp_path: Path) -> None:
    from assistant_gateway.config import GatewayInstanceConfig

    plan, target = _transfer(tmp_path, _old_instance(tmp_path))

    outcomes = {r.key: r for r in plan.records if r.section == "people"}
    assert set(outcomes) == {"usr_ana", "usr_ben"}, "a removed person stays behind"
    assert outcomes["usr_ana"].outcome is RecordOutcome.IMPORT
    assert outcomes["usr_ben"].outcome is RecordOutcome.ATTENTION
    assert "appearance is reset to the default" in outcomes["usr_ben"].reason

    repo = _repository(GatewayInstanceConfig.load(target / "instance_config.yaml"))
    ana = repo.get_gateway_user("usr_ana")
    assert (ana.display_name, ana.timezone, ana.status) == ("Ana", "Europe/Lisbon", "active")
    assert (ana.default_channel_name, ana.default_chat_id) == ("telegram", "42")
    assert [(m.team_id, m.role, m.status) for m in repo.list_team_memberships("usr_ana")] == [(TEAM, "owner", "active")]
    bindings = repo.list_channel_bindings_for_user("usr_ana", status="active")
    assert [(b.channel_name, b.external_user_id) for b in bindings] == [("telegram", "tg-42")], "CLI links stay behind"
    tokens = repo.list_web_user_tokens("usr_ana", status="active")
    assert [t.token_hash for t in tokens] == ["a" * 64], "expired sign-ins stay behind"
    assert repo.resolve_web_user_token(token_hash="a" * 64) is not None
    assert repo.get_gateway_user_appearance("usr_ana") is not None

    ben = repo.get_gateway_user("usr_ben")
    assert ben.status == "disabled" and repo.get_gateway_user_appearance("usr_ben") is None
    assert repo.get_gateway_user("usr_gone") is None


def test_an_import_without_an_active_owner_is_refused(tmp_path: Path) -> None:
    with pytest.raises(TransferError, match="No active owner"):
        _transfer(tmp_path, _old_instance(tmp_path, owner_role="admin"), dry_run=True)
