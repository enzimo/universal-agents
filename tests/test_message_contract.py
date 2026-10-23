"""The protocol 1 message contract and its Python implementation agree.

Sources are in contracts/messages/v1; ``scripts/export_message_contract.py``
generates the rest. These tests check that the generated files are current, that
schemas stay within the documented subset, that every valid example is accepted
(and every invalid one refused with its named code), and that the signing vectors
re-sign to the same bytes.
"""

from __future__ import annotations

import asyncio
import importlib.util
import json
import logging
import os
import shutil
import socket
from datetime import datetime
from pathlib import Path

import pytest
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey, Ed25519PublicKey
from cryptography.hazmat.primitives.serialization import Encoding, PublicFormat
from jsonschema import Draft202012Validator
from referencing import Registry, Resource

from custom_messages.wire import canonical, frame as wire_frame, kinds, models
from custom_messages.wire.canonical import CanonicalJsonError
from custom_messages.wire.frame import (
    FrameRefused,
    Receiver,
    TrustedKey,
    b64url,
    b64url_decode,
    body_model,
    sign_frame,
    signing_bytes,
    verify_frame,
)

ROOT = Path(__file__).resolve().parents[1]
CONTRACT = ROOT / "contracts" / "messages" / "v1"
EXAMPLES = CONTRACT / "examples"
SCHEMA_BASE = "https://universal-agents.local/contracts/messages/v1/schemas/"


def _exporter():
    spec = importlib.util.spec_from_file_location("export_message_contract", ROOT / "scripts" / "export_message_contract.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


EXPORT = _exporter()
REGISTRY = json.loads((CONTRACT / "generated" / "registry.json").read_text(encoding="utf-8"))
KEYS = json.loads((EXAMPLES / "keys.json").read_text(encoding="utf-8"))["keys"]


def schema_registry() -> Registry:
    resources = []
    for path in (CONTRACT / "schemas").rglob("*.schema.json"):
        schema = json.loads(path.read_text(encoding="utf-8"))
        resources.append((schema["$id"], Resource.from_contents(schema)))
    return Registry().with_resources(resources)


SCHEMAS = schema_registry()


def validator(schema_file: str) -> Draft202012Validator:
    return Draft202012Validator({"$ref": SCHEMA_BASE + schema_file}, registry=SCHEMAS)


def body_schema_file(kind: str) -> str:
    if kind in REGISTRY["kinds"]:
        return REGISTRY["kinds"][kind]["body"]
    return REGISTRY["kinds"][kind.removesuffix(".reply")]["reply"]["body"]


def trusted_keys(**context) -> dict[str, TrustedKey]:
    revoked_at = context.get("revoked_at")
    return {
        key_id: TrustedKey(
            public_key=Ed25519PublicKey.from_public_bytes(b64url_decode(spec["public_key"])),
            team=spec["team"],
            runtime=spec["runtime"],
            revoked_at=wire_frame.parse_timestamp(revoked_at) if revoked_at else None,
            min_membership_epoch=context.get("min_membership_epoch", 0),
        )
        for key_id, spec in KEYS.items()
    }


def replay_record_holding(digest: str):
    """A replay record that already holds the frame's key with ``digest``."""

    return lambda _runtime, _epoch, _message_id, _digest: digest


def valid_examples() -> list[Path]:
    return sorted((EXAMPLES / "valid").rglob("*.json"))


def invalid_examples() -> list[Path]:
    return sorted((EXAMPLES / "invalid").glob("*.json"))


def task_request_key(frame: dict) -> str:
    """Resolve the registry's frame and body placeholders for a dispatch."""

    rule = REGISTRY["kinds"]["task.request"]["idempotency"]
    return rule.replace("<task_id>", frame["body"]["task_id"]).replace("<message_id>", frame["message_id"])


# -- generated files and the schema subset -----------------------------------------------------------

def test_generated_files_are_current() -> None:
    assert EXPORT.stale(EXPORT.render()) == []


@pytest.mark.parametrize(
    ("schema", "problem"),
    [
        ({"type": "object", "properties": {"a": {"type": "string"}}}, "additionalProperties false"),
        ({"type": "number", "minimum": 0, "maximum": 1}, "outside the subset"),
        ({"type": "integer", "minimum": 0}, "within"),
        ({"type": "integer", "minimum": 0, "maximum": 2**53}, "within"),
        ({"type": "string", "pattern": "^(?=a)a$"}, "not portable"),
        ({"type": "string", "pattern": "a+"}, "anchored"),
        ({"type": "string", "format": "uuid"}, "outside the subset"),
        ({"$ref": "https://example.com/x.json"}, "local"),
        ({"oneOf": [{"type": "object", "properties": {"a": {"type": "string"}}, "additionalProperties": False}]}, "const"),
    ],
)
def test_the_schema_subset_refuses_what_other_implementations_read_differently(schema, problem) -> None:
    with pytest.raises(EXPORT.ContractError, match=problem):
        EXPORT.check_subset("test.schema.json", schema, "")


def test_every_kind_has_a_model_and_every_model_a_kind() -> None:
    assert set(kinds.KINDS) == set(REGISTRY["kinds"])
    adapters = {name.removesuffix("BodyAdapter") for name in dir(models) if name.endswith("BodyAdapter")}
    assert adapters == {EXPORT.class_name(kind) for kind in REGISTRY["kinds"]}
    for name, spec in kinds.KINDS.items():
        entry = REGISTRY["kinds"][name]
        assert (spec.reply_model is not None) == ("reply" in entry)
        assert spec.reply_errors == tuple(entry.get("reply", {}).get("errors", ()))
        assert (spec.delivery_class, spec.addresses, spec.expiry_seconds) == (
            entry["class"], tuple(EXPORT.entry_addresses(entry)), entry["expiry_seconds"])
    assert kinds.VERIFICATION_REFUSALS == tuple(item["code"] for item in REGISTRY["verification_refusals"])


def test_permissions_publish_only_under_the_senders_own_id() -> None:
    folder = CONTRACT / "generated" / "permissions"
    for role in REGISTRY["host_roles"]:
        document = json.loads((folder / f"{role}.json").read_text(encoding="utf-8"))
        for subject in document["publish"]:
            assert subject.startswith(("ua.v1.{team}.{self}.", "_INBOX.")), (role, subject)
        assert "_INBOX.{self}.>" in document["subscribe"]
        for name, entry in REGISTRY["kinds"].items():
            if role in entry["senders"]:
                for address in EXPORT.entry_addresses(entry):
                    assert "ua.v1.{team}.{self}." + EXPORT.subject_pattern(address) in document["publish"]


def test_asyncapi_describes_both_operations_for_every_address() -> None:
    document = json.loads((CONTRACT / "generated" / "asyncapi.json").read_text(encoding="utf-8"))
    operations = document["operations"]
    for channel, spec in document["channels"].items():
        actions = {
            operation["action"]
            for operation in operations.values()
            if operation["channel"]["$ref"] == f"#/channels/{channel}"
        }
        assert actions == {"send", "receive"}, spec["address"]
    for name, entry in REGISTRY["kinds"].items():
        if "reply" not in entry:
            continue
        message = name.replace(".", "_")
        for operation in operations.values():
            channel = document["channels"][operation["channel"]["$ref"].rsplit("/", 1)[1]]
            if operation["action"] == "send" and message in channel["messages"]:
                assert operation["reply"]["messages"] == [{"$ref": f"#/components/messages/{message}_reply"}]


def test_reassignment_preserves_the_task_but_changes_the_dispatch_key() -> None:
    original = json.loads((EXAMPLES / "valid" / "task.request" / "to-pool.json").read_text(encoding="utf-8"))
    replacement = json.loads((EXAMPLES / "valid" / "task.request" / "reassignment.json").read_text(encoding="utf-8"))
    assert replacement["body"]["task_id"] == original["body"]["task_id"]
    assert task_request_key(replacement) != task_request_key(original)


@pytest.mark.asyncio
@pytest.mark.integration
@pytest.mark.live
@pytest.mark.nats
@pytest.mark.skipif(shutil.which("nats-server") is None, reason="nats-server binary is not installed")
async def test_jetstream_delivers_reassignment_and_deduplicates_its_retry(tmp_path: Path, monkeypatch) -> None:
    """A handback cannot lose its replacement dispatch to the first send's replay record."""

    import nats
    from nats.js.api import AckPolicy, ConsumerConfig, RetentionPolicy, StorageType, StreamConfig

    import docker.nats_service as nats_service

    with socket.socket() as client_port, socket.socket() as monitor_port:
        client_port.bind(("127.0.0.1", 0))
        monitor_port.bind(("127.0.0.1", 0))
        port = client_port.getsockname()[1]
        monkeypatch.setattr(nats_service, "INTERNAL_NATS_PORT", port)
        monkeypatch.setattr(nats_service, "INTERNAL_NATS_MONITOR_PORT", monitor_port.getsockname()[1])
    env = {"PATH": os.environ["PATH"]}
    process = nats_service.start_local_nats(
        env=env,
        logger=logging.getLogger(__name__),
        cwd=tmp_path,
        service_root=tmp_path / "nats",
    )
    client = None
    try:
        async with asyncio.timeout(10):
            client = await nats.connect(
                f"nats://127.0.0.1:{port}",
                user=env["NATS_USER"],
                password=env["NATS_PASSWORD"],
                allow_reconnect=False,
            )
            js = client.jetstream()
            transport = json.loads((CONTRACT / "generated" / "transport.json").read_text(encoding="utf-8"))
            stream = transport["streams"]["UA1_WORK"]
            await js.add_stream(config=StreamConfig(
                name="UA1_WORK",
                subjects=stream["subjects"],
                retention=RetentionPolicy.WORK_QUEUE,
                storage=StorageType.FILE,
                duplicate_window=stream["duplicate_window_seconds"],
            ))
            original = json.loads((EXAMPLES / "valid" / "task.request" / "to-pool.json").read_text(encoding="utf-8"))
            replacement = json.loads((EXAMPLES / "valid" / "task.request" / "reassignment.json").read_text(encoding="utf-8"))
            subscription = await js.pull_subscribe(
                original["subject"], durable="pool", config=ConsumerConfig(ack_policy=AckPolicy.EXPLICIT),
            )

            async def publish(frame):
                return await js.publish(
                    frame["subject"], canonical.canonical_bytes(frame),
                    headers={"Nats-Msg-Id": task_request_key(frame)},
                )

            first = await publish(original)
            assert not first.duplicate
            admitted = (await subscription.fetch(1, timeout=2))[0]
            await admitted.ack_sync()

            second = await publish(replacement)
            assert not second.duplicate
            assert second.seq > first.seq
            retry = await publish(replacement)
            assert retry.duplicate
            assert retry.seq == second.seq
            received = (await subscription.fetch(1, timeout=2))[0]
            assert canonical.parse(received.data) == replacement
            await received.ack_sync()
            assert (await js.stream_info("UA1_WORK")).state.messages == 0
    finally:
        if client is not None:
            await client.close()
        process.terminate()
        process.wait(timeout=10)


# -- canonical form ---------------------------------------------------------------------------------

VECTORS = json.loads((CONTRACT / "signing-vectors" / "vectors.json").read_text(encoding="utf-8"))


@pytest.mark.parametrize("case", VECTORS["canonical"], ids=lambda case: case["name"])
def test_canonical_form(case) -> None:
    assert canonical.canonical_bytes(canonical.parse(case["input"])).decode("utf-8") == case["canonical"]


@pytest.mark.parametrize("case", VECTORS["canonical_refused"], ids=lambda case: case["name"])
def test_canonical_form_refuses(case) -> None:
    with pytest.raises(CanonicalJsonError):
        canonical.parse(case["input"])


# -- signing vectors --------------------------------------------------------------------------------

@pytest.mark.parametrize("vector", VECTORS["frames"], ids=lambda vector: vector["name"])
def test_signing_vectors_re_sign_to_the_same_bytes(vector) -> None:
    key = Ed25519PrivateKey.from_private_bytes(b64url_decode(vector["seed"]))
    assert b64url(key.public_key().public_bytes(Encoding.Raw, PublicFormat.Raw)) == vector["public_key"]
    assert signing_bytes(vector["frame"]).hex() == vector["signing_bytes_hex"]
    assert wire_frame.body_digest(vector["frame"]["body"]) == vector["body_digest"]
    resigned = sign_frame(vector["frame"], key)
    assert resigned["signature"] == vector["signature"]
    assert resigned == vector["frame"]


# -- examples ----------------------------------------------------------------------------------------

@pytest.mark.parametrize("path", valid_examples(), ids=lambda path: path.relative_to(EXAMPLES / "valid").as_posix())
def test_valid_examples_are_accepted(path: Path) -> None:
    data = path.read_bytes()
    document = json.loads(data)
    newer_minor = document["protocol"] != kinds.PROTOCOL
    if not newer_minor:
        validator("frame.schema.json").validate(document)
        validator(body_schema_file(document["kind"])).validate(document["body"])
    verified = verify_frame(data, keys=trusted_keys(), arrived_on=document["subject"],
                            receiver=Receiver(**document["audience"]) if "runtime" not in document["audience"] else None,
                            now=datetime(2026, 10, 22, 4, 30, 1, tzinfo=wire_frame.timezone.utc))
    assert verified.frame.kind == path.parent.name
    model, _shape = body_model(document["kind"])
    dumped = model.dump_python(verified.body, mode="json", exclude_none=True, by_alias=True)
    if newer_minor:
        assert set(dumped) < set(document["body"]), "a newer minor's unknown fields were not dropped"
    else:
        assert dumped == document["body"], "the body did not survive a round trip"
    if document["kind"] == "task.request":
        assert document["idempotency_key"] == task_request_key(document)


@pytest.mark.parametrize("path", invalid_examples(), ids=lambda path: path.stem)
def test_invalid_examples_are_refused_with_their_code(path: Path) -> None:
    case = json.loads(path.read_text(encoding="utf-8"))
    assert case["refusal"] in kinds.VERIFICATION_REFUSALS
    data = case["frame_text"].encode("utf-8")
    if "pad_to_bytes" in case:
        data += b" " * (case["pad_to_bytes"] - len(data))
    context = case["context"]
    replay = replay_record_holding(context["replay"]["digest"]) if "replay" in context else None
    with pytest.raises(FrameRefused) as refused:
        verify_frame(
            data,
            keys=trusted_keys(**context),
            arrived_on=context.get("arrived_on"),
            receiver=Receiver(**context["receiver"]) if "receiver" in context else None,
            now=wire_frame.parse_timestamp(context["now"]) if "now" in context else None,
            replay=replay,
        )
    assert refused.value.code == case["refusal"], refused.value


@pytest.mark.parametrize("path", [p for p in invalid_examples() if json.loads(p.read_text())["refusal"] == "invalid_body"],
                         ids=lambda path: path.stem)
def test_invalid_bodies_fail_their_schema_where_named(path: Path) -> None:
    case = json.loads(path.read_text(encoding="utf-8"))
    document = json.loads(case["frame_text"])
    errors = list(validator(body_schema_file(document["kind"])).iter_errors(document["body"]))
    assert errors
    locations = {"/body" + "".join(f"/{part}" for part in error.absolute_path) for error in errors}
    nested = {"/body" + "".join(f"/{part}" for part in sub.absolute_path) for error in errors for sub in error.context or ()}
    assert case["at"] in locations | nested, (locations, nested)


def test_every_kind_and_reply_has_a_valid_example() -> None:
    examples = {path.parent.name for path in valid_examples()}
    expected = set(REGISTRY["kinds"]) | {f"{name}.reply" for name, entry in REGISTRY["kinds"].items() if "reply" in entry}
    assert expected <= examples, sorted(expected - examples)
