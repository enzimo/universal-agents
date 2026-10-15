"""Onboarding's Import, end to end: export, select in onboarding, import, then the first start.

The old instance's broker exports a stored sign-in through its real transfer
route (a real broker app on a loopback port, file credential backend). The
export is copied into the new data folder's ``import/`` folder and imported
through onboarding's own routes, as the browser does. The new instance's
broker then stores the credentials at the first start
(``team_transfer.credential_handover``).
"""

from __future__ import annotations

import asyncio
import shutil
from pathlib import Path

import pytest
from aiohttp.test_utils import TestClient, TestServer

from assistant_gateway.config import GatewayInstanceConfig
from assistant_gateway.onboarding_web.server import OnboardingWebService, OnboardingWebSettings
from custom_agents.schedule_store import ScheduleStore
from internal_credentials import parse_env_file
from secure_tool_broker.service import SecureToolBrokerService
from storage.access_repository import AccessRepository
from storage.gateway_state_repository import create_gateway_state_repository
from storage.pinned_memory_repository import create_pinned_memory_repository
from team_transfer.broker_credentials import BrokerCredentialsSection
from team_transfer.credential_handover import import_handed_over_credentials
from team_transfer.crypto import new_key_derivation
from team_transfer.errors import TransferError
from team_transfer.people import PeopleSection
from team_transfer.pinned_memory import PinnedMemorySection
from team_transfer.schedules import SchedulesSection
from team_transfer.service import export_source, export_team
from team_transfer.settings import SettingsSection
from tests.broker_access_support import open_gate
from tests.pinned_memory_support import new_instance
from tests.test_team_transfer_broker_credentials import _use_store
from tests.test_team_transfer_schedules import TEAM, _schedule

PASSPHRASE = "correct horse battery staple"
OLD_BROKER_ROOT = "old-instance-broker-root"
GITHUB_TOKEN = "gho_ana_github_sign_in_value"
SECTIONS = (
    SettingsSection(),
    PeopleSection(),
    SchedulesSection(),
    PinnedMemorySection(),
    BrokerCredentialsSection(),
)


async def _serving_broker(root_token: str, monkeypatch: pytest.MonkeyPatch) -> TestServer:
    """A real broker app on a loopback port; the transfer code reaches it over HTTP."""

    service = SecureToolBrokerService(token=root_token, access_gate=open_gate(team_id=TEAM))
    server = TestServer(service.build_app(), host="127.0.0.1")
    await server.start_server()
    monkeypatch.setenv("SECURE_TOOL_BROKER_INTERNAL_BASE_URL", str(server.make_url("")).rstrip("/"))
    return server


async def _export(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    from secure_tool_broker.credential_state_store import write_credential_state

    path, config = new_instance(tmp_path, "alpha", TEAM)
    config.heartbeat.enabled = False
    config.save(path)
    (path.parent / "team.env").write_text(f"SECURE_TOOL_BROKER_TOKEN={OLD_BROKER_ROOT}\n")
    people = create_gateway_state_repository(config.state_storage_settings)
    for person, name, role in (("usr_ana", "Ana Lima", "owner"), ("usr_ben", "Ben Ode", "member")):
        people.create_gateway_user(canonical_user_id=person, display_name=name, timezone="UTC")
        people.create_team_membership(canonical_user_id=person, team_id=TEAM, role=role)
    people.upsert_session(
        session_key="sess_ana", owner_canonical_user_id="usr_ana", status="active", channel_name="telegram",
        user_id="tg-42", chat_id="42", conversation_id=None, last_task_id=None, manager_agent_id=None,
    )
    ScheduleStore(storage_settings=config.manager_storage_settings).add(
        _schedule("sched_morning", "usr_ana", schedule_name="Morning briefing")
    )
    memory = create_pinned_memory_repository(config.manager_storage_settings)
    memory.create_coremem(team_id=TEAM, fact_text="Keep the weekly report short", capacity=10)
    memory.create_coremem(team_id=TEAM, fact_text="An outdated fact", capacity=10)

    _use_store(tmp_path / "old_store", monkeypatch)
    assert write_credential_state("github.token", GITHUB_TOKEN, owner_id="usr_ana").status == "success"
    (tmp_path / "exports").mkdir()
    broker = await _serving_broker(OLD_BROKER_ROOT, monkeypatch)
    try:
        report = await asyncio.to_thread(
            export_team,
            export_source(path),
            tmp_path / "exports" / "team.uatransfer",
            PASSPHRASE,
            owner_person_id="usr_ana",
            sections=SECTIONS,
            key_derivation=new_key_derivation(opslimit=1, memlimit=8192),
        )
    finally:
        await broker.close()
    return report.path


def _onboarding(root: Path) -> OnboardingWebService:
    service = OnboardingWebService(
        OnboardingWebSettings(host="127.0.0.1", port=18090, instance_root=str(root), restart_on_complete=True)
    )
    service.completion_event = asyncio.Event()
    return service


@pytest.mark.asyncio
async def test_onboarding_imports_a_selected_team_and_the_first_start_stores_its_credentials(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from secure_tool_broker.credential_state_store import read_credential_state

    exported = await _export(tmp_path, monkeypatch)
    root = tmp_path / "new"
    (root / "import").mkdir(parents=True)
    shutil.copy(exported, root / "import" / "team.uatransfer")
    service = _onboarding(root)

    async with TestClient(TestServer(service.build_app())) as client:
        listing = await (await client.get("/api/team-import/exports")).json()
        assert listing["problem"] == ""
        assert [item["file_name"] for item in listing["exports"]] == ["team.uatransfer"]

        wrong = await client.post(
            "/api/team-import/open", json={"file_name": "team.uatransfer", "passphrase": "not the passphrase"}
        )
        assert wrong.status == 400 and "passphrase is wrong" in (await wrong.json())["error"]
        opened = await client.post(
            "/api/team-import/open", json={"file_name": "team.uatransfer", "passphrase": PASSPHRASE}
        )
        assert opened.status == 200
        token = (await opened.json())["token"]
        assert (await client.get("/api/team-import/review")).status == 403
        headers = {"X-Transfer-Token": token}
        review = await (await client.get("/api/team-import/review", headers=headers)).json()

        titles = {(r["section"], r["key"]): r["title"] for r in review["records"]}
        assert titles[("people", "usr_ana")] == "Ana Lima (owner)"
        assert titles[("schedules", "schedule/sched_morning")] == "Morning briefing"
        assert [s["name"] for s in review["sections"]] == [
            "settings", "people", "schedules", "pinned_memory", "broker_credentials",
        ]
        assert all(r["selected"] for r in review["records"]), "everything starts selected"
        assert review["destination"] == {"secrets_source": "env_file", "exported_secrets_source": "env_file"}
        assert GITHUB_TOKEN not in str(review)

        outdated = next(r for r in review["records"] if r["title"] == "An outdated fact")
        await client.post(
            "/api/team-import/record",
            json={"section": outdated["section"], "key": outdated["key"], "selected": False},
            headers=headers,
        )
        unconfirmed = await client.post("/api/team-import/validate", json={}, headers=headers)
        assert unconfirmed.status == 400 and "Confirm schedule" in (await unconfirmed.json())["error"]
        schedule = next(r for r in review["records"] if r["key"] == "schedule/sched_morning")
        await client.post(
            "/api/team-import/record",
            json={"section": "schedules", "key": schedule["key"], "selected": True,
                  "text": schedule["text"], "confirmed": True},
            headers=headers,
        )
        validation = await (await client.post("/api/team-import/validate", json={}, headers=headers)).json()
        assert validation["valid"], validation
        imported = await client.post(
            "/api/team-import/import", json={"digest": validation["digest"]}, headers=headers
        )
        result = await imported.json()
        assert imported.status == 200, result
        await asyncio.sleep(0.6)

    assert result["restarting"] is True and service.completion_event.is_set()
    assert result["missing_secrets"] == []
    assert [c["key"] for c in result["credentials"]] == ["person:usr_ana/stored_value/github.sign_in"]
    assert all(c["waiting"] and not c["imported"] for c in result["credentials"])
    config = GatewayInstanceConfig.load_saved(root / "instance_config.yaml")
    facts = create_pinned_memory_repository(config.manager_storage_settings).list_coremem(team_id=TEAM)
    assert [fact.fact_text for fact in facts] == ["Keep the weekly report short"]
    assert ScheduleStore(storage_settings=config.manager_storage_settings).get_row("sched_morning") is not None
    # The copied export and its review folder are gone; the credentials wait, private, for the broker.
    assert not (root / "import" / "team.uatransfer").exists()
    assert not any(config.layout.team_import_review_dir.iterdir())
    handover = config.layout.team_import_credentials_path
    assert oct(handover.stat().st_mode & 0o777) == "0o600"
    assert PASSPHRASE not in handover.read_text() and GITHUB_TOKEN not in handover.read_text()

    # The first start: the new instance's own broker, with its own root token.
    _use_store(tmp_path / "new_store", monkeypatch)
    new_root_token = parse_env_file(root / "team.env")["SECURE_TOOL_BROKER_TOKEN"]
    broker = await _serving_broker(new_root_token, monkeypatch)
    try:
        report = await asyncio.to_thread(
            import_handed_over_credentials, str(root / "instance_config.yaml"), retry_delays=(0.0,)
        )
    finally:
        await broker.close()
    assert report is not None and all(outcome["imported"] for outcome in report.outcomes)
    assert read_credential_state("github.token", owner_id="usr_ana") == GITHUB_TOKEN
    assert not handover.exists()
    notices = AccessRepository(config.manager_storage_settings).notices_for(TEAM, person_id="usr_ana", roles=())
    assert [notice.title for notice, _read in notices] == ["Your sign-ins and keys moved to this instance"]
    assert list(config.layout.team_import_dir.glob("credentials-*.json"))


@pytest.mark.asyncio
async def test_import_is_refused_on_a_folder_that_already_holds_an_instance(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    exported = await _export(tmp_path, monkeypatch)
    root = tmp_path / "new"
    (root / "import").mkdir(parents=True)
    shutil.copy(exported, root / "import" / "team.uatransfer")
    (root / "instance_config.yaml").write_text("schema_version: 1\n")

    async with TestClient(TestServer(_onboarding(root).build_app())) as client:
        listing = await (await client.get("/api/team-import/exports")).json()
        opened = await client.post(
            "/api/team-import/open", json={"file_name": "team.uatransfer", "passphrase": PASSPHRASE}
        )
    assert "fresh data folder" in listing["problem"]
    assert opened.status == 409


@pytest.mark.asyncio
async def test_only_files_directly_in_the_import_folder_can_be_opened(tmp_path: Path) -> None:
    root = tmp_path / "new"
    (root / "import").mkdir(parents=True)
    outside = tmp_path / "elsewhere.uatransfer"
    outside.write_bytes(b"not an export")
    (root / "import" / "linked.uatransfer").symlink_to(outside)
    (root / "import" / ".hidden").write_bytes(b"x")
    (root / "import" / "notes.txt").write_text("not an export")

    async with TestClient(TestServer(_onboarding(root).build_app())) as client:
        listing = await (await client.get("/api/team-import/exports")).json()
        replies = {}
        for name in ("../elsewhere.uatransfer", "linked.uatransfer", ".hidden", "notes.txt"):
            response = await client.post("/api/team-import/open", json={"file_name": name, "passphrase": PASSPHRASE})
            replies[name] = (response.status, (await response.json())["error"])

    assert [item["file_name"] for item in listing["exports"]] == ["notes.txt"]
    assert replies["../elsewhere.uatransfer"] == (400, "Choose one of the listed export files.")
    assert replies["linked.uatransfer"] == (400, "linked.uatransfer is not a regular file.")
    assert replies[".hidden"] == (400, "Choose one of the listed export files.")
    assert replies["notes.txt"][0] == 400 and "not a transfer file" in replies["notes.txt"][1]


@pytest.mark.asyncio
async def test_opening_another_export_ends_the_earlier_review(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    exported = await _export(tmp_path, monkeypatch)
    root = tmp_path / "new"
    (root / "import").mkdir(parents=True)
    shutil.copy(exported, root / "import" / "team.uatransfer")

    async with TestClient(TestServer(_onboarding(root).build_app())) as client:
        body = {"file_name": "team.uatransfer", "passphrase": PASSPHRASE}
        first = (await (await client.post("/api/team-import/open", json=body)).json())["token"]
        second = (await (await client.post("/api/team-import/open", json=body)).json())["token"]
        stale = await client.get("/api/team-import/review", headers={"X-Transfer-Token": first})
        current = await client.get("/api/team-import/review", headers={"X-Transfer-Token": second})
    assert first != second
    assert stale.status == 403 and current.status == 200


async def _confirm_schedules(client, headers, review) -> None:
    for record in review["records"]:
        if record["section"] == "schedules" and record["text"] is not None:
            await client.post(
                "/api/team-import/record",
                json={"section": "schedules", "key": record["key"], "selected": True,
                      "text": record["text"], "confirmed": True},
                headers=headers,
            )


@pytest.mark.asyncio
async def test_environment_secrets_are_listed_and_the_team_waits_for_them(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A Docker Swarm host gives every secret as a Docker secret; the import says which are missing."""

    exported = await _export(tmp_path, monkeypatch)
    root = tmp_path / "new"
    (root / "import").mkdir(parents=True)
    shutil.copy(exported, root / "import" / "team.uatransfer")
    monkeypatch.delenv("SECURE_TOOL_BROKER_TOKEN", raising=False)
    monkeypatch.delenv("SECURE_TOOL_BROKER_TOKEN_FILE", raising=False)
    service = _onboarding(root)

    async with TestClient(TestServer(service.build_app())) as client:
        body = {"file_name": "team.uatransfer", "passphrase": PASSPHRASE}
        headers = {"X-Transfer-Token": (await (await client.post("/api/team-import/open", json=body)).json())["token"]}
        review = await (await client.get("/api/team-import/review", headers=headers)).json()
        chosen = await client.post(
            "/api/team-import/destination", json={"secrets_source": "environment"}, headers=headers
        )
        assert chosen.status == 200
        await _confirm_schedules(client, headers, review)
        validation = await (await client.post("/api/team-import/validate", json={}, headers=headers)).json()
        result = await (
            await client.post("/api/team-import/import", json={"digest": validation["digest"]}, headers=headers)
        ).json()

    assert result["restarting"] is False and not service.completion_event.is_set()
    assert "SECURE_TOOL_BROKER_TOKEN" in {item["name"] for item in result["missing_secrets"]}
    assert "Docker secrets" in result["message"]
    assert not (root / "team.env").exists()
    saved = GatewayInstanceConfig.load_saved(root / "instance_config.yaml")
    assert saved.launch.secrets_from_environment
    # The credentials still wait for the first start, which follows once the secrets are added.
    assert saved.layout.team_import_credentials_path.is_file()


@pytest.mark.asyncio
async def test_no_export_opens_while_an_import_runs(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from team_transfer.review_session import ReviewSession

    exported = await _export(tmp_path, monkeypatch)
    root = tmp_path / "new"
    (root / "import").mkdir(parents=True)
    shutil.copy(exported, root / "import" / "team.uatransfer")
    release = asyncio.Event()

    async def slow_apply(self, payload):
        from team_transfer.errors import TransferError

        await release.wait()
        raise TransferError("stopped by the test")

    monkeypatch.setattr(ReviewSession, "apply", slow_apply)
    async with TestClient(TestServer(_onboarding(root).build_app())) as client:
        body = {"file_name": "team.uatransfer", "passphrase": PASSPHRASE}
        headers = {"X-Transfer-Token": (await (await client.post("/api/team-import/open", json=body)).json())["token"]}
        importing = asyncio.create_task(client.post("/api/team-import/import", json={"digest": "d"}, headers=headers))
        await asyncio.sleep(0.05)
        refused = await client.post("/api/team-import/open", json=body)
        refusal = await refused.json()
        release.set()
        finished = await importing
        reopened = await client.post("/api/team-import/open", json=body)

    assert refused.status == 409 and "import is running" in refusal["error"]
    assert finished.status == 400
    assert reopened.status == 200, "a failed import lets the person try again"


async def _validated_import(client) -> tuple[dict[str, str], str]:
    opened = await client.post(
        "/api/team-import/open", json={"file_name": "team.uatransfer", "passphrase": PASSPHRASE}
    )
    assert opened.status == 200
    headers = {"X-Transfer-Token": (await opened.json())["token"]}
    review = await (await client.get("/api/team-import/review", headers=headers)).json()
    await _confirm_schedules(client, headers, review)
    validation = await (await client.post("/api/team-import/validate", json={}, headers=headers)).json()
    assert validation["valid"], validation
    return headers, validation["digest"]


@pytest.mark.asyncio
@pytest.mark.parametrize("failure_at", ["handover", "receipt"])
async def test_credential_handover_and_receipt_failures_leave_the_instance_unfinished(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, failure_at: str
) -> None:
    from runtime_settings.instance_layout import InstanceLayout
    from team_transfer import credential_handover, service as transfer_service

    exported = await _export(tmp_path, monkeypatch)
    root = tmp_path / "new"
    (root / "import").mkdir(parents=True)
    shutil.copy(exported, root / "import" / "team.uatransfer")
    layout = InstanceLayout.at(root)
    service = _onboarding(root)
    module = credential_handover if failure_at == "handover" else transfer_service
    failed_path = (
        layout.team_import_credentials_path
        if failure_at == "handover"
        else layout.team_import_receipt_path
    )
    write_json = module.write_private_json

    def fail_once(path, value):
        if path == failed_path:
            assert layout.team_import_marker_path.exists()
            raise OSError("injected import write failure")
        write_json(path, value)

    async with TestClient(TestServer(service.build_app())) as client:
        headers, digest = await _validated_import(client)
        monkeypatch.setattr(module, "write_private_json", fail_once)
        response = await client.post(
            "/api/team-import/import", json={"digest": digest}, headers=headers
        )
        assert response.status == 400
        assert "injected import write failure" in (await response.json())["error"]

    assert layout.instance_config_path.exists()
    assert layout.team_import_marker_path.exists()
    assert layout.team_import_credentials_path.exists() == (failure_at == "receipt")
    assert not service.completion_event.is_set()
    assert (root / "import" / "team.uatransfer").exists()
    with pytest.raises(TransferError, match="did not finish"):
        GatewayInstanceConfig.load_for_process(layout.instance_config_path)


@pytest.mark.asyncio
@pytest.mark.parametrize("restart_required", [False, True])
async def test_first_start_retries_temporary_credential_storage_failures(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, restart_required: bool
) -> None:
    from secure_tool_broker import credential_state_store

    exported = await _export(tmp_path, monkeypatch)
    root = tmp_path / "new"
    (root / "import").mkdir(parents=True)
    shutil.copy(exported, root / "import" / "team.uatransfer")
    async with TestClient(TestServer(_onboarding(root).build_app())) as client:
        headers, digest = await _validated_import(client)
        response = await client.post(
            "/api/team-import/import", json={"digest": digest}, headers=headers
        )
        assert response.status == 200

    config = GatewayInstanceConfig.load_saved(root / "instance_config.yaml")
    handover = config.layout.team_import_credentials_path
    assert handover.exists() and not (root / "import" / "team.uatransfer").exists()
    _use_store(tmp_path / "new_store", monkeypatch)
    original_write = credential_state_store.write_credential_state
    attempts = 0

    def temporary_storage_error(*args, **kwargs):
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            raise OSError("temporary credential store failure")
        return original_write(*args, **kwargs)

    monkeypatch.setattr(credential_state_store, "write_credential_state", temporary_storage_error)
    token = parse_env_file(root / "team.env")["SECURE_TOOL_BROKER_TOKEN"]
    broker = await _serving_broker(token, monkeypatch)
    try:
        if restart_required:
            with pytest.raises(TransferError, match="next start tries again"):
                await asyncio.to_thread(
                    import_handed_over_credentials, str(config.config_path), retry_delays=(0.0,)
                )
            assert handover.exists()
            assert credential_state_store.read_credential_state("github.token", owner_id="usr_ana") is None
        report = await asyncio.to_thread(
            import_handed_over_credentials,
            str(config.config_path),
            retry_delays=(0.0, 0.0),
        )
    finally:
        await broker.close()

    assert attempts == 2
    assert report is not None and all(outcome["imported"] for outcome in report.outcomes)
    assert credential_state_store.read_credential_state("github.token", owner_id="usr_ana") == GITHUB_TOKEN
    assert not handover.exists()
