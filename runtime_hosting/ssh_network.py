"""Operator network policy validation and IP pinning helpers for brokered SSH."""

from __future__ import annotations

from collections.abc import Callable, Sequence
from dataclasses import dataclass
import ipaddress
import re
import socket
from typing import TYPE_CHECKING

from default_values import DEFAULT_SSH_DENIED_CIDRS

if TYPE_CHECKING:
    from runtime_settings.execution import ExecutionSSHSettings

IPAddress = ipaddress.IPv4Address | ipaddress.IPv6Address
IPNetwork = ipaddress.IPv4Network | ipaddress.IPv6Network
SSHHostResolver = Callable[[str, int], Sequence[str]]

_HOST_LABEL_RE = re.compile(r"^[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?$")


class SSHNetworkPolicyError(PermissionError):
    """Raised when an SSH target host violates the operator network policy."""


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


def ssh_host_key_alias(host: str, port: int) -> str:
    """Return the OpenSSH known_hosts / HostKeyAlias entry name for host and port."""

    return host if port == 22 else f"[{host}]:{port}"


def default_ssh_host_resolver(host: str, port: int) -> tuple[str, ...]:
    """Resolve an SSH hostname to deduplicated IP strings via system getaddrinfo."""

    infos = socket.getaddrinfo(host, port, type=socket.SOCK_STREAM)
    resolved: list[str] = []
    seen: set[str] = set()
    for _family, _socktype, _proto, _canonname, sockaddr in infos:
        address = str(sockaddr[0])
        if address not in seen:
            seen.add(address)
            resolved.append(address)
    return tuple(resolved)


def validate_ssh_host(
    host: str,
    port: int,
    policy: SSHNetworkPolicy,
    *,
    resolver: SSHHostResolver | None = None,
) -> ValidatedSSHHost:
    """Validate a host and port against operator SSH network policy.

    Always evaluates resolved IP addresses (never only typed hostname text),
    refuses denied CIDRs unconditionally, refuses special ranges (loopback,
    link-local, unspecified, multicast, including IPv4-mapped IPv6 forms)
    unless explicitly allowed in ``policy.allowed_cidrs``, and returns the
    validated IP and ``HostKeyAlias`` for DNS-rebinding-safe pinning.
    """

    if isinstance(port, bool) or not isinstance(port, int) or not (1 <= port <= 65535):
        raise SSHNetworkPolicyError("SSH target port must be an integer between 1 and 65535.")

    normalized_host, ip_literal = _normalize_host(host)
    if ip_literal is not None:
        resolved_ips: tuple[IPAddress, ...] = (ip_literal,)
        is_ip_literal = True
    else:
        resolved_ips = _resolve_host_ips(
            normalized_host,
            port,
            resolver=resolver or default_ssh_host_resolver,
        )
        is_ip_literal = False

    for ip in resolved_ips:
        if _ip_in_networks(ip, policy.denied_cidrs):
            raise SSHNetworkPolicyError(
                "SSH target resolves to an address blocked by execution_control.ssh.denied_cidrs."
            )
        if _is_special_address(ip) and not _ip_in_networks(ip, policy.allowed_cidrs):
            raise SSHNetworkPolicyError(
                "SSH target resolves to a loopback, link-local, unspecified, or multicast "
                "address that is not explicitly allowed in execution_control.ssh.allowed_cidrs."
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

    if policy.allowed_cidrs or policy.allowed_domains:
        domain_allowed = (not is_ip_literal) and any(
            normalized_host == domain or normalized_host.endswith(f".{domain}")
            for domain in policy.allowed_domains
        )
        cidrs_allowed = bool(policy.allowed_cidrs) and all(
            _ip_in_networks(ip, policy.allowed_cidrs) for ip in resolved_ips
        )
        if not (domain_allowed or cidrs_allowed):
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
        return _canonical_ip_text(parsed_v6), parsed_v6

    ip_literal = _parse_ip_literal(candidate)
    if ip_literal is not None:
        return _canonical_ip_text(ip_literal), ip_literal

    lowered = candidate.lower()
    if lowered.endswith("."):
        lowered = lowered[:-1]
    if not lowered or len(lowered) > 253:
        raise SSHNetworkPolicyError("SSH target hostname is invalid.")
    labels = lowered.split(".")
    if any(not _HOST_LABEL_RE.match(label) for label in labels):
        raise SSHNetworkPolicyError("SSH target hostname is invalid.")
    return lowered, None


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
        raise SSHNetworkPolicyError(f"Could not resolve SSH target host {host!r}.") from exc
    if not raw_addresses:
        raise SSHNetworkPolicyError(f"SSH target host {host!r} resolved to no IP addresses.")
    resolved: list[IPAddress] = []
    seen: set[str] = set()
    for raw in raw_addresses:
        try:
            parsed = ipaddress.ip_address(str(raw).strip())
        except ValueError as exc:
            raise SSHNetworkPolicyError(
                f"DNS resolver returned invalid IP address for SSH target host {host!r}."
            ) from exc
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
        return str(ipaddress.IPv6Address(ip.packed))
    return str(ip)


def _ip_in_networks(ip: IPAddress, networks: tuple[IPNetwork, ...]) -> bool:
    candidates = _candidate_addresses(ip)
    return any(
        candidate.version == network.version and candidate in network
        for candidate in candidates
        for network in networks
    )


def _is_special_address(ip: IPAddress) -> bool:
    return any(
        candidate.is_loopback
        or candidate.is_link_local
        or candidate.is_unspecified
        or candidate.is_multicast
        for candidate in _candidate_addresses(ip)
    )


__all__ = [
    "SSHHostResolver",
    "SSHNetworkPolicy",
    "SSHNetworkPolicyError",
    "ValidatedSSHHost",
    "default_ssh_host_resolver",
    "ssh_host_key_alias",
    "validate_ssh_host",
]
