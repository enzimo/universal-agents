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
  body and reply model, and the body shapes used to drop unknown fields that a
  newer minor version may send.

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
    "rendering", "covered_change", "copy_to", "verification",
}
REQUIRED_ENTRY_FIELDS = ENTRY_FIELDS - {"reply", "covered_change", "copy_to", "verification"}

# The schema subset (contracts/messages/v1/README.md).
ALLOWED_KEYWORDS = {
    "$schema", "$id", "$ref", "$defs", "title", "description", "type", "enum", "const", "properties",
    "required", "additionalProperties", "items", "minItems", "maxItems", "uniqueItems", "minLength",
    "maxLength", "pattern", "minimum", "maximum", "oneOf", "examples",
}
ALLOWED_TYPES = {"object", "string", "integer", "boolean", "array"}
MAX_SAFE_INTEGER = 2**53 - 1
UNPORTABLE_PATTERN = re.compile(r"\(\?[=!<>:]|\\[1-9]|[*+?}]\+|\\[AZzbBpP]")
# The frame's and the state document's bodies are the open objects: the schema
# their kind or record names checks them.
OPEN_OBJECTS = {("frame.schema.json", "/properties/body"), ("state.schema.json", "/properties/body")}


class ContractError(ValueError):
    pass


def json_text(payload: Any) -> str:
    return json.dumps(payload, indent=2, sort_keys=True, ensure_ascii=False) + "\n"


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


def check_subset(name: str, node: Any, pointer: str) -> None:
    """Refuse anything outside the documented schema subset."""

    if isinstance(node, list):
        for index, item in enumerate(node):
            check_subset(name, item, f"{pointer}/{index}")
        return
    if not isinstance(node, dict):
        return
    unknown = set(node) - ALLOWED_KEYWORDS
    if unknown and not pointer.endswith(("/properties", "/$defs")):
        raise ContractError(f"{name}{pointer}: keywords outside the subset: {sorted(unknown)}")
    if pointer.endswith(("/properties", "/$defs")):
        for key, child in node.items():
            check_subset(name, child, f"{pointer}/{key}")
        return
    if "type" in node and node["type"] not in ALLOWED_TYPES:
        raise ContractError(f"{name}{pointer}: type {node['type']!r} is outside the subset")
    if node.get("type") == "object" and (name, pointer) not in OPEN_OBJECTS:
        if node.get("additionalProperties") is not False or "properties" not in node:
            raise ContractError(f"{name}{pointer}: objects list their properties and set additionalProperties false")
    if node.get("type") == "integer":
        for bound in ("minimum", "maximum"):
            if bound not in node or abs(node[bound]) > MAX_SAFE_INTEGER:
                raise ContractError(f"{name}{pointer}: integers state {bound} within ±(2^53 - 1)")
    if "pattern" in node:
        pattern = node["pattern"]
        if not (pattern.startswith("^") and pattern.endswith("$")) or UNPORTABLE_PATTERN.search(pattern):
            raise ContractError(f"{name}{pointer}: pattern {pattern!r} is anchored badly or not portable")
    if "$ref" in node and "://" in node["$ref"]:
        raise ContractError(f"{name}{pointer}: $ref must be local to the contract")
    if "oneOf" in node:
        discriminator(name, pointer, node)
    for key, child in node.items():
        if key in {"properties", "$defs", "items", "oneOf"}:
            check_subset(name, child, f"{pointer}/{key}")


def discriminator(name: str, pointer: str, node: dict[str, Any]) -> str:
    """The property whose ``const`` tells the alternatives of a ``oneOf`` apart."""

    variants = node["oneOf"]
    common = None
    for variant in variants:
        consts = {key for key, value in variant.get("properties", {}).items() if "const" in value}
        common = consts if common is None else common & consts
    if not common or len(common) != 1:
        raise ContractError(f"{name}{pointer}: oneOf alternatives need one shared const property")
    key = next(iter(common))
    values = [variant["properties"][key]["const"] for variant in variants]
    if len(set(values)) != len(values):
        raise ContractError(f"{name}{pointer}: oneOf const values must differ")
    return key


def check_registry(registry: dict[str, Any], transport: dict[str, Any], topologies: dict[str, Any],
                   schemas: dict[str, dict[str, Any]]) -> None:
    roles = set(registry["host_roles"])
    edges = set(EDGES) - {"none"}
    for topology, spec in topologies["topologies"].items():
        for role, allowed in spec["allowed_edges"].items():
            if role not in topologies["agent_roles"] or not set(allowed) <= edges:
                raise ContractError(f"team-topologies.yaml: {topology}.{role} names an unknown role or edge")
    check_state_records(registry, transport, schemas)
    for name, entry in registry["kinds"].items():
        where = f"registry.yaml kinds.{name}"
        if not re.fullmatch(r"[a-z][a-z0-9_]*(\.[a-z][a-z0-9_]*)+", name) or name.endswith(".reply"):
            raise ContractError(f"{where}: bad kind name")
        missing = REQUIRED_ENTRY_FIELDS - set(entry)
        unknown = set(entry) - ENTRY_FIELDS
        if missing or unknown:
            raise ContractError(f"{where}: missing {sorted(missing)}, unknown {sorted(unknown)}")
        checks = {
            "class": (entry["class"], CLASSES), "authority": (entry["authority"], AUTHORITY),
            "routing": (entry["routing"], ROUTING), "topology_edge": (entry["topology_edge"], EDGES),
            "rendering": (entry["rendering"], RENDERING),
            "admission.priority": (entry["admission"].get("priority"), PRIORITIES),
        }
        for field_name, (value, allowed) in checks.items():
            if value not in allowed:
                raise ContractError(f"{where}: {field_name} {value!r} is not one of {sorted(allowed)}")
        check_wakes(where, entry["wakes_model"])
        if set(entry["admission"]) != {"execution_slot", "accepted_while_paused", "priority"}:
            raise ContractError(f"{where}: admission states execution_slot, accepted_while_paused and priority")
        if not set(entry["senders"]) <= roles or not set(entry["receivers"]) <= roles:
            raise ContractError(f"{where}: senders and receivers must be host roles")
        if not 1 <= entry["expiry_seconds"] <= transport["replay_ledger_retention_seconds"]:
            raise ContractError(f"{where}: expiry must be positive and within the replay record's retention")
        if entry["body"] not in schemas:
            raise ContractError(f"{where}: body schema {entry['body']} is missing")
        if (entry["class"] == "request_reply") != ("reply" in entry):
            raise ContractError(f"{where}: request/reply kinds, and only they, have a reply")
        if "reply" in entry and entry["reply"]["body"] not in schemas:
            raise ContractError(f"{where}: reply schema {entry['reply']['body']} is missing")
        for address in entry_addresses(entry):
            if not any(address_matches(address, template, name)
                       for template in class_addresses(transport, entry["class"])):
                raise ContractError(f"{where}: address {address!r} fits no {entry['class']} address")
        if entry.get("verification", "standard") not in VERIFICATION:
            raise ContractError(f"{where}: verification must be one of {sorted(VERIFICATION)}")
        if entry.get("verification") == "proof_of_possession" and entry["class"] != "request_reply":
            raise ContractError(f"{where}: only an installation request proves possession of its own key")
        if entry.get("copy_to") and entry["class"] != "durable":
            raise ContractError(f"{where}: only durable kinds are copied to other inboxes")


STATE_FIELDS = {"description", "bucket", "key", "writers", "body", "ttl_seconds", "write_rule"}


def check_state_records(registry: dict[str, Any], transport: dict[str, Any], schemas: dict[str, dict[str, Any]]) -> None:
    """Each state record names its bucket, key form, writer roles and body schema."""

    roles = set(registry["host_roles"])
    state_enum = set(schemas["state.schema.json"]["properties"]["record"]["enum"])
    if set(registry["state_records"]) != state_enum:
        raise ContractError("registry.yaml state_records and state.schema.json record names differ")
    for name, entry in registry["state_records"].items():
        where = f"registry.yaml state_records.{name}"
        if set(entry) - STATE_FIELDS or {"description", "bucket", "key", "writers", "body"} - set(entry):
            raise ContractError(f"{where}: fields must be {sorted(STATE_FIELDS)}")
        if entry["bucket"] not in transport["key_value"]:
            raise ContractError(f"{where}: bucket {entry['bucket']} is not in transport.yaml")
        if not set(entry["writers"]) <= roles or entry["body"] not in schemas:
            raise ContractError(f"{where}: writers must be host roles and the body schema must exist")


def full_subject(address: str, owner: str) -> str:
    """The NATS subject of an address: team addresses sit under ua.v1.<team>.<sender>, installation ones under ua.v1._install."""

    if address.startswith("_install."):
        return "ua.v1." + address.replace("{sender}", owner)
    return f"ua.v1.{{team}}.{owner}." + address


def entry_addresses(entry: dict[str, Any]) -> list[str]:
    """Every address a kind is published to: its own, then the copies (``copy_to``)."""

    own = entry["address"] if isinstance(entry["address"], list) else [entry["address"]]
    return list(own) + list(entry.get("copy_to", []))


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


def class_addresses(transport: dict[str, Any], delivery_class: str) -> list[str]:
    spec = transport["classes"][delivery_class]
    return list(spec["addresses"]) + list(spec.get("installation_addresses", []))


def address_matches(address: str, template: str, kind: str) -> bool:
    operation = kind.split(".", 1)[1] if kind.startswith("agent.") else kind
    expected = template.replace("{operation}", operation).replace("{event}", kind.replace(".", "_"))
    return address == expected


# -- generated JSON ------------------------------------------------------------------------------

def subject_pattern(address: str) -> str:
    """An address with its placeholders as wildcards, except the sender, which permissions bind to the publisher."""

    return re.sub(r"\{(?!sender\})[a-z_]+\}", "*", address)


def permissions(registry: dict[str, Any], transport: dict[str, Any]) -> dict[str, dict[str, Any]]:
    """Per host role: subjects it may publish (only under its own id) and subscribe to."""

    result = {}
    for role in registry["host_roles"]:
        publish, subscribe = set(), {f"{transport['reply_inbox_prefix'].replace('{runtime}', '{self}')}.>"}
        for name, entry in registry["kinds"].items():
            for address in map(subject_pattern, entry_addresses(entry)):
                if role in entry["senders"]:
                    publish.add(full_subject(address, "{self}"))
                if role in entry["receivers"] and entry["class"] not in {"durable", "dead_letter"}:
                    subscribe.add(full_subject(address, "*"))
            if role in entry["receivers"] and entry["class"] == "request_reply":
                publish.add("_INBOX.*.>")
        result[role] = {"role": role, "publish": sorted(publish), "subscribe": sorted(subscribe)}
    return result


def asyncapi(registry: dict[str, Any], transport: dict[str, Any]) -> dict[str, Any]:
    channels, operations, messages = {}, {}, {}
    for name, entry in sorted(registry["kinds"].items()):
        key = name.replace(".", "_")
        for index, address in enumerate(entry_addresses(entry)):
            channel = key if index == 0 else f"{key}_{index}"
            channels[channel] = {
                "address": transport["subject_prefix"] + "." + address,
                "messages": {key: {"$ref": f"#/components/messages/{key}"}},
            }
        messages[key] = {
            "name": name,
            "summary": entry["description"],
            "payload": {"$ref": f"../schemas/{entry['body']}"},
        }
        operations[f"send_{key}"] = {"action": "send", "channel": {"$ref": f"#/channels/{key}"}}
        operations[f"receive_{key}"] = {"action": "receive", "channel": {"$ref": f"#/channels/{key}"}}
        if "reply" in entry:
            reply_key = f"{key}_reply"
            messages[reply_key] = {"name": f"{name}.reply", "payload": {"$ref": f"../schemas/{entry['reply']['body']}"}}
            operations[f"send_{key}"]["reply"] = {"messages": [{"$ref": f"#/components/messages/{reply_key}"}]}
    return {
        "asyncapi": "3.1.0",
        "info": {"title": "Universal Agents messages", "version": registry["protocol"]},
        "defaultContentType": "application/json",
        "channels": channels,
        "operations": operations,
        "components": {"messages": messages},
    }


def compatibility(registry: dict[str, Any]) -> dict[str, Any]:
    label = registry["protocol"]
    major, rest = label.split(".", 1)
    return {
        "protocol": label,
        "accepts": {"major": int(major), "label": rest.split("-", 1)[1] if "-" in rest else None,
                    "minors": "any; a newer minor's unknown optional fields are ignored"},
        "signing_domain": registry["signing_domain"],
        "kinds": sorted(registry["kinds"]),
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
        if rendered != name:
            self.blocks.append(f"{name} = {rendered}\n")
        return name

    def model(self, file: str, schema: dict[str, Any], name: str) -> str:
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
                lines.append(f"    {attribute}: Optional[{prop_type}] = Field(default=None{alias})")
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


def render_python(registry: dict[str, Any], schemas: dict[str, dict[str, Any]]) -> dict[Path, str]:
    writer = ModelWriter(schemas)
    for fragment in schemas["common.schema.json"]["$defs"]:
        writer.named("common.schema.json", f"/$defs/{fragment}")
    writer.named("frame.schema.json", "")
    writer.named("state.schema.json", "")
    states = []
    for name, entry in sorted(registry["state_records"].items()):
        body = writer.type_of(entry["body"], schemas[entry["body"]], class_name(name) + "State")
        states.append((name, entry, body))
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
        "from typing import Annotated, Any, Literal, Optional, Union\n\n"
        "from pydantic import BaseModel, ConfigDict, Field, StringConstraints, TypeAdapter\n\n\n"
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
            f"        addresses={tuple(entry_addresses(entry))!r},\n"
            f"        body_model=models.{class_name(name)}BodyAdapter,\n"
            f"        reply_model={reply_text},\n"
            f"        reply_errors={tuple(entry.get('reply', {}).get('errors', ()))!r},\n"
            f"        senders={tuple(entry['senders'])!r},\n"
            f"        receivers={tuple(entry['receivers'])!r},\n"
            f"        expiry_seconds={entry['expiry_seconds']!r},\n"
            f"        authority={entry['authority']!r},\n"
            f"        execution_slot={entry['admission']['execution_slot']!r},\n"
            f"        accepted_while_paused={entry['admission']['accepted_while_paused']!r},\n"
            f"        priority={entry['admission']['priority']!r},\n"
            f"        routing={entry['routing']!r},\n"
            f"        topology_edge={entry['topology_edge']!r},\n"
            f"        ledger={entry['ledger']!r},\n"
            f"        wakes_model={entry['wakes_model']!r},\n"
            f"        rendering={entry['rendering']!r},\n"
            f"        body_shape={shape(writer, body_file, schemas[body_file])!r},\n"
            f"        reply_shape={reply_shape},\n"
            f"        verification={entry.get('verification', 'standard')!r},\n"
            "    ),"
        )
    kinds_text = header + (
        "from __future__ import annotations\n\n"
        "from custom_messages.wire import models\n"
        "from custom_messages.wire.spec import KindSpec, StateSpec\n\n"
        f"PROTOCOL = {registry['protocol']!r}\n"
        f"SIGNING_DOMAIN = {registry['signing_domain']!r}\n"
        f"MAX_FRAME_BYTES = {registry['max_frame_bytes']!r}\n"
        f"VERIFICATION_REFUSALS = {tuple(item['code'] for item in registry['verification_refusals'])!r}\n"
        f"CHANGE_REFUSALS = {tuple(item['code'] for item in registry['change_refusals'])!r}\n"
        f"HOST_ROLES = {tuple(registry['host_roles'])!r}\n"
        f"FRAME_SHAPE = {shape(writer, 'frame.schema.json', schemas['frame.schema.json'])!r}\n\n"
        "KINDS: dict[str, KindSpec] = {\n" + "\n".join(specs) + "\n}\n\n"
        f"STATE_SIGNING_DOMAIN = {registry['state_signing_domain']!r}\n"
        "STATE_RECORDS: dict[str, StateSpec] = {\n"
        + "\n".join(
            f"    {name!r}: StateSpec(name={name!r}, bucket={entry['bucket']!r}, key={entry['key']!r}, "
            f"writers={tuple(entry['writers'])!r}, ttl_seconds={entry.get('ttl_seconds')!r}, "
            f"body_model=models.{class_name(name)}StateAdapter),"
            for name, entry, _body in states)
        + "\n}\n"
    )
    return {WIRE / "models.py": models, WIRE / "kinds.py": kinds_text}


def render() -> dict[Path, str]:
    registry, transport, topologies = load_yaml("registry.yaml"), load_yaml("transport.yaml"), load_yaml("team-topologies.yaml")
    schemas = load_schemas()
    if registry["max_frame_bytes"] != transport["limits"]["max_frame_bytes"]:
        raise ContractError("registry.yaml and transport.yaml disagree on the frame size limit")
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
    files.update(render_python(registry, schemas))
    sources = sorted(path for path in CONTRACT.rglob("*") if path.is_file() and GENERATED not in path.parents)
    entries = {path.relative_to(REPO_ROOT).as_posix(): hashlib.sha256(path.read_bytes()).hexdigest() for path in sources}
    entries.update({path.relative_to(REPO_ROOT).as_posix(): hashlib.sha256(text.encode("utf-8")).hexdigest()
                    for path, text in files.items()})
    files[GENERATED / "manifest.json"] = json_text({"protocol": registry["protocol"], "files": entries})
    return files


def stale(files: dict[Path, str]) -> list[str]:
    problems = [path.relative_to(REPO_ROOT).as_posix() for path, text in files.items()
                if not path.is_file() or path.read_text(encoding="utf-8") != text]
    written = set(files)
    for path in sorted(GENERATED.rglob("*")):
        if path.is_file() and path not in written:
            problems.append(f"{path.relative_to(REPO_ROOT).as_posix()} (not generated)")
    for path in (WIRE / "models.py", WIRE / "kinds.py"):
        if path not in written:
            problems.append(path.relative_to(REPO_ROOT).as_posix())
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
        path.write_text(text, encoding="utf-8")
    return 0


if __name__ == "__main__":
    sys.exit(main())
