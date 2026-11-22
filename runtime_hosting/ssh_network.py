"""Operator network policy validation and IP pinning helpers for brokered SSH."""

from __future__ import annotations

from collections.abc import Callable, Sequence
from dataclasses import dataclass
import ipaddress
from pathlib import Path
import re
import socket
import sys
from typing import TYPE_CHECKING

from default_values import DEFAULT_SSH_DENIED_CIDRS

if TYPE_CHECKING:
    from runtime_settings.execution import ExecutionSSHSettings

IPAddress = ipaddress.IPv4Address | ipaddress.IPv6Address
IPNetwork = ipaddress.IPv4Network | ipaddress.IPv6Network
SSHHostResolver = Callable[[str, int], Sequence[str]]
LocalAddressChecker = Callable[[IPAddress], bool]

_HOST_LABEL_RE = re.compile(r"^[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?$")
_IPV6_SCOPE_ID_RE = re.compile(r"^[A-Za-z0-9_.-]{1,64}$")

# Cloud instance metadata addresses outside 169.254.0.0/16 that remain blocked
# in code even if an existing instance_config.yaml carries an older denied_cidrs list.
_ALWAYS_BLOCKED_METADATA_NETWORKS: tuple[IPNetwork, ...] = (
    ipaddress.ip_network("fd00:ec2::254/128"),
)


class SSHNetworkPolicyError(PermissionError):
    """Raised when an SSH target host violates the operator network policy."""


class SSHHostResolutionError(SSHNetworkPolicyError):
    """Raised when DNS resolution fails or returns no addresses for an SSH target host."""


@dataclass(frozen=True)
class SSHNetworkPolicy:
    """Compiled operator network policy for SSH probing and dynamic targets."""

    allowed_cidrs: tuple[IPNetwork, ...] = ()
    allowed_domains: tuple[str, ...] = ()
    denied_cidrs: tuple[IPNetwork, ...] = tuple(
        ipaddress.ip_network(cidr, strict=False) for cidr in DEFAULT_SSH_DENIED_CIDRS
    )

    @classmethod
    def default(cls) -> "SSHNetworkPolicy":
        return cls()

    @classmethod
    def from_settings(cls, settings: "ExecutionSSHSettings") -> "SSHNetworkPolicy":
        return cls(
            allowed_cidrs=tuple(
                ipaddress.ip_network(cidr, strict=False)
                for cidr in settings.allowed_cidrs
            ),
            allowed_domains=tuple(settings.allowed_domains),
            denied_cidrs=tuple(
                ipaddress.ip_network(cidr, strict=False)
                for cidr in settings.denied_cidrs
            ),
        )


@dataclass(frozen=True)
class ValidatedSSHHost:
    """Validated SSH target host with resolved IP(s) and OpenSSH HostKeyAlias."""

    host: str
    port: int
    resolved_ip: str
    resolved_ips: tuple[str, ...]
    host_key_alias: str

    def with_resolved_ip(self, ip: str) -> "ValidatedSSHHost":
        """Return a copy pinned to another validated address from ``resolved_ips``."""

        if ip not in self.resolved_ips:
            raise ValueError(f"Address {ip!r} is not in this target's validated addresses.")
        return ValidatedSSHHost(
            host=self.host,
            port=self.port,
            resolved_ip=ip,
            resolved_ips=self.resolved_ips,
            host_key_alias=self.host_key_alias,
        )


def ssh_host_key_alias(host: str, port: int) -> str:
    """Return the OpenSSH known_hosts / HostKeyAlias entry name for host and port."""

    return host if port == 22 else f"[{host}]:{port}"


def normalize_ssh_hostname(raw_host: str) -> str:
    """Validate and normalize an SSH hostname or domain (lowercase, no trailing dot).

    Shared by both runtime settings validation (``allowed_domains``) and
    ``validate_ssh_host`` so domain matching rules cannot drift apart.
    """

    if not isinstance(raw_host, str):
        raise ValueError("SSH hostname must be a non-empty string.")
    candidate = raw_host.strip().lower()
    if candidate.endswith("."):
        candidate = candidate[:-1]
    if not candidate or len(candidate) > 253:
        raise ValueError(f"Invalid SSH hostname {raw_host!r}.")
    if candidate.startswith("-") or any(ord(ch) <= 32 or ord(ch) == 127 for ch in candidate):
        raise ValueError(f"Invalid SSH hostname {raw_host!r}.")
    labels = candidate.split(".")
    if any(not _HOST_LABEL_RE.match(label) for label in labels):
        raise ValueError(f"Invalid SSH hostname {raw_host!r}.")
    return candidate


def default_ssh_host_resolver(host: str, port: int) -> tuple[str, ...]:
    """Resolve an SSH hostname to deduplicated IP strings via system getaddrinfo.

    Prefers ``AI_ADDRCONFIG`` so machines without IPv6 routing receive routable
    addresses first, falling back to unfiltered resolution when ``AI_ADDRCONFIG``
    returns nothing (for example loopback-only setups).
    """

    infos: list[tuple[socket.AddressFamily, socket.SocketKind, int, str, tuple[object, ...]]] = []
    if hasattr(socket, "AI_ADDRCONFIG"):
        try:
            infos = list(
                socket.getaddrinfo(
                    host,
                    port,
                    type=socket.SOCK_STREAM,
                    flags=socket.AI_ADDRCONFIG,
                )
            )
        except OSError:
            infos = []
    if not infos:
        infos = list(socket.getaddrinfo(host, port, type=socket.SOCK_STREAM))
    resolved: list[str] = []
    seen: set[str] = set()
    for _family, _socktype, _proto, _canonname, sockaddr in infos:
        address = str(sockaddr[0])
        if address not in seen:
            seen.add(address)
            resolved.append(address)
    return tuple(resolved)


def _ipv_nonlocal_bind_enabled(family: socket.AddressFamily) -> bool:
    """Return True on Linux when the kernel allows binding arbitrary non-local IPs."""

    if sys.platform != "linux":
        return False
    sysctl_path = (
        Path("/proc/sys/net/ipv6/ip_nonlocal_bind")
        if family == socket.AF_INET6
        else Path("/proc/sys/net/ipv4/ip_nonlocal_bind")
    )
    try:
        return sysctl_path.read_text(encoding="utf-8").strip() == "1"
    except OSError:
        return False


def is_local_machine_address(ip: IPAddress) -> bool:
    """Return True if ``ip`` (or its IPv4-mapped form) is assigned to this machine.

    Uses a cheap ephemeral UDP bind on port 0, which succeeds only for addresses
    assigned to a local interface (unless Linux ``ip_nonlocal_bind`` is enabled).
    """

    for candidate in _candidate_addresses(ip):
        family = (
            socket.AF_INET6
            if isinstance(candidate, ipaddress.IPv6Address)
            else socket.AF_INET
        )
        if _ipv_nonlocal_bind_enabled(family):
            continue
        try:
            with socket.socket(family, socket.SOCK_DGRAM) as sock:
                sock.bind((str(candidate), 0))
            return True
        except OSError:
            continue
    return False


def _domain_matches(hostname: str, allowed_domains: tuple[str, ...]) -> bool:
    return any(
        hostname == domain or hostname.endswith(f".{domain}")
        for domain in allowed_domains
    )


def validate_ssh_host(
    host: str,
    port: int,
    policy: SSHNetworkPolicy,
    *,
    resolver: SSHHostResolver | None = None,
    local_address_checker: LocalAddressChecker | None = None,
) -> ValidatedSSHHost:
    """Validate a host and port against operator SSH network policy.

    Evaluation order prevents DNS exfiltration and DNS rebinding:
    1. Reject invalid ports/host syntax without resolving.
    2. When ``allowed_domains`` is non-empty, refuse any hostname outside
       ``allowed_domains`` *before* calling DNS; IP literals are judged by
       ``allowed_cidrs`` alone.
    3. Refuse ``localhost`` / ``*.localhost`` before calling DNS when
       ``allowed_cidrs`` is empty.
    4. Resolve hostnames and validate every resolved IP against ``denied_cidrs``,
       built-in special/metadata ranges, and this machine's own addresses
       (all refused unless explicitly covered by ``allowed_cidrs``).
    5. Return a ``ValidatedSSHHost`` carrying the validated IP(s) and exact
       OpenSSH ``HostKeyAlias`` for DNS-rebinding-safe pinning.
    """

    if isinstance(port, bool) or not isinstance(port, int) or not (1 <= port <= 65535):
        raise SSHNetworkPolicyError("SSH target port must be an integer between 1 and 65535.")

    normalized_host, ip_literal = _normalize_host(host)
    is_ip_literal = ip_literal is not None
    allowlist_active = bool(policy.allowed_cidrs or policy.allowed_domains)

    # Decide by host/name before any DNS lookup wherever the name alone settles it.
    if is_ip_literal:
        if allowlist_active and not policy.allowed_cidrs:
            raise SSHNetworkPolicyError(
                "SSH target IP literal is not permitted when only execution_control.ssh.allowed_domains is set."
            )
    else:
        if (
            normalized_host == "localhost" or normalized_host.endswith(".localhost")
        ) and not policy.allowed_cidrs:
            raise SSHNetworkPolicyError(
                "SSH target hostname localhost is refused unless its resolved address is explicitly allowed."
            )
        if policy.allowed_domains and not _domain_matches(
            normalized_host, policy.allowed_domains
        ):
            raise SSHNetworkPolicyError(
                "SSH target hostname is not permitted by execution_control.ssh.allowed_domains."
            )

    if ip_literal is not None:
        resolved_ips: tuple[IPAddress, ...] = (ip_literal,)
    else:
        resolved_ips = _resolve_host_ips(
            normalized_host,
            port,
            resolver=resolver or default_ssh_host_resolver,
        )

    check_local = local_address_checker or is_local_machine_address
    for ip in resolved_ips:
        if _ip_in_networks(ip, policy.denied_cidrs):
            raise SSHNetworkPolicyError(
                "SSH target resolves to an address blocked by execution_control.ssh.denied_cidrs."
            )
        if _is_special_address(ip) and not _ip_in_networks(ip, policy.allowed_cidrs):
            raise SSHNetworkPolicyError(
                "SSH target resolves to a loopback, link-local, unspecified, cloud metadata, "
                "or multicast address that is not explicitly allowed in execution_control.ssh.allowed_cidrs."
            )
        if check_local(ip) and not _ip_in_networks(ip, policy.allowed_cidrs):
            raise SSHNetworkPolicyError(
                "SSH target resolves to an address assigned to this machine, which is refused "
                "unless explicitly allowed in execution_control.ssh.allowed_cidrs."
            )

    if (
        not is_ip_literal
        and (normalized_host == "localhost" or normalized_host.endswith(".localhost"))
        and not (
            policy.allowed_cidrs
            and all(_ip_in_networks(ip, policy.allowed_cidrs) for ip in resolved_ips)
        )
    ):
        raise SSHNetworkPolicyError(
            "SSH target hostname localhost is refused unless its resolved address is explicitly allowed."
        )

    if allowlist_active:
        if is_ip_literal:
            if not all(_ip_in_networks(ip, policy.allowed_cidrs) for ip in resolved_ips):
                raise SSHNetworkPolicyError(
                    "SSH target is not permitted by execution_control.ssh.allowed_cidrs."
                )
        elif not policy.allowed_domains:
            # Only allowed_cidrs is configured; hostnames must resolve inside allowed_cidrs.
            if not all(_ip_in_networks(ip, policy.allowed_cidrs) for ip in resolved_ips):
                raise SSHNetworkPolicyError(
                    "SSH target is not permitted by execution_control.ssh.allowed_cidrs "
                    "or execution_control.ssh.allowed_domains."
                )

    canonical_ips = tuple(_canonical_ip_text(ip) for ip in resolved_ips)
    canonical_host = canonical_ips[0] if is_ip_literal else normalized_host
    return ValidatedSSHHost(
        host=canonical_host,
        port=port,
        resolved_ip=canonical_ips[0],
        resolved_ips=canonical_ips,
        host_key_alias=ssh_host_key_alias(canonical_host, port),
    )


def _normalize_host(raw_host: str) -> tuple[str, IPAddress | None]:
    if not isinstance(raw_host, str):
        raise SSHNetworkPolicyError("SSH target host must be a non-empty string.")
    candidate = raw_host.strip()
    if not candidate or len(candidate) > 255:
        raise SSHNetworkPolicyError("SSH target host must be between 1 and 255 characters.")
    if candidate.startswith("-") or any(ord(ch) <= 32 or ord(ch) == 127 for ch in candidate):
        raise SSHNetworkPolicyError("SSH target host contains disallowed characters.")

    if candidate.startswith("[") or candidate.endswith("]"):
        if not (candidate.startswith("[") and candidate.endswith("]")):
            raise SSHNetworkPolicyError("SSH target host has mismatched brackets.")
        inner = candidate[1:-1]
        try:
            parsed_v6 = ipaddress.ip_address(inner)
        except ValueError as exc:
            raise SSHNetworkPolicyError("Bracketed SSH target host must be a valid IPv6 address.") from exc
        if not isinstance(parsed_v6, ipaddress.IPv6Address):
            raise SSHNetworkPolicyError("Bracketed SSH target host must be an IPv6 address.")
        _validate_ipv6_scope_id(parsed_v6)
        return _canonical_ip_text(parsed_v6), parsed_v6

    ip_literal = _parse_ip_literal(candidate)
    if ip_literal is not None:
        if isinstance(ip_literal, ipaddress.IPv6Address):
            _validate_ipv6_scope_id(ip_literal)
        return _canonical_ip_text(ip_literal), ip_literal

    try:
        normalized = normalize_ssh_hostname(candidate)
    except ValueError as exc:
        raise SSHNetworkPolicyError("SSH target hostname is invalid.") from exc
    return normalized, None


def _validate_ipv6_scope_id(ip: ipaddress.IPv6Address) -> None:
    scope_id = ip.scope_id
    if scope_id is not None and not _IPV6_SCOPE_ID_RE.fullmatch(scope_id):
        raise SSHNetworkPolicyError("SSH target IPv6 zone identifier is invalid.")


def _parse_ip_literal(text: str) -> IPAddress | None:
    try:
        return ipaddress.ip_address(text)
    except ValueError:
        pass
    # Catch legacy numeric IPv4 notations (e.g. 127.1, 0, 2130706433, 0x7f000001)
    # that glibc/BSD getaddrinfo accept as IPv4 literals.
    try:
        packed = socket.inet_aton(text)
    except OSError:
        return None
    return ipaddress.IPv4Address(packed)


def _resolve_host_ips(
    host: str,
    port: int,
    *,
    resolver: SSHHostResolver,
) -> tuple[IPAddress, ...]:
    try:
        raw_addresses = resolver(host, port)
    except OSError as exc:
        raise SSHHostResolutionError(f"Could not resolve SSH target host {host!r}.") from exc
    if not raw_addresses:
        raise SSHHostResolutionError(f"SSH target host {host!r} resolved to no IP addresses.")
    resolved: list[IPAddress] = []
    seen: set[str] = set()
    for raw in raw_addresses:
        try:
            parsed = ipaddress.ip_address(str(raw).strip())
        except ValueError as exc:
            raise SSHNetworkPolicyError(
                f"DNS resolver returned invalid IP address for SSH target host {host!r}."
            ) from exc
        if isinstance(parsed, ipaddress.IPv6Address):
            _validate_ipv6_scope_id(parsed)
        key = _canonical_ip_text(parsed)
        if key not in seen:
            seen.add(key)
            resolved.append(parsed)
    return tuple(resolved)


def _candidate_addresses(ip: IPAddress) -> tuple[IPAddress, ...]:
    if isinstance(ip, ipaddress.IPv6Address):
        # Strip any IPv6 zone identifier so network containment matches fe80::/10, etc.
        base = ipaddress.IPv6Address(ip.packed)
        if base.ipv4_mapped is not None:
            return (base, base.ipv4_mapped)
        return (base,)
    return (ip,)


def _canonical_ip_text(ip: IPAddress) -> str:
    if isinstance(ip, ipaddress.IPv6Address):
        base = str(ipaddress.IPv6Address(ip.packed))
        if ip.scope_id:
            return f"{base}%{ip.scope_id}"
        return base
    return str(ip)


def _ip_in_networks(ip: IPAddress, networks: tuple[IPNetwork, ...]) -> bool:
    candidates = _candidate_addresses(ip)
    return any(
        candidate.version == network.version and candidate in network
        for candidate in candidates
        for network in networks
    )


def _is_special_address(ip: IPAddress) -> bool:
    if _ip_in_networks(ip, _ALWAYS_BLOCKED_METADATA_NETWORKS):
        return True
    return any(
        candidate.is_loopback
        or candidate.is_link_local
        or candidate.is_unspecified
        or candidate.is_multicast
        for candidate in _candidate_addresses(ip)
    )


__all__ = [
    "LocalAddressChecker",
    "SSHHostResolutionError",
    "SSHHostResolver",
    "SSHNetworkPolicy",
    "SSHNetworkPolicyError",
    "ValidatedSSHHost",
    "default_ssh_host_resolver",
    "is_local_machine_address",
    "normalize_ssh_hostname",
    "ssh_host_key_alias",
    "validate_ssh_host",
]
