"""Tests for Sealbox SSH credential templates, private ssh-agent material, sandbox isolation, and scrubbing."""

from __future__ import annotations

import base64
from datetime import datetime, timedelta, timezone
import getpass
import logging
import os
from pathlib import Path
import shutil
import socket
import subprocess
import time

import pytest

from custom_messages.contracts.common_v1 import contract_sha256
from custom_tools.code_sandbox import (
    code_sandbox_status,
    prepare_sandboxed_command,
    release_sandboxed_command,
)
from runtime_hosting.artifacts import FilesystemArtifactStore
from runtime_hosting.authorization import ExecutionAccessGuard
from runtime_hosting.execution_contracts import (
    ExecutionActivationKind,
    InteractiveSessionV1,
    OwnerScopeKind,
    OwnerScopeV1,
    SSHConnectionTargetV1,
)
from runtime_hosting.ssh import (
    CatalogSSHCredentialMaterialProvider,
    CompositeSSHCredentialMaterialProvider,
    ConfiguredSSHCredentialMaterialProvider,
    SSHCredentialMaterial,
    SSHSessionBroker,
    SessionSecretScrubber,
    sweep_stale_ssh_ephemeral_dirs,
)
from runtime_hosting.ssh_credentials import (
    FORBIDDEN_EXTRA_SECRET_FIELDS,
    is_short_secret,
)
from runtime_hosting.ssh_network import ValidatedSSHHost, ssh_host_key_alias
from runtime_settings.instance_layout import InstanceLayout
from secure_tool_broker import credential_catalog
from secure_tool_broker.credential_catalog import (
    CatalogCredential,
    CatalogCredentialField,
    CatalogSourceRef,
    CredentialCatalog,
)
from secure_tool_broker.credential_catalog_store import CredentialOwnerScope
from secure_tool_broker.credential_templates import (
    load_credential_templates,
    render_credential_template_request,
)
from storage.execution_control_repository import SqliteExecutionControlRepository
from tests.execution_access_support import activation as build_activation
from tests.execution_access_support import host_key_fingerprint, team_owner


def _free_local_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def _wait_for_port(port: int, timeout_seconds: float = 5.0) -> None:
    deadline = time.monotonic() + timeout_seconds
    while time.monotonic() < deadline:
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
            sock.settimeout(0.2)
            if sock.connect_ex(("127.0.0.1", port)) == 0:
                return
        time.sleep(0.05)
    raise TimeoutError(f"Timed out waiting for local sshd on 127.0.0.1:{port}")


def test_builtin_ssh_credential_templates_load_and_render() -> None:
    templates = load_credential_templates()
    assert "ssh_private_key" in templates
    assert "ssh_host_bootstrap" in templates

    key_tpl = templates["ssh_private_key"].template
    key_fields = {f.name: f for f in key_tpl.fields}
    assert key_fields["ssh_public_key"].visibility == "metadata_visible"
    assert key_fields["ssh_public_key"].required is True
    assert key_fields["ssh_private_key"].visibility == "secret"
    assert key_fields["ssh_private_key"].kind == "credential_file"
    assert key_fields["ssh_private_key"].required is True
    assert key_fields["ssh_passphrase"].visibility == "secret"
    assert key_fields["ssh_passphrase"].required is False

    boot_tpl = templates["ssh_host_bootstrap"].template
    boot_fields = {f.name: f for f in boot_tpl.fields}
    assert boot_fields["username"].visibility == "metadata_visible"
    assert boot_fields["username"].required is True
    assert boot_fields["admin_password"].visibility == "secret"
    assert boot_fields["admin_password"].required is True
    assert boot_fields["ssh_private_key"].required is False
    assert boot_fields["ssh_passphrase"].required is False
    assert boot_fields["tailscale_auth_key"].visibility == "secret"
    assert boot_fields["tailscale_auth_key"].required is False

    rendered_key = render_credential_template_request(
        {"template_id": "ssh_private_key", "service_name": "Build Mac"}
    )
    assert rendered_key.status == "success"
    assert rendered_key.content["request"]["credential_key"] == "build_mac_ssh_key"

    rendered_boot = render_credential_template_request(
        {"template_id": "ssh_host_bootstrap", "service_name": "Lab Linux"}
    )
    assert rendered_boot.status == "success"
    assert rendered_boot.content["request"]["credential_key"] == "lab_linux_ssh_bootstrap"


def test_session_secret_scrubber_handles_split_reads_multiline_keys_and_short_secrets(
    caplog: pytest.LogCaptureFixture,
) -> None:
    secret_pw = "SuperSecretAdminPass!42"
    fake_pem = (
        "-----BEGIN OPENSSH PRIVATE KEY-----\n"
        "b3BlbnNzaC1rZXktdjEAAAAABG5vbmUAAAAEbm9uZQAAAAAAAAABAAAAMwAAAAtzc2gtZW\n"
        "QyNTUxOQAAACB1234567890abcdefghijklmnopqrstuvwxyzABCDEF\n"
        "-----END OPENSSH PRIVATE KEY-----"
    )
    scrubber = SessionSecretScrubber([secret_pw, fake_pem])

    # Exact + PEM body line + standard token regex redaction.
    raw = (
        f"pw={secret_pw} body=QyNTUxOQAAACB1234567890abcdefghijklmnopqrstuvwxyzABCDEF "
        "tok=ghp_1234567890abcdef1234567890abcdef"
    )
    scrubbed = scrubber.scrub_text(raw)
    assert secret_pw not in scrubbed
    assert "QyNTUxOQAAACB1234567890abcdefghijklmnopqrstuvwxyzABCDEF" not in scrubbed
    assert "ghp_1234567890abcdef" not in scrubbed
    assert scrubbed.count("[REDACTED]") == 3

    # C10: Stream scrubber catches secrets split across arbitrary chunk boundaries (including 1-byte chunks).
    for chunks in (
        [b"before " + secret_pw[:9].encode(), secret_pw[9:].encode() + b" after"],
        [bytes([b]) for b in f"x={secret_pw};done".encode()],
    ):
        stream = scrubber.new_stream_scrubber()
        out_parts = [stream.feed(chunk) for chunk in chunks]
        out_parts.append(stream.flush())
        combined = b"".join(out_parts).decode("utf-8")
        assert secret_pw not in combined
        assert "[REDACTED]" in combined
        # Ensure no half-secret was ever emitted in an earlier chunk.
        for part in out_parts:
            assert secret_pw[:9].encode() not in part
            assert secret_pw[9:].encode() not in part

    # Ordinary interactive output is emitted immediately with zero unnecessary buffering.
    prompt_stream = scrubber.new_stream_scrubber()
    assert prompt_stream.feed(b"deploy@box:~$ ") == b"deploy@box:~$ "
    assert prompt_stream.flush() == b""

    # Short secrets (< 6 chars) emit a warning without logging the secret value.
    assert is_short_secret("abc") is True
    assert is_short_secret("abcdef") is False
    with caplog.at_level(logging.WARNING):
        registered = scrubber.register("1234")
    assert registered is False
    assert "ssh_short_secret_skipped_from_exact_scrubbing" in caplog.text
    assert "1234" not in caplog.text


def test_catalog_provider_rejects_dev_shm_paths() -> None:
    with pytest.raises(ValueError, match="/dev"):
        CatalogSSHCredentialMaterialProvider(ephemeral_base_dir=Path("/dev/shm/ua-ssh"))
    with pytest.raises(ValueError, match="/dev"):
        CatalogSSHCredentialMaterialProvider(ephemeral_base_dir=Path("/dev"))


def test_catalog_provider_real_sshd_passphrase_agent_and_landlock_isolation(
    tmp_path: Path,
) -> None:
    sshd_bin = shutil.which("sshd")
    ssh_bin = shutil.which("ssh")
    keygen_bin = shutil.which("ssh-keygen")
    agent_bin = shutil.which("ssh-agent")
    add_bin = shutil.which("ssh-add")
    if not (sshd_bin and ssh_bin and keygen_bin and agent_bin and add_bin):
        pytest.skip("OpenSSH binaries are not installed in this environment.")

    layout = InstanceLayout.at(tmp_path / "instance")
    ws_dir = layout.team_dir
    ws_dir.mkdir(parents=True)

    host_key = tmp_path / "ssh_host_ed25519_key"
    client_key = tmp_path / "client_ed25519_key"
    passphrase = "unlock-passphrase-never-on-disk-9876"
    subprocess.run([keygen_bin, "-q", "-t", "ed25519", "-N", "", "-f", str(host_key)], check=True)
    subprocess.run(
        [keygen_bin, "-q", "-t", "ed25519", "-N", passphrase, "-f", str(client_key)],
        check=True,
    )
    private_key_pem = client_key.read_text(encoding="utf-8")
    public_key_line = client_key.with_suffix(".pub").read_text(encoding="utf-8").strip()
    client_key.unlink()

    authorized_keys = tmp_path / "authorized_keys"
    authorized_keys.write_text(f"{public_key_line}\n", encoding="utf-8")
    authorized_keys.chmod(0o600)

    port = _free_local_port()
    sshd_config = tmp_path / "sshd_config"
    sshd_config.write_text(
        "\n".join(
            [
                f"Port {port}",
                "ListenAddress 127.0.0.1",
                f"HostKey {host_key}",
                f"AuthorizedKeysFile {authorized_keys}",
                f"PidFile {tmp_path / 'sshd.pid'}",
                "PubkeyAuthentication yes",
                "PasswordAuthentication no",
                "KbdInteractiveAuthentication no",
                "UsePAM no",
                "StrictModes no",
            ]
        )
        + "\n",
        encoding="utf-8",
    )

    host_pub_parts = host_key.with_suffix(".pub").read_text(encoding="utf-8").strip().split()
    host_algo, host_b64 = host_pub_parts[0], host_pub_parts[1]
    host_fp = host_key_fingerprint(base64.b64decode(host_b64))
    target = SSHConnectionTargetV1(
        host="bootstrap.internal",
        port=port,
        username=getpass.getuser(),
        host_key_algorithm=host_algo,
        host_key_fingerprint=host_fp,
        credential_binding_id="catalog:build_mac_ssh_key",
    )
    digest = contract_sha256(target.model_dump(mode="json"))
    session = InteractiveSessionV1(
        session_id="ssh_sess_sealbox_real",
        owner=team_owner(),
        target=target,
        target_digest=digest,
        connect_activation=build_activation(
            ExecutionActivationKind.SSH_CONNECT,
            resource_id="ssh_sess_sealbox_real",
            resource_digest=digest,
            function="create_execution_ssh_session",
            expires_at=datetime.now(timezone.utc) + timedelta(minutes=10),
        ),
    )

    secrets_by_key = {
        ("build_mac_ssh_key", "ssh_private_key", OwnerScopeKind.TEAM, "team_alpha"): private_key_pem,
        ("build_mac_ssh_key", "ssh_passphrase", OwnerScopeKind.TEAM, "team_alpha"): passphrase,
        ("build_mac_ssh_key", "ssh_public_key", OwnerScopeKind.TEAM, "team_alpha"): public_key_line,
    }

    def _resolve(key: str, field_name: str, owner: OwnerScopeV1) -> str | None:
        return secrets_by_key.get((key, field_name, owner.scope_kind, owner.owner_id))

    provider = CatalogSSHCredentialMaterialProvider(
        ephemeral_base_dir=layout.ssh_ephemeral_dir,
        secret_resolver=_resolve,
        known_hosts_line_provider=lambda _s: (host_algo, host_b64),
        ssh_agent_executable=agent_bin,
        ssh_add_executable=add_bin,
    )
    broker = SSHSessionBroker(
        repository=SqliteExecutionControlRepository(str(tmp_path / "execution.db")),
        authorization_guard=ExecutionAccessGuard(authorization_enabled=False),
        material_provider=provider,
        artifact_store=FilesystemArtifactStore(tmp_path / "artifacts"),
        team_id="team_alpha",
        broker_id="broker-test",
        ssh_executable=ssh_bin,
    )

    sshd = subprocess.Popen(
        [sshd_bin, "-D", "-e", "-f", str(sshd_config)],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    session_dir: Path | None = None
    agent_pid: int | None = None
    try:
        _wait_for_port(port)
        scrubber = SessionSecretScrubber()
        with provider.prepare(session, scrubber=scrubber) as material:
            assert material.working_dir is not None
            session_dir = material.working_dir
            assert material.ssh_auth_sock == "./s"
            assert private_key_pem in material.touched_secrets
            assert passphrase in material.touched_secrets
            broker._verify_material(session, material)

            # No private key or passphrase bytes exist anywhere in the ephemeral dir.
            for child in session_dir.iterdir():
                if child.is_file():
                    raw_text = child.read_text(encoding="utf-8", errors="ignore")
                    assert "PRIVATE KEY" not in raw_text
                    assert passphrase not in raw_text

            # ssh-agent cmdline never leaks full directory path or secrets.
            agent_pid = int((session_dir / "agent.pid").read_text(encoding="utf-8").strip())
            cmdline_path = Path(f"/proc/{agent_pid}/cmdline")
            if cmdline_path.exists():
                cmdline = cmdline_path.read_bytes().decode("utf-8", errors="ignore")
                assert session_dir.name not in cmdline
                assert passphrase not in cmdline

            # Real OpenSSH login via private ssh-agent with zero private key file on disk.
            validated = ValidatedSSHHost(
                host="bootstrap.internal",
                port=port,
                resolved_ip="::ffff:127.0.0.1",
                resolved_ips=("::ffff:127.0.0.1",),
                host_key_alias=ssh_host_key_alias("bootstrap.internal", port),
            )
            argv = list(broker._ssh_argv(session, material, validated_host=validated))
            assert "IdentityAgent=SSH_AUTH_SOCK" in argv
            argv[argv.index("-tt")] = "-T"
            argv.append("echo sealbox-agent-ok")
            login = subprocess.run(
                argv,
                cwd=str(material.working_dir),
                env=broker._ssh_env(material),
                capture_output=True,
                text=True,
                timeout=10,
                check=False,
            )
            assert login.returncode == 0, f"ssh failed: out={login.stdout!r} err={login.stderr!r}"
            assert "sealbox-agent-ok" in login.stdout

            # C01: Real Landlock code_sandbox cannot list ssh_ephemeral_dir, read files,
            # or inspect the private ssh-agent's environment/cwd.
            status = code_sandbox_status()
            if status.available:
                probe_code = (
                    "import os\n"
                    f"for fn in (lambda: os.listdir({str(layout.ssh_ephemeral_dir)!r}), "
                    f"lambda: os.listdir({str(session_dir)!r}), "
                    f"lambda: open({str(material.known_hosts_file)!r}).read(), "
                    f"lambda: open('/proc/{agent_pid}/environ', 'rb').read(), "
                    f"lambda: os.readlink('/proc/{agent_pid}/cwd')):\n"
                    "  try:\n"
                    "    fn()\n"
                    "    raise SystemExit('LEAKED')\n"
                    "  except PermissionError:\n"
                    "    pass\n"
                    "print('SANDBOX_ISOLATED')\n"
                )
                sandboxed = prepare_sandboxed_command(
                    ("python3", "-c", probe_code),
                    task_roots=[str(ws_dir)],
                    env={**os.environ, "TEAM_INSTANCE_ROOT": str(layout.root)},
                    network=False,
                )
                try:
                    probe = subprocess.run(
                        sandboxed.argv,
                        env=sandboxed.env,
                        capture_output=True,
                        text=True,
                        timeout=10,
                        check=False,
                    )
                finally:
                    release_sandboxed_command(sandboxed)
                assert probe.returncode == 0, f"sandbox probe failed: {probe.stdout!r} {probe.stderr!r}"
                assert "SANDBOX_ISOLATED" in probe.stdout

        # After context exit: ephemeral directory wiped and ssh-agent terminated.
        assert session_dir is not None and not session_dir.exists()
        assert agent_pid is not None
        with pytest.raises(ProcessLookupError):
            os.kill(agent_pid, 0)
    finally:
        sshd.terminate()
        try:
            sshd.wait(timeout=5)
        except subprocess.TimeoutExpired:
            sshd.kill()
            sshd.wait(timeout=5)


def test_catalog_provider_wrong_passphrase_cleans_up_and_scrubs_error(tmp_path: Path) -> None:
    keygen_bin = shutil.which("ssh-keygen")
    agent_bin = shutil.which("ssh-agent")
    add_bin = shutil.which("ssh-add")
    if not (keygen_bin and agent_bin and add_bin):
        pytest.skip("OpenSSH binaries are not installed in this environment.")

    key_path = tmp_path / "encrypted_key"
    subprocess.run(
        [keygen_bin, "-q", "-t", "ed25519", "-N", "correct-passphrase-123", "-f", str(key_path)],
        check=True,
    )
    pem = key_path.read_text(encoding="utf-8")
    wrong_pass = "wrong-passphrase-secret-999"

    layout = InstanceLayout.at(tmp_path / "inst")
    provider = CatalogSSHCredentialMaterialProvider(
        ephemeral_base_dir=layout.ssh_ephemeral_dir,
        secret_resolver=lambda _k, field, _o: pem if field == "ssh_private_key" else (wrong_pass if field == "ssh_passphrase" else None),
        known_hosts_line_provider=lambda _s: ("ssh-ed25519", base64.b64encode(b"hostkey").decode("ascii")),
        ssh_agent_executable=agent_bin,
        ssh_add_executable=add_bin,
    )
    target = SSHConnectionTargetV1(
        host="box.internal",
        port=22,
        username="deploy",
        host_key_algorithm="ssh-ed25519",
        host_key_fingerprint=host_key_fingerprint(b"hostkey"),
        credential_binding_id="catalog:enc_key",
    )
    digest = contract_sha256(target.model_dump(mode="json"))
    session = InteractiveSessionV1(
        session_id="ssh_sess_wrong_pw",
        owner=team_owner(),
        target=target,
        target_digest=digest,
    )

    with pytest.raises(PermissionError) as exc_info:
        with provider.prepare(session):
            pass
    assert wrong_pass not in str(exc_info.value)
    assert list(layout.ssh_ephemeral_dir.iterdir()) == []


def test_catalog_provider_owner_scope_isolation_and_named_secrets(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    db_path = tmp_path / "catalog.db"
    monkeypatch.setenv("SECURE_TOOL_BROKER_CREDENTIAL_CATALOG_DB_PATH", str(db_path))
    monkeypatch.setenv("TEST_ALICE_TS_KEY", "tskey-auth-alice-secret-123456")
    monkeypatch.setenv("TEST_ALICE_ADMIN_PW", "alice-admin-password-789")

    alice_scope = CredentialOwnerScope.user("usr_alice")
    credential_catalog.save_catalog(
        CredentialCatalog(
            credentials={
                "alice_boot": CatalogCredential(
                    display_name="Alice Host Bootstrap",
                    purpose="Bootstrap Alice host",
                    fields={
                        "admin_password": CatalogCredentialField(
                            kind="password",
                            visibility="secret",
                            resolution_order=[CatalogSourceRef(source="env", env_var="TEST_ALICE_ADMIN_PW")],
                        ),
                        "tailscale_auth_key": CatalogCredentialField(
                            kind="token",
                            visibility="secret",
                            resolution_order=[CatalogSourceRef(source="env", env_var="TEST_ALICE_TS_KEY")],
                        ),
                    },
                )
            }
        ),
        scope=alice_scope,
    )

    layout = InstanceLayout.at(tmp_path / "inst")
    provider = CatalogSSHCredentialMaterialProvider(ephemeral_base_dir=layout.ssh_ephemeral_dir)
    alice_owner = OwnerScopeV1(scope_kind=OwnerScopeKind.USER, owner_id="usr_alice", team_id="team_alpha")
    bob_owner = OwnerScopeV1(scope_kind=OwnerScopeKind.USER, owner_id="usr_bob", team_id="team_alpha")

    # Alice resolves her own tailscale_auth_key and admin_password askpass.
    scrubber = SessionSecretScrubber()
    ts_key = provider.resolve_named_secret(
        owner=alice_owner,
        credential_key="alice_boot",
        field_name="tailscale_auth_key",
        scrubber=scrubber,
    )
    assert ts_key == "tskey-auth-alice-secret-123456"
    assert scrubber.scrub_text(f"joined with {ts_key}") == "joined with [REDACTED]"

    # Forbidden secret fields cannot be requested through resolve_named_secret.
    for forbidden in FORBIDDEN_EXTRA_SECRET_FIELDS:
        with pytest.raises(PermissionError):
            provider.resolve_named_secret(
                owner=alice_owner,
                credential_key="alice_boot",
                field_name=forbidden,
            )

    # Bob cannot resolve Alice's credential.
    with pytest.raises(PermissionError):
        provider.resolve_named_secret(
            owner=bob_owner,
            credential_key="alice_boot",
            field_name="tailscale_auth_key",
        )

    # Team-scoped credentials resolve under OwnerScopeKind.TEAM and cannot be read by another team.
    monkeypatch.setenv("TEST_TEAM_TS_KEY", "tskey-auth-team-secret-654321")
    credential_catalog.save_catalog(
        CredentialCatalog(
            credentials={
                "team_boot": CatalogCredential(
                    display_name="Team Host Bootstrap",
                    purpose="Team host",
                    fields={
                        "tailscale_auth_key": CatalogCredentialField(
                            kind="token",
                            visibility="secret",
                            resolution_order=[CatalogSourceRef(source="env", env_var="TEST_TEAM_TS_KEY")],
                        ),
                    },
                )
            }
        ),
        scope=CredentialOwnerScope.team("team_alpha"),
    )
    assert (
        provider.resolve_named_secret(
            owner=team_owner(),
            credential_key="team_boot",
            field_name="tailscale_auth_key",
        )
        == "tskey-auth-team-secret-654321"
    )
    with pytest.raises(PermissionError):
        provider.resolve_named_secret(
            owner=OwnerScopeV1(scope_kind=OwnerScopeKind.TEAM, owner_id="team_other", team_id="team_other"),
            credential_key="team_boot",
            field_name="tailscale_auth_key",
        )

    # One-shot password askpass emits the password once and wipes the secret file immediately.
    with provider.prepare_password_askpass(
        owner=alice_owner,
        credential_key="alice_boot",
        scrubber=scrubber,
    ) as askpass:
        helper = Path(askpass.env["SSH_ASKPASS"])
        secret_file = helper.parent / "bootstrap.pw"
        assert secret_file.exists()
        first = subprocess.run([str(helper)], capture_output=True, text=True, check=True)
        assert first.stdout.strip() == "alice-admin-password-789"
        assert not secret_file.exists()
    assert not helper.parent.exists()


def test_sweep_stale_ssh_ephemeral_dirs_kills_orphaned_agent_and_keeps_active(tmp_path: Path) -> None:
    agent_bin = shutil.which("ssh-agent")
    if not agent_bin:
        pytest.skip("ssh-agent is not installed in this environment.")

    layout = InstanceLayout.at(tmp_path / "inst")
    stale_dir = layout.ssh_ephemeral_dir / "ua-ssh-stale-crash"
    active_dir = layout.ssh_ephemeral_dir / "ua-ssh-active-live"
    stale_dir.mkdir(parents=True, mode=0o700)
    active_dir.mkdir(parents=True, mode=0o700)

    orphan = subprocess.Popen(
        [agent_bin, "-D", "-s", "-a", "./s"],
        cwd=str(stale_dir),
        stdout=subprocess.DEVNULL,
        stderr=subprocess.PIPE,
        text=True,
    )
    try:
        deadline = time.monotonic() + 5.0
        while not (stale_dir / "s").exists() and time.monotonic() < deadline:
            time.sleep(0.02)
        assert (stale_dir / "s").exists()
        (stale_dir / "agent.pid").write_text(f"{orphan.pid}\n", encoding="utf-8")
        (stale_dir / "leftover.txt").write_text("stale-data", encoding="utf-8")
        (active_dir / "keep.txt").write_text("still-live", encoding="utf-8")

        swept = sweep_stale_ssh_ephemeral_dirs(
            layout.ssh_ephemeral_dir,
            active_dirs=[active_dir],
        )
        assert swept == 1
        assert not stale_dir.exists()
        assert active_dir.exists()
        assert orphan.wait(timeout=5) is not None
    finally:
        if orphan.poll() is None:
            orphan.kill()
            orphan.wait(timeout=5)


def test_composite_material_provider_preserves_configured_and_routes_catalog(tmp_path: Path) -> None:
    id_file = tmp_path / "id_ed25519"
    kh_file = tmp_path / "known_hosts"
    id_file.write_text("dummy", encoding="utf-8")
    kh_file.write_text("dummy", encoding="utf-8")
    static_mat = SSHCredentialMaterial(identity_file=id_file, known_hosts_file=kh_file)

    configured = ConfiguredSSHCredentialMaterialProvider({"static_binding": static_mat})
    composite_no_catalog = CompositeSSHCredentialMaterialProvider(configured_provider=configured)

    static_target = SSHConnectionTargetV1(
        host="legacy.internal",
        port=22,
        username="ops",
        host_key_algorithm="ssh-ed25519",
        host_key_fingerprint=host_key_fingerprint(b"legacy-key"),
        credential_binding_id="static_binding",
    )
    static_session = InteractiveSessionV1(
        session_id="ssh_sess_static",
        owner=team_owner(),
        target=static_target,
        target_digest=contract_sha256(static_target.model_dump(mode="json")),
    )
    with composite_no_catalog.prepare(static_session) as resolved:
        assert resolved == static_mat

    catalog_target = static_target.model_copy(update={"credential_binding_id": "catalog:new_key"})
    catalog_session = InteractiveSessionV1(
        session_id="ssh_sess_catalog",
        owner=team_owner(),
        target=catalog_target,
        target_digest=contract_sha256(catalog_target.model_dump(mode="json")),
    )
    with pytest.raises(PermissionError, match="not enabled"):
        with composite_no_catalog.prepare(catalog_session):
            pass
