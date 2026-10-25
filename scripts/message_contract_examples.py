"""Sign the protocol 1 examples, trust fixtures and signing vectors.

Under contracts/messages/v1, these are written by hand without their derived
values:
- ``examples/keys.json``: each key's public key comes from its seed;
- ``examples/trust/``: state records (certificates, revocations, agent sessions,
  agreements) and the receiving installation's authority keys;
- ``examples/valid/``: complete frames;
- ``examples/invalid/``: a refusal case built from a valid example and listed
  changes (README, "Examples");
- ``signing-vectors/vectors.json``: frames, a token and a record with their exact
  signing bytes.

This module fills in public keys, idempotency keys, body digests, signatures and
each invalid example's ``frame_text``. Tokens are signed wherever a frame carries
them: its authority token, an authority token in its body (``body.authority``, the
scheduler's answer to ``run_context.sign``) and a change-conflict decision token
(``body.decision_token``). It signs and derives keys with RFC 8785,
SHA-256 and Ed25519 directly, not with ``custom_messages.wire``, so the tests
compare two implementations of the signing and key rules. ``scripts/export_message_contract.py`` runs it before writing its manifest;
``--check`` there fails when a file here is not what this module renders.
"""

from __future__ import annotations

import base64
import copy
import hashlib
import json
from pathlib import Path
from typing import Any, Callable

import rfc8785
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from cryptography.hazmat.primitives.serialization import Encoding, PublicFormat

CONTRACT = Path(__file__).resolve().parents[1] / "contracts" / "messages" / "v1"
EXAMPLES = CONTRACT / "examples"
VECTORS = CONTRACT / "signing-vectors" / "vectors.json"
# Fields of an invalid example; frame_text is the one this module writes.
INVALID_FIELDS = {"description", "refusal", "also", "at", "base", "changes", "signing", "signed_by", "token_signing",
                  "after_signing", "text_changes", "pad_to_bytes", "context", "frame_text"}
SIGNING_MODES = {"after", "before", "stale_digest"}
BASE64URL = "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789-_"


class ExampleError(ValueError):
    pass


def b64url(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode("ascii")


def _decode(text: str) -> bytes:
    return base64.urlsafe_b64decode(text + "=" * (-len(text) % 4))


def document_text(document: Any) -> str:
    return json.dumps(document, indent=2, ensure_ascii=False) + "\n"


def _steps(pointer: str) -> list[str]:
    if not pointer.startswith("/"):
        raise ExampleError(f"{pointer!r} is not a JSON pointer")
    return [step.replace("~1", "/").replace("~0", "~") for step in pointer[1:].split("/")]


def _parent(document: Any, pointer: str) -> tuple[Any, str]:
    *path, last = _steps(pointer)
    for step in path:
        document = document[int(step)] if isinstance(document, list) else document[step]
    return document, last


def _apply(document: dict[str, Any], change: dict[str, Any]) -> None:
    """One listed change: ``{"set": pointer, "to": value}`` or ``{"remove": pointer}``."""

    if "set" in change:
        parent, last = _parent(document, change["set"])
        parent[int(last) if isinstance(parent, list) else last] = copy.deepcopy(change["to"])
    elif "remove" in change:
        parent, last = _parent(document, change["remove"])
        del parent[int(last) if isinstance(parent, list) else last]
    else:
        raise ExampleError(f"unknown change {change}")


def _respell(text: str) -> str:
    """The same bytes spelled differently: flip an unused low bit of the last character."""

    unused = (6 * len(text)) % 8
    if not unused:
        raise ExampleError("this length has no unused bits")
    return text[:-1] + BASE64URL[BASE64URL.index(text[-1]) ^ 1]


def _pointed(document: dict[str, Any], pointer: str) -> Any:
    value: Any = document
    for step in pointer[1:].split("/"):
        if not isinstance(value, dict) or step not in value:
            return None
        value = value[step]
    return value


class Signer:
    def __init__(self, keys: dict[str, dict[str, Any]], registry: dict[str, Any]) -> None:
        self.keys = keys
        self.kinds = registry["kinds"]
        # Requests by message id, so a reply's key can come from its request's key.
        self.requests: dict[str, dict[str, Any]] = {}
        self.frame_domain = registry["signing_domain"]
        self.state_domain = registry["state_signing_domain"]
        self.token_domain = registry["authority_token_domain"]
        self.decision_domain = registry["decision_token_domain"]

    def private_key(self, key_id: str) -> Ed25519PrivateKey:
        if key_id not in self.keys:
            raise ExampleError(f"no test key {key_id}")
        return Ed25519PrivateKey.from_private_bytes(_decode(self.keys[key_id]["seed"]))

    def public_key(self, key_id: str) -> str:
        return b64url(self.private_key(key_id).public_key().public_bytes(Encoding.Raw, PublicFormat.Raw))

    @staticmethod
    def signing_bytes(domain: str, document: dict[str, Any]) -> bytes:
        return domain.encode("ascii") + b"\n" + rfc8785.dumps({k: v for k, v in document.items() if k != "signature"})

    def sign(self, domain: str, document: dict[str, Any], key_id: str) -> str:
        return b64url(self.private_key(key_id).sign(self.signing_bytes(domain, document)))

    @staticmethod
    def digest(body: Any) -> str:
        return b64url(hashlib.sha256(rfc8785.dumps(body)).digest())

    def idempotency_key(self, frame: dict[str, Any]) -> str:
        """The key the registry's parts give (README, "Idempotency keys"); unknown kinds keep theirs."""

        kind = frame["kind"]
        if kind.endswith(".reply") and kind.removesuffix(".reply") in self.kinds:
            request = self.requests.get(frame.get("causation_id", ""))
            if request is None:
                return str(frame["idempotency_key"])  # a reply without its request is refused before its key matters
            parts = [request["idempotency_key"]]
        elif kind in self.kinds:
            entry = self.kinds[kind]
            parts = [_pointed(frame, pointer) for pointer in entry["idempotency"]]
            copies = {"team_services" for address in entry.get("copy_to", []) if address == "work.team"}
            if frame["audience"]["target"] in copies:
                parts.append(frame["audience"]["target"])
        else:
            return str(frame["idempotency_key"])
        digest = hashlib.sha256(rfc8785.dumps(parts)).digest()
        return f"{kind}:{b64url(digest)}"

    def token(self, token: dict[str, Any]) -> dict[str, Any]:
        token = dict(token, algorithm="ed25519")
        token["signature"] = self.sign(self.token_domain, token, token["issuer"]["key_id"])
        return token

    def frame(self, frame: dict[str, Any], *, key_id: str | None = None, digest: str | None = None,
              sign_token: bool = True) -> dict[str, Any]:
        """Sign a frame: its tokens first (the frame's and those its body carries), then the body digest, then the frame."""

        frame = dict(frame)
        if "authority" in frame and sign_token:
            frame["authority"] = self.token(frame["authority"])
        body = frame.get("body")
        if isinstance(body, dict) and isinstance(body.get("authority"), dict) and sign_token:
            body = frame["body"] = dict(body, authority=self.token(body["authority"]))
        if isinstance(body, dict) and isinstance(body.get("decision_token"), dict) and sign_token:
            decision = dict(body["decision_token"], algorithm="ed25519")
            decision["signature"] = self.sign(self.decision_domain, decision, decision["issuer"]["key_id"])
            frame["body"] = dict(body, decision_token=decision)
        frame["algorithm"] = "ed25519"
        frame["body_digest"] = digest or self.digest(frame["body"])
        frame["signature"] = self.sign(self.frame_domain, frame, key_id or frame["issuer"]["key_id"])
        return frame

    def state(self, document: dict[str, Any]) -> dict[str, Any]:
        document = dict(document, algorithm="ed25519")
        document["body"] = self.fill_public_keys(document["body"])
        document["body_digest"] = self.digest(document["body"])
        document["signature"] = self.sign(self.state_domain, document, document["writer"]["key_id"])
        return document

    def fill_public_keys(self, value: Any) -> Any:
        """Every ``{"key_id", "public_key"}`` pair names a test key; its public key comes from the seed."""

        if isinstance(value, list):
            return [self.fill_public_keys(item) for item in value]
        if not isinstance(value, dict):
            return value
        filled = {key: self.fill_public_keys(item) for key, item in value.items()}
        if set(filled) >= {"key_id", "public_key"} and filled["key_id"] in self.keys:
            filled["public_key"] = self.public_key(filled["key_id"])
        return filled


def _read(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8"))


def render_keys() -> tuple[dict[str, dict[str, Any]], dict[Path, str]]:
    path = EXAMPLES / "keys.json"
    document = _read(path)
    for spec in document["keys"].values():
        seed = Ed25519PrivateKey.from_private_bytes(_decode(spec["seed"]))
        spec["public_key"] = b64url(seed.public_key().public_bytes(Encoding.Raw, PublicFormat.Raw))
    return document["keys"], {path: document_text(document)}


def render_trust(signer: Signer) -> dict[Path, str]:
    files = {}
    for path in sorted((EXAMPLES / "trust").rglob("*.json")):
        document = _read(path)
        if path.name == "anchors.json":
            files[path] = document_text(signer.fill_public_keys(document))
        else:
            files[path] = document_text(signer.state(document))
    return files


def render_valid(signer: Signer) -> tuple[dict[str, dict[str, Any]], dict[Path, str]]:
    """Requests first, so each reply's key can name its request's key."""

    documents = {path: _read(path) for path in sorted((EXAMPLES / "valid").rglob("*.json"))}
    ordered = sorted(documents, key=lambda path: (documents[path]["kind"].endswith(".reply"), path))
    frames, files = {}, {}
    for path in ordered:
        document = documents[path]
        document["idempotency_key"] = signer.idempotency_key(document)
        frame = signer.frame(document)
        signer.requests.setdefault(frame["message_id"], frame)
        frames[path.relative_to(EXAMPLES / "valid").as_posix()] = frame
        files[path] = document_text(frame)
    return frames, files


def invalid_frame_text(case: dict[str, Any], frames: dict[str, dict[str, Any]], signer: Signer) -> str:
    """The frame an invalid example describes: its base, changed and signed as it says."""

    unknown = set(case) - INVALID_FIELDS
    mode = case.get("signing", "after")
    if unknown or mode not in SIGNING_MODES or case["base"] not in frames:
        raise ExampleError(f"unknown fields {sorted(unknown)}, signing {mode!r} or base {case['base']!r}")
    frame = copy.deepcopy(frames[case["base"]])
    for change in case.get("changes", []):
        _apply(frame, change)
    if not any(change.get("set", change.get("remove")) == "/idempotency_key" for change in case.get("changes", [])):
        frame["idempotency_key"] = signer.idempotency_key(frame)
    if mode != "before":
        frame = signer.frame(frame, key_id=case.get("signed_by"),
                             digest=frame["body_digest"] if mode == "stale_digest" else None,
                             sign_token=case.get("token_signing", "after") == "after")
    for change in case.get("after_signing", []):
        parent, last = _parent(frame, change["respell"])
        parent[last] = _respell(parent[last])
    text = json.dumps(frame, ensure_ascii=False)
    for edit in case.get("text_changes", []):
        if edit["replace"] not in text:
            raise ExampleError(f"{edit['replace']!r} is not in the frame text")
        text = text.replace(edit["replace"], edit["with"], 1)
    return text


def render_invalid(signer: Signer, frames: dict[str, dict[str, Any]]) -> dict[Path, str]:
    files = {}
    for path in sorted((EXAMPLES / "invalid").glob("*.json")):
        case = _read(path)
        try:
            case["frame_text"] = invalid_frame_text(case, frames, signer)
        except (ExampleError, KeyError, IndexError, ValueError) as error:
            raise ExampleError(f"{path.name}: {error}") from error
        files[path] = document_text(case)
    return files


def render_vectors(signer: Signer, registry: dict[str, Any]) -> dict[Path, str]:
    document = _read(VECTORS)
    document["domain_prefixes"] = {kind: registry[name] + "\n" for kind, name in (
        ("frame", "signing_domain"), ("state", "state_signing_domain"), ("authority_token", "authority_token_domain"))}
    document.pop("domain_prefix", None)

    def vector(entry: dict[str, Any], field: str, domain: str, sign: Callable[[dict[str, Any]], dict[str, Any]]) -> None:
        entry["public_key"] = signer.public_key(entry["key_id"])
        entry["seed"] = signer.keys[entry["key_id"]]["seed"]
        signed = sign(entry[field])
        entry[field] = signed
        entry["signing_bytes_hex"] = signer.signing_bytes(domain, signed).hex()
        if "body_digest" in signed:
            entry["body_digest"] = signed["body_digest"]
        entry["signature"] = signed["signature"]

    for entry in document["frames"]:
        key_id = entry["key_id"]
        entry["frame"]["idempotency_key"] = signer.idempotency_key(entry["frame"])

        def sign_frame(frame: dict[str, Any], key_id: str = key_id) -> dict[str, Any]:
            return signer.frame(frame, key_id=key_id)

        vector(entry, "frame", signer.frame_domain, sign_frame)
    for entry in document.get("authority_tokens", []):
        vector(entry, "token", signer.token_domain, signer.token)
    for entry in document.get("states", []):
        vector(entry, "state", signer.state_domain, signer.state)
    return {VECTORS: document_text(document)}


def render_examples(registry: dict[str, Any]) -> dict[Path, str]:
    """Every example, trust fixture and vector file, with its derived values filled in."""

    keys, files = render_keys()
    signer = Signer(keys, registry)
    files.update(render_trust(signer))
    frames, valid = render_valid(signer)
    files.update(valid)
    files.update(render_invalid(signer, frames))
    files.update(render_vectors(signer, registry))
    return files
