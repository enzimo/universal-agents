"""The protocol 1 message contract and its Python implementation agree.

Sources are in contracts/messages/v1; ``scripts/export_message_contract.py``
generates the rest. These tests check that the generated files are current, that
schemas stay within the documented subset, that every valid example is accepted
(and every invalid one refused with its named code), and that the signing vectors
re-sign to the same bytes.
"""

from __future__ import annotations

import copy
import importlib.util
import json
from datetime import datetime, timedelta
from pathlib import Path

import pytest
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey, Ed25519PublicKey
from cryptography.hazmat.primitives.serialization import Encoding, PublicFormat
from jsonschema import Draft202012Validator
from pydantic import ValidationError
from referencing import Registry, Resource
from referencing.jsonschema import DRAFT202012

from custom_messages.wire import canonical, frame as wire_frame, kinds, models
from custom_messages.wire.canonical import CanonicalJsonError
from custom_messages.wire.frame import (
    FrameRefused,
    Receiver,
    TrustedKey,
    b64url,
    b64url_decode,
    body_model,
    idempotency_key,
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
        ({"type": "array", "items": {"type": "string"}, "uniqueItems": True}, "outside the subset"),
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
        assert (spec.delivery_class, spec.address, spec.expiry_seconds) == (entry["class"], entry["address"], entry["expiry_seconds"])
    assert kinds.VERIFICATION_REFUSALS == tuple(item["code"] for item in REGISTRY["verification_refusals"])


def test_permissions_publish_only_under_the_senders_own_id() -> None:
    folder = CONTRACT / "generated" / "permissions"
    for role in REGISTRY["host_roles"]:
        document = json.loads((folder / f"{role}.json").read_text(encoding="utf-8"))
        for subject in document["publish"]:
            assert subject.startswith(("ua.v1.{team}.{self}.", "_INBOX.", "$JS.API.CONSUMER.", "$JS.ACK.")), (role, subject)
            if subject.startswith("$JS."):
                assert "*" not in subject
                assert "agent_{team}_{agent}" in subject
        assert "_INBOX.{self}.>" in document["subscribe"]
        for name, entry in REGISTRY["kinds"].items():
            if role in entry["senders"]:
                assert "ua.v1.{team}.{self}." + EXPORT.subject_pattern(entry["address"]) in document["publish"]


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
                            receiver=Receiver(**document["audience"]),
                            now=datetime(2026, 10, 22, 4, 30, 1, tzinfo=wire_frame.timezone.utc))
    assert verified.frame.kind == path.parent.name
    model, _shape = body_model(document["kind"])
    dumped = model.dump_python(verified.body, mode="json", exclude_none=True, by_alias=True)
    if newer_minor:
        assert set(dumped) < set(document["body"]), "a newer minor's unknown fields were not dropped"
    else:
        assert dumped == document["body"], "the body did not survive a round trip"


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


def signed_example(kind: str = "task.stop") -> dict:
    path = EXAMPLES / "valid" / kind / ("paused.json" if kind.endswith(".reply") else "basic.json")
    return json.loads(path.read_text())


def resigned(frame: dict) -> bytes:
    seed = KEYS[frame["issuer"]["key_id"]]["seed"]
    private_key = Ed25519PrivateKey.from_private_bytes(b64url_decode(seed))
    return json.dumps(sign_frame(frame, private_key)).encode()


@pytest.mark.parametrize("kind", ["task.stop", "agent.pause", "agent.pause.reply"])
def test_omission_is_preserved_and_explicit_null_is_rejected(kind: str) -> None:
    frame = signed_example(kind)
    model, _shape = body_model(kind)
    field = "message" if kind.endswith(".reply") else "reason"
    if kind.endswith(".reply"):
        frame["body"] = {"outcome": "error", "code": "agent_exiting"}
    frame["body"].pop(field, None)
    validated = model.validate_python(frame["body"])
    assert model.dump_python(validated, mode="json") == frame["body"]
    validator(body_schema_file(kind)).validate(frame["body"])
    frame["body"][field] = None
    assert not validator(body_schema_file(kind)).is_valid(frame["body"])
    with pytest.raises(FrameRefused) as refused:
        verify_frame(resigned(frame), keys=trusted_keys())
    assert refused.value.code == "invalid_body"


def test_frame_omission_is_preserved_and_null_is_not_an_absent_value() -> None:
    document = signed_example()
    assert models.Frame.model_validate(document).model_dump(mode="json") == document
    for field in ("authority", "not_before", "causation_id"):
        invalid = dict(document, **{field: None})
        with pytest.raises(ValidationError):
            models.Frame.model_validate(invalid)
        with pytest.raises(FrameRefused) as refused:
            verify_frame(resigned(invalid), keys=trusted_keys())
        assert refused.value.code == "malformed"


@pytest.mark.parametrize("data", [
    b'{"nested":' + b'[' * 1500 + b'0' + b']' * 1500 + b'}',
    b'{"integer":' + b'1' * 5000 + b'}',
    b'{"text":"\\ud800"}',
])
def test_hostile_json_is_refused_before_key_lookup(data: bytes) -> None:
    with pytest.raises(FrameRefused) as refused:
        verify_frame(data, keys={})
    assert refused.value.code == "malformed"


def test_nesting_limit_counts_containers_but_not_escaped_string_content() -> None:
    depth = kinds.MAX_NESTING_DEPTH - 1  # the root object is a container too
    at_limit = b'{"nested":' + b'[' * depth + b'0' + b']' * depth + b'}'
    canonical.parse(at_limit)
    canonical.canonical_bytes(canonical.parse(at_limit))
    with pytest.raises(CanonicalJsonError):
        canonical.parse(b'{"nested":[' + at_limit + b']}')
    brackets_in_string = {"text": '[{"quoted": "escaped\\\"text"}]' * 100}
    assert canonical.parse(json.dumps(brackets_in_string)) == brackets_in_string


@pytest.mark.parametrize("field", ["issued_at", "expires_at", "not_before"])
def test_impossible_calendar_times_are_malformed_before_authentication(field: str) -> None:
    frame = signed_example()
    frame[field] = "2026-02-30T04:30:00.000Z"
    with pytest.raises(FrameRefused) as refused:
        verify_frame(resigned(frame), keys={})
    assert refused.value.code == "malformed"


@pytest.mark.parametrize("kind", ["task.stop", "agent.pause", "agent.pause.reply"])
def test_receiver_caps_lifetime_and_accepts_just_before_its_boundary(kind: str) -> None:
    frame = signed_example(kind)
    issued_at = wire_frame.parse_timestamp(frame["issued_at"])
    frame["expires_at"] = "9999-12-31T23:59:59.999Z"
    data = resigned(frame)
    limit = kinds.KINDS[kind.removesuffix(".reply")].expiry_seconds
    last_moment = issued_at + timedelta(seconds=limit, milliseconds=-1)
    verify_frame(data, keys=trusted_keys(), now=last_moment)
    with pytest.raises(FrameRefused) as refused:
        verify_frame(data, keys=trusted_keys(), now=issued_at + timedelta(seconds=limit))
    assert refused.value.code == "expired"
    with pytest.raises(FrameRefused) as refused:
        verify_frame(data, keys=trusted_keys(), now=issued_at - timedelta(milliseconds=1))
    assert refused.value.code == "not_yet_valid"


def test_timestamp_extremes_do_not_overflow_lifetime_checks() -> None:
    frame = signed_example()
    frame["issued_at"] = "9999-12-31T23:59:59.998Z"
    frame["expires_at"] = "9999-12-31T23:59:59.999Z"
    verify_frame(resigned(frame), keys=trusted_keys(), now=wire_frame.parse_timestamp(frame["issued_at"]))


def test_stop_key_preserves_id_boundaries_and_accepts_maximum_ids() -> None:
    assert idempotency_key("task.stop", "a:b", "c") != idempotency_key("task.stop", "a", "b:c")
    assert idempotency_key("task.stop", "a", "b") == idempotency_key("task.stop", "a", "b")
    frame = signed_example()
    frame["body"].update(task_id="a" * 128, stop_id="b" * 128)
    frame["idempotency_key"] = idempotency_key("task.stop", frame["body"]["task_id"], frame["body"]["stop_id"])
    assert len(frame["idempotency_key"]) <= 256
    verify_frame(resigned(frame), keys=trusted_keys())


@pytest.mark.parametrize("mutation", ["recipient", "request", "arrival", "kind"])
def test_reply_inboxes_bind_the_destination_request_and_kind(mutation: str) -> None:
    frame = signed_example("agent.pause.reply")
    arrived_on = frame["subject"]
    if mutation == "recipient":
        frame["audience"]["runtime"] = "another-host"
    elif mutation == "request":
        frame.pop("causation_id")
    elif mutation == "arrival":
        arrived_on += ".another"
    else:
        frame["kind"] = "agent.pause"
    with pytest.raises(FrameRefused) as refused:
        verify_frame(resigned(frame), keys=trusted_keys(), arrived_on=arrived_on)
    assert refused.value.code == "subject_mismatch"


def test_known_kind_cannot_be_carried_by_another_delivery_class() -> None:
    frame = signed_example()
    frame["subject"] = "ua.v1.team_example.lead-host-1.evt.skills_changed"
    with pytest.raises(FrameRefused) as refused:
        verify_frame(resigned(frame), keys=trusted_keys(), arrived_on=frame["subject"])
    assert refused.value.code == "subject_mismatch"


def test_asyncapi_accepts_complete_frames_and_references_reply_channel_messages() -> None:
    document = json.loads((CONTRACT / "generated/asyncapi.json").read_text())
    location = "https://universal-agents.local/contracts/messages/v1/generated/asyncapi.json"
    resources = SCHEMAS.with_resource(location, DRAFT202012.create_resource(document))
    for path in valid_examples():
        frame = json.loads(path.read_text())
        if frame["protocol"] != kinds.PROTOCOL:
            continue  # newer-minor pruning is a receiver rule, not a strict schema rule
        key = frame["kind"].replace(".", "_")
        payload = Draft202012Validator({"$ref": f"{location}#/components/messages/{key}/payload"}, registry=resources)
        payload.validate(frame)
        assert not payload.is_valid(frame["body"])
        invalid = copy.deepcopy(frame)
        invalid["body"]["unknown"] = True
        assert not payload.is_valid(invalid)
    for operation in document["operations"].values():
        if "reply" in operation:
            reply = operation["reply"]
            channel_path = reply["channel"]["$ref"]
            channel_name = channel_path.removeprefix("#/channels/")
            assert channel_name in document["channels"]
            for message in reply["messages"]:
                assert message["$ref"].startswith(channel_path + "/messages/")
