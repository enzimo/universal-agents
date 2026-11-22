"""Broker-side SSH operational primitives, sudo recipes, live revocation, and real local sshd tests."""

from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone
import ipaddress
import os
from pathlib import Path
import shutil
import socket
import subprocess
import time
from typing import Any

import pytest

from runtime_hosting.artifacts import FilesystemArtifactStore
from runtime_hosting.authorization import (
    ExecutionAccessGuard,
    ExecutionAuthorizationDenied,
    SSHCallerContext,
)
from runtime_hosting.execution_contracts import (
    EnrolledSSHTargetV1,
    ExecutionActivationKind,
    InteractiveSessionStatus,
    InteractiveSessionV1,
    OwnerScopeKind,
    OwnerScopeV1,
    SSHDelegationBoundsV1,
    SSHSecretRecipe,
    SSHTargetLifecycle,
    SSHTargetPlatform,
    ssh_interaction_resource_digest,
    ssh_public_key_sha256_fingerprint,
)
from runtime_hosting.ssh import (
    CatalogSSHCredentialMaterialProvider,
    CompositeSSHCredentialMaterialProvider,
    ConfiguredSSHCredentialMaterialProvider,
    SSHCommandStatus,
    SSHSessionBroker,
)
from runtime_hosting.ssh_network import SSHNetworkPolicy, SSHNetworkPolicyError
from runtime_hosting.ssh_operations import build_tar_gz_bundle
from storage.execution_control_repository import SqliteExecutionControlRepository
from tests.execution_access_support import activation as build_activation

_AGREEMENT_GUARD = ExecutionAccessGuard(authorization_enabled=False)
_USER_OWNER = OwnerScopeV1(scope_kind=OwnerScopeKind.USER, owner_id="user_alice", team_id="team_alpha")
_LEAD_CALLER = SSHCallerContext(agent_type="TeamArchitect", task_id="req_1", person_id="user_alice")
_WORKER_CALLER = SSHCallerContext(agent_type="CodeWritingAgent", task_id="req_1", person_id="user_alice")
_OTHER_WORKER = SSHCallerContext(agent_type="WebResearchAgent", task_id="req_1", person_id="user_alice")


def _generate_ed25519_keypair(
    directory: Path,
    *,
    name: str = "id_ed25519",
    passphrase: str = "",
) -> tuple[Path, str, str]:
    directory.mkdir(parents=True, exist_ok=True)
    key_path = directory / name
    subprocess.run(
        ["ssh-keygen", "-q", "-t", "ed25519", "-N", passphrase, "-f", str(key_path)],
        check=True,
        capture_output=True,
    )
    private_pem = key_path.read_text(encoding="utf-8")
    public_line = key_path.with_suffix(".pub").read_text(encoding="utf-8").strip()
    return key_path, private_pem, public_line


def _make_enrolled_target(
    *,
    target_id: str,
    host: str,
    port: int,
    username: str,
    platform: SSHTargetPlatform,
    host_pub: str,
    credential_key: str,
    credential_pub: str,
    bootstrap_credential_key: str | None = None,
) -> EnrolledSSHTargetV1:
    host_algo, host_fp = ssh_public_key_sha256_fingerprint(host_pub)
    _cred_algo, cred_fp = ssh_public_key_sha256_fingerprint(credential_pub)
    expires = datetime.now(timezone.utc) + timedelta(hours=2)
    from runtime_hosting.execution_contracts import enrolled_ssh_target_digest

    digest = enrolled_ssh_target_digest(
        target_id=target_id,
        owner=_USER_OWNER,
        host=host,
        port=port,
        username=username,
        platform=platform,
        host_key_algorithm=host_algo,
        host_key_fingerprint=host_fp,
        host_key_public_key=host_pub,
        credential_key=credential_key,
        credential_owner_scope=_USER_OWNER,
        credential_public_key_fingerprint=cred_fp,
        bootstrap_credential_key=bootstrap_credential_key,
        bootstrap_credential_owner_scope=_USER_OWNER if bootstrap_credential_key else None,
        lifecycle=SSHTargetLifecycle.TEMPORARY,
        expires_at_utc=expires,
        reason="Test target enrollment",
    )
    return EnrolledSSHTargetV1(
        target_id=target_id,
        owner=_USER_OWNER,
        host=host,
        port=port,
        username=username,
        platform=platform,
        host_key_algorithm=host_algo,
        host_key_fingerprint=host_fp,
        host_key_public_key=host_pub,
        credential_key=credential_key,
        credential_owner_scope=_USER_OWNER,
        credential_public_key_fingerprint=cred_fp,
        bootstrap_credential_key=bootstrap_credential_key,
        bootstrap_credential_owner_scope=_USER_OWNER if bootstrap_credential_key else None,
        lifecycle=SSHTargetLifecycle.TEMPORARY,
        expires_at_utc=expires,
        reason="Test target enrollment",
        activation=build_activation(
            ExecutionActivationKind.SSH_TARGET_ENROLL,
            resource_id=target_id,
            resource_digest=digest,
            owner_scope=_USER_OWNER,
            function="change:execution_ssh_target_enroll",
            expires_at=expires,
        ),
    )


def _create_session_and_delegation(
    repository: SqliteExecutionControlRepository,
    target: EnrolledSSHTargetV1,
    bounds: SSHDelegationBoundsV1,
    *,
    session_id: str,
    expires_in_seconds: int = 600,
) -> InteractiveSessionV1:
    conn_target = target.to_connection_target()
    conn_digest = conn_target.calculate_digest()
    session = InteractiveSessionV1(
        session_id=session_id,
        owner=_USER_OWNER,
        target=conn_target,
        target_digest=conn_digest,
        connect_activation=build_activation(
            ExecutionActivationKind.SSH_CONNECT,
            resource_id=session_id,
            resource_digest=conn_digest,
            owner_scope=_USER_OWNER,
            function="create_execution_ssh_session",
            expires_at=datetime.now(timezone.utc) + timedelta(hours=1),
        ),
    )
    repository.create_interactive_session(session)
    interaction_act = build_activation(
        ExecutionActivationKind.SSH_INTERACT,
        resource_id=session_id,
        resource_digest=ssh_interaction_resource_digest(target_digest=conn_digest, bounds=bounds),
        owner_scope=_USER_OWNER,
        function="change:execution_ssh_interaction",
        expires_at=datetime.now(timezone.utc) + timedelta(seconds=expires_in_seconds),
    )
    repository.put_ssh_interaction(interaction_act, bounds=bounds)
    return session


def _write_fake_ssh_exec(directory: Path, *, mode: str = "exec", state_dir: Path | None = None) -> Path:
    """Create a fake ``ssh`` executable that runs the trailing remote command locally."""

    directory.mkdir(parents=True, exist_ok=True)
    script = directory / f"fake-ssh-{mode}"
    state_root = str(state_dir or directory)
    if mode == "exec":
        # Parse ssh argv: extract trailing remote command after '-- user@host'.
        # When invoked with -tt and no remote command (interactive PTY), echo typed lines.
        script.write_text(
            "#!/usr/bin/env python3\n"
            "import os, subprocess, sys\n"
            "args = sys.argv[1:]\n"
            "idx = args.index('--')\n"
            "tail = args[idx + 1:]\n"
            "if len(tail) <= 1:\n"
            "    line = sys.stdin.readline()\n"
            "    sys.stdout.write('remote:' + line)\n"
            "    sys.stdout.flush()\n"
            "    import time; time.sleep(5)\n"
            "    sys.exit(0)\n"
            "cmd = ' '.join(tail[1:])\n"
            "env = dict(os.environ)\n"
            f"env['HOME'] = {state_root!r}\n"
            "res = subprocess.run(['sh', '-c', cmd], env=env)\n"
            "sys.exit(res.returncode)\n",
            encoding="utf-8",
        )
    elif mode == "bootstrap":
        # Simulates a host whose authorized_keys starts empty:
        # - PasswordAuthentication=yes invokes SSH_ASKPASS and verifies password == expected
        # - PubkeyAuthentication=yes succeeds ONLY after the key has been installed in HOME/.ssh/authorized_keys
        script.write_text(
            "#!/usr/bin/env python3\n"
            "import os, pathlib, subprocess, sys\n"
            "args = sys.argv[1:]\n"
            f"home = pathlib.Path({state_root!r})\n"
            "home.mkdir(parents=True, exist_ok=True)\n"
            "idx = args.index('--')\n"
            "tail = args[idx + 1:]\n"
            "cmd = ' '.join(tail[1:]) if len(tail) > 1 else ''\n"
            "if 'PasswordAuthentication=yes' in args:\n"
            "    askpass = os.environ.get('SSH_ASKPASS')\n"
            "    if not askpass:\n"
            "        sys.stderr.write('missing SSH_ASKPASS\\n')\n"
            "        sys.exit(255)\n"
            "    pw_proc = subprocess.run([askpass, 'password:'], capture_output=True, text=True)\n"
            "    pw = pw_proc.stdout.strip()\n"
            "    expected = (home / 'expected_password').read_text(encoding='utf-8').strip()\n"
            "    if pw != expected:\n"
            "        sys.stderr.write(f'Permission denied for password {pw}\\n')\n"
            "        sys.exit(255)\n"
            "    env = dict(os.environ)\n"
            "    env['HOME'] = str(home)\n"
            "    res = subprocess.run(['sh', '-c', cmd], env=env)\n"
            "    sys.exit(res.returncode)\n"
            "auth_keys = home / '.ssh' / 'authorized_keys'\n"
            "if not auth_keys.is_file() or not auth_keys.read_text(encoding='utf-8').strip():\n"
            "    sys.stderr.write('Permission denied (publickey).\\n')\n"
            "    sys.exit(255)\n"
            "if not cmd:\n"
            "    line = sys.stdin.readline()\n"
            "    sys.stdout.write('opened:' + line)\n"
            "    sys.stdout.flush()\n"
            "    import time; time.sleep(5)\n"
            "    sys.exit(0)\n"
            "env = dict(os.environ)\n"
            "env['HOME'] = str(home)\n"
            "res = subprocess.run(['sh', '-c', cmd], env=env)\n"
            "sys.exit(res.returncode)\n",
            encoding="utf-8",
        )
    script.chmod(0o755)
    return script


def _write_fake_sudo(directory: Path, *, behavior: str, expected_password: str = "SuperAdminPass!99") -> Path:
    """Create a fake sudo binary testing NOPASSWD, password+timestamp, wrong-password, and no-timestamp."""

    directory.mkdir(parents=True, exist_ok=True)
    stamp_file = directory / f"sudo_stamp_{behavior}"
    binary = directory / f"fake-sudo-{behavior}"
    binary.write_text(
        "#!/usr/bin/env python3\n"
        "import os, pathlib, subprocess, sys\n"
        f"behavior = {behavior!r}\n"
        f"expected = {expected_password!r}\n"
        f"stamp = pathlib.Path({str(stamp_file)!r})\n"
        "args = sys.argv[1:]\n"
        "if '-v' in args and '-S' in args:\n"
        "    pw = sys.stdin.readline().rstrip('\\r\\n')\n"
        "    if pw != expected:\n"
        "        sys.stderr.write('Sorry, try again.\\n')\n"
        "        sys.exit(1)\n"
        "    if behavior != 'no_timestamp':\n"
        "        stamp.write_text('ok', encoding='utf-8')\n"
        "    sys.exit(0)\n"
        "if '-n' in args and 'true' in args and '--' not in args:\n"
        "    if behavior == 'nopasswd' or stamp.is_file():\n"
        "        sys.exit(0)\n"
        "    sys.stderr.write('sudo: a password is required\\n')\n"
        "    sys.exit(1)\n"
        "if '--' in args:\n"
        "    if behavior != 'nopasswd' and not stamp.is_file():\n"
        "        sys.stderr.write('sudo: a password is required\\n')\n"
        "        sys.exit(1)\n"
        "    cmd = args[args.index('--') + 1:]\n"
        "    res = subprocess.run(cmd)\n"
        "    sys.exit(res.returncode)\n"
        "sys.exit(2)\n",
        encoding="utf-8",
    )
    binary.chmod(0o755)
    return binary


def _write_fake_tailscale(directory: Path, record_file: Path) -> Path:
    directory.mkdir(parents=True, exist_ok=True)
    binary = directory / "fake-tailscale"
    binary.write_text(
        "#!/usr/bin/env python3\n"
        "import json, pathlib, stat, sys\n"
        f"record = pathlib.Path({str(record_file)!r})\n"
        "args = sys.argv[1:]\n"
        "key_value = ''\n"
        "key_mode = None\n"
        "key_path = ''\n"
        "for item in args:\n"
        "    if item.startswith('--auth-key=file:'):\n"
        "        key_path = item.split('file:', 1)[1]\n"
        "        p = pathlib.Path(key_path)\n"
        "        key_mode = oct(stat.S_IMODE(p.lstat().st_mode))\n"
        "        key_value = p.read_text(encoding='utf-8').strip()\n"
        "record.write_text(json.dumps({'args': args, 'key_path': key_path, 'key_mode': key_mode, 'key_value': key_value}), encoding='utf-8')\n"
        "print(f'tailscale connected with {key_value}')\n"
        "sys.exit(0)\n",
        encoding="utf-8",
    )
    binary.chmod(0o755)
    return binary


def _catalog_broker(
    tmp_path: Path,
    repository: SqliteExecutionControlRepository,
    *,
    secrets_map: dict[tuple[str, str], str],
    ssh_executable: Path,
    network_policy: SSHNetworkPolicy | None = None,
    resolver: Any = None,
    local_address_checker: Any = None,
    allow_password_bootstrap: bool = False,
    sudo_search_paths: tuple[str, ...] | None = None,
    tailscale_search_paths: tuple[str, ...] | None = None,
    revocation_poll_interval_seconds: float = 0.05,
) -> SSHSessionBroker:
    def _resolve(key: str, field_name: str, _owner: OwnerScopeV1) -> str | None:
        return secrets_map.get((key, field_name))

    catalog_provider = CatalogSSHCredentialMaterialProvider(
        ephemeral_base_dir=tmp_path / "ssh-ephemeral",
        secret_resolver=_resolve,
    )
    composite = CompositeSSHCredentialMaterialProvider(
        configured_provider=ConfiguredSSHCredentialMaterialProvider({}),
        catalog_provider=catalog_provider,
    )
    kwargs: dict[str, Any] = {}
    if sudo_search_paths is not None:
        kwargs["sudo_search_paths"] = sudo_search_paths
    if tailscale_search_paths is not None:
        kwargs["tailscale_search_paths"] = tailscale_search_paths
    return SSHSessionBroker(
        repository=repository,
        authorization_guard=_AGREEMENT_GUARD,
        material_provider=composite,
        artifact_store=FilesystemArtifactStore(tmp_path / "artifacts"),
        team_id="team_alpha",
        broker_id="ssh-broker-ops-test",
        ssh_executable=str(ssh_executable),
        network_policy=network_policy or SSHNetworkPolicy.default(),
        resolver=resolver or (lambda host, _port: (host,)),
        local_address_checker=local_address_checker or (lambda _ip: False),
        allow_password_bootstrap=allow_password_bootstrap,
        revocation_poll_interval_seconds=revocation_poll_interval_seconds,
        **kwargs,
    )


# -- 1. Host key probing -------------------------------------------------------


def test_probe_host_key_enforces_network_policy_and_parses_keyscan(tmp_path: Path) -> None:
    _host_key_path, _priv, host_pub = _generate_ed25519_keypair(tmp_path / "hostkey", name="host_ed25519")
    expected_algo, expected_fp = ssh_public_key_sha256_fingerprint(host_pub)
    fake_keyscan = tmp_path / "bin" / "fake-keyscan"
    fake_keyscan.parent.mkdir(parents=True, exist_ok=True)
    fake_keyscan.write_text(
        "#!/usr/bin/env python3\n"
        "import sys\n"
        f"print('# comment line')\n"
        f"print('198.51.100.42 {host_pub}')\n",
        encoding="utf-8",
    )
    fake_keyscan.chmod(0o755)
    repository = SqliteExecutionControlRepository(str(tmp_path / "execution.db"))
    broker = _catalog_broker(
        tmp_path,
        repository,
        secrets_map={},
        ssh_executable=_write_fake_ssh_exec(tmp_path / "bin"),
        resolver=lambda _host, _port: ("198.51.100.42",),
    )
    broker.ssh_keyscan_executable = str(fake_keyscan)

    # Loopback and cloud metadata are refused before invoking keyscan.
    for blocked in ("127.0.0.1", "0.0.0.0", "169.254.169.254", "localhost"):
        with pytest.raises(SSHNetworkPolicyError):
            broker.probe_host_key(host=blocked, port=22)

    probed = broker.probe_host_key(host="mac-mini.example.net", port=2222)
    assert probed.host == "mac-mini.example.net"
    assert probed.port == 2222
    assert probed.resolved_ip == "198.51.100.42"
    assert probed.host_key_algorithm == expected_algo
    assert probed.host_key_fingerprint == expected_fp
    assert probed.host_key_public_key == " ".join(host_pub.split()[:2])
    assert set(probed.verification_commands) == {"linux", "macos", "windows"}


# -- 2. Async commands, command-only sessions, worker delegation, scrubbing ----


@pytest.mark.asyncio
async def test_run_command_and_polling_on_pending_session_with_worker_delegation_and_scrubbing(
    tmp_path: Path,
) -> None:
    _, host_priv_unused, host_pub = _generate_ed25519_keypair(tmp_path / "host", name="host_ed25519")
    _, client_priv, client_pub = _generate_ed25519_keypair(
        tmp_path / "client", name="client_ed25519", passphrase="KeyPassphrase!42"
    )
    del host_priv_unused
    repository = SqliteExecutionControlRepository(str(tmp_path / "execution.db"))
    target = _make_enrolled_target(
        target_id="ops_linux",
        host="198.51.100.50",
        port=22,
        username="deploy",
        platform=SSHTargetPlatform.LINUX,
        host_pub=host_pub,
        credential_key="ops_key",
        credential_pub=client_pub,
    )
    repository.put_ssh_target(target)
    bounds = SSHDelegationBoundsV1(
        delegated_agent_type="CodeWritingAgent",
        delegated_task_id="req_1",
        allow_commands=True,
    )
    session = _create_session_and_delegation(repository, target, bounds, session_id="ssh_sess_cmd")
    broker = _catalog_broker(
        tmp_path,
        repository,
        secrets_map={
            ("ops_key", "ssh_private_key"): client_priv,
            ("ops_key", "ssh_passphrase"): "KeyPassphrase!42",
            ("ops_key", "ssh_public_key"): client_pub,
        },
        ssh_executable=_write_fake_ssh_exec(tmp_path / "bin", mode="exec", state_dir=tmp_path / "remote_home"),
    )

    # Wrong agent types (including lead and another worker) are refused.
    with pytest.raises(ExecutionAuthorizationDenied, match="delegated only to agent type"):
        await broker.run_command(session_id=session.session_id, command="echo hi", caller=_LEAD_CALLER)
    with pytest.raises(ExecutionAuthorizationDenied, match="delegated only to agent type"):
        await broker.run_command(session_id=session.session_id, command="echo hi", caller=_OTHER_WORKER)

    # Delegated worker succeeds on PENDING session (no open PTY required) and scrubs touched passphrase.
    fast = await broker.run_command(
        session_id=session.session_id,
        command="printf 'ok:KeyPassphrase!42\\n'",
        caller=_WORKER_CALLER,
        wait_seconds=5.0,
    )
    assert fast.status == SSHCommandStatus.COMPLETED
    assert fast.exit_code == 0
    assert "ok:[REDACTED]" in fast.stdout
    assert "KeyPassphrase!42" not in fast.stdout

    # Long-running command returns RUNNING and polls cleanly via get_command.
    started = await broker.run_command(
        session_id=session.session_id,
        command="printf 'step1\\n'; sleep 0.4; printf 'step2\\n'",
        caller=_WORKER_CALLER,
        wait_seconds=0.05,
    )
    assert started.status == SSHCommandStatus.RUNNING
    finished = await broker.get_command(
        session_id=session.session_id,
        command_id=started.command_id,
        caller=_WORKER_CALLER,
        wait_seconds=5.0,
    )
    assert finished.status == SSHCommandStatus.COMPLETED
    assert "step1" in finished.stdout and "step2" in finished.stdout

    # recover_after_restart sweeps stale ephemeral dirs left behind by a crash.
    stale = tmp_path / "ssh-ephemeral" / "ua-ssh-deadbeef"
    stale.mkdir(parents=True, exist_ok=True)
    (stale / "leftover").write_text("stale", encoding="utf-8")
    broker.recover_after_restart()
    assert not stale.exists()


# -- 3. First-login password bootstrap: enroll -> latch -> open ----------------


@pytest.mark.asyncio
async def test_enroll_ssh_key_bootstrap_latches_and_allows_subsequent_open(tmp_path: Path) -> None:
    _, _, host_pub = _generate_ed25519_keypair(tmp_path / "host", name="host_ed25519")
    _, client_priv, client_pub = _generate_ed25519_keypair(tmp_path / "client", name="client_ed25519")
    remote_home = tmp_path / "remote_home"
    remote_home.mkdir(parents=True, exist_ok=True)
    (remote_home / "expected_password").write_text("BootstrapSecret!77", encoding="utf-8")

    repository = SqliteExecutionControlRepository(str(tmp_path / "execution.db"))
    target = _make_enrolled_target(
        target_id="fresh_mac",
        host="198.51.100.60",
        port=22,
        username="admin",
        platform=SSHTargetPlatform.MACOS,
        host_pub=host_pub,
        credential_key="mac_key",
        credential_pub=client_pub,
        bootstrap_credential_key="mac_boot",
    )
    repository.put_ssh_target(target)
    bounds = SSHDelegationBoundsV1(
        allow_password_bootstrap=True,
        allow_typing=True,
    )
    session = _create_session_and_delegation(repository, target, bounds, session_id="ssh_sess_boot")
    fake_ssh = _write_fake_ssh_exec(tmp_path / "bin", mode="bootstrap", state_dir=remote_home)

    # First: operator setting off -> refused.
    broker_off = _catalog_broker(
        tmp_path,
        repository,
        secrets_map={
            ("mac_key", "ssh_private_key"): client_priv,
            ("mac_key", "ssh_public_key"): client_pub,
            ("mac_boot", "admin_password"): "BootstrapSecret!77",
        },
        ssh_executable=fake_ssh,
        allow_password_bootstrap=False,
    )
    with pytest.raises(ExecutionAuthorizationDenied, match="allow_password_bootstrap is disabled"):
        await broker_off.enroll_ssh_key(session_id=session.session_id, caller=_LEAD_CALLER)

    # Second: wrong password fails cleanly, scrubs password, releases claim without latching.
    broker_wrong_pw = _catalog_broker(
        tmp_path,
        repository,
        secrets_map={
            ("mac_key", "ssh_private_key"): client_priv,
            ("mac_key", "ssh_public_key"): client_pub,
            ("mac_boot", "admin_password"): "WrongBootstrapPass!99",
        },
        ssh_executable=fake_ssh,
        allow_password_bootstrap=True,
    )
    with pytest.raises(RuntimeError) as wrong_exc:
        await broker_wrong_pw.enroll_ssh_key(session_id=session.session_id, caller=_LEAD_CALLER)
    assert "WrongBootstrapPass!99" not in str(wrong_exc.value)
    assert "[REDACTED]" in str(wrong_exc.value)
    still_unlatched = repository.get_ssh_target(team_id="team_alpha", target_id="fresh_mac")
    assert still_unlatched is not None and still_unlatched.password_bootstrap_completed_at_utc is None

    # Third: correct password installs key into authorized_keys, verifies key-only login, and latches!
    broker_ok = _catalog_broker(
        tmp_path,
        repository,
        secrets_map={
            ("mac_key", "ssh_private_key"): client_priv,
            ("mac_key", "ssh_public_key"): client_pub,
            ("mac_boot", "admin_password"): "BootstrapSecret!77",
        },
        ssh_executable=fake_ssh,
        allow_password_bootstrap=True,
    )
    enrolled = await broker_ok.enroll_ssh_key(session_id=session.session_id, caller=_LEAD_CALLER)
    assert enrolled.verified_key_login is True
    assert enrolled.target_id == "fresh_mac"
    auth_keys_text = (remote_home / ".ssh" / "authorized_keys").read_text(encoding="utf-8")
    assert client_pub in auth_keys_text

    # Re-running bootstrap is now permanently latched off.
    with pytest.raises(ExecutionAuthorizationDenied, match="permanently latched off"):
        await broker_ok.enroll_ssh_key(session_id=session.session_id, caller=_LEAD_CALLER)

    # And opening the SAME session immediately afterward succeeds using key-only login!
    opened = await broker_ok.open(
        session_id=session.session_id, lease_token="lease-after-boot", caller=_LEAD_CALLER
    )
    assert opened.status == InteractiveSessionStatus.OPEN
    await broker_ok.write(session_id=session.session_id, payload=b"hello\n", caller=_LEAD_CALLER)
    out = b""
    for _ in range(5):
        out += await broker_ok.read(session_id=session.session_id, timeout_seconds=2.0, caller=_LEAD_CALLER)
        if b"opened:hello" in out:
            break
    assert b"opened:hello" in out
    await broker_ok.close(session_id=session.session_id)


@pytest.mark.asyncio
async def test_enroll_ssh_key_refuses_concurrent_bootstrap_claim_on_same_target(tmp_path: Path) -> None:
    _, _, host_pub = _generate_ed25519_keypair(tmp_path / "host", name="host_ed25519")
    _, client_priv, client_pub = _generate_ed25519_keypair(tmp_path / "client", name="client_ed25519")
    repository = SqliteExecutionControlRepository(str(tmp_path / "execution.db"))
    target = _make_enrolled_target(
        target_id="race_mac",
        host="198.51.100.61",
        port=22,
        username="admin",
        platform=SSHTargetPlatform.MACOS,
        host_pub=host_pub,
        credential_key="mac_key",
        credential_pub=client_pub,
        bootstrap_credential_key="mac_boot",
    )
    repository.put_ssh_target(target)
    bounds = SSHDelegationBoundsV1(allow_password_bootstrap=True)
    session_a = _create_session_and_delegation(repository, target, bounds, session_id="ssh_sess_race_a")
    session_b = _create_session_and_delegation(repository, target, bounds, session_id="ssh_sess_race_b")
    # Session A holds an active lease and bootstrap claim.
    repository.claim_interactive_session(
        team_id="team_alpha",
        session_id=session_a.session_id,
        broker_id="broker-a",
        lease_token="lease-a",
        lease_ttl_seconds=300,
    )
    assert repository.claim_ssh_target_password_bootstrap(
        team_id="team_alpha", target_id="race_mac", session_id=session_a.session_id
    )
    broker = _catalog_broker(
        tmp_path,
        repository,
        secrets_map={
            ("mac_key", "ssh_private_key"): client_priv,
            ("mac_key", "ssh_public_key"): client_pub,
            ("mac_boot", "admin_password"): "BootstrapSecret!77",
        },
        ssh_executable=_write_fake_ssh_exec(tmp_path / "bin", mode="bootstrap", state_dir=tmp_path / "home"),
        allow_password_bootstrap=True,
    )
    with pytest.raises(PermissionError, match="already in progress"):
        await broker.enroll_ssh_key(session_id=session_b.session_id, caller=_LEAD_CALLER)


# -- 4. Separated sudo handling and tailscale_up recipe (C03 Lead Ruling) ------


@pytest.mark.asyncio
async def test_run_sudo_separated_handshake_and_tailscale_recipe_enforces_c03_ruling(
    tmp_path: Path,
) -> None:
    _, _, host_pub = _generate_ed25519_keypair(tmp_path / "host", name="host_ed25519")
    _, client_priv, client_pub = _generate_ed25519_keypair(tmp_path / "client", name="client_ed25519")
    repository = SqliteExecutionControlRepository(str(tmp_path / "execution.db"))
    target = _make_enrolled_target(
        target_id="linux_sudo",
        host="198.51.100.70",
        port=22,
        username="ops",
        platform=SSHTargetPlatform.LINUX,
        host_pub=host_pub,
        credential_key="ops_key",
        credential_pub=client_pub,
        bootstrap_credential_key="ops_boot",
    )
    repository.put_ssh_target(target)
    bounds = SSHDelegationBoundsV1(
        allow_broker_sudo=True,
        allowed_secret_recipes=(SSHSecretRecipe.TAILSCALE_UP,),
    )
    session = _create_session_and_delegation(repository, target, bounds, session_id="ssh_sess_sudo")
    fake_ssh = _write_fake_ssh_exec(tmp_path / "bin", mode="exec", state_dir=tmp_path / "home")
    admin_pw = "AdminSudoPass!88"
    ts_key = "tskey-auth-k1234567890-SECRETTOKEN"

    # (a) NOPASSWD fake sudo: admin_password is NEVER even resolved and command='rev' sees empty stdin.
    password_fetched = False

    def _tracking_resolver(key: str, field_name: str, _owner: OwnerScopeV1) -> str | None:
        nonlocal password_fetched
        if field_name == "admin_password":
            password_fetched = True
        return {
            ("ops_key", "ssh_private_key"): client_priv,
            ("ops_key", "ssh_public_key"): client_pub,
            ("ops_boot", "admin_password"): admin_pw,
            ("ops_boot", "tailscale_auth_key"): ts_key,
        }.get((key, field_name))

    fake_sudo_nopw = _write_fake_sudo(tmp_path / "sudo_nopw", behavior="nopasswd", expected_password=admin_pw)
    ts_record = tmp_path / "ts_record.json"
    fake_ts = _write_fake_tailscale(tmp_path / "ts_bin", ts_record)
    catalog_provider = CatalogSSHCredentialMaterialProvider(
        ephemeral_base_dir=tmp_path / "ssh-ephemeral-sudo",
        secret_resolver=_tracking_resolver,
    )
    broker_nopw = SSHSessionBroker(
        repository=repository,
        authorization_guard=_AGREEMENT_GUARD,
        material_provider=CompositeSSHCredentialMaterialProvider(
            ConfiguredSSHCredentialMaterialProvider({}), catalog_provider
        ),
        artifact_store=FilesystemArtifactStore(tmp_path / "artifacts"),
        team_id="team_alpha",
        broker_id="broker-sudo-nopw",
        ssh_executable=str(fake_ssh),
        network_policy=SSHNetworkPolicy.default(),
        resolver=lambda host, _port: (host,),
        local_address_checker=lambda _ip: False,
        sudo_search_paths=(str(fake_sudo_nopw),),
        tailscale_search_paths=(str(fake_ts),),
    )

    rev_nopw = await broker_nopw.run_sudo(
        session_id=session.session_id,
        command="rev",
        caller=_LEAD_CALLER,
    )
    assert rev_nopw.status == SSHCommandStatus.COMPLETED
    assert rev_nopw.stdout == ""
    assert password_fetched is False

    # (b) Password-required fake sudo with timestamp: validates alone via -S -k -v, command='rev' sees empty stdin!
    fake_sudo_pw = _write_fake_sudo(tmp_path / "sudo_pw", behavior="password", expected_password=admin_pw)
    broker_pw = _catalog_broker(
        tmp_path,
        repository,
        secrets_map={
            ("ops_key", "ssh_private_key"): client_priv,
            ("ops_key", "ssh_public_key"): client_pub,
            ("ops_boot", "admin_password"): admin_pw,
            ("ops_boot", "tailscale_auth_key"): ts_key,
        },
        ssh_executable=fake_ssh,
        sudo_search_paths=(str(fake_sudo_pw),),
        tailscale_search_paths=(str(fake_ts),),
    )
    rev_pw = await broker_pw.run_sudo(
        session_id=session.session_id,
        command="printf 'ran_ok:'; rev",
        caller=_LEAD_CALLER,
    )
    assert rev_pw.status == SSHCommandStatus.COMPLETED
    assert rev_pw.stdout == "ran_ok:"
    assert admin_pw not in rev_pw.stdout and admin_pw[::-1] not in rev_pw.stdout

    # (c) Wrong password -> clear failure, no retries, command never runs!
    marker_file = tmp_path / "should_not_exist"
    fake_sudo_bad = _write_fake_sudo(tmp_path / "sudo_bad", behavior="password", expected_password="DifferentPassword!1")
    broker_bad = _catalog_broker(
        tmp_path,
        repository,
        secrets_map={
            ("ops_key", "ssh_private_key"): client_priv,
            ("ops_key", "ssh_public_key"): client_pub,
            ("ops_boot", "admin_password"): admin_pw,
        },
        ssh_executable=fake_ssh,
        sudo_search_paths=(str(fake_sudo_bad),),
    )
    bad_res = await broker_bad.run_sudo(
        session_id=session.session_id,
        command=f"touch {marker_file}",
        caller=_LEAD_CALLER,
    )
    assert bad_res.status == SSHCommandStatus.FAILED
    assert bad_res.error and "Sudo authentication failed" in bad_res.error
    assert not marker_file.exists()
    assert admin_pw not in (bad_res.stdout + bad_res.stderr + (bad_res.error or ""))

    # (d) No-timestamp sudo (timestamp_timeout=0) -> clear refusal, command never runs!
    fake_sudo_nostamp = _write_fake_sudo(tmp_path / "sudo_nostamp", behavior="no_timestamp", expected_password=admin_pw)
    broker_nostamp = _catalog_broker(
        tmp_path,
        repository,
        secrets_map={
            ("ops_key", "ssh_private_key"): client_priv,
            ("ops_key", "ssh_public_key"): client_pub,
            ("ops_boot", "admin_password"): admin_pw,
        },
        ssh_executable=fake_ssh,
        sudo_search_paths=(str(fake_sudo_nostamp),),
    )
    nostamp_res = await broker_nostamp.run_sudo(
        session_id=session.session_id,
        command=f"touch {marker_file}",
        caller=_LEAD_CALLER,
    )
    assert nostamp_res.status == SSHCommandStatus.FAILED
    assert nostamp_res.error and "sudo authentication timestamp" in nostamp_res.error
    assert not marker_file.exists()

    # (e) Free command + extra_secret_field is strictly refused; forbidden fields are refused.
    with pytest.raises(PermissionError, match="never piped to arbitrary agent-written commands"):
        await broker_pw.run_sudo(
            session_id=session.session_id,
            command="rev",
            extra_secret_field="tailscale_auth_key",
            caller=_LEAD_CALLER,
        )
    for forbidden in ("admin_password", "ssh_private_key", "ssh_passphrase"):
        with pytest.raises(PermissionError, match="permanently forbidden"):
            await broker_pw.run_sudo(
                session_id=session.session_id,
                secret_recipe=SSHSecretRecipe.TAILSCALE_UP,
                extra_secret_field=forbidden,
                caller=_LEAD_CALLER,
            )

    # (f) Approved tailscale_up recipe validates flags, passes key via 0600 file (wiped after), scrubs stdout!
    with pytest.raises(ValueError, match="Unsupported tailscale_up recipe parameters"):
        await broker_pw.run_sudo(
            session_id=session.session_id,
            secret_recipe=SSHSecretRecipe.TAILSCALE_UP,
            recipe_params={"auth_key": "injected"},
            caller=_LEAD_CALLER,
        )

    import json

    ts_res = await broker_pw.run_sudo(
        session_id=session.session_id,
        secret_recipe=SSHSecretRecipe.TAILSCALE_UP,
        recipe_params={"hostname": "build-mac-01", "advertise_tags": ["tag:ci"], "ssh": True},
        caller=_LEAD_CALLER,
    )
    assert ts_res.status == SSHCommandStatus.COMPLETED, ts_res
    assert ts_key not in ts_res.stdout
    assert "[REDACTED]" in ts_res.stdout
    recorded = json.loads(ts_record.read_text(encoding="utf-8"))
    assert recorded["key_value"] == ts_key
    assert recorded["key_mode"] == "0o600"
    assert "--hostname" in recorded["args"] and "build-mac-01" in recorded["args"]
    assert "--advertise-tags" in recorded["args"] and "tag:ci" in recorded["args"]
    assert "--ssh" in recorded["args"]
    # Key value never appeared on argv, and ephemeral remote keyfile was wiped by trap.
    assert all(ts_key not in arg for arg in recorded["args"])
    assert not Path(recorded["key_path"]).exists()

    # (g) Unapproved secret_recipe is refused by ExecutionAccessGuard.
    session_no_recipe = _create_session_and_delegation(
        repository,
        target,
        SSHDelegationBoundsV1(allow_broker_sudo=True, allowed_secret_recipes=()),
        session_id="ssh_sess_sudo_norecipe",
    )
    with pytest.raises(ExecutionAuthorizationDenied, match="was not approved"):
        await broker_pw.run_sudo(
            session_id=session_no_recipe.session_id,
            secret_recipe=SSHSecretRecipe.TAILSCALE_UP,
            caller=_LEAD_CALLER,
        )

    # (h) Broker-assisted sudo is refused on Windows targets.
    win_target = _make_enrolled_target(
        target_id="win_sudo_refused",
        host="198.51.100.71",
        port=22,
        username="Administrator",
        platform=SSHTargetPlatform.WINDOWS,
        host_pub=host_pub,
        credential_key="ops_key",
        credential_pub=client_pub,
        bootstrap_credential_key="ops_boot",
    )
    repository.put_ssh_target(win_target)
    win_session = _create_session_and_delegation(
        repository,
        win_target,
        SSHDelegationBoundsV1(allow_broker_sudo=True),
        session_id="ssh_sess_win_sudo",
    )
    with pytest.raises(ExecutionAuthorizationDenied, match="not supported on Windows"):
        await broker_pw.run_sudo(
            session_id=win_session.session_id,
            command="whoami",
            caller=_LEAD_CALLER,
        )


# -- 5. Workspace bundle transfer & sensitive path filtering -------------------


@pytest.mark.asyncio
async def test_upload_bundle_rejects_sensitive_files_and_extracts_verified_archive(
    tmp_path: Path,
) -> None:
    with pytest.raises(PermissionError, match="sensitive"):
        build_tar_gz_bundle([("config/.env", b"SECRET=1")])
    with pytest.raises(PermissionError, match="sensitive"):
        build_tar_gz_bundle([(".ssh/id_ed25519", b"PRIVATE KEY")])
    with pytest.raises(ValueError, match="Unsafe archive entry path"):
        build_tar_gz_bundle([("../escape.txt", b"nope")])
    with pytest.raises(ValueError, match="exceeds maximum single file size"):
        build_tar_gz_bundle([("big.bin", b"x" * 100)], max_single_file_bytes=50)

    _, _, host_pub = _generate_ed25519_keypair(tmp_path / "host", name="host_ed25519")
    _, client_priv, client_pub = _generate_ed25519_keypair(tmp_path / "client", name="client_ed25519")
    repository = SqliteExecutionControlRepository(str(tmp_path / "execution.db"))
    target = _make_enrolled_target(
        target_id="bundle_host",
        host="198.51.100.80",
        port=22,
        username="deploy",
        platform=SSHTargetPlatform.LINUX,
        host_pub=host_pub,
        credential_key="ops_key",
        credential_pub=client_pub,
    )
    repository.put_ssh_target(target)
    bounds = SSHDelegationBoundsV1(allow_workspace_transfer=True)
    session = _create_session_and_delegation(repository, target, bounds, session_id="ssh_sess_bundle")
    broker = _catalog_broker(
        tmp_path,
        repository,
        secrets_map={
            ("ops_key", "ssh_private_key"): client_priv,
            ("ops_key", "ssh_public_key"): client_pub,
        },
        ssh_executable=_write_fake_ssh_exec(tmp_path / "bin", mode="exec", state_dir=tmp_path / "home"),
    )

    dest = tmp_path / "remote_dest"
    uploaded = await broker.upload_bundle(
        session_id=session.session_id,
        entries=[("scripts/deploy.sh", b"echo ok\n"), ("README.txt", b"hello\n")],
        remote_destination=str(dest),
        caller=_LEAD_CALLER,
    )
    assert uploaded.file_count == 2
    assert (dest / "scripts" / "deploy.sh").read_text(encoding="utf-8") == "echo ok\n"
    assert (dest / "README.txt").read_text(encoding="utf-8") == "hello\n"


# -- 6. Live revocation & cross-process DB revocation --------------------------


@pytest.mark.asyncio
async def test_live_revocation_and_db_revocation_immediately_terminate_pty_and_commands(
    tmp_path: Path,
) -> None:
    _, _, host_pub = _generate_ed25519_keypair(tmp_path / "host", name="host_ed25519")
    _, client_priv, client_pub = _generate_ed25519_keypair(
        tmp_path / "client", name="client_ed25519", passphrase="LiveRevokeSecret!33"
    )
    repository = SqliteExecutionControlRepository(str(tmp_path / "execution.db"))
    target = _make_enrolled_target(
        target_id="revoke_host",
        host="198.51.100.85",
        port=22,
        username="deploy",
        platform=SSHTargetPlatform.LINUX,
        host_pub=host_pub,
        credential_key="ops_key",
        credential_pub=client_pub,
    )
    repository.put_ssh_target(target)
    bounds = SSHDelegationBoundsV1(allow_commands=True, allow_typing=True)
    session = _create_session_and_delegation(repository, target, bounds, session_id="ssh_sess_rev1")
    broker = _catalog_broker(
        tmp_path,
        repository,
        secrets_map={
            ("ops_key", "ssh_private_key"): client_priv,
            ("ops_key", "ssh_passphrase"): "LiveRevokeSecret!33",
            ("ops_key", "ssh_public_key"): client_pub,
        },
        ssh_executable=_write_fake_ssh_exec(tmp_path / "bin", mode="exec", state_dir=tmp_path / "home"),
        revocation_poll_interval_seconds=0.05,
    )

    await broker.open(session_id=session.session_id, lease_token="lease-rev1", caller=_LEAD_CALLER)
    await broker.write(
        session_id=session.session_id, payload=b"echo LiveRevokeSecret!33\n", caller=_LEAD_CALLER
    )
    out = await broker.read(session_id=session.session_id, timeout_seconds=2.0, caller=_LEAD_CALLER)
    assert b"LiveRevokeSecret!33" not in out

    running_cmd = await broker.run_command(
        session_id=session.session_id,
        command="sleep 30",
        caller=_LEAD_CALLER,
        wait_seconds=0.05,
    )
    assert running_cmd.status == SSHCommandStatus.RUNNING

    # Direct revoke_session kills PTY + running command immediately and persists scrubbed transcript.
    t0 = time.monotonic()
    revoked_sess = await broker.revoke_session(session_id=session.session_id, revoked_by="admin_1")
    assert time.monotonic() - t0 < 3.0
    assert revoked_sess is not None
    assert revoked_sess.status == InteractiveSessionStatus.DISCONNECTED
    assert revoked_sess.transcript_artifact is not None
    stored_transcript = broker.artifact_store.read_verified(revoked_sess.transcript_artifact)
    assert b"LiveRevokeSecret!33" not in stored_transcript
    assert b"[REDACTED]" in stored_transcript

    cmd_after = await broker.get_command(
        session_id=session.session_id, command_id=running_cmd.command_id, caller=_LEAD_CALLER
    )
    assert cmd_after.status == SSHCommandStatus.REVOKED

    # Cross-process DB-only revocation via target revocation kills a second session's PTY & command quickly.
    session2 = _create_session_and_delegation(repository, target, bounds, session_id="ssh_sess_rev2")
    await broker.open(session_id=session2.session_id, lease_token="lease-rev2", caller=_LEAD_CALLER)
    cmd2 = await broker.run_command(
        session_id=session2.session_id,
        command="sleep 30",
        caller=_LEAD_CALLER,
        wait_seconds=0.05,
    )
    assert cmd2.status == SSHCommandStatus.RUNNING

    # Simulate another process (e.g. Gateway) revoking the target directly in SQLite.
    repository.revoke_ssh_target(team_id="team_alpha", target_id="revoke_host", revoked_by="admin_web")
    deadline = time.monotonic() + 3.0
    while time.monotonic() < deadline and session2.session_id in broker._sessions:
        await asyncio.sleep(0.05)
    assert session2.session_id not in broker._sessions
    cmd2_after = await broker.get_command(
        session_id=session2.session_id,
        command_id=cmd2.command_id,
        caller=_LEAD_CALLER,
        wait_seconds=2.0,
    )
    assert cmd2_after.status == SSHCommandStatus.REVOKED


# -- 7. Real local unprivileged sshd integration tests (C14) -------------------


def _free_tcp_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def _sshd_binary() -> str | None:
    return shutil.which("sshd") or ("/usr/sbin/sshd" if Path("/usr/sbin/sshd").is_file() else None)


@pytest.mark.skipif(
    _sshd_binary() is None or shutil.which("ssh") is None or shutil.which("ssh-keyscan") is None,
    reason="Real OpenSSH sshd/ssh/ssh-keyscan binaries are required for local sshd integration test.",
)
@pytest.mark.asyncio
async def test_real_local_sshd_probe_pinned_ip_passphrase_agent_command_bundle_and_revoke(
    tmp_path: Path,
) -> None:
    """C14: Real OpenSSH probe, pinned-IP non-default port HostKeyAlias, passphrase agent, commands, upload, revoke."""

    sshd_bin = _sshd_binary()
    assert sshd_bin is not None
    sshd_dir = tmp_path / "sshd"
    sshd_dir.mkdir(mode=0o700)
    host_key_path, _, host_pub = _generate_ed25519_keypair(sshd_dir, name="ssh_host_ed25519_key")
    host_key_path.chmod(0o600)
    passphrase = "RealSSHDKeyPassphrase!91"
    _, client_priv, client_pub = _generate_ed25519_keypair(
        tmp_path / "client_key", name="id_ed25519", passphrase=passphrase
    )
    auth_keys = sshd_dir / "authorized_keys"
    auth_keys.write_text(f"{client_pub}\n", encoding="utf-8")
    auth_keys.chmod(0o600)
    port = _free_tcp_port()
    current_user = os.environ.get("USER") or subprocess.check_output(["id", "-un"], text=True).strip()
    sshd_config = sshd_dir / "sshd_config"
    sshd_config.write_text(
        f"Port {port}\n"
        "ListenAddress 127.0.0.1\n"
        f"HostKey {host_key_path}\n"
        f"AuthorizedKeysFile {auth_keys}\n"
        "PasswordAuthentication no\n"
        "KbdInteractiveAuthentication no\n"
        "PubkeyAuthentication yes\n"
        "UsePAM no\n"
        "StrictModes no\n"
        "Subsystem sftp internal-sftp\n"
        f"PidFile {sshd_dir / 'sshd.pid'}\n",
        encoding="utf-8",
    )
    sshd_proc = subprocess.Popen(
        [sshd_bin, "-D", "-e", "-f", str(sshd_config)],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    try:
        # Wait until local sshd accepts connections.
        ready = False
        for _ in range(50):
            if sshd_proc.poll() is not None:
                break
            try:
                with socket.create_connection(("127.0.0.1", port), timeout=0.1):
                    ready = True
                    break
            except OSError:
                time.sleep(0.05)
        if not ready:
            stderr_text = (sshd_proc.stderr.read() if sshd_proc.stderr else b"").decode("utf-8", errors="replace")
            pytest.skip(f"Local unprivileged sshd could not bind/start in this environment: {stderr_text}")

        # Policy explicitly allows 127.0.0.1/32 for this test and resolves hostname 'localtest.example' -> 127.0.0.1.
        local_policy = SSHNetworkPolicy(
            allowed_cidrs=(ipaddress.ip_network("127.0.0.1/32"),),
            allowed_domains=("localtest.example",),
            denied_cidrs=(),
        )
        repository = SqliteExecutionControlRepository(str(tmp_path / "execution.db"))
        broker = _catalog_broker(
            tmp_path,
            repository,
            secrets_map={
                ("real_key", "ssh_private_key"): client_priv,
                ("real_key", "ssh_passphrase"): passphrase,
                ("real_key", "ssh_public_key"): client_pub,
            },
            ssh_executable=Path(shutil.which("ssh") or "ssh"),
            network_policy=local_policy,
            resolver=lambda host, _p: ("127.0.0.1",) if host == "localtest.example" else (),
            local_address_checker=lambda _ip: False,
            revocation_poll_interval_seconds=0.05,
        )

        # 1. Real ssh-keyscan probe returns exact ed25519 fingerprint matching ssh-host key.
        probed = broker.probe_host_key(host="localtest.example", port=port)
        expected_algo, expected_fp = ssh_public_key_sha256_fingerprint(host_pub)
        assert probed.host == "localtest.example"
        assert probed.port == port
        assert probed.resolved_ip == "127.0.0.1"
        assert probed.host_key_algorithm == expected_algo
        assert probed.host_key_fingerprint == expected_fp

        # 2. Enroll target using probed key on non-default port (verifying HostKeyAlias=[localtest.example]:<port>).
        target = _make_enrolled_target(
            target_id="real_local_host",
            host="localtest.example",
            port=port,
            username=current_user,
            platform=SSHTargetPlatform.LINUX,
            host_pub=probed.host_key_public_key,
            credential_key="real_key",
            credential_pub=client_pub,
        )
        repository.put_ssh_target(target)
        bounds = SSHDelegationBoundsV1(
            allow_commands=True,
            allow_workspace_transfer=True,
        )
        session = _create_session_and_delegation(repository, target, bounds, session_id="ssh_sess_real")

        # 3. Real OpenSSH command using passphrase-protected key in private ssh-agent + output scrubbing.
        cmd = await broker.run_command(
            session_id=session.session_id,
            command=f"printf 'hello-real-sshd:{passphrase}\\n'",
            caller=_LEAD_CALLER,
            wait_seconds=10.0,
        )
        assert cmd.status == SSHCommandStatus.COMPLETED, cmd
        assert "hello-real-sshd:[REDACTED]" in cmd.stdout
        assert passphrase not in cmd.stdout

        # 4. Real workspace bundle upload over real sshd.
        upload_dest = tmp_path / "real_sshd_upload_dest"
        uploaded = await broker.upload_bundle(
            session_id=session.session_id,
            entries=[("app/config.txt", b"mode=prod\n")],
            remote_destination=str(upload_dest),
            caller=_LEAD_CALLER,
        )
        assert uploaded.file_count == 1
        assert (upload_dest / "app" / "config.txt").read_text(encoding="utf-8") == "mode=prod\n"

        # 5. Live revocation immediately terminates a long-running real ssh command.
        long_cmd = await broker.run_command(
            session_id=session.session_id,
            command="sleep 30",
            caller=_LEAD_CALLER,
            wait_seconds=0.2,
        )
        assert long_cmd.status == SSHCommandStatus.RUNNING
        t0 = time.monotonic()
        await broker.revoke_session(session_id=session.session_id, revoked_by="admin_real")
        assert time.monotonic() - t0 < 4.0
        after = await broker.get_command(
            session_id=session.session_id, command_id=long_cmd.command_id, caller=_LEAD_CALLER
        )
        assert after.status == SSHCommandStatus.REVOKED
    finally:
        if sshd_proc.poll() is None:
            sshd_proc.terminate()
            try:
                sshd_proc.wait(timeout=3)
            except subprocess.TimeoutExpired:
                sshd_proc.kill()
                sshd_proc.wait(timeout=3)
