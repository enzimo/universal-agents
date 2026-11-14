"""``/upgrade``'s export, end to end with a real broker: page, request, export process, result.

The broker is the real app on a loopback port (file credential backend), with
one stored sign-in. The owner's page is opened as the gateway opens it and
submitted as the browser does; the export process
(``team_transfer.requested_export``) then collects the passphrase over HTTP as
the operator, writes the archive into ``exports/`` and reports the result,
which the page and its status route show. Also covered: a person who is no
longer an owner when the export runs gets a refusal on the page and no file;
the file is given to the data folder's owner when the export runs as root; and
the request and result helpers (``team_transfer.export_requests``).
"""

from __future__ import annotations

import asyncio
import json
import os
from datetime import datetime, timezone
from pathlib import Path

import aiohttp
import pytest
from aiohttp.test_utils import TestServer

from access_control.caller_tokens import CALLER_GATEWAY, derive_caller_token
from custom_messages.contracts.access_v2 import PersonStanding, TeamPersonV1
from runtime_settings.instance_layout import InstanceLayout
from secure_tool_broker.service import SecureToolBrokerService
from storage.access_repository import AccessRepository
from storage.gateway_state_repository import create_gateway_state_repository
from team_transfer import crypto, requested_export
from team_transfer.broker_credentials import BrokerCredentialsSection
from team_transfer.container import TransferReader
from team_transfer.export_requests import (
    EXPORT_REQUESTS_BY_SUPERVISOR,
    EXPORT_REQUESTS_ENV,
    TeamExportRequestV1,
    TeamExportResultV1,
    export_file_name,
    export_sections,
    section_lines,
    size_words,
    supervisor_runs_exports,
    take_export_request,
    write_export_request,
)
from team_transfer.people import PeopleSection
from team_transfer.settings import SettingsSection
from tests.broker_access_support import open_gate
from tests.pinned_memory_support import new_instance
from tests.test_team_transfer_broker_credentials import _use_store

TEAM = "team_alpha"
ROOT_TOKEN = "instance-broker-root"
PASSPHRASE = "correct horse battery staple"
GITHUB_TOKEN = "gho_owner_github_sign_in_value"


def _instance(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """A running team: two people (an owner and a member), one stored sign-in, the broker root in team.env."""

    from secure_tool_broker.credential_state_store import write_credential_state

    path, config = new_instance(tmp_path, "alpha", TEAM)
    config.heartbeat.enabled = False
    config.save(path)
    (path.parent / "team.env").write_text(f"SECURE_TOOL_BROKER_TOKEN={ROOT_TOKEN}\n")
    now = datetime.now(timezone.utc)
    AccessRepository(config.manager_storage_settings).replace_people(
        TEAM,
        [
            TeamPersonV1(team_id=TEAM, person_id="usr_owner", standing=PersonStanding.ACTIVE, roles=("owner", "admin"), changed_at_utc=now),
            TeamPersonV1(team_id=TEAM, person_id="usr_member", standing=PersonStanding.ACTIVE, roles=("member",), changed_at_utc=now),
        ],
        at=now,
    )
    people = create_gateway_state_repository(config.state_storage_settings)
    for person, name, role in (("usr_owner", "Ana Lima", "owner"), ("usr_member", "Ben Ode", "member")):
        people.create_gateway_user(canonical_user_id=person, display_name=name, timezone="UTC")
        people.create_team_membership(canonical_user_id=person, team_id=TEAM, role=role)
    _use_store(tmp_path / "store", monkeypatch)
    assert write_credential_state("github.token", GITHUB_TOKEN, owner_id="usr_owner").status == "success"
    monkeypatch.setenv("TEAM_INSTANCE_ROOT", str(path.parent))
    monkeypatch.setenv(EXPORT_REQUESTS_ENV, EXPORT_REQUESTS_BY_SUPERVISOR)
    import team_transfer.registry as registry

    monkeypatch.setattr(registry, "TRANSFER_SECTIONS", (SettingsSection(), PeopleSection(), BrokerCredentialsSection()))
    monkeypatch.setattr(crypto, "DEFAULT_OPSLIMIT", 1)
    monkeypatch.setattr(crypto, "DEFAULT_MEMLIMIT", 8192)
    return path


async def _serving_broker(monkeypatch: pytest.MonkeyPatch) -> TestServer:
    service = SecureToolBrokerService(token=ROOT_TOKEN, access_gate=open_gate(team_id=TEAM))
    server = TestServer(service.build_app(), host="127.0.0.1")
    await server.start_server()
    monkeypatch.setenv("SECURE_TOOL_BROKER_INTERNAL_BASE_URL", str(server.make_url("")).rstrip("/"))
    return server


async def _submitted_page(server: TestServer, person: str) -> str:
    """Open the export page as the gateway does (vouching owner roles) and submit a passphrase."""

    gateway = {"Authorization": f"Bearer {derive_caller_token(ROOT_TOKEN, CALLER_GATEWAY)}"}
    context = {"canonical_user_id": person, "principal_type": "user", "team_id": TEAM, "person_roles": ["owner"]}
    async with aiohttp.ClientSession() as client:
        created = await client.post(server.make_url("/v1/auth/team/sessions"), headers=gateway, json={"context": context, "method": "export"})
        assert created.status == 200, await created.text()
        page = "/" + (await created.json())["login_url"].split("/", 3)[3]
        submitted = await client.post(
            server.make_url(page), data={"action": "export", "passphrase": PASSPHRASE, "passphrase_again": PASSPHRASE}
        )
        assert "Exporting the team" in await submitted.text()
    return page


async def _page_status(server: TestServer, page: str) -> dict:
    async with aiohttp.ClientSession() as client:
        response = await client.get(server.make_url(f"{page}/status"))
        return await response.json()


@pytest.mark.asyncio
async def test_an_owners_export_collects_the_passphrase_writes_the_archive_and_ends_the_page(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config_path = _instance(tmp_path, monkeypatch)
    layout = InstanceLayout.at(config_path.parent)
    server = await _serving_broker(monkeypatch)
    try:
        page = await _submitted_page(server, "usr_owner")
        request = take_export_request(layout.team_export_requests_dir)
        assert request is not None and request.owner_person_id == "usr_owner"

        result = await asyncio.to_thread(
            requested_export.run_requested_export,
            config_path,
            export_id=request.export_id,
            owner_person_id=request.owner_person_id,
            now=datetime(2026, 10, 14, 10, 15, tzinfo=timezone.utc),
        )
        status = await _page_status(server, page)
    finally:
        await server.close()

    assert result.ok, result.error
    archive = layout.exports_dir / "alpha-20261014-101500Z.uatransfer"
    assert result.file_name == archive.name and result.size_bytes == archive.stat().st_size
    assert archive.stat().st_mode & 0o777 == 0o600 and layout.exports_dir.stat().st_mode & 0o777 == 0o700
    assert [(section.name, section.records) for section in result.sections] == [
        ("settings", result.sections[0].records),
        ("people", 2),
        ("broker_credentials", 1),
    ]
    with TransferReader.open(archive, PASSPHRASE) as reader:
        assert reader.manifest.exported_by.owner_person_id == "usr_owner"
    assert status["status"] == "success" and status["result"]["file_name"] == archive.name
    # Nothing on disk holds the passphrase or the sign-in it protects.
    for path in layout.root.rglob("*"):
        if path.is_file() and path.suffix != ".uatransfer":
            data = path.read_bytes()
            assert PASSPHRASE.encode() not in data and GITHUB_TOKEN.encode() not in data, path


@pytest.mark.asyncio
async def test_a_person_who_is_no_longer_an_owner_gets_a_refusal_and_no_file(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    import assistant_cli
    from team_transfer.cli import run_team_command

    config_path = _instance(tmp_path, monkeypatch)
    layout = InstanceLayout.at(config_path.parent)
    server = await _serving_broker(monkeypatch)
    try:
        # The gateway vouched owner roles when the page opened; the stored record says member now.
        page = await _submitted_page(server, "usr_member")
        request = take_export_request(layout.team_export_requests_dir)
        assert request is not None
        args = assistant_cli.build_parser().parse_args(
            ["team", "export-requested", "--config", str(config_path), "--export-id", request.export_id, "--owner", "usr_member"]
        )
        with pytest.raises(SystemExit, match="The team was not exported. usr_member is not an active owner"):
            await asyncio.to_thread(run_team_command, args)
        status = await _page_status(server, page)
    finally:
        await server.close()

    assert status["status"] == "failed" and "is not an active owner" in status["result"]["error"]
    assert not layout.exports_dir.exists() or not any(layout.exports_dir.iterdir())


def test_the_file_goes_to_the_data_folders_owner_when_the_export_runs_as_root(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls: list[tuple[str, int, int]] = []
    archive = tmp_path / "exports" / "alpha.uatransfer"
    archive.parent.mkdir()
    archive.write_bytes(b"x")
    monkeypatch.setattr(os, "geteuid", lambda: 0)
    monkeypatch.setattr(os, "chown", lambda path, uid, gid, follow_symlinks=True: calls.append((Path(path).name, uid, gid)))
    folder_owner = (os.stat(tmp_path).st_uid, os.stat(tmp_path).st_gid)

    requested_export._give_to_data_folder_owner((archive.parent, archive), tmp_path)

    if folder_owner == (0, 0):
        assert calls == []
    else:
        assert calls == [("exports", *folder_owner), ("alpha.uatransfer", *folder_owner)]


def test_a_root_owned_data_folder_keeps_root_as_the_owner(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[object] = []
    monkeypatch.setattr(os, "geteuid", lambda: 0)
    monkeypatch.setattr(os, "chown", lambda *args, **kwargs: calls.append(args))

    class _RootOwned:
        st_uid = 0
        st_gid = 0

    class _Root:
        def stat(self) -> _RootOwned:
            return _RootOwned()

    requested_export._give_to_data_folder_owner((tmp_path,), _Root())  # type: ignore[arg-type]

    assert calls == []


# -- requests and results ------------------------------------------------------------


def _request(export_id: str, minute: int) -> TeamExportRequestV1:
    return TeamExportRequestV1(
        export_id=export_id,
        team_id=TEAM,
        owner_person_id="usr_owner",
        requested_at_utc=datetime(2026, 10, 14, 10, minute, tzinfo=timezone.utc),
    )


def test_requests_are_taken_oldest_first_once_and_invalid_ones_are_removed(tmp_path: Path) -> None:
    requests_dir = tmp_path / "runtime" / "team-export" / "requests"
    write_export_request(requests_dir, _request("exp_second_aaaaaaaaaaaaaaaa", 20))
    write_export_request(requests_dir, _request("exp_first_bbbbbbbbbbbbbbbbb", 10))
    (requests_dir / "00000000T000000000000Z-broken.json").write_text("{not json")
    assert requests_dir.stat().st_mode & 0o777 == 0o700

    first = take_export_request(requests_dir)
    second = take_export_request(requests_dir)

    assert first is not None and first.export_id == "exp_first_bbbbbbbbbbbbbbbbb"
    assert second is not None and second.export_id == "exp_second_aaaaaaaaaaaaaaaa"
    assert take_export_request(requests_dir) is None and list(requests_dir.iterdir()) == []
    assert take_export_request(tmp_path / "missing") is None


def test_a_request_holds_no_secret_and_names_a_valid_export(tmp_path: Path) -> None:
    path = write_export_request(tmp_path, _request("exp_only_cccccccccccccccccc", 5))

    assert json.loads(path.read_text()) == {
        "schema_version": 1,
        "export_id": "exp_only_cccccccccccccccccc",
        "team_id": TEAM,
        "owner_person_id": "usr_owner",
        "requested_at_utc": "2026-10-14T10:05:00Z",
    }
    with pytest.raises(ValueError):
        _request("not-an-export-id", 5)


def test_names_sizes_and_parts_read_plainly() -> None:
    assert export_file_name("Alpha Team/../x", datetime(2026, 10, 14, 10, 15, 3, tzinfo=timezone.utc)) == (
        "Alpha-Team-x-20261014-101503Z.uatransfer"
    )
    assert export_file_name("///", datetime(2026, 1, 2, tzinfo=timezone.utc)) == "team-20260102-000000Z.uatransfer"
    assert [size_words(value) for value in (812, 14_000, 3_355_443, 1_503_238_554)] == [
        "812 bytes",
        "14 KB",
        "3.2 MB",
        "1.4 GB",
    ]
    result = TeamExportResultV1.model_validate(
        {
            "export_id": "exp_only_cccccccccccccccccc",
            "team_id": TEAM,
            "owner_person_id": "usr_owner",
            "ok": True,
            "sections": [
                {"name": "people", "records": 1, "size_bytes": 812},
                {"name": "broker_credentials", "records": 3, "size_bytes": 14_000, "encrypted": True},
            ],
        }
    )
    assert section_lines(result) == (
        "People: 1 record, 812 bytes",
        "Stored sign-ins and keys: 3 records, 14 KB (encrypted)",
    )


def test_each_parts_size_counts_its_document_files_and_ciphertext() -> None:
    from team_transfer.format import ExporterV1, KeyDerivationV1, MemberV1, SectionEntryV1, TransferManifestV1

    digest = "0" * 64
    manifest = TransferManifestV1(
        application_version="0.9.3",
        exported_at_utc="2026-10-14T10:15:00Z",
        team_id=TEAM,
        instance_name="alpha",
        exported_by=ExporterV1(owner_person_id="usr_owner"),
        key_derivation=KeyDerivationV1(salt_b64="c2FsdA==", opslimit=1, memlimit=8192),
        sections=(
            SectionEntryV1(name="skills", schema_version=1, record_count=2),
            SectionEntryV1(name="broker_credentials", schema_version=2, record_count=1, encrypted=True),
        ),
        members=(
            MemberV1(path="sections/skills.json", size=100, sha256=digest),
            MemberV1(path="files/skills/a/SKILL.md", size=40, sha256=digest),
            MemberV1(path="files/skills/b/SKILL.md", size=60, sha256=digest),
            MemberV1(path="credentials/broker_credentials.enc", size=900, sha256=digest),
        ),
    )

    sections = export_sections(manifest)

    assert [(item.name, item.records, item.size_bytes, item.encrypted) for item in sections] == [
        ("skills", 2, 200, False),
        ("broker_credentials", 1, 900, True),
    ]


def test_only_the_launcher_marks_its_processes() -> None:
    assert supervisor_runs_exports({EXPORT_REQUESTS_ENV: EXPORT_REQUESTS_BY_SUPERVISOR})
    assert not supervisor_runs_exports({})
    assert not supervisor_runs_exports({EXPORT_REQUESTS_ENV: "1"})


@pytest.mark.asyncio
async def test_an_export_stopped_by_shutdown_leaves_no_file_and_says_so(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import team_transfer.registry as registry

    config_path = _instance(tmp_path, monkeypatch)
    layout = InstanceLayout.at(config_path.parent)

    class _StoppedMidway(PeopleSection):
        def export(self, source, files):
            raise requested_export.ExportStopped("The instance stopped during the export.")

    monkeypatch.setattr(registry, "TRANSFER_SECTIONS", (SettingsSection(), _StoppedMidway()))
    server = await _serving_broker(monkeypatch)
    try:
        page = await _submitted_page(server, "usr_owner")
        request = take_export_request(layout.team_export_requests_dir)
        assert request is not None
        result = await asyncio.to_thread(
            requested_export.run_requested_export, config_path, export_id=request.export_id, owner_person_id="usr_owner"
        )
        status = await _page_status(server, page)
    finally:
        await server.close()

    assert not result.ok and result.error == "The instance stopped during the export."
    assert status["status"] == "failed" and status["result"]["error"] == "The instance stopped during the export."
    # Not even the hidden half-written file stays behind.
    assert list(layout.exports_dir.iterdir()) == []


def test_the_supervisors_stop_signal_becomes_a_clean_stop() -> None:
    import signal

    previous = signal.getsignal(signal.SIGTERM)
    try:
        requested_export.stop_on_signal()
        with pytest.raises(requested_export.ExportStopped, match="The instance stopped during the export."):
            os.kill(os.getpid(), signal.SIGTERM)
            for _ in range(1000):  # the handler runs at the next bytecode boundary
                pass
    finally:
        signal.signal(signal.SIGTERM, previous)
