"""``transport.queues``: the protocol 1 streams' byte limits, warning level, partition count and per-subject cap.

The settings live in both schemas (config.yaml and instance_config.yaml share
``TransportSettings``). Their defaults and bounds repeat a few contract values so the
configuration loader does not load the generated contract; these tests keep them equal.
"""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml
from pydantic import ValidationError

from custom_transport.protocol1.contract_files import transport
from default_values import MAX_QUEUE_PARTITIONS, MIB, MIN_QUEUE_BYTES, TRANSPORT_DEFAULTS
from runtime_settings.transport import (
    MAX_WARN_AT_PERCENT,
    MIN_WARN_AT_PERCENT,
    PARTITION_COUNT_SETTING,
    QueueByteLimits,
    QueueSettings,
    TransportSettings,
)
from tests.pinned_memory_support import new_instance


def test_defaults_and_bounds_match_the_contract() -> None:
    contract = transport()
    queues = QueueSettings()

    assert list(QueueByteLimits.model_fields) == contract["queues"]["size_kinds"]
    assert queues.warn_at_percent == contract["queues"]["warn_at_percent_default"]
    assert queues.partitions == contract["partitions"]["default_count"]
    assert MAX_QUEUE_PARTITIONS == contract["partitions"]["max_count"]
    assert MIN_QUEUE_BYTES == contract["limits"]["nats_max_payload_bytes"]
    assert ".".join(PARTITION_COUNT_SETTING) == contract["partitions"]["count_setting"]
    assert "transport.queues.max_bytes" == contract["queues"]["max_bytes_setting"]
    assert "transport.queues.warn_at_percent" == contract["queues"]["warn_at_percent_setting"]


def test_defaults_reserve_the_documented_room_per_team() -> None:
    limits = QueueByteLimits()
    per_team = (
        QueueSettings().partitions * limits.partition
        + limits.pool
        + limits.edge
        + limits.team_services
        + limits.dead_letter
    )

    assert per_team == 2176 * MIB  # 2.125 GiB, as config.example.yaml says
    assert TransportSettings().queues == QueueSettings()


@pytest.mark.parametrize("kind", list(QueueByteLimits.model_fields))
def test_a_byte_limit_below_one_mebibyte_is_refused_by_name(kind: str) -> None:
    QueueByteLimits.model_validate({kind: MIN_QUEUE_BYTES})
    with pytest.raises(ValidationError, match=f"transport.queues.max_bytes.{kind} must be at least {MIN_QUEUE_BYTES}"):
        QueueByteLimits.model_validate({kind: MIN_QUEUE_BYTES - 1})


@pytest.mark.parametrize(
    ("field", "least", "most"),
    [
        ("warn_at_percent", MIN_WARN_AT_PERCENT, MAX_WARN_AT_PERCENT),
        ("partitions", 1, MAX_QUEUE_PARTITIONS),
        ("max_messages_per_subject", 1, None),
    ],
)
def test_each_setting_takes_whole_numbers_within_its_bounds(field: str, least: int, most: int | None) -> None:
    assert getattr(QueueSettings.model_validate({field: least}), field) == least
    assert getattr(QueueSettings.model_validate({field: str(least)}), field) == least
    with pytest.raises(ValidationError, match=f"transport.queues.{field} must be"):
        QueueSettings.model_validate({field: least - 1})
    if most is not None:
        assert getattr(QueueSettings.model_validate({field: most}), field) == most
        with pytest.raises(ValidationError, match=f"transport.queues.{field} must be from {least} to {most}"):
            QueueSettings.model_validate({field: most + 1})
    for wrong in (True, 2.5, "eight"):
        with pytest.raises(ValidationError, match=f"transport.queues.{field} must be a whole number"):
            QueueSettings.model_validate({field: wrong})


def test_a_mistyped_kind_or_setting_is_refused_rather_than_ignored() -> None:
    with pytest.raises(ValidationError, match="partitons"):
        QueueByteLimits.model_validate({"partitons": 32 * MIB})
    with pytest.raises(ValidationError, match="warn_at"):
        QueueSettings.model_validate({"warn_at": 70})


def test_a_file_without_queue_settings_loads_their_defaults_and_saves_them(tmp_path: Path) -> None:
    """Older files have no ``transport.queues``; they load with the defaults, and saving writes them
    out, so the partition count an instance started with stays in its file whatever later defaults are."""

    from assistant_gateway.config import GatewayInstanceConfig

    path, _config = new_instance(tmp_path, "alpha", "team_a")
    saved = yaml.safe_load(path.read_text(encoding="utf-8"))
    del saved["transport"]["queues"]
    path.write_text(yaml.safe_dump(saved), encoding="utf-8")

    loaded = GatewayInstanceConfig.load(path)
    assert loaded.transport.queues == QueueSettings()
    loaded.save(path)

    text = path.read_text(encoding="utf-8")
    assert yaml.safe_load(text)["transport"]["queues"]["partitions"] == TRANSPORT_DEFAULTS.queues_partitions.value
    partitions_line = next(line for line in text.splitlines() if line.strip().startswith("partitions:"))
    assert "Fixed when the instance is created" in partitions_line


def test_config_yaml_validates_the_same_settings() -> None:
    from config_loader import Config, resolve_agent_type_settings
    from runtime_settings.resolution import normalize_runtime_sections

    chosen = {"queues": {"max_bytes": {"partition": 32 * MIB}, "partitions": 64}}
    normalized = normalize_runtime_sections({"transport": chosen})
    assert normalized["transport"]["queues"] == chosen["queues"]
    resolve_agent_type_settings(Config({"transport": chosen}))

    with pytest.raises(ValidationError, match="transport.queues.partitions must be from 1 to 128"):
        resolve_agent_type_settings(Config({"transport": {"queues": {"partitions": 0}}}))
