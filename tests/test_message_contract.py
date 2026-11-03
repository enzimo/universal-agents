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
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey
from cryptography.hazmat.primitives.serialization import Encoding, PublicFormat
from jsonschema import Draft202012Validator, ValidationError as SchemaError, validators
from pydantic import ValidationError
from referencing import Registry
from referencing.jsonschema import DRAFT202012

from custom_messages.enums import ChangeRecordKind, ChangeRefusal
from custom_messages.wire import canonical, frame as wire_frame, kinds, models, topology, trust as wire_trust
from custom_messages.wire.canonical import CanonicalJsonError
from custom_messages.wire.frame import (
    FrameRefused,
    b64url,
    b64url_decode,
    body_model,
    expected_idempotency_key,
    format_timestamp,
    idempotency_key,
    sign_authority_token,
    sign_frame,
    signing_bytes,
    verify_frame,
)
from custom_messages.wire.permissions import HostPlacement, host_permissions, host_subjects
from custom_messages.wire.trust import TrustStore, ed25519_public_key, sign_state, state_signing_bytes
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


def test_the_digest_covers_only_the_contracts_sources(tmp_path: Path) -> None:
    """README, "Versions": hidden files and other kinds of file never change the contract digest."""

    for name in ("README.md", "registry.yaml", "schemas/frame.schema.json", "generated/registry.json"):
        (tmp_path / name).parent.mkdir(parents=True, exist_ok=True)
        (tmp_path / name).write_text(name, encoding="utf-8")
    digest = EXPORT.contract_sha256({}, root=tmp_path)
    for name in (".DS_Store", "registry.yaml~", "notes.txt", ".editor/settings.json", "schemas/.frame.schema.json.swp"):
        (tmp_path / name).parent.mkdir(parents=True, exist_ok=True)
        (tmp_path / name).write_text("stray", encoding="utf-8")
    assert [path.relative_to(tmp_path).as_posix() for path in EXPORT.contract_sources(tmp_path)] == [
        "README.md", "registry.yaml", "schemas/frame.schema.json"]
    assert EXPORT.contract_sha256({}, root=tmp_path) == digest
    (tmp_path / "registry.yaml").write_text("registry.yaml\n", encoding="utf-8")
    assert EXPORT.contract_sha256({}, root=tmp_path) != digest
    attributes = (ROOT / ".gitattributes").read_text(encoding="utf-8")
    assert "contracts/messages/v1/** text eol=lf" in attributes


def test_the_seven_day_cut_off_and_the_one_day_revocations_record_are_pinned() -> None:
    """Owner decision 45 and the revocations record's day: a changed lifetime fails here instead of passing silently."""

    week, day = 7 * 24 * 3600, 24 * 3600
    assert TRANSPORT["classes"]["durable"]["max_expiry_seconds"] == week
    assert {entry["expiry_seconds"] for entry in REGISTRY["kinds"].values() if entry["class"] == "durable"} == {week}
    assert REGISTRY["authority_token_max_seconds"] == week
    records = REGISTRY["state_records"]
    assert (records["agent_session"]["max_lifetime_seconds"], records["block_list"]["max_lifetime_seconds"],
            records["revocations"]["max_lifetime_seconds"]) == (week, week, day)
    assert kinds.STATE_RECORDS["revocations"].max_lifetime_seconds == day


def test_the_compatibility_record_names_the_contract_by_its_digest() -> None:
    """README, "Versions": the SHA-256 of every source path and its bytes, in path order; peers compare it while -pre."""

    digest = hashlib.sha256()
    sources = sorted((path for path in CONTRACT.rglob("*") if path.is_file() and "generated" not in path.relative_to(CONTRACT).parts),
                     key=lambda path: path.relative_to(CONTRACT).as_posix().encode("utf-8"))
    assert CONTRACT / "README.md" in sources and CONTRACT / "registry.yaml" in sources
    for path in sources:
        data = path.read_bytes()
        digest.update(path.relative_to(CONTRACT).as_posix().encode("utf-8") + b"\0" + str(len(data)).encode() + b"\0" + data)
    record = json.loads((CONTRACT / "generated" / "compatibility.json").read_text(encoding="utf-8"))
    assert record["contract_sha256"] == digest.hexdigest()
    assert record["protocol"].endswith("-pre") and "-pre" in record["accepts"]["contract_sha256"]


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
        (lambda t: t["streams"]["UA1_AGENT_WORK_{team}"].pop("max_age_seconds"), "must keep messages"),
        (lambda t: t["classes"]["durable"].update(streams=["UA1_MISSING"]), "unknown stream"),
        (lambda t: t.update(replay_ledger_retention_seconds=t["classes"]["durable"]["max_expiry_seconds"] - 1),
         "replay records"),
    ],
)
def test_transport_settings_that_would_lose_or_replay_messages_are_refused(change, problem) -> None:
    transport = copy.deepcopy(TRANSPORT)
    change(transport)
    with pytest.raises(EXPORT.ContractError, match=problem):
        EXPORT.check_transport(transport)


@pytest.mark.parametrize(
    ("change", "problem"),
    [
        # NATS refuses a stream whose subjects overlap another's.
        (lambda t: t["streams"]["UA1_WORK"].update(subjects=["ua.v1.*.*.work.>"]), "overlap"),
        # Agent work left out of every stream would never be stored.
        (lambda t: t["streams"]["UA1_AGENT_WORK_{team}"].update(subjects=["ua.v1.{team}.*.work.pool.*"]),
         "work.agent.{agent} is stored in exactly one"),
        # Requests are answered at once; a stream holding them would keep every request.
        (lambda t: t["streams"]["UA1_WORK"]["subjects"].append("ua.v1.*.*.svc.team"), "stores request_reply traffic"),
        # A team's stream holds only that team's work.
        (lambda t: t["streams"]["UA1_AGENT_WORK_{team}"]["subjects"].append("ua.v1.*.*.work.agent.extra"),
         "starts ua.v1.{team}."),
        (lambda t: t["streams"].update({"UA1_{team}_{edge}": {"subjects": ["ua.v1.{team}.*.spare"]}}), "may name only"),
        (lambda t: t["consumers"]["edge"].update(stream="UA1_AGENT_WORK_{team}"), "stores its filter's subjects"),
        # An agent's control queue is read on its own, with its own consumer.
        (lambda t: t["streams"]["UA1_AGENT_WORK_{team}"]["subjects"].remove("ua.v1.{team}.*.work.control.*"),
         "work.control.{agent} is stored in exactly one"),
        (lambda t: t.pop("agent_queues"), "agent_queues names the inbox and the control queue"),
        (lambda t: t["agent_queues"].update(control="work.agent.{agent}"), "agent_queues names the inbox and the control queue"),
        (lambda t: t["consumers"].pop("agent_control"), "has exactly one consumer"),
        (lambda t: t["consumers"]["agent_control"].update(filter=t["consumers"]["agent_inbox"]["filter"]),
         "has exactly one consumer, so the host reads it on its own"),
    ],
)
def test_streams_store_each_queued_address_once_and_nothing_else(change, problem) -> None:
    transport = copy.deepcopy(TRANSPORT)
    change(transport)
    with pytest.raises(EXPORT.ContractError, match=re.escape(problem)):
        EXPORT.check_transport(transport)


def test_streams_store_a_full_size_frame_with_the_largest_headers_senders_may_set() -> None:
    """README, "NATS headers": JetStream counts headers in a stream's maximum message size."""

    assert EXPORT.largest_header_block(CONTRACT_SCHEMAS) == 891 <= TRANSPORT["limits"]["max_header_bytes"] == 1024
    readme = (CONTRACT / "README.md").read_text(encoding="utf-8")
    assert "is 891 bytes" in readme and "is 1,024" in readme
    for change, problem in (
            (lambda t: t["limits"].update(max_header_bytes=890), "max_header_bytes holds the largest header block"),
            (lambda t: t["streams"]["UA1_WORK"].update(max_message_bytes=262144), "UA1_WORK's max_message_bytes"),
            (lambda t: t["limits"].update(nats_max_payload_bytes=262144), "fit nats_max_payload_bytes")):
        transport = copy.deepcopy(TRANSPORT)
        change(transport)
        with pytest.raises(EXPORT.ContractError, match=problem):
            EXPORT.check_message_sizes(transport, CONTRACT_SCHEMAS)


def test_a_team_wide_address_is_received_by_the_same_roles_for_every_kind() -> None:
    """The scheduler answers only run_context.sign, so it listens at svc.scheduler, not svc.team."""

    registry = copy.deepcopy(EXPORT.load_yaml("registry.yaml"))
    registry["kinds"]["run_context.sign"]["address"] = "svc.team"
    with pytest.raises(EXPORT.ContractError, match="every kind sent there has those receivers"):
        EXPORT.check_registry(registry, TRANSPORT, EXPORT.load_yaml("team-topologies.yaml"), CONTRACT_SCHEMAS)


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


def consumer_grants() -> set[str]:
    """Every JetStream subject a reader may be granted: each consumer by its exact name, ids still placeholders."""

    grants = set()
    for consumer in TRANSPORT["consumers"].values():
        stream, name = consumer["stream"], consumer["name"]
        grants |= {f"$JS.API.CONSUMER.INFO.{stream}.{name}", f"$JS.API.CONSUMER.MSG.NEXT.{stream}.{name}",
                   f"$JS.ACK.{stream}.{name}.>"}
    return grants


def test_permissions_name_exact_receivers_and_publish_only_under_the_senders_own_id() -> None:
    """A host reads only the queues and control requests of the ids membership code fills in (README, "Grants ...")."""

    folder = CONTRACT / "generated" / "permissions"
    assert "team_grants" not in TRANSPORT
    management = EXPORT.stream_management_grants(TRANSPORT)
    buckets = EXPORT.key_value_grants(TRANSPORT)
    for role in REGISTRY["host_roles"]:
        document = json.loads((folder / f"{role}.json").read_text(encoding="utf-8"))
        grants = consumer_grants() | management.get(role, set()) | buckets.get(role, set())
        for subject in document["publish"]:
            if subject.startswith(("$JS.", "$KV.")):
                assert subject in grants, (role, subject)
            else:
                assert subject.startswith(("ua.v1.{team}.{self}.", "ua.v1._install.{self}.")), (role, subject)
        assert document["subscribe"][0] == "_INBOX.{self}.>"
        for subject in document["subscribe"][1:]:
            prefix = "ua.v1._install.*." if subject.startswith("ua.v1._install.") else "ua.v1.{team}.*."
            # Only the sender slot is a wildcard; every receiver id is a placeholder.
            assert subject.startswith(prefix) and "*" not in subject.removeprefix(prefix), subject
        receives_requests = False
        for entry in REGISTRY["kinds"].values():
            for address in EXPORT.entry_addresses(entry):
                if role in EXPORT.address_senders(entry, address):
                    assert EXPORT.full_subject(EXPORT.send_pattern(address), "{self}") in document["publish"]
                receives = role in EXPORT.receiving_roles(entry, address, TRANSPORT)
                if entry["class"] not in {"durable", "dead_letter"}:
                    granted = EXPORT.full_subject(address, "*")
                    assert (granted in document["subscribe"]) == receives, (role, address)
                receives_requests |= receives and entry["class"] == "request_reply"
        expires = f"{TRANSPORT['classes']['request_reply']['max_expiry_seconds'] + TRANSPORT['clock_skew_seconds']}s"
        assert document.get("allow_responses") == ({"max": 1, "expires": expires} if receives_requests else None)


def expanded(role: str, placement: HostPlacement | None = None, **ids: list[str]) -> set[str]:
    """A role's permission subjects filled for one host, as membership code fills them (README, "Filling the templates").

    Without ``placement``, the host serves team_example and runs or serves ``ids``
    (``agent``, ``pool`` and ``edge`` lists) there.
    """

    document = json.loads((CONTRACT / "generated" / "permissions" / f"{role}.json").read_text(encoding="utf-8"))
    if placement is None:
        placement = HostPlacement(f"{role}-1", ("team_example",),
                                  **{f"{name}s": {"team_example": tuple(values)} for name, values in ids.items()})
    filled = host_permissions(document, placement)
    return set(filled["publish"]) | set(filled["subscribe"])


def test_a_host_holds_no_grant_on_another_hosts_agents() -> None:
    """The worker host running researcher-2 cannot read the lead's queue or hear the lead's control requests."""

    worker = expanded("worker_host", agent=["researcher-2"], pool=["researcher"])
    lead = TRANSPORT["consumers"]["agent_inbox"]["name"].format(team="team_example", agent="lead")
    own = TRANSPORT["consumers"]["agent_inbox"]["name"].format(team="team_example", agent="researcher-2")
    stream = "UA1_AGENT_WORK_team_example"
    assert f"$JS.API.CONSUMER.MSG.NEXT.{stream}.{own}" in worker
    assert "ua.v1.team_example.*.ctl.agent.researcher-2" in worker
    assert not {subject for subject in worker if lead in subject or subject.endswith(".ctl.agent.lead")}
    # Bucket watches read their own consumers' information by any name; no queue consumer is a wildcard.
    assert not {subject for subject in worker if subject.endswith(".*") and "CONSUMER" in subject and ".KV_" not in subject}
    # Gaining an agent is a new credential, with that agent's subjects added.
    moved = expanded("worker_host", agent=["researcher-2", "lead"], pool=["researcher"])
    assert f"$JS.API.CONSUMER.MSG.NEXT.{stream}.{lead}" in moved - worker


def test_a_host_serving_two_teams_gets_grants_only_on_each_agents_own_team() -> None:
    """Team A's researcher and team B's researcher are different agents; a host running one never reaches the other."""

    placement = HostPlacement("worker-host-1", ("team_a", "team_b"),
                              agents={"team_a": ("researcher",)}, pools={"team_b": ("writer",)})
    subjects = expanded("worker_host", placement)
    inbox = TRANSPORT["consumers"]["agent_inbox"]["name"].format(team="team_a", agent="researcher")
    assert f"$JS.API.CONSUMER.MSG.NEXT.UA1_AGENT_WORK_team_a.{inbox}" in subjects
    assert "ua.v1.team_a.*.ctl.agent.researcher" in subjects
    assert not {subject for subject in subjects if "researcher" in subject and "team_b" in subject}
    assert not {subject for subject in subjects if "writer" in subject and "team_a" in subject}
    # A subject that names no agent, pool or edge serves every team of the host.
    assert {subject for subject in subjects if subject.startswith("ua.v1.team_b.worker-host-1.")}


@pytest.mark.parametrize("role", sorted(path.stem for path in (CONTRACT / "generated" / "permissions").glob("*.json")))
def test_every_roles_templates_fill_to_exact_ids(role: str) -> None:
    placement = HostPlacement(f"{role}-1", ("team_a", "team_b"), agents={"team_a": ("lead",), "team_b": ("lead",)},
                              pools={"team_a": ("researcher",)}, edges={"team_b": ("web",)})
    subjects = expanded(role, placement)
    assert subjects and not {subject for subject in subjects if "{" in subject}


def test_a_credential_fits_the_limit_servers_read_and_the_jwt_field_allows() -> None:
    """README, "Credential size": max_credential_bytes and the jwt field's maxLength are one limit."""

    schemas = EXPORT.load_schemas()
    jwt = schemas["common.schema.json"]["$defs"]["connection_credential"]["properties"]["jwt"]
    assert jwt["maxLength"] == TRANSPORT["limits"]["max_credential_bytes"] == 65536
    transport = copy.deepcopy(TRANSPORT)
    transport["limits"]["max_credential_bytes"] = 16384
    with pytest.raises(EXPORT.ContractError, match="max_credential_bytes"):
        EXPORT.check_message_sizes(transport, schemas)


def test_a_placement_names_only_teams_its_host_serves() -> None:
    with pytest.raises(ValueError, match="teams the host does not serve"):
        HostPlacement("worker-host-1", ("team_a",), agents={"team_b": ("researcher",)})
    with pytest.raises(ValueError, match="cannot fill"):
        host_subjects("ua.v1.{team}.{installation}.x", HostPlacement("worker-host-1", ("team_a",)))


def longest_id(definition: str) -> int:
    """The most characters a common.schema.json id definition allows (its pattern's {0,n} plus the first character)."""

    defs = CONTRACT_SCHEMAS["common.schema.json"]["$defs"]
    while "pattern" not in defs[definition]:
        definition = defs[definition]["$ref"].rsplit("/", 1)[-1]
    found = re.search(r"\{0,(\d+)\}\$$", defs[definition]["pattern"])
    assert found, definition
    return int(found.group(1)) + 1


# Each permission placeholder, filled with the longest id its definition allows.
PLACEHOLDER_IDS = {"team": "team_id", "agent": "agent_id", "pool": "agent_type", "edge": "token_id", "self": "runtime_id"}


def permission_bytes(subjects: list[str]) -> int:
    """Bytes of permission JSON for these templates, each placeholder filled with its longest id."""

    def longest(match: re.Match[str]) -> str:
        return "a" * longest_id(PLACEHOLDER_IDS[match.group(1)])

    return sum(len(re.sub(r"\{([a-z_]+)\}", longest, subject)) + len('"",') for subject in subjects)


def credential_growth(role: str, placeholder: str) -> int:
    """Bytes of permission JSON one more ``placeholder`` id adds to ``role``'s credential, every id as long as allowed."""

    document = json.loads((CONTRACT / "generated" / "permissions" / f"{role}.json").read_text(encoding="utf-8"))
    return permission_bytes([template for template in document["publish"] + document["subscribe"]
                             if "{" + placeholder + "}" in template])


def test_the_readme_states_what_each_hosted_agent_and_pool_adds_to_a_credential() -> None:
    readme = (CONTRACT / "README.md").read_text(encoding="utf-8")
    assert (longest_id("team_id"), longest_id("agent_id"), longest_id("agent_type")) == (24, 32, 24)
    agent = max(credential_growth(role, "agent") for role in ("lead_host", "worker_host"))
    pool = max(credential_growth(role, "pool") for role in ("lead_host", "worker_host"))
    assert f"{agent} bytes of permission JSON" in readme and f"{pool} bytes with" in readme
    assert f"about {-(-agent * 4 // 3):,} bytes" in readme


def test_a_credential_names_about_fifty_agents_beside_ten_pools_with_the_longest_ids() -> None:
    """README, "Credential size": what the id caps buy, counted as a NATS user JWT.

    The count allows 1,024 bytes for the JWT's header, claims and signature, and
    base64url turns three bytes into four.
    """

    document = json.loads((CONTRACT / "generated" / "permissions" / "lead_host.json").read_text(encoding="utf-8"))
    subjects = document["publish"] + document["subscribe"]
    fixed = permission_bytes([s for s in subjects if not re.search(r"\{(agent|pool|edge)\}", s)])
    agent, pool = credential_growth("lead_host", "agent"), credential_growth("lead_host", "pool")
    room = TRANSPORT["limits"]["max_credential_bytes"] * 3 // 4 - 1024 - fixed - 10 * pool
    assert room // agent in range(45, 50)
    readme = " ".join((CONTRACT / "README.md").read_text(encoding="utf-8").split())
    assert "about 45 agents beside 10 worker pools" in readme


def test_only_the_topology_owner_sets_up_streams_and_only_the_team_services_withdraw_queued_work() -> None:
    """README, "Who sets up the streams" and "Withdrawing queued work"."""

    assert (TRANSPORT["topology_owner"], TRANSPORT["withdrawal"]["role"]) == ("installation_authority", "team_services")
    buckets = EXPORT.key_value_grants(TRANSPORT)
    for role in REGISTRY["host_roles"]:
        # Bucket reads and watches are key-value grants (README, "Key-value grants"); the queues are what count here.
        publish = [subject for subject in role_permissions(role)["publish"] if subject not in buckets.get(role, set())]
        creates = {subject for subject in publish if ".CREATE." in subject or ".UPDATE." in subject or ".DELETE." in subject}
        deletes = {subject for subject in creates if ".MSG.DELETE." in subject}
        reads = {subject for subject in publish if ".MSG.GET." in subject or ".DIRECT.GET." in subject}
        if role == "installation_authority":
            assert "$JS.API.STREAM.CREATE.UA1_AGENT_WORK_{team}" in publish and not deletes and not reads
            assert "$JS.API.CONSUMER.CREATE.UA1_AGENT_WORK_{team}.*.>" in publish
            assert {f"$JS.API.STREAM.CREATE.KV_{bucket}" for bucket in TRANSPORT["key_value"]} <= set(publish)
        elif role == "team_services":
            # Their own team's streams only: no grant reaches the shared UA1_WORK or another team's dead letters.
            assert deletes == creates == {"$JS.API.STREAM.MSG.DELETE.UA1_AGENT_WORK_{team}",
                                          "$JS.API.STREAM.MSG.DELETE.UA1_DLQ_{team}"}
            assert reads == {"$JS.API.STREAM.MSG.GET.UA1_AGENT_WORK_{team}", "$JS.API.STREAM.MSG.GET.UA1_DLQ_{team}"}
        else:
            assert not creates and not reads, role
    for change, problem in ((lambda t: t.update(topology_owner="nobody"), "name host roles"),
                            (lambda t: t["withdrawal"].update(streams=["UA1_DLQ_{team}"]), "only from the durable class's streams"),
                            (lambda t: t["withdrawal"].update(streams=["UA1_WORK"]), "only in per-team streams")):
        transport = copy.deepcopy(TRANSPORT)
        change(transport)
        with pytest.raises(EXPORT.ContractError, match=problem):
            EXPORT.check_stream_management(EXPORT.load_yaml("registry.yaml"), transport)


def test_outside_agents_cannot_publish_into_worker_pools() -> None:
    edge = json.loads((CONTRACT / "generated" / "permissions" / "edge_adapter.json").read_text(encoding="utf-8"))
    assert "ua.v1.{team}.{self}.work.pool.*" not in edge["publish"]
    # Tasks, results and answers to an agent's inbox; claims and progress to its control queue.
    assert {"ua.v1.{team}.{self}.work.agent.*", "ua.v1.{team}.{self}.work.control.*"} <= set(edge["publish"])


# README, "An agent's two queues": the inbox holds what needs a free slot, the control queue the rest.
INBOX_KINDS = {"task.request", "task.result", "task.question", "task.answer", "question.closed", "credential.form.result",
               "tool.setup.result", "wait.ended"}
CONTROL_KINDS = {"task.stop", "task.steer", "custody.moved", "custody.confirmed", "access.decision", "task.progress",
                 "task.claimed", "task.handback", "task.account", "task.report", "delivery.refused", "ledger.fact"}


def test_an_agents_control_queue_holds_every_kind_its_host_takes_at_once() -> None:
    """A stop, a steer, a custody renewal or an access decision never waits behind work the agent cannot start yet."""

    queues = TRANSPORT["agent_queues"]
    assert queues == {"inbox": "work.agent.{agent}", "control": "work.control.{agent}"}
    queued = {name: spec.addresses["agent"] for name, spec in kinds.KINDS.items() if "agent" in spec.addresses}
    assert {name for name, address in queued.items() if address == queues["inbox"]} == INBOX_KINDS
    assert {name for name, address in queued.items() if address == queues["control"]} == CONTROL_KINDS
    for name in INBOX_KINDS:
        assert kinds.KINDS[name].execution_slot and not kinds.KINDS[name].accepted_while_paused, name
    for name in CONTROL_KINDS:
        assert not kinds.KINDS[name].execution_slot and kinds.KINDS[name].accepted_while_paused, name
    # Each queue has its own consumer, and an agent's host may read both.
    for role in ("lead_host", "worker_host"):
        publish = role_permissions(role)["publish"]
        for consumer in ("agent_inbox", "agent_control"):
            spec = TRANSPORT["consumers"][consumer]
            assert f"$JS.API.CONSUMER.MSG.NEXT.{spec['stream']}.{spec['name']}" in publish
            assert f"$JS.ACK.{spec['stream']}.{spec['name']}.>" in publish


@pytest.mark.parametrize(
    ("change", "problem"),
    [
        # In the inbox a custody renewal for the task holding the slot could wait behind new tasks.
        (lambda r: r["kinds"]["custody.confirmed"].update(address=["work.agent.{agent}", "work.edge.{edge}"]),
         "goes to the agent's control queue"),
        # On the control queue a new task would be taken while no slot is free.
        (lambda r: r["kinds"]["task.answer"].update(address=["work.control.{agent}", "work.edge.{edge}"]),
         "waits in the agent's inbox"),
        (lambda r: r["kinds"]["task.stop"]["admission"].update(accepted_while_paused=False), "accepted while paused"),
        (lambda r: r["kinds"]["task.answer"]["admission"].update(accepted_while_paused=True), "does not read its inbox"),
    ],
)
def test_a_kind_sent_to_an_agent_waits_in_its_inbox_only_when_it_needs_a_free_slot(change, problem) -> None:
    registry = copy.deepcopy(EXPORT.load_yaml("registry.yaml"))
    change(registry)
    with pytest.raises(EXPORT.ContractError, match=re.escape(problem)):
        EXPORT.check_registry(registry, TRANSPORT, EXPORT.load_yaml("team-topologies.yaml"), EXPORT.load_schemas())


def test_a_receiver_role_gets_only_the_addresses_it_serves() -> None:
    """team services receive task.request's copy on work.team, not the agent and pool inboxes (README, "Durable copies")."""

    services = json.loads((CONTRACT / "generated" / "permissions" / "team_services.json").read_text(encoding="utf-8"))
    buckets = EXPORT.key_value_grants(TRANSPORT)["team_services"]
    consumers = {subject.split(".")[-1] if not subject.startswith("$JS.ACK.") else subject.split(".")[3]
                 for subject in services["publish"] if subject.startswith(("$JS.API.CONSUMER.", "$JS.ACK."))
                 and subject not in buckets}
    # Their inbox's copies and their team's dead-letter notices (README, "Dead letters").
    assert consumers == {TRANSPORT["consumers"]["team_services"]["name"], TRANSPORT["consumers"]["dead_letters"]["name"]}


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
    ({"body_rules": [{"unique": "/body/summary"}]}, "is not a unique, requires, any_of, allowed or ends_within rule"),
    ({"body_rules": [{"requires": "/body/no_such_field", "when": [{"at": "/body/sequence", "is": 1}]}]}, "is not a unique"),
    ({"body_rules": [{"any_of": ["/body/summary"]}]}, "is not a unique"),
    ({"authority": "optional"}, "state authority_binding"),
    ({"senders_by_address": {"work.pool.{pool}": ["lead_host"]}}, "narrows the senders of one of its own addresses"),
    ({"topology_edge": ["up", "none"]}, "or none alone"),
    ({"wakes_model": {"by": "outcome.stat", "values": {"completed": "next_turn"}}}, "must name a body oneOf"),
    ({"wakes_model": {"by": "outcome.state", "values": {"completed": "next_turn", "failed": "always"}}}, "must name a body oneOf"),
    ({"body_rules": [{"unique": "/body/outcome/artifacts", "by": "no_such_member"}]}, "is not a unique"),
    ({"body_rules": [{"requires": "/body/summary", "when": [{"at": "/body/outcome/state", "is": "untill"}]}]}, "is not a unique"),
    # An allowed rule names values its field can hold, each once, under at least one condition.
    ({"body_rules": [{"allowed": "/body/outcome/state", "values": ["complete"], "when": [{"at": "/body/sequence", "is": 1}]}]},
     "is not a unique"),
    ({"body_rules": [{"allowed": "/body/outcome/state", "values": ["completed", "completed"],
                      "when": [{"at": "/body/sequence", "is": 1}]}]}, "is not a unique"),
    ({"body_rules": [{"allowed": "/body/outcome/state", "values": ["completed"], "when": []}]}, "is not a unique"),
    ({"body_rules": [{"allowed": "/body/no_such_field", "values": ["completed"], "when": [{"at": "/body/sequence", "is": 1}]}]},
     "is not a unique"),
    ({"idempotency": ["/body/summary"]}, "names no part every frame carries"),
    ({"senders": ["gateway"], "senders_by_address": {}}, "not a topology check"),
])
def test_the_generator_refuses_broken_key_parts_copies_and_idle_receivers(change: dict, problem: str) -> None:
    registry = copy.deepcopy(EXPORT.load_yaml("registry.yaml"))
    registry["kinds"]["task.result"].update(change)
    with pytest.raises(EXPORT.ContractError, match=problem):
        EXPORT.check_registry(registry, TRANSPORT, EXPORT.load_yaml("team-topologies.yaml"), EXPORT.load_schemas())


@pytest.mark.parametrize("rule", [
    {"ends_within": "/body/custody_window/ends_at", "of": "/issued_at", "seconds": 604800},
    {"ends_within": "/body/access_list/ends_at", "of": "/body/access_list/issued_at", "seconds": 1},
])
def test_the_generator_accepts_an_ends_within_rule_between_timestamps(rule: dict) -> None:
    registry = copy.deepcopy(EXPORT.load_yaml("registry.yaml"))
    registry["kinds"]["custody.confirmed"]["body_rules"] = [rule]
    EXPORT.check_registry(registry, TRANSPORT, EXPORT.load_yaml("team-topologies.yaml"), EXPORT.load_schemas())


@pytest.mark.parametrize("rule", [
    {"ends_within": "/body/task_id", "of": "/issued_at", "seconds": 60},                         # not a timestamp
    {"ends_within": "/body/custody_window/ends_at", "of": "/body/task_id", "seconds": 60},       # nor this
    {"ends_within": "/body/custody_window/ends_at", "of": "/subject", "seconds": 60},            # a frame field, not a time
    {"ends_within": "/body/custody_window/ends_at", "of": "/issued_at", "seconds": 0},           # no span
    {"ends_within": "/body/custody_window/ends_at", "of": "/issued_at", "seconds": "604800"},    # not a whole number
    {"ends_within": "/body/custody_window/ends_at", "of": "/body/custody_window/ends_at", "seconds": 60},
    {"ends_within": "/body/custody_window/ends_at", "of": "/issued_at"},
])
def test_the_generator_refuses_a_broken_ends_within_rule(rule: dict) -> None:
    registry = copy.deepcopy(EXPORT.load_yaml("registry.yaml"))
    registry["kinds"]["custody.confirmed"]["body_rules"] = [rule]
    with pytest.raises(EXPORT.ContractError, match="is not a unique, requires, any_of, allowed or ends_within rule"):
        EXPORT.check_registry(registry, TRANSPORT, EXPORT.load_yaml("team-topologies.yaml"), EXPORT.load_schemas())


def test_an_allowed_rule_checks_a_single_value_and_each_item_of_a_list() -> None:
    """README, "What bodies carry": under its conditions the field, or each item of a list, holds one of the values."""

    spec = dataclasses.replace(kinds.KINDS["task.result"], body_rules=(
        {"allowed": "/body/outcome/state", "values": ["completed"], "when": [{"at": "/body/sequence", "is": 2}]},
        {"allowed": "/body/tags", "values": ["a", "b"], "when": [{"at": "/body/sequence", "is": 2}]},
    ))
    check = wire_frame._check_body_rules  # noqa: SLF001 - the rule check itself, without a signed frame
    check({"body": {"sequence": 1, "outcome": {"state": "failed"}}}, spec)  # its condition does not hold
    check({"body": {"sequence": 2, "outcome": {"state": "completed"}, "tags": ["b", "a"]}}, spec)
    check({"body": {"sequence": 2}}, spec)  # an absent field holds no other value
    for body, at in (({"sequence": 2, "outcome": {"state": "failed"}}, "/body/outcome/state"),
                     ({"sequence": 2, "outcome": {"state": "completed"}, "tags": ["a", "c"]}, "/body/tags")):
        with pytest.raises(FrameRefused) as refused:
            check({"body": body}, spec)
        assert (refused.value.code, refused.value.at) == ("invalid_body", at)


def test_an_ends_within_rule_allows_the_whole_span_and_not_a_millisecond_more() -> None:
    """README, "What bodies carry": a custody window ends at most 7 days after the frame that carries it was issued."""

    spec = kinds.KINDS["custody.confirmed"]
    check = wire_frame._check_body_rules  # noqa: SLF001 - the rule check itself, without a signed frame

    def frame(ends_at: str) -> dict:
        return {"issued_at": "2026-10-22T04:30:00.000Z",
                "body": {"custody_window": {"renewed_by": "reconnection", "ends_at": ends_at}}}

    check(frame("2026-10-29T04:30:00.000Z"), spec)
    check({"issued_at": "2026-10-22T04:30:00.000Z", "body": {"custody_window": {"renewed_by": "host_record", "seconds": 300}}},
          spec)  # a window renewed by host record writes has no end to bound
    with pytest.raises(FrameRefused) as refused:
        check(frame("2026-10-29T04:30:00.001Z"), spec)
    assert (refused.value.code, refused.value.at) == ("invalid_body", "/body/custody_window/ends_at")


def drop_notice_reason(_registry: dict, _transport: dict, schemas: dict) -> None:
    schemas["bodies/dlq.notice.schema.json"]["properties"]["reason"]["enum"].remove("bad_authority")


def add_temporary_notice_reason(_registry: dict, _transport: dict, schemas: dict) -> None:
    schemas["bodies/dlq.notice.schema.json"]["properties"]["reason"]["enum"].append("revocations_not_current")


def temporary_flag(value: object):
    def change(registry: dict, _transport: dict, _schemas: dict) -> None:
        registry["verification_refusals"][0]["temporary"] = value
    return change


def host_ttl(ttl: int | None):
    def change(registry: dict, _transport: dict, _schemas: dict) -> None:
        registry["state_records"]["host"].pop("ttl_seconds")
        if ttl is not None:
            registry["state_records"]["host"]["ttl_seconds"] = ttl
    return change


def proof_of_possession_on(kind: str):
    def change(registry: dict, _transport: dict, _schemas: dict) -> None:
        registry["kinds"][kind]["verification"] = "proof_of_possession"
    return change


def overlap(seconds: int):
    def change(registry: dict, _transport: dict, _schemas: dict) -> None:
        registry["certificate_overlap_seconds"] = seconds
    return change


def scope_pattern(_registry: dict, _transport: dict, schemas: dict) -> None:
    schemas["common.schema.json"]["$defs"]["scope_id"]["pattern"] = "^[A-Za-z0-9][A-Za-z0-9_-]{0,63}$"


def pool_token(registry: dict, _transport: dict, _schemas: dict) -> None:
    del registry["kinds"]["task.request"]["authority_by_address"]


def notice_reader(registry: dict, _transport: dict, _schemas: dict) -> None:
    registry["kinds"]["dlq.notice"]["receivers"] = ["team_services", "installation_authority"]


@pytest.mark.parametrize(("change", "problem"), [
    (drop_notice_reason, "names every verification refusal"),
    (add_temporary_notice_reason, "names no temporary refusal"),
    (temporary_flag(False), "optionally temporary: true"),
    (host_ttl(None), "ttl_seconds is its bucket's"),
    (host_ttl(30), "ttl_seconds is its bucket's"),
    (proof_of_possession_on("agent.pause"), "proves possession"),
    (overlap(0), "certificate_overlap_seconds"),
    (overlap(86400), "certificate_overlap_seconds"),
    (scope_pattern, "scope_id must allow"),
    (notice_reader, "serve none of its addresses"),
    (pool_token, "carries no authority token"),
])
def test_the_generator_refuses_records_and_notices_that_break_the_contract(change, problem: str) -> None:
    registry, transport, schemas = (copy.deepcopy(EXPORT.load_yaml("registry.yaml")), copy.deepcopy(TRANSPORT),
                                    copy.deepcopy(EXPORT.load_schemas()))
    change(registry, transport, schemas)
    with pytest.raises(EXPORT.ContractError, match=problem):
        EXPORT.check_registry(registry, transport, EXPORT.load_yaml("team-topologies.yaml"), schemas)


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
    ("task.question", "lead", "person", False, True),   # the lead asks the person
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
    """README, "Relays to an agent's host": the gateway asks the team services, which pass the command on."""

    relayed = ("tasks.list", "tasks.list_finished", "tasks.remove", "tasks.purge", "debug.dump")
    for kind in relayed:
        assert kinds.KINDS[kind].senders_by_target == {"team_services": ("gateway",), "agent": ("team_services",)}
    gateway, services = role_permissions("gateway"), role_permissions("team_services")
    assert "ua.v1.{team}.{self}.svc.team" in gateway["publish"]
    assert "ua.v1.{team}.{self}.ctl.agent.*" in services["publish"]
    for host in ("lead_host", "worker_host"):
        assert "ua.v1.{team}.*.ctl.agent.{agent}" in role_permissions(host)["subscribe"]
    # Operations share their address, and the gateway sends other agent controls there,
    # so the agent's host, not NATS, refuses a relayed kind straight from the gateway.
    case = json.loads((EXAMPLES / "invalid" / "tasks-list-from-the-gateway-to-an-agent.json").read_text(encoding="utf-8"))
    assert (case["refusal"], case["at"]) == ("wrong_sender", "/kind")


def test_a_refused_request_is_answered_with_its_code_and_known_pointer_only() -> None:
    """README, "Refused requests": an unsigned answer that only stops the requester waiting."""

    from custom_messages.wire.refusals import REFUSAL_AT_HEADER, REFUSAL_HEADER, refusal_headers

    pause = fixtures.example_frame("agent.pause")
    pause["body"]["dear_bob_my_password_is_hunter2"] = 1
    with pytest.raises(FrameRefused) as refused:
        fixtures.verify(fixtures.resign(pause), pause)
    assert refusal_headers(refused.value) == {REFUSAL_HEADER: "invalid_body", REFUSAL_AT_HEADER: "/body"}
    readme = " ".join((CONTRACT / "README.md").read_text(encoding="utf-8").split())
    assert f"`{REFUSAL_HEADER}`, the refusal code" in readme and f"`{REFUSAL_AT_HEADER}`, the refusal's pointer" in readme


def test_key_value_grants_let_a_host_write_only_its_own_records() -> None:
    """README, "Key-value grants": each writer's keys are filled for one host, never as a wildcard of an id."""

    worker = expanded("worker_host", agent=["researcher-2"], pool=["researcher"])
    assert "$KV.UA1_HOSTS.worker_host-1" in worker
    assert "$KV.UA1_AGENTS.team_example.researcher-2" in worker
    assert not {subject for subject in worker if subject.startswith("$KV.")} - {
        "$KV.UA1_HOSTS.worker_host-1", "$KV.UA1_AGENTS.team_example.researcher-2"}
    services = expanded("team_services")
    assert {"$KV.UA1_PLACEMENT.team_example.*", "$KV.UA1_CERTS.session.*.team_example.*",
            "$KV.UA1_LOCKS.team_services.team_example", "$KV.UA1_BLOCK_LIST.team_example"} <= services
    # Only the team services take the single-instance lock, and only they place agents.
    for role in REGISTRY["host_roles"]:
        publish = role_permissions(role)["publish"]
        assert any(subject.startswith("$KV.UA1_LOCKS.") for subject in publish) == (role == "team_services"), role
        assert any(subject.startswith("$KV.UA1_PLACEMENT.") for subject in publish) == (role == "team_services"), role
    # Every role that may store a record has a key template for it.
    for name, record in REGISTRY["state_records"].items():
        assert set(record["writers"]) <= set(TRANSPORT["key_value"][record["bucket"]]["writers"]), name


def test_no_bucket_reader_deletes_another_hosts_watch() -> None:
    """A watch creates its own consumer and reads its information; no reader deletes a bucket's consumers."""

    for role in REGISTRY["host_roles"]:
        if role == TRANSPORT["topology_owner"]:
            continue
        deletes = [subject for subject in role_permissions(role)["publish"] if subject.startswith("$JS.API.CONSUMER.DELETE.KV_")]
        assert not deletes, (role, deletes)


@pytest.mark.parametrize(("change", "problem"), [
    (lambda t: t["key_value"]["UA1_CERTS"]["writers"].pop("team_services"), "have no key template"),
    (lambda t: t["key_value"]["UA1_HOSTS"]["writers"].update(worker_host=["{pool}"]), "uses only"),
    (lambda t: t["key_value"]["UA1_HOSTS"]["readers"].append("nobody"), "readers are distinct host roles"),
    (lambda t: t["key_value"]["UA1_LOCKS"].pop("readers"), "fields are"),
    (lambda t: t["dead_letter_custody"].update(streams=["UA1_WORK"]), "dead_letter class's streams"),
])
def test_the_generator_refuses_buckets_and_custody_it_cannot_grant(change, problem: str) -> None:
    transport = copy.deepcopy(TRANSPORT)
    change(transport)
    with pytest.raises(EXPORT.ContractError, match=problem):
        EXPORT.check_stream_management(EXPORT.load_yaml("registry.yaml"), transport)


def test_only_the_team_services_read_their_teams_dead_letters() -> None:
    """README, "Dead letters": notices wait in their team's stream, read through the team services' consumer."""

    consumer = TRANSPORT["consumers"]["dead_letters"]
    assert (consumer["stream"], consumer["name"]) == ("UA1_DLQ_{team}", "dlq~{team}")
    for role in REGISTRY["host_roles"]:
        reads = [subject for subject in role_permissions(role)["publish"] if "UA1_DLQ" in subject]
        if role == "team_services":
            assert set(reads) == {"$JS.API.CONSUMER.INFO.UA1_DLQ_{team}.dlq~{team}",
                                  "$JS.API.CONSUMER.MSG.NEXT.UA1_DLQ_{team}.dlq~{team}", "$JS.ACK.UA1_DLQ_{team}.dlq~{team}.>",
                                  "$JS.API.STREAM.MSG.GET.UA1_DLQ_{team}", "$JS.API.STREAM.MSG.DELETE.UA1_DLQ_{team}"}
        elif role != TRANSPORT["topology_owner"]:
            assert not reads, (role, reads)


def test_a_session_from_a_reply_is_kept_and_lets_its_host_sign_for_the_agent() -> None:
    """README, "Agent sessions": the host keeps the session it was given, then signs frames for the agent."""

    reply = fixtures.example_frame("sessions.open.reply", "ok")
    store = fixtures.trust_store()
    verified = fixtures.verify(json.dumps(reply).encode(), reply, trust=store)
    assert verified.frame.audience.target == "host"
    # Before it keeps the session, the host cannot sign for the agent.
    unplaced = deepcopy(fixtures.example_frame("schedules.get", "agent"))
    unplaced["issuer"].update(agent="researcher-3", session="s-researcher-3-1")
    with pytest.raises(FrameRefused) as refused:
        fixtures.verify(fixtures.resign(unplaced), unplaced, trust=store)
    assert refused.value.code == "wrong_issuer"
    assert store.add(json.dumps(reply["body"]["session"]).encode(), now=fixtures.NOW)
    status = deepcopy(fixtures.example_frame("schedules.get", "agent"))
    status["issuer"].update(agent="researcher-3", session="s-researcher-3-1")
    status["issued_at"], status["expires_at"] = "2026-10-22T04:30:00.500Z", "2026-10-22T04:30:30.000Z"
    fixtures.verify(fixtures.resign(status), status, trust=store)


def test_the_scheduler_answers_only_run_context_sign_and_reads_no_copies() -> None:
    """Only a scheduler signs a run's token (README, "A run's token"); it listens at its own address, never at svc.team."""

    scheduler = role_permissions("scheduler")
    assert scheduler["subscribe"] == ["_INBOX.{self}.>", "ua.v1.{team}.*.svc.scheduler"]
    # It reads the buckets and writes only its own liveness record; it reads no queue.
    buckets = EXPORT.key_value_grants(TRANSPORT)["scheduler"]
    assert [subject for subject in scheduler["publish"] if subject.startswith(("$JS.", "$KV."))] == sorted(buckets)
    assert [subject for subject in buckets if subject.startswith("$KV.")] == ["$KV.UA1_HOSTS.{self}"]
    assert "ua.v1.{team}.*.svc.scheduler" not in role_permissions("team_services")["subscribe"]
    assert [kind for kind, spec in kinds.KINDS.items() if "svc.scheduler" in spec.addresses.values()] == ["run_context.sign"]


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
                                                         valid_at=parse_timestamp(frame["issued_at"]), now=fixtures.NOW,
                                                         at=at)
            checked.append((path.parent.name, verified.certificate.runtime))
    assert ("run_context.sign.reply", "scheduler-1") in checked, checked


def test_a_token_is_valid_when_the_frame_is_received_within_the_clock_tolerance() -> None:
    """README, "On behalf of a person": without a stored time, a frame's token must still be valid by the receiver's clock."""

    frame = fixtures.example_frame("access.resolved", "full")
    data = json.dumps(frame).encode()
    token_end = parse_timestamp(frame["authority"]["expires_at"])
    assert parse_timestamp(frame["issued_at"]) < token_end
    fixtures.verify(data, frame, {"now": format_timestamp(token_end + SKEW - timedelta(milliseconds=1))})
    refused = refusal(data, frame, {"now": format_timestamp(token_end + SKEW)})
    assert (refused.code, refused.at) == ("bad_authority", "/authority/expires_at")


def test_queued_works_token_is_judged_when_the_stream_stored_it() -> None:
    """A person's decision that waits in the queue past its token's end is accepted; one stored after the end is not.

    The stream's timestamp is set by the server, so a sender cannot date a frame back to
    use a token that had already expired. A request is still judged on arrival.
    """

    frame = fixtures.example_frame("access.resolved", "full")
    data = json.dumps(frame).encode()
    token_end = parse_timestamp(frame["authority"]["expires_at"])
    later = format_timestamp(token_end + timedelta(hours=1))  # still within the example revocations record
    fixtures.verify(data, frame, {"now": later, "stored_at": format_timestamp(token_end + SKEW - timedelta(milliseconds=1))})
    refused = refusal(data, frame, {"now": later, "stored_at": format_timestamp(token_end + SKEW)})
    assert (refused.code, refused.at) == ("bad_authority", "/authority/expires_at")
    # A request is no queued work: a stored time, even one after its token's end, does not count.
    command = fixtures.example_frame("tasks.cancel", "by-all")
    command_end = parse_timestamp(command["authority"]["expires_at"])
    fixtures.verify(json.dumps(command).encode(), command, {"stored_at": format_timestamp(command_end + timedelta(hours=1))})
    with pytest.raises(ValueError, match="time zone"):
        wire_frame.verify_frame(data, trust=fixtures.trust_store(), receiver=fixtures.default_receiver(frame),
                                arrived_on=frame["subject"], now=fixtures.NOW, replay=fixtures.replay_holding(),
                                stored_at=fixtures.NOW.replace(tzinfo=None))


def test_a_corrected_decision_is_a_new_message_and_a_retried_one_is_not() -> None:
    """README, "Change reviews", step 3: each decision has its own id, which its key names."""

    decision = fixtures.example_frame("access.resolved", "use-change")
    corrected = copy.deepcopy(decision)
    corrected["body"]["decision_id"] = "decision_5"
    assert registry_key(decision) == registry_key(copy.deepcopy(decision))
    assert registry_key(corrected) != registry_key(decision)
    without = copy.deepcopy(decision)
    del without["body"]["decision_id"]
    assert not validator("bodies/access.resolved.schema.json").is_valid(without["body"])


def test_a_change_review_answers_with_the_version_and_refuses_another_requests_case() -> None:
    """README, "Change reviews", steps 1, 4 and 6."""

    decided = fixtures.example_frame("access.decision", "change-decided")["body"]["status"]
    assert (decided["choice"], decided["seen_version"]) == ("use_change", "3")
    schema = json.loads((CONTRACT / "schemas/bodies/access.decision.schema.json").read_text())
    refused = next(alternative for alternative in schema["properties"]["status"]["oneOf"]
                   if alternative["properties"]["state"]["const"] == "refused")
    assert "not_own_case" in refused["properties"]["code"]["enum"]
    withdrawn = fixtures.example_frame("access.resolved", "withdrawn-by-message")
    fixtures.verify(json.dumps(withdrawn).encode(), withdrawn)
    assert withdrawn["body"]["choice"] == "withdrawn_by_message"


def test_the_edge_adapter_neither_presents_nor_receives_a_persons_token() -> None:
    """README, "On behalf of a person": outside agents never hold a person's token, in either direction."""

    answer = fixtures.example_frame("task.answer", "from-outside-agent")
    answer["authority"] = fixtures.example_frame("custody.confirmed", "claim-with-token")["authority"]
    refused = refusal(fixtures.resign(answer), answer)
    assert (refused.code, refused.at) == ("bad_authority", "/authority/bound_to/type")
    for kind, entry in REGISTRY["kinds"].items():
        if "edge_adapter" in entry["senders"] and entry.get("authority", "none") != "none":
            bindings = entry["authority_binding"] if isinstance(entry["authority_binding"], list) else [entry["authority_binding"]]
            assert all("edge_adapter" not in binding.get("senders", entry["senders"]) for binding in bindings), kind
    registry = copy.deepcopy(EXPORT.load_yaml("registry.yaml"))
    registry["kinds"]["task.answer"]["authority_binding"] = {"type": "root_task"}
    with pytest.raises(EXPORT.ContractError, match="presents no token"):
        EXPORT.check_registry(registry, TRANSPORT, EXPORT.load_yaml("team-topologies.yaml"), EXPORT.load_schemas())


def test_a_move_and_the_confirmations_share_one_numbering_per_task() -> None:
    """README, "After a move" and "Claims": two claimants that lost are told apart, and an older confirmation is ignored."""

    assert REGISTRY["kinds"]["custody.moved"]["idempotency"] == ["/body/task_id", "/body/sequence"]
    moved = fixtures.example_frame("custody.moved", "claimed-elsewhere")
    claim = fixtures.example_frame("custody.confirmed", "claim-with-token")
    assert (moved["body"]["task_id"], moved["body"]["custody_token"]) == (claim["body"]["task_id"], claim["body"]["custody_token"])
    assert moved["body"]["sequence"] > claim["body"]["sequence"] and moved["body"]["reason"] == "claimed_elsewhere"


def test_tasks_for_a_worker_pool_carry_no_token_and_their_copies_do() -> None:
    """The holder of pool work receives the task's token from the team services, in custody.confirmed."""

    assert kinds.KINDS["task.request"].authority_by_target == {"pool": "none"}
    for path in (EXAMPLES / "valid" / "task.request").glob("*.json"):
        frame = json.loads(path.read_text(encoding="utf-8"))
        if frame["audience"]["target"] == "pool":
            assert "authority" not in frame, path.name
    copy_frame = fixtures.example_frame("task.request", "scheduled-run-copy-to-team-services")
    confirmed = fixtures.example_frame("custody.confirmed", "claim-with-token")
    assert copy_frame["authority"] == confirmed["authority"]
    assert confirmed["body"]["task_id"] == fixtures.example_frame("task.request", "scheduled-run")["body"]["task_id"]


@pytest.mark.parametrize(("narrower", "problem"), [
    ({"work.pool.{pool}": "required"}, "narrows the authority"),
    ({"work.team": "none"}, "narrows the authority"),
    ({"work.pool.{pool}": "sometimes"}, "narrows the authority"),
    (["work.pool.{pool}"], "maps some of its own addresses"),
])
def test_authority_by_address_only_narrows_a_kinds_own_addresses(narrower, problem: str) -> None:
    registry = copy.deepcopy(EXPORT.load_yaml("registry.yaml"))
    registry["kinds"]["task.request"]["authority_by_address"] = narrower
    with pytest.raises(EXPORT.ContractError, match=problem):
        EXPORT.check_registry(registry, TRANSPORT, EXPORT.load_yaml("team-topologies.yaml"), CONTRACT_SCHEMAS)


def test_a_run_token_names_no_person_exactly_when_the_run_is_system_work() -> None:
    answers = [json.loads(path.read_text(encoding="utf-8"))["body"]
               for path in (EXAMPLES / "valid" / "run_context.sign.reply").glob("*.json")]
    signed = [body for body in answers if body["outcome"] == "ok"]
    assert {body["system_work"] for body in signed} == {True, False}
    assert all(body["system_work"] == ("person" not in body["authority"]) for body in signed)


def test_each_example_command_has_its_own_token() -> None:
    """README, "On behalf of a person": a command token is bound to one command; only the team services' relay carries it again."""

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
    """README, "Examples": every kind has a valid example and an invalid one."""

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


def test_state_examples_with_two_defects_name_the_later_refusal() -> None:
    """State reports are refused in the registry's order too; each two-defect example lists the later code."""

    order = list(kinds.VERIFICATION_REFUSALS)
    pairs = set()
    for path in (STATE_EXAMPLES / "invalid").glob("*.json"):
        case = json.loads(path.read_text(encoding="utf-8"))
        for later in case.get("also", []):
            assert order.index(later) > order.index(case["refusal"]), path.stem
            pairs.add((case["refusal"], later))
    assert {("bad_signature", "wrong_issuer"), ("revoked", "epoch_too_old"), ("epoch_too_old", "wrong_team"),
            ("wrong_issuer", "invalid_body"), ("wrong_team", "expired"), ("expired", "invalid_body"),
            ("replayed", "invalid_body")} <= pairs


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


def test_a_certificate_names_its_connection_class_and_a_remote_hosts_tools() -> None:
    """README, "Access on remote hosts": a host with a tool list decides from signed lists; one without reads the store."""

    store = fixtures.trust_store()
    remote = store.host_certificate("install_example", "remote-host-1", 1)
    local = store.host_certificate("install_example", "worker-host-1", 3)
    assert remote is not None and local is not None
    assert (remote.connection_class, remote.tools) == ("intermittent", frozenset({"fn:files", "fn:web_research",
                                                                                 "cli:soil-sensor"}))
    assert (local.connection_class, local.tools) == ("connected", None)


def test_a_block_list_is_fresh_for_fifteen_minutes_and_usable_until_it_expires() -> None:
    """README, "Access on remote hosts": fresh_check tools need a list at most fresh_seconds old, by the reader's clock."""

    data = (STATE_EXAMPLES / "valid" / "block_list" / "team.json").read_bytes()
    written = parse_timestamp(json.loads(data)["issued_at"])
    fresh_for = timedelta(seconds=kinds.STATE_RECORDS["block_list"].fresh_seconds) + SKEW
    record = fixtures.verify_state(data)
    assert wire_trust.fresh(record, written + fresh_for)
    assert not wire_trust.fresh(record, written + fresh_for + timedelta(milliseconds=1))
    # Older than fresh, the list still serves other tools until it expires.
    later = written + timedelta(days=6)
    revocations = fixtures.current_revocations(later)
    old = fixtures.verify_state(data, {"now": format_timestamp(later)}, trust=fixtures.trust_store(
        extra_documents=[revocations], now=later))
    assert not wire_trust.fresh(old, later)
    # A record without fresh_seconds is never fresh.
    assert not wire_trust.fresh(fixtures.verify_state((STATE_EXAMPLES / "valid" / "placement" / "lead.json").read_bytes()),
                                written)


def test_a_reader_keeps_the_newest_block_list_even_when_it_holds_fewer_blocks() -> None:
    """A newer list may shrink; a copy of the older one written back is refused."""

    data = (STATE_EXAMPLES / "valid" / "block_list" / "team.json").read_bytes()
    record = fixtures.verify_state(data)
    newer = state_example("block_list/team.json")
    newer["sequence"] += 1
    newer["issued_at"] = "2026-10-22T04:45:00.000Z"
    newer["body"]["blocks"] = newer["body"]["blocks"][:1]
    newer_record = fixtures.verify_state(signed_state(newer), {"held": {"sequence": record.document.sequence,
                                                                         "digest": record.digest},
                                                                "now": "2026-10-22T04:45:00.000Z"})
    with pytest.raises(FrameRefused) as refused:
        fixtures.verify_state(data, {"held": {"sequence": newer_record.document.sequence, "digest": newer_record.digest}})
    assert (refused.value.code, refused.value.at) == ("replayed", "/sequence")


def test_team_view_key_must_name_its_body_entry() -> None:
    document = state_example("team_view/roster.json")
    document["key"] = "team_example.task.lead"
    with pytest.raises(FrameRefused) as refused:
        fixtures.verify_state(signed_state(document))
    assert (refused.value.code, refused.value.at) == ("invalid_body", "/key")


def test_state_reports_are_verified_when_read_not_kept() -> None:
    """Whoever writes to a bucket chooses what it holds, so another record type is refused, not trusted."""

    agent = (STATE_EXAMPLES / "valid" / "agent" / "lead-busy.json").read_bytes()
    certificate = (EXAMPLES / "trust" / "certificates" / "lead-host-1.json").read_bytes()
    for read in (lambda: fixtures.trust_store().add(agent, now=fixtures.NOW), lambda: fixtures.verify_state(certificate, record="placement"),
                 lambda: fixtures.verify_state(agent, record="placement")):
        with pytest.raises(FrameRefused) as refused:
            read()
        assert (refused.value.code, refused.value.at) == ("malformed", "/record")
    with pytest.raises(ValueError, match="kept with add"):
        fixtures.verify_state(certificate)
    with pytest.raises(ValueError, match="time zone"):
        fixtures.trust_store().verify(agent, record="agent", now=fixtures.NOW.replace(tzinfo=None))


def test_a_newer_minors_record_loses_unknown_optional_fields_and_the_same_minors_is_refused() -> None:
    """README, "Versions": records follow the frames' rule, so a certificate can gain optional fields within protocol 1."""

    document = state_example("agent/lead-busy.json")
    document["future_note"] = "an envelope field this version does not know"
    document["body"]["future_detail"] = "a body field this version does not know"
    with pytest.raises(FrameRefused) as refused:
        fixtures.verify_state(signed_state(document))
    assert refused.value.code == "malformed"
    document["protocol"] = "1.1-pre"
    verified = fixtures.verify_state(signed_state(document))
    assert not hasattr(verified.body, "future_detail") and verified.body.agent == "lead"
    del document["future_note"]
    document["protocol"] = kinds.PROTOCOL
    with pytest.raises(FrameRefused) as refused:
        fixtures.verify_state(signed_state(document))
    assert (refused.value.code, refused.value.at) == ("invalid_body", "/body/future_detail")


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
    assert document["channels"][channel_id]["address"] == "ua.v1._install.{sender}.svc"
    authority = json.loads((CONTRACT / "generated/permissions/installation_authority.json").read_text())
    assert "ua.v1._install.*.svc" in authority["subscribe"]


def test_a_notice_names_a_refusal_up_to_the_last_field_the_schema_knows() -> None:
    """README, "Dead letters": an unknown field's name, which the sender chose, never reaches the notice."""

    name = "dear_bob_my_password_is_hunter2"
    stop = signed_example()
    stop["body"][name] = 1
    refused = refusal(fixtures.resign(stop), stop)
    assert (refused.code, refused.at, refused.known_at) == ("invalid_body", f"/body/{name}", "/body")
    frame = signed_example()
    frame[name] = 1
    refused = refusal(fixtures.resign(frame), frame)
    assert (refused.code, refused.known_at) == ("malformed", "")
    # A newer minor's reply whose certificate, labelled with this minor, holds a field it does not know.
    reply = fixtures.example_frame("host.join.reply", "ok")
    reply["protocol"] = "1.1-pre"
    reply["body"]["certificate"][name] = 1
    refused = refusal(fixtures.resign(reply), reply)
    assert (refused.code, refused.at, refused.known_at) == ("invalid_body", f"/body/certificate/{name}", "/body/certificate")
    notice = fixtures.example_frame("dlq.notice", "bad-signature")["body"]
    assert validator("bodies/dlq.notice.schema.json").is_valid(dict(notice, pointer="/body"))
    assert not validator("bodies/dlq.notice.schema.json").is_valid(dict(notice, pointer="/body/Dear Bob, my password"))


def test_dead_letter_senders_can_publish_raw_copies_under_their_own_id() -> None:
    for role in REGISTRY["kinds"]["dlq.notice"]["senders"]:
        document = json.loads((CONTRACT / f"generated/permissions/{role}.json").read_text())
        assert "ua.v1.{team}.{self}.dlq.raw.*" in document["publish"]
        assert not any("ua.v1.{team}.*.dlq.raw" in subject for subject in document["publish"])


def join_frame(key_id: str = "test-lead-key-1", public_key_of: str | None = None) -> dict:
    """The join example, from lead-host-1 presenting ``public_key_of``'s key under ``key_id``."""

    document = fixtures.example_frame("host.join", "full")
    document["issuer"].update(runtime="lead-host-1", key_id=key_id)
    document["subject"] = "ua.v1._install.lead-host-1.svc"
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


# The identity point as a public key, and a signature that verifies under it for every message:
# R is the identity and S is zero.
IDENTITY_KEY = "AQAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA"
FORGED_SIGNATURE = b64url(b64url_decode(IDENTITY_KEY) + bytes(32))


def test_a_join_presenting_a_small_order_key_is_refused_though_its_forged_signature_verifies() -> None:
    document = join_frame()
    document["body"]["application_key"]["public_key"] = IDENTITY_KEY
    document["idempotency_key"] = registry_key(document)
    signed = sign_frame(document, fixtures.private_key("test-lead-key-1"))
    signed["signature"] = FORGED_SIGNATURE
    Ed25519PublicKey.from_public_bytes(b64url_decode(IDENTITY_KEY)).verify(
        b64url_decode(FORGED_SIGNATURE), signing_bytes(signed))
    refused = refusal(json.dumps(signed).encode(), signed, trust=without_lead_host())
    assert (refused.code, refused.at) == ("unknown_key", "/body/application_key/public_key")


def test_a_rotation_to_a_small_order_key_is_refused_though_its_forged_proof_verifies() -> None:
    document = fixtures.example_frame("host.renew", "rotate")
    rotation = document["body"]["application_key_rotation"]
    rotation["key"]["public_key"] = IDENTITY_KEY
    rotation["proof"] = FORGED_SIGNATURE
    Ed25519PublicKey.from_public_bytes(b64url_decode(IDENTITY_KEY)).verify(
        b64url_decode(FORGED_SIGNATURE), wire_frame.renew_key_proof_bytes(document))
    document["idempotency_key"] = registry_key(document)
    signed = sign_frame(document, fixtures.private_key("test-lead-key-1"))
    refused = refusal(json.dumps(signed).encode(), signed)
    assert (refused.code, refused.at) == ("invalid_body", "/body/application_key_rotation/proof")


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
    # A dead-letter notice waits in its team's UA1_DLQ_<team> like queued work.
    notice = fixtures.example_frame("dlq.notice", "bad-signature")
    worker_minimum = {"minimum_epochs": [{"installation": "install_example", "runtime": "worker-host-1", "membership": 9}]}
    fixtures.verify(json.dumps(notice).encode(), notice, worker_minimum)


def test_renewal_rotation_signing_vector() -> None:
    vector = json.loads((CONTRACT / "signing-vectors/key-rotation.json").read_text())
    document = fixtures.example_frame("host.renew", "rotate")
    before = copy.deepcopy(document)
    assert wire_frame.renew_key_proof_bytes(document).hex() == vector["signing_bytes_hex"]
    assert document["body"]["application_key_rotation"]["proof"] == vector["proof"]
    signed = wire_frame.sign_renew_key_proof(document, fixtures.private_key(vector["new_key"]))
    assert sign_frame(signed, fixtures.private_key(vector["old_key"])) == document
    assert document == before, "signing must not mutate the caller's frame"


def test_rotation_proof_covers_a_newer_minors_unknown_fields() -> None:
    document = fixtures.example_frame("host.renew", "rotate")
    document["protocol"] = "1.1-pre"
    document["body"]["application_key_rotation"]["future_option"] = "original"
    document = sign_frame(wire_frame.sign_renew_key_proof(document, fixtures.private_key("test-lead-key-2")),
                          fixtures.private_key("test-lead-key-1"))
    fixtures.verify(json.dumps(document).encode(), document)
    document["body"]["application_key_rotation"]["future_option"] = "changed"
    assert refusal(fixtures.resign(document), document).code == "invalid_body"


def test_a_host_that_has_not_joined_may_only_ask_to_join_and_read_its_inbox() -> None:
    joining = role_permissions(EXPORT.JOINING)
    assert joining == {"role": EXPORT.JOINING, "publish": ["ua.v1._install.{self}.svc"],
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
    """README, "Trust over time": frames from the replaced key are accepted until a short deadline after renewal."""

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
    store = fixtures.trust_store(extra_documents=[renewed_lead_certificate(issued_at="2026-10-22T04:35:00.000Z")],
                                 now=parse_timestamp("2026-10-22T05:00:00.000Z"))
    fixtures.verify(json.dumps(stop).encode(), stop, {"now": "2026-10-22T05:00:00.000Z"}, trust=store)


@pytest.mark.parametrize("epoch", [3, 2])
def test_a_renewed_certificate_needs_a_higher_epoch(epoch: int) -> None:
    """At the same epoch it would overwrite the certificate queued work still needs (host.<installation>.<runtime>.<epoch>)."""

    with pytest.raises(FrameRefused) as refused:
        fixtures.trust_store(extra_documents=[renewed_lead_certificate(epoch=epoch)])
    assert (refused.value.code, refused.value.at) == ("invalid_body", "/body/membership_epoch")


def test_a_renewed_certificate_needs_a_higher_epoch_when_it_arrives_first() -> None:
    store = fixtures.trust_store(remove=["certificates/lead-host-1.json"])
    store.add(renewed_lead_certificate(epoch=2), now=fixtures.NOW)
    with pytest.raises(FrameRefused) as refused:
        store.add((EXAMPLES / "trust/certificates/lead-host-1.json").read_bytes(), now=fixtures.NOW)
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
    bootstrap.add(json.dumps(verified.raw["body"]["certificate"]).encode(), now=fixtures.NOW)
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


@pytest.mark.parametrize("case", VECTORS["public_key_refused"], ids=lambda case: case["name"])
def test_a_public_key_outside_the_prime_order_subgroup_is_refused(case) -> None:
    with pytest.raises(ValueError):
        ed25519_public_key(case["text"])


def test_generated_public_keys_are_accepted() -> None:
    for key in fixtures.KEYS.values():
        assert b64url(ed25519_public_key(key["public_key"]).public_bytes_raw()) == key["public_key"]


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
# a reply's certificate (its signature, single validity window and what it certifies) and
# the end of the credential that comes with it, a key rotation's new key id, public key and
# proof (README, "Installation traffic"), and a session reply's session (its signature,
# writer and what it places; README, "Agent sessions").
RECEIVER_CHECKED = re.compile(r"/body/certificate/(signature|expires_at|body/[a-z_]+)|/body/connection/expires_at"
                              r"|/body/application_key_rotation/(key/key_id|key/public_key|proof)"
                              r"|/body/session/(signature|writer|body/[a-z_]+)")
ROTATION_PROOF_AT = "/body/application_key_rotation/proof"


def receiver_compares_body(case: dict) -> bool:
    """A missing proof fails the schema; a present proof still needs signature verification."""

    if case["at"] == ROTATION_PROOF_AT and pointed(json.loads(case["frame_text"]), ROTATION_PROOF_AT) is None:
        return False
    return RECEIVER_CHECKED.fullmatch(case["at"]) is not None


def receiver_only(case: dict) -> bool:
    """Refusals a schema cannot give: they recompute a key, apply a body rule, or compare the body with records.

    README, "Idempotency keys", "What bodies carry" and "Installation traffic".
    """

    kind = json.loads(case["frame_text"])["kind"]
    rules = REGISTRY["kinds"].get(kind, {}).get("body_rules", [])
    places = {rule.get("unique") or rule.get("requires") or rule.get("allowed") or rule.get("ends_within")
              or rule["any_of"][0].rsplit("/", 1)[0] for rule in rules}
    return case["at"] == "/idempotency_key" or case["at"] in places or receiver_compares_body(case)


@pytest.mark.parametrize("path", [p for p in invalid_examples() if receiver_compares_body(json.loads(p.read_text()))],
                         ids=lambda path: path.stem)
def test_refusals_that_compare_the_body_with_records_pass_the_schema(path: Path) -> None:
    """Only a receiver gives these, so the shared examples are its tests; a schema accepts the body."""

    case = json.loads(path.read_text(encoding="utf-8"))
    document = json.loads(case["frame_text"])
    assert case["refusal"] == "invalid_body"
    assert not list(validator(body_schema_file(document["kind"])).iter_errors(document["body"]))


def schema_checked(path: Path) -> bool:
    """Whether the schemas refuse an invalid example too.

    Every malformed frame and invalid body named by its place, except what only a receiver
    can check: dates that do not exist, new-key proofs that do not verify, keys that are not
    the ones their parts give, and body rules (README, "Canonical form and signatures",
    "Application key rotation", "Idempotency keys" and "What bodies carry").
    """

    case = json.loads(path.read_text(encoding="utf-8"))
    return (case["refusal"] in ("malformed", "invalid_body") and bool(case["at"])
            and path.stem != "impossible-date" and not receiver_only(case))


@pytest.mark.parametrize("path", [path for path in invalid_examples() if schema_checked(path)], ids=lambda path: path.stem)
def test_the_schemas_refuse_malformed_frames_and_invalid_bodies_where_named(path: Path) -> None:
    case = json.loads(path.read_text(encoding="utf-8"))
    document = json.loads(case["frame_text"])
    if case["refusal"] == "malformed":
        errors, prefix = list(validator("frame.schema.json").iter_errors(document)), ""
    else:
        errors, prefix = list(validator(body_schema_file(document["kind"])).iter_errors(document["body"])), "/body"
    assert errors
    locations = {prefix + "".join(f"/{part}" for part in error.absolute_path) for error in errors}
    nested = {prefix + "".join(f"/{part}" for part in sub.absolute_path) for error in errors for sub in error.context or ()}
    assert case["at"] in locations | nested or any(case["at"].startswith(location) for location in locations), (locations, nested)


# -- who sent it ----------------------------------------------------------------------------------------

def certificate_record(runtime: str, *, roles: tuple[str, ...], epoch: int = 1,
                       issued_at: str = "2026-10-22T00:00:00.000Z", expires_at: str = "2026-11-21T00:00:00.000Z",
                       key_id: str = "test-worker-key-1", installation: str = "install_example",
                       authority: str = "test-authority-key-1") -> bytes:
    """A host certificate for team_example, signed by ``authority``."""

    body = {
        "certificate_id": f"cert_{runtime}", "installation": installation, "runtime": runtime, "teams": ["team_example"],
        "roles": list(roles), "membership_epoch": epoch, "revocation_epoch": 1,
        "application_key": {"key_id": key_id, "public_key": fixtures.KEYS[key_id]["public_key"]},
        "nats_public_key": "U" + "X" * 55, "agent_types": [], "session_limit": 0, "connection_class": "connected",
        "protocol": kinds.PROTOCOL,
        "deployment": "local", "profile_sha256": "CgoKCgoKCgoKCgoKCgoKCgoKCgoKCgoKCgoKCgoKCgo",
        "issued_at": issued_at, "expires_at": expires_at,
    }
    document = {"record": "certificate", "protocol": kinds.PROTOCOL, "key": f"host.{installation}.{runtime}.{epoch}",
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
        store.add(data, now=fixtures.NOW)
    assert refused.value.code == code, refused.value


def test_a_session_signed_by_a_host_without_team_services_is_refused() -> None:
    store = fixtures.trust_store()
    session = json.loads((EXAMPLES / "trust" / "sessions" / "researcher-2.json").read_text())
    session["writer"] = {"type": "host", "installation": "install_example", "runtime": "worker-host-1",
                         "membership_epoch": 3, "key_id": "test-worker-key-1"}
    session["sequence"] = 5
    with pytest.raises(FrameRefused) as refused:
        store.add(json.dumps(sign_state(session, fixtures.private_key("test-worker-key-1"))).encode(), now=fixtures.NOW)
    assert (refused.value.code, refused.value.at) == ("wrong_issuer", "/writer")


def pruned_revocations(sequence: int, issued_at, revocation_epoch: int = 2) -> bytes:
    """The record that revoked the lead, written again at ``issued_at`` without the lead's entry."""

    document = json.loads((EXAMPLES / "trust" / "extra" / "lead-host-revoked-after-issue.json").read_text())
    document["sequence"], document["issued_at"] = sequence, format_timestamp(issued_at)
    document["body"]["revocation_epoch"] = revocation_epoch
    document["body"]["entries"] = [entry for entry in document["body"]["entries"] if entry["certificate_id"] != "cert_lead_host_1"]
    document["body"]["expires_at"] = format_timestamp(issued_at + timedelta(hours=6))
    return signed_state(document)


def test_an_entry_leaves_the_revocations_record_only_once_every_certificate_with_its_key_ended(caplog) -> None:
    """README, "Trust over time": an entry stays until its key_expires_at plus the replay retention; the epoch never falls.

    The lead's renewal kept the revoked key and ends after the lead's own certificate, so
    an entry that left with the lead's own end would let the renewal sign again.
    """

    store = fixtures.trust_store(add=["extra/lead-host-revoked-after-issue.json"],
                                 extra_documents=[renewed_lead_certificate(key_id="test-lead-key-1")])
    lead, renewal = (store.host_certificate("install_example", "lead-host-1", epoch) for epoch in (3, 4))
    assert lead is not None and renewal is not None and store.revoked(lead) and store.revoked(renewal)
    entry = next(item for item in json.loads((EXAMPLES / "trust" / "extra" / "lead-host-revoked-after-issue.json")
                                             .read_text())["body"]["entries"] if item["certificate_id"] == "cert_lead_host_1")
    assert parse_timestamp(entry["key_expires_at"]) == renewal.expires_at > lead.expires_at
    # A restarted host adds its stored record first, so an older copy written back to the bucket is ignored.
    assert store.add((EXAMPLES / "trust" / "revocations" / "install_example.json").read_bytes(), now=fixtures.NOW) is False
    early = lead.expires_at + wire_trust.RETENTION + SKEW
    assert store.add(pruned_revocations(9, early), now=early) is True
    assert store.revoked(renewal) and store.revoked(lead)
    assert "left out entry cert_lead_host_1" in caplog.text
    late = renewal.expires_at + wire_trust.RETENTION + SKEW
    assert store.add(pruned_revocations(10, late), now=late) is True
    assert not store.revoked(renewal) and not store.revoked(lead)
    assert store.add((EXAMPLES / "trust" / "extra" / "lead-host-revoked-after-issue.json").read_bytes(), now=late) is False
    with pytest.raises(FrameRefused) as refused:
        store.add(pruned_revocations(11, late, revocation_epoch=1), now=late)
    assert (refused.value.code, refused.value.at) == ("invalid_body", "/body/revocation_epoch")


def relabelled_certificate(runtime: str, key_id: str, public_key_of: str, epoch: int = 1) -> bytes:
    """A certificate for ``runtime`` that names ``key_id`` but holds ``public_key_of``'s public key."""

    document = json.loads(certificate_record(runtime, roles=("worker_host",), epoch=epoch))
    document["body"]["application_key"] = {"key_id": key_id, "public_key": fixtures.KEYS[public_key_of]["public_key"]}
    return signed_state({key: value for key, value in document.items() if key not in {"algorithm", "body_digest", "signature"}})


def test_a_revoked_public_key_stays_revoked_under_another_key_id_or_runtime() -> None:
    """README, "Trust over time": a key id is a label, so revoking a certificate revokes its public key everywhere."""

    relabelled = relabelled_certificate("worker-host-9", "relabelled-key-1", "test-lead-key-1")
    store = fixtures.trust_store(add=["extra/lead-host-revoked-after-issue.json"], extra_documents=[relabelled])
    certificate = store.host_certificate("install_example", "worker-host-9", 1)
    assert certificate is not None and store.revoked(certificate)
    assert not fixtures.trust_store(extra_documents=[relabelled]).revoked(certificate)
    # A join presenting the revoked public key under a new key id is refused too.
    document = join_frame(key_id="relabelled-key-2", public_key_of="test-lead-key-1")
    data = json.dumps(sign_frame(document, fixtures.private_key("test-lead-key-1"))).encode()
    refused = refusal(data, document, {"trust": {"add": ["extra/lead-host-revoked-after-issue.json"]}})
    assert (refused.code, refused.at) == ("revoked", "/body/application_key/public_key")
    fixtures.verify(data, document, trust=without_lead_host())


def test_a_rotation_to_a_public_key_another_certificate_holds_is_refused() -> None:
    document = fixtures.example_frame("host.renew", "rotate")
    document["body"]["application_key_rotation"]["key"] = {"key_id": "fresh-key-1",
                                                           "public_key": fixtures.KEYS["test-worker-key-1"]["public_key"]}
    document["body"]["application_key_rotation"] = wire_frame.sign_renew_key_proof(
        document, fixtures.private_key("test-worker-key-1"))["body"]["application_key_rotation"]
    document["idempotency_key"] = registry_key(document)
    signed = sign_frame(document, fixtures.private_key("test-lead-key-1"))
    refused = refusal(json.dumps(signed).encode(), signed)
    assert (refused.code, refused.at) == ("invalid_body", "/body/application_key_rotation/key/public_key")


def test_a_join_is_refused_for_a_listed_key_without_its_certificate_or_without_a_current_record() -> None:
    """The join's own revocation checks: a listed key id whose certificate the store never held, and the current record."""

    document = join_frame()
    data = json.dumps(sign_frame(document, fixtures.private_key("test-lead-key-1"))).encode()
    listed = json.loads((EXAMPLES / "trust" / "extra" / "lead-host-key-revoked.json").read_text())
    assert {(entry["runtime"], entry["key_id"]) for entry in listed["body"]["entries"]} >= {("lead-host-1", "test-lead-key-1")}
    store = fixtures.trust_store(remove=["certificates/lead-host-1.json", "sessions/lead.json"],
                                 add=["extra/lead-host-key-revoked.json"])
    assert store.certificates_with_key("install_example", "test-lead-key-1") == []
    refused = refusal(data, document, trust=store)
    assert (refused.code, refused.at) == ("revoked", "/issuer/key_id")
    stale = fixtures.trust_store(remove=["certificates/lead-host-1.json", "sessions/lead.json", "revocations/install_example.json"])
    refused = refusal(data, document, trust=stale)
    assert (refused.code, refused.at) == ("revocations_not_current", "/issuer/installation")


def test_revoking_a_certificate_revokes_every_certificate_of_its_runtime_with_its_key() -> None:
    store = fixtures.trust_store(add=["extra/lead-host-revoked-after-issue.json"],
                                 extra_documents=[renewed_lead_certificate(key_id="test-lead-key-1")])
    renewed = store.host_certificate("install_example", "lead-host-1", 4)
    assert renewed is not None and renewed.certificate_id != "cert_lead_host_1" and store.revoked(renewed)
    assert store.key_revoked("install_example", "lead-host-1", "test-lead-key-1")
    assert not store.key_revoked("install_example", "lead-host-2", "test-lead-key-1")
    rotated = fixtures.trust_store(add=["extra/lead-host-revoked-after-issue.json"],
                                   extra_documents=[renewed_lead_certificate(key_id="test-lead-key-2")])
    assert not rotated.revoked(rotated.host_certificate("install_example", "lead-host-1", 4))


def test_a_receiver_without_a_current_revocations_record_refuses_every_host_but_its_authority() -> None:
    frame = signed_example()
    data = json.dumps(frame).encode()
    store = fixtures.trust_store(remove=["revocations/install_example.json"])
    refused = refusal(data, frame, trust=store)
    assert (refused.code, refused.at) == ("revocations_not_current", "/issuer/installation")
    # The authority still answers, so a host whose record expired can renew and read a current one.
    reply = fixtures.example_frame("host.renew.reply", "ok")
    fixtures.verify(json.dumps(reply).encode(), reply, trust=store)
    token = fixtures.example_frame("access.resolved", "full")["authority"]
    with pytest.raises(FrameRefused) as refused_token:
        wire_frame.verify_authority_token(models.AuthorityToken.model_validate(token), token, store,
                                          valid_at=fixtures.NOW, now=fixtures.NOW)
    assert (refused_token.value.code, refused_token.value.at) == ("bad_authority", "/authority/issuer")
    # Only an expired record is as good as none, within the clock tolerance.
    ends = parse_timestamp(json.loads((EXAMPLES / "trust" / "revocations" / "install_example.json").read_text())
                           ["body"]["expires_at"])
    fixtures.verify(data, frame, {"now": format_timestamp(ends + SKEW - timedelta(milliseconds=1))})
    assert refusal(data, frame, {"now": format_timestamp(ends + SKEW)}).code == "revocations_not_current"
    fixtures.verify(data, frame, trust=fixtures.trust_store(now=ends + SKEW,
                                                            extra_documents=[fixtures.current_revocations(ends + SKEW)]),
                    context={"now": format_timestamp(ends + SKEW)})


def test_only_installation_traffic_from_the_receivers_own_authority_needs_no_current_record() -> None:
    """README, "Who sent it", step 4: the exception covers join and renewal replies, not every frame of an authority host."""

    store = fixtures.trust_store(remove=["revocations/install_example.json"])
    authority, = store.certificates_with_key("install_example", "test-authority-host-key-1")
    store.require_current_revocations(authority, fixtures.NOW, at="/issuer/installation", installation_traffic=True)
    with pytest.raises(FrameRefused) as refused:
        store.require_current_revocations(authority, fixtures.NOW, at="/issuer/installation")
    assert (refused.value.code, refused.value.at) == ("revocations_not_current", "/issuer/installation")
    # Another installation's host in the authority role gets no exception, even for installation traffic.
    peer_store = fixtures.trust_store(remove=["revocations/install_peer.json"])
    peer_host, = peer_store.certificates_with_key("install_peer", "test-peer-lead-key-1")
    peer_authority = dataclasses.replace(peer_host, roles=frozenset({"installation_authority"}))
    with pytest.raises(FrameRefused) as refused:
        peer_store.require_current_revocations(peer_authority, fixtures.NOW, at="/issuer/installation",
                                               installation_traffic=True)
    assert refused.value.code == "revocations_not_current"


def test_a_frame_from_another_installation_needs_a_current_record_of_that_installation() -> None:
    frame = fixtures.example_frame("task.stop", "from-peer")
    data = json.dumps(frame).encode()
    fixtures.verify(data, frame)
    refused = refusal(data, frame, trust=fixtures.trust_store(remove=["revocations/install_peer.json"]))
    assert (refused.code, refused.at) == ("revocations_not_current", "/issuer/installation")


def test_a_temporary_refusal_is_no_dead_letter_reason() -> None:
    """README, "Who sent it" and "Dead letters": a receiver waits for a current record and sets nothing aside for it."""

    assert kinds.TEMPORARY_REFUSALS == frozenset({"revocations_not_current"})
    reasons = set(json.loads((CONTRACT / "schemas/bodies/dlq.notice.schema.json").read_text())["properties"]["reason"]["enum"])
    assert set(kinds.VERIFICATION_REFUSALS) - kinds.TEMPORARY_REFUSALS <= reasons
    assert not reasons & kinds.TEMPORARY_REFUSALS


def test_the_revocations_record_is_never_forgotten() -> None:
    store = fixtures.trust_store()
    store.forget(forgotten_from("certificate") + timedelta(days=4000))
    assert store.held()["revocations"] == 2
    assert store.add((EXAMPLES / "trust" / "revocations" / "install_example.json").read_bytes(), now=fixtures.NOW) is False


def test_a_record_sequence_reused_for_other_content_is_refused() -> None:
    store = fixtures.trust_store()
    session = json.loads((EXAMPLES / "trust" / "sessions" / "lead.json").read_text())
    assert store.add(json.dumps(session).encode(), now=fixtures.NOW) is False  # the same record again
    session["body"]["ends_at"] = "2026-10-22T05:00:00.000Z"
    with pytest.raises(FrameRefused) as refused:
        store.add(json.dumps(sign_state(session, fixtures.private_key("test-services-key-1"))).encode(), now=fixtures.NOW)
    assert (refused.value.code, refused.value.at) == ("replayed", "/sequence")


def test_a_revocation_that_arrives_later_still_stops_queued_frames() -> None:
    """Recheck the same store after an update; a preloaded revocation cannot prove cache invalidation."""

    frame = signed_example()
    data = json.dumps(frame).encode()
    store = fixtures.trust_store()
    fixtures.verify(data, frame, trust=store)
    store.add((EXAMPLES / "trust" / "extra" / "lead-host-revoked-after-issue.json").read_bytes(), now=fixtures.NOW)
    assert refusal(data, frame, trust=store).code == "revoked"


def test_agreements_come_only_from_the_receivers_own_authority_and_carry_durable_kinds() -> None:
    agreement = json.loads((EXAMPLES / "trust" / "agreements" / "install_peer.json").read_text())
    agreement["sequence"] = 2
    agreement["body"]["kinds"] = ["task.stop", "agent.pause"]
    store = fixtures.trust_store()
    with pytest.raises(FrameRefused) as refused:
        store.add(json.dumps(sign_state(agreement, fixtures.private_key("test-authority-key-1"))).encode(), now=fixtures.NOW)
    assert (refused.value.code, refused.value.at) == ("invalid_body", "/body/kinds/1")
    forged = dict(agreement, writer={"type": "authority", "installation": "install_peer", "key_id": "test-peer-authority-key-1"})
    forged["body"] = dict(agreement["body"], kinds=["task.stop"])
    with pytest.raises(FrameRefused) as refused:
        store.add(json.dumps(sign_state(forged, fixtures.private_key("test-peer-authority-key-1"))).encode(), now=fixtures.NOW)
    assert refused.value.code == "wrong_issuer"


def test_a_host_that_joins_later_is_trusted_once_its_certificate_arrives() -> None:
    """New hosts need no change on existing receivers: their certificate and sessions are enough."""

    store = fixtures.trust_store()
    frame = signed_example()
    frame["issuer"] = {"installation": "install_example", "team": "team_example", "runtime": "lead-host-2",
                       "membership_epoch": 1, "revocation_epoch": 1, "key_id": "test-gateway-key-1"}
    frame["subject"] = "ua.v1.team_example.lead-host-2.work.control.researcher-2"
    data = json.dumps(sign_frame(frame, fixtures.private_key("test-gateway-key-1"))).encode()
    assert refusal(data, frame, trust=store).code == "unknown_key"
    store.add(certificate_record("lead-host-2", roles=("lead_host",), key_id="test-gateway-key-1"), now=fixtures.NOW)
    assert fixtures.verify(data, frame, trust=store).certificate.runtime == "lead-host-2"


# Trust records that end: the example record and the body field that says when.
ENDING_RECORDS = {"certificate": ("certificates/lead-host-1.json", "expires_at"),
                  "agent_session": ("sessions/lead.json", "ends_at"),
                  "agreement": ("agreements/install_peer.json", "expires_at"),
                  "revocations": ("revocations/install_example.json", "expires_at")}
# Those a receiver forgets once nothing can still need them; never the revocations record.
FORGOTTEN_RECORDS = sorted(set(ENDING_RECORDS) - {"revocations"})
RETENTION = timedelta(seconds=kinds.REPLAY_LEDGER_RETENTION_SECONDS)


def lifetime(record: str) -> timedelta:
    return timedelta(seconds=kinds.STATE_RECORDS[record].max_lifetime_seconds)


def trust_example(record: str) -> dict:
    return json.loads((EXAMPLES / "trust" / ENDING_RECORDS[record][0]).read_text())


def ending(record: str, after: timedelta) -> bytes:
    """The example ``record`` written again (a higher sequence) to end ``after`` it is written."""

    document = trust_example(record)
    end = parse_timestamp(document["issued_at"]) + after
    text = end.strftime("%Y-%m-%dT%H:%M:%S.") + f"{end.microsecond // 1000:03d}Z"
    document["body"][ENDING_RECORDS[record][1]] = text
    if "expires_at" in document:
        document["expires_at"] = text
    document["sequence"] += 1
    return json.dumps(sign_state(document, fixtures.private_key(document["writer"]["key_id"]))).encode()


def forgotten_from(record: str) -> object:
    """When the example ``record`` is forgotten: its newest version's writing plus its lifetime, retention and tolerance.

    A certificate is kept an agent session's lifetime and the tolerance longer, for sessions it signed.
    """

    reach = lifetime("agent_session") + SKEW if record == "certificate" else timedelta()
    return parse_timestamp(trust_example(record)["issued_at"]) + lifetime(record) + RETENTION + SKEW + reach


@pytest.mark.parametrize("record", sorted(ENDING_RECORDS))
def test_a_trust_record_ends_within_its_longest_lifetime(record: str) -> None:
    assert fixtures.trust_store().add(ending(record, lifetime(record)), now=fixtures.NOW) is True
    with pytest.raises(FrameRefused) as refused:
        fixtures.trust_store().add(ending(record, lifetime(record) + timedelta(milliseconds=1)), now=fixtures.NOW)
    assert (refused.value.code, refused.value.at) == ("invalid_body", f"/body/{ENDING_RECORDS[record][1]}")


@pytest.mark.parametrize("record", FORGOTTEN_RECORDS)
def test_a_trust_record_is_forgotten_when_nothing_can_still_need_it_and_does_not_come_back(record: str) -> None:
    store = fixtures.trust_store()
    held = store.held()
    assert held == {"agreement": 1, "certificate": 9, "revocations": 2, "agent_session": 4}
    store.forget(forgotten_from(record) - timedelta(milliseconds=1))
    assert store.held().get(record, 0) >= 1
    lookups = {"certificate": lambda: store.host_certificate("install_example", "lead-host-1", 3),
               "agent_session": lambda: store.agent_session("install_example", "team_example", "s-lead-1"),
               "agreement": lambda: store.agreement("install_peer")}
    assert lookups[record]() is not None
    forgotten = store.forget(forgotten_from(record))
    assert forgotten >= 1 and lookups[record]() is None
    # The record, and any version of it written no later, is ignored from now on.
    assert store.add((EXAMPLES / "trust" / ENDING_RECORDS[record][0]).read_bytes(), now=fixtures.NOW) is False
    assert lookups[record]() is None
    assert store.held()["revocations"] == 2


def test_forgetting_needs_a_newer_version_to_be_forgotten_too() -> None:
    """A renewal moves the moment a record is forgotten: it is measured from the newest version held."""

    store = fixtures.trust_store()
    renewed = trust_example("agent_session")
    renewed["sequence"] += 1
    later = parse_timestamp(renewed["issued_at"]) + timedelta(days=3)
    renewed["issued_at"] = later.strftime("%Y-%m-%dT%H:%M:%S.000Z")
    renewed["body"]["ends_at"] = (later + timedelta(hours=1)).strftime("%Y-%m-%dT%H:%M:%S.000Z")
    assert store.add(json.dumps(sign_state(renewed, fixtures.private_key(renewed["writer"]["key_id"]))).encode(), now=later)
    store.forget(forgotten_from("agent_session"))
    assert store.agent_session("install_example", "team_example", "s-lead-1") is not None
    store.forget(forgotten_from("agent_session") + timedelta(days=3))
    assert store.agent_session("install_example", "team_example", "s-lead-1") is None


def test_forgetting_keeps_a_replacement_whose_replaced_certificate_arrives_later() -> None:
    """The renewal that arrives first still ends the old certificate's overlap once the old one is added."""

    store = fixtures.trust_store(remove=["certificates/lead-host-1.json"])
    store.add(renewed_lead_certificate(issued_at="2026-10-22T04:20:00.000Z"), now=fixtures.NOW)
    store.forget(fixtures.NOW)
    store.add((EXAMPLES / "trust/certificates/lead-host-1.json").read_bytes(), now=fixtures.NOW)
    pause = fixtures.example_frame("agent.pause")
    refused = refusal(json.dumps(pause).encode(), pause, trust=store)
    assert (refused.code, refused.at) == ("unknown_key", "/issued_at")


@pytest.mark.parametrize(("record", "key"), [("certificate", "/body/application_key/public_key"),
                                             ("agreement", "/body/authority_keys/0/public_key")])
@pytest.mark.parametrize("change", ["respelled", "small-order"])
def test_a_trust_record_with_a_public_key_spelled_another_way_or_of_small_order_is_refused(
        record: str, key: str, change: str) -> None:
    """The same bytes with an unused low bit set, which the schema's pattern allows, or the identity point."""

    document = trust_example(record)
    holder = document["body"]["application_key"] if record == "certificate" else document["body"]["authority_keys"][0]
    alphabet = "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789-_"
    holder["public_key"] = (holder["public_key"][:-1] + alphabet[alphabet.index(holder["public_key"][-1]) ^ 1]
                            if change == "respelled" else IDENTITY_KEY)
    document["sequence"] += 1
    with pytest.raises(FrameRefused) as refused:
        fixtures.trust_store().add(json.dumps(sign_state(document, fixtures.private_key(document["writer"]["key_id"]))).encode(), now=fixtures.NOW)
    assert (refused.value.code, refused.value.at) == ("invalid_body", key)


def test_a_revoked_certificate_is_never_forgotten_so_its_key_cannot_join_again() -> None:
    store = fixtures.trust_store(add=["extra/lead-host-revoked-after-issue.json"])
    later = forgotten_from("certificate") + timedelta(days=400)
    store.forget(later)
    assert store.host_certificate("install_example", "lead-host-1", 3) is not None
    assert store.host_certificate("install_example", "worker-host-1", 3) is None
    join = fixtures.example_frame("host.join", "full")
    join["issuer"]["key_id"] = "test-lead-key-1"
    join["body"]["application_key"] = {"key_id": "test-lead-key-1", "public_key": fixtures.KEYS["test-lead-key-1"]["public_key"]}
    join["issued_at"], join["expires_at"] = (format_timestamp(later), format_timestamp(later + timedelta(seconds=30)))
    join["idempotency_key"] = expected_idempotency_key(join, kinds.KINDS["host.join"], False, None)
    data = json.dumps(sign_frame(join, fixtures.private_key("test-lead-key-1"))).encode()
    refused = refusal(data, join, {"now": format_timestamp(later)}, trust=store)
    assert (refused.code, refused.at) == ("revoked", "/issuer/key_id")
    # Read again from the bucket after forgetting, it is still taken back.
    reloaded = fixtures.trust_store(add=["extra/lead-host-revoked-after-issue.json"], remove=["certificates/lead-host-1.json"])
    reloaded.forget(later)
    assert reloaded.add((EXAMPLES / "trust/certificates/lead-host-1.json").read_bytes(), now=later) is True


def test_a_trust_record_dated_ahead_of_the_receivers_clock_is_refused() -> None:
    """Its longest lifetime counts from when it was written, so that time has come."""

    written = parse_timestamp("2026-10-22T00:00:00.000Z")
    record = certificate_record("ahead-host", roles=("lead_host",))
    assert fixtures.trust_store().add(record, now=written - SKEW) is True
    with pytest.raises(FrameRefused) as refused:
        fixtures.trust_store().add(record, now=written - SKEW - timedelta(milliseconds=1))
    assert (refused.value.code, refused.value.at) == ("not_yet_valid", "/issued_at")


def test_forgetting_drops_the_replacements_of_forgotten_certificates() -> None:
    store = fixtures.trust_store(extra_documents=[renewed_lead_certificate(issued_at="2026-10-22T04:20:00.000Z")])
    assert store._replacements  # noqa: SLF001 - what forget must shrink
    store.forget(forgotten_from("certificate") + timedelta(days=1))
    assert store.host_certificate("install_example", "lead-host-1", 4) is None
    assert not store._replacements  # noqa: SLF001


def test_forgetting_and_adding_need_an_aware_time() -> None:
    with pytest.raises(ValueError, match="time zone"):
        fixtures.trust_store().forget(fixtures.NOW.replace(tzinfo=None))
    with pytest.raises(ValueError, match="time zone"):
        fixtures.trust_store().add(certificate_record("naive-host", roles=("lead_host",)), now=fixtures.NOW.replace(tzinfo=None))


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

    def current(moment):
        """The receiver at ``moment`` holds a revocations record current then (README, "Trust over time")."""

        return fixtures.trust_store(now=moment, extra_documents=[fixtures.current_revocations(moment)])

    late = issued_at + limit + SKEW
    fixtures.verify(data, frame, at(late - millisecond), trust=current(late - millisecond))
    assert refusal(data, frame, at(late), trust=current(late)).code == "expired"
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
    latest = parse_timestamp("9999-12-31T23:59:59.999Z")
    store = fixtures.trust_store(extra_documents=[certificate_record(
        "far-host", roles=("lead_host",), issued_at="9999-10-02T23:59:59.999Z", expires_at="9999-12-31T23:59:59.999Z"),
        fixtures.current_revocations(latest)], now=latest)
    frame = signed_example()
    frame["issuer"] = {"installation": "install_example", "team": "team_example", "runtime": "far-host", "membership_epoch": 1,
                       "revocation_epoch": 1, "key_id": "test-worker-key-1"}
    frame["subject"] = "ua.v1.team_example.far-host.work.control.researcher-2"
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
