"""Unit and real-OpenSSH tests for SSH operator configuration and network guardrails."""

from __future__ import annotations

import base64
import getpass
from pathlib import Path
import shutil
import socket
import subprocess
import time

import pytest
import yaml

from assistant_gateway.config import GatewayInstanceConfig, _INSTANCE_CONFIG_COMMENTS
from assistant_gateway.runtime_config import load_runtime_config_file
from custom_messages.contracts.common_v1 import contract_sha256
from default_values import DEFAULT_SSH_DENIED_CIDRS
from runtime_hosting.artifacts import FilesystemArtifactStore
from runtime_hosting.authorization import ExecutionAccessGuard
from runtime_hosting.execution_contracts import (
    ExecutionActivationKind,
    InteractiveSessionV1,
    SSHConnectionTargetV1,
)
from runtime_hosting.ssh import (
    ConfiguredSSHCredentialMaterialProvider,
    SSHCredentialMaterial,
    SSHSessionBroker,
)
from runtime_hosting.ssh_network import (
    SSHNetworkPolicy,
    SSHNetworkPolicyError,
    ssh_host_key_alias,
    validate_ssh_host,
)
from runtime_settings.execution import ExecutionSSHSettings
from storage.execution_control_repository import SqliteExecutionControlRepository
from tests.execution_access_support import activation as build_activation
from tests.execution_access_support import host_key_fingerprint, team_owner

EXAMPLE_CONFIG_PATH = Path(__file__).resolve().parents[1] / "config.example.yaml"


def _example_ssh_lines() -> dict[str, tuple[str, str]]:
    """Return raw value and inline comment for each scalar/list key under execution_control.ssh."""

    lines = EXAMPLE_CONFIG_PATH.read_text(encoding="utf-8").splitlines()
    in_execution = False
    in_ssh = False
    entries: dict[str, tuple[str, str]] = {}
    for raw in lines:
        if raw.startswith("execution_control:"):
            in_execution = True
            continue
        if in_execution and raw and not raw.startswith((" ", "#")):
            break
        if in_execution and raw.startswith("  ssh:"):
            in_ssh = True
            continue
        if in_ssh and raw and not raw.startswith(("    ", "#")):
            break
        if in_ssh and raw.startswith("    ") and not raw.lstrip().startswith("#"):
            body, _, comment = raw.partition("#")
            key, _, value = body.strip().partition(":")
            entries[key.strip()] = (value.strip(), comment.strip())
    return entries


def test_ssh_settings_defaults_and_config_schema_parity() -> None:
    settings = ExecutionSSHSettings()

    assert settings.enabled is False
    assert settings.allow_password_bootstrap is False
    assert settings.max_delegation_ttl_seconds == 7200
    assert settings.allowed_cidrs == ()
    assert settings.allowed_domains == ()
    assert settings.denied_cidrs == DEFAULT_SSH_DENIED_CIDRS

    example = yaml.safe_load(EXAMPLE_CONFIG_PATH.read_text(encoding="utf-8"))
    example_ssh = example["execution_control"]["ssh"]
    assert settings.model_dump(mode="json") == example_ssh

    loaded = load_runtime_config_file(EXAMPLE_CONFIG_PATH)
    loaded_ssh = ExecutionSSHSettings.model_validate(loaded.to_dict()["execution_control"]["ssh"])
    assert loaded_ssh == settings

    instance_cfg = GatewayInstanceConfig(instance_name="test-ssh", instance_root=".")
    assert instance_cfg.execution_control.ssh == settings

    ssh_lines = _example_ssh_lines()
    for field_name in (
        "enabled",
        "ssh_executable",
        "lease_ttl_seconds",
        "transcript_limit_bytes",
        "allowed_cidrs",
        "allowed_domains",
        "denied_cidrs",
        "max_delegation_ttl_seconds",
        "allow_password_bootstrap",
        "targets",
    ):
        assert f"execution_control.ssh.{field_name}" in _INSTANCE_CONFIG_COMMENTS
        assert field_name in ssh_lines, f"{field_name} missing from config.example.yaml"
        assert ssh_lines[field_name][1], f"{field_name} has no inline comment in config.example.yaml"


def test_ssh_settings_validation_normalizes_and_rejects_bad_inputs() -> None:
    normalized = ExecutionSSHSettings.model_validate(
        {
            "allowed_cidrs": ["10.0.0.5/24", "10.0.0.0/24", "2001:db8::1/64"],
            "allowed_domains": [" Ops.Example.COM. ", "ops.example.com", "lab.local"],
            "denied_cidrs": ["127.0.0.1/8"],
            "max_delegation_ttl_seconds": 3600,
            "allow_password_bootstrap": True,
        }
    )
    assert normalized.allowed_cidrs == ("10.0.0.0/24", "2001:db8::/64")
    assert normalized.allowed_domains == ("ops.example.com", "lab.local")
    assert normalized.denied_cidrs == ("127.0.0.0/8",)
    assert normalized.max_delegation_ttl_seconds == 3600
    assert normalized.allow_password_bootstrap is True

    for invalid in (
        {"allowed_cidrs": "10.0.0.0/8"},
        {"allowed_cidrs": None},
        {"denied_cidrs": None},
        {"denied_cidrs": ["not-a-cidr"]},
        {"allowed_domains": "example.com"},
        {"allowed_domains": None},
        {"allowed_domains": ["example.com:22"]},
        {"allowed_domains": ["https://example.com"]},
        {"allowed_domains": ["*.example.com"]},
        {"allowed_domains": ["192.168.1.1"]},
        {"allowed_domains": ["bad domain"]},
        {"max_delegation_ttl_seconds": 0},
        {"max_delegation_ttl_seconds": -5},
        {"max_delegation_ttl_seconds": True},
    ):
        with pytest.raises(ValueError):
            ExecutionSSHSettings.model_validate(invalid)


@pytest.mark.parametrize(
    "host",
    [
        "127.0.0.1",
        "127.1",
        "::1",
        "[::1]",
        "0.0.0.0",
        "0",
        "::",
        "169.254.169.254",
        "fe80::1",
        "fe80::1%eth0",
        "::ffff:127.0.0.1",
        "::ffff:0.0.0.0",
        "::ffff:169.254.169.254",
        "224.0.0.1",
        "ff02::1",
        "::ffff:224.0.0.1",
    ],
)
def test_default_network_policy_refuses_loopback_unspecified_link_local_metadata_and_multicast(
    host: str,
) -> None:
    policy = SSHNetworkPolicy.default()
    with pytest.raises(SSHNetworkPolicyError):
        validate_ssh_host(host, 22, policy)


def test_network_policy_checks_resolved_ips_and_special_ranges_even_when_denied_cidrs_empty() -> None:
    policy = SSHNetworkPolicy.default()
    with pytest.raises(SSHNetworkPolicyError):
        validate_ssh_host("localhost", 22, policy, resolver=lambda _h, _p: ("127.0.0.1",))
    with pytest.raises(SSHNetworkPolicyError):
        validate_ssh_host("sub.localhost", 22, policy, resolver=lambda _h, _p: ("10.0.0.5",))
    with pytest.raises(SSHNetworkPolicyError):
        validate_ssh_host(
            "rebind.example.com",
            22,
            policy,
            resolver=lambda _h, _p: ("203.0.113.10", "169.254.169.254"),
        )

    # Even with denied_cidrs cleared, special ranges stay blocked unless explicitly in allowed_cidrs.
    open_policy = SSHNetworkPolicy(allowed_cidrs=(), allowed_domains=(), denied_cidrs=())
    for special in ("127.0.0.1", "0.0.0.0", "::", "169.254.169.254", "224.0.0.1"):
        with pytest.raises(SSHNetworkPolicyError):
            validate_ssh_host(special, 22, open_policy)

    # Explicit operator opt-in (removed from denied_cidrs and listed in allowed_cidrs) permits it.
    explicit_loopback = SSHNetworkPolicy.from_settings(
        ExecutionSSHSettings.model_validate(
            {"denied_cidrs": [], "allowed_cidrs": ["127.0.0.1/32"]}
        )
    )
    validated = validate_ssh_host("127.0.0.1", 2222, explicit_loopback)
    assert validated.resolved_ip == "127.0.0.1"
    assert validated.host_key_alias == "[127.0.0.1]:2222"


def test_network_policy_enforces_allowed_domains_allowed_cidrs_and_denied_precedence() -> None:
    policy = SSHNetworkPolicy.from_settings(
        ExecutionSSHSettings.model_validate(
            {
                "allowed_domains": ["ops.example.com"],
                "allowed_cidrs": ["100.64.0.0/10"],
                "denied_cidrs": [*DEFAULT_SSH_DENIED_CIDRS, "100.64.99.0/24"],
            }
        )
    )

    ok_domain = validate_ssh_host(
        "box1.ops.example.com",
        22,
        policy,
        resolver=lambda _h, _p: ("192.168.10.20",),
    )
    assert ok_domain.host == "box1.ops.example.com"
    assert ok_domain.resolved_ip == "192.168.10.20"
    assert ok_domain.host_key_alias == "box1.ops.example.com"

    ok_cidr = validate_ssh_host("100.64.1.5", 2222, policy)
    assert ok_cidr.resolved_ip == "100.64.1.5"
    assert ok_cidr.host_key_alias == "[100.64.1.5]:2222"

    # Partial suffix must not match allowed_domains.
    with pytest.raises(SSHNetworkPolicyError):
        validate_ssh_host(
            "evilops.example.com",
            22,
            policy,
            resolver=lambda _h, _p: ("192.168.10.20",),
        )

    # Denied CIDR overrides both allowed_cidrs and allowed_domains.
    with pytest.raises(SSHNetworkPolicyError):
        validate_ssh_host("100.64.99.5", 22, policy)
    with pytest.raises(SSHNetworkPolicyError):
        validate_ssh_host(
            "box2.ops.example.com",
            22,
            policy,
            resolver=lambda _h, _p: ("100.64.99.5",),
        )

    # DNS failure and option-injection hostnames fail closed.
    def _dns_fail(_h: str, _p: int) -> tuple[str, ...]:
        raise socket.gaierror("Name or service not known")

    with pytest.raises(SSHNetworkPolicyError):
        validate_ssh_host("missing.ops.example.com", 22, policy, resolver=_dns_fail)
    with pytest.raises(SSHNetworkPolicyError):
        validate_ssh_host("-oProxyCommand=id", 22, policy)
    with pytest.raises(SSHNetworkPolicyError):
        validate_ssh_host("box1.ops.example.com", 0, policy)


def test_ssh_host_key_alias_formats_default_and_non_default_ports() -> None:
    assert ssh_host_key_alias("ops.example.com", 22) == "ops.example.com"
    assert ssh_host_key_alias("ops.example.com", 2222) == "[ops.example.com]:2222"
    assert ssh_host_key_alias("2001:db8::1", 22) == "2001:db8::1"
    assert ssh_host_key_alias("2001:db8::1", 2222) == "[2001:db8::1]:2222"


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


def test_real_openssh_pinned_ip_with_host_key_alias_on_non_default_port(
    tmp_path: Path,
) -> None:
    sshd_bin = shutil.which("sshd")
    ssh_bin = shutil.which("ssh")
    keygen_bin = shutil.which("ssh-keygen")
    if not (sshd_bin and ssh_bin and keygen_bin):
        pytest.skip("OpenSSH sshd/ssh/ssh-keygen binaries are not installed in this environment.")

    host_key = tmp_path / "ssh_host_ed25519_key"
    client_key = tmp_path / "client_ed25519_key"
    subprocess.run(
        [keygen_bin, "-q", "-t", "ed25519", "-N", "", "-f", str(host_key)],
        check=True,
    )
    subprocess.run(
        [keygen_bin, "-q", "-t", "ed25519", "-N", "", "-f", str(client_key)],
        check=True,
    )
    client_key.chmod(0o600)

    authorized_keys = tmp_path / "authorized_keys"
    authorized_keys.write_text(
        client_key.with_suffix(".pub").read_text(encoding="utf-8"),
        encoding="utf-8",
    )
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
                "Subsystem sftp internal-sftp",
            ]
        )
        + "\n",
        encoding="utf-8",
    )

    host_pub_parts = host_key.with_suffix(".pub").read_text(encoding="utf-8").strip().split()
    algorithm, encoded_key = host_pub_parts[0], host_pub_parts[1]
    alias = ssh_host_key_alias("ops.internal", port)
    assert alias == f"[ops.internal]:{port}"
    known_hosts = tmp_path / "known_hosts"
    known_hosts.write_text(f"{alias} {algorithm} {encoded_key}\n", encoding="utf-8")

    key_bytes = base64.b64decode(encoded_key)
    target = SSHConnectionTargetV1(
        host="ops.internal",
        port=port,
        username=getpass.getuser(),
        host_key_algorithm=algorithm,
        host_key_fingerprint=host_key_fingerprint(key_bytes),
        credential_binding_id="binding-test",
    )
    digest = contract_sha256(target.model_dump(mode="json"))
    session = InteractiveSessionV1(
        session_id="ssh_sess_real_alias",
        owner=team_owner(),
        target=target,
        target_digest=digest,
        connect_activation=build_activation(
            ExecutionActivationKind.SSH_CONNECT,
            resource_id="ssh_sess_real_alias",
            resource_digest=digest,
            function="create_execution_ssh_session",
        ),
    )
    material = SSHCredentialMaterial(identity_file=client_key, known_hosts_file=known_hosts)
    broker = SSHSessionBroker(
        repository=SqliteExecutionControlRepository(str(tmp_path / "execution.db")),
        authorization_guard=ExecutionAccessGuard(authorization_enabled=False),
        material_provider=ConfiguredSSHCredentialMaterialProvider({"binding-test": material}),
        artifact_store=FilesystemArtifactStore(tmp_path / "artifacts"),
        team_id="team_alpha",
        broker_id="broker-test",
        ssh_executable=ssh_bin,
    )
    broker._verify_material(session, material)

    argv = list(broker._ssh_argv(session, material, resolved_ip="127.0.0.1"))
    assert "HostName=127.0.0.1" in argv
    assert f"HostKeyAlias={alias}" in argv
    # Replace -tt with -T for non-interactive command verification against real sshd.
    argv[argv.index("-tt")] = "-T"
    argv.append("echo pinned-ok")

    sshd = subprocess.Popen(
        [sshd_bin, "-D", "-e", "-f", str(sshd_config)],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    try:
        _wait_for_port(port)
        result = subprocess.run(
            argv,
            capture_output=True,
            text=True,
            timeout=10,
            check=False,
        )
        assert result.returncode == 0, f"ssh failed: stdout={result.stdout!r} stderr={result.stderr!r}"
        assert "pinned-ok" in result.stdout
    finally:
        sshd.terminate()
        try:
            sshd.wait(timeout=5)
        except subprocess.TimeoutExpired:
            sshd.kill()
            sshd.wait(timeout=5)
