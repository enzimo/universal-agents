"""The protocol 1 message contract and its Python implementation agree.

Sources are in contracts/messages/v1; ``scripts/export_message_contract.py``
generates the rest. These tests check that the generated files are current, that
schemas stay within the documented subset, that every valid example is accepted
(and every invalid one refused with its named code), and that the signing vectors
re-sign to the same bytes.
"""

from __future__ import annotations

import importlib.util
import json
from copy import deepcopy
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
    sign_renew_key_proof,
    renew_key_proof_bytes,
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
            assert subject.startswith(("ua.v1.{team}.{self}.", "ua.v1._install.{self}.", "_INBOX.")), (role, subject)
        assert "_INBOX.{self}.>" in document["subscribe"]
        for name, entry in REGISTRY["kinds"].items():
            if role in entry["senders"]:
                for address in EXPORT.entry_addresses(entry):
                    assert EXPORT.full_subject(EXPORT.subject_pattern(address), "{self}") in document["publish"]


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


STATE_EXAMPLES = EXAMPLES / "state"


@pytest.mark.parametrize("path", sorted((STATE_EXAMPLES / "valid").rglob("*.json")),
                         ids=lambda path: path.relative_to(STATE_EXAMPLES / "valid").as_posix())
def test_valid_state_records_are_accepted(path: Path) -> None:
    from custom_messages.wire.state import verify_state

    document = json.loads(path.read_text(encoding="utf-8"))
    validator("state.schema.json").validate(document)
    validator(REGISTRY["state_records"][document["record"]]["body"]).validate(document["body"])
    verified = verify_state(path.read_bytes(), keys=trusted_keys())
    assert verified.document.record == path.parent.name
    model = kinds.STATE_RECORDS[document["record"]].body_model
    assert model.dump_python(verified.body, mode="json", exclude_none=True, by_alias=True) == document["body"]


@pytest.mark.parametrize("path", sorted((STATE_EXAMPLES / "invalid").glob("*.json")), ids=lambda path: path.stem)
def test_invalid_state_records_are_refused_with_their_code(path: Path) -> None:
    from custom_messages.wire.state import verify_state

    case = json.loads(path.read_text(encoding="utf-8"))
    with pytest.raises(FrameRefused) as refused:
        verify_state(json.dumps(case["document"]).encode("utf-8"), keys=trusted_keys())
    assert refused.value.code == case["refusal"], refused.value


def test_every_state_record_has_a_valid_example() -> None:
    examples = {path.parent.name for path in (STATE_EXAMPLES / "valid").rglob("*.json")}
    assert set(REGISTRY["state_records"]) <= examples


def example(name: str) -> dict:
    return json.loads((EXAMPLES / name).read_text(encoding="utf-8"))


def private_key(key_id: str) -> Ed25519PrivateKey:
    return Ed25519PrivateKey.from_private_bytes(b64url_decode(KEYS[key_id]["seed"]))


def signed_state(document: dict) -> bytes:
    from custom_messages.wire.state import sign_state

    return json.dumps(sign_state(document, private_key(document["writer"]["key_id"]))).encode()


@pytest.mark.parametrize("record", ["agent/lead-busy", "placement/lead", "team_view/roster"])
@pytest.mark.parametrize("mismatch", ["writer", "record", "missing_team"])
def test_state_records_cannot_claim_another_team(record: str, mismatch: str) -> None:
    from custom_messages.wire.state import verify_state

    document = example(f"state/valid/{record}.json")
    if mismatch == "missing_team":
        del document["writer"]["team"]
    elif mismatch == "writer":
        document["writer"]["team"] = "another_team"
    elif document["record"] == "team_view":
        document["key"] = document["key"].replace("team_example", "another_team")
    else:
        document["body"]["team"] = "another_team"
        document["key"] = document["key"].replace("team_example", "another_team")
    with pytest.raises(FrameRefused) as refused:
        verify_state(signed_state(document), keys=trusted_keys())
    expected = "invalid_body" if mismatch == "record" and document["record"] == "team_view" else "wrong_team"
    assert refused.value.code == expected


def test_a_host_cannot_write_another_hosts_liveness() -> None:
    from custom_messages.wire.state import verify_state

    document = example("state/valid/host/lead-host.json")
    document["writer"].update(runtime="worker-host-1", key_id="test-worker-key-1")
    with pytest.raises(FrameRefused) as refused:
        verify_state(signed_state(document), keys=trusted_keys())
    assert refused.value.code == "invalid_body"


def test_host_liveness_does_not_require_a_team_scope() -> None:
    from custom_messages.wire.state import verify_state

    document = example("state/valid/host/lead-host.json")
    del document["writer"]["team"]
    assert verify_state(signed_state(document), keys=trusted_keys()).body.runtime == "lead-host-1"


def test_team_view_key_must_name_its_body_entry() -> None:
    from custom_messages.wire.state import verify_state

    document = example("state/valid/team_view/roster.json")
    document["key"] = "team_example.task.lead"
    with pytest.raises(FrameRefused) as refused:
        verify_state(signed_state(document), keys=trusted_keys())
    assert refused.value.code == "invalid_body"


@pytest.mark.parametrize("operation", ["join", "renew"])
def test_asyncapi_installation_subjects_match_the_authority_subscription(operation: str) -> None:
    document = json.loads((CONTRACT / "generated/asyncapi.json").read_text())
    assert document["channels"][f"host_{operation}"]["address"] == f"ua.v1._install.{{sender}}.svc.host.{operation}"


def test_dead_letter_senders_can_publish_raw_copies_under_their_own_id() -> None:
    for role in REGISTRY["kinds"]["dlq.notice"]["senders"]:
        document = json.loads((CONTRACT / f"generated/permissions/{role}.json").read_text())
        assert "ua.v1.{team}.{self}.dlq.raw.*" in document["publish"]
        assert not any("ua.v1.{team}.*.dlq.raw" in subject for subject in document["publish"])


@pytest.mark.parametrize("known", [False, True])
def test_join_proves_possession_even_when_the_key_is_known(known: bool) -> None:
    document = example("valid/host.join/full.json")
    document["issuer"].update(runtime="lead-host-1", key_id="test-lead-key-1")
    document["subject"] = "ua.v1._install.lead-host-1.svc.host.join"
    document["body"]["runtime"] = "lead-host-1"
    document["body"]["application_key"] = {"key_id": "test-lead-key-1", "public_key": KEYS["test-lead-key-1"]["public_key"]}
    signed = sign_frame(document, private_key("test-lead-key-1"))
    assert verify_frame(json.dumps(signed).encode(), keys=trusted_keys() if known else {}).frame.kind == "host.join"
    document["body"]["application_key"]["public_key"] = KEYS["test-worker-key-1"]["public_key"]
    signed = sign_frame(document, private_key("test-lead-key-1"))
    with pytest.raises(FrameRefused) as refused:
        verify_frame(json.dumps(signed).encode(), keys=trusted_keys() if known else {})
    assert refused.value.code == ("unknown_key" if known else "bad_signature")


def test_known_join_key_id_cannot_be_reassigned_to_another_public_key() -> None:
    document = example("valid/host.join/full.json")
    document["issuer"].update(runtime="lead-host-1", key_id="test-lead-key-1")
    document["subject"] = "ua.v1._install.lead-host-1.svc.host.join"
    document["body"]["runtime"] = "lead-host-1"
    document["body"]["application_key"] = {"key_id": "test-lead-key-1", "public_key": KEYS["test-worker-key-1"]["public_key"]}
    signed = sign_frame(document, private_key("test-worker-key-1"))
    with pytest.raises(FrameRefused) as refused:
        verify_frame(json.dumps(signed).encode(), keys=trusted_keys())
    assert refused.value.code == "unknown_key"


def test_lost_join_reply_can_be_recovered_after_the_key_becomes_known() -> None:
    from dataclasses import replace

    document = example("valid/host.join/full.json")
    document["issuer"].update(runtime="lead-host-1", key_id="test-lead-key-1")
    document["subject"] = "ua.v1._install.lead-host-1.svc.host.join"
    document["body"]["runtime"] = "lead-host-1"
    document["body"]["application_key"] = {"key_id": "test-lead-key-1", "public_key": KEYS["test-lead-key-1"]["public_key"]}
    data = json.dumps(sign_frame(document, private_key("test-lead-key-1"))).encode()
    verify_frame(data, keys={})
    known = {key_id: replace(key, min_membership_epoch=1, min_revocation_epoch=1) for key_id, key in trusted_keys().items()}
    verify_frame(data, keys=known)
    known["test-lead-key-1"] = replace(known["test-lead-key-1"], revoked_at=wire_frame.parse_timestamp("2026-10-22T04:30:00.000Z"))
    with pytest.raises(FrameRefused) as refused:
        verify_frame(data, keys=known)
    assert refused.value.code == "revoked"


def test_renewal_rotation_signing_vector() -> None:
    vector = json.loads((CONTRACT / "signing-vectors/key-rotation.json").read_text())
    document = example("valid/host.renew/rotate.json")
    before = deepcopy(document)
    assert renew_key_proof_bytes(document).hex() == vector["signing_bytes_hex"]
    assert document["body"]["application_key_rotation"]["proof"] == vector["proof"]
    signed = sign_renew_key_proof(document, private_key("test-worker-key-1"))
    assert sign_frame(signed, private_key("test-lead-key-1")) == document
    assert document == before, "signing must not mutate the caller's frame"


@pytest.mark.parametrize(("location", "field", "value"), [
    ("body", "renew_id", "changed"),
    ("body", "certificate_id", "changed"),
    ("body", "profile_sha256", "A" * 43),
    ("issuer", "revocation_epoch", 2),
    ("audience", "id", "another_authority"),
    ("", "message_id", "01928f3a-7b2c-7709-8000-000000000709"),
    ("", "expires_at", "2026-10-22T04:31:00.000Z"),
    ("", "protocol", "1.1-pre"),
])
def test_new_key_proof_cannot_be_reused_for_a_different_renewal(location: str, field: str, value) -> None:
    document = example("valid/host.renew/rotate.json")
    target = document[location] if location else document
    target[field] = value
    signed = sign_frame(document, private_key("test-lead-key-1"))
    with pytest.raises(FrameRefused) as refused:
        verify_frame(json.dumps(signed).encode(), keys=trusted_keys())
    assert refused.value.code == "invalid_body"


def test_rotation_proof_covers_a_newer_minors_unknown_fields() -> None:
    document = example("valid/host.renew/rotate.json")
    document["protocol"] = "1.1-pre"
    document["body"]["application_key_rotation"]["future_option"] = "original"
    document = sign_frame(sign_renew_key_proof(document, private_key("test-worker-key-1")), private_key("test-lead-key-1"))
    verify_frame(json.dumps(document).encode(), keys=trusted_keys())
    document["body"]["application_key_rotation"]["future_option"] = "changed"
    with pytest.raises(FrameRefused) as refused:
        verify_frame(json.dumps(sign_frame(document, private_key("test-lead-key-1"))).encode(), keys=trusted_keys())
    assert refused.value.code == "invalid_body"


@pytest.mark.parametrize("missing", ["key", "proof"])
def test_rotation_requires_both_key_and_proof_in_both_validators(missing: str) -> None:
    document = example("valid/host.renew/rotate.json")
    del document["body"]["application_key_rotation"][missing]
    assert list(validator("bodies/host.renew.schema.json").iter_errors(document["body"]))
    signed = sign_frame(document, private_key("test-lead-key-1"))
    with pytest.raises(FrameRefused) as refused:
        verify_frame(json.dumps(signed).encode(), keys=trusted_keys())
    assert refused.value.code == "invalid_body"


@pytest.mark.parametrize("bad", ["key", "proof"])
def test_rotation_requires_a_valid_signature_by_the_new_key(bad: str) -> None:
    document = example("valid/host.renew/rotate.json")
    rotation = document["body"]["application_key_rotation"]
    if bad == "key":
        rotation["key"]["public_key"] = KEYS["test-gateway-key-1"]["public_key"]
    else:
        rotation["proof"] = b64url(bytes(64))
    signed = sign_frame(document, private_key("test-lead-key-1"))
    with pytest.raises(FrameRefused) as refused:
        verify_frame(json.dumps(signed).encode(), keys=trusted_keys())
    assert refused.value.code == "invalid_body"


def test_rotation_cannot_be_null() -> None:
    document = example("valid/host.renew/rotate.json")
    document["body"]["application_key_rotation"] = None
    assert list(validator("bodies/host.renew.schema.json").iter_errors(document["body"]))
    with pytest.raises(FrameRefused) as refused:
        verify_frame(json.dumps(sign_frame(document, private_key("test-lead-key-1"))).encode(), keys=trusted_keys())
    assert refused.value.code == "invalid_body"


@pytest.mark.parametrize("operation", ["join", "renew"])
@pytest.mark.parametrize("bad", ["record", "body"])
def test_successful_installation_replies_require_a_certificate(operation: str, bad: str) -> None:
    document = example(f"valid/host.{operation}.reply/ok.json")
    if bad == "record":
        document["body"]["certificate"] = example("state/valid/team_view/roster.json")
    else:
        del document["body"]["certificate"]["body"]["application_key"]
    assert list(validator(f"bodies/host.{operation}.reply.schema.json").iter_errors(document["body"]))
    signed = sign_frame(document, private_key("test-services-key-1"))
    with pytest.raises(FrameRefused) as refused:
        verify_frame(json.dumps(signed).encode(), keys=trusted_keys())
    assert refused.value.code == "invalid_body"
