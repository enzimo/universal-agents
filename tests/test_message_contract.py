"""The protocol 1 message contract and its Python implementation agree.

Sources are in contracts/messages/v1; ``scripts/export_message_contract.py``
generates the rest and signs the examples. These tests check that the generated
files are current, that schemas stay within the documented subset, that every valid
example is accepted and every invalid one refused with its named code at its named
place, that the trust store accepts only records that chain to an authority, and
that the signing vectors re-sign to the same bytes.
"""

from __future__ import annotations

import copy
import dataclasses
import importlib.util
import json
import re
from copy import deepcopy
from datetime import timedelta
from pathlib import Path

import pytest
from cryptography.hazmat.primitives.serialization import Encoding, PublicFormat
from jsonschema import Draft202012Validator, ValidationError as SchemaError, validators
from pydantic import ValidationError
from referencing import Registry
from referencing.jsonschema import DRAFT202012

from custom_messages.enums import ChangeRefusal
from custom_messages.wire import canonical, frame as wire_frame, kinds, models
from custom_messages.wire.canonical import CanonicalJsonError
from custom_messages.wire.frame import (
    FrameRefused,
    b64url,
    b64url_decode,
    body_model,
    idempotency_key,
    sign_authority_token,
    sign_frame,
    signing_bytes,
    verify_frame,
)
from custom_messages.wire.trust import TrustStore, sign_state, state_signing_bytes
from custom_messages.wire.values import parse_timestamp
from tests import message_contract_support as fixtures

ROOT = Path(__file__).resolve().parents[1]
CONTRACT = ROOT / "contracts" / "messages" / "v1"
EXAMPLES = CONTRACT / "examples"
SCHEMA_BASE = "https://universal-agents.local/contracts/messages/v1/schemas/"
SKEW = timedelta(seconds=kinds.CLOCK_SKEW_SECONDS)


def _exporter():
    spec = importlib.util.spec_from_file_location("export_message_contract", ROOT / "scripts" / "export_message_contract.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


EXPORT = _exporter()
REGISTRY = json.loads((CONTRACT / "generated" / "registry.json").read_text(encoding="utf-8"))


def schema_registry() -> Registry:
    """Every contract schema, as draft 2020-12 resources.

    ``$schema`` is dropped so that jsonschema keeps the contract's validator
    (``ContractValidator``) when it follows a ``$ref``; with the key present it
    switches back to the stock draft 2020-12 validator.
    """

    resources = []
    for path in (CONTRACT / "schemas").rglob("*.schema.json"):
        schema = json.loads(path.read_text(encoding="utf-8"))
        assert schema.pop("$schema") == "https://json-schema.org/draft/2020-12/schema"
        resources.append((schema["$id"], DRAFT202012.create_resource(schema)))
    return Registry().with_resources(resources)


SCHEMAS = schema_registry()


def _end_anchored_pattern(validator, pattern, instance, schema):
    """``pattern`` as the README defines it: ``$`` is the end of the value.

    Python's ``re`` also lets ``$`` match before a final newline, so the plain
    jsonschema validator would accept ``"x\n"`` where every other engine refuses it.
    The subset checker guarantees the pattern ends with its only ``$``.
    """

    if validator.is_type(instance, "string") and not re.search(pattern[:-1] + r"\Z", instance):
        yield SchemaError(f"{instance!r} does not match {pattern!r}")


ContractValidator = validators.extend(Draft202012Validator, {"pattern": _end_anchored_pattern})


def validator(schema_file: str) -> Draft202012Validator:
    return ContractValidator({"$ref": SCHEMA_BASE + schema_file}, registry=SCHEMAS)


def body_schema_file(kind: str) -> str:
    if kind in REGISTRY["kinds"]:
        return REGISTRY["kinds"][kind]["body"]
    return REGISTRY["kinds"][kind.removesuffix(".reply")]["reply"]["body"]


def valid_examples() -> list[Path]:
    return sorted((EXAMPLES / "valid").rglob("*.json"))


def invalid_examples() -> list[Path]:
    return sorted((EXAMPLES / "invalid").glob("*.json"))


def task_request_key(frame: dict) -> str:
    """Resolve the registry's frame and body placeholders for a dispatch."""

    rule = REGISTRY["kinds"]["task.request"]["idempotency"]
    return rule.replace("<task_id>", frame["body"]["task_id"]).replace("<message_id>", frame["message_id"])


def invalid_case(path: Path) -> tuple[dict, bytes]:
    case = json.loads(path.read_text(encoding="utf-8"))
    data = case["frame_text"].encode("utf-8")
    if "pad_to_bytes" in case:
        data += b" " * (case["pad_to_bytes"] - len(data))
    return case, data


def signed_example(kind: str = "task.stop") -> dict:
    return fixtures.example_frame(kind, "paused" if kind.endswith(".reply") else "basic")


def refusal(data: bytes, frame: dict, context: dict | None = None, **overrides) -> FrameRefused:
    with pytest.raises(FrameRefused) as refused:
        fixtures.verify(data, frame, context, **overrides)
    return refused.value


# -- generated files and the schema subset -----------------------------------------------------------

def test_generated_files_are_current() -> None:
    assert EXPORT.stale(EXPORT.render()) == []


@pytest.mark.parametrize("kind", ["tasks.steer", "conflicts.decide"])
def test_export_refuses_retry_keys_with_missing_inputs(kind: str) -> None:
    registry = deepcopy(REGISTRY)
    registry["kinds"][kind]["idempotency"] = f"{kind}:<undefined_command_id>"
    with pytest.raises(EXPORT.ContractError, match="undefined_command_id"):
        EXPORT.check_registry(registry, EXPORT.load_yaml("transport.yaml"),
                              EXPORT.load_yaml("team-topologies.yaml"), EXPORT.load_schemas())


def test_export_refuses_a_retry_key_based_on_an_optional_field() -> None:
    registry = deepcopy(REGISTRY)
    registry["kinds"]["tasks.steer"]["idempotency"] = "tasks.steer:<replied_message>"
    with pytest.raises(EXPORT.ContractError, match="replied_message"):
        EXPORT.check_registry(registry, EXPORT.load_yaml("transport.yaml"),
                              EXPORT.load_yaml("team-topologies.yaml"), EXPORT.load_schemas())


def closed(**properties) -> dict:
    return {"type": "object", "properties": properties, "required": [], "additionalProperties": False}


@pytest.mark.parametrize(
    ("schema", "problem"),
    [
        ({"type": "object", "properties": {"a": {"type": "string"}}}, "additionalProperties false"),
        ({"type": "number", "minimum": 0, "maximum": 1}, "outside the subset"),
        ({"type": ["string", "integer"]}, "outside the subset"),
        ({"type": "integer", "minimum": 0}, "within"),
        ({"type": "integer", "minimum": 0, "maximum": 2**53}, "within"),
        ({"type": "string", "format": "uuid"}, "outside the subset"),
        ({"type": "array", "items": {"type": "string"}, "uniqueItems": True}, "outside the subset"),
        ({"type": "array"}, "items"),
        ({"maxLength": 3}, "needs type string"),
        ({"$ref": "https://example.com/x.json"}, "local"),
        ({"oneOf": [closed(a={"type": "string"})]}, "const"),
        ({"oneOf": [closed(kind={"const": "a"}), closed(kind={"const": "b"})]}, "requires"),
        # Constraints the generated models would drop.
        ({"$ref": "#/$defs/x", "type": "string", "maxLength": 3}, "beside \\$ref"),
        ({"type": "string", "enum": ["a", "bb"], "maxLength": 1}, "beside enum"),
        ({"type": "integer", "enum": [1, 5], "maximum": 3}, "beside enum"),
        ({"type": "string", "enum": ["a", 1]}, "type string"),
        ({"const": None}, "never null"),
        ({**closed(p={"type": "string"}), "required": ["p", "q"]}, "does not define"),
        # A property named like a keyword is still a schema.
        (closed(properties={"type": "string", "pattern": "^(?=a)a$"}), "not portable"),
        (closed(**{"$defs": {"type": "string", "format": "uuid"}}), "outside the subset"),
    ],
)
def test_the_schema_subset_refuses_what_other_implementations_read_differently(schema, problem) -> None:
    with pytest.raises(EXPORT.ContractError, match=problem):
        EXPORT.check_subset("test.schema.json", schema, "")


@pytest.mark.parametrize("pattern", [
    "a+", "^a", "^a|b$", "^(?=a)a$", "^(?i)a$", "^(?P<n>a)$", "^(a)\\1$", "^\\d+$", "^\\w+$", "^\\s$",
    "^.+$", "^[[:alpha:]]$", "^a++$", "^\\Aa$", "^a\\b$", "^\\p{L}$", "^[a$", "^a$b$",
])
def test_patterns_that_engines_read_differently_are_refused(pattern: str) -> None:
    assert EXPORT.pattern_problem(pattern), pattern


@pytest.mark.parametrize("pattern", [
    "^[A-Za-z0-9][A-Za-z0-9_-]{0,63}$", "^1\\.(0|[1-9][0-9]{0,3})-pre$", "^(?:a|b)$", "^[a$^.-]+$", "^a{1,3}?$",
])
def test_portable_patterns_are_allowed(pattern: str) -> None:
    assert EXPORT.pattern_problem(pattern) is None


def test_every_schema_pattern_refuses_a_trailing_newline_in_models_and_schemas() -> None:
    frame = signed_example()
    frame["body"]["task_id"] += "\n"
    assert not validator(body_schema_file("task.stop")).is_valid(frame["body"])
    with pytest.raises(ValidationError):
        kinds.KINDS["task.stop"].body_model.validate_python(frame["body"])


def test_two_kinds_with_one_generated_name_are_refused() -> None:
    registry = EXPORT.load_yaml("registry.yaml")
    for kind in ("audit.log_entry", "audit_log.entry"):
        registry["kinds"][kind] = dict(registry["kinds"]["skills.changed"], address=f"evt.{kind.replace('.', '_')}")
    with pytest.raises(EXPORT.ContractError, match="same model names"):
        EXPORT.check_registry(registry, TRANSPORT, EXPORT.load_yaml("team-topologies.yaml"), EXPORT.load_schemas())


def test_two_schemas_with_one_generated_name_are_refused() -> None:
    schemas = {"a.schema.json": {"$defs": {"note_text": {"type": "string", "maxLength": 1}}},
               "b.schema.json": {"$defs": {"note-text": {"type": "string", "maxLength": 9}}}}
    writer = EXPORT.ModelWriter(schemas)
    writer.named("a.schema.json", "/$defs/note_text")
    with pytest.raises(EXPORT.ContractError, match="already uses"):
        writer.named("b.schema.json", "/$defs/note-text")


@pytest.mark.parametrize(("name", "problem"), [
    ("agent~{team}_{agent}", "separate the ids"),
    ("agent_{team}~{agent}", "fixed word"),
    ("agent{team}~{agent}", "fixed word"),
    ("agent~{team}", "exactly the filter's placeholders"),
    ("agent.{team}~{agent}", "consumer names cannot hold"),
])
def test_consumer_names_that_different_ids_could_share_are_refused(name: str, problem: str) -> None:
    transport = copy.deepcopy(TRANSPORT)
    transport["consumers"]["agent_inbox"]["name"] = name
    with pytest.raises(EXPORT.ContractError, match=problem):
        EXPORT.check_consumer_names(transport)


def test_consumer_name_prefixes_cannot_begin_each_other() -> None:
    transport = copy.deepcopy(TRANSPORT)
    transport["consumers"]["pool"]["name"] = "agent~x~{team}~{pool}"
    with pytest.raises(EXPORT.ContractError, match="begins another prefix"):
        EXPORT.check_consumer_names(transport)


def test_check_mode_compares_bytes_so_rewritten_line_endings_are_stale(tmp_path: Path) -> None:
    generated = tmp_path / "generated"
    generated.mkdir()
    (generated / "a.json").write_bytes(b"{}\r\n")
    (generated / "stray.json").write_bytes(b"{}\n")
    problems = EXPORT.stale({generated / "a.json": "{}\n"}, generated)
    assert problems == [str(generated / "a.json"), f"{generated / 'stray.json'} (not generated)"]


def test_change_refusals_are_the_stores_refusal_codes() -> None:
    assert kinds.CHANGE_REFUSALS == tuple(refusal.value for refusal in ChangeRefusal)


TRANSPORT = json.loads((CONTRACT / "generated" / "transport.json").read_text(encoding="utf-8"))


@pytest.mark.parametrize(
    ("change", "problem"),
    [
        (lambda t: t["streams"]["UA1_WORK"].update(max_age_seconds=t["classes"]["durable"]["max_expiry_seconds"] - 1),
         "must keep messages"),
        (lambda t: t["streams"]["UA1_WORK"].pop("max_age_seconds"), "must keep messages"),
        (lambda t: t["classes"]["durable"].update(stream="UA1_MISSING"), "unknown stream"),
        (lambda t: t.update(replay_ledger_retention_seconds=t["classes"]["durable"]["max_expiry_seconds"] - 1),
         "replay records"),
    ],
)
def test_transport_settings_that_would_lose_or_replay_messages_are_refused(change, problem) -> None:
    transport = copy.deepcopy(TRANSPORT)
    change(transport)
    with pytest.raises(EXPORT.ContractError, match=problem):
        EXPORT.check_transport(transport)


def test_a_kind_cannot_outlive_its_delivery_class() -> None:
    registry = EXPORT.load_yaml("registry.yaml")
    registry["kinds"]["agent.pause"]["expiry_seconds"] = TRANSPORT["classes"]["request_reply"]["max_expiry_seconds"] + 1
    with pytest.raises(EXPORT.ContractError, match="max_expiry_seconds"):
        EXPORT.check_registry(registry, TRANSPORT, EXPORT.load_yaml("team-topologies.yaml"), EXPORT.load_schemas())


def test_every_kind_has_a_model_and_every_model_a_kind() -> None:
    assert set(kinds.KINDS) == set(REGISTRY["kinds"])
    adapters = {name.removesuffix("BodyAdapter") for name in dir(models) if name.endswith("BodyAdapter")}
    assert adapters == {EXPORT.class_name(kind) for kind in REGISTRY["kinds"]}
    for name, spec in kinds.KINDS.items():
        entry = REGISTRY["kinds"][name]
        assert (spec.reply_model is not None) == ("reply" in entry)
        assert spec.reply_errors == tuple(entry.get("reply", {}).get("errors", ()))
        addresses = {EXPORT.audience_target(address): address for address in EXPORT.entry_addresses(entry)}
        assert (spec.delivery_class, spec.addresses, spec.expiry_seconds) == (entry["class"], addresses, entry["expiry_seconds"])
    assert kinds.VERIFICATION_REFUSALS == tuple(item["code"] for item in REGISTRY["verification_refusals"])


def test_permissions_name_exact_identities_and_publish_only_under_the_senders_own_id() -> None:
    folder = CONTRACT / "generated" / "permissions"
    consumer_names = {consumer["name"] for consumer in TRANSPORT["consumers"].values()}
    for role in REGISTRY["host_roles"]:
        document = json.loads((folder / f"{role}.json").read_text(encoding="utf-8"))
        for subject in document["publish"]:
            if subject.startswith("$JS."):
                stream_and_name = re.fullmatch(r"\$JS\.(?:API\.CONSUMER\.(?:INFO|MSG\.NEXT)\.|ACK\.)UA1_WORK\.([^.]+)(\.>)?", subject)
                assert stream_and_name and stream_and_name.group(1) in consumer_names, (role, subject)
                assert (stream_and_name.group(2) is not None) == subject.startswith("$JS.ACK."), (role, subject)
            else:
                assert subject.startswith("ua.v1.{team}.{self}."), (role, subject)
        assert document["subscribe"][0] == "_INBOX.{self}.>"
        for subject in document["subscribe"][1:]:
            assert subject.startswith("ua.v1.{team}.*.") and "*" not in subject.removeprefix("ua.v1.{team}.*."), subject
        receives_requests = False
        for entry in REGISTRY["kinds"].values():
            for address in EXPORT.entry_addresses(entry):
                if role in entry["senders"]:
                    assert "ua.v1.{team}.{self}." + EXPORT.send_pattern(address) in document["publish"]
                receives = role in EXPORT.receiving_roles(entry, address, TRANSPORT)
                if entry["class"] != "durable":
                    assert ("ua.v1.{team}.*." + address in document["subscribe"]) == receives, (role, address)
                receives_requests |= receives and entry["class"] == "request_reply"
        expires = f"{TRANSPORT['classes']['request_reply']['max_expiry_seconds'] + TRANSPORT['clock_skew_seconds']}s"
        assert document.get("allow_responses") == ({"max": 1, "expires": expires} if receives_requests else None)


def test_a_receiver_role_gets_only_the_addresses_it_serves() -> None:
    """team services receive task.request's copy on work.team, not the agent and pool inboxes (review I5)."""

    services = json.loads((CONTRACT / "generated" / "permissions" / "team_services.json").read_text(encoding="utf-8"))
    consumers = {subject.split(".")[-1] if not subject.startswith("$JS.ACK.") else subject.split(".")[3]
                 for subject in services["publish"] if subject.startswith("$JS.")}
    assert consumers == {TRANSPORT["consumers"]["team_services"]["name"]}


def test_asyncapi_describes_both_operations_for_every_address() -> None:
    document = json.loads((CONTRACT / "generated" / "asyncapi.json").read_text(encoding="utf-8"))
    operations = document["operations"]
    for channel, spec in document["channels"].items():
        if channel.endswith("_reply"):
            continue  # reply inboxes are referenced by the request's operations
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
            if message in channel["messages"] and not channel["address"].startswith("_INBOX."):
                assert operation["reply"]["messages"] == [{"$ref": f"#/channels/{message}_reply/messages/{message}_reply"}]


def test_reassignment_preserves_the_task_but_changes_the_dispatch_key() -> None:
    original = fixtures.example_frame("task.request", "to-pool")
    replacement = fixtures.example_frame("task.request", "reassignment")
    assert replacement["body"]["task_id"] == original["body"]["task_id"]
    assert task_request_key(replacement) != task_request_key(original)


def test_every_kind_and_reply_has_a_valid_example() -> None:
    examples = {path.parent.name for path in valid_examples()}
    expected = set(REGISTRY["kinds"]) | {f"{name}.reply" for name, entry in REGISTRY["kinds"].items() if "reply" in entry}
    assert expected <= examples, sorted(expected - examples)


# -- canonical form and encodings ------------------------------------------------------------------

VECTORS = json.loads((CONTRACT / "signing-vectors" / "vectors.json").read_text(encoding="utf-8"))


@pytest.mark.parametrize("case", VECTORS["canonical"], ids=lambda case: case["name"])
def test_canonical_form(case) -> None:
    assert canonical.canonical_bytes(canonical.parse(case["input"])).decode("utf-8") == case["canonical"]


@pytest.mark.parametrize("case", VECTORS["canonical_refused"], ids=lambda case: case["name"])
def test_canonical_form_refuses(case) -> None:
    with pytest.raises(CanonicalJsonError):
        canonical.parse(case["input"])


@pytest.mark.parametrize("document", [bytearray(b'{"a":1}'), memoryview(b'{"a":1}')])
def test_byte_like_documents_parse_like_bytes(document) -> None:
    assert canonical.parse(document) == {"a": 1}


@pytest.mark.parametrize("document", [b'\xef\xbb\xbf{"a":1}', '﻿{"a":1}'])
def test_a_byte_order_mark_is_refused(document) -> None:
    with pytest.raises(CanonicalJsonError):
        canonical.parse(document)


@pytest.mark.parametrize("case", VECTORS["base64url_refused"], ids=lambda case: case["name"])
def test_only_one_spelling_of_each_base64url_value_decodes(case) -> None:
    with pytest.raises(ValueError):
        b64url_decode(case["text"])


def test_every_signature_spelling_but_one_is_refused() -> None:
    signature = signed_example()["signature"]
    assert b64url(b64url_decode(signature)) == signature
    alphabet = "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789-_"
    respelled = [signature[:-1] + alphabet[alphabet.index(signature[-1]) ^ bits] for bits in range(1, 16)]
    for text in respelled:
        with pytest.raises(ValueError):
            b64url_decode(text)


# -- signing vectors --------------------------------------------------------------------------------

@pytest.mark.parametrize("vector", VECTORS["frames"], ids=lambda vector: vector["name"])
def test_frame_vectors_re_sign_to_the_same_bytes(vector) -> None:
    key = fixtures.private_key(vector["key_id"])
    assert b64url(key.public_key().public_bytes(Encoding.Raw, PublicFormat.Raw)) == vector["public_key"]
    assert signing_bytes(vector["frame"]).hex() == vector["signing_bytes_hex"]
    assert wire_frame.body_digest(vector["frame"]["body"]) == vector["body_digest"]
    resigned = sign_frame(vector["frame"], key)
    assert resigned["signature"] == vector["signature"]
    assert resigned == vector["frame"]


@pytest.mark.parametrize("vector", VECTORS["authority_tokens"], ids=lambda vector: vector["name"])
def test_authority_token_vectors_re_sign_to_the_same_bytes(vector) -> None:
    assert wire_frame.authority_signing_bytes(vector["token"]).hex() == vector["signing_bytes_hex"]
    assert sign_authority_token(vector["token"], fixtures.private_key(vector["key_id"])) == vector["token"]


@pytest.mark.parametrize("vector", VECTORS["states"], ids=lambda vector: vector["name"])
def test_state_record_vectors_re_sign_to_the_same_bytes(vector) -> None:
    assert state_signing_bytes(vector["state"]).hex() == vector["signing_bytes_hex"]
    assert sign_state(vector["state"], fixtures.private_key(vector["key_id"])) == vector["state"]


def test_vector_domains_are_the_registrys() -> None:
    assert VECTORS["domain_prefixes"] == {
        "frame": kinds.SIGNING_DOMAIN + "\n",
        "state": kinds.STATE_SIGNING_DOMAIN + "\n",
        "authority_token": kinds.AUTHORITY_TOKEN_DOMAIN + "\n",
    }


# -- examples ----------------------------------------------------------------------------------------

@pytest.mark.parametrize("path", valid_examples(), ids=lambda path: path.relative_to(EXAMPLES / "valid").as_posix())
def test_valid_examples_are_accepted(path: Path) -> None:
    data = path.read_bytes()
    document = json.loads(data)
    newer_minor = document["protocol"] != kinds.PROTOCOL
    if not newer_minor:
        validator("frame.schema.json").validate(document)
        validator(body_schema_file(document["kind"])).validate(document["body"])
        entry = REGISTRY["kinds"].get(document["kind"])
        if entry is not None and entry["class"] == "request_reply":
            fields = {**document["body"], "message_id": document["message_id"], "agent": document["audience"]["id"]}
            expected_key = re.sub(r"<([^>]+)>", lambda match: str(fields[match[1]]), entry["idempotency"])
            assert document["idempotency_key"] == expected_key
    verified = fixtures.verify(data, document)
    assert verified.frame.kind == path.parent.name
    assert verified.replay_key.message_id == document["message_id"]
    assert (verified.session is not None) == ("session" in document["issuer"])
    assert (verified.authority is not None) == ("authority" in document)
    model, _shape = body_model(document["kind"])
    dumped = model.dump_python(verified.body, mode="json", exclude_none=True, by_alias=True)
    if newer_minor:
        assert set(dumped) < set(document["body"]), "a newer minor's unknown fields were not dropped"
    else:
        assert dumped == document["body"], "the body did not survive a round trip"
        assert models.Frame.model_validate(document).model_dump(mode="json", by_alias=True) == document
    if document["kind"] == "task.request":
        assert document["idempotency_key"] == task_request_key(document)


@pytest.mark.parametrize("path", invalid_examples(), ids=lambda path: path.stem)
def test_invalid_examples_are_refused_with_their_code_at_their_place(path: Path) -> None:
    case, data = invalid_case(path)
    assert case["refusal"] in kinds.VERIFICATION_REFUSALS
    refused = refusal(data, json.loads(case["frame_text"]), case["context"])
    assert (refused.code, refused.at) == (case["refusal"], case["at"]), refused


def test_every_refusal_code_but_access_denied_has_an_example() -> None:
    codes = {json.loads(path.read_text())["refusal"] for path in invalid_examples()}
    assert codes == set(kinds.VERIFICATION_REFUSALS) - {"access_denied"}


def test_neighbouring_checks_each_have_a_two_defect_example() -> None:
    """Swapping any two checks fails some example: a reordering always inverts a neighbouring pair.

    An example's ``also`` lists the refusals its further defects would give once the
    first is fixed.
    """

    order = [code for code in kinds.VERIFICATION_REFUSALS if code != "access_denied"]
    covered = set()
    for path in invalid_examples():
        case = json.loads(path.read_text(encoding="utf-8"))
        for later in case.get("also", []):
            assert order.index(later) > order.index(case["refusal"]), path.stem
            covered.add((case["refusal"], later))
    assert {(first, second) for first, second in zip(order, order[1:])} <= covered


@pytest.mark.parametrize("path", [p for p in invalid_examples() if json.loads(p.read_text())["refusal"] == "invalid_body"],
                         ids=lambda path: path.stem)
def test_invalid_bodies_fail_their_schema_where_named(path: Path) -> None:
    case = json.loads(path.read_text(encoding="utf-8"))
    document = json.loads(case["frame_text"])
    errors = list(validator(body_schema_file(document["kind"])).iter_errors(document["body"]))
    assert errors
    locations = {"/body" + "".join(f"/{part}" for part in error.absolute_path) for error in errors}
    nested = {"/body" + "".join(f"/{part}" for part in sub.absolute_path) for error in errors for sub in error.context or ()}
    assert case["at"] in locations | nested or any(case["at"].startswith(location) for location in locations), (locations, nested)


# A pattern cannot know month lengths, so impossible-date passes the schema; receivers
# refuse dates that do not exist (README, "Canonical form and signatures").
@pytest.mark.parametrize("path", [p for p in invalid_examples() if json.loads(p.read_text())["refusal"] == "malformed"
                                  and json.loads(p.read_text())["at"] and p.stem != "impossible-date"], ids=lambda path: path.stem)
def test_malformed_frames_fail_the_frame_schema_too(path: Path) -> None:
    case = json.loads(path.read_text(encoding="utf-8"))
    assert not validator("frame.schema.json").is_valid(json.loads(case["frame_text"]))


# -- who sent it ----------------------------------------------------------------------------------------

def test_the_reply_forgery_from_the_review_is_refused() -> None:
    """A host that received another agent's pause request answers as that agent (review finding 4)."""

    request = fixtures.example_frame("agent.pause", "to-researcher-3")
    reply = signed_example("agent.pause.reply")
    reply.update(causation_id=request["message_id"])
    reply["issuer"]["agent"] = "researcher-3"
    refused = refusal(fixtures.resign(reply), reply, {"answering": "agent.pause/to-researcher-3.json"})
    assert (refused.code, refused.at) == ("wrong_issuer", "/issuer/agent")


def test_a_reply_from_the_agent_asked_is_accepted_only_against_its_request() -> None:
    reply = signed_example("agent.pause.reply")
    data = json.dumps(reply).encode()
    assert fixtures.verify(data, reply).frame.causation_id == fixtures.example_frame("agent.pause")["message_id"]
    assert refusal(data, reply, {"answering": "agent.pause/to-researcher-3.json"}).code == "wrong_issuer"


def test_a_session_on_another_host_does_not_let_a_host_speak_for_the_agent() -> None:
    frame = signed_example()
    frame["issuer"].update(runtime="worker-host-1", key_id="test-worker-key-1")
    assert refusal(fixtures.resign(frame), frame).code == "wrong_issuer"


def certificate_record(runtime: str, *, roles: tuple[str, ...], epoch: int = 1,
                       issued_at: str = "2026-10-22T00:00:00.000Z", expires_at: str = "2026-11-21T00:00:00.000Z",
                       key_id: str = "test-worker-key-1", installation: str = "install_example",
                       authority: str = "test-authority-key-1") -> bytes:
    """A host certificate for team_example, signed by ``authority``."""

    body = {
        "certificate_id": f"cert_{runtime}", "installation": installation, "runtime": runtime, "teams": ["team_example"],
        "roles": list(roles), "membership_epoch": epoch, "revocation_epoch": 1,
        "application_key": {"key_id": key_id, "public_key": fixtures.KEYS[key_id]["public_key"]},
        "nats_public_key": "U" + "X" * 55, "agent_types": [], "session_limit": 0, "protocol": kinds.PROTOCOL,
        "deployment": "local", "profile_sha256": "CgoKCgoKCgoKCgoKCgoKCgoKCgoKCgoKCgoKCgoKCgo",
        "issued_at": issued_at, "expires_at": expires_at,
    }
    document = {"record": "certificate", "key": f"host.{installation}.{runtime}.{epoch}",
                "writer": {"type": "authority", "installation": installation, "key_id": authority},
                "sequence": 1, "issued_at": issued_at, "body": body}
    return json.dumps(sign_state(document, fixtures.private_key(authority))).encode()


@pytest.mark.parametrize(("record", "code"), [
    # A host cannot certify itself: certificates come from an authority key.
    (lambda: json.loads(certificate_record("rogue-host", roles=("lead_host",)))
     | {"writer": {"type": "host", "installation": "install_example", "runtime": "lead-host-1", "membership_epoch": 3,
                   "key_id": "test-lead-key-1"}}, "wrong_issuer"),
    # Another installation's authority vouches only through an agreement, and only for its own hosts.
    (lambda: certificate_record("peer-host-2", roles=("lead_host",), installation="install_peer",
                                authority="test-authority-key-1"), "unknown_key"),
    (lambda: certificate_record("x-install_peer", roles=("lead_host",)), "invalid_body"),
    (lambda: certificate_record("late-host", roles=("lead_host",), expires_at="2026-10-21T00:00:00.000Z"), "invalid_body"),
])
def test_the_trust_store_accepts_only_records_that_chain_to_an_authority(record, code) -> None:
    store = fixtures.trust_store()
    data = record()
    if isinstance(data, dict):
        data = json.dumps(data).encode()
    with pytest.raises(FrameRefused) as refused:
        store.add(data)
    assert refused.value.code == code, refused.value


def test_a_session_signed_by_a_host_without_team_services_is_refused() -> None:
    store = fixtures.trust_store()
    session = json.loads((EXAMPLES / "trust" / "sessions" / "researcher-2.json").read_text())
    session["writer"] = {"type": "host", "installation": "install_example", "runtime": "worker-host-1",
                         "membership_epoch": 3, "key_id": "test-worker-key-1"}
    session["sequence"] = 5
    with pytest.raises(FrameRefused) as refused:
        store.add(json.dumps(sign_state(session, fixtures.private_key("test-worker-key-1"))).encode())
    assert (refused.value.code, refused.value.at) == ("wrong_issuer", "/writer")


def test_an_older_record_never_replaces_a_newer_one_and_revocations_only_grow() -> None:
    store = fixtures.trust_store(add=["extra/lead-host-revoked-after-issue.json"])
    assert store.revoked("install_example", "cert_lead_host_1")
    assert store.add((EXAMPLES / "trust" / "revocations" / "install_example.json").read_bytes()) is False
    assert store.revoked("install_example", "cert_lead_host_1")
    shrunk = json.loads((EXAMPLES / "trust" / "revocations" / "install_example.json").read_text())
    shrunk["sequence"] = 9
    with pytest.raises(FrameRefused) as refused:
        store.add(json.dumps(sign_state(shrunk, fixtures.private_key("test-authority-key-1"))).encode())
    assert refused.value.code == "invalid_body"


def test_a_record_sequence_reused_for_other_content_is_refused() -> None:
    store = fixtures.trust_store()
    session = json.loads((EXAMPLES / "trust" / "sessions" / "lead.json").read_text())
    assert store.add(json.dumps(session).encode()) is False  # the same record again
    session["body"]["ends_at"] = "2026-10-22T05:00:00.000Z"
    with pytest.raises(FrameRefused) as refused:
        store.add(json.dumps(sign_state(session, fixtures.private_key("test-services-key-1"))).encode())
    assert (refused.value.code, refused.value.at) == ("replayed", "/sequence")


def test_a_frame_that_arrived_without_its_header_copies_is_refused() -> None:
    frame = signed_example()
    refused = refusal(json.dumps(frame).encode(), frame, {"headers": {}})
    assert (refused.code, refused.at) == ("subject_mismatch", "/kind")


def test_a_revocation_that_arrives_later_still_stops_queued_frames() -> None:
    frame = signed_example()
    data = json.dumps(frame).encode()
    store = fixtures.trust_store()
    fixtures.verify(data, frame, trust=store)
    store.add((EXAMPLES / "trust" / "extra" / "lead-host-revoked-after-issue.json").read_bytes())
    assert refusal(data, frame, trust=store).code == "revoked"


def test_agreements_come_only_from_the_receivers_own_authority_and_carry_durable_kinds() -> None:
    agreement = json.loads((EXAMPLES / "trust" / "agreements" / "install_peer.json").read_text())
    agreement["sequence"] = 2
    agreement["body"]["kinds"] = ["task.stop", "agent.pause"]
    store = fixtures.trust_store()
    with pytest.raises(FrameRefused) as refused:
        store.add(json.dumps(sign_state(agreement, fixtures.private_key("test-authority-key-1"))).encode())
    assert (refused.value.code, refused.value.at) == ("invalid_body", "/body/kinds/1")
    forged = dict(agreement, writer={"type": "authority", "installation": "install_peer", "key_id": "test-peer-authority-key-1"})
    forged["body"] = dict(agreement["body"], kinds=["task.stop"])
    with pytest.raises(FrameRefused) as refused:
        store.add(json.dumps(sign_state(forged, fixtures.private_key("test-peer-authority-key-1"))).encode())
    assert refused.value.code == "wrong_issuer"


def test_a_host_that_joins_later_is_trusted_once_its_certificate_arrives() -> None:
    """New hosts need no change on existing receivers: their certificate and sessions are enough."""

    store = fixtures.trust_store()
    frame = signed_example()
    frame["issuer"] = {"installation": "install_example", "team": "team_example", "runtime": "lead-host-2",
                       "membership_epoch": 1, "revocation_epoch": 1, "key_id": "test-gateway-key-1"}
    frame["subject"] = "ua.v1.team_example.lead-host-2.work.agent.researcher-2"
    data = json.dumps(sign_frame(frame, fixtures.private_key("test-gateway-key-1"))).encode()
    assert refusal(data, frame, trust=store).code == "unknown_key"
    store.add(certificate_record("lead-host-2", roles=("lead_host",), key_id="test-gateway-key-1"))
    assert fixtures.verify(data, frame, trust=store).certificate.runtime == "lead-host-2"


# -- frame rules ----------------------------------------------------------------------------------------

@pytest.mark.parametrize("kind", ["task.stop", "agent.pause", "agent.pause.reply"])
def test_omission_is_preserved_and_explicit_null_is_rejected(kind: str) -> None:
    frame = signed_example(kind)
    model, _shape = body_model(kind)
    field = "message" if kind.endswith(".reply") else "reason"
    if kind.endswith(".reply"):
        frame["body"] = {"outcome": "agent_exiting"}
    frame["body"].pop(field, None)
    validated = model.validate_python(frame["body"])
    assert model.dump_python(validated, mode="json") == frame["body"]
    validator(body_schema_file(kind)).validate(frame["body"])
    frame["body"][field] = None
    assert not validator(body_schema_file(kind)).is_valid(frame["body"])
    refused = refusal(fixtures.resign(frame), frame)
    assert (refused.code, refused.at) == ("invalid_body", f"/body/{field}")


def test_frame_omission_is_preserved_and_null_is_not_an_absent_value() -> None:
    document = signed_example()
    assert models.Frame.model_validate(document).model_dump(mode="json") == document
    for field in ("authority", "not_before", "causation_id"):
        invalid = dict(document, **{field: None})
        with pytest.raises(ValidationError):
            models.Frame.model_validate(invalid)
        refused = refusal(fixtures.resign(invalid), invalid)
        assert (refused.code, refused.at) == ("malformed", f"/{field}")


@pytest.mark.parametrize("data", [
    b'{"nested":' + b'[' * 1500 + b'0' + b']' * 1500 + b'}',
    b'{"integer":' + b'1' * 5000 + b'}',
    b'{"text":"\\ud800"}',
])
def test_hostile_json_is_refused_before_key_lookup(data: bytes) -> None:
    assert refusal(data, signed_example(), trust=TrustStore("install_example", [])).code == "malformed"


def test_str_naive_times_and_foreign_trust_stores_are_programming_errors() -> None:
    frame = signed_example()
    data = json.dumps(frame).encode()
    receiver = fixtures.default_receiver(frame)
    arguments = dict(trust=fixtures.trust_store(), receiver=receiver, arrived_on=frame["subject"], now=fixtures.NOW,
                     replay=fixtures.replay_holding())
    with pytest.raises(TypeError):
        verify_frame(data.decode(), **arguments)
    with pytest.raises(ValueError, match="time zone"):
        verify_frame(data, **dict(arguments, now=fixtures.NOW.replace(tzinfo=None)))
    with pytest.raises(ValueError, match="installation"):
        verify_frame(data, **dict(arguments, trust=TrustStore("install_peer", [])))
    assert verify_frame(bytearray(data), **arguments).frame.message_id == frame["message_id"]


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
    refused = refusal(fixtures.resign(frame), frame, trust=TrustStore("install_example", []))
    assert (refused.code, refused.at) == ("malformed", f"/{field}")


@pytest.mark.parametrize("kind", ["task.stop", "agent.pause", "agent.pause.reply"])
def test_the_clock_tolerance_applies_at_both_ends_of_the_lifetime(kind: str) -> None:
    frame = signed_example(kind)
    issued_at = parse_timestamp(frame["issued_at"])
    frame["expires_at"] = "9999-12-31T23:59:59.999Z"
    data = fixtures.resign(frame)
    limit = timedelta(seconds=kinds.KINDS[kind.removesuffix(".reply")].expiry_seconds)
    millisecond = timedelta(milliseconds=1)

    def at(moment):
        return {"now": wire_frame.format_timestamp(moment)}

    fixtures.verify(data, frame, at(issued_at + limit + SKEW - millisecond))
    assert refusal(data, frame, at(issued_at + limit + SKEW)).code == "expired"
    fixtures.verify(data, frame, at(issued_at - SKEW))
    assert refusal(data, frame, at(issued_at - SKEW - millisecond)).code == "not_yet_valid"


def test_the_senders_own_expiry_counts_before_the_kinds_lifetime() -> None:
    frame = signed_example()
    frame["expires_at"] = "2026-10-22T04:31:00.000Z"
    data = fixtures.resign(frame)
    fixtures.verify(data, frame, {"now": "2026-10-22T04:31:07.999Z"})
    refused = refusal(data, frame, {"now": "2026-10-22T04:31:08.000Z"})
    assert (refused.code, refused.at) == ("expired", "/expires_at")


def test_timestamp_extremes_do_not_overflow_lifetime_checks() -> None:
    store = fixtures.trust_store(extra_documents=[certificate_record(
        "far-host", roles=("lead_host",), issued_at="9999-01-01T00:00:00.000Z", expires_at="9999-12-31T23:59:59.999Z")])
    frame = signed_example()
    frame["issuer"] = {"installation": "install_example", "team": "team_example", "runtime": "far-host", "membership_epoch": 1,
                       "revocation_epoch": 1, "key_id": "test-worker-key-1"}
    frame["subject"] = "ua.v1.team_example.far-host.work.agent.researcher-2"
    frame["issued_at"] = "9999-12-31T23:59:59.998Z"
    frame["expires_at"] = "9999-12-31T23:59:59.999Z"
    data = json.dumps(sign_frame(frame, fixtures.private_key("test-worker-key-1"))).encode()
    fixtures.verify(data, frame, {"now": frame["issued_at"]}, trust=store)


def test_stop_key_preserves_id_boundaries_and_accepts_maximum_ids() -> None:
    assert idempotency_key("task.stop", "a:b", "c") != idempotency_key("task.stop", "a", "b:c")
    assert idempotency_key("task.stop", "a", "b") == idempotency_key("task.stop", "a", "b")
    frame = signed_example()
    frame["body"].update(task_id="a" * 128, stop_id="b" * 128)
    frame["idempotency_key"] = idempotency_key("task.stop", frame["body"]["task_id"], frame["body"]["stop_id"])
    assert len(frame["idempotency_key"]) <= 256
    fixtures.verify(fixtures.resign(frame), frame)


@pytest.mark.parametrize(("mutation", "code"), [
    ("recipient", "wrong_audience"), ("request", "wrong_issuer"), ("arrival", "subject_mismatch"), ("kind", "wrong_sender"),
    ("inbox", "subject_mismatch"),
])
def test_reply_inboxes_bind_the_destination_request_and_kind(mutation: str, code: str) -> None:
    frame = signed_example("agent.pause.reply")
    context: dict = {"answering": "agent.pause/basic.json"}
    if mutation == "recipient":
        frame["audience"]["runtime"] = "another-host"
        context["receiver"] = {"runtime": "lead-host-1"}
    elif mutation == "request":
        frame.pop("causation_id")
    elif mutation == "arrival":
        context["arrived_on"] = frame["subject"] + ".another"
    elif mutation == "kind":
        frame["kind"] = "agent.pause"
    else:
        frame["subject"] = "_INBOX.another-host.pause_1"
    assert refusal(fixtures.resign(frame), frame, context).code == code


def test_known_kind_cannot_be_carried_by_another_delivery_class() -> None:
    frame = signed_example()
    frame["subject"] = "ua.v1.team_example.lead-host-1.evt.skills_changed"
    assert refusal(fixtures.resign(frame), frame).code == "subject_mismatch"


def test_matching_nats_headers_are_accepted() -> None:
    frame = signed_example()
    headers = {"UA-Kind": frame["kind"], "UA-Id": frame["message_id"], "Nats-Msg-Id": frame["idempotency_key"],
               "traceparent": "00-0af7651916cd43dd8448eb211c80319c-b7ad6b7169203331-01"}
    fixtures.verify(json.dumps(frame).encode(), frame, {"headers": headers})


def test_verification_reports_the_admission_record_and_records_nothing() -> None:
    frame = signed_example()
    data = json.dumps(frame).encode()
    seen = []

    def replay(key):
        seen.append(key)
        return None

    verified = verify_frame(data, trust=fixtures.trust_store(), receiver=fixtures.default_receiver(frame),
                            arrived_on=frame["subject"], now=fixtures.NOW, replay=replay)
    assert seen == [verified.replay_key] and verified.frame_digest == wire_frame.frame_digest(frame)
    # The same frame redelivered is not a replay; other content under the same key is.
    fixtures.verify(data, frame, {"replay": {"digest": verified.frame_digest}})
    assert refusal(data, frame, {"replay": {"digest": "B" * 43}}).code == "replayed"


def test_an_authority_token_binds_to_the_root_task_the_handler_holds() -> None:
    frame = fixtures.example_frame("task.stop", "for-person")
    verified = fixtures.verify(json.dumps(frame).encode(), frame)
    assert verified.authority is not None and verified.authority.certificate.runtime == "gateway-1"
    verified.authority.require_bound_to("root_task", "task_root_7f")
    with pytest.raises(FrameRefused) as refused:
        verified.authority.require_bound_to("root_task", "task_other")
    assert (refused.value.code, refused.value.at) == ("bad_authority", "/authority/bound_to")


def test_a_kind_that_requires_authority_refuses_frames_without_it(monkeypatch) -> None:
    frame = signed_example()
    monkeypatch.setitem(kinds.KINDS, "task.stop", dataclasses.replace(kinds.KINDS["task.stop"], authority="required"))
    refused = refusal(json.dumps(frame).encode(), frame)
    assert (refused.code, refused.at) == ("bad_authority", "/authority")


def test_asyncapi_accepts_complete_frames_and_references_reply_channel_messages() -> None:
    document = json.loads((CONTRACT / "generated/asyncapi.json").read_text())
    location = "https://universal-agents.local/contracts/messages/v1/generated/asyncapi.json"
    resources = SCHEMAS.with_resource(location, DRAFT202012.create_resource(document))
    for path in valid_examples():
        frame = json.loads(path.read_text())
        if frame["protocol"] != kinds.PROTOCOL:
            continue  # newer-minor pruning is a receiver rule, not a strict schema rule
        key = frame["kind"].replace(".", "_")
        payload = ContractValidator({"$ref": f"{location}#/components/messages/{key}/payload"}, registry=resources)
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
