"""Keys from the transfer passphrase: one authenticates the file, one encrypts credentials.

PyNaCl (libsodium) is already a direct dependency, so no new package is
needed:

- Argon2id (``nacl.pwhash.argon2id``) turns the passphrase and a random salt
  into a 32-byte master key. The salt and cost parameters are stored in the
  manifest so the importer derives the same key.
- Two subkeys come from the master key with keyed BLAKE2b and distinct
  ``person`` labels, so the key that authenticates the manifest is never the
  key that encrypts credentials.
- The manifest MAC is keyed BLAKE2b over the canonical JSON of the manifest
  without its ``mac`` field; it is compared in constant time.
- Credentials are encrypted with ``nacl.secret.SecretBox`` (XSalsa20-Poly1305,
  random nonce per message). The broker encrypts and decrypts its own
  sections with the encryption subkey, so values never pass through the
  command-line process in the clear.

Nothing here logs or returns a passphrase or key.
"""

from __future__ import annotations

import base64
import hmac
import json
from dataclasses import dataclass, field
from typing import Any, Final

from nacl import pwhash, secret, utils
from nacl.encoding import RawEncoder
from nacl.exceptions import CryptoError
from nacl.hash import blake2b

from team_transfer.format import KeyDerivationV1, TransferManifestV1

# A passphrase this short is guessable offline from a stolen file.
MIN_PASSPHRASE_CHARS: Final = 12
KEY_BYTES: Final = 32
_MAC_LABEL: Final = b"ua-xfer-mac-v1"
_ENCRYPTION_LABEL: Final = b"ua-xfer-enc-v1"
# libsodium's "moderate" profile: about a second and 256 MiB on a server.
DEFAULT_OPSLIMIT: Final = pwhash.argon2id.OPSLIMIT_MODERATE
DEFAULT_MEMLIMIT: Final = pwhash.argon2id.MEMLIMIT_MODERATE


class TransferKeyError(ValueError):
    """The passphrase is unusable or does not match the file."""


@dataclass(frozen=True)
class TransferKeys:
    """The two subkeys of one transfer file. ``repr`` never shows them."""

    mac_key: bytes = field(repr=False)
    encryption_key: bytes = field(repr=False)


def new_key_derivation(*, opslimit: int | None = None, memlimit: int | None = None) -> KeyDerivationV1:
    """A fresh random salt with this module's cost (read at call time, so tests can lower it)."""

    salt = utils.random(pwhash.argon2id.SALTBYTES)
    return KeyDerivationV1(
        salt_b64=base64.b64encode(salt).decode("ascii"),
        opslimit=DEFAULT_OPSLIMIT if opslimit is None else opslimit,
        memlimit=DEFAULT_MEMLIMIT if memlimit is None else memlimit,
    )


def check_passphrase(passphrase: str) -> None:
    if len(passphrase) < MIN_PASSPHRASE_CHARS:
        raise TransferKeyError(
            f"Use a passphrase of at least {MIN_PASSPHRASE_CHARS} characters; the file holds people's credentials."
        )


def derive_keys(passphrase: str, derivation: KeyDerivationV1) -> TransferKeys:
    try:
        salt = base64.b64decode(derivation.salt_b64, validate=True)
    except ValueError as exc:
        raise TransferKeyError("The file's key salt is not valid.") from exc
    if len(salt) != pwhash.argon2id.SALTBYTES:
        raise TransferKeyError("The file's key salt has the wrong length.")
    master = pwhash.argon2id.kdf(
        KEY_BYTES,
        passphrase.encode("utf-8"),
        salt,
        opslimit=derivation.opslimit,
        memlimit=derivation.memlimit,
    )
    return TransferKeys(mac_key=_subkey(master, _MAC_LABEL), encryption_key=_subkey(master, _ENCRYPTION_LABEL))


def _subkey(master: bytes, label: bytes) -> bytes:
    return blake2b(b"", digest_size=KEY_BYTES, key=master, person=label.ljust(16, b"\0"), encoder=RawEncoder)


def canonical_json(value: Any) -> bytes:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False).encode("utf-8")


def manifest_mac(manifest: TransferManifestV1, keys: TransferKeys) -> str:
    return blake2b(canonical_json(manifest.unsigned()), digest_size=32, key=keys.mac_key).decode("ascii")


def verify_manifest(manifest: TransferManifestV1, keys: TransferKeys) -> None:
    if not hmac.compare_digest(manifest.mac, manifest_mac(manifest, keys)):
        raise TransferKeyError(
            "The passphrase is wrong, or the file was changed after it was exported. Nothing was imported."
        )


def encrypt(plaintext: bytes, keys: TransferKeys) -> bytes:
    return encrypt_with_key(plaintext, keys.encryption_key)


def decrypt(ciphertext: bytes, keys: TransferKeys) -> bytes:
    return decrypt_with_key(ciphertext, keys.encryption_key)


def encrypt_with_key(plaintext: bytes, encryption_key: bytes) -> bytes:
    """Encrypt with the encryption subkey alone (the broker gets only this key, never the passphrase)."""

    if len(encryption_key) != KEY_BYTES:
        raise TransferKeyError("The encryption key has the wrong length.")
    return bytes(secret.SecretBox(encryption_key).encrypt(plaintext))


def decrypt_with_key(ciphertext: bytes, encryption_key: bytes) -> bytes:
    if len(encryption_key) != KEY_BYTES:
        raise TransferKeyError("The encryption key has the wrong length.")
    try:
        return bytes(secret.SecretBox(encryption_key).decrypt(ciphertext))
    except CryptoError as exc:
        raise TransferKeyError("An encrypted section could not be decrypted with this passphrase.") from exc


__all__ = [
    "DEFAULT_MEMLIMIT",
    "DEFAULT_OPSLIMIT",
    "MIN_PASSPHRASE_CHARS",
    "TransferKeyError",
    "TransferKeys",
    "canonical_json",
    "check_passphrase",
    "decrypt",
    "decrypt_with_key",
    "derive_keys",
    "encrypt",
    "encrypt_with_key",
    "manifest_mac",
    "new_key_derivation",
    "verify_manifest",
]
