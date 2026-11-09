"""Host and authority keys in the instance's ``trust/`` folder (embedded profile).

Every process that sends protocol 1 frames is a host with its own Ed25519
application key, which signs its frames and records, and a NATS user key, which a
join presents (README "Installation traffic"). The installation authority has its
own key, which signs certificates and revocations. The launcher creates any key that
is missing before it starts the processes (``ensure_*``); each process then loads only
its own (``load_*``). Private keys never leave these files: no message, database row
or log line carries them.

Files are written whole and atomically, readable only by their owner, in owner-only
folders, and created exclusively (``private_files``), so two processes starting at once
can never each keep a different key for one host. A key object never prints its secret
parts (``repr``). Every process runs as the same operating-system user inside one
container, so this keeps keys apart by path, not by permission: a broken-into process
could read another host's key file. The authorization plan records this residual risk;
remote hosts (message contract phase 5) keep their keys on their own machines.
"""

from __future__ import annotations

import hashlib
import json
import secrets
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey, Ed25519PublicKey
from cryptography.hazmat.primitives.serialization import Encoding, NoEncryption, PrivateFormat, PublicFormat

from custom_messages.wire.encoding import b64url, b64url_decode
from custom_transport.protocol1 import nkeys
from custom_transport.protocol1.private_files import NotAPrivateFile, read_private_file, write_private_file
from runtime_settings.instance_layout import InstanceLayout

HOST_KEYS_FILENAME = "keys.json"
AUTHORITY_KEY_FILENAME = "authority-key.json"
_KEY_FILE_VERSION = 1


class KeyFileError(RuntimeError):
    """A key file is missing, damaged, or readable by others."""


def _public_bytes(public_key: Ed25519PublicKey) -> bytes:
    return public_key.public_bytes(Encoding.Raw, PublicFormat.Raw)


def _private_seed(private_key: Ed25519PrivateKey) -> bytes:
    return private_key.private_bytes(Encoding.Raw, PrivateFormat.Raw, NoEncryption())


def key_id_for(owner: str, public_key: Ed25519PublicKey) -> str:
    """A key id that names its owner and differs for every key the owner ever has.

    Key ids hold no ``_`` (contract ``key_id``), which runtime and installation ids
    may; the owner part is only for people reading logs, and the key's digest keeps
    every id distinct.
    """

    return f"{owner.replace('_', '-')}.{hashlib.sha256(_public_bytes(public_key)).hexdigest()[:16]}"


@dataclass(frozen=True)
class HostKeys:
    """One host's keys: its application key and NATS user key."""

    runtime: str
    key_id: str
    application_key: Ed25519PrivateKey = field(repr=False)
    nats_seed: bytes = field(repr=False)

    @property
    def public_key(self) -> Ed25519PublicKey:
        return self.application_key.public_key()

    @property
    def public_key_text(self) -> str:
        """The application key's public key in unpadded base64url, as joins and certificates carry it."""

        return b64url(_public_bytes(self.public_key))

    @property
    def nats_public_key(self) -> str:
        """The NATS user public key (``U...``) a join presents."""

        return nkeys.user_public_key(_public_bytes(Ed25519PrivateKey.from_private_bytes(self.nats_seed).public_key()))


@dataclass(frozen=True)
class AuthorityKeys:
    """The installation authority's key and the installation it speaks for."""

    installation: str
    key_id: str
    private_key: Ed25519PrivateKey = field(repr=False)

    @property
    def public_key_text(self) -> str:
        return b64url(_public_bytes(self.private_key.public_key()))


def _write_exclusively(path: Path, document: dict[str, Any]) -> bool:
    """Publish a complete 0600 key file only if none exists; return whether this call wrote it."""

    content = (json.dumps(document, indent=2, sort_keys=True) + "\n").encode("utf-8")
    created = write_private_file(path, content, exclusive=True)
    # The key's own folder is closed even when something else made it first.
    path.parent.chmod(0o700)
    return created


def _read_private(path: Path) -> dict[str, Any]:
    try:
        raw = read_private_file(path)
    except FileNotFoundError as error:
        raise KeyFileError(f"Key file {path} is missing; the launcher creates it before starting this process.") from error
    except NotAPrivateFile as error:
        raise KeyFileError(f"Key file {path} must be a regular file readable only by its owner (mode 0600): {error}.") from error
    except OSError as error:
        raise KeyFileError(f"Key file {path} cannot be opened: {error}") from error
    try:
        document = json.loads(raw)
    except ValueError as error:
        raise KeyFileError(f"Key file {path} is damaged: {error}") from error
    if not isinstance(document, dict) or document.get("version") != _KEY_FILE_VERSION:
        raise KeyFileError(f"Key file {path} has an unknown format.")
    return document


def _host_keys_path(layout: InstanceLayout, runtime: str) -> Path:
    return layout.host_trust_dir(runtime) / HOST_KEYS_FILENAME


def ensure_host_keys(layout: InstanceLayout, runtime: str) -> tuple[HostKeys, bool]:
    """The host's keys, created when missing; returns them and whether this call created them.

    Only the launcher calls this, before it starts the host's process.
    """

    path = _host_keys_path(layout, runtime)
    if not path.exists():
        application = Ed25519PrivateKey.generate()
        document = {
            "version": _KEY_FILE_VERSION,
            "runtime": runtime,
            "application_key": {"key_id": key_id_for(runtime, application.public_key()),
                                "seed": b64url(_private_seed(application))},
            "nats_key": {"seed": nkeys.user_seed(secrets.token_bytes(32))},
        }
        created = _write_exclusively(path, document)
        return load_host_keys(layout, runtime), created
    return load_host_keys(layout, runtime), False


def load_host_keys(layout: InstanceLayout, runtime: str) -> HostKeys:
    """This host's keys; refuses a missing, damaged or widely readable file."""

    path = _host_keys_path(layout, runtime)
    document = _read_private(path)
    try:
        if document["runtime"] != runtime:
            raise KeyFileError(f"Key file {path} belongs to {document['runtime']!r}, not {runtime!r}.")
        application = Ed25519PrivateKey.from_private_bytes(b64url_decode(document["application_key"]["seed"]))
        key_id = str(document["application_key"]["key_id"])
        if key_id != key_id_for(runtime, application.public_key()):
            raise KeyFileError(f"Key file {path} names another key id than its key gives.")
        nats_seed = nkeys.seed_bytes(str(document["nats_key"]["seed"]))
    except (KeyError, TypeError, ValueError) as error:
        raise KeyFileError(f"Key file {path} is damaged: {error}") from error
    return HostKeys(runtime=runtime, key_id=key_id, application_key=application, nats_seed=nats_seed)


def ensure_authority_keys(layout: InstanceLayout, installation: str) -> tuple[AuthorityKeys, bool]:
    """The installation authority's key, created when missing; only the launcher calls this.

    An existing key of another installation is refused, not returned: the bundle and
    every certificate would name the wrong installation.
    """

    path = layout.trust_authority_dir / AUTHORITY_KEY_FILENAME
    created = False
    if not path.exists():
        private_key = Ed25519PrivateKey.generate()
        document = {
            "version": _KEY_FILE_VERSION,
            "installation": installation,
            "key_id": key_id_for(f"{installation}-authority", private_key.public_key()),
            "seed": b64url(_private_seed(private_key)),
        }
        created = _write_exclusively(path, document)
    authority = load_authority_keys(layout)
    if authority.installation != installation:
        raise KeyFileError(f"Key file {path} belongs to installation {authority.installation!r}, not {installation!r}.")
    return authority, created


def load_authority_keys(layout: InstanceLayout) -> AuthorityKeys:
    """The authority's key; only the installation authority process reads it."""

    path = layout.trust_authority_dir / AUTHORITY_KEY_FILENAME
    document = _read_private(path)
    try:
        private_key = Ed25519PrivateKey.from_private_bytes(b64url_decode(document["seed"]))
        installation = str(document["installation"])
        key_id = str(document["key_id"])
        if key_id != key_id_for(f"{installation}-authority", private_key.public_key()):
            raise KeyFileError(f"Key file {path} names another key id than its key gives.")
    except (KeyError, TypeError, ValueError) as error:
        raise KeyFileError(f"Key file {path} is damaged: {error}") from error
    return AuthorityKeys(installation=installation, key_id=key_id, private_key=private_key)
