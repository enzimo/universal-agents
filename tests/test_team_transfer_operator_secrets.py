"""The ``operator_secrets`` transfer section carries operator-supplied secrets, encrypted."""

from __future__ import annotations

import stat
from pathlib import Path

from internal_credentials import parse_env_file
from team_transfer.container import TransferReader
from team_transfer.crypto import new_key_derivation
from team_transfer.operator_secrets import OperatorSecretsSection
from team_transfer.sections import RecordOutcome
from team_transfer.service import export_source, export_team, import_team
from team_transfer.settings import SettingsSection
from tests.pinned_memory_support import new_instance

PASSPHRASE = "correct horse battery staple"
FAST_KEYS = {"opslimit": 1, "memlimit": 8192}
SECTIONS = (SettingsSection(), OperatorSecretsSection())
BOT_TOKEN = "123456:telegram-bot-token-value"
MODEL_KEY = "sk-operator-model-key-value"
OLD_SIGNING_KEY = "gateway-old:old-signing-seed"
CODEX_AUTH = '{"tokens": {"refresh_token": "codex-refresh-value"}}'


def _old_instance(tmp_path: Path) -> Path:
    path, config = new_instance(tmp_path, "alpha", "team_a")
    config.layout.team_env_path.write_text(
        f"TELEGRAM_BOT_TOKEN={BOT_TOKEN}\nOPENAI_API_KEY={MODEL_KEY}\nACCESS_GATEWAY_SIGNING_KEY={OLD_SIGNING_KEY}\n"
    )
    config.layout.openai_codex_auth_path.parent.mkdir(parents=True)
    config.layout.openai_codex_auth_path.write_text(CODEX_AUTH)
    return path


def test_operator_secrets_travel_encrypted_and_generated_keys_are_made_fresh(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("OPENAI_API_KEY", "the-operators-personal-shell-key")
    out = tmp_path / "team.uatransfer"
    export_team(
        export_source(_old_instance(tmp_path)), out, PASSPHRASE, owner_person_id="usr_owner", sections=SECTIONS,
        key_derivation=new_key_derivation(**FAST_KEYS),
    )
    raw = out.read_bytes()
    for secret in (BOT_TOKEN, MODEL_KEY, "codex-refresh-value"):
        assert secret.encode() not in raw

    with TransferReader.open(out, PASSPHRASE) as reader:
        from team_transfer.sections import read_section_records

        carried = {r["name"]: r["value"] for r in read_section_records(reader, "operator_secrets", OperatorSecretsSection.schema_version)}
    assert carried["OPENAI_API_KEY"] == MODEL_KEY, "the instance's team.env, not the operator's own shell"
    assert "ACCESS_GATEWAY_SIGNING_KEY" not in carried

    target = tmp_path / "new"
    target.mkdir()
    # A value the operator already put in the new team.env wins.
    (target / "team.env").write_text("OPENAI_API_KEY=new-instance-own-key\n")
    with TransferReader.open(out, PASSPHRASE) as reader:
        plan = import_team(reader, target, dry_run=False, sections=SECTIONS)

    outcomes = {r.key: r for r in plan.records if r.section == "operator_secrets"}
    assert set(outcomes) == {"environment/TELEGRAM_BOT_TOKEN", "environment/OPENAI_API_KEY", "auth_file/openai_codex_auth.json"}
    assert outcomes["environment/OPENAI_API_KEY"].outcome is RecordOutcome.ATTENTION
    for record in plan.records:
        assert BOT_TOKEN not in record.reason and MODEL_KEY not in record.reason

    env = parse_env_file(target / "team.env")
    assert env["TELEGRAM_BOT_TOKEN"] == BOT_TOKEN
    assert env["OPENAI_API_KEY"] == "new-instance-own-key"
    assert env["ACCESS_GATEWAY_SIGNING_KEY"] and env["ACCESS_GATEWAY_SIGNING_KEY"] != OLD_SIGNING_KEY
    codex = target / "auth" / "openai_codex_auth.json"
    assert codex.read_text() == CODEX_AUTH and stat.S_IMODE(codex.stat().st_mode) == 0o600
    report = next((target / "runtime" / "team-import").glob("report-*.json")).read_text()
    assert BOT_TOKEN not in report and MODEL_KEY not in report and "codex-refresh-value" not in report
