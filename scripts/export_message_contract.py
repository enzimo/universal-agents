#!/usr/bin/env python3
"""Generate, or check, the files derived from the protocol 1 message contract.

Sources (written by hand) are under contracts/messages/v1: registry.yaml,
transport.yaml, team-topologies.yaml and schemas/. This script validates them and
writes:

- contracts/messages/v1/generated/: JSON copies of the YAML files, per-role NATS
  permissions, the AsyncAPI 3.1 document, the compatibility record (with the digest
  of the contract's sources) and a digest manifest of every source and generated file;
- custom_messages/wire/models.py: Pydantic models for the shared values, the
  frame and every body;
- custom_messages/wire/kinds.py: the registry as Python constants, each kind's
  body and reply model, each state record's model, and the body shapes used to
  drop unknown fields that a newer minor version may send;
- the digests, signatures and invalid frame texts of the examples, trust
  fixtures and signing vectors (scripts/message_contract_examples.py).

``--check`` renders in memory and fails on any difference, and on any file in a
generated folder that this script does not write.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import keyword
import re
import sys
from pathlib import Path
from typing import Any

import yaml
from jsonschema import Draft202012Validator

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from scripts.contract_schema_export import canonical_json_text as json_text  # noqa: E402
from scripts.message_contract_examples import render_examples  # noqa: E402

CONTRACT = REPO_ROOT / "contracts" / "messages" / "v1"
SCHEMAS = CONTRACT / "schemas"
GENERATED = CONTRACT / "generated"
WIRE = REPO_ROOT / "custom_messages" / "wire"
SCHEMA_BASE = "https://universal-agents.local/contracts/messages/v1/schemas/"

CLASSES = {"durable", "request_reply", "event", "dead_letter"}
VERIFICATION = {"standard", "proof_of_possession"}
# How strongly a kind asks for an authority token, weakest first.
AUTHORITY = ("none", "optional", "required")
ROUTING = {"requester", "holder", "team", "person", "named", "broadcast", "sender"}
EDGES = {"up", "down", "person", "sideways", "out", "none"}
WAKES = {"never", "always", "next_turn"}
RENDERING = {"none", "sender_line", "summary", "full"}
PRIORITIES = {"low", "normal", "high"}
ENTRY_FIELDS = {
    "description", "class", "address", "body", "reply", "senders", "receivers", "expiry_seconds",
    "authority", "idempotency", "admission", "routing", "topology_edge", "ledger", "wakes_model",
    "rendering", "covered_change", "copy_to", "senders_by_address", "authority_binding", "body_rules", "verification",
    "authority_by_address",
}
REQUIRED_ENTRY_FIELDS = ENTRY_FIELDS - {"reply", "covered_change", "copy_to", "senders_by_address", "authority_binding",
                                        "body_rules", "verification", "authority_by_address"}

# The schema subset (contracts/messages/v1/README.md).
ALLOWED_KEYWORDS = {
    "$schema", "$id", "$ref", "$defs", "title", "description", "type", "enum", "const", "properties",
    "required", "additionalProperties", "items", "minItems", "maxItems", "minLength",
    "maxLength", "pattern", "minimum", "maximum", "oneOf", "examples",
}
ALLOWED_TYPES = {"object", "string", "integer", "boolean", "array"}
# Keywords that check a value only of one type. Generated models apply them only
# there, so anywhere else they would be dropped silently.
TYPED_KEYWORDS = {
    "minLength": "string", "maxLength": "string", "pattern": "string",
    "minimum": "integer", "maximum": "integer",
    "items": "array", "minItems": "array", "maxItems": "array",
    "properties": "object", "required": "object", "additionalProperties": "object",
}
# Keywords that change no validation; only these may stand beside $ref, const or
# enum, whose generated models ignore every other keyword.
ANNOTATIONS = {"title", "description", "examples"}
MAX_SAFE_INTEGER = 2**53 - 1
# Escaped characters that every common engine reads as the character itself.
PORTABLE_ESCAPES = set(".-\\[](){}*+?|^$/")
# A bounded repetition: {n}, {n,} or {n,m}, the only forms a pattern's braces may take.
REPETITION = re.compile(r"\{(\d+)(,(\d*))?\}")
# Characters an id may hold (common.schema.json token_id). Consumer names join ids
# with a character outside this set.
ID_CHARACTER = re.compile(r"[A-Za-z0-9_-]")
# Address placeholders that name the receiver. A sender may address any receiver in
# its team. Subscriptions and consumer grants keep the placeholder, and membership code
# fills it with each id the host runs or serves, so a host reads only its own queues.
RECEIVER_PLACEHOLDERS = ("agent", "pool", "edge")
# Other address placeholders: a dead-letter copy id (the body's copy_id), and the
# sender of an installation address (<installation scope>.{sender}.svc).
COPY_PLACEHOLDER, SENDER_PLACEHOLDER = "copy", "sender"
# Hosts that send frames for agents; a kind no agent sends is not a topology check.
AGENT_HOSTS = {"lead_host", "worker_host"}
# Audience targets that get durable copies, and the roles that read them there: the
# team services record custody facts. Another role serving the same target (the
# scheduler answers run_context.sign at svc.scheduler) never reads copies.
COPY_READERS = {"team_services": ["team_services"]}
# The token type bound to one command; its binding always names the command id (`at`).
# The host role that speaks for outside agents (README, "On behalf of a person").
OUTSIDE_SPEAKER = "edge_adapter"
COMMAND_BINDING = "command"
# The permission set of a host that has not joined yet (it holds no role): it only sends
# its own join request and reads its own reply inbox (README, "Installation traffic").
JOINING = "joining_host"
# A kind (with ".reply") within 84 characters keeps its idempotency key, the kind,
# a colon and 43 digest characters, within the 128 that common.schema.json allows.
MAX_KIND_LENGTH = 84
# The frame's and the state record's bodies are the open objects: the schema their
# kind or record names checks them.
OPEN_OBJECTS = {("frame.schema.json", "/properties/body"), ("state.schema.json", "/properties/body")}
# Who sends the version a covered change was based on (README, "Covered changes"), and
# whether the body's seen_version is then required, optional or absent.
SEEN_VERSION_FIELD = {"required": "required", "required_for_request_writers": "optional", "optional": "optional",
                      "none": "absent"}
# Shared refusals that only the store-safety checks of covered records give: another
# request's change, an escalated record, a file or sign-in another request writes.
# changed_since_read and request_closed also answer the lead's operations on its own
# request (intent.set, requests.pause), which change no covered record.
COVERED_ONLY_REFUSALS = {"change_conflict", "needs_person_decision", "record_busy"}
# Frame fields that differ on every frame. A retry is a new frame, so a key made from
# one of them would never repeat and could not find the stored outcome.
PER_FRAME_PARTS = {"/message_id", "/idempotency_key", "/issued_at", "/not_before", "/expires_at", "/body_digest",
                   "/signature"}
# The NATS headers a sender may set (README, "NATS headers"), each with the longest value it
# holds: the idempotency key and the kind (the common schema definition whose maxLength
# bounds them), the message id (a UUID's 36 characters), and W3C Trace Context's traceparent
# (always 55 characters) and tracestate (senders keep it within the 512 characters W3C asks
# every system to pass on).
HEADER_VALUES: dict[str, str | int] = {"Nats-Msg-Id": "idempotency_key", "UA-Kind": "kind", "UA-Id": 36,
                                       "traceparent": 55, "tracestate": 512}
# NATS's header block: a version line, one "Name: value" line per header, and an empty line.
NATS_HEADER_VERSION, NATS_LINE_END = "NATS/1.0\r\n", "\r\n"
STATE_FIELDS = {"description", "bucket", "key", "signer", "signer_roles", "writers", "body", "ttl_seconds", "max_lifetime_seconds",
                "fresh_seconds", "write_rule"}
REQUIRED_STATE_FIELDS = {"description", "bucket", "key", "signer", "writers", "body", "write_rule"}
SIGNERS = {"authority", "host"}


class ContractError(ValueError):
    pass


def load_yaml(name: str) -> dict[str, Any]:
    data = yaml.safe_load((CONTRACT / name).read_text(encoding="utf-8"))
    if not isinstance(data, dict):
        raise ContractError(f"{name} must hold a mapping")
    return data


# Names transport.yaml defines once: the scope installation-wide traffic names instead
# of a team, and the subject root before the team token (ua.v1.). Every installation
# address and subject this script builds follows them.
_NAMES = load_yaml("transport.yaml")
INSTALLATION_SCOPE = str(_NAMES["installation_scope"])
INSTALLATION_ADDRESS = INSTALLATION_SCOPE + ".{" + SENDER_PLACEHOLDER + "}."
SUBJECT_ROOT = str(_NAMES["subject_prefix"]).split("{team}", 1)[0]


def load_schemas() -> dict[str, dict[str, Any]]:
    schemas = {}
    for path in sorted(SCHEMAS.rglob("*.schema.json")):
        relative = path.relative_to(SCHEMAS).as_posix()
        schema = json.loads(path.read_text(encoding="utf-8"))
        if schema.get("$id") != SCHEMA_BASE + relative:
            raise ContractError(f"{relative}: $id must be {SCHEMA_BASE + relative}")
        if schema.get("$schema") != "https://json-schema.org/draft/2020-12/schema":
            raise ContractError(f"{relative}: $schema must name draft 2020-12")
        Draft202012Validator.check_schema(schema)
        check_subset(relative, schema, "")
        schemas[relative] = schema
    return schemas


def pattern_problem(pattern: str) -> str | None:
    """Why ``pattern`` could match differently in Python, ECMAScript or Pydantic's engine, if it could.

    Allowed: literal characters, character classes, escaped punctuation, groups,
    ``(?:``, alternatives inside a group, and one quantifier per atom (?, *, +, {n},
    {n,} or {n,m}), optionally lazy. ``$`` means the end of the value; Python's ``re``
    also matches it before a final newline, which the README tells Python validators to
    refuse. A ``]`` directly after ``[`` or ``[^`` is refused: Python reads it as a literal,
    ECMAScript as the end of an empty class (``[]`` never matches, ``[^]`` matches anything).
    So is a ``]`` outside a class, which ECMAScript's Unicode mode refuses, like a lone ``}``.
    A pattern Python's ``re`` cannot compile, or whose repetition count is past its limit
    (``re`` raises OverflowError, not re.error), is refused too, as a backstop.
    """

    if len(pattern) < 2 or not (pattern.startswith("^") and pattern.endswith("$")) or pattern.endswith("\\$"):
        return "anchor the whole pattern with ^ and $"
    body, depth, in_class, index = pattern[1:-1], 0, False, 0
    # What the previous token was, for the quantifier that may follow it: "atom", "quantifier"
    # (a complete repetition, which only a lazy ? may follow) or "lazy" (nothing may follow).
    previous = "start"
    while index < len(body):
        character, following = body[index], body[index + 1:index + 2]
        if character == "\\":
            if following not in PORTABLE_ESCAPES:
                return f"\\{following} reads differently between engines; list the characters instead"
            index, previous = index + 2, "atom"
            continue
        if in_class:
            if character == "[":
                return "a [ inside a character class, such as [[:alpha:]], reads differently between engines"
            in_class = character != "]"
            index, previous = index + 1, "atom"
            continue
        if character in "*+?{":
            problem, index = _quantifier_problem(body, index, previous)
            if problem:
                return problem
            previous = "lazy" if previous == "quantifier" else "quantifier"
            continue
        if character == "[":
            if body.startswith("]", index + 1) or body.startswith("^]", index + 1):
                return "a ] directly after [ or [^ reads differently between engines; escape a literal ] as \\]"
            in_class = True
        elif character == "(":
            if following == "?" and body[index + 2:index + 3] != ":":
                return "group extensions other than (?: differ between engines"
            depth += 1
            index += 2 if following == "?" else 0  # step over ?: so it is not read as a quantifier
        elif character == ")":
            depth -= 1
        elif character == "|" and depth == 0:
            return "group top-level alternatives, or ^ and $ apply to only one of them"
        elif character == ".":
            return "an unescaped . matches different characters in different engines"
        elif character in "^$":
            return "anchors belong only at the two ends"
        elif character == "}":
            return "a } closes only {n}, {n,} or {n,m}; escape a literal brace as \\}"
        elif character == "]":
            return "a ] outside a character class is an error in ECMAScript's Unicode mode; escape it as \\]"
        previous = "atom" if character not in "(|" else "start"
        index += 1
    if in_class:
        return "a character class is not closed"
    try:
        re.compile(pattern)
    except (re.error, OverflowError) as error:
        return f"Python's re refuses it ({error})"
    return None


def _quantifier_problem(body: str, index: int, previous: str) -> tuple[str | None, int]:
    """Why the quantifier at ``body[index]`` is not portable, if it is not, and the index after it.

    One quantifier per atom, optionally followed by one lazy ``?``: engines disagree on a
    quantifier after another (Python refuses ``a{2}{3}``, Pydantic's engine repeats the
    repetition, so ``[a-z]{100}{1000}`` matches 100,000 characters there), on a possessive
    ``+`` and on a quantifier with nothing to repeat. A brace must start {n}, {n,} or {n,m}:
    Python reads {,n} as 0 to n, ECMAScript without the u flag reads it as literal text, and
    Pydantic's engine refuses it; these are the forms pattern_longest_match counts.
    """

    character = body[index]
    if previous == "quantifier" and character == "+":
        return "possessive quantifiers are not portable", index
    if previous == "quantifier" and character == "?":
        return None, index + 1
    if previous in ("quantifier", "lazy"):
        return "a quantifier directly after another reads differently between engines", index
    if previous == "start":
        return "a quantifier needs something before it to repeat", index
    if character != "{":
        return None, index + 1
    repetition = REPETITION.match(body, index)
    if repetition is None:
        return "a { starts only {n}, {n,} or {n,m}; escape a literal brace as \\{", index
    low, high = repetition.group(1), repetition.group(3)
    if high and int(high) < int(low):
        return f"the repetition {repetition.group(0)} has its upper bound below its lower one", index
    return None, repetition.end()


def _of_type(value: Any, kind: str) -> bool:
    if kind == "string":
        return isinstance(value, str)
    if kind == "integer":
        return isinstance(value, int) and not isinstance(value, bool)
    return kind == "boolean" and isinstance(value, bool)


def _check_values(where: str, node: dict[str, Any], kind: Any) -> None:
    """``const`` and ``enum`` hold scalars of the stated type, with only annotations beside them."""

    for word in ("const", "enum"):
        if word not in node:
            continue
        values = [node["const"]] if word == "const" else node["enum"]
        if any(value is None or isinstance(value, (dict, list, float)) for value in values):
            raise ContractError(f"{where}: {word} values are strings, integers or booleans, never null")
        beside = set(node) - ANNOTATIONS - {word, "type"}
        if beside:
            raise ContractError(f"{where}: generated models would drop {sorted(beside)} beside {word}")
        if kind is not None and not all(_of_type(value, kind) for value in values):
            raise ContractError(f"{where}: {word} values must have type {kind}")


def check_subset(name: str, node: Any, pointer: str) -> None:
    """Refuse anything outside the documented schema subset.

    The walk follows keywords, so a property or definition named like a keyword
    (``properties``, ``$defs``) is still a schema and still checked. Every rule
    here keeps the generated models exactly as strict as the schema.
    """

    where = f"{name}{pointer}"
    if not isinstance(node, dict):
        raise ContractError(f"{where}: a schema must be an object")
    unknown = set(node) - ALLOWED_KEYWORDS
    if unknown:
        raise ContractError(f"{where}: keywords outside the subset: {sorted(unknown)}")
    kind = node.get("type")
    if kind is not None and (not isinstance(kind, str) or kind not in ALLOWED_TYPES):
        raise ContractError(f"{where}: type {kind!r} is outside the subset")
    for word, expected in TYPED_KEYWORDS.items():
        if word in node and kind != expected:
            raise ContractError(f"{where}: {word} needs type {expected}")
    if "$ref" in node:
        if "://" in node["$ref"]:
            raise ContractError(f"{where}: $ref must be local to the contract")
        if set(node) - ANNOTATIONS - {"$ref"}:
            raise ContractError(f"{where}: generated models would drop {sorted(set(node) - ANNOTATIONS - {'$ref'})} beside $ref")
    _check_values(where, node, kind)
    if kind == "object" and (name, pointer) not in OPEN_OBJECTS:
        if node.get("additionalProperties") is not False or "properties" not in node:
            raise ContractError(f"{where}: objects list their properties and set additionalProperties false")
    if "additionalProperties" in node and node["additionalProperties"] is not False:
        raise ContractError(f"{where}: additionalProperties may only be false")
    missing = set(node.get("required", [])) - set(node.get("properties", {}))
    if missing:
        raise ContractError(f"{where}: required names properties the object does not define: {sorted(missing)}")
    if kind == "integer" and "const" not in node and "enum" not in node:
        for bound in ("minimum", "maximum"):
            if bound not in node or abs(node[bound]) > MAX_SAFE_INTEGER:
                raise ContractError(f"{where}: integers state {bound} within ±(2^53 - 1)")
    if kind == "array" and "items" not in node:
        raise ContractError(f"{where}: arrays state their items")
    if "pattern" in node:
        problem = pattern_problem(node["pattern"])
        if problem:
            raise ContractError(f"{where}: pattern {node['pattern']!r} is not portable: {problem}")
    if "oneOf" in node:
        discriminator(name, pointer, node)
    for word in ("properties", "$defs"):
        for key, child in node.get(word, {}).items():
            check_subset(name, child, f"{pointer}/{word}/{key}")
    if "items" in node:
        check_subset(name, node["items"], f"{pointer}/items")
    for index, variant in enumerate(node.get("oneOf", [])):
        check_subset(name, variant, f"{pointer}/oneOf/{index}")


def literal(values: list[Any]) -> str:
    """A ``Literal`` of ``values``. Pydantic lets ``1`` match ``True`` and ``True`` match ``1``
    in a ``Literal``, even in strict mode, so numbers and booleans also check their type."""

    text = "Literal[" + ", ".join(repr(value) for value in values) + "]"
    if not any(isinstance(value, int) for value in values):
        return text
    types = ", ".join(sorted({type(value).__name__ for value in values}))
    return f"Annotated[{text}, BeforeValidator(exact_type({types}))]"


def discriminator(name: str, pointer: str, node: dict[str, Any]) -> str:
    """The property whose ``const`` tells the alternatives of a ``oneOf`` apart."""

    variants = node["oneOf"]
    common: set[str] | None = None
    for variant in variants:
        consts = {key for key, value in variant.get("properties", {}).items() if "const" in value}
        common = consts if common is None else common & consts
    if not common or len(common) != 1:
        raise ContractError(f"{name}{pointer}: oneOf alternatives need one shared const property")
    key = next(iter(common))
    if not all(key in variant.get("required", []) for variant in variants):
        raise ContractError(f"{name}{pointer}: every oneOf alternative requires its {key!r} property")
    values = [variant["properties"][key]["const"] for variant in variants]
    if not all(isinstance(value, str) for value in values):
        raise ContractError(f"{name}{pointer}: oneOf alternatives are told apart by strings")
    if len(set(values)) != len(values):
        raise ContractError(f"{name}{pointer}: oneOf const values must differ")
    return key


def check_registry(registry: dict[str, Any], transport: dict[str, Any], topologies: dict[str, Any],
                   schemas: dict[str, dict[str, Any]]) -> None:
    check_state_records(registry, transport, schemas)
    check_shared_lists(registry, transport, topologies, schemas)
    if not 0 < registry["command_token_max_seconds"] <= registry["authority_token_max_seconds"]:
        raise ContractError("registry.yaml: command_token_max_seconds is positive and within authority_token_max_seconds")
    overlap = registry["certificate_overlap_seconds"]
    if not isinstance(overlap, int) or not 0 < overlap <= 3600:
        raise ContractError("registry.yaml: certificate_overlap_seconds is a short positive deadline, at most an hour")
    check_name_lists(registry)
    edges = set(EDGES) - {"none"}
    if set(topologies["edges"]) != edges:
        raise ContractError(f"team-topologies.yaml: edges describes exactly {sorted(edges)}")
    for topology, spec in topologies["topologies"].items():
        for role, allowed in spec["allowed_edges"].items():
            if role not in topologies["agent_roles"] or not set(allowed) <= edges:
                raise ContractError(f"team-topologies.yaml: {topology}.{role} names an unknown role or edge")
    for name, entry in registry["kinds"].items():
        where = f"registry.yaml kinds.{name}"
        check_entry_fields(where, name, entry, registry, transport, schemas)
        check_entry_addresses(where, name, entry, transport)
        check_agent_queue(where, entry, transport)
        check_agent_addressed_edges(where, entry)
        check_idempotency(where, name, entry, schemas, registry["frame_schema"])
        check_body_rules(where, entry, schemas, registry["frame_schema"])
        check_wake_alternatives(where, entry, schemas)
        check_reply_errors(where, entry, schemas)
        check_covered_change(where, entry, registry, schemas)
    for kind in topologies.get("requester_kinds", []):
        entry = registry["kinds"].get(kind)
        if entry is None or "down" not in topology_edges(entry):
            raise ContractError(f"team-topologies.yaml: requester_kinds names {kind}, which is not a kind sent down")
    check_shared_addresses(registry, transport)
    check_object_store(registry, transport, schemas)
    check_nats_account(registry, transport, schemas)
    check_knowledge_text(registry, schemas)
    check_ledger_waits(registry)
    names: dict[str, str] = {}
    for name in registry["kinds"]:
        # Generated model and adapter names come from the kind name; two kinds
        # with one name would silently share the second kind's schema.
        other = names.setdefault(class_name(name), name)
        if other != name:
            raise ContractError(f"registry.yaml: kinds {other} and {name} generate the same model names")


def check_ledger_waits(registry: dict[str, Any]) -> None:
    """A wait one kind's ledger entry closes is one another kind's entry opens, and the other way round.

    A work ledger that closes a wait nothing opened, or opens one nothing closes, would
    lose or keep a waiting task's state with no record of why (registry header, ``ledger``).
    """

    ledgers = {entry["ledger"] for entry in registry["kinds"].values()}
    for value in sorted(ledgers):
        for end, other in (("_wait_closed", "_wait_open"), ("_wait_open", "_wait_closed")):
            if value.endswith(end) and value[:-len(end)] + other not in ledgers:
                raise ContractError(f"registry.yaml: ledger {value} has no {value[:-len(end)] + other} in any kind")


def check_entry_fields(where: str, name: str, entry: dict[str, Any], registry: dict[str, Any],
                       transport: dict[str, Any], schemas: dict[str, dict[str, Any]]) -> None:
    """A kind's name, its fields and their values, its lifetime and its schemas."""

    if not re.fullmatch(r"[a-z][a-z0-9_]*(\.[a-z][a-z0-9_]*)+", name) or name.endswith(".reply"):
        raise ContractError(f"{where}: bad kind name")
    if len(name + ".reply") > MAX_KIND_LENGTH:
        raise ContractError(f"{where}: a kind and its reply name stay within {MAX_KIND_LENGTH} characters, so keys fit")
    missing = REQUIRED_ENTRY_FIELDS - set(entry)
    unknown = set(entry) - ENTRY_FIELDS
    if missing or unknown:
        raise ContractError(f"{where}: missing {sorted(missing)}, unknown {sorted(unknown)}")
    checks = {
        "class": (entry["class"], CLASSES), "authority": (entry["authority"], set(AUTHORITY)),
        "routing": (entry["routing"], ROUTING), "rendering": (entry["rendering"], RENDERING),
        "admission.priority": (entry["admission"].get("priority"), PRIORITIES),
    }
    for field_name, (value, allowed) in checks.items():
        if value not in allowed:
            raise ContractError(f"{where}: {field_name} {value!r} is not one of {sorted(allowed)}")
    check_routing_rules(where, entry)
    roles = set(registry["host_roles"])
    if not set(entry["senders"]) <= roles or not set(entry["receivers"]) <= roles:
        raise ContractError(f"{where}: senders and receivers must be host roles")
    if not 1 <= entry["expiry_seconds"] <= transport["classes"][entry["class"]]["max_expiry_seconds"]:
        raise ContractError(f"{where}: expiry must be positive and within its class's max_expiry_seconds")
    if entry["body"] not in schemas:
        raise ContractError(f"{where}: body schema {entry['body']} is missing")
    if (entry["class"] == "request_reply") != ("reply" in entry):
        raise ContractError(f"{where}: request/reply kinds, and only they, have a reply")
    if "reply" in entry and entry["reply"]["body"] not in schemas:
        raise ContractError(f"{where}: reply schema {entry['reply']['body']} is missing")
    check_authority_by_address(where, entry)
    check_pool_tokens(where, entry)
    check_authority_binding(where, entry, schemas)
    if entry.get("verification", "standard") not in VERIFICATION:
        raise ContractError(f"{where}: verification must be one of {sorted(VERIFICATION)}")
    if entry.get("verification") == "proof_of_possession" and (
            entry["class"] != "request_reply" or entry["authority"] != "none"
            or not all(address.startswith(INSTALLATION_ADDRESS) for address in own_addresses(entry))):
        raise ContractError(f"{where}: only an installation request without an authority token proves possession of its own key")


def check_routing_rules(where: str, entry: dict[str, Any]) -> None:
    """Edges, wake rule and admission: how receivers route and admit the kind."""

    edges = topology_edges(entry)
    if not edges or not set(edges) <= EDGES or ("none" in edges and len(edges) > 1):
        raise ContractError(f"{where}: topology_edge is an edge, a list of edges, or none alone")
    if not set(entry["senders"]) & AGENT_HOSTS and edges != ["none"]:
        raise ContractError(f"{where}: only hosts send it, so it is not a topology check: topology_edge none")
    check_wakes(where, entry["wakes_model"])
    if set(entry["admission"]) != {"execution_slot", "accepted_while_paused", "priority"}:
        raise ContractError(f"{where}: admission states execution_slot, accepted_while_paused and priority")


def check_agent_queue(where: str, entry: dict[str, Any], transport: dict[str, Any]) -> None:
    """A kind sent to an agent waits in its inbox only when it needs a free slot (README, "An agent's two queues").

    The host reads the inbox only while the agent can start more work, so a message the
    host could act on at once would wait there behind tasks it cannot start yet: a stop
    or a custody renewal for the very task that holds the slot. The control queue is read
    whenever the host runs the agent, paused or not, so everything on it is taken at once.
    """

    queues = transport["agent_queues"]
    admission = entry["admission"]
    for address in own_addresses(entry):
        if address == queues["inbox"] and not admission["execution_slot"]:
            raise ContractError(f"{where}: it needs no execution slot, so it goes to the agent's control queue "
                                f"({queues['control']}), where it never waits behind new work")
        if address == queues["inbox"] and admission["accepted_while_paused"]:
            raise ContractError(f"{where}: a paused agent's host does not read its inbox, so a kind sent there is not "
                                "accepted while paused")
        if address == queues["control"] and admission["execution_slot"]:
            raise ContractError(f"{where}: it needs an execution slot, so it waits in the agent's inbox "
                                f"({queues['inbox']}): the host reads the control queue even when no slot is free")
        if address == queues["control"] and not admission["accepted_while_paused"]:
            raise ContractError(f"{where}: the host reads the control queue while the agent is paused, so a kind sent "
                                "there is accepted while paused")


def check_agent_addressed_edges(where: str, entry: dict[str, Any]) -> None:
    """A kind an agent sends to another agent, a worker pool or an edge route names the edges it may take.

    ``none`` means "not a topology check" (README, "Who may talk to whom"): the
    kind goes to the team services or only hosts send it. When a lead or worker
    host may send it to a receiver's own address, the receiver checks the edge,
    so ``none`` would let any agent role send it anywhere. A refusal answered to
    the refused frame's sender (routing ``sender``) goes back wherever that frame
    came from, so it is no topology check either.
    """

    if topology_edges(entry) != ["none"] or entry["routing"] == "sender":
        return
    for address in own_addresses(entry):
        if audience_target(address) in RECEIVER_PLACEHOLDERS and set(address_senders(entry, address)) & AGENT_HOSTS:
            raise ContractError(f"{where}: agent hosts send it to {address}, so it is a topology check: "
                                "name the edges it may take instead of none")



def address_authority(entry: dict[str, Any], address: str) -> str:
    """Whether frames to one of the kind's addresses carry a token: its own authority, or a narrower one for that address."""

    return str(entry.get("authority_by_address", {}).get(address, entry["authority"]))


def check_authority_by_address(where: str, entry: dict[str, Any]) -> None:
    """``authority_by_address`` only narrows the kind's authority, for some of its own addresses."""

    narrower = entry.get("authority_by_address", {})
    if not isinstance(narrower, dict):
        raise ContractError(f"{where}: authority_by_address maps some of its own addresses to an authority")
    for address, authority in narrower.items():
        if address not in own_addresses(entry) or authority not in AUTHORITY \
                or AUTHORITY.index(authority) >= AUTHORITY.index(entry["authority"]):
            raise ContractError(f"{where}: authority_by_address narrows the authority of one of its own addresses")


def token_bindings(entry: dict[str, Any]) -> list[dict[str, Any]]:
    """The kind's ``authority_binding`` as a list: one binding, or a list of them, one per token type."""

    binding = entry.get("authority_binding")
    if binding is None:
        return []
    return list(binding) if isinstance(binding, list) else [binding]


def check_pool_tokens(where: str, entry: dict[str, Any]) -> None:
    """Every host that runs a worker pool reads its queue, so no kind carries a token into one (README, "On behalf of a person")."""

    for address in own_addresses(entry):
        if audience_target(address) == "pool" and address_authority(entry, address) != "none":
            raise ContractError(f"{where}: every host running a pool reads its queue, so {address} carries no authority "
                                "token: set its authority_by_address to none")


def token_binding_types(schemas: dict[str, dict[str, Any]]) -> list[str]:
    """The record types a token is bound to: the ``type`` constants of ``bound_to``'s alternatives.

    Only the root task's alternative carries the person's request id, and it must
    (13C.5): grants, change-ledger rows and the coordination registry are keyed by it.
    """

    bound_to = schemas["common.schema.json"]["$defs"]["authority_token"]["properties"]["bound_to"]
    variants = {variant["properties"]["type"]["const"]: variant for variant in bound_to["oneOf"]}
    for name, variant in variants.items():
        if ("request_id" in variant["required"]) != (name == "root_task") or \
                ("request_id" in variant["properties"]) != (name == "root_task"):
            raise ContractError("common.schema.json: authority_token.bound_to: the root_task binding, and only it, "
                                "carries and requires request_id")
    return list(variants)


def check_authority_binding(where: str, entry: dict[str, Any], schemas: dict[str, dict[str, Any]]) -> None:
    """A kind that takes a token lists each token type it takes, where the body names its record, and who presents it.

    A binding is ``{type, at?, senders?}``. A command token always names its
    command in the body (``at``): a command token is bound to its command's id, and
    the receiver checks the field that names it. ``senders`` narrows who may
    present that type; without it every sender of the frame's address may. A kind
    that requires a token must accept one from each of its senders.
    """

    bindings = token_bindings(entry)
    if bool(bindings) != (entry["authority"] != "none"):
        raise ContractError(f"{where}: kinds that take an authority token, and only they, state authority_binding")
    types = token_binding_types(schemas)
    named = [binding.get("type") if isinstance(binding, dict) else None for binding in bindings]
    if not all(isinstance(binding, dict) and not set(binding) - {"type", "at", "senders"} for binding in bindings) \
            or not all(isinstance(kind, str) and kind in types for kind in named) or len(set(named)) != len(named):
        raise ContractError(f"{where}: authority_binding is a binding {{type, at, senders}}, or a list of them, each "
                            f"with its own type, one of {types}")
    for binding in bindings:
        check_binding_parameters(where, entry, schemas, binding)
    check_binding_presenters(where, entry, bindings)
    # Outside agents never hold a person's token, so the role that speaks for them presents none.
    if OUTSIDE_SPEAKER in entry["senders"] and any(OUTSIDE_SPEAKER in binding.get("senders", entry["senders"])
                                                   for binding in bindings):
        raise ContractError(f"{where}: the {OUTSIDE_SPEAKER} speaks for outside agents and presents no token; leave it "
                            "out of the binding's senders")


def check_binding_parameters(where: str, entry: dict[str, Any], schemas: dict[str, dict[str, Any]],
                             binding: dict[str, Any]) -> None:
    """One binding names a valid record field and narrows the kind's senders."""

    if binding["type"] == COMMAND_BINDING and "at" not in binding:
        raise ContractError(f"{where}: a command token is bound to the command its body names: give its binding `at`")
    if "at" in binding:
        check_binding_field(where, entry, schemas, binding["at"])
    if "senders" in binding and (not binding["senders"] or not set(binding["senders"]) <= set(entry["senders"])):
        raise ContractError(f"{where}: an authority_binding's senders are some of the kind's senders")


def check_binding_presenters(where: str, entry: dict[str, Any], bindings: list[dict[str, Any]]) -> None:
    """Every sender of a token-required operation can present one of its accepted token types."""

    for address in own_addresses(entry):
        if address_authority(entry, address) != "required":
            continue
        for sender in address_senders(entry, address):
            if not any(sender in binding.get("senders", [sender]) for binding in bindings):
                raise ContractError(f"{where}: {sender} sends it to {address} but may present none of its token types")


def check_binding_field(where: str, entry: dict[str, Any], schemas: dict[str, dict[str, Any]], at: str) -> None:
    """A binding's ``at`` names a string or integer body field that every body carries."""

    steps = at.split("/")
    leaves = schema_leaves(ModelWriter(schemas), entry["body"], schemas[entry["body"]], steps[2:]) \
        if at.startswith("/body/") else []
    if not leaves or not all(scalar_schema(ModelWriter(schemas), file, leaf) for file, leaf in leaves) \
            or not always_supplied(entry, schemas, "frame.schema.json", steps[1:]):
        raise ContractError(f"{where}: authority_binding at {at} names no field every body carries")


def topology_edges(entry: dict[str, Any]) -> list[str]:
    edge = entry["topology_edge"]
    return list(edge) if isinstance(edge, list) else [edge]


def address_senders(entry: dict[str, Any], address: str) -> list[str]:
    """The roles that may publish the kind to one of its addresses; a copy has its kind's senders."""

    return list(entry.get("senders_by_address", {}).get(address, entry["senders"]))


def check_entry_addresses(where: str, name: str, entry: dict[str, Any], transport: dict[str, Any]) -> None:
    """Each address fits its class; each delivers to its own target and has a reader; copies go to custody inboxes."""

    for address in entry_addresses(entry):
        if not any(address_matches(address, template, name) for template in class_addresses(transport, entry["class"])):
            raise ContractError(f"{where}: address {address!r} fits no {entry['class']} address")
        allowed = set(RECEIVER_PLACEHOLDERS) | {COPY_PLACEHOLDER}
        if address.startswith(INSTALLATION_ADDRESS):
            allowed.add(SENDER_PLACEHOLDER)
        if not set(re.findall(r"\{([a-z_]+)\}", address)) <= allowed:
            raise ContractError(f"{where}: an address's placeholders name its receiver, a copy or an installation sender")
    targets = [audience_target(address) for address in entry_addresses(entry)]
    if len(set(targets)) != len(targets):
        raise ContractError(f"{where}: two of its addresses deliver to one audience target")
    check_entry_receivers(where, entry, transport)


def check_entry_receivers(where: str, entry: dict[str, Any], transport: dict[str, Any]) -> None:
    """Every address has a reader, every listed receiver serves one of the kind's own addresses, copies go to custody inboxes."""

    if entry.get("copy_to") and entry["class"] != "durable":
        raise ContractError(f"{where}: only durable kinds are copied to other inboxes")
    if any(audience_target(address) not in COPY_READERS for address in entry.get("copy_to", [])):
        raise ContractError(f"{where}: copies go only to {sorted(COPY_READERS)} inboxes")
    for address in entry_addresses(entry):
        target = audience_target(address)
        if not receiving_roles(entry, address, transport):
            raise ContractError(f"{where}: none of its receivers serves {target} audiences, so nobody receives {address}")
    own_targets = {audience_target(address) for address in own_addresses(entry)}
    idle = [role for role in entry["receivers"]
            if not any(role in transport["audience_targets"][target] for target in own_targets)]
    if idle:
        raise ContractError(f"{where}: receivers {idle} serve none of its addresses")
    for address, senders in entry.get("senders_by_address", {}).items():
        if address not in own_addresses(entry) or not senders or not set(senders) <= set(entry["senders"]):
            raise ContractError(f"{where}: senders_by_address narrows the senders of one of its own addresses")


def check_shared_addresses(registry: dict[str, Any], transport: dict[str, Any]) -> None:
    """Every role that receives at a team-wide address receives every kind sent there.

    Operations share their receiving service's address and the kind names the
    operation, so a role listening at an address hears every kind on it; one that
    did not answer some of them could still reply first, or read requests meant for
    another service. An agent, pool or edge address names its receiver, and only the
    host that runs it listens there.
    """

    receivers: dict[str, set[str]] = {}
    for name, entry in sorted(registry["kinds"].items()):
        for address in entry_addresses(entry):
            if audience_target(address) in RECEIVER_PLACEHOLDERS:
                continue
            roles = set(receiving_roles(entry, address, transport))
            if receivers.setdefault(address, roles) != roles:
                raise ContractError(f"registry.yaml kinds.{name}: {address} is received by {sorted(receivers[address])}, "
                                    f"so every kind sent there has those receivers, not {sorted(roles)}")


def own_addresses(entry: dict[str, Any]) -> list[str]:
    """The addresses a kind is delivered to as itself, without its copies."""

    return list(entry["address"]) if isinstance(entry["address"], list) else [entry["address"]]


def full_subject(address: str, owner: str) -> str:
    """The NATS subject of an address: team addresses sit under ua.v1.<team>.<sender>, installation ones under ua.v1.

    An installation address already names its scope and sender (_install.{sender}.svc).
    """

    if address.startswith(INSTALLATION_ADDRESS):
        return SUBJECT_ROOT + address.replace("{" + SENDER_PLACEHOLDER + "}", owner)
    return f"{SUBJECT_ROOT}{{team}}.{owner}." + address


def entry_addresses(entry: dict[str, Any]) -> list[str]:
    """Every address a kind is published to: its own, then the copies (``copy_to``)."""

    return own_addresses(entry) + list(entry.get("copy_to", []))


def receiving_roles(entry: dict[str, Any], address: str, transport: dict[str, Any]) -> list[str]:
    """The roles that receive the kind at this address (transport.yaml audience_targets).

    At its own addresses, the kind's receivers that serve the address's audience
    target: a role gets permissions only for the addresses it can serve. A copy
    goes to the copy target's readers that serve it (README, "Durable copies"),
    which are not receivers of the kind itself: the team services record a task's
    custody from its copy without the kind's slot, wake or rendering rules.
    """

    target = audience_target(address)
    serving = transport["audience_targets"][target]
    if address in entry.get("copy_to", []):
        return [role for role in COPY_READERS[target] if role in serving]
    return [role for role in entry["receivers"] if role in serving]


def check_idempotency(where: str, kind: str, entry: dict[str, Any], schemas: dict[str, dict[str, Any]],
                      frame_schema: str) -> None:
    """Each key part is a JSON pointer to a string or integer field of the kind's frame or body.

    An operation's retry key must come from what every caller supplies: required body
    or frame fields, or the audience id its address names. It never means a value
    inferred from an authority token.
    """

    pointers = entry["idempotency"]
    if not isinstance(pointers, list) or not pointers or len(set(pointers)) != len(pointers):
        raise ContractError(f"{where}: idempotency lists the JSON pointers whose values make the key, each once")
    resolver = ModelWriter(schemas)
    for pointer in pointers:
        if not isinstance(pointer, str) or not re.fullmatch(r"(/[a-z_]+)+", pointer):
            raise ContractError(f"{where}: idempotency part {pointer!r} is not a JSON pointer into the frame")
        steps = pointer[1:].split("/")
        if steps[0] == "body":
            leaves = schema_leaves(resolver, entry["body"], schemas[entry["body"]], steps[1:])
        else:
            leaves = schema_leaves(resolver, frame_schema, schemas[frame_schema], steps)
        if not leaves or not all(scalar_schema(resolver, file, leaf) for file, leaf in leaves):
            raise ContractError(f"{where}: idempotency part {pointer} names no string or integer field of {kind}")
        if entry["class"] == "request_reply" and not always_supplied(entry, schemas, frame_schema, steps):
            raise ContractError(f"{where}: idempotency part {pointer} is not supplied by every request")
    if not any(always_supplied(entry, schemas, frame_schema, pointer[1:].split("/")) for pointer in pointers):
        raise ContractError(f"{where}: idempotency names no part every frame carries, so frames without them share a key")


def check_wake_alternatives(where: str, entry: dict[str, Any], schemas: dict[str, dict[str, Any]]) -> None:
    """A per-alternative wake rule names a body oneOf's constant and gives every alternative, and only those, a value."""

    wakes = entry["wakes_model"]
    if isinstance(wakes, str):
        return
    *parent, key = str(wakes["by"]).split(".")
    resolver = ModelWriter(schemas)
    alternatives = None
    for file, node in schema_leaves(resolver, entry["body"], schemas[entry["body"]], parent, keep_one_of=True):
        if "oneOf" in node and discriminator(file, "", node) == key:
            alternatives = {variant["properties"][key]["const"] for variant in node["oneOf"]}
    if alternatives is None or alternatives != set(wakes["values"]):
        raise ContractError(f"{where}: wakes_model by {wakes['by']} must name a body oneOf and give each of its "
                            f"alternatives {sorted(alternatives or [])} a value")


def check_reply_errors(where: str, entry: dict[str, Any], schemas: dict[str, dict[str, Any]]) -> None:
    """An operation's ``reply.errors`` are exactly its reply schema's outcomes other than ``ok``.

    Replies tell their alternatives apart by the constant ``outcome``. The error
    list is what generated code and readers rely on, so a code the schema lacks
    would be promised but never sendable, and an outcome missing from the list
    would be sendable but undocumented.
    """

    if "reply" not in entry:
        return
    errors = entry["reply"]["errors"]
    outcomes = [variant.get("properties", {}).get("outcome", {}).get("const")
                for _file, variant in reply_alternatives(entry, schemas)]
    if None in outcomes or "ok" not in outcomes:
        raise ContractError(f"{where}: its reply's alternatives are told apart by a constant outcome, one of them ok")
    if len(set(errors)) != len(errors) or set(errors) != set(outcomes) - {"ok"}:
        raise ContractError(f"{where}: reply errors {sorted(set(errors))} must be the reply schema's outcomes other "
                            f"than ok, each once: {sorted(set(outcomes) - {'ok'})}")


def check_covered_change(where: str, entry: dict[str, Any], registry: dict[str, Any],
                         schemas: dict[str, dict[str, Any]]) -> None:
    """A covered change names its record and version rule, and its body, reply and key agree with them.

    README, "Covered changes": ``seen_version`` required means the body requires
    the field; required_for_request_writers and optional mean the body offers it
    without requiring it (the receiver refuses an agent's change without it, which
    a schema cannot say); none means the body has no such field. Whenever the
    writer may send a version, the reply's ``changed_since_read`` carries the
    current one. A kind offering a refusal that only covered changes give must be
    covered, and a covered change's key never uses a part a retry's new frame changes.
    """

    covered = entry.get("covered_change")
    errors = set(entry.get("reply", {}).get("errors", []))
    if covered is None:
        if errors & COVERED_ONLY_REFUSALS:
            raise ContractError(f"{where}: its reply offers {sorted(errors & COVERED_ONLY_REFUSALS)}, which only covered "
                                "changes give: state its covered_change")
        return
    if entry["class"] != "request_reply":
        raise ContractError(f"{where}: only request/reply operations state covered_change")
    if not isinstance(covered, dict) or set(covered) != {"record_kind", "seen_version"} \
            or covered["record_kind"] not in registry["covered_record_kinds"] \
            or covered["seen_version"] not in SEEN_VERSION_FIELD:
        raise ContractError(f"{where}: covered_change is {{record_kind, seen_version}} with record_kind one of "
                            f"covered_record_kinds and seen_version one of {sorted(SEEN_VERSION_FIELD)}")
    rule = covered["seen_version"]
    field = seen_version_field(entry, schemas)
    if field != SEEN_VERSION_FIELD[rule]:
        raise ContractError(f"{where}: seen_version {rule} needs a body whose seen_version is "
                            f"{SEEN_VERSION_FIELD[rule]}, not {field}")
    if rule != "none" and not stale_refusal_carries_version(entry, schemas):
        raise ContractError(f"{where}: a writer may send seen_version, so its reply offers changed_since_read "
                            "with a required current_version")
    if rule == "required_for_request_writers" and "invalid_request" not in errors:
        raise ContractError(f"{where}: an agent's change without seen_version is refused as invalid_request, "
                            "so its reply offers invalid_request")
    per_frame = sorted(set(entry["idempotency"]) & PER_FRAME_PARTS)
    if per_frame:
        raise ContractError(f"{where}: idempotency part {per_frame[0]} changes with every frame, so a retry of the "
                            "change would never find its stored outcome")


def seen_version_field(entry: dict[str, Any], schemas: dict[str, dict[str, Any]]) -> str:
    """Whether every alternative of the body requires ``seen_version``, offers it, or lacks it; else ``mixed``."""

    states = {"required" if "seen_version" in variant.get("required", [])
              else "optional" if "seen_version" in variant.get("properties", {}) else "absent"
              for _file, variant in schema_leaves(ModelWriter(schemas), entry["body"], schemas[entry["body"]], [])}
    return states.pop() if len(states) == 1 else "mixed"


def stale_refusal_carries_version(entry: dict[str, Any], schemas: dict[str, dict[str, Any]]) -> bool:
    """Whether the reply has a ``changed_since_read`` alternative that requires ``current_version``."""

    return any(variant.get("properties", {}).get("outcome", {}).get("const") == "changed_since_read"
               and "current_version" in variant.get("required", [])
               for _file, variant in reply_alternatives(entry, schemas))


def reply_alternatives(entry: dict[str, Any], schemas: dict[str, dict[str, Any]]) -> list[tuple[str, dict[str, Any]]]:
    """The alternatives of a kind's reply body, references followed."""

    return schema_leaves(ModelWriter(schemas), entry["reply"]["body"], schemas[entry["reply"]["body"]], [])


class BodyFields:
    """Looks up a kind's body fields by JSON pointer, for checking its body rules.

    ``frame_schema`` also lets a rule name a field of the frame around the body
    (``/issued_at``), where a rule allows one.
    """

    def __init__(self, entry: dict[str, Any], schemas: dict[str, dict[str, Any]], frame_schema: str = "") -> None:
        self.resolver = ModelWriter(schemas)
        self.file, self.root = entry["body"], schemas[entry["body"]]
        self.frame_file, self.frame_root = frame_schema, schemas.get(frame_schema, {})

    def leaves(self, pointer: Any) -> list[tuple[str, dict[str, Any]]]:
        if not isinstance(pointer, str) or not pointer.startswith("/body/"):
            return []
        return schema_leaves(self.resolver, self.file, self.root, pointer.split("/")[2:])

    def frame_leaves(self, pointer: Any) -> list[tuple[str, dict[str, Any]]]:
        """A field of the frame outside the body, such as ``/issued_at``."""

        if not isinstance(pointer, str) or not re.fullmatch(r"(/[a-z_]+)+", pointer) or pointer.startswith("/body/"):
            return []
        return schema_leaves(self.resolver, self.frame_file, self.frame_root, pointer[1:].split("/"))

    def timestamp(self, pointer: Any, in_frame: bool = False) -> bool:
        """Whether every schema the pointer reaches is the shared timestamp definition."""

        leaves = self.frame_leaves(pointer) if in_frame else self.leaves(pointer)
        return bool(leaves) and all(node is self.resolver.node("common.schema.json", "/$defs/timestamp")
                                    for _file, node in leaves)

    def defined(self, pointer: Any, kind: str | None = None) -> bool:
        leaves = self.leaves(pointer)
        return bool(leaves) and (kind is None or all(resolved_type(self.resolver, f, leaf) == kind for f, leaf in leaves))

    def items_have(self, pointer: str, member: str) -> bool:
        """``by`` names a member every object item carries; plain values (strings, integers) compare whole."""

        items = [item for file, node in self.leaves(pointer) for item in schema_leaves(self.resolver, file, node["items"], [])]
        return all(member in node.get("required", []) if node.get("type") == "object"
                   else node.get("type") in {"string", "integer"} or "enum" in node or "const" in node
                   for _file, node in items)

    def fits(self, pointer: str, value: Any) -> bool:
        return any(value_fits(self.resolver, file, node, value) for file, node in self.leaves(pointer))

    def holds(self, pointer: str, value: Any) -> bool:
        """Whether the field, or each item of it when it is a list, could hold ``value``."""

        leaves = self.leaves(pointer)
        return bool(leaves) and any(
            any(value_fits(self.resolver, item_file, item, value)
                for item_file, item in schema_leaves(self.resolver, file, node["items"], []))
            if resolved_type(self.resolver, file, node) == "array" else value_fits(self.resolver, file, node, value)
            for file, node in leaves)


def unique_rule_valid(rule: dict[str, Any], fields: BodyFields) -> bool:
    return set(rule) <= {"unique", "by"} and fields.defined(rule.get("unique"), "array") \
        and ("by" not in rule or fields.items_have(rule["unique"], rule["by"]))


def conditions_valid(conditions: Any, fields: BodyFields) -> bool:
    """A rule's ``when``: at least one {at, is}, each naming a body field that can hold the value."""

    return isinstance(conditions, list) and bool(conditions) and all(
        isinstance(c, dict) and set(c) == {"at", "is"} and fields.defined(c["at"]) and fields.fits(c["at"], c["is"])
        for c in conditions)


def requires_rule_valid(rule: dict[str, Any], fields: BodyFields) -> bool:
    return set(rule) == {"requires", "when"} and fields.defined(rule["requires"]) and conditions_valid(rule["when"], fields)


def allowed_rule_valid(rule: dict[str, Any], fields: BodyFields) -> bool:
    """``{allowed, values, when}``: distinct values the field (or each item of a list) can hold, under conditions."""

    values = rule.get("values")
    return set(rule) == {"allowed", "values", "when"} and isinstance(values, list) and bool(values) \
        and len({json.dumps(value) for value in values}) == len(values) \
        and all(fields.holds(rule["allowed"], value) for value in values) and conditions_valid(rule["when"], fields)


def forbids_rule_valid(rule: dict[str, Any], fields: BodyFields) -> bool:
    """``{forbids, when}``: a field the body defines that must be absent whenever every condition holds."""

    return set(rule) == {"forbids", "when"} and fields.defined(rule["forbids"]) and conditions_valid(rule["when"], fields)


def any_of_rule_valid(rule: dict[str, Any], fields: BodyFields) -> bool:
    return set(rule) == {"any_of"} and len(rule["any_of"]) > 1 and all(fields.defined(p) for p in rule["any_of"])


def ends_within_rule_valid(rule: dict[str, Any], fields: BodyFields) -> bool:
    """``{ends_within, of, seconds}``: a body timestamp at most ``seconds`` after another body or frame timestamp."""

    seconds = rule.get("seconds")
    return set(rule) == {"ends_within", "of", "seconds"} and type(seconds) is int and seconds > 0 \
        and fields.timestamp(rule["ends_within"]) and rule["of"] != rule["ends_within"] \
        and (fields.timestamp(rule["of"]) or fields.timestamp(rule["of"], in_frame=True))


def check_body_rules(where: str, entry: dict[str, Any], schemas: dict[str, dict[str, Any]], frame_schema: str) -> None:
    """Each rule is one of the six README shapes and names fields its body defines, with values they can hold."""

    fields = BodyFields(entry, schemas, frame_schema)
    for rule in entry.get("body_rules", []):
        if not (unique_rule_valid(rule, fields) or requires_rule_valid(rule, fields) or forbids_rule_valid(rule, fields)
                or any_of_rule_valid(rule, fields) or allowed_rule_valid(rule, fields)
                or ends_within_rule_valid(rule, fields)):
            raise ContractError(f"{where}: body rule {rule} is not a unique, requires, forbids, any_of, allowed or "
                                "ends_within rule over its body's fields")


def value_fits(resolver: "ModelWriter", file: str, node: dict[str, Any], value: Any) -> bool:
    """Whether a field could hold ``value``: its const, one of its enum values, or a value of its type."""

    while "$ref" in node:
        file, fragment = resolver.resolve(file, node["$ref"])
        node = resolver.node(file, fragment)
    if "const" in node:
        return bool(node["const"] == value)
    if "enum" in node:
        return value in node["enum"]
    kind = node.get("type")
    return isinstance(value, bool) if kind == "boolean" else _of_type(value, kind) if kind in {"string", "integer"} else False


def resolved_type(resolver: "ModelWriter", file: str, node: dict[str, Any]) -> Any:
    while "$ref" in node:
        file, fragment = resolver.resolve(file, node["$ref"])
        node = resolver.node(file, fragment)
    return node.get("type")


def always_supplied(entry: dict[str, Any], schemas: dict[str, dict[str, Any]], frame_schema: str, steps: list[str]) -> bool:
    """Whether every frame of the kind carries the field: required at each step, or the audience id its address names."""

    if steps == ["audience", "id"]:
        return any("{" in address for address in own_addresses(entry))
    resolver = ModelWriter(schemas)
    file, node = (entry["body"], schemas[entry["body"]]) if steps[0] == "body" else (frame_schema, schemas[frame_schema])
    for step in steps[1:] if steps[0] == "body" else steps:
        while "$ref" in node:
            file, fragment = resolver.resolve(file, node["$ref"])
            node = resolver.node(file, fragment)
        variants = node.get("oneOf", [node])
        if not all(step in variant.get("required", []) for variant in variants):
            return False
        node = variants[0]["properties"][step]
    return True


def schema_leaves(resolver: "ModelWriter", file: str, node: dict[str, Any], steps: list[str],
                  keep_one_of: bool = False) -> list[tuple[str, dict[str, Any]]]:
    """The schemas a path of property names reaches, through references and every oneOf alternative.

    With ``keep_one_of``, a oneOf the path ends at is returned whole, not split into its alternatives.
    """

    while "$ref" in node:
        file, fragment = resolver.resolve(file, node["$ref"])
        node = resolver.node(file, fragment)
    if not steps and (keep_one_of or "oneOf" not in node):
        return [(file, node)]
    if "oneOf" in node:
        return [leaf for variant in node["oneOf"] for leaf in schema_leaves(resolver, file, variant, steps, keep_one_of)]
    child = node.get("properties", {}).get(steps[0])
    return [] if child is None else schema_leaves(resolver, file, child, steps[1:], keep_one_of)


def scalar_schema(resolver: "ModelWriter", file: str, node: dict[str, Any]) -> bool:
    while "$ref" in node:
        file, fragment = resolver.resolve(file, node["$ref"])
        node = resolver.node(file, fragment)
    if "const" in node or "enum" in node:
        values = [node["const"]] if "const" in node else node["enum"]
        return all(isinstance(value, (str, int)) and not isinstance(value, bool) for value in values)
    return node.get("type") in {"string", "integer"}


def check_wakes(where: str, wakes: Any) -> None:
    """``wakes_model`` is one value, or one value per alternative of a body property."""

    if isinstance(wakes, str):
        if wakes not in WAKES:
            raise ContractError(f"{where}: wakes_model {wakes!r} is not one of {sorted(WAKES)}")
        return
    if not isinstance(wakes, dict) or set(wakes) != {"by", "values"} or not wakes["values"]:
        raise ContractError(f"{where}: wakes_model is a value or {{by, values}}")
    for variant, value in wakes["values"].items():
        if value not in WAKES:
            raise ContractError(f"{where}: wakes_model for {variant!r} is not one of {sorted(WAKES)}")


def check_state_records(registry: dict[str, Any], transport: dict[str, Any], schemas: dict[str, dict[str, Any]]) -> None:
    """Each signed record names its bucket, key form, signer, writer roles and body schema."""

    roles = set(registry["host_roles"])
    partition = schemas["common.schema.json"]["$defs"].get("partition", {})
    if partition.get("type") != "integer" or partition.get("minimum") != 0 \
            or partition.get("maximum") != transport["partitions"]["max_count"] - 1:
        raise ContractError("common.schema.json $defs.partition is a whole number from 0 to transport.yaml "
                            "partitions.max_count minus 1")
    names = set(schemas[registry["state_schema"]]["properties"]["record"]["enum"])
    if set(registry["state_records"]) != names:
        raise ContractError(f"registry.yaml state_records and {registry['state_schema']} record names differ")
    for name, entry in registry["state_records"].items():
        where = f"registry.yaml state_records.{name}"
        if set(entry) - STATE_FIELDS or REQUIRED_STATE_FIELDS - set(entry):
            raise ContractError(f"{where}: fields are {sorted(REQUIRED_STATE_FIELDS)}, optionally {sorted(STATE_FIELDS - REQUIRED_STATE_FIELDS)}")
        if entry["bucket"] not in transport["key_value"]:
            raise ContractError(f"{where}: bucket {entry['bucket']} is not in transport.yaml")
        if entry["signer"] not in SIGNERS:
            raise ContractError(f"{where}: signer must be one of {sorted(SIGNERS)}")
        if ("signer_roles" in entry) != (entry["signer"] == "host") or not set(entry.get("signer_roles", [])) <= roles:
            raise ContractError(f"{where}: host-signed records, and only they, name signer_roles from host_roles")
        if not set(entry["writers"]) <= roles or entry["body"] not in schemas:
            raise ContractError(f"{where}: writers must be host roles and the body schema must exist")
        bucket_ttl = transport["key_value"][entry["bucket"]]["per_key_ttl_seconds"]
        if entry.get("ttl_seconds", 0) != bucket_ttl:
            raise ContractError(f"{where}: ttl_seconds is its bucket's per_key_ttl_seconds ({bucket_ttl}), and only then present")
        check_record_ages(where, entry, bucket_ttl)


def check_record_ages(where: str, entry: dict[str, Any], bucket_ttl: int) -> None:
    """A longest lifetime only for records whose bucket keeps them, and a fresh age shorter than that lifetime."""

    lifetime = entry.get("max_lifetime_seconds", 1)
    if type(lifetime) is not int or lifetime < 1 or ("max_lifetime_seconds" in entry and bucket_ttl):
        raise ContractError(f"{where}: max_lifetime_seconds is a positive whole number, for records whose bucket keeps them")
    fresh = entry.get("fresh_seconds")
    if fresh is not None and (type(fresh) is not int or fresh < 1 or "max_lifetime_seconds" not in entry
                              or fresh >= entry["max_lifetime_seconds"]):
        raise ContractError(f"{where}: fresh_seconds is a positive whole number, shorter than the record's "
                            "max_lifetime_seconds")


def check_shared_lists(registry: dict[str, Any], transport: dict[str, Any], topologies: dict[str, Any],
                       schemas: dict[str, dict[str, Any]]) -> None:
    """Lists the schemas repeat must match the registry and topologies they come from."""

    for item in registry["verification_refusals"]:
        if not set(item) <= {"code", "meaning", "temporary"} or item.get("temporary", True) is not True:
            raise ContractError(f"registry.yaml: refusal {item.get('code')} has a code, a meaning and optionally temporary: true")
    # A temporary refusal waits for what the receiver lacks, so no message is set aside for it.
    refusals = {item["code"] for item in registry["verification_refusals"] if not item.get("temporary")}
    temporary = {item["code"] for item in registry["verification_refusals"] if item.get("temporary")}
    for entry in registry["kinds"].values():
        if entry["class"] == "dead_letter":
            reasons = set(schemas[entry["body"]]["properties"]["reason"]["enum"])
            if not refusals <= reasons:
                raise ContractError(f"{entry['body']}: a notice's reason names every verification refusal; missing "
                                    f"{sorted(refusals - reasons)}")
            if reasons & temporary:
                raise ContractError(f"{entry['body']}: a notice's reason names no temporary refusal; remove "
                                    f"{sorted(reasons & temporary)}")

    common = schemas["common.schema.json"]["$defs"]
    if set(common["host_role"]["enum"]) != set(registry["host_roles"]):
        raise ContractError("common.schema.json host_role must list registry.yaml host_roles")
    targets = transport["audience_targets"]
    if set(targets) != set(common["audience"]["properties"]["target"]["enum"]):
        raise ContractError("transport.yaml audience_targets must name every audience target")
    if not all(set(roles) <= set(registry["host_roles"]) for roles in targets.values()):
        raise ContractError("transport.yaml audience_targets name host roles only")
    if set(common["agent_role"]["enum"]) != set(topologies["agent_roles"]):
        raise ContractError("common.schema.json agent_role must list team-topologies.yaml agent_roles")
    if not common["scope_id"]["pattern"].startswith(f"^({re.escape(INSTALLATION_SCOPE)}|"):
        raise ContractError("common.schema.json scope_id must allow transport.yaml installation_scope")
    for spec in transport["classes"].values():
        if not all(address.startswith(INSTALLATION_ADDRESS) for address in spec.get("installation_addresses", [])):
            raise ContractError(f"transport.yaml: installation addresses start with {INSTALLATION_ADDRESS}")


def check_name_lists(registry: dict[str, Any]) -> None:
    """Registry lists of names that kinds and handlers refer to: each a list of distinct names."""

    for key in ("covered_record_kinds", "retry_outcomes"):
        names = registry.get(key)
        if not isinstance(names, list) or not names or len(set(names)) != len(names) \
                or not all(isinstance(name, str) and re.fullmatch(r"[a-z][a-z0-9_]*", name) for name in names):
            raise ContractError(f"registry.yaml: {key} lists distinct snake_case names")


def audience_target(address: str) -> str:
    """The audience target an address delivers to (README, "Delivery")."""

    for placeholder in RECEIVER_PLACEHOLDERS:
        if "{" + placeholder + "}" in address:
            return placeholder
    if address.startswith("evt."):
        return "team"
    if address in {"work.team", "svc.team", "svc.scheduler"}:
        return "team_services"
    if address.startswith("dlq."):
        return "team_services"
    if address.startswith(INSTALLATION_ADDRESS):
        return "installation_service"
    raise ContractError(f"address {address!r} names no audience target")


def check_consumer_names(transport: dict[str, Any]) -> None:
    """Different ids must never produce one consumer name.

    NATS silently updates an existing consumer's filter when a consumer with the
    same name is created, so one team could take another's work. Each name starts
    with a fixed word that ends in a character ids cannot contain, separates its
    ids the same way, and holds every id of its filter that its stream's name does not
    (a name is unique within its stream only), and no other.
    """

    prefixes = []
    for key, consumer in transport["consumers"].items():
        where = f"transport.yaml consumers.{key}"
        parts = re.split(r"(\{[a-z_]+\})", consumer["name"])
        literals, placeholders = parts[::2], parts[1::2]
        filter_ids = set(re.findall(r"\{[a-z_]+\}", consumer["filter"]))
        stream_ids = set(re.findall(r"\{[a-z_]+\}", consumer["stream"]))
        if not filter_ids - stream_ids <= set(placeholders) <= filter_ids or len(set(placeholders)) != len(placeholders):
            raise ContractError(f"{where}: the name holds each of its filter's ids that its stream's name does not, "
                                "once, and no other")
        if not literals[0] or ID_CHARACTER.fullmatch(literals[0][-1]):
            raise ContractError(f"{where}: start the name with a fixed word ending in a character ids cannot contain")
        if any(all(ID_CHARACTER.fullmatch(character) for character in literal) for literal in literals[1:-1]):
            raise ContractError(f"{where}: separate the ids with a character they cannot contain")
        if re.search(r"[\s.*>/\\]", consumer["name"]):
            raise ContractError(f"{where}: NATS consumer names cannot hold whitespace, ., *, >, / or \\")
        prefixes.append(literals[0])
    for prefix in prefixes:
        if any(other != prefix and other.startswith(prefix) for other in prefixes):
            raise ContractError(f"transport.yaml: consumer name prefix {prefix!r} begins another prefix")


def check_transport(transport: dict[str, Any]) -> None:
    """Expiry, stream age and replay retention must agree, or valid messages are lost or replayed.

    A stream that drops a message before its class's longest expiry loses work a
    receiver would still accept; a replay record forgotten earlier lets a copy of
    an old message in again.
    """

    if (transport.get("installation_scope"), transport.get("subject_prefix", "").split("{team}", 1)[0]) != (
            INSTALLATION_SCOPE, SUBJECT_ROOT):
        # The generator reads both once, when it loads; a transport passed in later must agree.
        raise ContractError("transport.yaml: installation_scope and the subject root are the ones the generator loaded")
    skew = transport["clock_skew_seconds"]
    if not isinstance(skew, int) or not 0 <= skew <= 300:
        raise ContractError("transport.yaml: clock_skew_seconds is a whole number of seconds from 0 to 300")
    longest = max(spec["max_expiry_seconds"] for spec in transport["classes"].values())
    if transport["replay_ledger_retention_seconds"] < longest + skew:
        raise ContractError("transport.yaml: replay records must outlive the longest class expiry plus the clock tolerance")
    for name, spec in transport["classes"].items():
        if not isinstance(spec.get("between_installations"), bool):
            raise ContractError(f"transport.yaml: class {name} states between_installations")
        check_class_streams(name, spec, transport, skew)
    check_partitions(transport)
    check_stream_subjects(transport)
    check_agent_queues(transport)
    check_stream_sizes(transport)


# The installation setting that holds the partition count (transport.yaml partitions), what
# the account mapping's last token computes, and the step max_count is a multiple of.
PARTITION_SETTING = "transport.queues.partitions"
PARTITION_PLACEHOLDER = "partition"
# The account mappings name the count by this token; a server's configuration has the installation's count there.
PARTITION_COUNT_TOKEN = "COUNT"
PARTITION_FUNCTION = "{{partition(" + PARTITION_COUNT_TOKEN + ",1,3)}}"
PARTITION_COUNT_STEP = 32
# A stream name that holds a partition joins it to its team with ~, which ids cannot hold.
PARTITION_STREAM_PART = "{team}~{partition}"


def partitioned_addresses(transport: dict[str, Any]) -> list[str]:
    """The addresses the account maps to the agent's partition: every address delivered to an agent (README, "Partitions")."""

    return [address for spec in transport["classes"].values() for address in spec["addresses"]
            if audience_target(address) == "agent"]


def partition_mapping(address: str) -> tuple[str, str]:
    """The account mapping for one agent address: any team, sender and agent, to the same subject plus the partition.

    nats-server's mapping functions must fill a whole token, and partition(COUNT,1,3) hashes the
    first and third wildcards, the team and the agent, in that order.
    """

    source = SUBJECT_ROOT + "*.*." + address.replace("{agent}", "*")
    target = (SUBJECT_ROOT + "{{wildcard(1)}}.{{wildcard(2)}}." + address.replace("{agent}", "{{wildcard(3)}}")
              + "." + PARTITION_FUNCTION)
    return source, target


def stored_subject(address: str, owner: str, transport: dict[str, Any]) -> str:
    """The subject NATS stores and delivers an address under: the signed subject, plus {partition} where the account maps it."""

    subject = full_subject(address, owner)
    return subject + ".{" + PARTITION_PLACEHOLDER + "}" if address in partitioned_addresses(transport) else subject


def publish_subject(address: str, transport: dict[str, Any]) -> str:
    """A sender's publish grant for an address under its own id; NATS checks it against the mapped subject (any partition)."""

    subject = full_subject(send_pattern(address), "{self}")
    return subject + ".*" if address in partitioned_addresses(transport) else subject


def subscribe_subject(address: str, transport: dict[str, Any]) -> str:
    """A receiver's subscription for a request or event address: per partition it owns, any agent there; otherwise per id."""

    subject = stored_subject(address, "*", transport)
    return subject.replace("{agent}", "*") if address in partitioned_addresses(transport) else subject


def probe_grant(transport: dict[str, Any]) -> str:
    """The topology owner's publish and subscribe grant for the mapping check (README, "Partitions").

    Every mapped address of its own probe agents under the probe scope (the inbox, the control
    queue and live control), mapped (with the partition token) or not, so the check judges each
    of the account's mapping lines and also sees a server that does not map. No stream stores
    a subject under the probe scope, since every stream's subjects name a team id, so these are
    plain messages.
    """

    return f"{SUBJECT_ROOT}{transport['partitions']['probe_scope']}.{{self}}.>"


def check_partitions(transport: dict[str, Any]) -> None:
    """The partition count's setting and limits, and the account mappings that put each agent's addresses in its partition.

    The mappings must be exactly the ones the contract derives from the agent addresses: a
    mapping that missed one would leave its subjects unmapped, stored in no partition stream,
    and one that hashed other tokens would put an agent's queues where its host does not read.
    The probe scope cannot be a team id or the installation scope, so a probe never reaches
    a team's receivers or streams.
    """

    where = "transport.yaml partitions"
    spec = transport.get("partitions")
    if not isinstance(spec, dict) \
            or set(spec) != {"count_setting", "default_count", "max_count", "handover_seconds", "account_mappings",
                             "probe_scope"} \
            or spec["count_setting"] != PARTITION_SETTING:
        raise ContractError(f"{where}: names the setting {PARTITION_SETTING}, the default and largest counts, the "
                            "handover deadline, the account mappings and the probe scope")
    probe = spec["probe_scope"]
    if not isinstance(probe, str) or not re.fullmatch(r"_[a-z][a-z0-9_]*", probe) or probe == INSTALLATION_SCOPE:
        raise ContractError(f"{where}: probe_scope is a token starting with _ (no team id can), other than "
                            "installation_scope")
    handover = spec["handover_seconds"]
    if type(handover) is not int or not 2 * transport["clock_skew_seconds"] < handover <= 3600:
        # Bounded so a host never holds back a renewal its credential or certificate needs for long.
        raise ContractError(f"{where}: handover_seconds is a whole number of seconds above twice the clock tolerance "
                            "and at most 3600")
    largest, default = spec["max_count"], spec["default_count"]
    if type(largest) is not int or largest < PARTITION_COUNT_STEP or largest % PARTITION_COUNT_STEP:
        raise ContractError(f"{where}: max_count is a positive multiple of {PARTITION_COUNT_STEP}")
    if type(default) is not int or not 1 <= default <= largest:
        raise ContractError(f"{where}: default_count is a whole number from 1 to max_count")
    expected = dict(partition_mapping(address) for address in partitioned_addresses(transport))
    if spec["account_mappings"] != expected:
        raise ContractError(f"{where}: account_mappings map exactly the agent addresses to their partition: {expected}")


# The instance settings that size the streams (transport.yaml queues).
QUEUE_SETTINGS = {"max_bytes_setting": "transport.queues.max_bytes",
                  "warn_at_percent_setting": "transport.queues.warn_at_percent",
                  "max_messages_per_subject_setting": "transport.queues.max_messages_per_subject"}


def check_stream_sizes(transport: dict[str, Any]) -> None:
    """Every queued stream has a byte limit from the instance's settings and refuses new messages when full.

    A stream that dropped its oldest messages when full (discard: old) would lose queued work
    or unread set-aside copies and notices without anyone being told (README, "Stream sizes").
    """

    queues = transport.get("queues")
    if not isinstance(queues, dict) or set(queues) != {*QUEUE_SETTINGS, "size_kinds", "warn_at_percent_default"} \
            or any(queues[key] != name for key, name in QUEUE_SETTINGS.items()):
        raise ContractError("transport.yaml: queues names the settings transport.queues.max_bytes, "
                            "transport.queues.warn_at_percent and transport.queues.max_messages_per_subject, the size "
                            "kinds and the warning's default")
    kinds = queues["size_kinds"]
    if not isinstance(kinds, list) or not kinds or len(set(kinds)) != len(kinds) \
            or not all(isinstance(kind, str) and re.fullmatch(r"[a-z][a-z0-9_]*", kind) for kind in kinds):
        raise ContractError("transport.yaml: queues.size_kinds lists distinct snake_case names")
    warn = queues["warn_at_percent_default"]
    if type(warn) is not int or not 1 <= warn <= 99:
        raise ContractError("transport.yaml: queues.warn_at_percent_default is a whole percentage from 1 to 99")
    queued = {stream for spec in transport["classes"].values() for stream in spec.get("streams", [])}
    used = set()
    for name, stream in transport["streams"].items():
        if name not in queued:
            continue
        if stream.get("size_kind") not in kinds:
            raise ContractError(f"transport.yaml: stream {name} names its byte limit's size kind (size_kind, one of "
                                "queues.size_kinds)")
        if stream.get("discard") != "new":
            raise ContractError(f"transport.yaml: stream {name} refuses new messages when full (discard: new), so "
                                "nothing set aside is dropped to make room")
        used.add(stream["size_kind"])
    for bucket, spec in transport.get("object_store", {}).items():
        if spec.get("size_kind") not in kinds:
            raise ContractError(f"transport.yaml: object_store.{bucket} names its byte limit's size kind (size_kind, one of "
                                "queues.size_kinds)")
        used.add(spec["size_kind"])
    if used != set(kinds):
        raise ContractError(f"transport.yaml: queues.size_kinds names only kinds a stream uses; unused "
                            f"{sorted(set(kinds) - used)}")
    check_per_subject_caps(transport)


def check_per_subject_caps(transport: dict[str, Any]) -> None:
    """The streams of agents' queues cap each sender's waiting messages per agent from the settings, refusing past it.

    Review point FA-3 of PR #151: one agent's backlog could fill its partition stream, and
    then its partition's control messages were refused too. The stored subject names the
    sender and the agent, so a per-subject cap bounds one sender's backlog for one agent's
    queue; with discard_new_per_subject a sender at the cap is refused (10077) instead of the
    oldest message being dropped, and the stream still takes other subjects (NATS also needs
    discard: new, which check_stream_sizes requires of every queued stream). A stream with a
    fixed cap keeps one message per subject, the latest (README, "Stream sizes"), so it must
    not set discard_new_per_subject: the dead letters' retried set-aside would be refused
    (10077) instead of replacing its copy.
    """

    setting = transport["queues"]["max_messages_per_subject_setting"]
    stored = [stored_subject(address, "*", transport) for address in transport["agent_queues"].values()]
    for name, stream in transport["streams"].items():
        named = stream.get("max_msgs_per_subject_setting")
        holds_agent_queues = any(subject_covers(pattern, subject) for pattern in stream["subjects"] for subject in stored)
        if holds_agent_queues and named != setting:
            raise ContractError(f"transport.yaml: stream {name} stores agents' queues, so it caps each sender's waiting "
                                f"messages per agent (max_msgs_per_subject_setting: {setting})")
        if named is not None and (named != setting or "max_msgs_per_subject" in stream):
            raise ContractError(f"transport.yaml: stream {name} takes its per-subject cap from {setting} alone")
        per_subject = stream.get("discard_new_per_subject", False)
        if not isinstance(per_subject, bool) or (named is not None and not per_subject):
            raise ContractError(f"transport.yaml: stream {name} refuses a sender's next message at a cap from the "
                                "settings (discard_new_per_subject: true), so queued work is never dropped")
        # NATS needs a cap for discard_new_per_subject, and with a fixed cap it would refuse the
        # dead letters' retried set-aside instead of replacing its copy, so only the cap from the
        # settings takes it.
        if per_subject and named is None:
            raise ContractError(f"transport.yaml: stream {name} sets discard_new_per_subject only with its cap from "
                                f"{setting}; a fixed cap without it keeps the latest message per subject")


def check_agent_queues(transport: dict[str, Any]) -> None:
    """An agent's queued messages go to one of its two queues, each with its own consumer (README, "An agent's two queues")."""

    queues = transport.get("agent_queues")
    durable = [address for address in transport["classes"]["durable"]["addresses"] if audience_target(address) == "agent"]
    if not isinstance(queues, dict) or set(queues) != {"inbox", "control"} or sorted(queues.values()) != sorted(durable) \
            or len(durable) != 2:
        raise ContractError("transport.yaml: agent_queues names the inbox and the control queue, the durable class's "
                            "two agent addresses")
    filters = [consumer["filter"] for consumer in transport["consumers"].values()]
    for queue, address in queues.items():
        if filters.count(stored_subject(address, "*", transport)) != 1:
            name = "control queue" if queue == "control" else queue
            raise ContractError(f"transport.yaml: the agent's {name} ({address}) has exactly one consumer, so the host "
                                "reads it on its own")


def largest_header_block(schemas: dict[str, dict[str, Any]]) -> int:
    """Bytes of the largest NATS header block a sender may set (HEADER_VALUES)."""

    common = schemas["common.schema.json"]["$defs"]
    sizes = {name: value if isinstance(value, int) else int(common[value]["maxLength"]) for name, value in HEADER_VALUES.items()}
    return len(NATS_HEADER_VERSION) + sum(len(f"{name}: {NATS_LINE_END}") + size for name, size in sizes.items()) \
        + len(NATS_LINE_END)


def check_message_sizes(transport: dict[str, Any], schemas: dict[str, dict[str, Any]]) -> None:
    """Every stream stores a full-size frame with its headers, which JetStream counts in a message's size."""

    limits = transport["limits"]
    largest = largest_header_block(schemas)
    if not isinstance(limits.get("max_header_bytes"), int) or limits["max_header_bytes"] < largest:
        raise ContractError(f"transport.yaml: max_header_bytes holds the largest header block senders may set, {largest} bytes")
    message = limits["max_frame_bytes"] + limits["max_header_bytes"]
    if message > limits["nats_max_payload_bytes"]:
        raise ContractError("transport.yaml: a frame and its headers fit nats_max_payload_bytes")
    for name, stream in transport["streams"].items():
        if stream.get("max_message_bytes") != message:
            raise ContractError(f"transport.yaml: stream {name}'s max_message_bytes is max_frame_bytes plus max_header_bytes")
    credential = schemas["common.schema.json"]["$defs"]["connection_credential"]["properties"]["jwt"]["maxLength"]
    if limits.get("max_credential_bytes") != credential:
        raise ContractError("transport.yaml: max_credential_bytes is the connection credential's jwt maxLength in "
                            "common.schema.json")


# Host roles that, beside the topology owner, serve the whole installation (owner decision of
# 2026-11-17): the gateway routes every team's people and edges.
INSTALLATION_WIDE_ROLES = ("gateway",)


def check_single_team_roles(registry: dict[str, Any], transport: dict[str, Any]) -> None:
    """Every host role but the gateway and the topology owner serves one team per runtime (single_team_roles).

    Owner decision of 2026-11-17: each runtime serves exactly one team, and only the gateway and
    the installation authority (the topology owner) stay installation-wide. Partitions, pools and
    edges belong to a team, so a one-team credential is what the bound in README "Credential
    size" counts; and a scheduler signs authority tokens, system work among them, only for its
    own team. No role whose grants name {partition} may serve the whole installation.
    """

    roles = transport.get("single_team_roles")
    if not isinstance(roles, list) or len(set(roles)) != len(roles) or not set(roles) <= set(registry["host_roles"]):
        raise ContractError("transport.yaml: single_team_roles lists distinct host roles")
    wide = {transport["topology_owner"], *INSTALLATION_WIDE_ROLES}
    expected = set(registry["host_roles"]) - wide
    if set(roles) != expected:
        raise ContractError(f"transport.yaml: single_team_roles lists every host role but the gateway and the topology "
                            f"owner, which serve the whole installation; missing {sorted(expected - set(roles))}, "
                            f"installation-wide {sorted(set(roles) & wide)}")
    partitioned = sorted(role for role, document in permissions(registry, transport).items()
                         if role in wide and any("{partition}" in subject
                                                 for subject in document["publish"] + document["subscribe"]))
    if partitioned:
        raise ContractError(f"transport.yaml: an installation-wide role's grants name partitions: {partitioned}")


def check_withdrawal_spares_own_inbox(transport: dict[str, Any]) -> None:
    """Withdrawal searches no stream that holds its own role's inbox.

    Withdrawal deletes every task.request whose body names the task, and a task's copy to the
    team services carries the same body, so a search of the stream that holds their inbox
    (work.team, in UA1_TEAM_WORK_{team}) would delete the copies too (README, "Withdrawing
    queued work").
    """

    role = transport["withdrawal"]["role"]
    patterns = [(stream, subject) for stream in transport["withdrawal"]["streams"]
                for subject in transport["streams"][stream]["subjects"]]
    for address in transport["classes"]["durable"]["addresses"]:
        if role not in transport["audience_targets"].get(audience_target(address), []):
            continue
        subject = stored_subject(address, "*", transport)
        for stream, pattern in patterns:
            if subjects_overlap(pattern, subject):
                raise ContractError(f"transport.yaml: withdrawal searches {stream}, which holds the {role} role's own inbox "
                                    f"({address}), where the copies of the tasks it withdraws wait")


def check_copy_inbox_alone(registry: dict[str, Any], transport: dict[str, Any]) -> None:
    """A stream that stores a copy address (registry copy_to) stores no other durable address.

    A copied kind is stored at its copy address before its frame is published, so every
    task, result, progress report, account, claim and handback of a team waits on room in
    that stream. Another reader's backlog sharing its byte limit (an edge offline for a day)
    would refuse them all; on its own, the stream fills only when its own reader stops
    (README, "Stream sizes").
    """

    copies = {address for entry in registry["kinds"].values() for address in entry.get("copy_to", [])}
    durable = transport["classes"]["durable"]["addresses"]
    for name, stream in transport["streams"].items():
        stored = {address for address in durable
                  if any(subjects_overlap(pattern, stored_subject(address, "*", transport)) for pattern in stream["subjects"])}
        if stored & copies and stored - copies:
            raise ContractError(f"transport.yaml: stream {name} stores the copy address {sorted(stored & copies)} beside "
                                f"{sorted(stored - copies)}; a backlog there would refuse every copied kind")


def check_stream_management(registry: dict[str, Any], transport: dict[str, Any]) -> None:
    """The topology owner and the withdrawal role are host roles; withdrawal deletes only from durable work streams."""

    if transport.get("topology_owner") not in registry["host_roles"]:
        raise ContractError("transport.yaml: topology_owner and withdrawal.role name host roles")
    for key, class_name in (("withdrawal", "durable"), ("dead_letter_custody", "dead_letter")):
        custody = transport.get(key)
        if not isinstance(custody, dict) or set(custody) != {"role", "streams"} or custody["role"] not in registry["host_roles"]:
            raise ContractError(f"transport.yaml: topology_owner and {key}.role name host roles")
        streams = set(transport["classes"][class_name]["streams"])
        if not custody["streams"] or not set(custody["streams"]) <= streams:
            raise ContractError(f"transport.yaml: {key} deletes queued work only from the {class_name} class's streams")
        if not all("{team}" in stream for stream in custody["streams"]):
            raise ContractError(f"transport.yaml: {key} reads and deletes only in per-team streams ({{team}}), so it never "
                                "reaches another team's messages")
    check_withdrawal_spares_own_inbox(transport)
    check_copy_inbox_alone(registry, transport)
    for stream in transport["dead_letter_custody"]["streams"]:
        # A raw copy is found by its subject and deleted by the sequence that read returns; a second
        # copy under the same subject, from a retried publish, would survive that delete.
        if transport["streams"][stream].get("max_msgs_per_subject") != 1:
            raise ContractError(f"transport.yaml: stream {stream} keeps one message per subject (max_msgs_per_subject: 1)")
    check_pool_controller(registry, transport)
    check_size_check(registry, transport)
    check_key_value(registry, transport)


def check_pool_controller(registry: dict[str, Any], transport: dict[str, Any]) -> None:
    """The worker pool controller's host reads every pool consumer's information, and that stream holds nothing else (A3).

    The grant names any consumer of the stream, so the stream must hold only pool
    consumers: an agent's consumer there would show another host's agent to it.
    """

    where = "transport.yaml pool_controller"
    spec = transport.get("pool_controller")
    if not isinstance(spec, dict) or set(spec) != {"role", "stream"} or spec["role"] not in registry["host_roles"] \
            or spec["role"] not in transport["audience_targets"]["pool"]:
        raise ContractError(f"{where}: names a host role that serves pools and the stream of its team's pools")
    pool = stored_subject(next(address for address in transport["classes"]["durable"]["addresses"]
                               if audience_target(address) == "pool"), "*", transport)
    stream = transport["streams"].get(spec["stream"])
    readers = [consumer for consumer in transport["consumers"].values() if consumer["stream"] == spec["stream"]]
    if stream is None or "{team}" not in spec["stream"] or [consumer["filter"] for consumer in readers] != [pool] \
            or not all(subject_covers(subject, pool) for subject in stream["subjects"]):
        raise ContractError(f"{where}: stream is a per-team stream that holds only the pool queues and their consumers")


def check_size_check(registry: dict[str, Any], transport: dict[str, Any]) -> None:
    """The stream size check runs in one host role other than the topology owner, which holds its grants anyway.

    Its grants read the account's information and every stream's limits and size, never a
    message (README, "Stream sizes").
    """

    spec = transport.get("size_check")
    if not isinstance(spec, dict) or set(spec) != {"role"} or spec["role"] not in registry["host_roles"] \
            or spec["role"] == transport["topology_owner"]:
        raise ContractError("transport.yaml size_check: names the one host role, other than the topology owner, "
                            "that checks the streams' sizes")


# What a key template may name: the host itself, each team it serves, each partition it owns, its
# own installation and each installation an agreement names.
KEY_PLACEHOLDERS = {"self", "team", PARTITION_PLACEHOLDER, "installation", "peer"}
# What a bucket name may name: a bucket per team holds that team's records only.
BUCKET_PLACEHOLDERS = {"team"}
# The stream settings every bucket has (transport.yaml key_value_stream): only the consumer
# limit and the storage are a choice. Rollup headers would let a writer of one key purge every
# key; no role purges or deletes messages through the stream API; readers use direct gets.
KEY_VALUE_STREAM = {"history": 1, "allow_rollup_hdrs": False, "deny_delete": True, "deny_purge": True,
                    "allow_direct": True}


def check_key_value(registry: dict[str, Any], transport: dict[str, Any]) -> None:
    """Each bucket names its readers and, per writer role, the keys it may write (README, "Key-value grants").

    A record's writers in the registry are among its bucket's writers, so the generated
    grants let every writer store its records. Key templates hold only placeholders
    membership code fills for a host, and never make a wildcard of one.
    """

    roles = set(registry["host_roles"])
    check_key_value_stream(transport.get("key_value_stream"))
    for bucket, spec in transport["key_value"].items():
        where = f"transport.yaml key_value.{bucket}"
        if set(re.findall(r"\{([a-z_]+)\}", bucket)) - BUCKET_PLACEHOLDERS \
                or not re.fullmatch(r"[A-Za-z0-9_-]+", re.sub(r"\{[a-z_]+\}", "x", bucket)):
            raise ContractError(f"{where}: a bucket name holds letters, digits, _ and -, and only {{team}} as a "
                                "placeholder")
        if set(spec) != {"description", "per_key_ttl_seconds", "readers", "writers"}:
            raise ContractError(f"{where}: fields are description, per_key_ttl_seconds, readers and writers")
        if not spec["readers"] or not set(spec["readers"]) <= roles or len(set(spec["readers"])) != len(spec["readers"]):
            raise ContractError(f"{where}: readers are distinct host roles")
        if not isinstance(spec["writers"], dict) or not spec["writers"] or not set(spec["writers"]) <= roles:
            raise ContractError(f"{where}: writers name host roles")
        for role, keys in spec["writers"].items():
            if not isinstance(keys, list) or not keys or not all(isinstance(key, str) and key for key in keys):
                raise ContractError(f"{where}: writer {role} lists the key templates it may write")
            for key in keys:
                check_key_template(where, key)
    for name, entry in registry["state_records"].items():
        writers = transport["key_value"][entry["bucket"]]["writers"]
        if not set(entry["writers"]) <= set(writers):
            raise ContractError(f"registry.yaml state_records.{name}: its writers {sorted(set(entry['writers']) - set(writers))} "
                                f"have no key template in transport.yaml key_value.{entry['bucket']}")
        for role in entry["writers"]:
            if not any(key_form_covered(entry["key"], template) for template in writers[role]):
                raise ContractError(f"registry.yaml state_records.{name}: its key {entry['key']!r} fits none of {role}'s key "
                                    f"templates in transport.yaml key_value.{entry['bucket']} {writers[role]}")


# Which parts of a record's key form (registry.yaml state_records, <name>) a key template's
# placeholder may stand for: membership code fills {self} with the host's runtime, {peer} with
# another installation's id, and the others with the value of the same name.
KEY_FORM_FILLERS = {"self": {"runtime"}, "team": {"team"}, PARTITION_PLACEHOLDER: {"partition"},
                    "installation": {"installation"}, "peer": {"installation", "peer"}}


def key_form_covered(form: str, template: str) -> bool:
    """Whether a writer holding ``template`` may write every key of the record's key ``form``.

    Token by token: a word is itself, a placeholder only the parts of the form it fills
    (KEY_FORM_FILLERS), ``*`` any one part and a final ``>`` one part or more. So a record's
    key and its writers' grants cannot drift apart unnoticed (an agent record's key gained
    the partition, which its writers' templates name).
    """

    parts, tokens = form.split("."), template.split(".")
    for index, token in enumerate(tokens):
        if token == ">":
            return len(parts) > index
        if index >= len(parts):
            return False
        placeholder, part = re.fullmatch(r"\{([a-z_]+)\}", token), re.fullmatch(r"<([a-z_]+)>", parts[index])
        if placeholder is not None:
            if part is None or part.group(1) not in KEY_FORM_FILLERS.get(placeholder.group(1), set()):
                return False
        elif token != "*" and token != parts[index]:
            return False
    return len(parts) == len(tokens)


# The stream settings every object store has (transport.yaml object_store_stream): no rollup
# headers, since NATS checks a publish against its subject only and a writer's Nats-Rollup: all
# would erase every object of the bucket; deleting an object purges its chunks (the deleters'
# grant alone); no message is deleted one by one; full stores refuse new writes (discard: new)
# rather than dropping live objects. Storage and max_consumers are choices.
OBJECT_STORE_STREAM = {"allow_rollup_hdrs": False, "deny_delete": True, "deny_purge": False, "discard": "new"}
OBJECT_STORE_FIELDS = {"description", "object_name", "headers", "readers", "writers", "deleters", "chunk_bytes",
                       "orphan_chunk_grace_seconds", "size_kind"}
# When an object's metadata carries a header: always, as one of the owner headers (exactly one
# of them), or when a person's work stored the object.
BLOB_HEADER_PRESENCE = {"always", "owner", "person"}
BLOB_REF = ("common.schema.json", "/$defs/blob_ref")
# Joins an object name's parts; neither a team id nor a base64url digest holds it.
BLOB_NAME_SEPARATOR = "~"


def check_object_store(registry: dict[str, Any], transport: dict[str, Any], schemas: dict[str, dict[str, Any]]) -> None:
    """Who reads, writes and deletes blobs, and the headers that name a blob's owner (README, "Blobs").

    NATS cannot limit a reader to some objects of a bucket, so the readers are exactly
    the roles that receive a kind or reply that can carry a blob reference: a role
    that receives one could not read it without the grant, and any other role gets
    no grant it does not need. An object's name holds its team, so one team's
    services never delete another team's copy of the same bytes. A writer looks the
    object up before it puts it, since an object that exists already holds the same
    bytes and replacing it would purge the other's chunks, which only deleters may;
    a deleter marks the object deleted as a writer does, then purges its chunks.
    """

    where = "transport.yaml object_store_stream"
    settings = transport.get("object_store_stream")
    if not isinstance(settings, dict) or set(settings) != {*OBJECT_STORE_STREAM, "storage", "max_consumers"}:
        raise ContractError(f"{where}: fields are {sorted({*OBJECT_STORE_STREAM, 'storage', 'max_consumers'})}")
    for field_name, value in OBJECT_STORE_STREAM.items():
        if type(settings[field_name]) is not type(value) or settings[field_name] != value:
            raise ContractError(f"{where}: {field_name} is {value} (README, \"Blobs\")")
    if settings["storage"] not in {"file", "memory"}:
        raise ContractError(f"{where}: storage is file or memory")
    if type(settings["max_consumers"]) is not int or settings["max_consumers"] < 1:
        raise ContractError(f"{where}: max_consumers is a positive whole number")
    roles = set(registry["host_roles"])
    receivers = blob_receivers(registry, transport, schemas)
    size_kinds = set(transport.get("queues", {}).get("size_kinds", []))
    for bucket, spec in transport["object_store"].items():
        where = f"transport.yaml object_store.{bucket}"
        if not isinstance(spec, dict) or set(spec) != OBJECT_STORE_FIELDS or "{" in bucket:
            raise ContractError(f"{where}: an installation-wide bucket with fields {sorted(OBJECT_STORE_FIELDS)}")
        if spec["size_kind"] not in size_kinds:
            raise ContractError(f"{where}: size_kind is one of transport.yaml queues.size_kinds")
        check_blob_name(where, spec["object_name"], schemas)
        for field_name in ("readers", "writers", "deleters"):
            listed = spec[field_name]
            if not isinstance(listed, list) or not listed or not set(listed) <= roles or len(set(listed)) != len(listed):
                raise ContractError(f"{where}: {field_name} are distinct host roles")
        if set(spec["readers"]) != receivers:
            raise ContractError(f"{where}: readers are exactly the roles that receive a kind or reply that can carry a "
                                f"blob reference: {sorted(receivers)}")
        if not set(spec["writers"]) <= set(spec["readers"]) or not set(spec["deleters"]) <= set(spec["writers"]):
            raise ContractError(f"{where}: writers also read (to find an object before putting it) and deleters also write")
        check_blob_headers(where, spec["headers"], schemas)
        check_blob_timing(where, spec, transport)


# Knowledge text over long_text's length travels as a blob (README, "Knowledge"; decision 77).
KNOWLEDGE_PREFIX = "knowledge."
LONG_TEXT = ("common.schema.json", "/$defs/long_text")


def pattern_longest_match(pattern: str) -> int | None:
    """The most characters a value matching ``pattern`` can hold, or None when it has no bound.

    Patterns are anchored and portable (``pattern_problem``): literals, escapes, character
    classes, groups with alternatives, and the quantifiers ?, *, +, {n}, {n,} and {n,m}, each
    optionally lazy. Every atom matches one character, a group the longest of its
    alternatives, and a quantifier multiplies by its upper bound, so nested repetitions such
    as (?:[a-z]{0,200}){0,200} count 40,000. A pattern that is not portable, such as one with
    {,n} (Python reads it as 0 to n, ECMAScript as literal text), counts as unbounded, so a
    check that relies on this count fails closed.
    """

    if pattern_problem(pattern):
        return None
    longest, end = _sequence_longest(pattern[1:-1], 0)
    if end != len(pattern) - 2:
        raise ContractError(f"pattern {pattern!r}: an unmatched )")
    return longest


def _sequence_longest(body: str, index: int) -> tuple[int | None, int]:
    """The longest match of the alternatives from ``index`` to the closing ) or the end, and where they stop."""

    best: int | None = 0
    total: int | None = 0
    while index < len(body) and body[index] != ")":
        if body[index] == "|":
            best = None if best is None or total is None else max(best, total)
            total, index = 0, index + 1
            continue
        atom, index = _atom_longest(body, index)
        upper, index = _quantifier_upper(body, index)
        total = None if total is None or atom is None or upper is None else total + atom * upper
    return (None if best is None or total is None else max(best, total)), index


def _atom_longest(body: str, index: int) -> tuple[int | None, int]:
    """The longest match of the atom at ``index`` (one character, or a group), and where it ends."""

    character = body[index]
    if character == "\\":
        return 1, index + 2
    if character == "[":
        index += 1
        while body[index] != "]":
            index += 2 if body[index] == "\\" else 1
        return 1, index + 1
    if character == "(":
        index += 3 if body.startswith("(?:", index) else 1
        longest, index = _sequence_longest(body, index)
        if index >= len(body):
            raise ContractError(f"pattern body {body!r}: an unclosed (")
        return longest, index + 1
    return 1, index + 1


def _quantifier_upper(body: str, index: int) -> tuple[int | None, int]:
    """How many times the quantifier at ``index`` lets its atom repeat at most (1 without one), and where it ends."""

    upper: int | None = 1
    repetition = REPETITION.match(body, index)
    if repetition:
        low, comma, high = repetition.groups()
        upper = int(low) if comma is None else (int(high) if high else None)
        index = repetition.end()
    elif index < len(body) and body[index] in "?*+":
        upper = 1 if body[index] == "?" else None
        index += 1
    else:
        return upper, index
    return upper, index + 1 if body.startswith("?", index) else index


def unbounded_text(resolver: "ModelWriter", file: str, node: Any, at: str, limit: int,
                   seen: frozenset[tuple[str, str]], found: list[str]) -> None:
    """Collect the string fields under a schema that may hold more than ``limit`` characters inline.

    A blob reference is the long form and is not walked. A string (a type of "string", or a
    type list that holds it) is bounded by its maxLength, a const or enum, a format, or, without
    a maxLength, by the longest match of its pattern (``pattern_longest_match``); a bound above
    ``limit`` counts as unbounded. The walk follows every keyword that can
    hold a string, including those the contract's schemas do not use yet, so a new schema
    form fails closed rather than passing unexamined.
    """

    if isinstance(node, list):
        for item in node:
            unbounded_text(resolver, file, item, at, limit, seen, found)
        return
    if not isinstance(node, dict):
        return
    if "$ref" in node:
        target = resolver.resolve(file, node["$ref"])
        if target != BLOB_REF and target not in seen:
            unbounded_text(resolver, target[0], resolver.node(*target), at, limit, seen | {target}, found)
        return
    kind = node.get("type")
    text = kind == "string" or (isinstance(kind, list) and "string" in kind)
    if text and "const" not in node and "enum" not in node and "format" not in node:
        length = node.get("maxLength")
        if length is None and "pattern" in node:
            length = pattern_longest_match(node["pattern"])
        if length is None or length > limit:
            found.append(at or "/")
    for key, value in node.items():
        if key in SCHEMA_NAMED_CHILDREN:
            for name, child in value.items():
                unbounded_text(resolver, file, child, f"{at}/{name}", limit, seen, found)
        elif key in SCHEMA_CHILDREN:
            unbounded_text(resolver, file, value, at, limit, seen, found)


# The JSON Schema keywords whose values are schemas a value of the node must (or may) match:
# named children (by property name or pattern) and unnamed ones.
SCHEMA_NAMED_CHILDREN = {"properties", "patternProperties"}
SCHEMA_CHILDREN = {"items", "prefixItems", "additionalProperties", "oneOf", "anyOf", "allOf"}


def check_knowledge_text(registry: dict[str, Any], schemas: dict[str, dict[str, Any]]) -> None:
    """Every Knowledge field that may exceed long_text's length has a blob form (A6, decision 77).

    Pages, notes, revisions and proposals have no length limit in the knowledge service, so
    a request or reply field that held them as a plain string would either cut them or fail
    for a long page. Fields that are text_content (inline up to long_text, or a blob) are
    fine; any other string must be bounded at or below long_text's length.
    """

    resolver = ModelWriter(schemas)
    limit = resolver.node(*LONG_TEXT)["maxLength"]
    for name, entry in registry["kinds"].items():
        if not name.startswith(KNOWLEDGE_PREFIX):
            continue
        for label, file in ((name, entry["body"]), (f"{name}.reply", entry.get("reply", {}).get("body"))):
            found: list[str] = []
            if file:
                unbounded_text(resolver, file, schemas[file], "", limit, frozenset(), found)
            if found:
                raise ContractError(f"{file}: {label} text at {sorted(set(found))} may exceed {limit} characters "
                                    "inline; use text_content, whose long form is a blob (README, \"Knowledge\")")


# The installation's NATS account (transport.yaml nats_account; README, "NATS users and the
# account"): the settings the launcher or an external server's operator puts in the account,
# and the user every process logs in with. The installation authority's and a joining host's
# users exist before any certificate does, so the launcher writes them (the authority writes a
# remote host's bootstrap user); every other process's user comes with its certificate.
NATS_ACCOUNT_FIELDS = {"mode", "signing_key_holder", "operator_signing_key_holder", "credential_lifetime_setting",
                       "account_settings", "users"}
# Every user JWT ends after this instance setting's lifetime (owner decision 2026-11-17, README "NATS users and the account").
CREDENTIAL_LIFETIME_SETTING = "transport.nats.credential_lifetime_seconds"
# A bootstrap user reads only replies under this token of its runtime's inbox: its join's ("Starting a host").
JOIN_INBOX_TOKEN = "join"
NATS_ACCOUNT_SETTINGS = {"jetstream": True, "mappings": "partitions.account_mappings"}
NATS_USER_FIELDS = {"permissions", "issued_by", "carried_in"}
HOST_USER = "host"
CERTIFICATE_ROLES = "certificate_roles"
CONNECTION_CREDENTIAL_REF = "common.schema.json#/$defs/connection_credential"


def credential_replies(registry: dict[str, Any], schemas: dict[str, dict[str, Any]]) -> list[str]:
    """The kinds whose reply carries a host's NATS connection credential (host.join, host.renew)."""

    return sorted(name for name, entry in registry["kinds"].items()
                  if "reply" in entry and CONNECTION_CREDENTIAL_REF in json.dumps(schemas[entry["reply"]["body"]]))


def check_nats_account(registry: dict[str, Any], transport: dict[str, Any], schemas: dict[str, dict[str, Any]]) -> None:
    """Operator mode, one account whose signing key the topology owner holds, its settings, and every process's user.

    Decisions 88 and 89: every process logs in with its own user JWT, and an external server's
    operator includes the settings in the installation's account. The authority issues the
    hosts' users, so it is the one that holds the account's signing key, and it already
    creates every stream; the settings carry the partition mappings, without which no agent's
    queue reaches its partition stream. The launcher signs only the first users, before any
    process starts; a remote host's bootstrap user comes from the authority. Revoking a user
    needs an operator signature (README, "NATS users and the account"): on the bundled server
    the topology owner also holds an operator signing key; on any server every user JWT ends
    after the lifetime the named setting holds, so it stops without one (owner decision
    2026-11-17).
    """

    where = "transport.yaml nats_account"
    spec = transport.get("nats_account")
    owner = transport["topology_owner"]
    if not isinstance(spec, dict) or set(spec) != NATS_ACCOUNT_FIELDS or spec["mode"] != "operator" \
            or spec["signing_key_holder"] != owner or spec["operator_signing_key_holder"] != owner:
        raise ContractError(f"{where}: operator mode, and the topology owner ({owner}) holds the account's signing key "
                            "and, on the bundled server, an operator signing key")
    if spec["credential_lifetime_setting"] != CREDENTIAL_LIFETIME_SETTING:
        raise ContractError(f"{where}: credential_lifetime_setting names {CREDENTIAL_LIFETIME_SETTING}")
    if spec["account_settings"] != NATS_ACCOUNT_SETTINGS:
        raise ContractError(f"{where}: account_settings are {NATS_ACCOUNT_SETTINGS}")
    expected = {
        owner: {"permissions": owner, "issued_by": "launcher", "carried_in": "trust_folder"},
        JOINING: {"permissions": JOINING, "issued_by": ["launcher", owner], "carried_in": "bootstrap_bundle"},
        HOST_USER: {"permissions": CERTIFICATE_ROLES, "issued_by": owner,
                    "carried_in": [f"{kind}.reply" for kind in credential_replies(registry, schemas)]},
    }
    users = spec["users"]
    if not isinstance(users, dict) or set(users) != set(expected) \
            or any(not isinstance(user, dict) or set(user) != NATS_USER_FIELDS for user in users.values()):
        raise ContractError(f"{where}: users are {sorted(expected)}, each with {sorted(NATS_USER_FIELDS)}")
    for name, user in expected.items():
        if users[name] != user:
            raise ContractError(f"{where}: users.{name} is {user}")
    for kind in credential_replies(registry, schemas):
        if registry["kinds"][kind]["receivers"] != [owner]:
            raise ContractError(f"{where}: only {owner} answers {kind}, whose reply carries a credential")


def check_blob_timing(where: str, spec: dict[str, Any], transport: dict[str, Any]) -> None:
    """A chunk fits one NATS message, and the orphan sweep's grace leaves a writer time to finish.

    A writer gives a write up when its metadata would come later than half the grace after
    it began, and the sweep waits the whole grace plus the clock tolerance after a chunk
    subject's newest message, so the grace must be more than twice the clock tolerance for
    a writer to have any time at all (README, "Blobs").
    """

    chunk, grace = spec["chunk_bytes"], spec["orphan_chunk_grace_seconds"]
    if type(chunk) is not int or not 0 < chunk <= transport["limits"]["nats_max_payload_bytes"]:
        raise ContractError(f"{where}: chunk_bytes is a whole number of bytes that fits nats_max_payload_bytes")
    if type(grace) is not int or grace <= 2 * transport["clock_skew_seconds"]:
        raise ContractError(f"{where}: orphan_chunk_grace_seconds is a whole number of seconds over twice "
                            f"clock_skew_seconds")


def check_blob_name(where: str, template: Any, schemas: dict[str, dict[str, Any]]) -> None:
    """An object's name is its team and one blob_ref field, joined by the separator (README, "Blobs")."""

    fields = schemas[BLOB_REF[0]]["$defs"]["blob_ref"]["properties"]
    parts = template.split(BLOB_NAME_SEPARATOR) if isinstance(template, str) else []
    placeholders = [part[1:-1] for part in parts if re.fullmatch(r"\{[a-z0-9_]+\}", part)]
    if len(parts) != 2 or placeholders != [part[1:-1] for part in parts] or placeholders[0] != "team" \
            or placeholders[1] not in fields:
        raise ContractError(f"{where}: object_name is {{team}}{BLOB_NAME_SEPARATOR}{{<blob_ref field>}}: the team of the "
                            f"frame that carries the reference, then the field the object is named by")
    # The name splits one way only if neither part's pattern allows the separator.
    shared = schemas[BLOB_REF[0]]["$defs"]
    field = fields[placeholders[1]]
    types = [shared["team_id"], shared[field["$ref"].rsplit("/", 1)[1]] if "$ref" in field else field]
    if any(BLOB_NAME_SEPARATOR in kind.get("pattern", BLOB_NAME_SEPARATOR) for kind in types):
        raise ContractError(f"{where}: object_name's parts have patterns without {BLOB_NAME_SEPARATOR!r}, so a name "
                            f"splits one way")


def check_blob_headers(where: str, headers: Any, schemas: dict[str, dict[str, Any]]) -> None:
    """Each header holds a value of a named shared type, and the owner headers name one owner each."""

    shared = schemas["common.schema.json"]["$defs"]
    if not isinstance(headers, dict) or not headers:
        raise ContractError(f"{where}: headers name the metadata headers of every object")
    for name, header in headers.items():
        if not re.fullmatch(r"UA-Blob-[A-Za-z0-9]+(-[A-Za-z0-9]+)*", name):
            raise ContractError(f"{where}: header {name!r} is UA-Blob- followed by words joined by -")
        if not isinstance(header, dict) or set(header) != {"value", "presence"} or header["value"] not in shared \
                or header["presence"] not in BLOB_HEADER_PRESENCE:
            raise ContractError(f"{where}: header {name} names a common.schema.json type (value) and when it is "
                                f"present ({sorted(BLOB_HEADER_PRESENCE)})")
    if sum(header["presence"] == "owner" for header in headers.values()) < 2:
        raise ContractError(f"{where}: the owner headers name at least two kinds of owner (a request, an agent)")


def reaches_blob(resolver: "ModelWriter", file: str, node: Any, seen: set[tuple[str, str]]) -> bool:
    """Whether a schema can hold a blob reference anywhere, through any reference."""

    if isinstance(node, list):
        return any(reaches_blob(resolver, file, item, seen) for item in node)
    if not isinstance(node, dict):
        return False
    if "$ref" in node:
        target = resolver.resolve(file, node["$ref"])
        if target == BLOB_REF:
            return True
        if target in seen:
            return False
        seen.add(target)
        return reaches_blob(resolver, target[0], resolver.node(*target), seen)
    return any(reaches_blob(resolver, file, item, seen) for item in node.values())


def blob_receivers(registry: dict[str, Any], transport: dict[str, Any], schemas: dict[str, dict[str, Any]]) -> set[str]:
    """The roles that receive a kind, a copy of it, or a reply whose schema can carry a blob reference."""

    resolver = ModelWriter(schemas)
    roles: set[str] = set()
    for entry in registry["kinds"].values():
        if reaches_blob(resolver, entry["body"], schemas[entry["body"]], set()):
            roles.update(role for address in entry_addresses(entry) for role in receiving_roles(entry, address, transport))
        reply = entry.get("reply", {}).get("body")
        if reply and reaches_blob(resolver, reply, schemas[reply], set()):
            # A reply goes back to whoever sent the request.
            roles.update(role for address in own_addresses(entry) for role in address_senders(entry, address))
    return roles


def check_key_value_stream(settings: Any) -> None:
    """Every bucket's stream refuses rollup headers and stream-API deletes and serves direct reads (README, "Key-value grants")."""

    where = "transport.yaml key_value_stream"
    if not isinstance(settings, dict) or set(settings) != {*KEY_VALUE_STREAM, "storage", "max_consumers"}:
        raise ContractError(f"{where}: fields are {sorted({*KEY_VALUE_STREAM, 'storage', 'max_consumers'})}")
    for field_name, value in KEY_VALUE_STREAM.items():
        if type(settings[field_name]) is not type(value) or settings[field_name] != value:
            raise ContractError(f"{where}: {field_name} is {value} (README, \"Every bucket's stream settings\")")
    if settings["storage"] not in {"file", "memory"}:
        raise ContractError(f"{where}: storage is file or memory")
    if type(settings["max_consumers"]) is not int or settings["max_consumers"] < 1:
        raise ContractError(f"{where}: max_consumers is a positive whole number")


def check_key_template(where: str, key: str) -> None:
    """A key template names only known placeholders and subject characters, and never starts with a wildcard.

    A template that starts with ``*`` or ``>`` would reach every key of the bucket, every
    other host's or agent's record included; ``>`` may only end a template, as in NATS.
    """

    if set(re.findall(r"\{([a-z_]+)\}", key)) - KEY_PLACEHOLDERS or re.search(r"[\s/\\]", key):
        raise ContractError(f"{where}: key template {key!r} uses only {sorted(KEY_PLACEHOLDERS)} and subject characters")
    tokens = key.split(".")
    if tokens[0] in {"*", ">"} or ">" in tokens[:-1] or "" in tokens:
        raise ContractError(f"{where}: key template {key!r} starts with a fixed word or a placeholder, has no empty "
                            "token, and ends with > if it holds one")


def check_class_streams(name: str, spec: dict[str, Any], transport: dict[str, Any], skew: int) -> None:
    """A queued class's streams exist and keep its messages for its longest expiry plus the clock tolerance."""

    for stream_name in spec.get("streams", []):
        stream = transport["streams"].get(stream_name)
        if stream is None:
            raise ContractError(f"transport.yaml: class {name} names an unknown stream {stream_name}")
        if stream.get("max_age_seconds", 0) < spec["max_expiry_seconds"] + skew:
            raise ContractError(f"transport.yaml: stream {stream_name} must keep messages for class {name}'s "
                                "max_expiry_seconds plus the clock tolerance")


def subject_tokens(subject: str) -> list[str]:
    """A subject template's tokens; a {placeholder} stands for one token, like *."""

    return [token if not re.fullmatch(r"\{[a-z_]+\}", token) else "*" for token in subject.split(".")]


def subject_covers(pattern: str, subject: str) -> bool:
    """Whether every subject the template ``subject`` stands for matches ``pattern``.

    A placeholder in the pattern matches only the same placeholder, so a stream for
    one team covers that team's addresses, not every team's.
    """

    want, have = pattern.split("."), subject.split(".")
    for index, token in enumerate(want):
        if token == ">":
            return len(have) > index
        if index >= len(have) or have[index] == ">" or not (token == "*" or token == have[index]):
            return False
    return len(want) == len(have)


def subjects_overlap(first: str, second: str) -> bool:
    """Whether some subject matches both templates (NATS refuses streams whose subjects overlap)."""

    left, right = subject_tokens(first), subject_tokens(second)
    for index in range(max(len(left), len(right))):
        if index >= len(left) or index >= len(right):
            return False
        if ">" in (left[index], right[index]):
            return True
        if "*" not in (left[index], right[index]) and left[index] != right[index]:
            return False
    return True


def check_stream_subjects(transport: dict[str, Any]) -> None:
    """Every queued address is stored in exactly one stream of its class, and no stream stores other traffic."""

    for name, spec in transport["streams"].items():
        check_stream_name(name, spec)
    patterns = [(name, subject) for name, spec in transport["streams"].items() for subject in spec["subjects"]]
    check_streams_disjoint(patterns)
    for class_name, spec in transport["classes"].items():
        for address in class_addresses(transport, class_name):
            check_address_storage(class_name, spec.get("streams"), address, stored_subject(address, "*", transport), patterns)
    for key, consumer in transport["consumers"].items():
        stream = transport["streams"].get(consumer["stream"])
        if stream is None or not any(subject_covers(pattern, consumer["filter"]) for pattern in stream["subjects"]):
            raise ContractError(f"transport.yaml consumers.{key}: its stream stores its filter's subjects")


def check_streams_disjoint(patterns: list[tuple[str, str]]) -> None:
    """No two streams store one subject, which NATS refuses.

    One team's instance of a {team} stream is compared with the other streams; two
    teams' instances cannot overlap, since each subject names its team.
    """

    for index, (name, subject) in enumerate(patterns):
        overlapping = [other for other, pattern in patterns[index + 1:] if other != name and subjects_overlap(subject, pattern)]
        if overlapping:
            raise ContractError(f"transport.yaml: streams {name} and {overlapping[0]} overlap, which NATS refuses")


def check_stream_name(name: str, spec: dict[str, Any]) -> None:
    """A stream name may hold {team}, and {partition} joined to it; it stores only that team's (and partition's) subjects."""

    placeholders = set(re.findall(r"\{([a-z_]+)\}", name))
    if placeholders - {"team", PARTITION_PLACEHOLDER} or (
            PARTITION_PLACEHOLDER in placeholders and PARTITION_STREAM_PART not in name):
        raise ContractError(f"transport.yaml: stream {name} may name only {{team}}, and {{partition}} as "
                            f"{PARTITION_STREAM_PART}")
    team_token = f"{SUBJECT_ROOT}{{team}}."
    if placeholders and not all(subject.startswith(team_token) for subject in spec["subjects"]):
        raise ContractError(f"transport.yaml: each subject of stream {name} starts {team_token}")
    partition_token = ".{" + PARTITION_PLACEHOLDER + "}"
    if (PARTITION_PLACEHOLDER in placeholders) != all(subject.endswith(partition_token) for subject in spec["subjects"]) \
            or any(partition_token in subject[:-len(partition_token)] for subject in spec["subjects"]):
        raise ContractError(f"transport.yaml: stream {name}'s subjects end with {{partition}} exactly when its name holds it")
    if re.search(r"[\s.*>/\\]", name.replace("{team}", "").replace("{" + PARTITION_PLACEHOLDER + "}", "")):
        raise ContractError(f"transport.yaml: stream {name}: NATS stream names cannot hold whitespace, ., *, >, / or \\")


def check_address_storage(class_name: str, streams: list[str] | None, address: str, subject: str,
                          patterns: list[tuple[str, str]]) -> None:
    """A queued class's address, stored under ``subject``, is in exactly one of its streams; any other class's in none."""

    storing = [stream for stream, pattern in patterns if subjects_overlap(pattern, subject)]
    if streams is None:
        if storing:
            raise ContractError(f"transport.yaml: stream {storing[0]} stores {class_name} traffic ({address})")
        return
    covering = [stream for stream, pattern in patterns if subject_covers(pattern, subject)]
    if len(covering) != 1 or storing != covering or covering[0] not in streams:
        raise ContractError(f"transport.yaml: {address} is stored in exactly one of the {class_name} class's streams")


def class_addresses(transport: dict[str, Any], delivery_class: str) -> list[str]:
    spec = transport["classes"][delivery_class]
    return list(spec["addresses"]) + list(spec.get("installation_addresses", []))


def address_matches(address: str, template: str, kind: str) -> bool:
    """Whether a kind's address is the class template; an event's names the event, an operation's only its family."""

    return address == template.replace("{event}", kind.replace(".", "_"))


# -- generated JSON ------------------------------------------------------------------------------

def send_pattern(address: str) -> str:
    """The subjects a sender may publish an address to: any receiver of its team and any copy id.

    The sender placeholder stays: full_subject binds it to the publisher.
    """

    return re.sub(r"\{(?!sender\})[a-z_]+\}", "*", address)


def publication_addresses(entry: dict[str, Any], transport: dict[str, Any]) -> list[str]:
    addresses = entry_addresses(entry)
    if entry["class"] == "dead_letter":
        # Exact raw copies have no frame kind, but the receiver must be able
        # to store them beside its signed notices, under its own runtime id.
        addresses += transport["classes"]["dead_letter"]["addresses"]
    return addresses


def consumer_permissions(address: str, transport: dict[str, Any]) -> set[str]:
    """Pull, inspect and acknowledge the consumers for this delivery address.

    Consumer names keep their {team} and receiver placeholders; membership code fills
    them with the host's own teams and each pool and edge it runs or serves, never with
    wildcards, so a host reads no other host's pools. In a partition stream the grant
    names the stream and any consumer: the topology owner creates consumers there only
    for agents placed in the partition, and they all run on the host that owns it, so
    the host's credential changes with its partitions, never its agents. Creating
    consumers stays with the topology owner.
    """

    # A dead-letter address names the copy id, which no consumer filters on.
    expected_filter = stored_subject(address.replace("{copy}", "*"), "*", transport)
    matches = [consumer for consumer in transport["consumers"].values() if consumer["filter"] == expected_filter]
    if not matches:
        raise ContractError(f"no pull consumer for {address}")
    subjects = set()
    for consumer in matches:
        stream = consumer["stream"]
        name = "*" if "{" + PARTITION_PLACEHOLDER + "}" in stream else consumer["name"]
        subjects.update({
            f"$JS.API.CONSUMER.INFO.{stream}.{name}",
            f"$JS.API.CONSUMER.MSG.NEXT.{stream}.{name}",
            f"$JS.ACK.{stream}.{name}.>",
        })
    return subjects


# The topology owner's JetStream grants, on every stream of the account (README, "Who sets up
# the streams"): the account's information (its storage and what the streams set aside, checked
# before it creates any), streams, their consumers (bucket watches that readers left behind
# among them) and single messages (a team's removal and the old streams at an upgrade).
TOPOLOGY_OWNER_GRANTS = frozenset({
    "$JS.API.INFO",
    *(f"$JS.API.STREAM.{action}.*" for action in ("CREATE", "UPDATE", "INFO", "DELETE", "MSG.GET", "MSG.DELETE")),
    *(f"$JS.API.CONSUMER.{action}.*.*" for action in ("CREATE", "DURABLE.CREATE", "INFO", "DELETE")),
    "$JS.API.CONSUMER.CREATE.*.*.>", "$JS.API.CONSUMER.NAMES.*", "$JS.API.CONSUMER.LIST.*",
})
# The stream size check's grants (README, "Stream sizes"): the account's information, and every
# stream's settings and state in one paged list, two lines whatever the number of teams and
# partitions. Metadata only: neither reads a message.
SIZE_CHECK_GRANTS = frozenset({"$JS.API.INFO", "$JS.API.STREAM.LIST"})


def stream_management_grants(transport: dict[str, Any]) -> dict[str, set[str]]:
    """The JetStream API subjects of the topology owner, the role that withdraws queued work and the pool controller.

    The topology owner creates, updates, inspects and deletes every stream and consumer of
    the account: it already decides which hosts serve which teams, partitions and pools and
    holds the account's signing key, so wider grants add no power, and lines per team and
    partition would grow with both. The withdrawal role reads its own team's streams to
    find a queued message and deletes it by its stream sequence; NATS cannot narrow either
    to some subjects of a stream, so withdrawal uses per-team streams only. The pool
    controller's host reads the information of every consumer of its team's pool stream, and
    the size check's role the account's information and every stream's size.
    """

    grants = {transport["topology_owner"]: set(TOPOLOGY_OWNER_GRANTS)}
    grants.setdefault(transport["size_check"]["role"], set()).update(SIZE_CHECK_GRANTS)
    controller = transport["pool_controller"]
    grants.setdefault(controller["role"], set()).add(f"$JS.API.CONSUMER.INFO.{controller['stream']}.*")
    # Withdrawal and dead-letter custody read a message by its sequence and delete it there.
    for key in ("withdrawal", "dead_letter_custody"):
        custody = transport[key]
        grants.setdefault(custody["role"], set()).update(
            f"$JS.API.STREAM.MSG.{action}.{stream}" for stream in custody["streams"] for action in ("GET", "DELETE"))
    return grants


def bucket_stream(bucket: str) -> str:
    """The JetStream stream behind a key-value bucket."""

    return f"KV_{bucket}"


def key_value_grants(transport: dict[str, Any]) -> dict[str, set[str]]:
    """Per role: the subjects that read and write the key-value buckets (README, "Key-value grants").

    A reader gets the bucket's stream information, its direct reads, and what a watch
    needs: creating an unnamed consumer, whose deliveries arrive on the reader's own inbox
    (every role already subscribes to it), reading that consumer's information (the server
    names it, so the grant covers any name; it only reads), and answering its flow control.
    Only the unnamed create: NATS refuses a durable consumer through it, and a request that
    names no consumer cannot change an existing one, so no host changes another host's
    watch; no reader may delete a consumer either. A writer publishes the keys its templates
    name; membership code fills them like subjects, so a host writes only its own records and
    its own agents'. A bucket per team is filled per team, so its grants reach no other team.
    """

    grants: dict[str, set[str]] = {}
    for bucket, spec in transport["key_value"].items():
        stream = bucket_stream(bucket)
        reads = {f"$JS.API.STREAM.INFO.{stream}", f"$JS.API.DIRECT.GET.{stream}", f"$JS.API.DIRECT.GET.{stream}.>",
                 f"$JS.API.CONSUMER.CREATE.{stream}", f"$JS.API.CONSUMER.INFO.{stream}.*", f"$JS.FC.{stream}.>"}
        for role in spec["readers"]:
            grants.setdefault(role, set()).update(reads)
        for role, keys in spec["writers"].items():
            grants.setdefault(role, set()).update(f"$KV.{bucket}.{key}" for key in keys)
    return grants


def object_stream(bucket: str) -> str:
    """The JetStream stream behind an object store."""

    return f"OBJ_{bucket}"


def object_store_grants(transport: dict[str, Any]) -> dict[str, set[str]]:
    """Per role: the subjects that read, write and delete objects (README, "Blobs").

    A reader gets the bucket's stream information and direct message gets
    (``$JS.API.STREAM.MSG.GET.<stream>``) to read an object's last metadata
    (``last_by_subj``) and its chunks in sequence order (``next_by_subj``).
    Readers hold no consumer-create grant (closing PR #151 S2) and no
    stream-name lookup, which would list every team's streams. A writer
    publishes chunks and metadata without a rollup header; NATS cannot tie a
    publish to one object, so readers check every blob's SHA-256 against its
    name. A deleter purges the object's chunks after marking it deleted; the
    purge grant covers the whole bucket, so only the team services hold it.
    """

    grants: dict[str, set[str]] = {}
    for bucket, spec in transport["object_store"].items():
        stream = object_stream(bucket)
        reads = {f"$JS.API.STREAM.INFO.{stream}", f"$JS.API.STREAM.MSG.GET.{stream}"}
        for role in spec["readers"]:
            grants.setdefault(role, set()).update(reads)
        for role in spec["writers"]:
            grants.setdefault(role, set()).update({f"$O.{bucket}.C.>", f"$O.{bucket}.M.>"})
        for role in spec["deleters"]:
            grants.setdefault(role, set()).add(f"$JS.API.STREAM.PURGE.{stream}")
    return grants


def permissions(registry: dict[str, Any], transport: dict[str, Any]) -> dict[str, dict[str, Any]]:
    """Per host role: the NATS permissions it gets, as templates.

    {team} is each team the host serves and {self} its runtime id. {partition}, {pool} and
    {edge} are each partition the host owns, each pool it runs and each edge it serves:
    membership code repeats a subject once per id and never turns one into a wildcard, so
    a host receives requests and queued work only for the agents of its own partitions and
    its own pools, and its credential changes when it gains or loses a partition or a
    pool, never an agent. No template names an agent. A host publishes only under its own id.
    A receiver of requests answers each one once, within the class's expiry, through
    NATS allow_responses; it holds no grant on other runtimes' inboxes.
    """

    reply_window = transport["classes"]["request_reply"]["max_expiry_seconds"] + transport["clock_skew_seconds"]
    management = stream_management_grants(transport)
    buckets = key_value_grants(transport)
    blobs = object_store_grants(transport)
    result = {}
    for role in registry["host_roles"]:
        publish: set[str] = set(management.get(role, set())) | buckets.get(role, set()) | blobs.get(role, set())
        subscribe = {f"{transport['reply_inbox_prefix'].replace('{runtime}', '{self}')}.>"}
        answers_requests = False
        for entry in registry["kinds"].values():
            publish.update(publish_subject(address, transport) for address in publication_addresses(entry, transport)
                           if role in address_senders(entry, address))
            for address in entry_addresses(entry):
                if role not in receiving_roles(entry, address, transport):
                    continue
                if entry["class"] in {"durable", "dead_letter"}:
                    # Queued work and dead-letter notices are pulled from their consumers.
                    publish.update(consumer_permissions(address, transport))
                else:
                    subscribe.add(subscribe_subject(address, transport))
                answers_requests = answers_requests or entry["class"] == "request_reply"
        if role == transport["topology_owner"]:
            publish.add(probe_grant(transport))
            subscribe.add(probe_grant(transport))
        if any("{agent}" in subject for subject in publish | subscribe):
            raise ContractError(f"{role}: generated grants name partitions, not agents (README, \"Partitions\")")
        document: dict[str, Any] = {"role": role, "publish": sorted(publish), "subscribe": sorted(subscribe)}
        if answers_requests:
            document["allow_responses"] = {"max": 1, "expires": f"{reply_window}s"}
        result[role] = document
    if JOINING in result:
        raise ContractError(f"registry.yaml: {JOINING} is the permission set of hosts that hold no role yet")
    joins = [address for entry in registry["kinds"].values() if entry.get("verification") == "proof_of_possession"
             for address in own_addresses(entry)]
    result[JOINING] = {"role": JOINING, "publish": sorted(full_subject(send_pattern(address), "{self}") for address in joins),
                       "subscribe": [f"{transport['reply_inbox_prefix'].replace('{runtime}', '{self}')}.{JOIN_INBOX_TOKEN}.>"]}
    return result


def channel_name(address: str) -> str:
    """An AsyncAPI channel id from an address: its fixed words joined by _ (work.agent.{agent} is work_agent)."""

    return re.sub(r"[^a-z0-9]+", "_", re.sub(r"\{[a-z_]+\}", "", address)).strip("_")


def channel(address: str) -> dict[str, Any]:
    return {
        "address": address,
        "parameters": {name: {"description": f"The {name} subject token."}
                       for name in re.findall(r"\{([a-z_]+)\}", address)},
        "messages": {},
    }


def framed_payload(registry: dict[str, Any], kind: str, body: str) -> dict[str, Any]:
    """A complete frame constrained to this kind, retaining the frame's closed fields."""

    return {"allOf": [
        {"$ref": f"../schemas/{registry['frame_schema']}"},
        {"type": "object", "properties": {"kind": {"const": kind}, "body": {"$ref": f"../schemas/{body}"}}},
    ]}


def asyncapi(registry: dict[str, Any], transport: dict[str, Any]) -> dict[str, Any]:
    """One channel per address, listing every kind sent there; per kind and destination a send and a receive operation.

    Operations are named by the audience target they deliver to (``send_task_result_edge``);
    a durable copy's are ``send_<kind>_copy_<target>`` and carry ``x-ua-copy``. Replies share
    the requester's inbox channel.
    """

    channels: dict[str, Any] = {}
    operations: dict[str, Any] = {}
    messages: dict[str, Any] = {}
    addresses: dict[str, str] = {}
    inbox = "reply_inbox"
    for name, entry in sorted(registry["kinds"].items()):
        key = name.replace(".", "_")
        messages[key] = {"name": name, "summary": entry["description"], "payload": framed_payload(registry, name, entry["body"])}
        reply = None
        if "reply" in entry:
            reply_key = f"{key}_reply"
            messages[reply_key] = {"name": f"{name}.reply", "payload": framed_payload(registry, f"{name}.reply", entry["reply"]["body"])}
            channels.setdefault(inbox, channel(transport["reply_inbox_prefix"] + ".{inbox}"))["messages"][reply_key] = \
                {"$ref": f"#/components/messages/{reply_key}"}
            reply = {"channel": {"$ref": f"#/channels/{inbox}"}, "messages": [{"$ref": f"#/channels/{inbox}/messages/{reply_key}"}]}
        for address in entry_addresses(entry):
            channel_id = channel_name(address)
            if addresses.setdefault(channel_id, address) != address:
                raise ContractError(f"addresses {addresses[channel_id]} and {address} make one AsyncAPI channel id")
            channels.setdefault(channel_id, channel(full_subject(address, "{sender}")))["messages"][key] = \
                {"$ref": f"#/components/messages/{key}"}
            operations.update(address_operations(key, entry, address, channel_id, reply))
    return {
        "asyncapi": "3.1.0",
        "info": {"title": "Universal Agents messages", "version": registry["protocol"]},
        "defaultContentType": "application/json",
        "channels": dict(sorted(channels.items())),
        "operations": operations,
        "components": {"messages": messages},
    }


def address_operations(key: str, entry: dict[str, Any], address: str, channel_id: str,
                       reply: dict[str, Any] | None) -> dict[str, Any]:
    """The send and receive operations of one kind at one of its addresses; a copy's are marked."""

    is_copy = address in entry.get("copy_to", [])
    suffix = f"{key}_{'copy_' if is_copy else ''}{audience_target(address)}"
    operations = {}
    for action in ("send", "receive"):
        operation: dict[str, Any] = {"action": action, "channel": {"$ref": f"#/channels/{channel_id}"},
                                     "messages": [{"$ref": f"#/channels/{channel_id}/messages/{key}"}]}
        if is_copy:
            operation["summary"] = "A durable copy for the team services (README, \"Durable copies\")."
            operation["x-ua-copy"] = True
        if reply is not None:
            operation["reply"] = reply
        operations[f"{action}_{suffix}"] = operation
    return operations


# The kinds of file the contract's sources are; the digest covers only these (README, "Versions").
SOURCE_SUFFIXES = frozenset({".md", ".yaml", ".json"})


def contract_sources(root: Path = CONTRACT) -> list[Path]:
    """The contract's source files under ``root``: its Markdown, YAML and JSON files outside generated/.

    A hidden file or folder (a name starting with ".") and any other kind of file, such
    as an editor's backup or a desktop's folder index, is no source, so it never
    changes the digest.
    """

    generated = root / GENERATED.relative_to(CONTRACT)
    return sorted(path for path in root.rglob("*")
                  if path.is_file() and path.suffix in SOURCE_SUFFIXES and generated not in path.parents
                  and not any(part.startswith(".") for part in path.relative_to(root).parts))


def contract_sha256(files: dict[Path, str], root: Path = CONTRACT) -> str:
    """The digest of the contract's sources (README, "Versions"): its source files, see ``contract_sources``.

    In order of their paths relative to the folder, compared as UTF-8 bytes, it hashes
    each path in UTF-8, a zero byte, the file's size in decimal digits, a zero byte and
    the file's bytes. Examples and vectors count as this run renders them, so the digest
    never depends on a stale signature.
    """

    generated = root / GENERATED.relative_to(CONTRACT)
    paths = set(contract_sources(root))
    paths |= {path for path in files if root in path.parents and generated not in path.parents}
    digest = hashlib.sha256()
    for path in sorted(paths, key=lambda item: item.relative_to(root).as_posix().encode("utf-8")):
        data = files[path].encode("utf-8") if path in files else path.read_bytes()
        digest.update(path.relative_to(root).as_posix().encode("utf-8") + b"\0" + str(len(data)).encode("ascii")
                      + b"\0" + data)
    return digest.hexdigest()


def compatibility(registry: dict[str, Any], digest: str) -> dict[str, Any]:
    label = registry["protocol"]
    major, rest = label.split(".", 1)
    return {
        "protocol": label,
        "contract_sha256": digest,
        "accepts": {"major": int(major), "label": rest.split("-", 1)[1] if "-" in rest else None,
                    "minors": "any; a newer minor's unknown optional fields are ignored",
                    "contract_sha256": "the same, while the label ends in -pre"},
        "signing_domain": registry["signing_domain"],
        "state_signing_domain": registry["state_signing_domain"],
        "authority_token_domain": registry["authority_token_domain"],
        "kinds": sorted(registry["kinds"]),
        "state_records": sorted(registry["state_records"]),
    }


# -- generated Python ------------------------------------------------------------------------------

def class_name(text: str) -> str:
    return "".join(part[:1].upper() + part[1:] for part in re.split(r"[._\-]", text) if part)


class ModelWriter:
    """Render Pydantic models for the schema subset."""

    def __init__(self, schemas: dict[str, dict[str, Any]]) -> None:
        self.schemas = schemas
        self.blocks: list[str] = []
        self.names: dict[tuple[str, str], str] = {}
        self.defined: set[str] = {"WireModel"}

    def define(self, name: str, where: str) -> None:
        """Claim a generated name; a second schema with the same name would silently replace the first."""

        if name in self.defined:
            raise ContractError(f"{where}: generates the name {name}, which another schema already uses")
        self.defined.add(name)

    def resolve(self, current: str, ref: str) -> tuple[str, str]:
        file, _, fragment = ref.partition("#")
        target = current if not file else (Path(current).parent / file).as_posix()
        target = str(Path(target)).replace("\\", "/")
        parts = [part for part in target.split("/") if part]
        normalized: list[str] = []
        for part in parts:
            if part == "..":
                normalized.pop()
            elif part != ".":
                normalized.append(part)
        return "/".join(normalized), fragment

    def node(self, file: str, pointer: str) -> dict[str, Any]:
        node: Any = self.schemas[file]
        for part in [p for p in pointer.split("/") if p]:
            node = node[part]
        if not isinstance(node, dict):
            raise ContractError(f"{file}#{pointer}: a reference must name a schema object")
        return node

    def type_of(self, file: str, schema: dict[str, Any], hint: str) -> str:
        if "$ref" in schema:
            target_file, fragment = self.resolve(file, schema["$ref"])
            return self.named(target_file, fragment)
        if "const" in schema:
            return literal([schema["const"]])
        if "enum" in schema:
            return literal(schema["enum"])
        if "oneOf" in schema:
            return self.union(file, schema, hint)
        kind = schema.get("type")
        if kind == "string":
            return self.annotated("str", "StringConstraints", schema, {"pattern": "pattern", "minLength": "min_length", "maxLength": "max_length"})
        if kind == "integer":
            return self.annotated("int", "Field", schema, {"minimum": "ge", "maximum": "le"})
        if kind == "boolean":
            return "bool"
        if kind == "array":
            item = self.type_of(file, schema["items"], hint + "Item")
            return self.annotated(f"list[{item}]", "Field", schema, {"minItems": "min_length", "maxItems": "max_length"})
        if kind == "object":
            if "properties" not in schema:
                return "dict[str, Any]"
            return self.model(file, schema, hint)
        raise ContractError(f"{file}: cannot render {schema}")

    @staticmethod
    def annotated(base: str, constraint: str, schema: dict[str, Any], keys: dict[str, str]) -> str:
        arguments = [f"{python}={schema[json_key]!r}" for json_key, python in keys.items() if json_key in schema]
        return f"Annotated[{base}, {constraint}({', '.join(arguments)})]" if arguments else base

    def named(self, file: str, fragment: str) -> str:
        key = (file, fragment)
        if key in self.names:
            return self.names[key]
        schema = self.node(file, fragment)
        name = class_name(fragment.rsplit("/", 1)[-1]) if fragment else class_name(Path(file).name.removesuffix(".schema.json"))
        self.names[key] = name
        rendered = self.type_of(file, schema, name)
        if file == "common.schema.json" and fragment == "/$defs/timestamp":
            rendered = f"Annotated[{rendered}, AfterValidator(validate_timestamp)]"
        if rendered != name:
            self.define(name, f"{file}#{fragment}")
            self.blocks.append(f"{name} = {rendered}\n")
        return name

    def model(self, file: str, schema: dict[str, Any], name: str) -> str:
        self.define(name, file)
        required = set(schema.get("required", []))
        lines = [f"class {name}(WireModel):"]
        if schema.get("description"):
            lines.append(f"    {json.dumps(schema['description'], ensure_ascii=False)}")
            lines.append("")
        for prop, prop_schema in schema["properties"].items():
            attribute = prop + "_" if keyword.iskeyword(prop) else prop
            prop_type = self.type_of(file, prop_schema, name + class_name(prop))
            alias = f", alias={prop!r}" if attribute != prop else ""
            if prop in required:
                lines.append(f"    {attribute}: {prop_type}" + (f" = Field({alias[2:]})" if alias else ""))
            else:
                lines.append(f"    {attribute}: Union[{prop_type}, MISSING] = Field(default=MISSING{alias})")
        self.blocks.append("\n".join(lines) + "\n")
        return name

    def union(self, file: str, schema: dict[str, Any], name: str) -> str:
        key = discriminator(file, "", schema)
        members = []
        for variant in schema["oneOf"]:
            value = variant["properties"][key]["const"]
            members.append(self.type_of(file, variant, name + class_name(str(value))))
        return f"Annotated[Union[{', '.join(members)}], Field(discriminator={key!r})]"


def shape(writer: ModelWriter, file: str, schema: dict[str, Any]) -> Any:
    """The structure the pruning step walks: which object keys exist, by alternative."""

    if "$ref" in schema:
        target_file, fragment = writer.resolve(file, schema["$ref"])
        return shape(writer, target_file, writer.node(target_file, fragment))
    if "oneOf" in schema:
        key = discriminator(file, "", schema)
        return {"one_of": key, "variants": {str(v["properties"][key]["const"]): shape(writer, file, v) for v in schema["oneOf"]}}
    if schema.get("type") == "object" and "properties" in schema:
        return {"properties": {prop: shape(writer, file, value) for prop, value in schema["properties"].items()}}
    if schema.get("type") == "array":
        return {"items": shape(writer, file, schema["items"])}
    return None


def binding_spec(entry: dict[str, Any]) -> tuple[tuple[str, str | None, tuple[str, ...] | None], ...]:
    """Each token type the kind takes, as the verifier reads it: (type, body pointer or None, senders or None).

    The generated ``KindSpec.authority_binding`` holds these as ``TokenBinding``
    values; it is empty for a kind that takes no token.
    """

    return tuple((binding["type"], binding.get("at"), tuple(binding["senders"]) if "senders" in binding else None)
                 for binding in token_bindings(entry))


def binding_text(entry: dict[str, Any]) -> str:
    """``binding_spec`` as Python source for kinds.py."""

    items = [f"TokenBinding(type={kind!r}, at={at!r}, senders={senders!r})" for kind, at, senders in binding_spec(entry)]
    return "(" + ", ".join(items) + ("," if len(items) == 1 else "") + ")"


def render_python(registry: dict[str, Any], schemas: dict[str, dict[str, Any]], transport: dict[str, Any],
                  topologies: dict[str, Any], digest: str) -> dict[Path, str]:
    """models.py and kinds.py; ``digest`` is the contract's own digest, which kinds.py names (README, "Versions")."""

    writer = ModelWriter(schemas)
    for fragment in schemas["common.schema.json"]["$defs"]:
        writer.named("common.schema.json", f"/$defs/{fragment}")
    writer.named("frame.schema.json", "")
    writer.named(registry["state_schema"], "")
    states = [(name, entry, writer.type_of(entry["body"], schemas[entry["body"]], class_name(name) + "State"))
              for name, entry in sorted(registry["state_records"].items())]
    for _name, entry, body in states:
        writer.names[(entry["body"], "")] = body
    kinds = []
    for name, entry in sorted(registry["kinds"].items()):
        body = writer.type_of(f"bodies/{Path(entry['body']).name}", schemas[entry["body"]], class_name(name) + "Body")
        reply = None
        if "reply" in entry:
            reply = writer.type_of(f"bodies/{Path(entry['reply']['body']).name}", schemas[entry["reply"]["body"]], class_name(name) + "Reply")
        kinds.append((name, entry, body, reply))
    header = '"""Generated by scripts/export_message_contract.py from contracts/messages/v1. Do not edit."""\n\n'
    models = header + (
        "from __future__ import annotations\n\n"
        "from typing import Annotated, Any, Literal, Union\n\n"
        "from pydantic import MISSING, AfterValidator, BaseModel, BeforeValidator, ConfigDict, Field, StringConstraints, TypeAdapter\n\n"
        "from custom_messages.wire.values import exact_type, validate_timestamp\n\n\n"
        "class WireModel(BaseModel):\n"
        "    \"\"\"Strict, immutable and closed to unknown fields; fields are read only under their wire names.\"\"\"\n\n"
        "    model_config = ConfigDict(extra=\"forbid\", frozen=True, strict=True)\n\n\n"
    )
    models += "\n\n".join(writer.blocks)
    adapters = []
    for name, _entry, body, reply in kinds:
        adapters.append(f"{class_name(name)}BodyAdapter = TypeAdapter({body})")
        if reply:
            adapters.append(f"{class_name(name)}ReplyAdapter = TypeAdapter({reply})")
    for name, _entry, body in states:
        adapters.append(f"{class_name(name)}StateAdapter = TypeAdapter({body})")
    models += "\n\n" + "\n".join(adapters) + "\n"

    specs = []
    for name, entry, _body, reply in kinds:
        body_file = entry["body"]
        reply_text = "None"
        reply_shape = "None"
        if reply:
            reply_text = f"models.{class_name(name)}ReplyAdapter"
            reply_shape = repr(shape(writer, entry["reply"]["body"], schemas[entry["reply"]["body"]]))
        specs.append(
            f"    {name!r}: KindSpec(\n"
            f"        name={name!r},\n"
            f"        delivery_class={entry['class']!r},\n"
            f"        addresses={ {audience_target(address): address for address in entry_addresses(entry)}!r},\n"
            f"        copies={tuple(audience_target(address) for address in entry.get('copy_to', []))!r},\n"
            f"        idempotency={tuple(entry['idempotency'])!r},\n"
            f"        body_model=models.{class_name(name)}BodyAdapter,\n"
            f"        reply_model={reply_text},\n"
            f"        reply_errors={tuple(entry.get('reply', {}).get('errors', ()))!r},\n"
            f"        senders={tuple(entry['senders'])!r},\n"
            f"        senders_by_target={ {audience_target(a): tuple(s) for a, s in entry.get('senders_by_address', {}).items()}!r},\n"
            f"        receivers={tuple(entry['receivers'])!r},\n"
            f"        expiry_seconds={entry['expiry_seconds']!r},\n"
            f"        authority={entry['authority']!r},\n"
            f"        authority_by_target={ {audience_target(a): v for a, v in entry.get('authority_by_address', {}).items()}!r},\n"
            f"        authority_binding={binding_text(entry)},\n"
            f"        body_rules={tuple(entry.get('body_rules', ()))!r},\n"
            f"        execution_slot={entry['admission']['execution_slot']!r},\n"
            f"        accepted_while_paused={entry['admission']['accepted_while_paused']!r},\n"
            f"        priority={entry['admission']['priority']!r},\n"
            f"        routing={entry['routing']!r},\n"
            f"        topology_edges={tuple(topology_edges(entry))!r},\n"
            f"        ledger={entry['ledger']!r},\n"
            f"        wakes_model={entry['wakes_model']!r},\n"
            f"        rendering={entry['rendering']!r},\n"
            f"        body_shape={shape(writer, body_file, schemas[body_file])!r},\n"
            f"        reply_shape={reply_shape},\n"
            f"        verification={entry.get('verification', 'standard')!r},\n"
            "    ),"
        )
    state_specs = [
        f"    {name!r}: StateSpec(name={name!r}, bucket={entry['bucket']!r}, key={entry['key']!r}, "
        f"signer={entry['signer']!r}, signer_roles={tuple(entry.get('signer_roles', ()))!r}, "
        f"writers={tuple(entry['writers'])!r}, ttl_seconds={entry.get('ttl_seconds')!r}, "
        f"max_lifetime_seconds={entry.get('max_lifetime_seconds')!r}, fresh_seconds={entry.get('fresh_seconds')!r}, "
        f"body_model=models.{class_name(name)}StateAdapter, "
        f"body_shape={shape(writer, entry['body'], schemas[entry['body']])!r}),"
        for name, entry, _body in states
    ]
    between = sorted(name for name, spec in transport["classes"].items() if spec["between_installations"])
    kinds_text = header + (
        "from __future__ import annotations\n\n"
        "from custom_messages.wire import models\n"
        "from custom_messages.wire.spec import KindSpec, StateSpec, TokenBinding\n\n"
        f"PROTOCOL = {registry['protocol']!r}\n"
        "# The digest of the contract these files were generated from, which a host compares with a\n"
        "# peer's while the label is a pre-release (README, \"Versions\"). Generated files are not\n"
        "# contract sources, so naming it here does not change it.\n"
        f"CONTRACT_SHA256 = {digest!r}\n"
        f"SIGNING_DOMAIN = {registry['signing_domain']!r}\n"
        f"STATE_SIGNING_DOMAIN = {registry['state_signing_domain']!r}\n"
        f"AUTHORITY_TOKEN_DOMAIN = {registry['authority_token_domain']!r}\n"
        f"RENEW_KEY_PROOF_DOMAIN = {registry['renew_key_proof_domain']!r}\n"
        f"AUTHORITY_TOKEN_MAX_SECONDS = {registry['authority_token_max_seconds']!r}\n"
        f"COMMAND_TOKEN_MAX_SECONDS = {registry['command_token_max_seconds']!r}\n"
        f"CERTIFICATE_OVERLAP_SECONDS = {registry['certificate_overlap_seconds']!r}\n"
        f"MAX_FRAME_BYTES = {registry['max_frame_bytes']!r}\n"
        f"MAX_HEADER_BYTES = {transport['limits']['max_header_bytes']!r}\n"
        f"MAX_NESTING_DEPTH = {registry['max_nesting_depth']!r}\n"
        f"CLOCK_SKEW_SECONDS = {transport['clock_skew_seconds']!r}\n"
        f"DEFAULT_PARTITION_COUNT = {transport['partitions']['default_count']!r}\n"
        f"MAX_PARTITION_COUNT = {transport['partitions']['max_count']!r}\n"
        f"PARTITION_COUNT_TOKEN = {PARTITION_COUNT_TOKEN!r}\n"
        f"PARTITION_ACCOUNT_MAPPINGS = {dict(sorted(transport['partitions']['account_mappings'].items()))!r}\n"
        f"PARTITION_PROBE_SCOPE = {transport['partitions']['probe_scope']!r}\n"
        f"SINGLE_TEAM_ROLES = {tuple(transport['single_team_roles'])!r}\n"
        f"PARTITIONED_ADDRESSES = {tuple(sorted(partitioned_addresses(transport)))!r}\n"
        f"REPLAY_LEDGER_RETENTION_SECONDS = {transport['replay_ledger_retention_seconds']!r}\n"
        f"SUBJECT_PREFIX = {transport['subject_prefix']!r}\n"
        f"PEER_SENDER_SLOT = {transport['peer_sender_slot']!r}\n"
        f"REPLY_INBOX_PREFIX = {transport['reply_inbox_prefix']!r}\n"
        f"INSTALLATION_SCOPE = {INSTALLATION_SCOPE!r}\n"
        f"INSTALLATION_ADDRESS_PREFIX = {INSTALLATION_ADDRESS!r}\n"
        f"BETWEEN_INSTALLATIONS = frozenset({between!r})\n"
        f"VERIFICATION_REFUSALS = {tuple(item['code'] for item in registry['verification_refusals'])!r}\n"
        f"TEMPORARY_REFUSALS = frozenset({sorted(item['code'] for item in registry['verification_refusals'] if item.get('temporary'))!r})\n"
        f"CHANGE_REFUSALS = {tuple(item['code'] for item in registry['change_refusals'])!r}\n"
        f"COVERED_RECORD_KINDS = {tuple(registry['covered_record_kinds'])!r}\n"
        f"RETRY_OUTCOMES = frozenset({sorted(registry['retry_outcomes'])!r})\n"
        f"HOST_ROLES = {tuple(registry['host_roles'])!r}\n"
        f"TOPOLOGIES = { {name: {role: tuple(edges) for role, edges in spec['allowed_edges'].items()} for name, spec in topologies['topologies'].items()}!r}\n"
        f"REQUESTER_KINDS = frozenset({sorted(topologies.get('requester_kinds', []))!r})\n"
        f"FRAME_SHAPE = {shape(writer, 'frame.schema.json', schemas['frame.schema.json'])!r}\n"
        f"STATE_SHAPE = {shape(writer, registry['state_schema'], schemas[registry['state_schema']])!r}\n\n"
        "KINDS: dict[str, KindSpec] = {\n" + "\n".join(specs) + "\n}\n\n"
        "STATE_RECORDS: dict[str, StateSpec] = {\n" + "\n".join(state_specs) + "\n}\n"
    )
    return {WIRE / "models.py": models, WIRE / "kinds.py": kinds_text}


def render() -> dict[Path, str]:
    registry, transport, topologies = load_yaml("registry.yaml"), load_yaml("transport.yaml"), load_yaml("team-topologies.yaml")
    schemas = load_schemas()
    if registry["max_frame_bytes"] != transport["limits"]["max_frame_bytes"]:
        raise ContractError("registry.yaml and transport.yaml disagree on the frame size limit")
    check_transport(transport)
    check_message_sizes(transport, schemas)
    check_consumer_names(transport)
    check_registry(registry, transport, topologies, schemas)
    check_stream_management(registry, transport)
    # After every other check: it builds each role's permissions, which needs a whole transport.
    check_single_team_roles(registry, transport)
    files: dict[Path, str] = {
        GENERATED / "registry.json": json_text(registry),
        GENERATED / "transport.json": json_text(transport),
        GENERATED / "team-topologies.json": json_text(topologies),
        GENERATED / "asyncapi.json": json_text(asyncapi(registry, transport)),
    }
    for role, document in permissions(registry, transport).items():
        files[GENERATED / "permissions" / f"{role}.json"] = json_text(document)
    # Examples are sources whose digests and signatures this run fills in; the
    # contract digest and the manifest record them as rendered. The Python files
    # name the digest, and lie outside the sources, so they follow it.
    files.update(render_examples(registry))
    digest = contract_sha256(files)
    files.update(render_python(registry, schemas, transport, topologies, digest))
    files[GENERATED / "compatibility.json"] = json_text(compatibility(registry, digest))
    sources = contract_sources()
    entries = {path.relative_to(REPO_ROOT).as_posix(): hashlib.sha256(path.read_bytes()).hexdigest() for path in sources}
    entries.update({path.relative_to(REPO_ROOT).as_posix(): hashlib.sha256(text.encode("utf-8")).hexdigest()
                    for path, text in files.items()})
    files[GENERATED / "manifest.json"] = json_text({"protocol": registry["protocol"], "files": entries})
    return files


def stale(files: dict[Path, str], generated: Path = GENERATED) -> list[str]:
    """Generated files that differ from what the sources produce, byte for byte, and stray files.

    Bytes, not text: reading text would turn CRLF line endings back into LF, so a
    rewritten file would pass while its digest no longer matched the manifest.
    """

    def shown(path: Path) -> str:
        return path.relative_to(REPO_ROOT).as_posix() if path.is_relative_to(REPO_ROOT) else str(path)

    problems = [shown(path) for path, text in files.items()
                if not path.is_file() or path.read_bytes() != text.encode("utf-8")]
    for path in sorted(generated.rglob("*")):
        if path.is_file() and path not in files:
            problems.append(f"{shown(path)} (not generated)")
    return problems


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--check", action="store_true", help="fail instead of writing when generated files differ")
    args = parser.parse_args()
    files = render()
    if args.check:
        problems = stale(files)
        if problems:
            print("Message contract files are stale; run scripts/export_message_contract.py:", file=sys.stderr)
            for problem in problems:
                print(f"  {problem}", file=sys.stderr)
            return 1
        return 0
    for path in sorted(GENERATED.rglob("*")):
        if path.is_file() and path not in files:
            path.unlink()
    for path, text in files.items():
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(text.encode("utf-8"))
    return 0


if __name__ == "__main__":
    sys.exit(main())
