"""The bundled NATS server's generated configuration follows the message contract.

README (contracts/messages/v1), "Delivery classes" and "Credential size": every server
that carries protocol 1 syncs each confirmed JetStream write to disk, and its control
line fits the largest host credential.
"""

from __future__ import annotations

import json
import logging
from pathlib import Path

import pytest

import docker.nats_service as nats_service

TRANSPORT = json.loads(nats_service.CONTRACT_TRANSPORT_PATH.read_text(encoding="utf-8"))


def test_the_server_configuration_applies_the_contracts_server_settings(tmp_path: Path) -> None:
    config = nats_service.write_local_nats_server_config(tmp_path, user="ua-runtime", password='p"w')
    text = config.read_text(encoding="utf-8")
    assert 'password: "p\\"w"' in text
    assert "jetstream {\n  sync_interval: \"always\"\n}\n" in text
    control_line = TRANSPORT["limits"]["max_credential_bytes"] + nats_service.CONNECT_LINE_ROOM_BYTES
    assert f"max_control_line: {control_line}\n" in text
    assert TRANSPORT["server"]["jetstream"] == {"sync_interval": "always"}
    assert config.stat().st_mode & 0o777 == 0o600


@pytest.mark.parametrize(("output", "reported"), [
    ("nats-server: v2.15.1", False),
    ("nats-server: v2.16.0", False),
    ("nats-server: v2.14.5", True),
    ("", True),
])
def test_an_older_or_unreadable_server_version_is_reported(output: str, reported: bool, caplog) -> None:
    with caplog.at_level(logging.ERROR, logger="test_nats_service_config"):
        nats_service.report_server_below_contract(output, logging.getLogger("test_nats_service_config"))
    assert ("nats_server_below_contract_minimum" in caplog.text) is reported
