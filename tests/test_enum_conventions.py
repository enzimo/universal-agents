"""String enums are StrEnum, so their text is their value everywhere.

On Python 3.11 and later, ``str()``, f-strings and ``format()`` of a
``class X(str, Enum)`` member give the qualified name
(``RoutingType.ALL_AGENTS_ROUTE``), not the value (``++<all>++``). That text
differed from the JSON, database and NATS value: it reached a NATS header and
task history shown to models, and the subject mapper had to compare against both
forms (see CONTRIBUTING.md, "String enums").
"""

from __future__ import annotations

import ast
import subprocess
from pathlib import Path

from custom_messages.contracts.envelope_v1 import MessageEnvelope
from custom_messages.contracts.routing_v1 import MessageHeader
from custom_messages.contracts.task_v1 import Task
from custom_messages.enums import MessageMetaType, ProgressState, RoutingType
from custom_transport.base import TransportPublishOptions
from custom_transport.nats_adapter import NatsTransportAdapter, NatsTransportConfig
from custom_transport.subjects import NatsSubjectMapper

PROJECT_ROOT = Path(__file__).resolve().parents[1]


def _str_mixin_enum_classes(project_root: Path = PROJECT_ROOT) -> list[str]:
    # The convention belongs to project sources, never installed dependencies.
    tracked = subprocess.run(
        ["git", "ls-files", "-z", "--", "*.py"],
        cwd=project_root,
        check=True,
        capture_output=True,
        text=True,
    ).stdout
    found = []
    for relative_path in filter(None, tracked.split("\0")):
        path = project_root / relative_path
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in ast.walk(tree):
            if not isinstance(node, ast.ClassDef):
                continue
            bases = {ast.unparse(base) for base in node.bases}
            if "str" in bases and bases & {"Enum", "enum.Enum"}:
                found.append(f"{relative_path}:{node.lineno} {node.name}")
    return found


def test_no_enum_mixes_str_into_a_plain_enum():
    assert _str_mixin_enum_classes() == [], "use enum.StrEnum (CONTRIBUTING.md, String enums)"


def test_enum_scan_ignores_unrelated_virtual_environments(tmp_path: Path):
    owned = tmp_path / "owned.py"
    owned.write_text("from enum import StrEnum\nclass Owned(StrEnum):\n    VALUE = 'value'\n", encoding="utf-8")
    dependency = tmp_path / ".ci-skillspector/lib/python3.12/site-packages/third_party.py"
    dependency.parent.mkdir(parents=True)
    dependency.write_text("from enum import Enum\nclass ThirdParty(str, Enum):\n    VALUE = 'value'\n", encoding="utf-8")
    subprocess.run(["git", "init", "--quiet", str(tmp_path)], check=True, capture_output=True)
    subprocess.run(["git", "add", "owned.py"], cwd=tmp_path, check=True, capture_output=True)

    assert _str_mixin_enum_classes(tmp_path) == []
    # An unstaged edit to a tracked source must still be checked.
    owned.write_text("from enum import Enum\nclass Owned(str, Enum):\n    VALUE = 'value'\n", encoding="utf-8")
    assert _str_mixin_enum_classes(tmp_path) == ["owned.py:2 Owned"]


def test_string_enum_text_is_its_value():
    assert str(RoutingType.ALL_AGENTS_ROUTE) == "++<all>++"
    assert f"{ProgressState.IN_PROGRESS}" == "in_progress"
    assert "{}".format(MessageMetaType.REQUEST) == "request"


def test_the_all_agents_route_maps_to_broadcast_as_member_or_value():
    mapper = NatsSubjectMapper(team_id="team_t")
    expected = mapper.broadcast_subject()
    assert mapper.route_subject(RoutingType.ALL_AGENTS_ROUTE) == expected
    assert mapper.route_subject("++<all>++") == expected


def test_the_routing_header_carries_the_route_value():
    adapter = NatsTransportAdapter(config=NatsTransportConfig(), mapper=NatsSubjectMapper(team_id="team_t"))
    envelope = MessageEnvelope(
        msg_header=MessageHeader(from_agent_id="a", to_routing_path=RoutingType.ALL_AGENTS_ROUTE),
        msg_content=Task(task_type=MessageMetaType.REQUEST, problem_statement="x"),
    )
    headers = adapter._headers_for(envelope, TransportPublishOptions())
    assert headers["to_routing_path"] == "++<all>++"
