"""Unit and real-OpenSSH tests for SSH operator configuration and network guardrails."""

from __future__ import annotations

import base64
import getpass
import ipaddress
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
    ValidatedSSHHost,
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


def _first_local_non_loopback_ip() -> str | None:
    """Return one non-loopback address assigned to this machine, if any."""

    try:
        output = subprocess.check_output(["hostname", "-I"], text=True, timeout=3).strip()
    except (OSError, subprocess.SubprocessError):
        return None
    for token in output.split():
        try:
            parsed = ipaddress.ip_address(token)
        except ValueError:
            continue
        if not (
            parsed.is_loopback
            or parsed.is_link_local
            or parsed.is_unspecified
            or parsed.is_multicast
        ):
            return str(parsed)
    return None


def test_ssh_settings_defaults_and_config_schema_parity() -> None:
    settings = ExecutionSSHSettings()

    assert settings.enabled is False
    assert settings.allow_password_bootstrap is False
    assert settings.max_delegation_ttl_seconds == 7200
    assert settings.allowed_cidrs == ()
    assert settings.allowed_domains == ()
    assert settings.denied_cidrs == DEFAULT_SSH_DENIED_CIDRS
    assert "fd00:ec2::254/128" in settings.denied_cidrs

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

    # C20: empty/None and non-whole numbers raise clear ValueErrors naming the setting.
    for field_name in ("lease_ttl_seconds", "transcript_limit_bytes", "max_delegation_ttl_seconds"):
        for bad_value in (None, 3.9, "3.9", "abc"):
            with pytest.raises(ValueError, match=rf"execution_control\.ssh\.{field_name}"):
                ExecutionSSHSettings.model_validate({field_name: bad_value})


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
        "fd00:ec2::254",
        "[fd00:ec2::254]",
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


def test_network_policy_checks_resolved_ips_special_ranges_and_scoped_ipv6() -> None:
    def _never_resolve(host: str, port: int) -> tuple[str, ...]:
        pytest.fail(f"DNS resolver must not be called for refused host {host!r}:{port}")

    policy = SSHNetworkPolicy.default()
    # C16: localhost refused before DNS when allowed_cidrs is empty.
    with pytest.raises(SSHNetworkPolicyError):
        validate_ssh_host("localhost", 22, policy, resolver=_never_resolve)
    with pytest.raises(SSHNetworkPolicyError):
        validate_ssh_host("sub.localhost", 22, policy, resolver=_never_resolve)
    with pytest.raises(SSHNetworkPolicyError):
        validate_ssh_host(
            "rebind.example.com",
            22,
            policy,
            resolver=lambda _h, _p: ("203.0.113.10", "169.254.169.254"),
        )

    # C18: Even with denied_cidrs cleared, special ranges including IPv6 metadata stay blocked.
    open_policy = SSHNetworkPolicy(allowed_cidrs=(), allowed_domains=(), denied_cidrs=())
    for special in ("127.0.0.1", "0.0.0.0", "::", "169.254.169.254", "fd00:ec2::254", "224.0.0.1"):
        with pytest.raises(SSHNetworkPolicyError):
            validate_ssh_host(special, 22, open_policy)

    # C19: Explicit operator opt-in permits 127.0.0.1/32 and switches other hosts to allowlist-only.
    explicit_loopback = SSHNetworkPolicy.from_settings(
        ExecutionSSHSettings.model_validate(
            {"denied_cidrs": [], "allowed_cidrs": ["127.0.0.1/32"]}
        )
    )
    validated = validate_ssh_host("127.0.0.1", 2222, explicit_loopback)
    assert validated.resolved_ip == "127.0.0.1"
    assert validated.host_key_alias == "[127.0.0.1]:2222"
    with pytest.raises(SSHNetworkPolicyError):
        validate_ssh_host("203.0.113.20", 22, explicit_loopback)

    # C20: Explicitly allowed link-local literal preserves its IPv6 interface scope_id.
    explicit_link_local = SSHNetworkPolicy.from_settings(
        ExecutionSSHSettings.model_validate(
            {"denied_cidrs": [], "allowed_cidrs": ["fe80::/10"]}
        )
    )
    scoped = validate_ssh_host(
        "fe80::1%eth0",
        22,
        explicit_link_local,
        local_address_checker=lambda _ip: False,
    )
    assert scoped.resolved_ip == "fe80::1%eth0"
    assert scoped.host == "fe80::1%eth0"


def test_network_policy_refuses_unmatched_hostnames_before_dns_and_pins_either_list_rule() -> None:
    def _fail_resolver(host: str, port: int) -> tuple[str, ...]:
        pytest.fail(f"DNS lookup must not run before domain allowlist check for {host!r}:{port}")

    # Domain-only allowlist refuses unrelated domains AND IP literals without resolving.
    domain_only = SSHNetworkPolicy.from_settings(
        ExecutionSSHSettings.model_validate({"allowed_domains": ["ops.example.com"]})
    )
    with pytest.raises(SSHNetworkPolicyError):
        validate_ssh_host("exfil.attacker.example", 22, domain_only, resolver=_fail_resolver)
    with pytest.raises(SSHNetworkPolicyError):
        validate_ssh_host("203.0.113.10", 22, domain_only, resolver=_fail_resolver)

    # Combined allowlists (C16 + C19):
    # - Unmatched hostnames are refused BEFORE DNS even when allowed_cidrs is non-empty.
    # - Matched allowed_domains hostnames pass even when resolving outside allowed_cidrs (either-list).
    # - IP literals are checked against allowed_cidrs.
    combined = SSHNetworkPolicy.from_settings(
        ExecutionSSHSettings.model_validate(
            {
                "allowed_domains": ["ops.example.com"],
                "allowed_cidrs": ["100.64.0.0/10"],
                "denied_cidrs": [*DEFAULT_SSH_DENIED_CIDRS, "100.64.99.0/24"],
            }
        )
    )
    with pytest.raises(SSHNetworkPolicyError):
        validate_ssh_host("evilops.example.com", 22, combined, resolver=_fail_resolver)

    ok_domain_outside_cidr = validate_ssh_host(
        "box1.ops.example.com",
        22,
        combined,
        resolver=lambda _h, _p: ("2001:db8::10", "192.168.10.20"),
        local_address_checker=lambda _ip: False,
    )
    assert ok_domain_outside_cidr.host == "box1.ops.example.com"
    assert ok_domain_outside_cidr.resolved_ip == "2001:db8::10"
    assert ok_domain_outside_cidr.resolved_ips == ("2001:db8::10", "192.168.10.20")
    fallback_host = ok_domain_outside_cidr.with_resolved_ip("192.168.10.20")
    assert fallback_host.resolved_ip == "192.168.10.20"
    assert fallback_host.host_key_alias == "box1.ops.example.com"
    with pytest.raises(ValueError):
        ok_domain_outside_cidr.with_resolved_ip("203.0.113.99")

    ok_cidr = validate_ssh_host(
        "100.64.1.5",
        2222,
        combined,
        local_address_checker=lambda _ip: False,
    )
    assert ok_cidr.resolved_ip == "100.64.1.5"
    assert ok_cidr.host_key_alias == "[100.64.1.5]:2222"

    # Denied CIDR overrides both allowed_cidrs and allowed_domains.
    with pytest.raises(SSHNetworkPolicyError):
        validate_ssh_host("100.64.99.5", 22, combined, local_address_checker=lambda _ip: False)
    with pytest.raises(SSHNetworkPolicyError):
        validate_ssh_host(
            "box2.ops.example.com",
            22,
            combined,
            resolver=lambda _h, _p: ("100.64.99.5",),
            local_address_checker=lambda _ip: False,
        )

    # DNS failure and option-injection hostnames fail closed.
    def _dns_fail(_h: str, _p: int) -> tuple[str, ...]:
        raise socket.gaierror("Name or service not known")

    with pytest.raises(SSHNetworkPolicyError):
        validate_ssh_host("missing.ops.example.com", 22, combined, resolver=_dns_fail)
    with pytest.raises(SSHNetworkPolicyError):
        validate_ssh_host("-oProxyCommand=id", 22, combined, resolver=_fail_resolver)
    with pytest.raises(SSHNetworkPolicyError):
        validate_ssh_host("box1.ops.example.com", 0, combined, resolver=_fail_resolver)


def test_network_policy_refuses_this_machines_own_non_loopback_address() -> None:
    local_ip = _first_local_non_loopback_ip()
    if local_ip is None:
        pytest.skip("Test machine has no non-loopback network address assigned.")

    policy = SSHNetworkPolicy.default()
    with pytest.raises(SSHNetworkPolicyError, match="assigned to this machine"):
        validate_ssh_host(local_ip, 22, policy)
    with pytest.raises(SSHNetworkPolicyError, match="assigned to this machine"):
        validate_ssh_host(
            "self-host.example.test",
            22,
            policy,
            resolver=lambda _h, _p: (local_ip,),
        )

    # Explicitly allowing this machine's CIDR permits it.
    explicit = SSHNetworkPolicy.from_settings(
        ExecutionSSHSettings.model_validate(
            {"allowed_cidrs": [f"{local_ip}/32" if ":" not in local_ip else f"{local_ip}/128"]}
        )
    )
    allowed = validate_ssh_host(local_ip, 22, explicit)
    assert allowed.resolved_ip == local_ip


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
    initial_known_hosts_bytes = f"{alias} {algorithm} {encoded_key}\n".encode("utf-8")
    known_hosts.write_bytes(initial_known_hosts_bytes)

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

    # Unpinned operator-configured path keeps CheckHostIP=yes.
    unpinned_argv = broker._ssh_argv(session, material)
    assert "CheckHostIP=yes" in unpinned_argv
    assert "CheckHostIP=no" not in unpinned_argv

    # C15 + C20: Pinned path takes ValidatedSSHHost and replaces CheckHostIP=yes with CheckHostIP=no.
    # Using ::ffff:127.0.0.1 exercises OpenSSH's non-loopback CheckHostIP code path.
    validated = ValidatedSSHHost(
        host="ops.internal",
        port=port,
        resolved_ip="::ffff:127.0.0.1",
        resolved_ips=("::ffff:127.0.0.1",),
        host_key_alias=alias,
    )
    with pytest.raises(ValueError, match="port"):
        broker._ssh_argv(
            session,
            material,
            validated_host=ValidatedSSHHost(
                host="ops.internal",
                port=port + 1,
                resolved_ip="127.0.0.1",
                resolved_ips=("127.0.0.1",),
                host_key_alias=ssh_host_key_alias("ops.internal", port + 1),
            ),
        )

    argv = list(broker._ssh_argv(session, material, validated_host=validated))
    assert "CheckHostIP=no" in argv
    assert "CheckHostIP=yes" not in argv
    assert "HostName=::ffff:127.0.0.1" in argv
    assert f"HostKeyAlias={alias}" in argv
    argv[argv.index("-tt")] = "-T"
    argv.append("echo pinned-ok")

    # C15: Refusal case when known_hosts holds the right key under a different name.
    wrong_name_known_hosts = tmp_path / "wrong_known_hosts"
    wrong_name_known_hosts.write_text(
        f"[other.internal]:{port} {algorithm} {encoded_key}\n",
        encoding="utf-8",
    )
    wrong_material = SSHCredentialMaterial(
        identity_file=client_key,
        known_hosts_file=wrong_name_known_hosts,
    )
    wrong_argv = list(broker._ssh_argv(session, wrong_material, validated_host=validated))
    wrong_argv[wrong_argv.index("-tt")] = "-T"
    wrong_argv.append("echo should-not-run")

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
        assert "Permanently added" not in result.stderr
        assert known_hosts.read_bytes() == initial_known_hosts_bytes

        wrong_result = subprocess.run(
            wrong_argv,
            capture_output=True,
            text=True,
            timeout=10,
            check=False,
        )
        assert wrong_result.returncode != 0
        assert "Host key verification failed" in wrong_result.stderr
        assert "should-not-run" not in wrong_result.stdout
    finally:
        sshd.terminate()
        try:
            sshd.wait(timeout=5)
        except subprocess.TimeoutExpired:
            sshd.kill()
            sshd.wait(timeout=5)
