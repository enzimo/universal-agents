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
import hashlib
import importlib.util
import json
import re
from copy import deepcopy
from datetime import timedelta
from pathlib import Path

import pytest
import rfc8785
from cryptography.hazmat.primitives.serialization import Encoding, PublicFormat
from jsonschema import Draft202012Validator, ValidationError as SchemaError, validators
from pydantic import ValidationError
from referencing import Registry
from referencing.jsonschema import DRAFT202012

from custom_messages.enums import ChangeRecordKind, ChangeRefusal
from custom_messages.wire import canonical, frame as wire_frame, kinds, models, topology
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


def checked(kind: str, body: dict):
    """A workflow body survives both language-neutral validation and a generated-model round trip."""

    validator(body_schema_file(kind)).validate(body)
    adapter, _ = body_model(kind)
    result = adapter.validate_python(body)
    assert adapter.dump_python(result, mode="json", exclude_none=True, by_alias=True) == body
    return result


def valid_examples() -> list[Path]:
    return sorted((EXAMPLES / "valid").rglob("*.json"))


def invalid_examples() -> list[Path]:
    return sorted((EXAMPLES / "invalid").glob("*.json"))


def pointed(document: dict, pointer: str):
    value = document
    for step in pointer[1:].split("/"):
        if not isinstance(value, dict) or step not in value:
            return None
        value = value[step]
    return value


def registry_key(frame: dict) -> str:
    """The README's key rule, written out here: the kind, a colon, and the digest of the listed values.

    A reply's single part is its request's key; a copy adds its audience target.
    """

    kind = frame["kind"]
    if kind.endswith(".reply"):
        parts = [fixtures.answered_request(frame).idempotency_key]
    else:
        entry = REGISTRY["kinds"][kind]
        parts = [pointed(frame, pointer) for pointer in entry["idempotency"]]
        if frame["audience"]["target"] in {EXPORT.audience_target(address) for address in entry.get("copy_to", [])}:
            parts.append(frame["audience"]["target"])
    return f"{kind}:{b64url(hashlib.sha256(rfc8785.dumps(parts)).digest())}"


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


def test_export_refuses_a_retry_key_based_on_an_optional_field() -> None:
    registry = deepcopy(REGISTRY)
    registry["kinds"]["tasks.steer"]["idempotency"] = ["/body/replied_message/author"]
    with pytest.raises(EXPORT.ContractError, match="is not supplied by every request"):
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
CONTRACT_SCHEMAS = EXPORT.load_schemas()


def test_covered_record_kinds_are_the_stores_record_kinds() -> None:
    assert kinds.COVERED_RECORD_KINDS == tuple(REGISTRY["covered_record_kinds"])
    assert kinds.COVERED_RECORD_KINDS == tuple(record_kind.value for record_kind in ChangeRecordKind)


def test_outcomes_that_ask_for_a_retry_are_not_stored() -> None:
    """README, "Stored outcomes": a retry with the same key is decided again after these outcomes.

    Steering sent while its request waits gets not_started and is sent again under the same
    steering id (0.9.8's held guidance); a staged change waits for the person's decision.
    """

    assert kinds.RETRY_OUTCOMES == frozenset(REGISTRY["retry_outcomes"])
    assert "not_started" in kinds.KINDS["tasks.steer"].reply_errors and "not_started" in kinds.RETRY_OUTCOMES
    assert "waiting" in kinds.KINDS["staged_changes.apply"].reply_errors and "waiting" in kinds.RETRY_OUTCOMES
    registry = copy.deepcopy(EXPORT.load_yaml("registry.yaml"))
    registry["retry_outcomes"].append("waiting")
    with pytest.raises(EXPORT.ContractError, match="retry_outcomes lists distinct snake_case names"):
        EXPORT.check_registry(registry, TRANSPORT, EXPORT.load_yaml("team-topologies.yaml"), CONTRACT_SCHEMAS)


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
        copies = tuple(EXPORT.audience_target(address) for address in entry.get("copy_to", []))
        assert (spec.copies, spec.idempotency) == (copies, tuple(entry["idempotency"]))
        assert spec.topology_edges == tuple(EXPORT.topology_edges(entry))
        assert spec.authority_binding == EXPORT.binding_spec(entry)
        for address in EXPORT.own_addresses(entry):
            assert spec.senders_by_target.get(EXPORT.audience_target(address), spec.senders) == tuple(EXPORT.address_senders(entry, address))
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
                assert subject.startswith(("ua.v1.{team}.{self}.", "ua.v1._install.{self}.")), (role, subject)
        assert document["subscribe"][0] == "_INBOX.{self}.>"
        for subject in document["subscribe"][1:]:
            prefix = "ua.v1._install.*." if subject.startswith("ua.v1._install.") else "ua.v1.{team}.*."
            assert subject.startswith(prefix) and "*" not in subject.removeprefix(prefix), subject
        receives_requests = False
        for entry in REGISTRY["kinds"].values():
            for address in EXPORT.entry_addresses(entry):
                if role in EXPORT.address_senders(entry, address):
                    assert EXPORT.full_subject(EXPORT.send_pattern(address), "{self}") in document["publish"]
                receives = role in EXPORT.receiving_roles(entry, address, TRANSPORT)
                if entry["class"] not in {"durable", "dead_letter"}:
                    assert (EXPORT.full_subject(address, "*") in document["subscribe"]) == receives, (role, address)
                receives_requests |= receives and entry["class"] == "request_reply"
        expires = f"{TRANSPORT['classes']['request_reply']['max_expiry_seconds'] + TRANSPORT['clock_skew_seconds']}s"
        assert document.get("allow_responses") == ({"max": 1, "expires": expires} if receives_requests else None)


def test_outside_agents_cannot_publish_into_worker_pools() -> None:
    edge = json.loads((CONTRACT / "generated" / "permissions" / "edge_adapter.json").read_text(encoding="utf-8"))
    assert "ua.v1.{team}.{self}.work.pool.*" not in edge["publish"]
    assert "ua.v1.{team}.{self}.work.agent.*" in edge["publish"]


def test_a_receiver_role_gets_only_the_addresses_it_serves() -> None:
    """team services receive task.request's copy on work.team, not the agent and pool inboxes (review I5)."""

    services = json.loads((CONTRACT / "generated" / "permissions" / "team_services.json").read_text(encoding="utf-8"))
    consumers = {subject.split(".")[-1] if not subject.startswith("$JS.ACK.") else subject.split(".")[3]
                 for subject in services["publish"] if subject.startswith("$JS.")}
    assert consumers == {TRANSPORT["consumers"]["team_services"]["name"]}


def test_asyncapi_describes_both_operations_for_every_kind_and_destination() -> None:
    """One channel per address; each kind it carries has a send and a receive operation there."""

    document = json.loads((CONTRACT / "generated" / "asyncapi.json").read_text(encoding="utf-8"))
    operations = document["operations"]
    for name, entry in REGISTRY["kinds"].items():
        key = name.replace(".", "_")
        for address in EXPORT.entry_addresses(entry):
            is_copy = address in entry.get("copy_to", [])
            channel_id = EXPORT.channel_name(address)
            assert document["channels"][channel_id]["address"] == EXPORT.full_subject(address, "{sender}")
            assert key in document["channels"][channel_id]["messages"]
            for action in ("send", "receive"):
                operation = operations[f"{action}_{key}_{'copy_' if is_copy else ''}{EXPORT.audience_target(address)}"]
                assert operation["channel"] == {"$ref": f"#/channels/{channel_id}"}
                assert operation["messages"] == [{"$ref": f"#/channels/{channel_id}/messages/{key}"}]
                assert operation.get("x-ua-copy", False) is is_copy
                if "reply" in entry:
                    assert operation["reply"]["messages"] == [{"$ref": f"#/channels/reply_inbox/messages/{key}_reply"}]
                else:
                    assert "reply" not in operation


def test_reassignment_preserves_the_task_but_changes_the_dispatch_key() -> None:
    original = fixtures.example_frame("task.request", "to-pool")
    replacement = fixtures.example_frame("task.request", "reassignment")
    assert replacement["body"]["task_id"] == original["body"]["task_id"]
    assert replacement["idempotency_key"] != original["idempotency_key"]


def test_a_copy_and_its_original_carry_different_keys_and_the_copy_has_no_receiver_rules() -> None:
    """The broker drops repeats of one Nats-Msg-Id across the whole stream (README, "Durable copies")."""

    original = fixtures.example_frame("task.result", "completed")
    copy_frame = fixtures.example_frame("task.result", "copy-to-team-services")
    assert copy_frame["body"] == original["body"] and copy_frame["audience"]["target"] == "team_services"
    assert copy_frame["idempotency_key"] != original["idempotency_key"]
    fixtures.verify(json.dumps(copy_frame).encode(), copy_frame)
    assert "team_services" not in kinds.KINDS["task.result"].receivers
    assert kinds.KINDS["task.result"].copies == ("team_services",)


@pytest.mark.parametrize(("change", "problem"), [
    ({"idempotency": ["/body/no_such_field"]}, "names no string or integer field"),
    ({"idempotency": ["/body/assumptions"]}, "names no string or integer field"),
    ({"idempotency": "task_result:<task_id>"}, "lists the JSON pointers"),
    ({"copy_to": ["work.agent.{agent}"]}, "two of its addresses deliver to one audience target"),
    ({"copy_to": ["work.pool.{pool}"]}, "copies go only to"),
    ({"receivers": ["lead_host", "gateway", "edge_adapter", "scheduler"]}, "serve none of its addresses"),
    ({"body_rules": [{"unique": "/body/summary"}]}, "is not a unique, requires or any_of rule"),
    ({"body_rules": [{"requires": "/body/no_such_field", "when": [{"at": "/body/sequence", "is": 1}]}]}, "is not a unique"),
    ({"body_rules": [{"any_of": ["/body/summary"]}]}, "is not a unique"),
    ({"authority": "optional"}, "state authority_binding"),
    ({"senders_by_address": {"work.pool.{pool}": ["lead_host"]}}, "narrows the senders of one of its own addresses"),
    ({"topology_edge": ["up", "none"]}, "or none alone"),
    ({"wakes_model": {"by": "outcome.stat", "values": {"completed": "next_turn"}}}, "must name a body oneOf"),
    ({"wakes_model": {"by": "outcome.state", "values": {"completed": "next_turn", "failed": "always"}}}, "must name a body oneOf"),
    ({"body_rules": [{"unique": "/body/outcome/artifacts", "by": "no_such_member"}]}, "is not a unique"),
    ({"body_rules": [{"requires": "/body/summary", "when": [{"at": "/body/outcome/state", "is": "untill"}]}]}, "is not a unique"),
    ({"idempotency": ["/body/summary"]}, "names no part every frame carries"),
    ({"senders": ["gateway"], "senders_by_address": {}}, "not a topology check"),
])
def test_the_generator_refuses_broken_key_parts_copies_and_idle_receivers(change: dict, problem: str) -> None:
    registry = copy.deepcopy(EXPORT.load_yaml("registry.yaml"))
    registry["kinds"]["task.result"].update(change)
    with pytest.raises(EXPORT.ContractError, match=problem):
        EXPORT.check_registry(registry, TRANSPORT, EXPORT.load_yaml("team-topologies.yaml"), EXPORT.load_schemas())


def reply_variant(schemas: dict, kind: str, outcome: str) -> dict:
    """The alternative of a kind's reply schema with this outcome."""

    reply = schemas[REGISTRY["kinds"][kind]["reply"]["body"]]
    return next(variant for variant in reply["oneOf"] if variant["properties"]["outcome"]["const"] == outcome)


def drop_reply_outcome(registry: dict, schemas: dict, kind: str, outcome: str) -> None:
    """Remove an outcome from both the registry's error list and the reply schema, so the two still agree."""

    registry["kinds"][kind]["reply"]["errors"].remove(outcome)
    schemas[registry["kinds"][kind]["reply"]["body"]]["oneOf"].remove(reply_variant(schemas, kind, outcome))


def covered(registry: dict, kind: str, **value) -> None:
    registry["kinds"][kind]["covered_change"] = value


COVERED_CHANGE_BREAKS = {
    "unknown record kind": (lambda r, s: covered(r, "memory.replace", record_kind="memory",
                                                  seen_version="required_for_request_writers"),
                            "covered_change is {record_kind, seen_version}"),
    "unknown version rule": (lambda r, s: covered(r, "memory.replace", record_kind="memory_fact", seen_version="sometimes"),
                             "covered_change is {record_kind, seen_version}"),
    "not a mapping": (lambda r, s: r["kinds"]["memory.replace"].update(covered_change="memory_fact"),
                      "covered_change is {record_kind, seen_version}"),
    "an unknown field": (lambda r, s: covered(r, "memory.undo", record_kind="memory_fact", seen_version="none",
                                              checked=True),
                         "covered_change is {record_kind, seen_version}"),
    "record kinds repeated": (lambda r, s: r["covered_record_kinds"].append("schedule"), "lists distinct snake_case names"),
    "a durable kind": (lambda r, s: covered(r, "task.stop", record_kind="schedule", seen_version="none"),
                       "only request/reply operations"),
    "required but the body may leave it out": (lambda r, s: s["bodies/schedules.pause.schema.json"]["required"].remove("seen_version"),
                                               "seen_version required needs a body whose seen_version is required, not optional"),
    "for request writers but the body requires it": (lambda r, s: s["bodies/memory.replace.schema.json"]["required"].append("seen_version"),
                                                     "is optional, not required"),
    "optional but required": (lambda r, s: covered(r, "knowledge.write", record_kind="knowledge_page", seen_version="required"),
                              "is required, not optional"),
    "none but the body has it": (lambda r, s: s["bodies/memory.create.schema.json"]["properties"].update(
        seen_version={"$ref": "../common.schema.json#/$defs/record_version"}), "is absent, not optional"),
    "the current version is optional": (lambda r, s: reply_variant(s, "heartbeat_items.update", "changed_since_read")["required"].remove(
        "current_version"), "changed_since_read with a required current_version"),
    "no stale refusal": (lambda r, s: drop_reply_outcome(r, s, "objectives.pause", "changed_since_read"),
                         "changed_since_read with a required current_version"),
    "an agent's missing version has no refusal": (lambda r, s: drop_reply_outcome(r, s, "memory.delete", "invalid_request"),
                                                  "so its reply offers invalid_request"),
    "a covered create left unmarked": (lambda r, s: r["kinds"]["memory.create"].pop("covered_change"),
                                       "which only covered changes give"),
    "a key made from the message id": (lambda r, s: r["kinds"]["memory.replace"].update(idempotency=["/message_id"]),
                                       "/message_id changes with every frame"),
    "a key made from the issue time": (lambda r, s: r["kinds"]["schedules.pause"].update(idempotency=["/body/request_id", "/issued_at"]),
                                       "/issued_at changes with every frame"),
}


@pytest.mark.parametrize(("change", "problem"), COVERED_CHANGE_BREAKS.values(), ids=COVERED_CHANGE_BREAKS.keys())
def test_the_generator_refuses_covered_changes_that_disagree_with_their_schemas(change, problem: str) -> None:
    registry = copy.deepcopy(EXPORT.load_yaml("registry.yaml"))
    schemas = copy.deepcopy(CONTRACT_SCHEMAS)
    change(registry, schemas)
    with pytest.raises(EXPORT.ContractError, match=re.escape(problem)):
        EXPORT.check_registry(registry, TRANSPORT, EXPORT.load_yaml("team-topologies.yaml"), schemas)


def rename_ok(registry: dict, schemas: dict) -> None:
    reply_variant(schemas, "tasks.remove", "ok")["properties"]["outcome"]["const"] = "done"
    registry["kinds"]["tasks.remove"]["reply"]["errors"].append("done")


REPLY_ERROR_BREAKS = {
    "a code the reply schema lacks": lambda r, s: r["kinds"]["knowledge.freeze"]["reply"]["errors"].append("page_exists"),
    "an outcome the list leaves out": lambda r, s: r["kinds"]["tasks.remove"]["reply"]["errors"].remove("not_found"),
    "a code listed twice": lambda r, s: r["kinds"]["tasks.remove"]["reply"]["errors"].append("not_found"),
    "a reply without ok": rename_ok,
}


@pytest.mark.parametrize("change", REPLY_ERROR_BREAKS.values(), ids=REPLY_ERROR_BREAKS.keys())
def test_reply_errors_are_the_reply_schemas_outcomes_other_than_ok(change) -> None:
    registry = copy.deepcopy(EXPORT.load_yaml("registry.yaml"))
    schemas = copy.deepcopy(CONTRACT_SCHEMAS)
    change(registry, schemas)
    with pytest.raises(EXPORT.ContractError, match="reply errors .* must be the reply schema's outcomes|one of them ok"):
        EXPORT.check_registry(registry, TRANSPORT, EXPORT.load_yaml("team-topologies.yaml"), schemas)


@pytest.mark.parametrize(("kind", "role", "edge", "given_by_person", "allowed"), [
    ("task.result", "lead", "person", False, True),     # the lead answers the person (forwarded-as-is)
    ("task.question", "lead", "person", False, True),   # the lead asks the person (change-conflict)
    ("task.result", "worker", "up", False, True),       # a worker answers its lead
    ("task.answer", "lead", "down", False, True),       # the lead answers its worker
    ("task.request", "lead", "out", False, True),       # the lead delegates to an outside agent
    ("task.question", "worker", "sideways", False, False),  # hub and spoke: workers do not ask each other
    ("task.result", "worker", "person", False, False),  # delegated work reports through the lead
    ("task.result", "worker", "person", True, True),    # a run of the person's own schedule reports to them
    ("task.progress", "lead", "person", True, False),   # progress has no person edge
    ("access.request", "worker", "up", False, True),    # to the team services: not a topology check
])
def test_edges_follow_the_kind_and_the_senders_role(kind: str, role: str, edge: str, given_by_person: bool,
                                                    allowed: bool) -> None:
    assert topology.edge_allowed(kind, "hub_and_spoke", role, edge, given_by_person=given_by_person) is allowed


@pytest.mark.parametrize("kind", ["agent.resume", "agent.exit", "agent.status", "task.probe"])
def test_a_kind_agent_hosts_send_to_an_agent_names_its_edges(kind: str) -> None:
    """The lead sends these to an agent's address, like agent.pause; none would skip the receiver's edge check."""

    registry = copy.deepcopy(EXPORT.load_yaml("registry.yaml"))
    assert registry["kinds"][kind]["topology_edge"] == "down"
    registry["kinds"][kind]["topology_edge"] = "none"
    with pytest.raises(EXPORT.ContractError, match="so it is a topology check"):
        EXPORT.check_registry(registry, TRANSPORT, EXPORT.load_yaml("team-topologies.yaml"), EXPORT.load_schemas())


def test_a_lead_control_sent_to_several_agents_gets_one_key_per_agent() -> None:
    """One request id resumes two agents; a key without the audience would answer the second from the first's outcome."""

    for kind in ("agent.pause", "agent.resume", "agent.exit"):
        frames = [json.loads(path.read_text(encoding="utf-8")) for path in sorted((EXAMPLES / "valid" / kind).glob("*.json"))]
        assert len({frame["body"]["request_id"] for frame in frames}) == 1, kind
        assert len({frame["audience"]["id"] for frame in frames}) == len(frames) > 1, kind
        assert len({frame["idempotency_key"] for frame in frames}) == len(frames), kind


def test_a_token_binding_must_name_a_field_every_body_carries() -> None:
    registry = copy.deepcopy(EXPORT.load_yaml("registry.yaml"))
    registry["kinds"]["tool.setup.request"]["authority_binding"] = {"type": "root_task", "at": "/body/delivery_ref"}
    with pytest.raises(EXPORT.ContractError, match="names no field every body carries"):
        EXPORT.check_registry(registry, TRANSPORT, EXPORT.load_yaml("team-topologies.yaml"), EXPORT.load_schemas())


GATEWAY_COMMAND = {"type": "command", "at": "/body/request_id", "senders": ["gateway"]}


@pytest.mark.parametrize(("kind", "binding", "problem"), [
    ("tasks.cancel", {"type": "command"}, "give its binding `at`"),                       # a command names its id
    ("tasks.cancel", {"type": ["command", "root_task"]}, "is a binding"),                 # one type per binding
    ("tasks.cancel", {"type": "person", "at": "/body/request_id"}, "is a binding"),       # an unknown type
    ("objectives.create", [{"type": "root_task"}, {"type": "root_task"}], "each with its own type"),
    ("memory.delete", [dict(GATEWAY_COMMAND, senders=["scheduler"]), {"type": "root_task"}], "some of the kind's senders"),
    ("memory.delete", [GATEWAY_COMMAND, {"type": "root_task", "senders": ["lead_host"]}], "worker_host sends it"),
])
def test_token_bindings_name_known_types_and_who_presents_them(kind: str, binding, problem: str) -> None:
    registry = copy.deepcopy(EXPORT.load_yaml("registry.yaml"))
    registry["kinds"][kind]["authority_binding"] = binding
    with pytest.raises(EXPORT.ContractError, match=problem):
        EXPORT.check_registry(registry, TRANSPORT, EXPORT.load_yaml("team-topologies.yaml"), EXPORT.load_schemas())


def test_a_command_token_lasts_no_longer_than_other_tokens() -> None:
    registry = copy.deepcopy(EXPORT.load_yaml("registry.yaml"))
    registry["command_token_max_seconds"] = registry["authority_token_max_seconds"] + 1
    with pytest.raises(EXPORT.ContractError, match="command_token_max_seconds"):
        EXPORT.check_registry(registry, TRANSPORT, EXPORT.load_yaml("team-topologies.yaml"), EXPORT.load_schemas())


def role_permissions(role: str) -> dict:
    return json.loads((CONTRACT / "generated" / "permissions" / f"{role}.json").read_text(encoding="utf-8"))


def test_agent_queues_and_debug_settings_are_reached_only_through_the_team_services() -> None:
    """Plan 13A.4 and 11A.10: the gateway asks the team services, which pass the command on to the agent's host."""

    relayed = ("tasks.list", "tasks.list_finished", "tasks.remove", "tasks.purge", "debug.dump")
    to_agents = {f"ua.v1.{{team}}.{{self}}.ctl.agent.*.{operation}" for operation in relayed}
    to_services = {f"ua.v1.{{team}}.{{self}}.svc.team.{operation}" for operation in relayed}
    gateway, services = role_permissions("gateway"), role_permissions("team_services")
    assert to_services <= set(gateway["publish"]) and not to_agents & set(gateway["publish"])
    assert to_agents <= set(services["publish"]) and not to_services & set(services["publish"])
    for host in ("lead_host", "worker_host"):
        assert "ua.v1.{team}.*.ctl.agent.{agent}.debug.dump" in role_permissions(host)["subscribe"]


def test_the_scheduler_answers_only_run_context_sign_and_reads_no_copies() -> None:
    """Only a scheduler signs a run's token (plan 13A.10); serving svc.team for it gives it nothing else."""

    scheduler = role_permissions("scheduler")
    assert scheduler["subscribe"] == ["_INBOX.{self}.>", "ua.v1.{team}.*.svc.team.run_context.sign"]
    assert not [subject for subject in scheduler["publish"] if subject.startswith("$JS.")]
    assert "ua.v1.{team}.*.svc.team.run_context.sign" not in role_permissions("team_services")["subscribe"]


def carried_tokens(value, at: str):
    """The authority tokens a body carries, with their JSON pointers."""

    if isinstance(value, dict):
        if {"token_id", "bound_to", "signature"} <= set(value):
            yield at, value
        for name, item in value.items():
            yield from carried_tokens(item, f"{at}/{name}")
    elif isinstance(value, list):
        for index, item in enumerate(value):
            yield from carried_tokens(item, f"{at}/{index}")


def test_every_token_an_example_body_carries_verifies() -> None:
    """A token in a body (the scheduler's run token) passes the receivers' own checks of a token.

    Its signer's certificate and role, the person or schedule it names, its signature
    and its lifetime, at the time the frame carrying it was issued.
    """

    trust = fixtures.trust_store()
    checked = []
    for path in valid_examples():
        frame = json.loads(path.read_text(encoding="utf-8"))
        for at, token in carried_tokens(frame["body"], "/body"):
            verified = wire_frame.verify_authority_token(models.AuthorityToken.model_validate(token), token, trust,
                                                         valid_at=parse_timestamp(frame["issued_at"]), at=at)
            checked.append((path.parent.name, verified.certificate.runtime))
    assert ("run_context.sign.reply", "scheduler-1") in checked, checked


def test_a_run_token_names_no_person_exactly_when_the_run_is_system_work() -> None:
    answers = [json.loads(path.read_text(encoding="utf-8"))["body"]
               for path in (EXAMPLES / "valid" / "run_context.sign.reply").glob("*.json")]
    signed = [body for body in answers if body["outcome"] == "ok"]
    assert {body["system_work"] for body in signed} == {True, False}
    assert all(body["system_work"] == ("person" not in body["authority"]) for body in signed)


def test_each_example_command_has_its_own_token() -> None:
    """Plan section 8: a command token is bound to one command; only the team services' relay carries it again."""

    uses: dict[str, list] = {}
    for path in valid_examples():
        frame = json.loads(path.read_text(encoding="utf-8"))
        token = frame.get("authority")
        if token is not None and token["bound_to"]["type"] == "command":
            uses.setdefault(token["token_id"], []).append((frame["kind"], token))
    assert uses
    for token_id, carried in uses.items():
        assert all(use == carried[0] for use in carried), token_id


def test_every_reply_example_names_exactly_one_request() -> None:
    """A reply example finds its request by message id, so no other example may share that id."""

    frames = [json.loads(path.read_text(encoding="utf-8")) for path in valid_examples()]
    ids = [frame["message_id"] for frame in frames]
    shared = sorted({frame["causation_id"] for frame in frames if "causation_id" in frame and ids.count(frame["causation_id"]) != 1})
    assert not shared, shared


def test_every_kind_has_an_invalid_example() -> None:
    """Plan 13A.8: every kind has a full example and invalid examples."""

    refused = set()
    for path in invalid_examples():
        case = json.loads(path.read_text(encoding="utf-8"))
        refused.add(json.loads((EXAMPLES / "valid" / case["base"]).read_text(encoding="utf-8"))["kind"])
    assert set(REGISTRY["kinds"]) <= refused, sorted(set(REGISTRY["kinds"]) - refused)


RESOLVER = EXPORT.ModelWriter(EXPORT.load_schemas())


def variant_values(file: str, node: dict, path: str, found: dict, depth: int = 0) -> None:
    """Each oneOf in a body schema: its JSON path and the constant that names each alternative."""

    while "$ref" in node:
        file, fragment = RESOLVER.resolve(file, node["$ref"])
        node = RESOLVER.node(file, fragment)
    if depth > 32:
        raise AssertionError(f"{file}{path}: schema nesting exceeds the frame limit; example coverage cannot skip it")
    if "oneOf" in node:
        key = EXPORT.discriminator(file, "", node)
        found.setdefault(f"{path}.{key}", set()).update(variant["properties"][key]["const"] for variant in node["oneOf"])
        for variant in node["oneOf"]:
            variant_values(file, variant, path, found, depth + 1)
    if node.get("type") == "array":
        variant_values(file, node["items"], path + "[]", found, depth + 1)
    for name, child in node.get("properties", {}).items():
        variant_values(file, child, f"{path}.{name}", found, depth + 1)


def example_values(value, path: str, wanted: dict, seen: dict) -> None:
    if isinstance(value, dict):
        for name, item in value.items():
            if f"{path}.{name}" in wanted:
                seen.setdefault(f"{path}.{name}", set()).add(item)
            example_values(item, f"{path}.{name}", wanted, seen)
    elif isinstance(value, list):
        for item in value:
            example_values(item, path + "[]", wanted, seen)


@pytest.mark.parametrize("kind", sorted([
    *REGISTRY["kinds"],
    *(f"{name}.reply" for name, entry in REGISTRY["kinds"].items() if "reply" in entry),
]))
def test_every_variant_of_a_body_has_a_valid_example(kind: str) -> None:
    """Request and reply alternatives both belong to the shared conformance corpus."""

    file = body_schema_file(kind)
    wanted: dict = {}
    variant_values(file, json.loads((CONTRACT / "schemas" / file).read_text(encoding="utf-8")), "", wanted)
    seen: dict = {}
    for path in (EXAMPLES / "valid" / kind).glob("*.json"):
        example_values(json.loads(path.read_text(encoding="utf-8"))["body"], "", wanted, seen)
    missing = {place: sorted(values - seen.get(place, set())) for place, values in wanted.items() if values - seen.get(place, set())}
    assert not missing, missing


def test_every_kind_and_reply_has_a_valid_example() -> None:
    examples = {path.parent.name for path in valid_examples()}
    expected = set(REGISTRY["kinds"]) | {f"{name}.reply" for name, entry in REGISTRY["kinds"].items() if "reply" in entry}
    assert expected <= examples, sorted(expected - examples)


# -- state records, installation traffic and dead letters ---------------------------------------------

STATE_EXAMPLES = EXAMPLES / "state"


def state_case(path: Path) -> tuple[dict, bytes]:
    case = json.loads(path.read_text(encoding="utf-8"))
    return case, case["document_text"].encode("utf-8")


@pytest.mark.parametrize("path", sorted((STATE_EXAMPLES / "valid").rglob("*.json")),
                         ids=lambda path: path.relative_to(STATE_EXAMPLES / "valid").as_posix())
def test_valid_state_records_are_accepted(path: Path) -> None:
    document = json.loads(path.read_text(encoding="utf-8"))
    validator("state.schema.json").validate(document)
    validator(REGISTRY["state_records"][document["record"]]["body"]).validate(document["body"])
    verified = fixtures.verify_state(path.read_bytes())
    assert verified.document.record == path.parent.name
    model = kinds.STATE_RECORDS[document["record"]].body_model
    assert model.dump_python(verified.body, mode="json", exclude_none=True, by_alias=True) == document["body"]


@pytest.mark.parametrize("path", sorted((STATE_EXAMPLES / "invalid").glob("*.json")), ids=lambda path: path.stem)
def test_invalid_state_records_are_refused_with_their_code_at_their_place(path: Path) -> None:
    case, data = state_case(path)
    with pytest.raises(FrameRefused) as refused:
        fixtures.verify_state(data, case["context"])
    assert (refused.value.code, refused.value.at) == (case["refusal"], case["at"]), refused.value


def test_every_state_record_has_a_valid_example() -> None:
    trust = {json.loads(path.read_text())["record"] for path in (EXAMPLES / "trust").rglob("*.json") if path.name != "anchors.json"}
    reports = {path.parent.name for path in (STATE_EXAMPLES / "valid").rglob("*.json")}
    assert set(REGISTRY["state_records"]) <= trust | reports


def state_example(name: str) -> dict:
    document = json.loads((STATE_EXAMPLES / "valid" / name).read_text(encoding="utf-8"))
    return {key: value for key, value in document.items() if key not in {"algorithm", "body_digest", "signature"}}


def signed_state(document: dict) -> bytes:
    return json.dumps(sign_state(document, fixtures.private_key(document["writer"]["key_id"]))).encode()


@pytest.mark.parametrize("record", ["agent/lead-busy.json", "placement/lead.json", "team_view/roster.json"])
def test_state_records_cannot_claim_a_team_the_writer_does_not_serve(record: str) -> None:
    document = state_example(record)
    document["key"] = document["key"].replace("team_example", "another_team")
    if "team" in document["body"]:
        document["body"]["team"] = "another_team"
    with pytest.raises(FrameRefused) as refused:
        fixtures.verify_state(signed_state(document))
    assert refused.value.code == "wrong_team"


def test_a_host_cannot_write_another_hosts_liveness() -> None:
    document = state_example("host/lead-host.json")
    document["writer"].update(runtime="worker-host-1", key_id="test-worker-key-1")
    with pytest.raises(FrameRefused) as refused:
        fixtures.verify_state(signed_state(document))
    assert (refused.value.code, refused.value.at) == ("invalid_body", "/body/runtime")


def test_team_view_key_must_name_its_body_entry() -> None:
    document = state_example("team_view/roster.json")
    document["key"] = "team_example.task.lead"
    with pytest.raises(FrameRefused) as refused:
        fixtures.verify_state(signed_state(document))
    assert (refused.value.code, refused.value.at) == ("invalid_body", "/key")


def test_state_reports_are_verified_when_read_not_kept() -> None:
    with pytest.raises(ValueError, match="verify them when read"):
        fixtures.trust_store().add((STATE_EXAMPLES / "valid" / "agent" / "lead-busy.json").read_bytes())
    with pytest.raises(ValueError, match="keep them with add"):
        fixtures.verify_state((EXAMPLES / "trust" / "certificates" / "lead-host-1.json").read_bytes())
    with pytest.raises(ValueError, match="time zone"):
        fixtures.trust_store().verify((STATE_EXAMPLES / "valid" / "agent" / "lead-busy.json").read_bytes(),
                                      now=fixtures.NOW.replace(tzinfo=None))


def test_a_reader_accepts_the_record_it_holds_and_newer_ones_only() -> None:
    """A placement's signed sequence is its fencing token: a copy written back to the bucket is never newer."""

    data = (STATE_EXAMPLES / "valid" / "placement" / "lead.json").read_bytes()
    record = fixtures.verify_state(data)
    held = {"sequence": record.document.sequence, "digest": record.digest}
    fixtures.verify_state(data, {"held": held})
    newer = state_example("placement/lead.json")
    newer.update(sequence=2)
    newer["body"]["reason"] = "rebalanced"
    fixtures.verify_state(signed_state(newer), {"held": held})
    same_sequence = dict(newer, sequence=record.document.sequence)
    with pytest.raises(FrameRefused) as refused:
        fixtures.verify_state(signed_state(same_sequence), {"held": held})
    assert (refused.value.code, refused.value.at) == ("replayed", "/sequence")


@pytest.mark.parametrize("operation", ["join", "renew"])
def test_asyncapi_installation_subjects_match_the_authority_subscription(operation: str) -> None:
    document = json.loads((CONTRACT / "generated/asyncapi.json").read_text())
    channel_id = EXPORT.channel_name(REGISTRY["kinds"][f"host.{operation}"]["address"])
    assert document["channels"][channel_id]["address"] == f"ua.v1._install.{{sender}}.svc.host.{operation}"
    authority = json.loads((CONTRACT / "generated/permissions/installation_authority.json").read_text())
    assert f"ua.v1._install.*.svc.host.{operation}" in authority["subscribe"]


def test_dead_letter_senders_can_publish_raw_copies_under_their_own_id() -> None:
    for role in REGISTRY["kinds"]["dlq.notice"]["senders"]:
        document = json.loads((CONTRACT / f"generated/permissions/{role}.json").read_text())
        assert "ua.v1.{team}.{self}.dlq.raw.*" in document["publish"]
        assert not any("ua.v1.{team}.*.dlq.raw" in subject for subject in document["publish"])


def join_frame(key_id: str = "test-lead-key-1", public_key_of: str | None = None) -> dict:
    """The join example, from lead-host-1 presenting ``public_key_of``'s key under ``key_id``."""

    document = fixtures.example_frame("host.join", "full")
    document["issuer"].update(runtime="lead-host-1", key_id=key_id)
    document["subject"] = "ua.v1._install.lead-host-1.svc.host.join"
    document["body"]["runtime"] = "lead-host-1"
    document["body"]["application_key"] = {"key_id": key_id,
                                           "public_key": fixtures.KEYS[public_key_of or key_id]["public_key"]}
    document["idempotency_key"] = registry_key(document)
    return document


def without_lead_host() -> TrustStore:
    return fixtures.trust_store(remove=["certificates/lead-host-1.json", "sessions/lead.json"])


@pytest.mark.parametrize("known", [False, True])
def test_join_proves_possession_even_when_the_key_is_known(known: bool) -> None:
    store = fixtures.trust_store() if known else without_lead_host()
    document = join_frame()
    signed = json.dumps(sign_frame(document, fixtures.private_key("test-lead-key-1"))).encode()
    verified = fixtures.verify(signed, document, trust=store)
    assert verified.frame.kind == "host.join" and verified.certificate is None
    document = join_frame(public_key_of="test-worker-key-1")
    signed = json.dumps(sign_frame(document, fixtures.private_key("test-lead-key-1"))).encode()
    assert refusal(signed, document, trust=fixtures.trust_store() if known else without_lead_host()).code == (
        "unknown_key" if known else "bad_signature")


def test_known_join_key_id_cannot_be_reassigned_to_another_public_key() -> None:
    document = join_frame(public_key_of="test-worker-key-1")
    signed = json.dumps(sign_frame(document, fixtures.private_key("test-worker-key-1"))).encode()
    refused = refusal(signed, document)
    assert (refused.code, refused.at) == ("unknown_key", "/issuer/key_id")


def test_lost_join_reply_can_be_recovered_after_the_key_becomes_known() -> None:
    document = join_frame()
    data = json.dumps(sign_frame(document, fixtures.private_key("test-lead-key-1"))).encode()
    fixtures.verify(data, document, trust=without_lead_host())
    fixtures.verify(data, document, {"minimum_epochs": [{"installation": "install_example", "membership": 9, "revocation": 9}]})
    assert refusal(data, document, {"trust": {"add": ["extra/lead-host-revoked-after-issue.json"]}}).code == "revoked"


def test_a_join_speaks_only_for_its_host_in_the_installation_scope() -> None:
    document = join_frame()
    document["issuer"]["team"] = "team_example"
    data = json.dumps(sign_frame(document, fixtures.private_key("test-lead-key-1"))).encode()
    assert refusal(data, document).code == "wrong_issuer"


def test_queued_work_is_judged_by_its_certificate_not_the_epoch_minimum() -> None:
    frame = signed_example()
    minimum = {"minimum_epochs": [{"installation": "install_example", "runtime": "lead-host-1", "membership": 4}]}
    fixtures.verify(json.dumps(frame).encode(), frame, minimum)
    pause = fixtures.example_frame("agent.pause")
    assert refusal(json.dumps(pause).encode(), pause, minimum).code == "epoch_too_old"


def test_renewal_rotation_signing_vector() -> None:
    vector = json.loads((CONTRACT / "signing-vectors/key-rotation.json").read_text())
    document = fixtures.example_frame("host.renew", "rotate")
    before = copy.deepcopy(document)
    assert wire_frame.renew_key_proof_bytes(document).hex() == vector["signing_bytes_hex"]
    assert document["body"]["application_key_rotation"]["proof"] == vector["proof"]
    signed = wire_frame.sign_renew_key_proof(document, fixtures.private_key(vector["new_key"]))
    assert sign_frame(signed, fixtures.private_key(vector["old_key"])) == document
    assert document == before, "signing must not mutate the caller's frame"


@pytest.mark.parametrize(("location", "field", "value", "code"), [
    ("body", "renew_id", "changed", "invalid_body"),
    ("body", "certificate_id", "changed", "wrong_issuer"),  # a renewal names the certificate that signs it
    ("body", "profile_sha256", "A" * 43, "invalid_body"),
    ("issuer", "revocation_epoch", 2, "wrong_issuer"),
    ("audience", "id", "another_authority", "invalid_body"),
    ("", "message_id", "01928f3a-7b2c-7709-8000-000000000709", "invalid_body"),
    ("", "expires_at", "2026-10-22T04:31:00.000Z", "invalid_body"),
    ("", "protocol", "1.1-pre", "invalid_body"),
])
def test_new_key_proof_cannot_be_reused_for_a_different_renewal(location: str, field: str, value, code: str) -> None:
    document = fixtures.example_frame("host.renew", "rotate")
    target = document[location] if location else document
    target[field] = value
    assert refusal(fixtures.resign(document), document).code == code


def test_rotation_proof_covers_a_newer_minors_unknown_fields() -> None:
    document = fixtures.example_frame("host.renew", "rotate")
    document["protocol"] = "1.1-pre"
    document["body"]["application_key_rotation"]["future_option"] = "original"
    document = sign_frame(wire_frame.sign_renew_key_proof(document, fixtures.private_key("test-lead-key-2")),
                          fixtures.private_key("test-lead-key-1"))
    fixtures.verify(json.dumps(document).encode(), document)
    document["body"]["application_key_rotation"]["future_option"] = "changed"
    assert refusal(fixtures.resign(document), document).code == "invalid_body"


@pytest.mark.parametrize("missing", ["key", "proof"])
def test_rotation_requires_both_key_and_proof_in_both_validators(missing: str) -> None:
    document = fixtures.example_frame("host.renew", "rotate")
    del document["body"]["application_key_rotation"][missing]
    assert list(validator("bodies/host.renew.schema.json").iter_errors(document["body"]))
    assert refusal(fixtures.resign(document), document).code == "invalid_body"


@pytest.mark.parametrize("bad", ["key", "proof"])
def test_rotation_requires_a_valid_signature_by_the_new_key(bad: str) -> None:
    document = fixtures.example_frame("host.renew", "rotate")
    rotation = document["body"]["application_key_rotation"]
    if bad == "key":
        rotation["key"]["public_key"] = fixtures.KEYS["test-gateway-key-1"]["public_key"]
    else:
        rotation["proof"] = b64url(bytes(64))
    refused = refusal(fixtures.resign(document), document)
    assert (refused.code, refused.at) == ("invalid_body", "/body/application_key_rotation/proof")


def test_rotation_cannot_be_null() -> None:
    document = fixtures.example_frame("host.renew", "rotate")
    document["body"]["application_key_rotation"] = None
    assert list(validator("bodies/host.renew.schema.json").iter_errors(document["body"]))
    assert refusal(fixtures.resign(document), document).code == "invalid_body"


def test_a_host_that_has_not_joined_may_only_ask_to_join_and_read_its_inbox() -> None:
    joining = role_permissions(EXPORT.JOINING)
    assert joining == {"role": EXPORT.JOINING, "publish": ["ua.v1._install.{self}.svc.host.join"],
                       "subscribe": ["_INBOX.{self}.>"]}
    assert EXPORT.JOINING not in REGISTRY["host_roles"]


def renewed_lead_certificate(*, issued_at: str = "2026-10-22T04:30:00.000Z", epoch: int = 4,
                             key_id: str = "test-lead-key-2") -> bytes:
    """The certificate the rotation example's reply carries, issued at ``issued_at``."""

    document = copy.deepcopy(fixtures.example_frame("host.renew.reply", "ok")["body"]["certificate"])
    document["key"] = f"host.install_example.lead-host-1.{epoch}"
    document["body"].update(membership_epoch=epoch, issued_at=issued_at,
                            application_key={"key_id": key_id, "public_key": fixtures.KEYS[key_id]["public_key"]})
    document["issued_at"] = issued_at
    document["sequence"] = 2  # a newer record than any held under the same key
    return json.dumps(sign_state(document, fixtures.private_key(document["writer"]["key_id"]))).encode()


def test_a_replaced_certificate_signs_new_frames_only_during_the_overlap() -> None:
    """Authorization plan 12.5: in-flight frames from the old key are accepted until a short deadline."""

    store = fixtures.trust_store(extra_documents=[renewed_lead_certificate(issued_at="2026-10-22T04:20:00.000Z")])
    pause = fixtures.example_frame("agent.pause")
    assert parse_timestamp(pause["issued_at"]) - parse_timestamp("2026-10-22T04:20:00.000Z") > timedelta(
        seconds=kinds.CERTIFICATE_OVERLAP_SECONDS) + SKEW
    refused = refusal(json.dumps(pause).encode(), pause, trust=store)
    assert (refused.code, refused.at) == ("unknown_key", "/issued_at")
    # Within the overlap the old key still signs new frames.
    store = fixtures.trust_store(extra_documents=[renewed_lead_certificate(issued_at="2026-10-22T04:28:00.000Z")])
    fixtures.verify(json.dumps(pause).encode(), pause, trust=store)
    # Queued work issued before the replacement is still judged by the old certificate.
    stop = signed_example()
    store = fixtures.trust_store(extra_documents=[renewed_lead_certificate(issued_at="2026-10-22T04:35:00.000Z")])
    fixtures.verify(json.dumps(stop).encode(), stop, {"now": "2026-10-22T05:00:00.000Z"}, trust=store)


@pytest.mark.parametrize("epoch", [3, 2])
def test_a_renewed_certificate_needs_a_higher_epoch(epoch: int) -> None:
    """At the same epoch it would overwrite the certificate queued work still needs (host.<installation>.<runtime>.<epoch>)."""

    with pytest.raises(FrameRefused) as refused:
        fixtures.trust_store(extra_documents=[renewed_lead_certificate(epoch=epoch)])
    assert (refused.value.code, refused.value.at) == ("invalid_body", "/body/membership_epoch")


def test_a_renewed_certificate_needs_a_higher_epoch_when_it_arrives_first() -> None:
    store = fixtures.trust_store(remove=["certificates/lead-host-1.json"])
    store.add(renewed_lead_certificate(epoch=2))
    with pytest.raises(FrameRefused) as refused:
        store.add((EXAMPLES / "trust/certificates/lead-host-1.json").read_bytes())
    assert (refused.value.code, refused.value.at) == ("invalid_body", "/body/membership_epoch")


def test_a_rotation_retried_after_a_lost_reply_is_accepted_once_its_certificate_is_known() -> None:
    document = fixtures.example_frame("host.renew", "rotate")
    fixtures.verify(json.dumps(document).encode(), document, trust=fixtures.trust_store(extra_documents=[renewed_lead_certificate()]))
    # The same key id with another public key is not a retry.
    document["body"]["application_key_rotation"]["key"]["public_key"] = fixtures.KEYS["test-gateway-key-1"]["public_key"]
    refused = refusal(fixtures.resign(document), document, trust=fixtures.trust_store(extra_documents=[renewed_lead_certificate()]))
    assert (refused.code, refused.at) == ("invalid_body", "/body/application_key_rotation/key/key_id")


def test_a_new_host_verifies_its_join_reply_from_the_bootstrap_records_alone() -> None:
    """The bootstrap bundle: the authority keys, the authority hosts' certificates and the revocations (README)."""

    bootstrap = fixtures.trust_store(remove=[name for name in fixtures.default_records()
                                             if name not in {"certificates/authority-1.json", "revocations/install_example.json"}])
    reply = fixtures.example_frame("host.join.reply", "ok")
    verified = fixtures.verify(json.dumps(reply).encode(), reply, trust=bootstrap)
    bootstrap.add(json.dumps(verified.raw["body"]["certificate"]).encode())
    assert bootstrap.host_certificate("install_example", "new-host-1", 1) is not None


@pytest.mark.parametrize("operation", ["join", "renew"])
@pytest.mark.parametrize("bad", ["record", "body"])
def test_successful_installation_replies_require_a_certificate(operation: str, bad: str) -> None:
    document = fixtures.example_frame(f"host.{operation}.reply", "ok")
    if bad == "record":
        document["body"]["certificate"] = json.loads((STATE_EXAMPLES / "valid" / "team_view" / "roster.json").read_text())
    else:
        del document["body"]["certificate"]["body"]["application_key"]
    assert list(validator(f"bodies/host.{operation}.reply.schema.json").iter_errors(document["body"]))
    assert refusal(fixtures.resign(document), document).code == "invalid_body"


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
    assert document["idempotency_key"] == registry_key(document)


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


# Body fields a receiver checks against its trust store or the request a reply answers:
# a reply's certificate (its signature, single validity window and what it certifies)
# and a key rotation's new key id and proof (README, "Installation traffic").
RECEIVER_CHECKED = re.compile(r"/body/certificate/(signature|expires_at|body/[a-z_]+)|/body/application_key_rotation/(key/key_id|proof)")


def receiver_only(case: dict) -> bool:
    """Refusals a schema cannot give: they recompute a key, apply a body rule, or compare the body with records.

    README, "Idempotency keys", "What bodies carry" and "Installation traffic".
    """

    kind = json.loads(case["frame_text"])["kind"]
    rules = REGISTRY["kinds"].get(kind, {}).get("body_rules", [])
    places = {rule.get("unique") or rule.get("requires") or rule["any_of"][0].rsplit("/", 1)[0] for rule in rules}
    return case["at"] == "/idempotency_key" or case["at"] in places or RECEIVER_CHECKED.fullmatch(case["at"]) is not None


@pytest.mark.parametrize("path", [p for p in invalid_examples() if RECEIVER_CHECKED.fullmatch(json.loads(p.read_text())["at"])],
                         ids=lambda path: path.stem)
def test_refusals_that_compare_the_body_with_records_pass_the_schema(path: Path) -> None:
    """Only a receiver gives these, so the shared examples are its tests; a schema accepts the body."""

    case = json.loads(path.read_text(encoding="utf-8"))
    document = json.loads(case["frame_text"])
    assert case["refusal"] == "invalid_body"
    assert not list(validator(body_schema_file(document["kind"])).iter_errors(document["body"]))


@pytest.mark.parametrize("path", [p for p in invalid_examples() if json.loads(p.read_text())["refusal"] == "invalid_body"
                                  and not receiver_only(json.loads(p.read_text()))], ids=lambda path: path.stem)
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
    assert len(frame["idempotency_key"]) <= 128
    fixtures.verify(fixtures.resign(frame), frame)


def test_keys_stay_within_128_characters_for_maximum_ids_and_the_longest_kind() -> None:
    longest = max([*REGISTRY["kinds"], *(f"{name}.reply" for name in REGISTRY["kinds"])], key=len)
    assert len(idempotency_key(longest, "a" * 128, 2**53 - 1, None)) <= 128
    assert len(idempotency_key("k" * 80 + ".a.b", "x")) == 128


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
