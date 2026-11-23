"""Broker-side SSH operational primitives, sudo recipes, live revocation, and real local sshd tests."""

from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone
import ipaddress
import json
import logging
import os
from pathlib import Path
import shlex
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
    SSHSessionUnavailable,
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
            "res = subprocess.run(['sh', '-c', cmd], env=env, start_new_session=True)\n"
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


def _write_fake_sudo(
    directory: Path,
    *,
    behavior: str,
    expected_password: str = "SuperAdminPass!99",
    calls_log: Path | None = None,
    isolate_child_session: bool = True,
) -> Path:
    """Create a realistic fake sudo binary matching real sudo's -k, TS_PPID, and process-group isolation.

    - ``-k`` paired with ``-v`` validates the password but NEVER writes a timestamp
      (matching real sudo's ``MODE_IGNORE_TICKET``).
    - Without a TTY, timestamps are keyed strictly on ``os.getppid()`` (matching real
      sudo's ``TS_PPID``), so ``exec sudo`` from the outer shell fails with
      ``a password is required``.
    - When ``isolate_child_session`` is True, runs the target command with
      ``start_new_session=True`` so the calling non-root shell's own process-group
      ``kill -TERM 0`` cannot reach the child (matching root ownership / ``use_pty``).
    """

    directory.mkdir(parents=True, exist_ok=True)
    stamp_dir = directory / f"sudo_stamps_{behavior}"
    stamp_dir.mkdir(parents=True, exist_ok=True)
    binary = directory / f"fake-sudo-{behavior}"
    calls_log_str = str(calls_log) if calls_log is not None else ""
    binary.write_text(
        "#!/usr/bin/env python3\n"
        "import json, os, pathlib, subprocess, sys\n"
        f"behavior = {behavior!r}\n"
        f"expected = {expected_password!r}\n"
        f"stamp_dir = pathlib.Path({str(stamp_dir)!r})\n"
        f"calls_log = pathlib.Path({calls_log_str!r}) if {bool(calls_log_str)!r} else None\n"
        f"isolate_session = {isolate_child_session!r}\n"
        "stamp = stamp_dir / f'ppid_{os.getppid()}'\n"
        "args = sys.argv[1:]\n"
        "if calls_log is not None:\n"
        "    with calls_log.open('a', encoding='utf-8') as fh:\n"
        "        fh.write(json.dumps(args) + '\\n')\n"
        "if '-v' in args and '-S' in args:\n"
        "    pw = sys.stdin.readline().rstrip('\\r\\n')\n"
        "    if pw != expected:\n"
        "        sys.stderr.write('Sorry, try again.\\n')\n"
        "        sys.exit(1)\n"
        "    # Real sudo with -k (MODE_IGNORE_TICKET) never updates cached credentials.\n"
        "    if behavior != 'no_timestamp' and '-k' not in args:\n"
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
        "    res = subprocess.run(cmd, start_new_session=isolate_session)\n"
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

    # Long-running command streams live output while RUNNING (including quiet prompt and split-secret scrubbing).
    started = await broker.run_command(
        session_id=session.session_id,
        command=(
            "printf 'step1\\n'; "
            "printf 'KeyPass'; sleep 0.1; printf 'phrase!42\\n'; "
            "printf 'Continue? [y/N]'; "
            "sleep 0.9; printf '\\nstep2\\n'"
        ),
        caller=_WORKER_CALLER,
        wait_seconds=0.05,
    )
    assert started.status == SSHCommandStatus.RUNNING
    live_seen = ""
    poll_deadline = time.monotonic() + 2.0
    while time.monotonic() < poll_deadline:
        polled = await broker.get_command(
            session_id=session.session_id,
            command_id=started.command_id,
            caller=_WORKER_CALLER,
            wait_seconds=0.0,
        )
        assert "KeyPassphrase!42" not in polled.stdout
        if polled.status == SSHCommandStatus.RUNNING and "step1" in polled.stdout and "Continue? [y/N]" in polled.stdout:
            live_seen = polled.stdout
            break
        if polled.status != SSHCommandStatus.RUNNING:
            break
        await asyncio.sleep(0.05)
    assert "step1" in live_seen
    assert "Continue? [y/N]" in live_seen
    assert "[REDACTED]" in live_seen
    assert "KeyPassphrase!42" not in live_seen

    finished = await broker.get_command(
        session_id=session.session_id,
        command_id=started.command_id,
        caller=_WORKER_CALLER,
        wait_seconds=5.0,
    )
    assert finished.status == SSHCommandStatus.COMPLETED
    assert "step1" in finished.stdout and "step2" in finished.stdout
    assert "KeyPassphrase!42" not in finished.stdout

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

    # (b) Password-required fake sudo with timestamp: validates alone via -S -p '' -v (no -k, TS_PPID-keyed), command='rev' sees empty stdin!
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

    # Revoking a running sudo command terminates the remote child process too (C55).
    sudo_pid_file = tmp_path / "sudo_revoke.pid"
    session_sudo_rev = _create_session_and_delegation(
        repository, target, bounds, session_id="ssh_sess_sudo_rev"
    )
    sudo_long = await broker_pw.run_sudo(
        session_id=session_sudo_rev.session_id,
        command=f"echo $$ > {sudo_pid_file}; sleep 60",
        caller=_LEAD_CALLER,
        wait_seconds=0.4,
    )
    assert sudo_long.status == SSHCommandStatus.RUNNING
    wait_sudo = time.monotonic() + 3.0
    while time.monotonic() < wait_sudo and not sudo_pid_file.is_file():
        await asyncio.sleep(0.05)
    assert sudo_pid_file.is_file()
    sudo_child_pid = int(sudo_pid_file.read_text(encoding="utf-8").strip())
    await broker_pw.revoke_session(session_id=session_sudo_rev.session_id, revoked_by="admin_sudo_rev")
    sudo_after = await broker_pw.get_command(
        session_id=session_sudo_rev.session_id, command_id=sudo_long.command_id, caller=_LEAD_CALLER
    )
    assert sudo_after.status == SSHCommandStatus.REVOKED
    await asyncio.sleep(0.4)
    with pytest.raises(ProcessLookupError):
        os.kill(sudo_child_pid, 0)

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

    # (i) #186 (A2): Recipe-only delegation (allow_broker_sudo=False) runs approved tailscale_up
    #     while refusing free-form sudo commands, C03 combinations, recipe_params on free commands,
    #     and Windows recipe-only targets.
    ts_record_recipe_only = tmp_path / "ts_record_recipe_only.json"
    fake_ts_recipe_only = _write_fake_tailscale(tmp_path / "ts_bin_recipe_only", ts_record_recipe_only)
    broker_recipe_only = _catalog_broker(
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
        tailscale_search_paths=(str(fake_ts_recipe_only),),
    )
    recipe_only_session = _create_session_and_delegation(
        repository,
        target,
        SSHDelegationBoundsV1(
            allow_broker_sudo=False,
            allowed_secret_recipes=(SSHSecretRecipe.TAILSCALE_UP,),
        ),
        session_id="ssh_sess_recipe_only",
    )
    # Free-form sudo command is refused with a clear message explaining unrestricted broker sudo was not approved.
    with pytest.raises(
        ExecutionAuthorizationDenied,
        match=r"Unrestricted broker-assisted sudo \(allow_broker_sudo\) was not approved",
    ):
        await broker_recipe_only.run_sudo(
            session_id=recipe_only_session.session_id,
            command="id",
            caller=_LEAD_CALLER,
        )
    # Combining free-form command with secret_recipe / extra_secret_field stays strictly refused (C03).
    with pytest.raises(ValueError, match="Do not pass free command text together with a broker secret_recipe"):
        await broker_recipe_only.run_sudo(
            session_id=recipe_only_session.session_id,
            command="id",
            secret_recipe=SSHSecretRecipe.TAILSCALE_UP,
            extra_secret_field="tailscale_auth_key",
            caller=_LEAD_CALLER,
        )
    with pytest.raises(PermissionError, match="never piped to arbitrary agent-written commands"):
        await broker_recipe_only.run_sudo(
            session_id=recipe_only_session.session_id,
            command="id",
            extra_secret_field="tailscale_auth_key",
            caller=_LEAD_CALLER,
        )
    with pytest.raises(ValueError, match="recipe_params can only be used with an approved broker secret_recipe"):
        await broker_recipe_only.run_sudo(
            session_id=session.session_id,
            command="id",
            recipe_params={"hostname": "bad"},
            caller=_LEAD_CALLER,
        )
    # Approved tailscale_up recipe succeeds on the recipe-only delegation (with explicit extra_secret_field).
    recipe_only_res = await broker_recipe_only.run_sudo(
        session_id=recipe_only_session.session_id,
        secret_recipe="tailscale_up",
        extra_secret_field="tailscale_auth_key",
        recipe_params={"hostname": "recipe-only-node", "ssh": True},
        caller=_LEAD_CALLER,
    )
    assert recipe_only_res.status == SSHCommandStatus.COMPLETED, recipe_only_res
    assert ts_key not in recipe_only_res.stdout
    assert "[REDACTED]" in recipe_only_res.stdout
    ro_recorded = json.loads(ts_record_recipe_only.read_text(encoding="utf-8"))
    assert ro_recorded["key_value"] == ts_key
    assert ro_recorded["key_mode"] == "0o600"
    assert "--hostname" in ro_recorded["args"] and "recipe-only-node" in ro_recorded["args"]
    assert not Path(ro_recorded["key_path"]).exists()

    # Windows target still refuses recipe-only delegations.
    win_recipe_session = _create_session_and_delegation(
        repository,
        win_target,
        SSHDelegationBoundsV1(
            allow_broker_sudo=False,
            allowed_secret_recipes=(SSHSecretRecipe.TAILSCALE_UP,),
        ),
        session_id="ssh_sess_win_recipe_only",
    )
    with pytest.raises(ExecutionAuthorizationDenied, match="not supported on Windows"):
        await broker_recipe_only.run_sudo(
            session_id=win_recipe_session.session_id,
            secret_recipe=SSHSecretRecipe.TAILSCALE_UP,
            caller=_LEAD_CALLER,
        )

    # Target without bootstrap credential still refuses recipe-only delegations.
    no_boot_target = _make_enrolled_target(
        target_id="linux_no_boot_recipe",
        host="198.51.100.72",
        port=22,
        username="ops",
        platform=SSHTargetPlatform.LINUX,
        host_pub=host_pub,
        credential_key="ops_key",
        credential_pub=client_pub,
        bootstrap_credential_key=None,
    )
    repository.put_ssh_target(no_boot_target)
    no_boot_session = _create_session_and_delegation(
        repository,
        no_boot_target,
        SSHDelegationBoundsV1(
            allow_broker_sudo=False,
            allowed_secret_recipes=(SSHSecretRecipe.TAILSCALE_UP,),
        ),
        session_id="ssh_sess_noboot_recipe_only",
    )
    with pytest.raises(ExecutionAuthorizationDenied, match="bootstrap credential"):
        await broker_recipe_only.run_sudo(
            session_id=no_boot_session.session_id,
            secret_recipe=SSHSecretRecipe.TAILSCALE_UP,
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

    # C60(d): ~/app_tilde expands to $HOME/app_tilde rather than a literal '~' directory.
    tilde_uploaded = await broker.upload_bundle(
        session_id=session.session_id,
        entries=[("hello.txt", b"tilde-ok\n")],
        remote_destination="~/app_tilde",
        caller=_LEAD_CALLER,
    )
    assert tilde_uploaded.file_count == 1
    assert (tmp_path / "home" / "app_tilde" / "hello.txt").read_text(encoding="utf-8") == "tilde-ok\n"
    assert not (tmp_path / "home" / "~").exists()


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

        # 5. Live revocation immediately terminates BOTH local ssh AND the actual remote processes on host (C55).
        pid_file_revoke = tmp_path / "remote_revoke.pid"
        tick_file_revoke = tmp_path / "remote_revoke.ticks"
        long_cmd = await broker.run_command(
            session_id=session.session_id,
            command=(
                f"echo $$ > {pid_file_revoke}; "
                f"while true; do date +%s >> {tick_file_revoke}; sleep 0.15; done"
            ),
            caller=_LEAD_CALLER,
            wait_seconds=0.4,
        )
        assert long_cmd.status == SSHCommandStatus.RUNNING
        # Wait until the remote loop has actually written its PID and first tick.
        wait_start = time.monotonic() + 3.0
        while time.monotonic() < wait_start and not (pid_file_revoke.is_file() and tick_file_revoke.is_file()):
            await asyncio.sleep(0.05)
        assert pid_file_revoke.is_file()
        remote_pid_revoke = int(pid_file_revoke.read_text(encoding="utf-8").strip())

        t0 = time.monotonic()
        await broker.revoke_session(session_id=session.session_id, revoked_by="admin_real")
        assert time.monotonic() - t0 < 4.0
        after = await broker.get_command(
            session_id=session.session_id, command_id=long_cmd.command_id, caller=_LEAD_CALLER
        )
        assert after.status == SSHCommandStatus.REVOKED
        await asyncio.sleep(0.4)
        with pytest.raises(ProcessLookupError):
            os.kill(remote_pid_revoke, 0)
        lines_after_revoke = len(tick_file_revoke.read_text(encoding="utf-8").splitlines())
        await asyncio.sleep(0.45)
        assert len(tick_file_revoke.read_text(encoding="utf-8").splitlines()) == lines_after_revoke

        # 6. Command timeout terminates the actual remote process on the host (C55).
        session_timeout = _create_session_and_delegation(
            repository, target, bounds, session_id="ssh_sess_real_timeout"
        )
        pid_file_timeout = tmp_path / "remote_timeout.pid"
        timed_cmd = await broker.run_command(
            session_id=session_timeout.session_id,
            command=f"echo $$ > {pid_file_timeout}; sleep 60",
            caller=_LEAD_CALLER,
            timeout_seconds=1.0,
            wait_seconds=5.0,
        )
        assert timed_cmd.status == SSHCommandStatus.TIMED_OUT
        assert pid_file_timeout.is_file()
        remote_pid_timeout = int(pid_file_timeout.read_text(encoding="utf-8").strip())
        await asyncio.sleep(0.4)
        with pytest.raises(ProcessLookupError):
            os.kill(remote_pid_timeout, 0)

        # 7. Delegation expiry while a command is running terminates the actual remote process (C55).
        session_exp = _create_session_and_delegation(
            repository,
            target,
            bounds,
            session_id="ssh_sess_real_expiry",
            expires_in_seconds=2,
        )
        pid_file_exp = tmp_path / "remote_expiry.pid"
        exp_cmd = await broker.run_command(
            session_id=session_exp.session_id,
            command=f"echo $$ > {pid_file_exp}; sleep 60",
            caller=_LEAD_CALLER,
            timeout_seconds=30.0,
            wait_seconds=0.4,
        )
        assert exp_cmd.status == SSHCommandStatus.RUNNING
        wait_exp = time.monotonic() + 3.0
        while time.monotonic() < wait_exp and not pid_file_exp.is_file():
            await asyncio.sleep(0.05)
        assert pid_file_exp.is_file()
        remote_pid_exp = int(pid_file_exp.read_text(encoding="utf-8").strip())
        exp_after = await broker.get_command(
            session_id=session_exp.session_id,
            command_id=exp_cmd.command_id,
            caller=_LEAD_CALLER,
            wait_seconds=5.0,
        )
        assert exp_after.status in {SSHCommandStatus.REVOKED, SSHCommandStatus.TIMED_OUT}
        await asyncio.sleep(0.4)
        with pytest.raises(ProcessLookupError):
            os.kill(remote_pid_exp, 0)
        await broker.shutdown()
    finally:
        if sshd_proc.poll() is None:
            sshd_proc.terminate()
            try:
                sshd_proc.wait(timeout=3)
            except subprocess.TimeoutExpired:
                sshd_proc.kill()
                sshd_proc.wait(timeout=3)


# -- 8. C54-C60 regression coverage: scalability, cancellation, restart, static targets, edge cases --


@pytest.mark.asyncio
async def test_c56_async_commands_do_not_starve_thread_pool_and_enforce_concurrency_limits(
    tmp_path: Path,
) -> None:
    _, _, host_pub = _generate_ed25519_keypair(tmp_path / "host", name="host_ed25519")
    _, client_priv, client_pub = _generate_ed25519_keypair(tmp_path / "client", name="client_ed25519")
    repository = SqliteExecutionControlRepository(str(tmp_path / "execution.db"))
    target = _make_enrolled_target(
        target_id="pool_host",
        host="198.51.100.90",
        port=22,
        username="deploy",
        platform=SSHTargetPlatform.LINUX,
        host_pub=host_pub,
        credential_key="ops_key",
        credential_pub=client_pub,
    )
    repository.put_ssh_target(target)
    bounds = SSHDelegationBoundsV1(allow_commands=True)
    session_a = _create_session_and_delegation(repository, target, bounds, session_id="ssh_sess_pool_a")
    session_b = _create_session_and_delegation(repository, target, bounds, session_id="ssh_sess_pool_b")
    broker = _catalog_broker(
        tmp_path,
        repository,
        secrets_map={
            ("ops_key", "ssh_private_key"): client_priv,
            ("ops_key", "ssh_public_key"): client_pub,
        },
        ssh_executable=_write_fake_ssh_exec(tmp_path / "bin", mode="exec", state_dir=tmp_path / "home"),
    )
    broker.max_concurrent_commands_per_session = 6
    broker.max_concurrent_commands_per_broker = 7
    try:
        # Start 6 long-running commands on session_a; with subprocess threads this would starve the pool.
        for _ in range(6):
            res = await broker.run_command(
                session_id=session_a.session_id,
                command="sleep 30",
                caller=_LEAD_CALLER,
                wait_seconds=0.02,
            )
            assert res.status == SSHCommandStatus.RUNNING

        # Unrelated asyncio.to_thread must complete immediately (zero threads held by commands).
        t0 = time.monotonic()
        value = await asyncio.wait_for(asyncio.to_thread(lambda: "pool-free"), timeout=1.0)
        assert value == "pool-free"
        assert time.monotonic() - t0 < 0.5

        # 7th command on session_a hits the per-session limit.
        with pytest.raises(RuntimeError, match="already has 6 running commands"):
            await broker.run_command(
                session_id=session_a.session_id,
                command="echo nope",
                caller=_LEAD_CALLER,
                wait_seconds=0.02,
            )

        # 1st command on session_b reaches broker limit (7), 2nd is refused by broker limit.
        res_b = await broker.run_command(
            session_id=session_b.session_id,
            command="sleep 30",
            caller=_LEAD_CALLER,
            wait_seconds=0.02,
        )
        assert res_b.status == SSHCommandStatus.RUNNING
        with pytest.raises(RuntimeError, match="SSH broker already has 7 running commands"):
            await broker.run_command(
                session_id=session_b.session_id,
                command="echo nope",
                caller=_LEAD_CALLER,
                wait_seconds=0.02,
            )
    finally:
        await broker.shutdown()


@pytest.mark.asyncio
async def test_c58_cancel_shutdown_crash_pid_sweep_and_enroll_cancel_cleanup(tmp_path: Path) -> None:
    _, _, host_pub = _generate_ed25519_keypair(tmp_path / "host", name="host_ed25519")
    _, client_priv, client_pub = _generate_ed25519_keypair(
        tmp_path / "client", name="client_ed25519", passphrase="CancelPass!44"
    )
    repository = SqliteExecutionControlRepository(str(tmp_path / "execution.db"))
    target = _make_enrolled_target(
        target_id="cancel_host",
        host="198.51.100.91",
        port=22,
        username="deploy",
        platform=SSHTargetPlatform.LINUX,
        host_pub=host_pub,
        credential_key="ops_key",
        credential_pub=client_pub,
        bootstrap_credential_key="ops_boot",
    )
    repository.put_ssh_target(target)
    bounds = SSHDelegationBoundsV1(allow_commands=True, allow_password_bootstrap=True)
    session = _create_session_and_delegation(repository, target, bounds, session_id="ssh_sess_cancel")
    ephemeral_dir = tmp_path / "ssh-ephemeral"
    broker = _catalog_broker(
        tmp_path,
        repository,
        secrets_map={
            ("ops_key", "ssh_private_key"): client_priv,
            ("ops_key", "ssh_passphrase"): "CancelPass!44",
            ("ops_key", "ssh_public_key"): client_pub,
            ("ops_boot", "admin_password"): "BootSecret!44",
        },
        ssh_executable=_write_fake_ssh_exec(tmp_path / "bin", mode="exec", state_dir=tmp_path / "home"),
        allow_password_bootstrap=True,
    )

    # (a) Cancelling a running command's runner_task stops the child process and marks a terminal status.
    pid_file = tmp_path / "cancelled_cmd.pid"
    running = await broker.run_command(
        session_id=session.session_id,
        command=f"echo $$ > {pid_file}; sleep 60",
        caller=_LEAD_CALLER,
        wait_seconds=0.3,
    )
    assert running.status == SSHCommandStatus.RUNNING
    wait_until = time.monotonic() + 3.0
    while time.monotonic() < wait_until and not pid_file.is_file():
        await asyncio.sleep(0.05)
    assert pid_file.is_file()
    child_pid = int(pid_file.read_text(encoding="utf-8").strip())

    live_cmd = broker._commands[running.command_id]
    assert live_cmd.runner_task is not None
    live_cmd.runner_task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await live_cmd.runner_task
    after_cancel = await broker.get_command(
        session_id=session.session_id, command_id=running.command_id, caller=_LEAD_CALLER
    )
    assert after_cancel.status == SSHCommandStatus.FAILED
    assert after_cancel.error and "interrupted" in after_cancel.error
    await asyncio.sleep(0.3)
    with pytest.raises(ProcessLookupError):
        os.kill(child_pid, 0)
    assert list(ephemeral_dir.glob("ua-ssh-*")) == []

    # (b) Crash recovery: leftover ssh.pid in a stale ephemeral dir is killed on recover_after_restart,
    # and a restarted broker's get_command explains clearly that the outcome is unknown.
    from runtime_hosting.ssh_credentials import record_ssh_child_pid

    stale_dir = ephemeral_dir / "ua-ssh-crashed1234"
    stale_dir.mkdir(parents=True, exist_ok=True)
    orphan = subprocess.Popen(["sleep", "60"], start_new_session=True)
    record_ssh_child_pid(stale_dir, orphan.pid)
    restarted_broker = _catalog_broker(
        tmp_path,
        repository,
        secrets_map={
            ("ops_key", "ssh_private_key"): client_priv,
            ("ops_key", "ssh_passphrase"): "CancelPass!44",
            ("ops_key", "ssh_public_key"): client_pub,
        },
        ssh_executable=_write_fake_ssh_exec(tmp_path / "bin", mode="exec", state_dir=tmp_path / "home"),
    )
    restarted_broker.recover_after_restart()
    orphan.wait(timeout=3)
    assert orphan.poll() is not None
    assert not stale_dir.exists()
    with pytest.raises(LookupError, match="broker may have restarted.*outcome is unknown"):
        await restarted_broker.get_command(
            session_id=session.session_id, command_id=running.command_id, caller=_LEAD_CALLER
        )

    # (c) Cancelling enroll_ssh_key deterministically mid-prepare raises CancelledError and leaks nothing (C65(g)).
    import threading
    from contextlib import contextmanager

    entered_prepare = threading.Event()
    release_prepare = threading.Event()
    catalog_provider = broker._catalog_provider()
    orig_prepare = catalog_provider.prepare

    @contextmanager
    def _gated_prepare(*args: Any, **kwargs: Any) -> Any:
        with orig_prepare(*args, **kwargs) as mat:
            entered_prepare.set()
            release_prepare.wait(timeout=5.0)
            yield mat

    catalog_provider.prepare = _gated_prepare  # type: ignore[method-assign]
    enroll_task = asyncio.create_task(
        broker.enroll_ssh_key(session_id=session.session_id, caller=_LEAD_CALLER, timeout_seconds=30.0)
    )
    wait_enter = time.monotonic() + 5.0
    while time.monotonic() < wait_enter and not entered_prepare.is_set():
        await asyncio.sleep(0.02)
    assert entered_prepare.is_set(), "enroll_ssh_key did not reach credential preparation before cancel"
    enroll_task.cancel()
    release_prepare.set()
    with pytest.raises(asyncio.CancelledError):
        await enroll_task
    await asyncio.sleep(0.2)
    assert list(ephemeral_dir.glob("ua-ssh-*")) == []
    await broker.shutdown()


@pytest.mark.asyncio
async def test_c59_and_c60_static_target_policy_bypass_legacy_expiry_tilde_and_sid_icacls(
    tmp_path: Path,
) -> None:
    from runtime_hosting.execution_contracts import SSHConnectionTargetV1
    from runtime_hosting.ssh_operations import build_bundle_extract_command, build_key_install_command
    from tests.execution_access_support import host_key_fingerprint, write_ssh_material

    # C60(d): ~/app expands $HOME rather than creating a literal '~' directory; ~other is refused.
    posix_tilde = build_bundle_extract_command("~/app", SSHTargetPlatform.LINUX)[0]
    assert '"$HOME"/app' in posix_tilde
    with pytest.raises(ValueError, match="~user"):
        build_bundle_extract_command("~otheruser/app", SSHTargetPlatform.LINUX)

    # C60(e): Windows key install uses locale-independent well-known SIDs with icacls.
    import base64

    win_cmd = build_key_install_command(SSHTargetPlatform.WINDOWS)[0]
    encoded_ps = win_cmd.split("-EncodedCommand ", 1)[1]
    decoded_ps = base64.b64decode(encoded_ps).decode("utf-16-le")
    assert "*S-1-5-32-544:F" in decoded_ps
    assert "*S-1-5-18:F" in decoded_ps
    assert "Administrators:F" not in decoded_ps

    # C59 & C60(c): Static operator-pinned target (target_id=None) on 127.0.0.1 succeeds even when
    # the broker carries a strict SSHNetworkPolicy allowlist, connects without HostName/HostKeyAlias,
    # and keeps its live PTY open for reading when a legacy 0.9 typing approval expires.
    key_bytes = b"static-target-host-key"
    identity, known_hosts = write_ssh_material(tmp_path / "static_ssh", key_bytes=key_bytes)
    known_hosts.write_text(
        f"127.0.0.1 ssh-ed25519 {base64.b64encode(key_bytes).decode('ascii')}\n",
        encoding="utf-8",
    )
    argv_log = tmp_path / "static_argv.txt"
    fake_ssh_static = tmp_path / "bin" / "fake-ssh-static"
    fake_ssh_static.parent.mkdir(parents=True, exist_ok=True)
    fake_ssh_static.write_text(
        "#!/usr/bin/env python3\n"
        "import pathlib, sys, time\n"
        f"pathlib.Path({str(argv_log)!r}).write_text('\\n'.join(sys.argv[1:]), encoding='utf-8')\n"
        "sys.stdout.write('static-ready\\n')\n"
        "sys.stdout.flush()\n"
        "time.sleep(5)\n",
        encoding="utf-8",
    )
    fake_ssh_static.chmod(0o755)

    repository = SqliteExecutionControlRepository(str(tmp_path / "execution.db"))
    static_target = SSHConnectionTargetV1(
        host="127.0.0.1",
        username="deploy",
        host_key_algorithm="ssh-ed25519",
        host_key_fingerprint=host_key_fingerprint(key_bytes),
        credential_binding_id="static_binding",
    )
    static_digest = static_target.calculate_digest()
    static_session = InteractiveSessionV1(
        session_id="ssh_sess_static_c59",
        owner=_USER_OWNER,
        target=static_target,
        target_digest=static_digest,
        connect_activation=build_activation(
            ExecutionActivationKind.SSH_CONNECT,
            resource_id="ssh_sess_static_c59",
            resource_digest=static_digest,
            owner_scope=_USER_OWNER,
            function="create_execution_ssh_session",
            expires_at=datetime.now(timezone.utc) + timedelta(hours=1),
        ),
    )
    repository.create_interactive_session(static_session)
    # Legacy 0.9 typing approval (bounds=None) that expires quickly.
    repository.put_ssh_interaction(
        build_activation(
            ExecutionActivationKind.SSH_INTERACT,
            resource_id="ssh_sess_static_c59",
            resource_digest=static_digest,
            owner_scope=_USER_OWNER,
            function="change:execution_ssh_interaction",
            expires_at=datetime.now(timezone.utc) + timedelta(milliseconds=100),
        )
    )
    strict_policy = SSHNetworkPolicy(
        allowed_cidrs=(ipaddress.ip_network("10.0.0.0/8"),),
        allowed_domains=(),
        denied_cidrs=(ipaddress.ip_network("127.0.0.0/8"),),
    )
    from runtime_hosting.ssh import SSHCredentialMaterial

    broker = SSHSessionBroker(
        repository=repository,
        authorization_guard=_AGREEMENT_GUARD,
        material_provider=ConfiguredSSHCredentialMaterialProvider(
            {"static_binding": SSHCredentialMaterial(identity_file=identity, known_hosts_file=known_hosts)}
        ),
        artifact_store=FilesystemArtifactStore(tmp_path / "artifacts"),
        team_id="team_alpha",
        broker_id="broker-static-c59",
        ssh_executable=str(fake_ssh_static),
        network_policy=strict_policy,
        revocation_poll_interval_seconds=0.05,
    )
    try:
        opened = await broker.open(
            session_id="ssh_sess_static_c59",
            lease_token="lease-static",
            caller=_LEAD_CALLER,
        )
        assert opened.status == InteractiveSessionStatus.OPEN
        wait_argv = time.monotonic() + 3.0
        while time.monotonic() < wait_argv and not argv_log.is_file():
            await asyncio.sleep(0.05)
        logged_argv = argv_log.read_text(encoding="utf-8")
        assert "HostKeyAlias=" not in logged_argv
        assert "HostName=" not in logged_argv

        # Wait past legacy typing approval expiry; monitor must NOT close static read-only PTY (C60(c)).
        await asyncio.sleep(0.3)
        assert "ssh_sess_static_c59" in broker._sessions
        out = await broker.read(
            session_id="ssh_sess_static_c59",
            timeout_seconds=1.0,
            caller=_LEAD_CALLER,
        )
        assert b"static-ready" in out
        # Typing is refused once the legacy approval has expired.
        with pytest.raises(ExecutionAuthorizationDenied, match="expired"):
            await broker.write(
                session_id="ssh_sess_static_c59",
                payload=b"ls\n",
                caller=_LEAD_CALLER,
            )
    finally:
        await broker.shutdown()


@pytest.mark.asyncio
async def test_c60_run_sudo_rechecks_revocation_after_preflight_and_enroll_bounds_lease(
    tmp_path: Path,
) -> None:
    _, _, host_pub = _generate_ed25519_keypair(tmp_path / "host", name="host_ed25519")
    _, client_priv, client_pub = _generate_ed25519_keypair(tmp_path / "client", name="client_ed25519")
    repository = SqliteExecutionControlRepository(str(tmp_path / "execution.db"))
    target = _make_enrolled_target(
        target_id="c60_host",
        host="198.51.100.92",
        port=22,
        username="ops",
        platform=SSHTargetPlatform.LINUX,
        host_pub=host_pub,
        credential_key="ops_key",
        credential_pub=client_pub,
        bootstrap_credential_key="ops_boot",
    )
    repository.put_ssh_target(target)
    bounds = SSHDelegationBoundsV1(allow_broker_sudo=True, allow_password_bootstrap=True)
    session = _create_session_and_delegation(repository, target, bounds, session_id="ssh_sess_c60")

    # C60(b): Revocation happening between sudo preflight and password resolution must refuse
    # without ever resolving or sending admin_password.
    password_resolved = False

    def _resolver(key: str, field_name: str, _owner: OwnerScopeV1) -> str | None:
        nonlocal password_resolved
        if field_name == "admin_password":
            password_resolved = True
        return {
            ("ops_key", "ssh_private_key"): client_priv,
            ("ops_key", "ssh_public_key"): client_pub,
            ("ops_boot", "admin_password"): "NeverSendAfterRevoke!1",
        }.get((key, field_name))

    fake_sudo = _write_fake_sudo(tmp_path / "sudo_c60", behavior="password", expected_password="NeverSendAfterRevoke!1")
    catalog = CatalogSSHCredentialMaterialProvider(
        ephemeral_base_dir=tmp_path / "ssh-ephemeral-c60",
        secret_resolver=_resolver,
    )
    broker = SSHSessionBroker(
        repository=repository,
        authorization_guard=_AGREEMENT_GUARD,
        material_provider=CompositeSSHCredentialMaterialProvider(
            ConfiguredSSHCredentialMaterialProvider({}), catalog
        ),
        artifact_store=FilesystemArtifactStore(tmp_path / "artifacts"),
        team_id="team_alpha",
        broker_id="broker-c60",
        ssh_executable=str(_write_fake_ssh_exec(tmp_path / "bin", mode="exec", state_dir=tmp_path / "home")),
        network_policy=SSHNetworkPolicy.default(),
        resolver=lambda host, _port: (host,),
        local_address_checker=lambda _ip: False,
        sudo_search_paths=(str(fake_sudo),),
        allow_password_bootstrap=True,
    )

    orig_probe = broker._probe_remote_sudo

    async def _probe_then_revoke(**kwargs: Any) -> tuple[str, bool]:
        result = await orig_probe(**kwargs)
        repository.revoke_interactive_session(
            team_id="team_alpha", session_id=session.session_id, revoked_by="admin_mid_preflight"
        )
        return result

    broker._probe_remote_sudo = _probe_then_revoke  # type: ignore[method-assign]
    with pytest.raises(ExecutionAuthorizationDenied, match="revoked"):
        await broker.run_sudo(session_id=session.session_id, command="whoami", caller=_LEAD_CALLER)
    assert password_resolved is False

    # C60(a): Huge timeout_seconds on enroll_ssh_key is bounded to <= 3630s lease TTL (not raw timeout).
    session_boot = _create_session_and_delegation(repository, target, bounds, session_id="ssh_sess_c60_boot")
    recorded_ttls: list[int] = []
    orig_claim = repository.claim_interactive_session

    def _capturing_claim(*args: Any, **kwargs: Any) -> InteractiveSessionV1:
        recorded_ttls.append(int(kwargs["lease_ttl_seconds"]))
        return orig_claim(*args, **kwargs)

    repository.claim_interactive_session = _capturing_claim  # type: ignore[method-assign]
    enrolled = await broker.enroll_ssh_key(
        session_id=session_boot.session_id,
        caller=_LEAD_CALLER,
        timeout_seconds=10**9,
    )
    assert enrolled.verified_key_login is True
    assert recorded_ttls and recorded_ttls[0] <= 3630
    await broker.shutdown()


# -- 9. C61-C65 hardening coverage: expired sudo tickets, bg jobs, slow uploads, monitor isolation --


@pytest.mark.asyncio
async def test_c61_expired_sudo_ticket_and_isolated_session_stops_on_revoke_and_recipe(
    tmp_path: Path,
) -> None:
    """C61: Root-side watcher stops a sudo command even after the sudo ticket expires and under isolated pgrp."""

    _, _, host_pub = _generate_ed25519_keypair(tmp_path / "host", name="host_ed25519")
    _, client_priv, client_pub = _generate_ed25519_keypair(tmp_path / "client", name="client_ed25519")
    repository = SqliteExecutionControlRepository(str(tmp_path / "execution.db"))
    target = _make_enrolled_target(
        target_id="c61_host",
        host="198.51.100.93",
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
    session = _create_session_and_delegation(repository, target, bounds, session_id="ssh_sess_c61")

    sudo_dir = tmp_path / "sudo_c61"
    calls_log = tmp_path / "sudo_calls.jsonl"
    fake_sudo = _write_fake_sudo(
        sudo_dir,
        behavior="password",
        expected_password="SudoExpirePass!61",
        calls_log=calls_log,
        isolate_child_session=True,
    )
    stamp_dir = sudo_dir / "sudo_stamps_password"

    ts_pid_file = tmp_path / "slow_ts.pid"
    ts_keypath_file = tmp_path / "slow_ts.keypath"
    slow_tailscale = tmp_path / "bin" / "slow-tailscale"
    slow_tailscale.parent.mkdir(parents=True, exist_ok=True)
    slow_tailscale.write_text(
        "#!/usr/bin/env python3\n"
        "import os, pathlib, signal, sys, time\n"
        "signal.signal(signal.SIGTERM, signal.SIG_IGN)\n"
        f"pathlib.Path({str(ts_pid_file)!r}).write_text(str(os.getpid()), encoding='utf-8')\n"
        "for item in sys.argv[1:]:\n"
        "    if item.startswith('--auth-key=file:'):\n"
        f"        pathlib.Path({str(ts_keypath_file)!r}).write_text(item.split('file:', 1)[1], encoding='utf-8')\n"
        "time.sleep(60)\n",
        encoding="utf-8",
    )
    slow_tailscale.chmod(0o755)

    broker = _catalog_broker(
        tmp_path,
        repository,
        secrets_map={
            ("ops_key", "ssh_private_key"): client_priv,
            ("ops_key", "ssh_public_key"): client_pub,
            ("ops_boot", "admin_password"): "SudoExpirePass!61",
            ("ops_boot", "tailscale_auth_key"): "tskey-auth-c61-secret-value",
        },
        ssh_executable=_write_fake_ssh_exec(tmp_path / "bin", mode="exec", state_dir=tmp_path / "home"),
        sudo_search_paths=(str(fake_sudo),),
        tailscale_search_paths=(str(slow_tailscale),),
    )
    try:
        # (a) Free sudo command ignoring SIGTERM in an isolated session; expire ticket before revoke.
        pid_file = tmp_path / "c61_root_cmd.pid"
        running = await broker.run_sudo(
            session_id=session.session_id,
            command=f"trap '' TERM; echo $$ > {pid_file}; echo root-started; sleep 60",
            caller=_LEAD_CALLER,
            timeout_seconds=60.0,
            wait_seconds=0.4,
        )
        assert running.status == SSHCommandStatus.RUNNING
        deadline = time.monotonic() + 3.0
        while time.monotonic() < deadline and not pid_file.is_file():
            await asyncio.sleep(0.05)
        assert pid_file.is_file()
        root_pid = int(pid_file.read_text(encoding="utf-8").strip())
        os.kill(root_pid, 0)

        # Simulate sudo timestamp_timeout expiry: remove every cached ticket file.
        for stamp in stamp_dir.glob("ppid_*"):
            stamp.unlink()
        calls_before_revoke = len(calls_log.read_text(encoding="utf-8").splitlines())

        t0 = time.monotonic()
        await broker.revoke_session(session_id=session.session_id, revoked_by="admin_c61")
        assert time.monotonic() - t0 < 5.0
        after = await broker.get_command(
            session_id=session.session_id, command_id=running.command_id, caller=_LEAD_CALLER
        )
        assert after.status == SSHCommandStatus.REVOKED
        assert after.error and "remote process group signalled" in after.error
        await asyncio.sleep(0.2)
        with pytest.raises(ProcessLookupError):
            os.kill(root_pid, 0)
        # Stopping ran directly inside the root shell without calling sudo again.
        calls_after_revoke = len(calls_log.read_text(encoding="utf-8").splitlines())
        assert calls_after_revoke == calls_before_revoke

        # (b) Secret recipe (tailscale_up) ignoring SIGTERM with expired ticket: revoke kills it and wipes keyfile.
        session_ts = _create_session_and_delegation(
            repository, target, bounds, session_id="ssh_sess_c61_ts"
        )
        ts_cmd = await broker.run_sudo(
            session_id=session_ts.session_id,
            secret_recipe=SSHSecretRecipe.TAILSCALE_UP,
            recipe_params={"hostname": "c61-node"},
            caller=_LEAD_CALLER,
            timeout_seconds=60.0,
            wait_seconds=0.4,
        )
        assert ts_cmd.status == SSHCommandStatus.RUNNING
        deadline = time.monotonic() + 3.0
        while time.monotonic() < deadline and not (ts_pid_file.is_file() and ts_keypath_file.is_file()):
            await asyncio.sleep(0.05)
        assert ts_pid_file.is_file() and ts_keypath_file.is_file()
        ts_pid = int(ts_pid_file.read_text(encoding="utf-8").strip())
        keyfile_path = Path(ts_keypath_file.read_text(encoding="utf-8").strip())
        os.kill(ts_pid, 0)

        for stamp in stamp_dir.glob("ppid_*"):
            stamp.unlink()
        ts_calls_before = len(calls_log.read_text(encoding="utf-8").splitlines())

        await broker.revoke_session(session_id=session_ts.session_id, revoked_by="admin_c61_ts")
        await asyncio.sleep(0.2)
        with pytest.raises(ProcessLookupError):
            os.kill(ts_pid, 0)
        assert not keyfile_path.exists()
        assert len(calls_log.read_text(encoding="utf-8").splitlines()) == ts_calls_before
    finally:
        await broker.shutdown()


@pytest.mark.skipif(
    _sshd_binary() is None or shutil.which("ssh") is None,
    reason="Real OpenSSH sshd/ssh binaries are required for real-sshd C62/C65 test.",
)
@pytest.mark.asyncio
async def test_c62_and_c65_real_sshd_background_jobs_sigint_and_termination_notes(
    tmp_path: Path,
) -> None:
    """C62 & C65(a,c): Real sshd cleans up leftover background jobs, preserves SIGINT, and words notes accurately."""

    sshd_bin = _sshd_binary()
    assert sshd_bin is not None
    sshd_dir = tmp_path / "sshd_c62"
    sshd_dir.mkdir(mode=0o700)
    host_key_path, _, host_pub = _generate_ed25519_keypair(sshd_dir, name="ssh_host_ed25519_key")
    host_key_path.chmod(0o600)
    _, client_priv, client_pub = _generate_ed25519_keypair(tmp_path / "client_c62", name="id_ed25519")
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
        f"PidFile {sshd_dir / 'sshd.pid'}\n",
        encoding="utf-8",
    )
    sshd_proc = subprocess.Popen(
        [sshd_bin, "-D", "-e", "-f", str(sshd_config)],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    try:
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
            pytest.skip("Local unprivileged sshd could not bind/start.")

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
                ("real_key", "ssh_public_key"): client_pub,
            },
            ssh_executable=Path(shutil.which("ssh") or "ssh"),
            network_policy=local_policy,
            resolver=lambda host, _p: ("127.0.0.1",) if host == "localtest.example" else (),
            local_address_checker=lambda _ip: False,
            revocation_poll_interval_seconds=0.05,
        )
        target = _make_enrolled_target(
            target_id="c62_host",
            host="localtest.example",
            port=port,
            username=current_user,
            platform=SSHTargetPlatform.LINUX,
            host_pub=host_pub,
            credential_key="real_key",
            credential_pub=client_pub,
        )
        repository.put_ssh_target(target)
        bounds = SSHDelegationBoundsV1(allow_commands=True)
        session = _create_session_and_delegation(repository, target, bounds, session_id="ssh_sess_c62")
        try:
            # C65(a): Foreground layout preserves default SIGINT disposition and returns promptly with exit code.
            t0 = time.monotonic()
            sigint_res = await broker.run_command(
                session_id=session.session_id,
                command="trap 'echo got-int' INT; kill -INT $$; exit 7",
                caller=_LEAD_CALLER,
                wait_seconds=5.0,
            )
            assert time.monotonic() - t0 < 2.0
            assert sigint_res.status == SSHCommandStatus.FAILED
            assert sigint_res.exit_code == 7
            assert "got-int" in sigint_res.stdout

            # C62: Command leaving a background job holding stdout finishes and the background process is stopped.
            bg_pid_file = tmp_path / "bg_sleep.pid"
            bg_res = await broker.run_command(
                session_id=session.session_id,
                command=f"sleep 600 & echo $! > {bg_pid_file}; echo bg-started",
                caller=_LEAD_CALLER,
                timeout_seconds=10.0,
                wait_seconds=5.0,
            )
            assert bg_res.status == SSHCommandStatus.COMPLETED
            assert "bg-started" in bg_res.stdout
            assert bg_pid_file.is_file()
            bg_pid = int(bg_pid_file.read_text(encoding="utf-8").strip())
            await asyncio.sleep(0.2)
            with pytest.raises(ProcessLookupError):
                os.kill(bg_pid, 0)

            # C62 + C65(c) + C67: Even a background job that ignores SIGTERM is killed after
            # the 2s normal-exit drain grace plus the ~1.5s TERM-to-KILL grace period.
            bg_ign_pid_file = tmp_path / "bg_ign.pid"
            bg_ign_res = await broker.run_command(
                session_id=session.session_id,
                command=(
                    f"sh -c 'trap \"\" TERM; echo $$ > {bg_ign_pid_file}; sleep 600' & "
                    f"while [ ! -s {bg_ign_pid_file} ]; do sleep 0.02; done; echo bg-ign-started"
                ),
                caller=_LEAD_CALLER,
                timeout_seconds=12.0,
                wait_seconds=7.0,
            )
            assert bg_ign_res.status == SSHCommandStatus.COMPLETED
            assert "bg-ign-started" in bg_ign_res.stdout
            bg_ign_pid = int(bg_ign_pid_file.read_text(encoding="utf-8").strip())
            await asyncio.sleep(0.2)
            with pytest.raises(ProcessLookupError):
                os.kill(bg_ign_pid, 0)

            # C62 (Windows wording): Non-watched Windows target must not claim "remote process group signalled".
            win_target = _make_enrolled_target(
                target_id="c62_win_host",
                host="198.51.100.99",
                port=22,
                username="Administrator",
                platform=SSHTargetPlatform.WINDOWS,
                host_pub=host_pub,
                credential_key="real_key",
                credential_pub=client_pub,
            )
            repository.put_ssh_target(win_target)
            win_session = _create_session_and_delegation(
                repository, win_target, bounds, session_id="ssh_sess_c62_win"
            )
            sleeper_ssh = tmp_path / "bin" / "fake-ssh-win-sleep"
            sleeper_ssh.parent.mkdir(parents=True, exist_ok=True)
            sleeper_ssh.write_text("#!/usr/bin/env python3\nimport time; time.sleep(30)\n", encoding="utf-8")
            sleeper_ssh.chmod(0o755)
            win_broker = _catalog_broker(
                tmp_path,
                repository,
                secrets_map={
                    ("real_key", "ssh_private_key"): client_priv,
                    ("real_key", "ssh_public_key"): client_pub,
                },
                ssh_executable=sleeper_ssh,
            )
            try:
                win_res = await win_broker.run_command(
                    session_id=win_session.session_id,
                    command="Start-Sleep -Seconds 30",
                    caller=_LEAD_CALLER,
                    timeout_seconds=1.0,
                    wait_seconds=4.0,
                )
                assert win_res.status == SSHCommandStatus.TIMED_OUT
                assert win_res.error is not None
                assert "remote process group signalled" not in win_res.error
                assert "no remote signal watcher was active" in win_res.error
            finally:
                await win_broker.shutdown()
        finally:
            await broker.shutdown()
    finally:
        if sshd_proc.poll() is None:
            sshd_proc.terminate()
            try:
                sshd_proc.wait(timeout=3)
            except subprocess.TimeoutExpired:
                sshd_proc.kill()
                sshd_proc.wait(timeout=3)


@pytest.mark.asyncio
async def test_c63_slow_bundle_upload_honors_timeout_revoke_and_shutdown(tmp_path: Path) -> None:
    """C63: Stdin upload runs concurrently with deadline, revoke_session, and shutdown."""

    _, _, host_pub = _generate_ed25519_keypair(tmp_path / "host", name="host_ed25519")
    _, client_priv, client_pub = _generate_ed25519_keypair(tmp_path / "client", name="client_ed25519")
    repository = SqliteExecutionControlRepository(str(tmp_path / "execution.db"))
    target = _make_enrolled_target(
        target_id="c63_host",
        host="198.51.100.94",
        port=22,
        username="deploy",
        platform=SSHTargetPlatform.LINUX,
        host_pub=host_pub,
        credential_key="ops_key",
        credential_pub=client_pub,
    )
    repository.put_ssh_target(target)
    bounds = SSHDelegationBoundsV1(allow_workspace_transfer=True)

    # Fake ssh that never reads stdin (simulating a stalled / slow remote receiver).
    pid_record = tmp_path / "stalled_ssh.pid"
    stalled_ssh = tmp_path / "bin" / "fake-ssh-stalled-stdin"
    stalled_ssh.parent.mkdir(parents=True, exist_ok=True)
    stalled_ssh.write_text(
        "#!/usr/bin/env python3\n"
        "import os, pathlib, time\n"
        f"pathlib.Path({str(pid_record)!r}).write_text(str(os.getpid()), encoding='utf-8')\n"
        "time.sleep(60)\n",
        encoding="utf-8",
    )
    stalled_ssh.chmod(0o755)
    # 2 MB incompressible-ish payload (exceeds OS pipe buffer so stdin.drain() blocks).
    payload_bytes = os.urandom(2 * 1024 * 1024)

    broker = _catalog_broker(
        tmp_path,
        repository,
        secrets_map={
            ("ops_key", "ssh_private_key"): client_priv,
            ("ops_key", "ssh_public_key"): client_pub,
        },
        ssh_executable=stalled_ssh,
    )
    try:
        # (a) Timeout ends stalled upload promptly with TimeoutError (never "exit None"), no live ssh left.
        sess_to = _create_session_and_delegation(repository, target, bounds, session_id="ssh_sess_c63_to")
        t0 = time.monotonic()
        with pytest.raises(TimeoutError) as exc_info:
            await broker.upload_bundle(
                session_id=sess_to.session_id,
                entries=[("big.bin", payload_bytes)],
                remote_destination="/tmp/dest",
                caller=_LEAD_CALLER,
                timeout_seconds=1.0,
            )
        assert time.monotonic() - t0 < 4.5
        assert "no remote signal watcher was active" in str(exc_info.value)
        assert all(c.status is not SSHCommandStatus.RUNNING for c in broker._commands.values())
        stalled_pid = int(pid_record.read_text(encoding="utf-8").strip())
        await asyncio.sleep(0.1)
        with pytest.raises(ProcessLookupError):
            os.kill(stalled_pid, 0)

        # (b) revoke_session stops a stalled upload promptly instead of waiting for drain().
        sess_rev = _create_session_and_delegation(repository, target, bounds, session_id="ssh_sess_c63_rev")
        upload_task = asyncio.create_task(
            broker.upload_bundle(
                session_id=sess_rev.session_id,
                entries=[("big.bin", payload_bytes)],
                remote_destination="/tmp/dest",
                caller=_LEAD_CALLER,
                timeout_seconds=30.0,
            )
        )
        await asyncio.sleep(0.25)
        t0 = time.monotonic()
        await broker.revoke_session(session_id=sess_rev.session_id, revoked_by="admin_c63")
        assert time.monotonic() - t0 < 3.5
        with pytest.raises(ExecutionAuthorizationDenied):
            await upload_task

        # (c) shutdown() returns promptly while a stalled upload is in progress.
        sess_sd = _create_session_and_delegation(repository, target, bounds, session_id="ssh_sess_c63_sd")
        sd_task = asyncio.create_task(
            broker.upload_bundle(
                session_id=sess_sd.session_id,
                entries=[("big.bin", payload_bytes)],
                remote_destination="/tmp/dest",
                caller=_LEAD_CALLER,
                timeout_seconds=30.0,
            )
        )
        await asyncio.sleep(0.25)
        t0 = time.monotonic()
        await broker.shutdown()
        assert time.monotonic() - t0 < 3.5
        with pytest.raises(ExecutionAuthorizationDenied):
            await sd_task
    finally:
        await broker.shutdown()


@pytest.mark.asyncio
async def test_c64_monitor_isolates_errors_logs_once_and_fails_closed(
    tmp_path: Path,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """C64: One broken record never blocks other sessions; repeated monitor errors fail closed and log once."""

    _, _, host_pub = _generate_ed25519_keypair(tmp_path / "host", name="host_ed25519")
    _, client_priv, client_pub = _generate_ed25519_keypair(tmp_path / "client", name="client_ed25519")
    repository = SqliteExecutionControlRepository(str(tmp_path / "execution.db"))
    target = _make_enrolled_target(
        target_id="c64_host",
        host="198.51.100.95",
        port=22,
        username="deploy",
        platform=SSHTargetPlatform.LINUX,
        host_pub=host_pub,
        credential_key="ops_key",
        credential_pub=client_pub,
    )
    repository.put_ssh_target(target)
    bounds = SSHDelegationBoundsV1(allow_commands=True, allow_typing=True)
    sess_bad = _create_session_and_delegation(repository, target, bounds, session_id="ssh_sess_c64_bad")
    sess_good = _create_session_and_delegation(repository, target, bounds, session_id="ssh_sess_c64_good")
    sess_pty = _create_session_and_delegation(repository, target, bounds, session_id="ssh_sess_c64_pty")

    broker = _catalog_broker(
        tmp_path,
        repository,
        secrets_map={
            ("ops_key", "ssh_private_key"): client_priv,
            ("ops_key", "ssh_public_key"): client_pub,
        },
        ssh_executable=_write_fake_ssh_exec(tmp_path / "bin", mode="exec", state_dir=tmp_path / "home"),
        revocation_poll_interval_seconds=0.05,
    )
    try:
        cmd_bad = await broker.run_command(
            session_id=sess_bad.session_id,
            command="sleep 60",
            caller=_LEAD_CALLER,
            wait_seconds=0.1,
        )
        cmd_good = await broker.run_command(
            session_id=sess_good.session_id,
            command="sleep 60",
            caller=_LEAD_CALLER,
            wait_seconds=0.1,
        )
        await broker.open(
            session_id=sess_pty.session_id,
            lease_token="lease-c64-pty",
            caller=_LEAD_CALLER,
        )
        assert cmd_bad.status == SSHCommandStatus.RUNNING
        assert cmd_good.status == SSHCommandStatus.RUNNING
        assert sess_pty.session_id in broker._sessions

        orig_get_interaction = repository.get_ssh_interaction

        def _failing_interaction(*, team_id: str, session_id: str) -> Any:
            if session_id in {sess_bad.session_id, sess_pty.session_id}:
                raise RuntimeError(f"corrupt interaction row for {session_id}")
            return orig_get_interaction(team_id=team_id, session_id=session_id)

        caplog.set_level(logging.ERROR)
        repository.get_ssh_interaction = _failing_interaction  # type: ignore[method-assign]
        # Revoke sess_good directly in storage (simulating gateway Revoke from another process).
        repository.revoke_interactive_session(
            team_id="team_alpha",
            session_id=sess_good.session_id,
            revoked_by="gateway_admin",
        )

        # sess_good must still be revoked promptly by the monitor despite sess_bad's failing read.
        live_good = broker._commands[cmd_good.command_id]
        live_bad = broker._commands[cmd_bad.command_id]
        await asyncio.wait_for(live_good.done_event.wait(), timeout=3.0)
        good_after = await broker.get_command(
            session_id=sess_good.session_id,
            command_id=cmd_good.command_id,
            caller=_LEAD_CALLER,
        )
        assert good_after.status == SSHCommandStatus.REVOKED

        # sess_bad must fail closed after 3 consecutive monitor errors with a clear reason.
        await asyncio.wait_for(live_bad.done_event.wait(), timeout=3.0)
        bad_after = await broker.get_command(
            session_id=sess_bad.session_id,
            command_id=cmd_bad.command_id,
            caller=_LEAD_CALLER,
        )
        assert bad_after.status == SSHCommandStatus.REVOKED
        assert bad_after.error and "authorization checks could not run" in bad_after.error

        # Live PTY session whose monitor check keeps failing must also close fail-closed.
        deadline = time.monotonic() + 2.5
        while time.monotonic() < deadline and sess_pty.session_id in broker._sessions:
            await asyncio.sleep(0.05)
        assert sess_pty.session_id not in broker._sessions

        # Log-once per fault: exactly one check_failed error for sess_bad's command, not once per tick.
        bad_check_logs = [
            rec.getMessage()
            for rec in caplog.records
            if "event=ssh_monitor_check_failed" in rec.getMessage()
            and f"session_id={sess_bad.session_id}" in rec.getMessage()
        ]
        assert len(bad_check_logs) == 1
    finally:
        await broker.shutdown()


@pytest.mark.asyncio
async def test_c65_shutdown_refusal_non_linux_pid_comm_and_unknown_command_guard(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """C65(b,d,e): Shutdown refuses new work; non-Linux PID sweeps check comm; get_command delegates to guard."""

    from runtime_hosting import ssh_credentials

    _, _, host_pub = _generate_ed25519_keypair(tmp_path / "host", name="host_ed25519")
    _, client_priv, client_pub = _generate_ed25519_keypair(tmp_path / "client", name="client_ed25519")
    repository = SqliteExecutionControlRepository(str(tmp_path / "execution.db"))
    target = _make_enrolled_target(
        target_id="c65_host",
        host="198.51.100.96",
        port=22,
        username="deploy",
        platform=SSHTargetPlatform.LINUX,
        host_pub=host_pub,
        credential_key="ops_key",
        credential_pub=client_pub,
    )
    repository.put_ssh_target(target)
    bounds = SSHDelegationBoundsV1(allow_commands=True, allow_workspace_transfer=True)
    session = _create_session_and_delegation(repository, target, bounds, session_id="ssh_sess_c65")
    broker = _catalog_broker(
        tmp_path,
        repository,
        secrets_map={
            ("ops_key", "ssh_private_key"): client_priv,
            ("ops_key", "ssh_public_key"): client_pub,
        },
        ssh_executable=_write_fake_ssh_exec(tmp_path / "bin", mode="exec", state_dir=tmp_path / "home"),
    )

    # C65(e): Unknown command_id checks full guard authorization (refuses unauthorized agent type too).
    other_agent = SSHCallerContext(person_id="person_admin", agent_type="UnrelatedWorker")
    with pytest.raises(ExecutionAuthorizationDenied):
        await broker.get_command(
            session_id=session.session_id,
            command_id="ssh_cmd_unknown_c65",
            caller=other_agent,
        )
    with pytest.raises(LookupError, match="outcome is unknown"):
        await broker.get_command(
            session_id=session.session_id,
            command_id="ssh_cmd_unknown_c65",
            caller=_LEAD_CALLER,
        )

    # C65(b): After shutdown(), new operations are refused and no monitor restarts.
    await broker.shutdown()
    with pytest.raises(SSHSessionUnavailable, match="shutting down"):
        await broker.run_command(session_id=session.session_id, command="echo hi", caller=_LEAD_CALLER)
    with pytest.raises(SSHSessionUnavailable, match="shutting down"):
        await broker.open(session_id=session.session_id, lease_token="lease-after-sd", caller=_LEAD_CALLER)
    with pytest.raises(SSHSessionUnavailable, match="shutting down"):
        await broker.upload_bundle(
            session_id=session.session_id,
            entries=[("a.txt", b"x")],
            remote_destination="/tmp/x",
            caller=_LEAD_CALLER,
        )
    assert broker._monitor_task is None

    # C65(d): On non-Linux hosts, PID sweeps verify process comm before signalling.
    monkeypatch.setattr(ssh_credentials.sys, "platform", "darwin")
    monkeypatch.setattr(ssh_credentials, "_read_proc_start_time", lambda _pid: None)
    fake_comms = {42424: "ssh", 42425: "postgres", 42426: "ssh-agent"}
    monkeypatch.setattr(ssh_credentials, "_read_proc_comm", lambda pid: fake_comms.get(pid))

    pid_dir = tmp_path / "non_linux_pid"
    pid_dir.mkdir()
    ssh_credentials.record_ssh_child_pid(pid_dir, 42424)
    parsed = ssh_credentials._parse_ssh_pid_file(pid_dir / "ssh.pid")
    assert parsed == (42424, None, "ssh")
    assert ssh_credentials._pid_matches_recorded_ssh(42424, expected_start_time=None, expected_comm="ssh")
    assert not ssh_credentials._pid_matches_recorded_ssh(42425, expected_start_time=None, expected_comm="ssh")
    assert not ssh_credentials._pid_matches_recorded_ssh(42425, expected_start_time=None, expected_comm=None)
    assert ssh_credentials._pid_is_ssh_agent(42426)
    assert not ssh_credentials._pid_is_ssh_agent(42425)


# -- 10. C66-C68 hardening: bash 5.1+ as sh, helper output drain, multiline secrets --


def _bash_5_1_or_newer() -> Path | None:
    bash_path = shutil.which("bash")
    if not bash_path:
        return None
    proc = subprocess.run(
        [bash_path, "-c", 'printf "%s.%s" "${BASH_VERSINFO[0]}" "${BASH_VERSINFO[1]}"'],
        capture_output=True,
        text=True,
        check=False,
    )
    if proc.returncode != 0:
        return None
    parts = proc.stdout.strip().split(".")
    if len(parts) != 2 or not all(p.isdigit() for p in parts):
        return None
    major, minor = int(parts[0]), int(parts[1])
    return Path(bash_path) if (major, minor) >= (5, 1) else None


@pytest.mark.skipif(
    _bash_5_1_or_newer() is None,
    reason="bash 5.1 or newer is required to test implicit-exec sh behaviour (C66).",
)
@pytest.mark.asyncio
async def test_c66_password_sudo_and_recipe_succeed_when_sh_is_bash_5_1_plus(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """C66: Password sudo (free command and secret recipe) keeps sudo's TS_PPID ticket when sh is bash >= 5.1."""

    bash_bin = _bash_5_1_or_newer()
    assert bash_bin is not None
    bash_sh_dir = tmp_path / "bash_sh_bin"
    bash_sh_dir.mkdir()
    (bash_sh_dir / "sh").symlink_to(bash_bin)
    monkeypatch.setenv("PATH", f"{bash_sh_dir}{os.pathsep}{os.environ.get('PATH', '')}")

    # Prove `sh` on PATH actually executes bash >= 5.1 in this test.
    probe = subprocess.run(
        ["sh", "-c", 'printf "%s.%s" "${BASH_VERSINFO[0]}" "${BASH_VERSINFO[1]}"'],
        capture_output=True,
        text=True,
        check=True,
    )
    major_s, minor_s = probe.stdout.strip().split(".", 1)
    assert (int(major_s), int(minor_s)) >= (5, 1)

    _, _, host_pub = _generate_ed25519_keypair(tmp_path / "host", name="host_ed25519")
    _, client_priv, client_pub = _generate_ed25519_keypair(tmp_path / "client", name="client_ed25519")
    repository = SqliteExecutionControlRepository(str(tmp_path / "execution.db"))
    target = _make_enrolled_target(
        target_id="c66_bash_host",
        host="198.51.100.97",
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
    session = _create_session_and_delegation(repository, target, bounds, session_id="ssh_sess_c66")

    admin_pw = "BashShSudoPass!66"
    ts_key = "tskey-auth-c66-bash-sh-secret"
    fake_sudo = _write_fake_sudo(tmp_path / "sudo_c66", behavior="password", expected_password=admin_pw)
    ts_record = tmp_path / "ts_c66.json"
    fake_ts = _write_fake_tailscale(tmp_path / "ts_c66_bin", ts_record)
    broker = _catalog_broker(
        tmp_path,
        repository,
        secrets_map={
            ("ops_key", "ssh_private_key"): client_priv,
            ("ops_key", "ssh_public_key"): client_pub,
            ("ops_boot", "admin_password"): admin_pw,
            ("ops_boot", "tailscale_auth_key"): ts_key,
        },
        ssh_executable=_write_fake_ssh_exec(tmp_path / "bin", mode="exec", state_dir=tmp_path / "home"),
        sudo_search_paths=(str(fake_sudo),),
        tailscale_search_paths=(str(fake_ts),),
    )
    try:
        # Free password sudo command preserves exact exit code and does NOT fail with 'a password is required'.
        free_res = await broker.run_sudo(
            session_id=session.session_id,
            command="printf 'ran-as-root\\n'; exit 5",
            caller=_LEAD_CALLER,
        )
        assert free_res.status == SSHCommandStatus.FAILED
        assert free_res.exit_code == 5
        assert free_res.stdout == "ran-as-root\n"
        assert "password is required" not in free_res.stderr

        # Free password sudo command with empty stdin completes cleanly.
        ok_res = await broker.run_sudo(
            session_id=session.session_id,
            command="printf 'bash_sh_ok:'; rev",
            caller=_LEAD_CALLER,
        )
        assert ok_res.status == SSHCommandStatus.COMPLETED
        assert ok_res.stdout == "bash_sh_ok:"

        # Secret recipe under bash-as-sh succeeds and wipes ephemeral keyfile.
        ts_res = await broker.run_sudo(
            session_id=session.session_id,
            secret_recipe=SSHSecretRecipe.TAILSCALE_UP,
            recipe_params={"hostname": "c66-rhel9"},
            caller=_LEAD_CALLER,
        )
        assert ts_res.status == SSHCommandStatus.COMPLETED, ts_res
        recorded = json.loads(ts_record.read_text(encoding="utf-8"))
        assert recorded["key_value"] == ts_key
        assert not Path(recorded["key_path"]).exists()
    finally:
        await broker.shutdown()


@pytest.mark.asyncio
async def test_c68_multiline_secrets_refused_and_watcher_waits_for_true_eof(
    tmp_path: Path,
) -> None:
    """C68(b): Line breaks in stored secrets are refused by field name, and watcher ignores stray lines before EOF."""

    from runtime_hosting.ssh_operations import _wrap_posix_watched_command

    _, _, host_pub = _generate_ed25519_keypair(tmp_path / "host", name="host_ed25519")
    _, client_priv, client_pub = _generate_ed25519_keypair(tmp_path / "client", name="client_ed25519")
    repository = SqliteExecutionControlRepository(str(tmp_path / "execution.db"))
    target = _make_enrolled_target(
        target_id="c68_host",
        host="198.51.100.98",
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
        allow_password_bootstrap=True,
        allowed_secret_recipes=(SSHSecretRecipe.TAILSCALE_UP,),
    )
    session = _create_session_and_delegation(repository, target, bounds, session_id="ssh_sess_c68")

    fake_sudo = _write_fake_sudo(tmp_path / "sudo_c68", behavior="password", expected_password="CleanPassword!68")
    fake_ts = _write_fake_tailscale(tmp_path / "ts_c68_bin", tmp_path / "ts_c68.json")
    fake_ssh = _write_fake_ssh_exec(tmp_path / "bin", mode="exec", state_dir=tmp_path / "home")

    # (a) Trailing newline in admin_password is refused cleanly with field name (no secret leaked).
    bad_pw = "TrailingNewlineSecret!68\n"
    broker_bad_pw = _catalog_broker(
        tmp_path,
        repository,
        secrets_map={
            ("ops_key", "ssh_private_key"): client_priv,
            ("ops_key", "ssh_public_key"): client_pub,
            ("ops_boot", "admin_password"): bad_pw,
            ("ops_boot", "tailscale_auth_key"): "tskey-auth-clean-68",
        },
        ssh_executable=fake_ssh,
        allow_password_bootstrap=True,
        sudo_search_paths=(str(fake_sudo),),
        tailscale_search_paths=(str(fake_ts),),
    )
    try:
        with pytest.raises(ValueError) as exc_sudo_pw:
            await broker_bad_pw.run_sudo(
                session_id=session.session_id,
                command="echo should-not-run",
                caller=_LEAD_CALLER,
            )
        assert "admin_password" in str(exc_sudo_pw.value)
        assert "TrailingNewlineSecret" not in str(exc_sudo_pw.value)

        with pytest.raises(ValueError) as exc_boot_pw:
            await broker_bad_pw.enroll_ssh_key(
                session_id=session.session_id,
                caller=_LEAD_CALLER,
            )
        assert "admin_password" in str(exc_boot_pw.value)
        assert "TrailingNewlineSecret" not in str(exc_boot_pw.value)
    finally:
        await broker_bad_pw.shutdown()

    # (b) Trailing newline in tailscale_auth_key is refused cleanly with field name (no secret leaked).
    bad_ts = "tskey-auth-trailing-68\r\n"
    broker_bad_ts = _catalog_broker(
        tmp_path,
        repository,
        secrets_map={
            ("ops_key", "ssh_private_key"): client_priv,
            ("ops_key", "ssh_public_key"): client_pub,
            ("ops_boot", "admin_password"): "CleanPassword!68",
            ("ops_boot", "tailscale_auth_key"): bad_ts,
        },
        ssh_executable=fake_ssh,
        sudo_search_paths=(str(fake_sudo),),
        tailscale_search_paths=(str(fake_ts),),
    )
    try:
        with pytest.raises(ValueError) as exc_ts:
            await broker_bad_ts.run_sudo(
                session_id=session.session_id,
                secret_recipe=SSHSecretRecipe.TAILSCALE_UP,
                caller=_LEAD_CALLER,
            )
        assert "tailscale_auth_key" in str(exc_ts.value)
        assert "tskey-auth-trailing-68" not in str(exc_ts.value)
    finally:
        await broker_bad_ts.shutdown()

    # (c) Defense in depth: the POSIX watcher waits for true EOF, not just a single stray stdin line.
    wrapped = _wrap_posix_watched_command("sh -c 'echo first; sleep 0.5; echo second'")
    proc = subprocess.Popen(
        ["sh", "-c", wrapped],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        start_new_session=True,
    )
    try:
        assert proc.stdin is not None and proc.stdout is not None and proc.stderr is not None
        proc.stdin.write("\nstray-line\n")
        proc.stdin.flush()
        # Keep stdin open while the command finishes naturally (communicate() would close stdin immediately).
        rc = proc.wait(timeout=5.0)
        out = proc.stdout.read()
        err = proc.stderr.read()
        assert rc == 0, err
        assert out == "first\nsecond\n"
    finally:
        if proc.stdin is not None:
            proc.stdin.close()
        if proc.poll() is None:
            proc.kill()
            proc.wait(timeout=2.0)


@pytest.mark.skipif(
    _sshd_binary() is None or shutil.which("ssh") is None or shutil.which("bash") is None,
    reason="Real OpenSSH sshd/ssh and bash binaries are required for the C67 output-helper drain test.",
)
@pytest.mark.asyncio
async def test_c67_real_sshd_output_helper_pipeline_flushes_on_normal_exit(
    tmp_path: Path,
) -> None:
    """C67 & C70(b): Real sshd with a bash process-substitution output helper preserves trailing output and log lines."""

    sshd_bin = _sshd_binary()
    bash_bin = shutil.which("bash")
    assert sshd_bin is not None and bash_bin is not None
    sshd_dir = tmp_path / "sshd_c67"
    sshd_dir.mkdir(mode=0o700)
    host_key_path, _, host_pub = _generate_ed25519_keypair(sshd_dir, name="ssh_host_ed25519_key")
    host_key_path.chmod(0o600)
    _, client_priv, client_pub = _generate_ed25519_keypair(tmp_path / "client_c67", name="id_ed25519")
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
        f"PidFile {sshd_dir / 'sshd.pid'}\n"
        "PubkeyAuthentication yes\n"
        "PasswordAuthentication no\n"
        "KbdInteractiveAuthentication no\n"
        "ChallengeResponseAuthentication no\n"
        "UsePAM no\n"
        "StrictModes no\n"
        "PermitRootLogin yes\n"
        "LogLevel ERROR\n",
        encoding="utf-8",
    )
    sshd_proc = subprocess.Popen(
        [str(sshd_bin), "-D", "-e", "-f", str(sshd_config)],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    try:
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
            pytest.skip("Local unprivileged sshd could not bind/start.")

        repository = SqliteExecutionControlRepository(str(tmp_path / "execution.db"))
        target = _make_enrolled_target(
            target_id="c67_helper_host",
            host="127.0.0.1",
            port=port,
            username=current_user,
            platform=SSHTargetPlatform.LINUX,
            host_pub=host_pub,
            credential_key="real_key",
            credential_pub=client_pub,
        )
        repository.put_ssh_target(target)
        bounds = SSHDelegationBoundsV1(allow_commands=True)
        session = _create_session_and_delegation(repository, target, bounds, session_id="ssh_sess_c67")
        loopback_policy = SSHNetworkPolicy(
            allowed_cidrs=(ipaddress.ip_network("127.0.0.1/32"),),
            allowed_domains=("localtest.example",),
            denied_cidrs=(),
        )
        broker = _catalog_broker(
            tmp_path,
            repository,
            secrets_map={
                ("real_key", "ssh_private_key"): client_priv,
                ("real_key", "ssh_public_key"): client_pub,
            },
            ssh_executable=shutil.which("ssh") or "ssh",
            network_policy=loopback_policy,
        )
        try:
            for run_idx in range(3):
                helper_log = tmp_path / f"helper_{run_idx}.log"
                helper_script = (
                    f'exec > >(while IFS= read -r l; do printf "%s %s\\n" "$(date +%T)" "$l"; done '
                    f'| tee -a "{helper_log}") 2>&1; '
                    'for i in 1 2 3 4 5 6 7 8; do echo "step $i"; done; '
                    'echo "FATAL: disk full, aborting"; exit 3'
                )
                helper_res = await broker.run_command(
                    session_id=session.session_id,
                    command=f"{bash_bin} -c {shlex.quote(helper_script)}",
                    caller=_LEAD_CALLER,
                    timeout_seconds=10.0,
                    wait_seconds=5.0,
                )
                assert helper_res.status == SSHCommandStatus.FAILED, helper_res
                assert helper_res.exit_code == 3
                assert "FATAL: disk full, aborting" in helper_res.stdout, helper_res
                log_text = helper_log.read_text(encoding="utf-8")
                assert "FATAL: disk full, aborting" in log_text, log_text
                for step_num in range(1, 9):
                    assert f"step {step_num}" in helper_res.stdout
                    assert f"step {step_num}" in log_text
        finally:
            await broker.shutdown()
    finally:
        sshd_proc.terminate()
        try:
            sshd_proc.wait(timeout=3.0)
        except subprocess.TimeoutExpired:
            sshd_proc.kill()
            sshd_proc.wait(timeout=2.0)


def test_f3_windows_bundle_extract_rejects_unicode_single_quotes_and_uses_base64() -> None:
    """F3: Windows workspace bundle extraction rejects Unicode single quotes and decodes via base64."""

    import base64
    from runtime_hosting.ssh_operations import (
        build_bundle_extract_command,
        validate_remote_destination,
    )

    for bad_quote in ("\u2018", "\u2019", "\u201a", "\u201b"):
        with pytest.raises(ValueError, match="Unicode single-quote"):
            validate_remote_destination(f"C:\\work\\a{bad_quote}; calc.exe; {bad_quote}b")
    with pytest.raises(ValueError, match="control characters"):
        validate_remote_destination("C:\\work\\a\x85b")

    for dest in ("C:\\Deploy\\O'Brien\\app", "~/O'Brien/deploy"):
        argv = build_bundle_extract_command(dest, SSHTargetPlatform.WINDOWS)
        assert len(argv) == 1
        tokens = argv[0].split()
        assert tokens[0] == "powershell"
        assert "-EncodedCommand" in tokens
        encoded_cmd = tokens[tokens.index("-EncodedCommand") + 1]
        decoded_ps = base64.b64decode(encoded_cmd).decode("utf-16le")
        assert "[Text.Encoding]::UTF8.GetString([Convert]::FromBase64String(" in decoded_ps
        assert "O'Brien" not in decoded_ps
        expected_b64 = base64.b64encode(
            (dest[2:] if dest.startswith("~/") else dest).encode("utf-8")
        ).decode("ascii")
        assert expected_b64 in decoded_ps


@pytest.mark.asyncio
async def test_f9_enroll_ssh_key_passes_python_executable_under_minimal_path(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """F9: _execute_key_bootstrap passes catalog_provider.python_executable so SSH_ASKPASS works without python3 on PATH."""

    import sys

    _, _, host_pub = _generate_ed25519_keypair(tmp_path / "host", name="host_ed25519")
    _, client_priv, client_pub = _generate_ed25519_keypair(tmp_path / "client", name="client_ed25519")
    repository = SqliteExecutionControlRepository(str(tmp_path / "execution.db"))
    target = _make_enrolled_target(
        target_id="f9_minimal_path_host",
        host="198.51.100.99",
        port=22,
        username="ops",
        platform=SSHTargetPlatform.LINUX,
        host_pub=host_pub,
        credential_key="ops_key",
        credential_pub=client_pub,
        bootstrap_credential_key="ops_boot",
    )
    repository.put_ssh_target(target)
    bounds = SSHDelegationBoundsV1(allow_password_bootstrap=True)
    session = _create_session_and_delegation(repository, target, bounds, session_id="ssh_sess_f9")

    # Put a custom python3-free fake ssh script in a minimal PATH directory that executes
    # SSH_ASKPASS directly using the inherited PATH (so env python3 fails if python_executable
    # was not added to PATH).
    min_bin = tmp_path / "min_bin"
    min_bin.mkdir(parents=True, exist_ok=True)
    for required_tool in ("env", "ssh-agent", "ssh-add"):
        resolved_tool = shutil.which(required_tool)
        assert resolved_tool is not None, f"Missing {required_tool} in test environment"
        os.symlink(resolved_tool, min_bin / required_tool)
    fake_ssh = min_bin / "fake-ssh"
    askpass_out_file = tmp_path / "askpass_out.txt"
    fake_ssh.write_text(
        f"#!{sys.executable}\n"
        "import os, pathlib, subprocess, sys\n"
        "args = sys.argv[1:]\n"
        "if 'PasswordAuthentication=yes' in args:\n"
        "    askpass = os.environ['SSH_ASKPASS']\n"
        "    # Run SSH_ASKPASS via its shebang (#!/usr/bin/env python3) with the subprocess PATH.\n"
        "    res = subprocess.run([askpass], capture_output=True, text=True, env=dict(os.environ))\n"
        "    if res.returncode != 0:\n"
        "        sys.stderr.write(res.stderr)\n"
        "        sys.exit(res.returncode)\n"
        f"    pathlib.Path({str(askpass_out_file)!r}).write_text(res.stdout.strip(), encoding='utf-8')\n"
        "    _ = sys.stdin.read()\n"
        "    sys.exit(0)\n"
        "print('UA_KEY_AUTH_OK')\n"
        "sys.exit(0)\n",
        encoding="utf-8",
    )
    fake_ssh.chmod(0o755)

    # Create a dedicated bin dir containing ONLY a python3 symlink so catalog_provider.python_executable
    # is distinct from min_bin and ambient PATH lacks python3 completely.
    py_bin = tmp_path / "custom_py_bin"
    py_bin.mkdir(parents=True, exist_ok=True)
    custom_py = py_bin / "python3"
    os.symlink(sys.executable, custom_py)

    monkeypatch.setenv("PATH", str(min_bin))

    broker = _catalog_broker(
        tmp_path,
        repository,
        secrets_map={
            ("ops_key", "ssh_private_key"): client_priv,
            ("ops_key", "ssh_public_key"): client_pub,
            ("ops_boot", "admin_password"): "MinimalPathBootPass!99",
        },
        ssh_executable=str(fake_ssh),
        allow_password_bootstrap=True,
    )
    catalog_provider = broker._catalog_provider()
    catalog_provider.python_executable = str(custom_py)
    try:
        result = await broker.enroll_ssh_key(
            session_id=session.session_id,
            caller=_LEAD_CALLER,
            timeout_seconds=15.0,
        )
        assert result.verified_key_login is True
        assert askpass_out_file.read_text(encoding="utf-8") == "MinimalPathBootPass!99"
    finally:
        await broker.shutdown()
