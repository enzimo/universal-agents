#!/usr/bin/env python3
"""Generate, or check, the files derived from the protocol 1 message contract.

Sources (written by hand) are under contracts/messages/v1: registry.yaml,
transport.yaml, team-topologies.yaml and schemas/. This script validates them and
writes:

- contracts/messages/v1/generated/: JSON copies of the YAML files, per-role NATS
  permissions, the AsyncAPI 3.1 document, the compatibility record and a digest
  manifest of every source and generated file;
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
AUTHORITY = {"none", "optional", "required"}
ROUTING = {"requester", "holder", "team", "person", "named", "broadcast", "sender"}
EDGES = {"up", "down", "person", "sideways", "out", "none"}
WAKES = {"never", "always", "next_turn"}
RENDERING = {"none", "sender_line", "summary", "full"}
PRIORITIES = {"low", "normal", "high"}
ENTRY_FIELDS = {
    "description", "class", "address", "body", "reply", "senders", "receivers", "expiry_seconds",
    "authority", "idempotency", "admission", "routing", "topology_edge", "ledger", "wakes_model",
    "rendering", "covered_change", "copy_to", "senders_by_address", "authority_binding", "body_rules", "verification",
}
REQUIRED_ENTRY_FIELDS = ENTRY_FIELDS - {"reply", "covered_change", "copy_to", "senders_by_address", "authority_binding",
                                        "body_rules", "verification"}

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
# Characters an id may hold (common.schema.json token_id). Consumer names join ids
# with a character outside this set.
ID_CHARACTER = re.compile(r"[A-Za-z0-9_-]")
# Address placeholders that name the receiver. A sender may address any receiver in
# its team; a receiver reads only its own, so subscriptions and consumer grants keep
# them as placeholders for membership code to fill per assigned id.
RECEIVER_PLACEHOLDERS = ("agent", "pool", "edge")
# Other address placeholders: a dead-letter copy id (one subject token the sender
# chooses), and the sender of an installation address (_install.{sender}.svc.<op>).
COPY_PLACEHOLDER, SENDER_PLACEHOLDER = "copy", "sender"
INSTALLATION_ADDRESS = "_install.{sender}."
# Hosts that send frames for agents; a kind no agent sends is not a topology check.
AGENT_HOSTS = {"lead_host", "worker_host"}
# Audience targets that get durable copies: the team services record custody facts.
COPY_TARGETS = {"team_services"}
# A kind (with ".reply") within 84 characters keeps its idempotency key, the kind,
# a colon and 43 digest characters, within the 128 that common.schema.json allows.
MAX_KIND_LENGTH = 84
# The frame's and the state record's bodies are the open objects: the schema their
# kind or record names checks them.
OPEN_OBJECTS = {("frame.schema.json", "/properties/body"), ("state.schema.json", "/properties/body")}
STATE_FIELDS = {"description", "bucket", "key", "signer", "signer_roles", "writers", "body", "ttl_seconds", "write_rule"}
REQUIRED_STATE_FIELDS = {"description", "bucket", "key", "signer", "writers", "body", "write_rule"}
SIGNERS = {"authority", "host"}


class ContractError(ValueError):
    pass


def load_yaml(name: str) -> dict[str, Any]:
    data = yaml.safe_load((CONTRACT / name).read_text(encoding="utf-8"))
    if not isinstance(data, dict):
        raise ContractError(f"{name} must hold a mapping")
    return data


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
    ``(?:``, alternatives inside a group, and quantifiers. ``$`` means the end of
    the value; Python's ``re`` also matches it before a final newline, which the
    README tells Python validators to refuse.
    """

    if len(pattern) < 2 or not (pattern.startswith("^") and pattern.endswith("$")) or pattern.endswith("\\$"):
        return "anchor the whole pattern with ^ and $"
    body, depth, in_class, index = pattern[1:-1], 0, False, 0
    while index < len(body):
        character, following = body[index], body[index + 1:index + 2]
        if character == "\\":
            if following not in PORTABLE_ESCAPES:
                return f"\\{following} reads differently between engines; list the characters instead"
            index += 2
            continue
        if in_class:
            if character == "[":
                return "a [ inside a character class, such as [[:alpha:]], reads differently between engines"
            in_class = character != "]"
        elif character == "[":
            in_class = True
        elif character == "(":
            if following == "?" and body[index + 2:index + 3] != ":":
                return "group extensions other than (?: differ between engines"
            depth += 1
        elif character == ")":
            depth -= 1
        elif character == "|" and depth == 0:
            return "group top-level alternatives, or ^ and $ apply to only one of them"
        elif character == ".":
            return "an unescaped . matches different characters in different engines"
        elif character in "^$":
            return "anchors belong only at the two ends"
        elif character in "*+?}" and following == "+":
            return "possessive quantifiers are not portable"
        index += 1
    return "a character class is not closed" if in_class else None


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
    if len(set(values)) != len(values):
        raise ContractError(f"{name}{pointer}: oneOf const values must differ")
    return key


def check_registry(registry: dict[str, Any], transport: dict[str, Any], topologies: dict[str, Any],
                   schemas: dict[str, dict[str, Any]]) -> None:
    check_state_records(registry, transport, schemas)
    check_shared_lists(registry, transport, topologies, schemas)
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
        check_idempotency(where, name, entry, schemas, registry["frame_schema"])
        check_body_rules(where, entry, schemas)
        check_wake_alternatives(where, entry, schemas)
    names: dict[str, str] = {}
    for name in registry["kinds"]:
        # Generated model and adapter names come from the kind name; two kinds
        # with one name would silently share the second kind's schema.
        other = names.setdefault(class_name(name), name)
        if other != name:
            raise ContractError(f"registry.yaml: kinds {other} and {name} generate the same model names")


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
        "class": (entry["class"], CLASSES), "authority": (entry["authority"], AUTHORITY),
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
    check_authority_binding(where, entry, schemas)
    if entry.get("verification", "standard") not in VERIFICATION:
        raise ContractError(f"{where}: verification must be one of {sorted(VERIFICATION)}")
    if entry.get("verification") == "proof_of_possession" and entry["class"] != "request_reply":
        raise ContractError(f"{where}: only an installation request proves possession of its own key")


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


def check_authority_binding(where: str, entry: dict[str, Any], schemas: dict[str, dict[str, Any]]) -> None:
    """A kind that takes a token says what the token is bound to; with ``at``, the body field holding that id."""

    binding = entry.get("authority_binding")
    if (binding is None) != (entry["authority"] == "none"):
        raise ContractError(f"{where}: kinds that take an authority token, and only they, state authority_binding")
    if binding is None:
        return
    types = schemas["common.schema.json"]["$defs"]["authority_token"]["properties"]["bound_to"]["properties"]["type"]["enum"]
    named = binding.get("type") if isinstance(binding, dict) else None
    named = named if isinstance(named, list) else [named]
    if not isinstance(binding, dict) or set(binding) - {"type", "at"} or not named or not set(named) <= set(types) \
            or ("at" in binding and len(named) != 1):
        raise ContractError(f"{where}: authority_binding is {{type, at}} with type one of {types}; a list of types "
                            "only when the handler checks the binding")
    if "at" in binding:
        steps = binding["at"].split("/")
        leaves = schema_leaves(ModelWriter(schemas), entry["body"], schemas[entry["body"]], steps[2:]) \
            if binding["at"].startswith("/body/") else []
        if not leaves or not all(scalar_schema(ModelWriter(schemas), file, leaf) for file, leaf in leaves) \
                or not always_supplied(entry, schemas, "frame.schema.json", steps[1:]):
            raise ContractError(f"{where}: authority_binding at {binding['at']} names no field every body carries")


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

    for address in entry_addresses(entry):
        target = audience_target(address)
        if not receiving_roles(entry, address, transport):
            raise ContractError(f"{where}: none of its receivers serves {target} audiences, so nobody receives {address}")
    own_targets = {audience_target(address) for address in own_addresses(entry)}
    idle = [role for role in entry["receivers"]
            if not any(role in transport["audience_targets"][target] for target in own_targets)]
    # Dead-letter readers read the stream with their own grants, not through one address.
    if idle and entry["class"] != "dead_letter":
        raise ContractError(f"{where}: receivers {idle} serve none of its addresses")
    if entry.get("copy_to") and entry["class"] != "durable":
        raise ContractError(f"{where}: only durable kinds are copied to other inboxes")
    if any(audience_target(address) not in COPY_TARGETS for address in entry.get("copy_to", [])):
        raise ContractError(f"{where}: copies go only to {sorted(COPY_TARGETS)} inboxes")
    for address, senders in entry.get("senders_by_address", {}).items():
        if address not in own_addresses(entry) or not senders or not set(senders) <= set(entry["senders"]):
            raise ContractError(f"{where}: senders_by_address narrows the senders of one of its own addresses")


def own_addresses(entry: dict[str, Any]) -> list[str]:
    """The addresses a kind is delivered to as itself, without its copies."""

    return list(entry["address"]) if isinstance(entry["address"], list) else [entry["address"]]


def full_subject(address: str, owner: str) -> str:
    """The NATS subject of an address: team addresses sit under ua.v1.<team>.<sender>, installation ones under ua.v1._install."""

    if address.startswith("_install."):
        return "ua.v1." + address.replace("{sender}", owner)
    return f"ua.v1.{{team}}.{owner}." + address


def entry_addresses(entry: dict[str, Any]) -> list[str]:
    """Every address a kind is published to: its own, then the copies (``copy_to``)."""

    return own_addresses(entry) + list(entry.get("copy_to", []))


def receiving_roles(entry: dict[str, Any], address: str, transport: dict[str, Any]) -> list[str]:
    """The roles that receive the kind at this address (transport.yaml audience_targets).

    At its own addresses, the kind's receivers that serve the address's audience
    target: a role gets permissions only for the addresses it can serve. A copy
    goes to every role serving the copy's target (README, "Durable copies"), which
    is not a receiver of the kind itself: the team services record a task's custody
    from its copy without the kind's slot, wake or rendering rules.
    """

    serving = transport["audience_targets"][audience_target(address)]
    if address in entry.get("copy_to", []):
        return list(serving)
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


class BodyFields:
    """Looks up a kind's body fields by JSON pointer, for checking its body rules."""

    def __init__(self, entry: dict[str, Any], schemas: dict[str, dict[str, Any]]) -> None:
        self.resolver = ModelWriter(schemas)
        self.file, self.root = entry["body"], schemas[entry["body"]]

    def leaves(self, pointer: Any) -> list[tuple[str, dict[str, Any]]]:
        if not isinstance(pointer, str) or not pointer.startswith("/body/"):
            return []
        return schema_leaves(self.resolver, self.file, self.root, pointer.split("/")[2:])

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


def unique_rule_valid(rule: dict[str, Any], fields: BodyFields) -> bool:
    return set(rule) <= {"unique", "by"} and fields.defined(rule.get("unique"), "array") \
        and ("by" not in rule or fields.items_have(rule["unique"], rule["by"]))


def requires_rule_valid(rule: dict[str, Any], fields: BodyFields) -> bool:
    return set(rule) == {"requires", "when"} and fields.defined(rule["requires"]) and bool(rule["when"]) \
        and all(set(c) == {"at", "is"} and fields.defined(c["at"]) and fields.fits(c["at"], c["is"]) for c in rule["when"])


def any_of_rule_valid(rule: dict[str, Any], fields: BodyFields) -> bool:
    return set(rule) == {"any_of"} and len(rule["any_of"]) > 1 and all(fields.defined(p) for p in rule["any_of"])


def check_body_rules(where: str, entry: dict[str, Any], schemas: dict[str, dict[str, Any]]) -> None:
    """Each rule is one of the three README shapes and names fields its body defines, with values they can hold."""

    fields = BodyFields(entry, schemas)
    for rule in entry.get("body_rules", []):
        if not (unique_rule_valid(rule, fields) or requires_rule_valid(rule, fields) or any_of_rule_valid(rule, fields)):
            raise ContractError(f"{where}: body rule {rule} is not a unique, requires or any_of rule over its body's fields")


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


def check_shared_lists(registry: dict[str, Any], transport: dict[str, Any], topologies: dict[str, Any],
                       schemas: dict[str, dict[str, Any]]) -> None:
    """Lists the schemas repeat must match the registry and topologies they come from."""

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


def audience_target(address: str) -> str:
    """The audience target an address delivers to (README, "Delivery")."""

    for placeholder in RECEIVER_PLACEHOLDERS:
        if "{" + placeholder + "}" in address:
            return placeholder
    if address.startswith("evt."):
        return "team"
    if address == "work.team" or address.startswith("svc.team."):
        return "team_services"
    if address.startswith("dlq."):
        return "team_services"
    if address.startswith("_install."):
        return "installation_service"
    raise ContractError(f"address {address!r} names no audience target")


def check_consumer_names(transport: dict[str, Any]) -> None:
    """Different ids must never produce one consumer name.

    NATS silently updates an existing consumer's filter when a consumer with the
    same name is created, so one team could take another's work. Each name starts
    with a fixed word that ends in a character ids cannot contain, separates its
    ids the same way, and uses exactly its filter's placeholders.
    """

    prefixes = []
    for key, consumer in transport["consumers"].items():
        where = f"transport.yaml consumers.{key}"
        parts = re.split(r"(\{[a-z_]+\})", consumer["name"])
        literals, placeholders = parts[::2], parts[1::2]
        if sorted(placeholders) != sorted(re.findall(r"\{[a-z_]+\}", consumer["filter"])):
            raise ContractError(f"{where}: the name must use exactly the filter's placeholders")
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

    skew = transport["clock_skew_seconds"]
    if not isinstance(skew, int) or not 0 <= skew <= 300:
        raise ContractError("transport.yaml: clock_skew_seconds is a whole number of seconds from 0 to 300")
    longest = max(spec["max_expiry_seconds"] for spec in transport["classes"].values())
    if transport["replay_ledger_retention_seconds"] < longest + skew:
        raise ContractError("transport.yaml: replay records must outlive the longest class expiry plus the clock tolerance")
    for name, spec in transport["classes"].items():
        if not isinstance(spec.get("between_installations"), bool):
            raise ContractError(f"transport.yaml: class {name} states between_installations")
        if "stream" not in spec:
            continue
        stream = transport["streams"].get(spec["stream"])
        if stream is None:
            raise ContractError(f"transport.yaml: class {name} names an unknown stream {spec['stream']}")
        if stream.get("max_age_seconds", 0) < spec["max_expiry_seconds"] + skew:
            raise ContractError(f"transport.yaml: stream {spec['stream']} must keep messages for class {name}'s "
                                "max_expiry_seconds plus the clock tolerance")


def class_addresses(transport: dict[str, Any], delivery_class: str) -> list[str]:
    spec = transport["classes"][delivery_class]
    return list(spec["addresses"]) + list(spec.get("installation_addresses", []))


def address_matches(address: str, template: str, kind: str) -> bool:
    operation = kind.split(".", 1)[1] if kind.startswith("agent.") else kind
    expected = template.replace("{operation}", operation).replace("{event}", kind.replace(".", "_"))
    return address == expected


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
    """Pull, inspect and acknowledge only the consumers for this delivery address.

    Consumer names keep their {team} and receiver placeholders; membership code
    fills them with the host's own team and each id it serves, never with
    wildcards. Creating consumers stays with the topology owner.
    """

    expected_filter = transport["subject_prefix"].replace("{sender}", "*") + "." + address
    matches = [consumer for consumer in transport["consumers"].values() if consumer["filter"] == expected_filter]
    if not matches:
        raise ContractError(f"no pull consumer for {address}")
    subjects = set()
    for consumer in matches:
        stream, name = consumer["stream"], consumer["name"]
        subjects.update({
            f"$JS.API.CONSUMER.INFO.{stream}.{name}",
            f"$JS.API.CONSUMER.MSG.NEXT.{stream}.{name}",
            f"$JS.ACK.{stream}.{name}.>",
        })
    return subjects


def permissions(registry: dict[str, Any], transport: dict[str, Any]) -> dict[str, dict[str, Any]]:
    """Per host role: the NATS permissions it gets, as templates.

    {team} is each team the host serves and {self} its runtime id. {agent},
    {pool} and {edge} are each id the host serves: membership code repeats a
    subject once per id and never turns one into a wildcard, so a host receives
    only its own agents' requests and work. A host publishes only under its own
    id. A receiver of requests answers each one once, within the class's
    expiry, through NATS allow_responses; it holds no grant on other runtimes'
    inboxes.
    """

    reply_window = transport["classes"]["request_reply"]["max_expiry_seconds"] + transport["clock_skew_seconds"]
    result = {}
    for role in registry["host_roles"]:
        publish: set[str] = set()
        subscribe = {f"{transport['reply_inbox_prefix'].replace('{runtime}', '{self}')}.>"}
        answers_requests = False
        for entry in registry["kinds"].values():
            publish.update(full_subject(send_pattern(address), "{self}") for address in publication_addresses(entry, transport)
                           if role in address_senders(entry, address))
            for address in entry_addresses(entry):
                if role not in receiving_roles(entry, address, transport):
                    continue
                if entry["class"] == "durable":
                    publish.update(consumer_permissions(address, transport))
                elif entry["class"] != "dead_letter":  # dead-letter readers get stream grants later
                    subscribe.add(full_subject(address, "*"))
                answers_requests = answers_requests or entry["class"] == "request_reply"
        document: dict[str, Any] = {"role": role, "publish": sorted(publish), "subscribe": sorted(subscribe)}
        if answers_requests:
            document["allow_responses"] = {"max": 1, "expires": f"{reply_window}s"}
        result[role] = document
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


def compatibility(registry: dict[str, Any]) -> dict[str, Any]:
    label = registry["protocol"]
    major, rest = label.split(".", 1)
    return {
        "protocol": label,
        "accepts": {"major": int(major), "label": rest.split("-", 1)[1] if "-" in rest else None,
                    "minors": "any; a newer minor's unknown optional fields are ignored"},
        "signing_domain": registry["signing_domain"],
        "state_signing_domain": registry["state_signing_domain"],
        "authority_token_domain": registry["authority_token_domain"],
        "decision_token_domain": registry["decision_token_domain"],
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
            return f"Literal[{schema['const']!r}]"
        if "enum" in schema:
            return "Literal[" + ", ".join(repr(value) for value in schema["enum"]) + "]"
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


def binding_spec(entry: dict[str, Any]) -> tuple[str, str | None] | None:
    """(type, pointer) for the verifier; several types (gateway commands and agents' root tasks) join with |."""

    binding = entry.get("authority_binding")
    if binding is None:
        return None
    named = binding["type"]
    return ("|".join(named) if isinstance(named, list) else named, binding.get("at"))


def render_python(registry: dict[str, Any], schemas: dict[str, dict[str, Any]], transport: dict[str, Any],
                  topologies: dict[str, Any]) -> dict[Path, str]:
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
        "from pydantic import MISSING, AfterValidator, BaseModel, ConfigDict, Field, StringConstraints, TypeAdapter\n\n"
        "from custom_messages.wire.values import validate_timestamp\n\n\n"
        "class WireModel(BaseModel):\n"
        "    \"\"\"Strict, immutable and closed to unknown fields.\"\"\"\n\n"
        "    model_config = ConfigDict(extra=\"forbid\", frozen=True, strict=True, populate_by_name=True)\n\n\n"
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
            f"        authority_binding={binding_spec(entry)!r},\n"
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
        f"writers={tuple(entry['writers'])!r}, body_model=models.{class_name(name)}StateAdapter),"
        for name, entry, _body in states
    ]
    between = sorted(name for name, spec in transport["classes"].items() if spec["between_installations"])
    kinds_text = header + (
        "from __future__ import annotations\n\n"
        "from custom_messages.wire import models\n"
        "from custom_messages.wire.spec import KindSpec, StateSpec\n\n"
        f"PROTOCOL = {registry['protocol']!r}\n"
        f"SIGNING_DOMAIN = {registry['signing_domain']!r}\n"
        f"STATE_SIGNING_DOMAIN = {registry['state_signing_domain']!r}\n"
        f"AUTHORITY_TOKEN_DOMAIN = {registry['authority_token_domain']!r}\n"
        f"DECISION_TOKEN_DOMAIN = {registry['decision_token_domain']!r}\n"
        f"RENEW_KEY_PROOF_DOMAIN = {registry['renew_key_proof_domain']!r}\n"
        f"AUTHORITY_TOKEN_MAX_SECONDS = {registry['authority_token_max_seconds']!r}\n"
        f"MAX_FRAME_BYTES = {registry['max_frame_bytes']!r}\n"
        f"MAX_NESTING_DEPTH = {registry['max_nesting_depth']!r}\n"
        f"CLOCK_SKEW_SECONDS = {transport['clock_skew_seconds']!r}\n"
        f"SUBJECT_PREFIX = {transport['subject_prefix']!r}\n"
        f"PEER_SENDER_SLOT = {transport['peer_sender_slot']!r}\n"
        f"REPLY_INBOX_PREFIX = {transport['reply_inbox_prefix']!r}\n"
        f"BETWEEN_INSTALLATIONS = frozenset({between!r})\n"
        f"VERIFICATION_REFUSALS = {tuple(item['code'] for item in registry['verification_refusals'])!r}\n"
        f"CHANGE_REFUSALS = {tuple(item['code'] for item in registry['change_refusals'])!r}\n"
        f"HOST_ROLES = {tuple(registry['host_roles'])!r}\n"
        f"TOPOLOGIES = { {name: {role: tuple(edges) for role, edges in spec['allowed_edges'].items()} for name, spec in topologies['topologies'].items()}!r}\n"
        f"FRAME_SHAPE = {shape(writer, 'frame.schema.json', schemas['frame.schema.json'])!r}\n\n"
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
    check_consumer_names(transport)
    check_registry(registry, transport, topologies, schemas)
    files: dict[Path, str] = {
        GENERATED / "registry.json": json_text(registry),
        GENERATED / "transport.json": json_text(transport),
        GENERATED / "team-topologies.json": json_text(topologies),
        GENERATED / "asyncapi.json": json_text(asyncapi(registry, transport)),
        GENERATED / "compatibility.json": json_text(compatibility(registry)),
    }
    for role, document in permissions(registry, transport).items():
        files[GENERATED / "permissions" / f"{role}.json"] = json_text(document)
    files.update(render_python(registry, schemas, transport, topologies))
    # Examples are sources whose digests and signatures this run fills in; the
    # manifest records them as rendered.
    files.update(render_examples(registry))
    sources = sorted(path for path in CONTRACT.rglob("*") if path.is_file() and GENERATED not in path.parents)
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
