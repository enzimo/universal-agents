from __future__ import annotations

import json
import subprocess
from pathlib import Path
from types import SimpleNamespace

import pytest

from secure_tool_broker import credential_catalog
from custom_tools.secure_cli_tool import _trusted_runtime_context
from secure_tool_broker.credential_catalog import (
    CatalogCredential,
    CatalogCredentialField,
    CatalogSourceRef,
    CredentialCatalog,
    load_catalog,
    save_catalog,
)
from secure_tool_broker.credential_catalog_store import (
    CredentialCatalogStore,
    CredentialOwnerScope,
)
from secure_tool_broker.credential_state import (
    active_credential_owner_id,
    credential_owner_context,
    credential_owner_environment,
)
from secure_tool_broker.credential_state_store import (
    read_credential_state,
    write_credential_state,
)
from secure_tool_broker.sealbox_tenants import sealbox_tenant_environment
from secure_tools.providers import ToolResult


def test_secure_cli_derives_owner_only_from_active_task_gateway_context(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    agent = SimpleNamespace(id="agent-1", team_id="team-1")
    task_meta = SimpleNamespace(
        conversation_id="conversation-1",
        gateway_context=SimpleNamespace(canonical_user_id="alice"),
    )
    monkeypatch.setattr(
        "custom_tools.runtime_context.get_agent_ref",
        lambda: agent,
    )
    monkeypatch.setattr(
        "custom_tools.runtime_context.active_task_context",
        lambda _agent: ("task-1", task_meta),
    )

    assert _trusted_runtime_context() == {
        "canonical_user_id": "alice",
        "principal_type": "user",
        "agent_id": "agent-1",
        "team_id": "team-1",
        "task_id": "task-1",
        "conversation_id": "conversation-1",
    }

    task_meta.gateway_context = SimpleNamespace(canonical_user_id="")
    assert _trusted_runtime_context() is None


def _catalog_with_label(label: str) -> CredentialCatalog:
    return CredentialCatalog(
        credentials={
            "same_key": CatalogCredential(
                display_name=label,
                purpose=f"Credential for {label}",
                fields={
                    "account": CatalogCredentialField(
                        kind="account_id",
                        visibility="metadata_visible",
                        value_source="agent_supplied_metadata",
                        value=label,
                    )
                },
            )
        }
    )


def test_owner_scoped_catalog_allows_same_key_for_different_owners(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    db_path = tmp_path / "credential_catalog.db"
    monkeypatch.setenv("SECURE_TOOL_BROKER_CREDENTIAL_CATALOG_DB_PATH", str(db_path))
    alice = CredentialOwnerScope.user("alice")
    bob = CredentialOwnerScope.user("bob")

    save_catalog(_catalog_with_label("alice-account"), scope=alice)
    save_catalog(_catalog_with_label("bob-account"), scope=bob)

    assert (
        load_catalog(scope=alice).credentials["same_key"].display_name
        == "alice-account"
    )
    assert load_catalog(scope=bob).credentials["same_key"].display_name == "bob-account"
    with CredentialCatalogStore(db_path)._connect() as conn:
        rows = conn.execute(
            "SELECT scope_kind, owner_id, credential_key FROM credential_catalog_entries ORDER BY owner_id"
        ).fetchall()
    assert [tuple(row) for row in rows] == [
        ("user", "alice", "same_key"),
        ("user", "bob", "same_key"),
    ]


def test_catalog_entry_writes_do_not_replace_sibling_credentials(
    tmp_path: Path,
) -> None:
    store = CredentialCatalogStore(tmp_path / "credential_catalog.db")
    owner = CredentialOwnerScope.user("alice")

    store.put_credential(owner, "first", {"display_name": "First", "fields": {}})
    stale_snapshot = store.load_scope(owner)
    store.put_credential(owner, "second", {"display_name": "Second", "fields": {}})
    store.put_credential(
        owner, "first", {**stale_snapshot["first"], "purpose": "Updated"}
    )

    stored = store.load_scope(owner)
    assert sorted(stored) == ["first", "second"]
    assert stored["first"]["purpose"] == "Updated"
    assert store.delete_credential(owner, "first") is True
    assert store.delete_credential(owner, "first") is False
    assert sorted(store.load_scope(owner)) == ["second"]


def test_owner_scoped_catalog_resolution_activates_provider_owner(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    observations: list[tuple[str | None, str | None]] = []
    monkeypatch.setenv("SECURE_TOOL_BROKER_SECRET_CACHE_TTL_SECONDS", "0")

    class OwnerAwareSource:
        def get_secret(self, request):
            observations.append((active_credential_owner_id(), request.owner_id))
            return f"secret-for-{active_credential_owner_id()}"

    monkeypatch.setattr(
        "secure_tool_broker.credential_catalog.get_credential_source",
        lambda _source: OwnerAwareSource(),
    )
    catalog = CredentialCatalog(
        credentials={
            "external_key": CatalogCredential(
                display_name="External",
                purpose="Verify owner-scoped provider resolution.",
                fields={
                    "token": CatalogCredentialField(
                        kind="token",
                        visibility="secret",
                        resolution_order=[
                            CatalogSourceRef(
                                source="bitwarden",
                                item_name="external-item",
                                field_ref="password",
                            )
                        ],
                    )
                },
            )
        }
    )

    resolved = credential_catalog.resolve_secret_field(
        "external_key",
        "token",
        catalog=catalog,
        scope=CredentialOwnerScope.user("bob"),
    )

    assert resolved.value == "secret-for-bob"
    assert observations == [("bob", "bob")]
    assert active_credential_owner_id() is None


def test_catalog_calls_that_name_no_owner_are_refused(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # There is no default owner; the old import owner is gone.
    monkeypatch.setenv("SECURE_TOOL_BROKER_CREDENTIAL_CATALOG_DB_PATH", str(tmp_path / "catalog.db"))

    result = credential_catalog.collect_credential({"credential_key": "same_key"}, {})

    assert result.status == "error"
    assert "must name its owner" in str(result.error)
    for call in (
        lambda: credential_catalog.get_credential_metadata("same_key"),
        lambda: credential_catalog.resolve_secret_field("same_key", "password"),
    ):
        with pytest.raises(ValueError, match="must name its owner"):
            call()


def test_sealbox_tenant_provisioning_is_opaque_and_idempotent(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root_token = tmp_path / "root-token"
    root_token.write_text("root-secret\n", encoding="utf-8")
    monkeypatch.setenv("SEALBOX_URL", "http://sealbox.test")
    monkeypatch.setenv("SEALBOX_ROOT_TOKEN_FILE", str(root_token))
    monkeypatch.setenv("SEALBOX_CLI_PATH", "/usr/local/bin/sealbox-cli")
    monkeypatch.setenv(
        "SECURE_TOOL_BROKER_CREDENTIAL_CATALOG_DB_PATH", str(tmp_path / "catalog.db")
    )
    monkeypatch.setenv(
        "SECURE_TOOL_BROKER_SEALBOX_TENANT_MATERIAL_DIR", str(tmp_path / "tenants")
    )
    calls: list[list[str]] = []

    def fake_run_sealbox(args, *, input_text=None, env=None, timeout_seconds=30):
        calls.append(list(args))
        assert input_text is None
        assert timeout_seconds > 0
        if args[:2] == ["tenant", "create"]:
            token_path = Path(args[args.index("--token-file") + 1])
            token_path.write_text(f"tenant-token-{len(calls)}\n", encoding="utf-8")
            tenant_id = f"ten_test_{len([call for call in calls if call[:2] == ['tenant', 'create']])}"
            return subprocess.CompletedProcess(
                ["sealbox-cli", *args],
                0,
                stdout=json.dumps(
                    {"tenant": {"id": tenant_id}, "token_file": str(token_path)}
                ),
                stderr="",
            )
        if args[:2] in (["key", "register"], ["key", "status"]):
            return subprocess.CompletedProcess(
                ["sealbox-cli", *args],
                0,
                stdout=json.dumps({"master_key": {"id": "key-test"}}),
                stderr="",
            )
        raise AssertionError(args)

    monkeypatch.setattr(
        "secure_tool_broker.sealbox_tenants.run_sealbox", fake_run_sealbox
    )
    alice = sealbox_tenant_environment(
        scope_kind="user", owner_id="alice@example", provision=True
    )
    alice_again = sealbox_tenant_environment(
        scope_kind="user", owner_id="alice@example", provision=True
    )
    bob = sealbox_tenant_environment(
        scope_kind="user", owner_id="bob@example", provision=True
    )

    assert isinstance(alice, dict)
    assert isinstance(alice_again, dict)
    assert isinstance(bob, dict)
    assert alice == alice_again
    assert alice["SEALBOX_API_VERSION"] == "v2"
    assert "SEALBOX_ROOT_TOKEN_FILE" not in alice
    assert alice["SEALBOX_TOKEN_FILE"] != bob["SEALBOX_TOKEN_FILE"]
    assert "alice@example" not in alice["SEALBOX_TOKEN_FILE"]
    assert "bob@example" not in bob["SEALBOX_TOKEN_FILE"]
    assert len([call for call in calls if call[:2] == ["tenant", "create"]]) == 2
    missing = sealbox_tenant_environment(
        scope_kind="user", owner_id="charlie", provision=False
    )
    assert isinstance(missing, ToolResult)
    assert missing.content["error_code"] == "sealbox_tenant_not_provisioned"


def test_unconfigured_sealbox_provisioning_has_no_material_side_effect(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    material_dir = tmp_path / "tenants"
    monkeypatch.setenv(
        "SECURE_TOOL_BROKER_CREDENTIAL_CATALOG_DB_PATH", str(tmp_path / "catalog.db")
    )
    monkeypatch.setenv(
        "SECURE_TOOL_BROKER_SEALBOX_TENANT_MATERIAL_DIR", str(material_dir)
    )
    monkeypatch.delenv("SEALBOX_URL", raising=False)
    monkeypatch.delenv("SEALBOX_ROOT_TOKEN_FILE", raising=False)

    result = sealbox_tenant_environment(
        scope_kind="user",
        owner_id="unconfigured-user",
        provision=True,
    )

    assert isinstance(result, ToolResult)
    assert result.content["error_code"] == "sealbox_root_not_configured"
    assert not material_dir.exists()


def test_provider_state_files_and_process_environment_are_owner_scoped(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root = tmp_path / "broker_credentials"
    monkeypatch.setenv("SECURE_TOOL_BROKER_CREDENTIALS_DIR", str(root))
    monkeypatch.setenv("SECURE_TOOL_BROKER_CREDENTIAL_STATE_BACKEND_MODE", "files")
    monkeypatch.setenv("GH_TOKEN", "ambient-github-token")
    monkeypatch.setenv("BW_SESSION", "ambient-bitwarden-session")
    monkeypatch.setenv("SEALBOX_ROOT_TOKEN_FILE", "/broker/sealbox/root-token")
    monkeypatch.delenv("GH_CONFIG_DIR", raising=False)
    monkeypatch.delenv("OP_CONFIG_DIR", raising=False)

    with credential_owner_context("alice"):
        assert write_credential_state("github.token", "alice-token").status == "success"
        alice_env = credential_owner_environment()
    with credential_owner_context("bob"):
        assert write_credential_state("github.token", "bob-token").status == "success"
        bob_env = credential_owner_environment()
    with credential_owner_context("alice"):
        assert read_credential_state("github.token") == "alice-token"
    with credential_owner_context("bob"):
        assert read_credential_state("github.token") == "bob-token"

    assert alice_env["GH_CONFIG_DIR"] != bob_env["GH_CONFIG_DIR"]
    assert "alice" not in alice_env["GH_CONFIG_DIR"]
    assert "bob" not in bob_env["GH_CONFIG_DIR"]
    assert "GH_TOKEN" not in alice_env
    assert "BW_SESSION" not in alice_env
    assert "SEALBOX_ROOT_TOKEN_FILE" not in alice_env
    state_files = sorted(root.glob("users/owner-*/gh/token"))
    assert len(state_files) == 2
    assert {path.read_text(encoding="utf-8").strip() for path in state_files} == {
        "alice-token",
        "bob-token",
    }
