"""The broker deletes a removed person's stored credentials, for the gateway only.

Real broker app and credential catalog, with the in-memory Sealbox of the
account link tests: a person links an account with a new credential through
the broker's form, then the gateway asks the broker to delete that person's
credentials (``secure_tool_broker.departed_person_credentials``). The secret
leaves Sealbox and the catalog; another process's token is refused; the
team's credentials are untouched.

The sign-in tests use the real file-based credential state: the person's
GitHub, Google and X sign-ins and their private credentials folder are
deleted, another person's and the team's stay, links planted in the folder
are not followed, and a repeat is harmless. The live Sealbox store is
covered in ``tests/test_live_sealbox_integration.py``.
"""

from __future__ import annotations

import os
import subprocess
from pathlib import Path
from urllib.parse import urlsplit

import pytest
from aiohttp.test_utils import TestClient, TestServer

from custom_messages.contracts.access_v2 import (
    CredentialsFolderOutcome,
    PersonCredentialDeletionResultV1,
    PersonCredentialDeletionV1,
    SignInService,
)
from secure_tool_broker import credential_catalog
from secure_tool_broker import credential_state_store
from secure_tool_broker.credential_state import (
    broker_credentials_root,
    credential_owner_context,
    person_credential_parts,
)
from secure_tool_broker.credential_state_store import (
    STATE_KEY_PATHS,
    STATE_KEY_SERVICES,
    read_credential_state,
    write_credential_state,
)
from secure_tool_broker.departed_person_credentials import delete_person_credentials
from secure_tool_broker.google_workspace import store_person_client, store_person_token
from secure_tool_broker.x_sign_in import store_person_sign_in
from secure_tool_broker.credential_sources import sealbox as sealbox_source
from secure_tool_broker.credential_catalog import load_catalog
from secure_tool_broker.credential_catalog_store import CredentialOwnerScope
from secure_tool_broker.defaults import PERSON_CREDENTIAL_DELETION_ROUTE
from secure_tool_broker.service import SecureToolBrokerService
from tests.broker_access_support import TEST_TEAM, caller_headers, gateway_headers
from tests.test_account_links import (  # noqa: F401 - pytest fixtures
    _isolated_credentials,
    link_request,
    new_credential_form,
    sealbox,
    world,
)


@pytest.fixture
def deletable_sealbox(sealbox, monkeypatch):  # noqa: F811 - the imported pytest fixture
    """The in-memory Sealbox, also answering ``secret delete``."""

    stored = credential_catalog.run_sealbox

    def run(args, *, input_text=None, env=None, timeout_seconds=30):
        if list(args[:2]) in (["secret", "delete"], ["credential", "delete"]):
            sealbox.pop(args[2], None)
            return subprocess.CompletedProcess(["sealbox-cli", *args], 0, stdout='{"deleted":true}\n', stderr="")
        return stored(args, input_text=input_text, env=env, timeout_seconds=timeout_seconds)

    monkeypatch.setattr(credential_catalog, "run_sealbox", run)
    monkeypatch.setattr(sealbox_source, "run_sealbox", run)
    return sealbox


async def _link_new_credential(broker: TestClient, *, owner_kind: str, request_id: str, value: str) -> None:
    roles = ("owner",) if owner_kind == "team" else ("member",)
    created = await broker.post(
        "/v1/account-link/sessions", headers=gateway_headers(), json=link_request(owner_kind, roles, request_id)
    )
    assert created.status == 200
    form_path = urlsplit((await created.json())["form_url"]).path
    submitted = await broker.post(form_path, data=new_credential_form(value))
    assert submitted.status == 200


@pytest.mark.asyncio
async def test_the_gateway_deletes_a_removed_persons_credentials(world, deletable_sealbox) -> None:  # noqa: F811
    gate, store = world
    service = SecureToolBrokerService(token="test-token", access_gate=gate)
    async with TestClient(TestServer(service.build_app())) as broker:
        await _link_new_credential(broker, owner_kind="person", request_id="req-person", value="person-secret")
        await _link_new_credential(broker, owner_kind="team", request_id="req-team", value="team-secret")
        person_keys = set(load_catalog(scope=CredentialOwnerScope.user("user-1")).credentials)
        request = PersonCredentialDeletionV1(team_id=TEST_TEAM, person_id="user-1", credential_keys=tuple(person_keys))

        refused = await broker.post(
            PERSON_CREDENTIAL_DELETION_ROUTE, headers=caller_headers(), json=request.model_dump(mode="json")
        )
        other_team = await broker.post(
            PERSON_CREDENTIAL_DELETION_ROUTE,
            headers=gateway_headers(),
            json=request.model_copy(update={"team_id": "another-team"}).model_dump(mode="json"),
        )
        deleted = await broker.post(
            PERSON_CREDENTIAL_DELETION_ROUTE, headers=gateway_headers(), json=request.model_dump(mode="json")
        )

        assert refused.status == 403 and other_team.status == 403
        assert deleted.status == 200
        result = PersonCredentialDeletionResultV1.model_validate(await deleted.json())
    assert set(result.deleted) == person_keys and person_keys and result.failed == ()
    assert load_catalog(scope=CredentialOwnerScope.user("user-1")).credentials == {}
    assert list(deletable_sealbox.values()) == ["team-secret"]
    assert set(load_catalog(scope=CredentialOwnerScope.team(TEST_TEAM)).credentials)
    assert len(store.list_links(TEST_TEAM)) == 2  # the gateway revokes links; the broker only deletes secrets


# -- sign-ins and the private credentials folder (real file-based store) -----------------

MIA = "user-mia"
OMAR = "user-omar"
PERSON_SIGN_INS = ("github.token", "gog.token_json", "gog.oauth_client_credentials_json", "x.sign_in_json", "xurl.sessions_json")


def _sign_in_everywhere(person_id: str) -> None:
    """GitHub, Google and X sign-ins through the broker's own store functions."""

    assert write_credential_state("github.token", f"ghp-{person_id}", owner_id=person_id).status == "success"
    assert store_person_client(person_id, f'{{"installed":{{"client_id":"{person_id}"}}}}').status == "success"
    assert store_person_token(person_id, f'{{"refresh_token":"{person_id}"}}').status == "success"
    assert store_person_sign_in(person_id, f'{{"username":"{person_id}"}}').status == "success"
    assert write_credential_state("xurl.sessions_json", "{}", owner_id=person_id).status == "success"


def _team_values() -> None:
    with credential_owner_context(TEST_TEAM, scope_kind="team"):
        assert write_credential_state("x.oauth_client_json", "team-x-app").status == "success"
        assert write_credential_state("gog.oauth_client_credentials_json", "team-google-client").status == "success"


def _stored(person_id: str) -> dict[str, object]:
    return {key: read_credential_state(key, owner_id=person_id) for key in PERSON_SIGN_INS}


def _person_folder(person_id: str) -> Path:
    root = broker_credentials_root()
    assert root is not None
    return root.joinpath(*person_credential_parts(person_id))


def test_every_credential_state_key_names_its_service() -> None:
    assert set(STATE_KEY_SERVICES) == set(STATE_KEY_PATHS)


@pytest.mark.asyncio
async def test_removal_deletes_the_persons_sign_ins_and_folder_only(world, tmp_path: Path) -> None:  # noqa: F811
    gate, _store = world
    _sign_in_everywhere(MIA)
    _sign_in_everywhere(OMAR)
    _team_values()
    folder = _person_folder(MIA)
    outside_file = tmp_path / "outside.txt"
    outside_file.write_text("not hers", encoding="utf-8")
    outside_folder = tmp_path / "outside_folder"
    outside_folder.mkdir()
    (outside_folder / "kept.txt").write_text("kept", encoding="utf-8")
    os.symlink(outside_file, folder / "gh" / "hosts.yml")
    os.symlink(outside_folder, folder / "planted_folder")
    request = PersonCredentialDeletionV1(team_id=TEST_TEAM, person_id=MIA)
    service = SecureToolBrokerService(token="test-token", access_gate=gate)

    async with TestClient(TestServer(service.build_app())) as broker:
        first = await broker.post(PERSON_CREDENTIAL_DELETION_ROUTE, headers=gateway_headers(), json=request.model_dump(mode="json"))
        repeat = await broker.post(PERSON_CREDENTIAL_DELETION_ROUTE, headers=gateway_headers(), json=request.model_dump(mode="json"))
        assert first.status == 200 and repeat.status == 200
        result = PersonCredentialDeletionResultV1.model_validate(await first.json())
        repeated = PersonCredentialDeletionResultV1.model_validate(await repeat.json())

    assert result.sign_ins_deleted == (SignInService.GITHUB, SignInService.GOOGLE, SignInService.X)
    assert result.sign_ins_failed == () and result.folder is CredentialsFolderOutcome.DELETED
    assert not folder.exists()
    assert all(value is None for value in _stored(MIA).values())
    # Links inside were deleted as links; what they point at is untouched.
    assert outside_file.read_text(encoding="utf-8") == "not hers"
    assert (outside_folder / "kept.txt").read_text(encoding="utf-8") == "kept"
    # Another person's sign-ins and the team's stay.
    assert _stored(OMAR)["github.token"] == f"ghp-{OMAR}" and all(_stored(OMAR).values())
    with credential_owner_context(TEST_TEAM, scope_kind="team"):
        assert read_credential_state("x.oauth_client_json") == "team-x-app"
        assert read_credential_state("gog.oauth_client_credentials_json") == "team-google-client"
    # A repeat finds nothing more and fails nothing.
    assert repeated == PersonCredentialDeletionResultV1(folder=CredentialsFolderOutcome.ABSENT)


def test_a_link_in_place_of_the_persons_folder_is_refused_not_followed(tmp_path: Path) -> None:
    target = tmp_path / "elsewhere"
    target.mkdir()
    (target / "kept.txt").write_text("kept", encoding="utf-8")
    folder = _person_folder(MIA)
    folder.parent.mkdir(parents=True, exist_ok=True)
    os.symlink(target, folder)

    result = delete_person_credentials(PersonCredentialDeletionV1(team_id=TEST_TEAM, person_id=MIA))

    assert result.folder is CredentialsFolderOutcome.FAILED and result.sign_ins_failed == ()
    assert folder.is_symlink() and (target / "kept.txt").read_text(encoding="utf-8") == "kept"


def test_a_sign_in_that_cannot_be_deleted_keeps_the_folder_for_a_retry(monkeypatch: pytest.MonkeyPatch) -> None:
    _sign_in_everywhere(MIA)
    monkeypatch.setenv("SECURE_TOOL_BROKER_CREDENTIAL_STATE_BACKEND_MODE", "credential_source")
    monkeypatch.setenv("SECURE_TOOL_BROKER_CREDENTIAL_STATE_BACKEND_SOURCE", "vault")
    monkeypatch.setattr(credential_state_store.shutil, "which", lambda _name: None)  # no vault CLI

    result = delete_person_credentials(PersonCredentialDeletionV1(team_id=TEST_TEAM, person_id=MIA))

    assert result.sign_ins_failed == (SignInService.GITHUB, SignInService.GOOGLE, SignInService.MODEL_KEYS, SignInService.X)
    assert result.sign_ins_deleted == () and result.folder is CredentialsFolderOutcome.KEPT
    assert _person_folder(MIA).is_dir()
