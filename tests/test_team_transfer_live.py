"""Team transfer end to end with real processes (live profile).

Starts the old instance's secure tool broker as the runtime does
(``assistant_cli run-secure-tool-broker``, file-based credential store),
exports with the real command, imports into a fresh root, starts the new
instance's broker and runs ``team import-credentials``, then reads everything
back. The Sealbox-backed broker store needs a Sealbox server and is covered by
the Docker acceptance run.
"""
from __future__ import annotations

import json
import os
import shutil
import socket
import subprocess
import time
from datetime import datetime, timezone
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]
BASE = REPO / "test_results" / "team_transfer_live"
PY = str(REPO / ".venv" / "bin" / "python")
PASSPHRASE = "end to end passphrase"
GITHUB = "gho_e2e_person_token"
MODEL = "sk-e2e-team-model-key"


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def broker_env(config) -> dict[str, str]:
    env = dict(os.environ)
    env.update(
        SECURE_TOOL_BROKER_CREDENTIAL_STATE_BACKEND_MODE="files",
        SECURE_TOOL_BROKER_CREDENTIALS_DIR=str(config.broker_credentials_path),
        SECURE_TOOL_BROKER_CREDENTIAL_CATALOG_DB_PATH=str(config.credential_catalog_database_path),
        TEAM_INSTANCE_ROOT=str(config.instance_root_path),
    )
    return env


def start_broker(config_path: Path, config, port: int) -> subprocess.Popen:
    log = open(BASE / f"broker-{port}.log", "w")
    proc = subprocess.Popen(
        [PY, "-m", "assistant_cli", "run-secure-tool-broker", "--config", str(config_path),
         "--env", str(config.layout.team_env_path), "--host", "127.0.0.1", "--port", str(port)],
        cwd=REPO, env=broker_env(config), stdout=log, stderr=subprocess.STDOUT,
    )
    import httpx
    for _ in range(120):
        try:
            if httpx.get(f"http://127.0.0.1:{port}/health", timeout=1).status_code == 200:
                return proc
        except httpx.HTTPError:
            pass
        if proc.poll() is not None:
            raise SystemExit(f"broker exited; see {log.name}")
        time.sleep(0.5)
    raise SystemExit("broker did not start")


def cli(*args: str, env: dict[str, str]) -> subprocess.CompletedProcess:
    result = subprocess.run([PY, "-m", "assistant_cli", *args], cwd=REPO, env=env, capture_output=True, text=True)
    print(f"$ assistant_cli {' '.join(args)}  -> exit {result.returncode}")
    print(result.stdout[-4000:])
    if result.stderr.strip():
        print("stderr:", result.stderr[-2000:])
    return result


def run(base: Path) -> None:
    global BASE
    BASE = base
    from assistant_gateway.bootstrap import bootstrap_instance
    from assistant_gateway.config import GatewayInstanceConfig
    from assistant_gateway.instance_secrets import ensure_internal_credential_env_file
    from custom_messages.contracts.access_v2 import (
        AccessLevel, GrantV1, PersonStanding, SubjectKind, SubjectRefV1, TargetKind, TargetRefV1, TeamPersonV1,
    )
    from storage.access_repository import AccessRepository
    from storage.gateway_state_repository import create_gateway_state_repository

    shutil.rmtree(BASE, ignore_errors=True)
    BASE.mkdir(parents=True)
    saved_environment = dict(os.environ)
    old_root, new_root = BASE / "old", BASE / "new"
    config = GatewayInstanceConfig.create(instance_name="alpha", instance_root=str(old_root), team_id="team_e2e", timezone="UTC")
    config.security_layer.credential_state_backend.mode = "files"
    config.security_layer.sealbox.enabled = False
    config.heartbeat.enabled = False
    config_path = config.save(old_root / "instance_config.yaml")
    bootstrap_instance(config)
    ensure_internal_credential_env_file(config)
    with open(config.layout.team_env_path, "a") as handle:
        handle.write("TELEGRAM_BOT_TOKEN=123:e2e-bot-token\n")

    now = datetime.now(timezone.utc)
    people = create_gateway_state_repository(config.state_storage_settings)
    for person, role in (("usr_ana", "owner"), ("usr_ben", "member")):
        people.create_gateway_user(canonical_user_id=person, display_name=person, timezone="UTC")
        people.create_team_membership(canonical_user_id=person, team_id="team_e2e", role=role)
    people.create_channel_binding(canonical_user_id="usr_ana", channel_name="telegram", external_user_id="tg-1")
    store = AccessRepository(config.manager_storage_settings)
    store.replace_people("team_e2e", [
        TeamPersonV1(team_id="team_e2e", person_id="usr_ana", standing=PersonStanding.ACTIVE, roles=("owner", "admin"), changed_at_utc=now),
        TeamPersonV1(team_id="team_e2e", person_id="usr_ben", standing=PersonStanding.ACTIVE, roles=("member",), changed_at_utc=now),
    ], at=now)
    store.create(GrantV1(grant_id="g_e2e", team_id="team_e2e", subject=SubjectRefV1(kind=SubjectKind.USER, id="usr_ben"),
                         target=TargetRefV1(kind=TargetKind.TOOL, id="fn:schedules"), level=AccessLevel.WRITE,
                         granted_by=SubjectRefV1(kind=SubjectKind.USER, id="usr_ana"), created_at_utc=now))

    # Credentials as the old broker stored them (file backend).
    os.environ.update({k: v for k, v in broker_env(config).items() if k.startswith(("SECURE_TOOL_BROKER", "TEAM_INSTANCE"))})
    from model_providers.endpoints import EndpointKind
    from secure_tool_broker.credential_state_store import write_credential_state
    from secure_tool_broker.model_keys import TEAM_SCOPE, ModelKeyStore, StoredModelKey
    assert write_credential_state("github.token", GITHUB, owner_id="usr_ana").status == "success"
    ModelKeyStore("team_e2e", scope=TEAM_SCOPE).save(StoredModelKey(
        address="https://api.openai.com/v1", kind=EndpointKind.OPENAI, key=MODEL, saved_at=now, saved_by_person_id="usr_ana"))

    passfile = BASE / "passphrase"
    passfile.write_text(PASSPHRASE + "\n")
    out = BASE / "team.uatransfer"
    port = free_port()
    old_broker = start_broker(config_path, config, port)
    cli_env = dict(os.environ, SECURE_TOOL_BROKER_INTERNAL_BASE_URL=f"http://127.0.0.1:{port}")
    try:
        exported = cli("team", "export", "--config", str(config_path), "--owner", "usr_ana", "--out", str(out),
                       "--passphrase-file", str(passfile), env=cli_env)
    finally:
        old_broker.terminate()
        old_broker.wait(10)
    assert exported.returncode == 0
    raw = out.read_bytes()
    for secret in (GITHUB, MODEL, "e2e-bot-token"):
        assert secret.encode() not in raw, secret

    clean_env = {k: v for k, v in os.environ.items() if not k.startswith(("SECURE_TOOL_BROKER", "TEAM_INSTANCE"))}
    dry = cli("team", "import", "--in", str(out), "--instance-root", str(new_root), "--dry-run", "--passphrase-file", str(passfile), env=clean_env)
    assert dry.returncode == 0 and "Nothing was written (dry run)." in dry.stdout
    assert not new_root.exists()
    from team_transfer.container import TransferReader
    from team_transfer.review import prepare_review

    review_dir = BASE / "review"
    with TransferReader.open(out, PASSPHRASE) as reader:
        prepare_review(reader, new_root, review_dir)
    done = cli("team", "import", "--in", str(out), "--instance-root", str(new_root), "--review", str(review_dir), "--passphrase-file", str(passfile), env=clean_env)
    assert done.returncode == 0, done.stdout
    assert (new_root / "instance_config.yaml").is_file()

    new_config = GatewayInstanceConfig.load(new_root / "instance_config.yaml")
    port2 = free_port()
    new_broker = start_broker(new_root / "instance_config.yaml", new_config, port2)
    try:
        creds = cli("team", "import-credentials", "--in", str(out), "--config", str(new_root / "instance_config.yaml"),
                    "--review", str(review_dir), "--passphrase-file", str(passfile), env=dict(clean_env, SECURE_TOOL_BROKER_INTERNAL_BASE_URL=f"http://127.0.0.1:{port2}"))
        repeated = cli("team", "import-credentials", "--in", str(out), "--config", str(new_root / "instance_config.yaml"),
                       "--review", str(review_dir), "--passphrase-file", str(passfile), env=dict(clean_env, SECURE_TOOL_BROKER_INTERNAL_BASE_URL=f"http://127.0.0.1:{port2}"))
        assert repeated.returncode == 0 and "Stored 0 of 0" in repeated.stdout
    finally:
        new_broker.terminate()
        new_broker.wait(10)
    assert creds.returncode == 0

    for key in list(os.environ):
        if key.startswith(("SECURE_TOOL_BROKER", "TEAM_INSTANCE")):
            del os.environ[key]
    os.environ.update({k: v for k, v in broker_env(new_config).items() if k.startswith(("SECURE_TOOL_BROKER", "TEAM_INSTANCE"))})
    from secure_tool_broker.credential_state_store import read_credential_state
    checks = {
        "github sign-in moved": read_credential_state("github.token", owner_id="usr_ana") == GITHUB,
        "team model key moved": [e.key for e in ModelKeyStore("team_e2e", scope=TEAM_SCOPE).entries()] == [MODEL],
        "bot token in new team.env": "123:e2e-bot-token" in (new_root / "team.env").read_text(),
        "new signing key differs": (new_root / "team.env").read_text().split("ACCESS_GATEWAY_SIGNING_KEY=")[1].split("\n")[0]
            != (old_root / "team.env").read_text().split("ACCESS_GATEWAY_SIGNING_KEY=")[1].split("\n")[0],
        "grant moved": [g.grant_id for g in AccessRepository(new_config.manager_storage_settings).list_team("team_e2e")] == ["g_e2e"],
        "people moved": sorted(p.canonical_user_id for p in create_gateway_state_repository(new_config.state_storage_settings).list_team_people("team_e2e")) == ["usr_ana", "usr_ben"],
        "notice for ana": any(n.kind.value == "credentials_imported" for n, _read in AccessRepository(new_config.manager_storage_settings).notices_for("team_e2e", person_id="usr_ana", roles=())),
        "no unfinished marker": not (new_root / "runtime" / "team-import" / "in-progress.json").exists(),
        "file backend carried": new_config.security_layer.credential_state_backend.mode == "files",
    }
    print(json.dumps(checks, indent=2))
    assert all(v is not False for v in checks.values()), checks
    os.environ.clear()
    os.environ.update(saved_environment)


@pytest.mark.live
def test_team_transfer_end_to_end_with_real_broker_processes(tmp_path: Path) -> None:
    run(tmp_path / "work")
