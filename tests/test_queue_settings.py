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

from assistant_gateway.config import _INSTANCE_CONFIG_COMMENTS, StorageWarningSettings
from custom_transport.protocol1.contract_files import transport
from default_values import (
    MAX_QUEUE_MESSAGE_BYTES,
    MAX_QUEUE_PARTITIONS,
    MIB,
    MIN_QUEUE_BYTES,
    QUEUE_SENDERS_AT_CAP,
    TRANSPORT_DEFAULTS,
)
from runtime_settings.transport import (
    MAX_WARN_AT_PERCENT,
    MIN_WARN_AT_PERCENT,
    PARTITION_COUNT_SETTING,
    QueueByteLimits,
    QueueSettings,
    TransportSettings,
    least_partition_bytes,
    most_per_subject_cap,
)
from runtime_settings.values import MAX_INT64, whole_number
from tests.pinned_memory_support import new_instance

EXAMPLE_CONFIG = Path(__file__).resolve().parents[1] / "config.example.yaml"
CAP = "transport.queues.max_messages_per_subject"
PARTITION_LIMIT = "transport.queues.max_bytes.partition"


def test_defaults_and_bounds_match_the_contract() -> None:
    contract = transport()
    queues = QueueSettings()

    assert list(QueueByteLimits.model_fields) == contract["queues"]["size_kinds"]
    assert queues.warn_at_percent == contract["queues"]["warn_at_percent_default"]
    assert queues.partitions == contract["partitions"]["default_count"]
    assert MAX_QUEUE_PARTITIONS == contract["partitions"]["max_count"]
    assert MIN_QUEUE_BYTES == contract["limits"]["nats_max_payload_bytes"]
    assert {stream["max_message_bytes"] for stream in contract["streams"].values()} == {MAX_QUEUE_MESSAGE_BYTES}
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
    with pytest.raises(ValidationError, match=f"transport.queues.max_bytes.{kind} must be from {MIN_QUEUE_BYTES} to {MAX_INT64}"):
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
    for wrong in (True, 2.5, "eight", "²"):
        with pytest.raises(ValidationError, match=f"transport.queues.{field} must be a whole number"):
            QueueSettings.model_validate({field: wrong})


@pytest.mark.parametrize("chosen", [{"max_bytes": {"partition": MAX_INT64 + 1}}, {"max_bytes": {"dead_letter": MAX_INT64 + 1}},
                                    {"max_messages_per_subject": MAX_INT64 + 1}])
def test_values_above_what_nats_reads_as_a_64_bit_number_are_refused_by_name(chosen: dict) -> None:
    with pytest.raises(ValidationError, match=f"must be from 1(048576)? to {MAX_INT64}, not {MAX_INT64 + 1}"):
        QueueSettings.model_validate(chosen)
    assert QueueSettings.model_validate({"max_bytes": {"partition": MAX_INT64}}).max_bytes.partition == MAX_INT64


@pytest.mark.parametrize("wrong", [True, 2.5, "eight", "²", "-3", None])
def test_every_refused_value_names_the_setting_in_each_user_of_the_rule(wrong: object) -> None:
    """One rule for whole-number settings (runtime_settings.values.whole_number); ``²`` passes str.isdigit."""

    with pytest.raises(ValueError, match="^x.y must be a whole number.$"):
        whole_number(wrong, name="x.y")
    with pytest.raises(ValidationError, match="gateway.storage_warnings.person_step_gb must be a whole number"):
        StorageWarningSettings(person_step_gb=wrong)
    assert whole_number(" 12 ", name="x.y", least=12, most=12) == 12
    with pytest.raises(ValueError, match="x.y must be at most 3, not 4"):
        whole_number(4, name="x.y", most=3)


def test_the_default_partition_limit_holds_the_measured_senders_at_the_default_cap_and_room_for_more() -> None:
    queues = QueueSettings()

    assert least_partition_bytes(queues.max_messages_per_subject) == (QUEUE_SENDERS_AT_CAP + 1) * 8 * 263_168 == 10_526_720
    assert queues.max_bytes.partition >= least_partition_bytes(queues.max_messages_per_subject)
    assert most_per_subject_cap(queues.max_bytes.partition) == 12
    assert QueueSettings.model_validate({"max_bytes": {"partition": 10_526_720}}).max_bytes.partition == 10_526_720


def test_a_partition_limit_lowered_alone_below_what_the_cap_needs_is_refused_naming_both_settings() -> None:
    """FA-3 again: at 2 MiB, one sender's 8 full-size messages for one agent fill the stream before the cap."""

    with pytest.raises(ValidationError) as refused:
        QueueSettings.model_validate({"max_bytes": {"partition": 2 * MIB}})
    message = str(refused.value)
    assert f"{PARTITION_LIMIT} (2,097,152 bytes) is too small for {CAP} (8)" in message
    assert f"Raise {PARTITION_LIMIT} to 10,526,720 or more, or lower {CAP} to 1 or less." in message
    assert QueueSettings.model_validate({"max_bytes": {"partition": 2 * MIB}, "max_messages_per_subject": 1})


def test_a_cap_raised_alone_past_what_the_partition_limit_holds_is_refused() -> None:
    with pytest.raises(ValidationError, match=rf"{CAP} \(13\).*or lower {CAP} to 12 or less"):
        QueueSettings.model_validate({"max_messages_per_subject": 13})


def test_a_partition_limit_too_small_for_any_cap_only_says_to_raise_it() -> None:
    with pytest.raises(ValidationError) as refused:
        QueueSettings.model_validate({"max_bytes": {"partition": MIN_QUEUE_BYTES}, "max_messages_per_subject": 1})
    assert f"Raise {PARTITION_LIMIT} to 1,315,840 or more." in str(refused.value)
    assert "or lower" not in str(refused.value)


def _example_queue_lines() -> dict[str, str]:
    """Each line under transport.queues in config.example.yaml, by its dotted setting path."""

    lines: dict[str, str] = {}
    stack: list[tuple[int, str]] = []
    for line in EXAMPLE_CONFIG.read_text(encoding="utf-8").splitlines():
        stripped = line.lstrip()
        if not stripped or stripped.startswith("#") or ":" not in stripped:
            continue
        indent = len(line) - len(stripped)
        while stack and stack[-1][0] >= indent:
            stack.pop()
        key = stripped.split(":", 1)[0].strip()
        path = ".".join([*(name for _, name in stack), key])
        stack.append((indent, key))
        if path.startswith("transport.queues"):
            lines[path] = line
    return lines


def _leaf_paths(values: dict, prefix: str) -> dict[str, object]:
    leaves: dict[str, object] = {}
    for key, value in values.items():
        path = f"{prefix}.{key}"
        leaves.update(_leaf_paths(value, path) if isinstance(value, dict) else {path: value})
    return leaves


def test_every_queue_setting_has_its_comment_and_default_in_both_files() -> None:
    """A1: each setting is documented in instance_config.yaml's comments and inline in config.example.yaml."""

    leaves = _leaf_paths(QueueSettings().model_dump(), "transport.queues")
    example = _example_queue_lines()
    assert len(leaves) == 8  # five byte limits, the warning level, the partition count and the cap
    for path, default in leaves.items():
        assert _INSTANCE_CONFIG_COMMENTS.get(path), f"{path} has no instance file comment"
        line = example.get(path)
        assert line is not None, f"{path} is not in config.example.yaml"
        value, _, comment = line.split(":", 1)[1].partition(" # ")
        assert yaml.safe_load(value) == default, f"{path} in config.example.yaml is not the default {default}"
        assert comment.strip(), f"{path} in config.example.yaml has no inline comment"
    for group in ("transport.queues", "transport.queues.max_bytes"):
        assert _INSTANCE_CONFIG_COMMENTS.get(group) and " # " in example[group]


def test_both_settings_comments_state_the_relation_the_check_enforces() -> None:
    example = _example_queue_lines()
    least_at_default = f"{least_partition_bytes(QueueSettings().max_messages_per_subject):,}"
    for path in (PARTITION_LIMIT, CAP):
        for text in (_INSTANCE_CONFIG_COMMENTS[path], example[path]):
            assert f"{QUEUE_SENDERS_AT_CAP + 1} x" in text and f"{MAX_QUEUE_MESSAGE_BYTES:,} bytes" in text
    for text in (_INSTANCE_CONFIG_COMMENTS[PARTITION_LIMIT], example[PARTITION_LIMIT]):
        assert f"({least_at_default} at a cap of 8)" in text


def test_a_mistyped_kind_or_setting_is_refused_rather_than_ignored() -> None:
    with pytest.raises(ValidationError, match="partitons"):
        QueueByteLimits.model_validate({"partitons": 32 * MIB})
    with pytest.raises(ValidationError, match="warn_at"):
        QueueSettings.model_validate({"warn_at": 70})


def test_a_file_without_queue_settings_loads_their_defaults_and_saves_them(tmp_path: Path) -> None:
    """A current file without ``transport.queues`` (edited by hand) loads with the defaults, and saving
    through the model writes them out. Upgraded files get the partition count from the instance
    configuration's step 14 -> 15 instead (tests/test_transport_config_migrations.py), because the
    launcher's upgrade writes the upgraded settings without saving through the model."""

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
