"""Tests for Sealbox SSH credential templates, private ssh-agent material, sandbox isolation, and scrubbing."""

from __future__ import annotations

import asyncio
import base64
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
import getpass
import logging
import os
from pathlib import Path
import shutil
import signal
import socket
import stat
import subprocess
import sys
import time
from typing import Any, Iterator

import pytest

from custom_tools.code_sandbox import (
    code_sandbox_status,
    prepare_sandboxed_command,
    release_sandboxed_command,
)
from runtime_hosting.artifacts import FilesystemArtifactStore
from runtime_hosting.authorization import ExecutionAccessGuard
from runtime_hosting.execution_contracts import (
    ExecutionActivationKind,
    InteractiveSessionStatus,
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
    _ASKPASS_HELPER_PATH,
    _read_proc_start_time,
    build_ssh_subprocess_env,
    is_short_secret,
)
from runtime_hosting.ssh_network import ValidatedSSHHost, ssh_host_key_alias
from runtime_settings.instance_layout import InstanceLayout
from secure_tool_broker import credential_catalog, credential_sources
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
from tests.execution_access_support import fake_ssh, host_key_fingerprint, team_owner, write_ssh_material


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

    # C25: Token patterns (GitHub, OpenAI, JWT) split across chunk boundaries are redacted.
    gh_tok = "ghp_1234567890abcdef1234567890abcdef"
    oai_tok = "sk-proj-abcdefghijklmnopqrstuvwxyz123456"
    jwt_tok = (
        "eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9aaaa."
        "eyJzdWIiOiIxMjM0NTY3ODkwIn0bbbb."
        "SflKxwRJSMeKKF2QT4fwpMeJf36POk6yJV_adQssw5c"
    )
    for token, split_at in ((gh_tok, 14), (oai_tok, 10), (jwt_tok, 45)):
        pattern_stream = scrubber.new_stream_scrubber()
        p1 = pattern_stream.feed(f"token {token[:split_at]}".encode("utf-8"))
        p2 = pattern_stream.feed(f"{token[split_at:]}\n".encode("utf-8"))
        p3 = pattern_stream.flush()
        assert token[:split_at].encode("utf-8") not in p1
        assert token[split_at:].encode("utf-8") not in p2
        assert b"".join((p1, p2, p3)).decode("utf-8") == "token [REDACTED]\n"

    # Held prefix bytes (e.g. partial match of a registered secret) are released at flush.
    held_stream = scrubber.new_stream_scrubber()
    assert held_stream.feed(b"login as Super") == b"login as "
    assert held_stream.flush() == b"Super"

    # Ordinary interactive output ending in prompt punctuation/whitespace is emitted immediately.
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
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    sshd_bin = shutil.which("sshd")
    ssh_bin = shutil.which("ssh")
    keygen_bin = shutil.which("ssh-keygen")
    agent_bin = shutil.which("ssh-agent")
    add_bin = shutil.which("ssh-add")
    if not (sshd_bin and ssh_bin and keygen_bin and agent_bin and add_bin):
        pytest.skip("OpenSSH binaries are not installed in this environment.")

    # C24: Broker-only secrets in process environment must never leak into SSH subprocesses.
    monkeypatch.setenv("SEALBOX_TOKEN", "sbx-secret-broker-token-should-not-leak")
    monkeypatch.setenv("SECURE_TOOL_BROKER_TOKEN", "stb-secret-broker-token-should-not-leak")

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
    digest = target.calculate_digest()
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
    assert provider.default_key_lifetime_seconds == 60
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
            assert material.argv_identity_file() == "./id.pub"
            assert material.argv_known_hosts_file() == "./known_hosts"
            assert private_key_pem in material.touched_secrets
            assert passphrase in material.touched_secrets
            # C24: repr() must never expose raw secrets.
            material_repr = repr(material)
            assert private_key_pem not in material_repr
            assert passphrase not in material_repr
            broker._verify_material(session, material)

            # No private key, passphrase, or executable scripts exist anywhere in the ephemeral dir.
            for child in session_dir.iterdir():
                if child.is_file():
                    raw_text = child.read_text(encoding="utf-8", errors="ignore")
                    assert "PRIVATE KEY" not in raw_text
                    assert passphrase not in raw_text
                    assert not (child.lstat().st_mode & 0o111)

            # ssh-agent cmdline never leaks full directory path or secrets.
            pid_tokens = (session_dir / "agent.pid").read_text(encoding="utf-8").strip().split()
            agent_pid = int(pid_tokens[0])
            if sys.platform == "linux":
                assert len(pid_tokens) == 2
                assert int(pid_tokens[1]) == _read_proc_start_time(agent_pid)
            cmdline_path = Path(f"/proc/{agent_pid}/cmdline")
            if cmdline_path.exists():
                cmdline = cmdline_path.read_bytes().decode("utf-8", errors="ignore")
                assert session_dir.name not in cmdline
                assert passphrase not in cmdline

            # C21 + C24: ssh argv uses relative ./id.pub and ./known_hosts (never session_dir)
            # and env is allowlisted (never carries broker tokens).
            validated = ValidatedSSHHost(
                host="bootstrap.internal",
                port=port,
                resolved_ip="::ffff:127.0.0.1",
                resolved_ips=("::ffff:127.0.0.1",),
                host_key_alias=ssh_host_key_alias("bootstrap.internal", port),
            )
            argv = list(broker._ssh_argv(session, material, validated_host=validated))
            assert "IdentityAgent=SSH_AUTH_SOCK" in argv
            assert "UserKnownHostsFile=./known_hosts" in argv
            assert "./id.pub" in argv
            assert all(session_dir.name not in arg for arg in argv)
            child_env = broker._ssh_env(material)
            assert "SEALBOX_TOKEN" not in child_env
            assert "SECURE_TOOL_BROKER_TOKEN" not in child_env

            # Real OpenSSH login via private ssh-agent with zero private key file on disk.
            login_argv = list(argv)
            login_argv[login_argv.index("-tt")] = "-T"
            login_argv.append("echo sealbox-agent-ok")
            login = subprocess.run(
                login_argv,
                cwd=str(material.working_dir),
                env=child_env,
                capture_output=True,
                text=True,
                timeout=10,
                check=False,
            )
            assert login.returncode == 0, f"ssh failed: out={login.stdout!r} err={login.stderr!r}"
            assert "sealbox-agent-ok" in login.stdout

            # C01 + C21: Keep a real broker-started ssh process alive AND trigger a
            # passphrase unlock while a no-hint Landlock sandbox scans /proc for
            # any trace of any ua-ssh-* folder path.
            status = code_sandbox_status()
            if status.available:
                live_ssh_argv = list(argv)
                live_ssh_argv[live_ssh_argv.index("-tt")] = "-T"
                live_ssh_argv.append("sleep 30")
                live_ssh = subprocess.Popen(
                    live_ssh_argv,
                    cwd=str(material.working_dir),
                    env=child_env,
                    stdin=subprocess.DEVNULL,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                )
                try:
                    time.sleep(0.25)
                    assert live_ssh.poll() is None
                    no_hint_probe_code = (
                        "import os, time\n"
                        "from pathlib import Path\n"
                        "needle = 'ua-' + 'ssh-'\n"
                        "needle_bytes = needle.encode('ascii')\n"
                        "my_pid = os.getpid()\n"
                        "leaks = []\n"
                        "deadline = time.monotonic() + 0.6\n"
                        "while time.monotonic() < deadline:\n"
                        "  for entry in os.listdir('/proc'):\n"
                        "    if not entry.isdigit() or int(entry) == my_pid:\n"
                        "      continue\n"
                        "    p = Path('/proc') / entry\n"
                        "    for fname in ('cmdline', 'environ', 'maps'):\n"
                        "      try:\n"
                        "        data = (p / fname).read_bytes()\n"
                        "        if needle_bytes in data:\n"
                        "          leaks.append(f'{entry}/{fname}:{data[:120]!r}')\n"
                        "      except OSError:\n"
                        "        pass\n"
                        "    for lname in ('cwd', 'exe', 'root'):\n"
                        "      try:\n"
                        "        target = os.readlink(p / lname)\n"
                        "        if needle in target:\n"
                        "          leaks.append(f'{entry}/{lname}:{target}')\n"
                        "      except OSError:\n"
                        "        pass\n"
                        "    try:\n"
                        "      for fd_name in os.listdir(p / 'fd'):\n"
                        "        try:\n"
                        "          target = os.readlink(p / 'fd' / fd_name)\n"
                        "          if needle in target:\n"
                        "            leaks.append(f'{entry}/fd/{fd_name}:{target}')\n"
                        "        except OSError:\n"
                        "          pass\n"
                        "    except OSError:\n"
                        "      pass\n"
                        "  try:\n"
                        "    unix_txt = Path('/proc/net/unix').read_text(errors='ignore')\n"
                        "    if needle in unix_txt:\n"
                        "      leaks.append('proc/net/unix')\n"
                        "  except OSError:\n"
                        "    pass\n"
                        "  time.sleep(0.02)\n"
                        "if leaks:\n"
                        "  raise SystemExit('LEAKED:' + ';'.join(leaks[:3]))\n"
                        "print('NO_HINT_ISOLATED')\n"
                    )
                    sandboxed = prepare_sandboxed_command(
                        ("python3", "-c", no_hint_probe_code),
                        task_roots=[str(ws_dir)],
                        env={**os.environ, "TEAM_INSTANCE_ROOT": str(layout.root)},
                        network=False,
                    )
                    try:
                        probe_proc = subprocess.Popen(
                            sandboxed.argv,
                            env=sandboxed.env,
                            stdout=subprocess.PIPE,
                            stderr=subprocess.PIPE,
                            text=True,
                        )
                        # Trigger a real passphrase unlock while the no-hint sandbox polls /proc.
                        with provider.prepare(session, scrubber=scrubber):
                            pass
                        out, err = probe_proc.communicate(timeout=10)
                    finally:
                        release_sandboxed_command(sandboxed)
                    assert probe_proc.returncode == 0, f"no-hint sandbox probe failed: {out!r} {err!r}"
                    assert "NO_HINT_ISOLATED" in out

                    # Also verify direct access to ephemeral dir / files / agent proc is denied by Landlock.
                    direct_probe_code = (
                        "import os\n"
                        f"for fn in (lambda: os.listdir({str(layout.ssh_ephemeral_dir)!r}), "
                        f"lambda: os.listdir({str(session_dir)!r}), "
                        f"lambda: open({str(material.resolved_known_hosts_file())!r}).read(), "
                        f"lambda: open('/proc/{agent_pid}/environ', 'rb').read(), "
                        f"lambda: os.readlink('/proc/{agent_pid}/cwd')):\n"
                        "  try:\n"
                        "    fn()\n"
                        "    raise SystemExit('DIRECT_LEAK')\n"
                        "  except PermissionError:\n"
                        "    pass\n"
                        "print('DIRECT_ISOLATED')\n"
                    )
                    direct_sandboxed = prepare_sandboxed_command(
                        ("python3", "-c", direct_probe_code),
                        task_roots=[str(ws_dir)],
                        env={**os.environ, "TEAM_INSTANCE_ROOT": str(layout.root)},
                        network=False,
                    )
                    try:
                        direct_res = subprocess.run(
                            direct_sandboxed.argv,
                            env=direct_sandboxed.env,
                            capture_output=True,
                            text=True,
                            timeout=10,
                            check=False,
                        )
                    finally:
                        release_sandboxed_command(direct_sandboxed)
                    assert direct_res.returncode == 0, (
                        f"direct sandbox probe failed: {direct_res.stdout!r} {direct_res.stderr!r}"
                    )
                    assert "DIRECT_ISOLATED" in direct_res.stdout
                finally:
                    live_ssh.terminate()
                    try:
                        live_ssh.wait(timeout=5)
                    except subprocess.TimeoutExpired:
                        live_ssh.kill()
                        live_ssh.wait(timeout=5)

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
    digest = target.calculate_digest()
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

    # C24 + C27: One-shot FIFO password askpass uses tracked helper outside session_dir,
    # writes zero password bytes to disk, hides password from repr(), and rejects a second read.
    with provider.prepare_password_askpass(
        owner=alice_owner,
        credential_key="alice_boot",
        scrubber=scrubber,
    ) as askpass:
        assert "alice-admin-password-789" not in repr(askpass)
        assert askpass.working_dir is not None
        helper = Path(askpass.env["SSH_ASKPASS"])
        assert helper == _ASKPASS_HELPER_PATH
        assert askpass.working_dir not in helper.parents
        assert askpass.env["UA_SSH_ASKPASS_FIFO"] == "./pw.fifo"
        fifo_path = askpass.working_dir / "pw.fifo"
        assert stat.S_ISFIFO(os.lstat(fifo_path).st_mode)
        assert [p.name for p in askpass.working_dir.iterdir()] == ["pw.fifo"]

        askpass_env = build_ssh_subprocess_env(extra=askpass.env, python_executable=sys.executable)
        first = subprocess.run(
            [str(helper)],
            cwd=str(askpass.working_dir),
            env=askpass_env,
            capture_output=True,
            text=True,
            timeout=5,
            check=True,
        )
        assert first.stdout.strip() == "alice-admin-password-789"
        assert not fifo_path.exists()

        second = subprocess.run(
            [str(helper)],
            cwd=str(askpass.working_dir),
            env=askpass_env,
            capture_output=True,
            text=True,
            timeout=5,
            check=False,
        )
        assert second.returncode != 0
        assert second.stdout == ""
    assert not askpass.working_dir.exists()

    # Exiting prepare_password_askpass without reading the FIFO cleans up immediately without hanging.
    t0 = time.monotonic()
    with provider.prepare_password_askpass(
        owner=alice_owner,
        credential_key="alice_boot",
        scrubber=scrubber,
    ) as unused_askpass:
        assert unused_askpass.working_dir is not None
        unused_dir = unused_askpass.working_dir
    assert time.monotonic() - t0 < 2.0
    assert not unused_dir.exists()


def test_collect_credential_optional_blank_secrets_and_default_resolver(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """C22: Both SSH templates save with optional secrets blank and resolve cleanly."""

    keygen_bin = shutil.which("ssh-keygen")
    agent_bin = shutil.which("ssh-agent")
    add_bin = shutil.which("ssh-add")
    if not (keygen_bin and agent_bin and add_bin):
        pytest.skip("OpenSSH binaries are not installed in this environment.")

    db_path = tmp_path / "catalog.db"
    monkeypatch.setenv("SECURE_TOOL_BROKER_CREDENTIAL_CATALOG_DB_PATH", str(db_path))
    monkeypatch.setenv("TEAM_INSTANCE_NAME", "test-inst")
    credential_catalog._SECRET_CACHE.clear()

    stored_sealbox: dict[tuple[str, str], str] = {}
    sealbox_write_calls: list[list[str]] = []

    def fake_tenant_env(*, scope_kind: str, owner_id: str, provision: bool = True) -> dict[str, str]:
        return {"TEST_TENANT": f"{scope_kind}:{owner_id}"}

    def fake_run_sealbox(
        args: list[str],
        *,
        input_text: str | None = None,
        env: dict[str, str] | None = None,
        timeout_seconds: int = 30,
    ) -> subprocess.CompletedProcess[str]:
        sealbox_write_calls.append(list(args))
        tenant = (env or {}).get("TEST_TENANT", "")
        if args[:2] == ["credential", "set"]:
            stored_sealbox[(tenant, args[2])] = input_text or ""
        elif args[:2] == ["secret", "set"]:
            stored_sealbox[(tenant, args[2])] = input_text or ""
        return subprocess.CompletedProcess(args=args, returncode=0, stdout='{"version":1}\n', stderr="")

    class _FakeSealboxSource:
        def get_secret(self, request: Any) -> str | None:
            tenant = f"{request.owner_scope_kind}:{request.owner_id}"
            item_key = request.ref.item_id or request.ref.item_name or ""
            return stored_sealbox.get((tenant, item_key))

    monkeypatch.setattr(credential_catalog, "sealbox_tenant_environment", fake_tenant_env)
    monkeypatch.setattr(credential_catalog, "run_sealbox", fake_run_sealbox)
    monkeypatch.setattr(
        credential_catalog,
        "get_credential_source",
        lambda name: _FakeSealboxSource() if name == "sealbox" else credential_sources.get_credential_source(name),
    )

    alice_scope = CredentialOwnerScope.user("usr_alice")
    alice_owner = OwnerScopeV1(scope_kind=OwnerScopeKind.USER, owner_id="usr_alice", team_id="team_alpha")

    # (a) ssh_host_bootstrap with only username and admin_password (optional secrets blank).
    boot_rendered = render_credential_template_request(
        {"template_id": "ssh_host_bootstrap", "service_name": "Lab Linux"}
    )
    assert boot_rendered.status == "success"
    boot_req = boot_rendered.content["request"]
    boot_req["write_source"] = "sealbox"

    # Missing required admin_password leaves zero writes in Sealbox.
    missing_req = credential_catalog.collect_credential(
        boot_req,
        {"username": "admin", "admin_password": "", "ssh_private_key": ""},
        scope=alice_scope,
    )
    assert missing_req.status == "error"
    assert missing_req.content["error_code"] == "missing_required_credential_field"
    assert sealbox_write_calls == []

    boot_saved = credential_catalog.collect_credential(
        boot_req,
        {
            "username": "admin",
            "admin_password": "bootstrap-admin-pw-1234",
            "ssh_public_key": "",
            "ssh_private_key": "",
            "ssh_passphrase": "",
            "tailscale_auth_key": "",
        },
        scope=alice_scope,
    )
    assert boot_saved.status == "success", boot_saved.error
    assert boot_saved.content["stored_secret_fields"] == ["admin_password"]
    saved_boot_cred = credential_catalog.load_catalog(scope=alice_scope).credentials[boot_req["credential_key"]]
    assert "ssh_private_key" not in saved_boot_cred.fields
    assert "ssh_passphrase" not in saved_boot_cred.fields
    assert "tailscale_auth_key" not in saved_boot_cred.fields

    layout = InstanceLayout.at(tmp_path / "inst")
    provider = CatalogSSHCredentialMaterialProvider(
        ephemeral_base_dir=layout.ssh_ephemeral_dir,
        known_hosts_line_provider=lambda _s: ("ssh-ed25519", base64.b64encode(b"hostkey").decode("ascii")),
        ssh_agent_executable=agent_bin,
        ssh_add_executable=add_bin,
    )
    assert (
        provider.resolve_admin_password(owner=alice_owner, credential_key=boot_req["credential_key"])
        == "bootstrap-admin-pw-1234"
    )

    # (b) ssh_private_key with an unencrypted key and blank optional ssh_passphrase.
    unenc_key = tmp_path / "unenc_ed25519"
    subprocess.run([keygen_bin, "-q", "-t", "ed25519", "-N", "", "-f", str(unenc_key)], check=True)
    unenc_pem = unenc_key.read_text(encoding="utf-8")
    unenc_pub = unenc_key.with_suffix(".pub").read_text(encoding="utf-8").strip()

    key_rendered = render_credential_template_request(
        {"template_id": "ssh_private_key", "service_name": "Unenc Box"}
    )
    assert key_rendered.status == "success"
    key_req = key_rendered.content["request"]
    key_req["write_source"] = "sealbox"
    key_saved = credential_catalog.collect_credential(
        key_req,
        {
            "ssh_public_key": unenc_pub,
            "ssh_private_key": unenc_pem,
            "ssh_passphrase": "",
        },
        scope=alice_scope,
    )
    assert key_saved.status == "success", key_saved.error
    assert key_saved.content["stored_secret_fields"] == ["ssh_private_key"]
    saved_key_cred = credential_catalog.load_catalog(scope=alice_scope).credentials[key_req["credential_key"]]
    assert "ssh_passphrase" not in saved_key_cred.fields

    target = SSHConnectionTargetV1(
        host="box.internal",
        port=22,
        username="deploy",
        host_key_algorithm="ssh-ed25519",
        host_key_fingerprint=host_key_fingerprint(b"hostkey"),
        credential_binding_id=f"catalog:{key_req['credential_key']}",
    )
    session = InteractiveSessionV1(
        session_id="ssh_sess_unenc_catalog",
        owner=alice_owner,
        target=target,
        target_digest=target.calculate_digest(),
    )
    with provider.prepare(session) as mat:
        assert mat.public_key == unenc_pub

    # (c) ssh_private_key with encrypted key and passphrase also saves and prepares cleanly.
    enc_key = tmp_path / "enc_ed25519"
    subprocess.run(
        [keygen_bin, "-q", "-t", "ed25519", "-N", "key-passphrase-456", "-f", str(enc_key)],
        check=True,
    )
    enc_pem = enc_key.read_text(encoding="utf-8")
    enc_pub = enc_key.with_suffix(".pub").read_text(encoding="utf-8").strip()
    enc_rendered = render_credential_template_request(
        {"template_id": "ssh_private_key", "service_name": "Enc Box"}
    )
    enc_req = enc_rendered.content["request"]
    enc_req["write_source"] = "sealbox"
    enc_saved = credential_catalog.collect_credential(
        enc_req,
        {
            "ssh_public_key": enc_pub,
            "ssh_private_key": enc_pem,
            "ssh_passphrase": "key-passphrase-456",
        },
        scope=alice_scope,
    )
    assert enc_saved.status == "success", enc_saved.error
    enc_session = session.model_copy(
        update={
            "session_id": "ssh_sess_enc_catalog",
            "target": target.model_copy(update={"credential_binding_id": f"catalog:{enc_req['credential_key']}"}),
        }
    )
    with provider.prepare(enc_session) as mat:
        assert mat.public_key == enc_pub


def test_ephemeral_agent_exits_after_broker_sigkill(tmp_path: Path) -> None:
    """C23: Private ssh-agent exits on its own even when its starting broker receives SIGKILL."""

    agent_bin = shutil.which("ssh-agent")
    if not agent_bin:
        pytest.skip("ssh-agent is not installed in this environment.")

    layout = InstanceLayout.at(tmp_path / "inst")
    session_dir = layout.ssh_ephemeral_dir / "ua-ssh-sigkill-check"
    session_dir.mkdir(parents=True, mode=0o700)

    standin_code = (
        "from pathlib import Path\n"
        "import time\n"
        "from runtime_hosting.ssh_credentials import CatalogSSHCredentialMaterialProvider\n"
        f"provider = CatalogSSHCredentialMaterialProvider(ephemeral_base_dir=Path({str(layout.ssh_ephemeral_dir)!r}), ssh_agent_executable={agent_bin!r})\n"
        f"handle = provider._start_ephemeral_agent(Path({str(session_dir)!r}))\n"
        "print(handle.agent_pid, flush=True)\n"
        "time.sleep(60)\n"
    )
    standin = subprocess.Popen(
        [sys.executable, "-c", standin_code],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    agent_pid: int | None = None
    try:
        assert standin.stdout is not None
        line = standin.stdout.readline().strip()
        assert line.isdigit(), f"stand-in failed: {standin.stderr.read() if standin.stderr else ''}"
        agent_pid = int(line)
        os.kill(agent_pid, 0)

        os.kill(standin.pid, signal.SIGKILL)
        standin.wait(timeout=5)

        deadline = time.monotonic() + 15.0
        while time.monotonic() < deadline:
            try:
                os.kill(agent_pid, 0)
            except ProcessLookupError:
                return
            time.sleep(0.2)
        pytest.fail(f"ssh-agent pid {agent_pid} survived 15s after starting process received SIGKILL")
    finally:
        if standin.poll() is None:
            standin.kill()
            standin.wait(timeout=5)
        if agent_pid is not None:
            try:
                os.kill(agent_pid, signal.SIGKILL)
            except ProcessLookupError:
                pass


def test_sweep_stale_ssh_ephemeral_dirs_kills_orphaned_agent_and_keeps_active(tmp_path: Path) -> None:
    agent_bin = shutil.which("ssh-agent")
    if not agent_bin:
        pytest.skip("ssh-agent is not installed in this environment.")

    layout = InstanceLayout.at(tmp_path / "inst")
    stale_dir = layout.ssh_ephemeral_dir / "ua-ssh-stale-crash"
    wrong_start_dir = layout.ssh_ephemeral_dir / "ua-ssh-wrong-start"
    no_start_dir = layout.ssh_ephemeral_dir / "ua-ssh-no-start"
    active_dir = layout.ssh_ephemeral_dir / "ua-ssh-active-live"
    outside_dir = tmp_path / "outside-precious"
    outside_dir.mkdir(parents=True)
    precious = outside_dir / "precious.txt"
    precious.write_text("do-not-delete", encoding="utf-8")

    for directory in (stale_dir, wrong_start_dir, no_start_dir, active_dir):
        directory.mkdir(parents=True, mode=0o700)
    symlink_entry = layout.ssh_ephemeral_dir / "ua-ssh-symlink-outside"
    symlink_entry.symlink_to(outside_dir)

    orphan = subprocess.Popen(
        [agent_bin, "-D", "-s", "-a", "./s"],
        cwd=str(stale_dir),
        stdout=subprocess.DEVNULL,
        stderr=subprocess.PIPE,
        text=True,
    )
    unrelated_agent = subprocess.Popen(
        [agent_bin, "-D", "-s", "-a", "./unrelated.sock"],
        cwd=str(tmp_path),
        stdout=subprocess.DEVNULL,
        stderr=subprocess.PIPE,
        text=True,
    )
    try:
        deadline = time.monotonic() + 5.0
        while not (stale_dir / "s").exists() and time.monotonic() < deadline:
            time.sleep(0.02)
        assert (stale_dir / "s").exists()

        orphan_start = _read_proc_start_time(orphan.pid)
        pid_line = f"{orphan.pid} {orphan_start}\n" if orphan_start is not None else f"{orphan.pid}\n"
        (stale_dir / "agent.pid").write_text(pid_line, encoding="utf-8")
        (stale_dir / "leftover.txt").write_text("stale-data", encoding="utf-8")

        # C23: Unrelated ssh-agent with mismatched or missing start time must NOT be stopped.
        unrelated_start = _read_proc_start_time(unrelated_agent.pid)
        wrong_start = (unrelated_start + 99999) if unrelated_start is not None else 1
        (wrong_start_dir / "agent.pid").write_text(f"{unrelated_agent.pid} {wrong_start}\n", encoding="utf-8")
        (no_start_dir / "agent.pid").write_text(f"{unrelated_agent.pid}\n", encoding="utf-8")
        (active_dir / "keep.txt").write_text("still-live", encoding="utf-8")

        swept = sweep_stale_ssh_ephemeral_dirs(
            layout.ssh_ephemeral_dir,
            active_dirs=[active_dir],
        )
        assert swept == 3
        assert not stale_dir.exists()
        assert not wrong_start_dir.exists()
        assert not no_start_dir.exists()
        assert active_dir.exists()
        assert orphan.wait(timeout=5) is not None
        # Symlink was skipped and external file is intact.
        assert symlink_entry.is_symlink()
        assert precious.read_text(encoding="utf-8") == "do-not-delete"
        # Unrelated ssh-agent was NOT killed on Linux.
        if sys.platform == "linux":
            assert unrelated_agent.poll() is None
    finally:
        for proc in (orphan, unrelated_agent):
            if proc.poll() is None:
                proc.kill()
                proc.wait(timeout=5)


@pytest.mark.asyncio
async def test_broker_open_and_close_do_not_block_event_loop_and_scrub_split_output(
    tmp_path: Path,
) -> None:
    """C25 + C26: Material enter/exit run off the event loop; split secrets and held tails scrub cleanly."""

    exact_secret = "SuperSecretBrokerPw!99"
    split_token = "ghp_1234567890abcdef1234567890abcdef"
    identity, known_hosts = write_ssh_material(tmp_path / "ssh", key_bytes=b"host-key-bytes")

    # Fake ssh script that writes split exact secret, split token pattern, and a held prefix then exits.
    fake_ssh_path = tmp_path / "bin" / "split-ssh"
    fake_ssh_path.parent.mkdir(parents=True, exist_ok=True)
    fake_ssh_path.write_text(
        f"#!{sys.executable}\n"
        "import sys, time\n"
        f"sys.stdout.write('pw=' + {exact_secret[:10]!r}); sys.stdout.flush(); time.sleep(0.08)\n"
        f"sys.stdout.write({exact_secret[10:]!r} + ' tok=' + {split_token[:14]!r}); sys.stdout.flush(); time.sleep(0.08)\n"
        f"sys.stdout.write({split_token[14:]!r} + '\\nlogin as Super'); sys.stdout.flush()\n",
        encoding="utf-8",
    )
    fake_ssh_path.chmod(0o755)

    class _SlowSecretMaterialProvider:
        @contextmanager
        def prepare(self, _session: InteractiveSessionV1) -> Iterator[SSHCredentialMaterial]:
            time.sleep(0.25)
            try:
                yield SSHCredentialMaterial(
                    identity_file=identity,
                    known_hosts_file=known_hosts,
                    touched_secrets=(exact_secret,),
                )
            finally:
                time.sleep(0.25)

    target = SSHConnectionTargetV1(
        host="ops.internal",
        username="deploy",
        host_key_algorithm="ssh-ed25519",
        host_key_fingerprint=host_key_fingerprint(b"host-key-bytes"),
        credential_binding_id="slow_binding",
    )
    digest = target.calculate_digest()
    session = InteractiveSessionV1(
        session_id="ssh_sess_slow_scrub",
        owner=team_owner(),
        target=target,
        target_digest=digest,
        connect_activation=build_activation(
            ExecutionActivationKind.SSH_CONNECT,
            resource_id="ssh_sess_slow_scrub",
            resource_digest=digest,
            function="create_execution_ssh_session",
            expires_at=datetime.now(timezone.utc) + timedelta(minutes=10),
        ),
    )
    repository = SqliteExecutionControlRepository(str(tmp_path / "execution.db"))
    repository.create_interactive_session(session)
    broker = SSHSessionBroker(
        repository=repository,
        authorization_guard=ExecutionAccessGuard(authorization_enabled=False),
        material_provider=_SlowSecretMaterialProvider(),
        artifact_store=FilesystemArtifactStore(tmp_path / "artifacts"),
        team_id="team_alpha",
        broker_id="broker-async-test",
        ssh_executable=str(fake_ssh_path),
    )

    # Verify concurrent coroutine keeps ticking during broker.open().
    open_ticks = 0
    stop_ticking = asyncio.Event()

    async def ticker() -> None:
        nonlocal open_ticks
        while not stop_ticking.is_set():
            open_ticks += 1
            await asyncio.sleep(0.02)

    tick_task = asyncio.create_task(ticker())
    try:
        await broker.open(session_id=session.session_id, lease_token="lease-1")
    finally:
        stop_ticking.set()
        await tick_task
    assert open_ticks >= 5, f"Event loop was blocked during broker.open() (ticks={open_ticks})"

    # Read until the script exits (EOF triggers flush + close).
    received_chunks: list[bytes] = []
    for _ in range(20):
        if session.session_id not in broker._sessions:
            break
        chunk = await broker.read(session_id=session.session_id, timeout_seconds=0.05)
        if chunk:
            assert exact_secret.encode() not in chunk
            assert split_token[:14].encode() not in chunk
            assert split_token[14:].encode() not in chunk
            received_chunks.append(chunk)

    combined = b"".join(received_chunks).decode("utf-8", errors="replace")
    assert exact_secret not in combined
    assert split_token not in combined
    assert "pw=[REDACTED] tok=[REDACTED]" in combined
    assert "login as Super" in combined

    persisted = repository.get_interactive_session(team_id="team_alpha", session_id=session.session_id)
    assert persisted is not None and persisted.transcript_artifact is not None
    transcript_text = broker.artifact_store.read_verified(persisted.transcript_artifact).decode("utf-8")
    assert exact_secret not in transcript_text
    assert split_token not in transcript_text
    assert "pw=[REDACTED] tok=[REDACTED]" in transcript_text
    assert "login as Super" in transcript_text

    # Also verify broker.close() does not block the event loop when __exit__ is slow.
    session2 = session.model_copy(
        update={
            "session_id": "ssh_sess_slow_close",
            "connect_activation": build_activation(
                ExecutionActivationKind.SSH_CONNECT,
                resource_id="ssh_sess_slow_close",
                resource_digest=digest,
                function="create_execution_ssh_session",
                expires_at=datetime.now(timezone.utc) + timedelta(minutes=10),
            ),
        }
    )
    repository.create_interactive_session(session2)
    broker2 = SSHSessionBroker(
        repository=repository,
        authorization_guard=ExecutionAccessGuard(authorization_enabled=False),
        material_provider=_SlowSecretMaterialProvider(),
        artifact_store=FilesystemArtifactStore(tmp_path / "artifacts2"),
        team_id="team_alpha",
        broker_id="broker-async-test-2",
        ssh_executable=str(fake_ssh(tmp_path / "bin2")),
    )
    await broker2.open(session_id=session2.session_id, lease_token="lease-2")

    close_ticks = 0
    stop_close_ticking = asyncio.Event()

    async def close_ticker() -> None:
        nonlocal close_ticks
        while not stop_close_ticking.is_set():
            close_ticks += 1
            await asyncio.sleep(0.02)

    close_tick_task = asyncio.create_task(close_ticker())
    try:
        await broker2.close(session_id=session2.session_id)
    finally:
        stop_close_ticking.set()
        await close_tick_task
    assert close_ticks >= 5, f"Event loop was blocked during broker.close() (ticks={close_ticks})"


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
        target_digest=static_target.calculate_digest(),
    )
    with composite_no_catalog.prepare(static_session) as resolved:
        assert resolved == static_mat

    catalog_target = static_target.model_copy(update={"credential_binding_id": "catalog:new_key"})
    catalog_session = InteractiveSessionV1(
        session_id="ssh_sess_catalog",
        owner=team_owner(),
        target=catalog_target,
        target_digest=catalog_target.calculate_digest(),
    )
    with pytest.raises(PermissionError, match="not enabled"):
        with composite_no_catalog.prepare(catalog_session):
            pass


def test_start_ephemeral_agent_enforces_5s_timeout_on_silent_standin(tmp_path: Path) -> None:
    """C28: Silent stand-in ssh-agent hits the default 5-second limit and leaves no process behind."""

    pid_file = tmp_path / "standin.pid"
    silent_agent = tmp_path / "silent-ssh-agent"
    silent_agent.write_text(
        f"#!{sys.executable}\n"
        "import os, time\n"
        f"open({str(pid_file)!r}, 'w', encoding='utf-8').write(str(os.getpid()))\n"
        "time.sleep(25)\n",
        encoding="utf-8",
    )
    silent_agent.chmod(0o755)

    layout = InstanceLayout.at(tmp_path / "inst")
    provider = CatalogSSHCredentialMaterialProvider(
        ephemeral_base_dir=layout.ssh_ephemeral_dir,
        ssh_agent_executable=str(silent_agent),
    )
    assert provider.agent_start_timeout_seconds == 5.0
    session_dir = layout.ssh_ephemeral_dir / "ua-ssh-silent-timeout"
    session_dir.mkdir(parents=True, mode=0o700)

    started = time.monotonic()
    with pytest.raises(RuntimeError, match="timed out"):
        provider._start_ephemeral_agent(session_dir)
    elapsed = time.monotonic() - started
    assert 4.5 <= elapsed <= 8.5, f"Expected ~5s timeout, took {elapsed:.2f}s"

    recorded_pid = int(pid_file.read_text(encoding="utf-8").strip())
    with pytest.raises(ProcessLookupError):
        os.kill(recorded_pid, 0)


def test_start_ephemeral_agent_handles_partial_pid_and_slow_python_with_real_agent(
    tmp_path: Path,
) -> None:
    """C28: Partial PID lines and slow Python under real ssh-agent time out cleanly and leave no agent."""

    # (a) Partial line without newline must not block past the deadline.
    partial_pid_file = tmp_path / "partial.pid"
    partial_agent = tmp_path / "partial-ssh-agent"
    partial_agent.write_text(
        f"#!{sys.executable}\n"
        "import os, sys, time\n"
        f"open({str(partial_pid_file)!r}, 'w', encoding='utf-8').write(str(os.getpid()))\n"
        "sys.stdout.write('12345')\n"
        "sys.stdout.flush()\n"
        "time.sleep(25)\n",
        encoding="utf-8",
    )
    partial_agent.chmod(0o755)

    layout = InstanceLayout.at(tmp_path / "inst-partial")
    provider = CatalogSSHCredentialMaterialProvider(
        ephemeral_base_dir=layout.ssh_ephemeral_dir,
        ssh_agent_executable=str(partial_agent),
        agent_start_timeout_seconds=0.6,
    )
    session_dir = layout.ssh_ephemeral_dir / "ua-ssh-partial"
    session_dir.mkdir(parents=True, mode=0o700)

    t0 = time.monotonic()
    with pytest.raises(RuntimeError, match="timed out"):
        provider._start_ephemeral_agent(session_dir)
    assert time.monotonic() - t0 < 3.0
    partial_pid = int(partial_pid_file.read_text(encoding="utf-8").strip())
    with pytest.raises(ProcessLookupError):
        os.kill(partial_pid, 0)

    # (b) Real ssh-agent + slow Python interpreter must not hang or orphan ssh-agent.
    agent_bin = shutil.which("ssh-agent")
    if not agent_bin:
        return
    slow_py_agent_pid_file = tmp_path / "real_agent.pid"
    slow_python = tmp_path / "slow-python"
    slow_python.write_text(
        "#!/bin/sh\n"
        f"printf '%s' \"$SSH_AGENT_PID\" > '{slow_py_agent_pid_file}'\n"
        "sleep 15\n",
        encoding="utf-8",
    )
    slow_python.chmod(0o755)

    layout2 = InstanceLayout.at(tmp_path / "inst-slow-py")
    provider2 = CatalogSSHCredentialMaterialProvider(
        ephemeral_base_dir=layout2.ssh_ephemeral_dir,
        ssh_agent_executable=agent_bin,
        python_executable=str(slow_python),
        agent_start_timeout_seconds=0.8,
    )
    session_dir2 = layout2.ssh_ephemeral_dir / "ua-ssh-slow-py"
    session_dir2.mkdir(parents=True, mode=0o700)

    t1 = time.monotonic()
    with pytest.raises(RuntimeError, match="timed out"):
        provider2._start_ephemeral_agent(session_dir2)
    assert time.monotonic() - t1 < 4.0
    assert slow_py_agent_pid_file.exists(), "slow wrapper must record real ssh-agent pid"
    raw_pid = slow_py_agent_pid_file.read_text(encoding="utf-8").strip()
    assert raw_pid.isdigit()
    with pytest.raises(ProcessLookupError):
        os.kill(int(raw_pid), 0)


def test_stream_scrubber_quiet_release_and_overlong_token_fail_closed() -> None:
    """C29: Word-ending prompts release after quiet time; overlong split tokens fail closed."""

    now = [0.0]
    scrubber = SessionSecretScrubber(["SuperSecretBrokerPw!99"])
    stream = scrubber.new_stream_scrubber(
        max_pattern_holdback=1024,
        quiet_release_seconds=0.3,
        clock=lambda: now[0],
    )

    # Word-ending prompt releases after 300ms quiet time.
    assert stream.feed(b"Enter the new hostname") == b"Enter the new "
    now[0] = 0.2
    assert stream.release_quiet() == b""
    now[0] = 0.31
    assert stream.release_quiet() == b"hostname"
    assert stream.release_quiet() == b""

    # Standalone word, pager prompt, and trailing ANSI sequence release after quiet time.
    for sample in (b"Username", b"--More--", b"deploy@box:~$ \x1b[?2004h"):
        s = scrubber.new_stream_scrubber(quiet_release_seconds=0.3, clock=lambda: now[0])
        now[0] = 1.0
        first = s.feed(sample)
        now[0] = 1.35
        second = s.release_quiet()
        assert first + second == sample

    # A held prefix of a registered exact secret MUST stay held even after quiet time
    # (while any preceding non-secret token bytes such as "pw=" release cleanly).
    secret_stream = scrubber.new_stream_scrubber(quiet_release_seconds=0.3, clock=lambda: now[0])
    now[0] = 2.0
    assert secret_stream.feed(b"pw=SuperSecret") == b""
    now[0] = 3.0
    assert secret_stream.release_quiet() == b"pw="
    assert secret_stream.time_until_quiet_release() is None
    assert secret_stream.release_quiet() == b""
    assert secret_stream.feed(b"BrokerPw!99\n") == b"[REDACTED]\n"

    # A complete pattern token held at end of buffer redacts on quiet release.
    gh_tok = b"ghp_1234567890abcdef1234567890abcdef"
    tok_stream = scrubber.new_stream_scrubber(quiet_release_seconds=0.3, clock=lambda: now[0])
    now[0] = 4.0
    assert tok_stream.feed(gh_tok) == b""
    now[0] = 4.35
    assert tok_stream.release_quiet() == b"[REDACTED]"

    # Overlong 2008-byte JWT split at late points fails closed with zero leaked bytes.
    header = "eyJhbGciOiJSUzI1NiIsInR5cCI6IkpXVCJ9"
    payload = "eyJzdWIiOiJrdWJlcm5ldGVzLXNlcnZpY2UtYWNjb3VudCJ9" + ("A" * 1850)
    sig = "B" * (2008 - len(header) - 1 - len(payload) - 1)
    long_jwt = f"{header}.{payload}.{sig}"
    assert len(long_jwt) == 2008
    jwt_bytes = long_jwt.encode("ascii")

    for split_points in ((1024,), (1500,), (1900,), (900, 1900)):
        ov_stream = scrubber.new_stream_scrubber(max_pattern_holdback=1024)
        chunks: list[bytes] = []
        cursor = 0
        for idx, pt in enumerate(split_points):
            prefix = b"tok=" if idx == 0 else b""
            chunks.append(prefix + jwt_bytes[cursor:pt])
            cursor = pt
        chunks.append(jwt_bytes[cursor:] + b" done\n")
        emitted = [ov_stream.feed(chunk) for chunk in chunks]
        emitted.append(ov_stream.flush())
        combined = b"".join(emitted)
        assert header.encode("ascii") not in combined
        assert b"AAAA" not in combined
        assert b"BBBB" not in combined
        assert combined == b"[REDACTED] done\n"

    # C33: Steady progress dots (no letter or digit) release immediately and never hit [REDACTED].
    dot_stream = scrubber.new_stream_scrubber(
        max_pattern_holdback=1024,
        quiet_release_seconds=0.3,
        clock=lambda: now[0],
    )
    now[0] = 10.0
    assert dot_stream.feed(b"waiting ") == b"waiting "
    for _ in range(24):
        now[0] += 0.25
        assert dot_stream.feed(b".") == b"."
    assert dot_stream.feed(b"\nstatus: done\n") == b"\nstatus: done\n"
    assert dot_stream.flush() == b""

    # 1500 progress dots (exceeding 1024) stay visible and are never replaced with [REDACTED].
    many_dots_stream = scrubber.new_stream_scrubber(max_pattern_holdback=1024)
    emitted_dots = b"".join(many_dots_stream.feed(b".") for _ in range(1500))
    emitted_dots += many_dots_stream.flush()
    assert emitted_dots == b"." * 1500
    assert b"[REDACTED]" not in emitted_dots

    # Punctuation before a split token releases immediately while the alphanumeric token is still redacted.
    prefixed_tok_stream = scrubber.new_stream_scrubber(max_pattern_holdback=1024)
    assert prefixed_tok_stream.feed(b"....ghp_1234") == b"...."
    assert prefixed_tok_stream.feed(b"567890abcdef1234567890abcdef\n") == b"[REDACTED]\n"
    assert prefixed_tok_stream.flush() == b""

    # Exact stored secrets consisting only of punctuation remain protected by prefix holdback.
    punct_secret_scrubber = SessionSecretScrubber(["..==..--"])
    punct_stream = punct_secret_scrubber.new_stream_scrubber(max_pattern_holdback=1024)
    assert punct_stream.feed(b"val ..==") == b"val "
    assert punct_stream.feed(b"..-- ok\n") == b"[REDACTED] ok\n"
    assert punct_stream.flush() == b""


@pytest.mark.asyncio
async def test_broker_read_releases_word_prompts_and_handles_delayed_split_tokens(
    tmp_path: Path,
) -> None:
    """C29: Broker read() emits word-ending prompts after quiet time and still catches buffered split tokens."""

    identity, known_hosts = write_ssh_material(tmp_path / "ssh", key_bytes=b"host-key-bytes")
    split_token = "ghp_1234567890abcdef1234567890abcdef"
    prompt_ssh = tmp_path / "bin" / "prompt-ssh"
    prompt_ssh.parent.mkdir(parents=True, exist_ok=True)
    prompt_ssh.write_text(
        f"#!{sys.executable}\n"
        "import sys, time\n"
        "sys.stdout.write('Setup wizard\\nEnter the new hostname'); sys.stdout.flush()\n"
        "time.sleep(0.55)\n"
        "sys.stdout.write('\\nUsername'); sys.stdout.flush()\n"
        "time.sleep(0.55)\n"
        f"sys.stdout.write('\\n{split_token[:12]}'); sys.stdout.flush()\n"
        "time.sleep(0.02)\n"
        f"sys.stdout.write('{split_token[12:]}\\n'); sys.stdout.flush()\n"
        "time.sleep(10)\n",
        encoding="utf-8",
    )
    prompt_ssh.chmod(0o755)

    target = SSHConnectionTargetV1(
        host="ops.internal",
        username="deploy",
        host_key_algorithm="ssh-ed25519",
        host_key_fingerprint=host_key_fingerprint(b"host-key-bytes"),
        credential_binding_id="prompt_binding",
    )
    digest = target.calculate_digest()
    session = InteractiveSessionV1(
        session_id="ssh_sess_word_prompt",
        owner=team_owner(),
        target=target,
        target_digest=digest,
        connect_activation=build_activation(
            ExecutionActivationKind.SSH_CONNECT,
            resource_id="ssh_sess_word_prompt",
            resource_digest=digest,
            function="create_execution_ssh_session",
            expires_at=datetime.now(timezone.utc) + timedelta(minutes=10),
        ),
    )
    repository = SqliteExecutionControlRepository(str(tmp_path / "execution.db"))
    repository.create_interactive_session(session)
    broker = SSHSessionBroker(
        repository=repository,
        authorization_guard=ExecutionAccessGuard(authorization_enabled=False),
        material_provider=ConfiguredSSHCredentialMaterialProvider(
            {"prompt_binding": SSHCredentialMaterial(identity_file=identity, known_hosts_file=known_hosts)}
        ),
        artifact_store=FilesystemArtifactStore(tmp_path / "artifacts"),
        team_id="team_alpha",
        broker_id="broker-prompt-test",
        ssh_executable=str(prompt_ssh),
    )

    await broker.open(session_id=session.session_id, lease_token="lease-p")
    try:
        collected = b""
        deadline = time.monotonic() + 2.5
        while b"Enter the new hostname" not in collected and time.monotonic() < deadline:
            collected += await broker.read(session_id=session.session_id, timeout_seconds=0.5)
        assert b"Enter the new hostname" in collected

        deadline = time.monotonic() + 2.5
        while b"Username" not in collected and time.monotonic() < deadline:
            collected += await broker.read(session_id=session.session_id, timeout_seconds=0.5)
        assert b"Username" in collected

        # Wait until both halves are sitting in the PTY buffer, then read only the
        # first half with a small maximum_bytes so stream_scrubber holds it in
        # _pending while the second half stays buffered; then wait past quiet time
        # and verify the next read drains the second half BEFORE releasing quiet bytes.
        await asyncio.sleep(0.5)
        first_chunk = await broker.read(
            session_id=session.session_id,
            maximum_bytes=14,  # "\r\n" + first 12 bytes of split_token
            timeout_seconds=0.1,
        )
        assert split_token[:12].encode("ascii") not in first_chunk
        await asyncio.sleep(0.4)
        token_chunk = await broker.read(session_id=session.session_id, timeout_seconds=0.2)
        assert split_token.encode("ascii") not in (first_chunk + token_chunk)
        assert split_token[:12].encode("ascii") not in (first_chunk + token_chunk)
        assert b"[REDACTED]" in (first_chunk + token_chunk)
    finally:
        await broker.close(session_id=session.session_id)


@pytest.mark.asyncio
async def test_broker_open_and_close_cancellation_cleans_up_material_and_persists_transcript(
    tmp_path: Path,
) -> None:
    """C30: Cancelling open() or close() exits material, marks failed/closed, and keeps transcript."""

    identity, known_hosts = write_ssh_material(tmp_path / "ssh", key_bytes=b"host-key-bytes")
    open_exited = False
    close_exited = False

    class _TrackedSlowProvider:
        def __init__(self, *, slow_enter: float, slow_exit: float, mark: str) -> None:
            self.slow_enter = slow_enter
            self.slow_exit = slow_exit
            self.mark = mark

        @contextmanager
        def prepare(self, _session: InteractiveSessionV1) -> Iterator[SSHCredentialMaterial]:
            nonlocal open_exited, close_exited
            if self.slow_enter:
                time.sleep(self.slow_enter)
            try:
                yield SSHCredentialMaterial(identity_file=identity, known_hosts_file=known_hosts)
            finally:
                if self.slow_exit:
                    time.sleep(self.slow_exit)
                if self.mark == "open":
                    open_exited = True
                else:
                    close_exited = True

    target = SSHConnectionTargetV1(
        host="ops.internal",
        username="deploy",
        host_key_algorithm="ssh-ed25519",
        host_key_fingerprint=host_key_fingerprint(b"host-key-bytes"),
        credential_binding_id="slow_cancel_binding",
    )
    digest = target.calculate_digest()
    repository = SqliteExecutionControlRepository(str(tmp_path / "execution.db"))

    # (a) Cancel during slow open() prepare.
    sess_open = InteractiveSessionV1(
        session_id="ssh_sess_cancel_open",
        owner=team_owner(),
        target=target,
        target_digest=digest,
        connect_activation=build_activation(
            ExecutionActivationKind.SSH_CONNECT,
            resource_id="ssh_sess_cancel_open",
            resource_digest=digest,
            function="create_execution_ssh_session",
            expires_at=datetime.now(timezone.utc) + timedelta(minutes=10),
        ),
    )
    repository.create_interactive_session(sess_open)
    broker_open = SSHSessionBroker(
        repository=repository,
        authorization_guard=ExecutionAccessGuard(authorization_enabled=False),
        material_provider=_TrackedSlowProvider(slow_enter=0.4, slow_exit=0.0, mark="open"),
        artifact_store=FilesystemArtifactStore(tmp_path / "artifacts-open"),
        team_id="team_alpha",
        broker_id="broker-cancel-open",
        ssh_executable=str(fake_ssh(tmp_path / "bin-open")),
    )
    open_task = asyncio.create_task(
        broker_open.open(session_id=sess_open.session_id, lease_token="lease-cancel-1")
    )
    await asyncio.sleep(0.1)
    open_task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await open_task
    assert open_exited is True
    stored_open = repository.get_interactive_session(
        team_id="team_alpha",
        session_id=sess_open.session_id,
    )
    assert stored_open is not None
    assert stored_open.status == InteractiveSessionStatus.FAILED
    assert stored_open.lease_owner is None
    assert sess_open.session_id not in broker_open._sessions

    # (b) Cancel during slow close() material exit.
    sess_close = InteractiveSessionV1(
        session_id="ssh_sess_cancel_close",
        owner=team_owner(),
        target=target,
        target_digest=digest,
        connect_activation=build_activation(
            ExecutionActivationKind.SSH_CONNECT,
            resource_id="ssh_sess_cancel_close",
            resource_digest=digest,
            function="create_execution_ssh_session",
            expires_at=datetime.now(timezone.utc) + timedelta(minutes=10),
        ),
    )
    repository.create_interactive_session(sess_close)
    broker_close = SSHSessionBroker(
        repository=repository,
        authorization_guard=ExecutionAccessGuard(authorization_enabled=False),
        material_provider=_TrackedSlowProvider(slow_enter=0.0, slow_exit=0.4, mark="close"),
        artifact_store=FilesystemArtifactStore(tmp_path / "artifacts-close"),
        team_id="team_alpha",
        broker_id="broker-cancel-close",
        ssh_executable=str(fake_ssh(tmp_path / "bin-close")),
    )
    await broker_close.open(session_id=sess_close.session_id, lease_token="lease-cancel-2")
    repository.put_ssh_interaction(
        build_activation(
            ExecutionActivationKind.SSH_INTERACT,
            resource_id=sess_close.session_id,
            resource_digest=digest,
            function="change:execution_ssh_interaction",
        )
    )
    await broker_close.write(session_id=sess_close.session_id, payload=b"keep-transcript\n")
    out = b""
    for _ in range(5):
        out += await broker_close.read(session_id=sess_close.session_id, timeout_seconds=0.5)
        if b"remote:keep-transcript" in out:
            break
    assert b"remote:keep-transcript" in out

    close_task = asyncio.create_task(broker_close.close(session_id=sess_close.session_id))
    await asyncio.sleep(0.1)
    close_task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await close_task
    assert close_exited is True
    stored_close = repository.get_interactive_session(
        team_id="team_alpha",
        session_id=sess_close.session_id,
    )
    assert stored_close is not None
    assert stored_close.status == InteractiveSessionStatus.CLOSED
    assert stored_close.lease_owner is None
    assert stored_close.transcript_artifact is not None
    transcript = broker_close.artifact_store.read_verified(stored_close.transcript_artifact)
    assert b"remote:keep-transcript" in transcript


@pytest.mark.asyncio
async def test_dot_leading_paths_truncated_secret_transcript_and_repr_hiding(
    tmp_path: Path,
) -> None:
    """C31: Dot-leading relative paths stay intact; truncated transcripts drop secret prefixes; repr hides raw output."""

    # (1) Dot-leading relative paths keep their leading dot.
    mat = SSHCredentialMaterial(
        identity_file=Path(".keys/id.pub"),
        known_hosts_file=Path("./.ssh/known_hosts"),
        working_dir=tmp_path,
    )
    assert mat.argv_identity_file() == "./.keys/id.pub"
    assert mat.argv_known_hosts_file() == "./.ssh/known_hosts"
    assert mat.resolved_identity_file() == tmp_path / ".keys/id.pub"
    assert mat.resolved_known_hosts_file() == tmp_path / ".ssh/known_hosts"

    dot_only = SSHCredentialMaterial(
        identity_file=Path("."),
        known_hosts_file=Path("known_hosts"),
        working_dir=tmp_path,
    )
    with pytest.raises(PermissionError, match="must not be empty"):
        dot_only.argv_identity_file()

    # (2) & (3) Secret cut by transcript_limit_bytes is stripped; live repr() hides raw_transcript & _sessions.
    secret_pw = "SuperSecretBrokerPw!99"
    identity, known_hosts = write_ssh_material(tmp_path / "ssh", key_bytes=b"host-key-bytes")
    trunc_ssh = tmp_path / "bin" / "trunc-ssh"
    trunc_ssh.parent.mkdir(parents=True, exist_ok=True)
    trunc_ssh.write_text(
        f"#!{sys.executable}\n"
        "import sys, time\n"
        f"sys.stdout.write('pw={secret_pw}\\n'); sys.stdout.flush()\n"
        "time.sleep(10)\n",
        encoding="utf-8",
    )
    trunc_ssh.chmod(0o755)

    target = SSHConnectionTargetV1(
        host="ops.internal",
        username="deploy",
        host_key_algorithm="ssh-ed25519",
        host_key_fingerprint=host_key_fingerprint(b"host-key-bytes"),
        credential_binding_id="trunc_binding",
    )
    digest = target.calculate_digest()
    session = InteractiveSessionV1(
        session_id="ssh_sess_trunc",
        owner=team_owner(),
        target=target,
        target_digest=digest,
        connect_activation=build_activation(
            ExecutionActivationKind.SSH_CONNECT,
            resource_id="ssh_sess_trunc",
            resource_digest=digest,
            function="create_execution_ssh_session",
            expires_at=datetime.now(timezone.utc) + timedelta(minutes=10),
        ),
    )
    repository = SqliteExecutionControlRepository(str(tmp_path / "execution.db"))
    repository.create_interactive_session(session)
    broker = SSHSessionBroker(
        repository=repository,
        authorization_guard=ExecutionAccessGuard(authorization_enabled=False),
        material_provider=ConfiguredSSHCredentialMaterialProvider(
            {
                "trunc_binding": SSHCredentialMaterial(
                    identity_file=identity,
                    known_hosts_file=known_hosts,
                    touched_secrets=(secret_pw,),
                )
            }
        ),
        artifact_store=FilesystemArtifactStore(tmp_path / "artifacts-trunc"),
        team_id="team_alpha",
        broker_id="broker-trunc",
        ssh_executable=str(trunc_ssh),
        transcript_limit_bytes=18,  # cuts 15 bytes into the 22-byte secret ("pw=SuperSecretBrok")
    )

    await broker.open(session_id=session.session_id, lease_token="lease-trunc")
    chunk = await broker.read(session_id=session.session_id, timeout_seconds=1.0)
    assert secret_pw.encode("ascii") not in chunk
    live = broker._sessions[session.session_id]
    assert "SuperSecret" not in repr(live)
    assert "SuperSecret" not in repr(broker)
    assert "raw_transcript" not in repr(live)
    assert "_sessions" not in repr(broker)

    closed = await broker.close(session_id=session.session_id)
    assert closed.transcript_artifact is not None
    transcript = broker.artifact_store.read_verified(closed.transcript_artifact)
    assert transcript == b"pw="
    assert b"SuperSecret" not in transcript


@pytest.mark.asyncio
async def test_broker_loop_shutdown_cancellation_does_not_spin_on_open_or_close(
    tmp_path: Path,
) -> None:
    """C32: Cancelling every task on the loop (like asyncio.run shutdown) exits promptly without spinning."""

    identity, known_hosts = write_ssh_material(tmp_path / "ssh", key_bytes=b"host-key-bytes")
    open_exited = False
    close_exited = False

    class _SlowProvider:
        def __init__(self, *, slow_enter: float, slow_exit: float, mark: str) -> None:
            self.slow_enter = slow_enter
            self.slow_exit = slow_exit
            self.mark = mark

        @contextmanager
        def prepare(self, _session: InteractiveSessionV1) -> Iterator[SSHCredentialMaterial]:
            nonlocal open_exited, close_exited
            if self.slow_enter:
                time.sleep(self.slow_enter)
            try:
                yield SSHCredentialMaterial(identity_file=identity, known_hosts_file=known_hosts)
            finally:
                if self.slow_exit:
                    time.sleep(self.slow_exit)
                if self.mark == "open":
                    open_exited = True
                else:
                    close_exited = True

    target = SSHConnectionTargetV1(
        host="ops.internal",
        username="deploy",
        host_key_algorithm="ssh-ed25519",
        host_key_fingerprint=host_key_fingerprint(b"host-key-bytes"),
        credential_binding_id="shutdown_cancel_binding",
    )
    digest = target.calculate_digest()
    repository = SqliteExecutionControlRepository(str(tmp_path / "execution.db"))

    # (a) Cancel EVERY task created by open() (mirroring asyncio.run's _cancel_all_tasks).
    sess_open = InteractiveSessionV1(
        session_id="ssh_sess_shutdown_open",
        owner=team_owner(),
        target=target,
        target_digest=digest,
        connect_activation=build_activation(
            ExecutionActivationKind.SSH_CONNECT,
            resource_id="ssh_sess_shutdown_open",
            resource_digest=digest,
            function="create_execution_ssh_session",
            expires_at=datetime.now(timezone.utc) + timedelta(minutes=10),
        ),
    )
    repository.create_interactive_session(sess_open)
    broker_open = SSHSessionBroker(
        repository=repository,
        authorization_guard=ExecutionAccessGuard(authorization_enabled=False),
        material_provider=_SlowProvider(slow_enter=0.5, slow_exit=0.0, mark="open"),
        artifact_store=FilesystemArtifactStore(tmp_path / "artifacts-shutdown-open"),
        team_id="team_alpha",
        broker_id="broker-shutdown-open",
        ssh_executable=str(fake_ssh(tmp_path / "bin-shutdown-open")),
    )
    current = asyncio.current_task()
    before_open_tasks = set(asyncio.all_tasks())
    open_task = asyncio.create_task(
        broker_open.open(session_id=sess_open.session_id, lease_token="lease-sd-open")
    )
    await asyncio.sleep(0.08)
    spawned_open = [t for t in (asyncio.all_tasks() - before_open_tasks) if t is not current]
    assert len(spawned_open) >= 2, "expected open_task and inner prepare_task"
    for task in spawned_open:
        task.cancel()
    t0 = time.monotonic()
    await asyncio.wait_for(asyncio.gather(*spawned_open, return_exceptions=True), timeout=2.0)
    assert time.monotonic() - t0 < 2.0
    assert open_task.cancelled()
    stored_open = repository.get_interactive_session(
        team_id="team_alpha",
        session_id=sess_open.session_id,
    )
    assert stored_open is not None
    assert stored_open.status == InteractiveSessionStatus.FAILED
    assert stored_open.lease_owner is None
    # Wait for the background prepare thread to finish and confirm it still exits the material.
    deadline = time.monotonic() + 2.0
    while not open_exited and time.monotonic() < deadline:
        await asyncio.sleep(0.05)
    assert open_exited is True

    # (b) Cancel EVERY task created by close() (mirroring asyncio.run's _cancel_all_tasks).
    sess_close = InteractiveSessionV1(
        session_id="ssh_sess_shutdown_close",
        owner=team_owner(),
        target=target,
        target_digest=digest,
        connect_activation=build_activation(
            ExecutionActivationKind.SSH_CONNECT,
            resource_id="ssh_sess_shutdown_close",
            resource_digest=digest,
            function="create_execution_ssh_session",
            expires_at=datetime.now(timezone.utc) + timedelta(minutes=10),
        ),
    )
    repository.create_interactive_session(sess_close)
    broker_close = SSHSessionBroker(
        repository=repository,
        authorization_guard=ExecutionAccessGuard(authorization_enabled=False),
        material_provider=_SlowProvider(slow_enter=0.0, slow_exit=0.5, mark="close"),
        artifact_store=FilesystemArtifactStore(tmp_path / "artifacts-shutdown-close"),
        team_id="team_alpha",
        broker_id="broker-shutdown-close",
        ssh_executable=str(fake_ssh(tmp_path / "bin-shutdown-close")),
    )
    await broker_close.open(session_id=sess_close.session_id, lease_token="lease-sd-close")
    before_close_tasks = set(asyncio.all_tasks())
    close_task = asyncio.create_task(broker_close.close(session_id=sess_close.session_id))
    await asyncio.sleep(0.08)
    spawned_close = [t for t in (asyncio.all_tasks() - before_close_tasks) if t is not current]
    assert len(spawned_close) >= 2, "expected close_task and inner _finish_close task"
    for task in spawned_close:
        task.cancel()
    t1 = time.monotonic()
    await asyncio.wait_for(asyncio.gather(*spawned_close, return_exceptions=True), timeout=2.0)
    assert time.monotonic() - t1 < 2.0
    assert close_task.cancelled()
    # Wait for the background exit thread to finish cleanly.
    deadline = time.monotonic() + 2.0
    while not close_exited and time.monotonic() < deadline:
        await asyncio.sleep(0.05)
    assert close_exited is True
