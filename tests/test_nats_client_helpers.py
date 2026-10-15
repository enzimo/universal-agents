import json
import os
import sys

import pytest
import yaml

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import live_nats_client
from custom_messages.contracts.envelope_v1 import MessageEnvelope
from custom_messages.contracts.routing_v1 import MessageHeader, MessageMeta
from custom_messages.contracts.task_v1 import Task
from custom_messages.enums import MessageMetaType
from runtime_settings.projection import _project_transport


@pytest.mark.parametrize("instance", [False, True])
def test_live_transport_resolves_config_and_auth_once(tmp_path, monkeypatch, instance):
    from assistant_gateway.config import GatewayInstanceConfig
    from config_loader import CONFIG_SCHEMA_VERSION

    monkeypatch.setattr(live_nats_client, "PROJECT_ROOT", tmp_path)
    monkeypatch.delenv("TEAM_NATS_LIVE_ENV", raising=False)
    monkeypatch.delenv("NATS_SERVERS", raising=False)
    monkeypatch.delenv("NATS_USER", raising=False)
    password_path = tmp_path / "password"
    password_path.write_text("test-password")
    monkeypatch.delenv("NATS_PASSWORD", raising=False)
    monkeypatch.setenv("NATS_PASSWORD_FILE", str(password_path))
    monkeypatch.setenv("NATS_URL", "nats://127.0.0.1:44222")
    monkeypatch.setenv("NATS_USER_FILE", str(tmp_path / "user"))
    (tmp_path / "user").write_text("test-user")
    monkeypatch.setenv("NATS_CREDENTIALS_FILE", str(tmp_path / "test.creds"))
    if instance:
        raw = GatewayInstanceConfig.create(
            instance_name="transport-test", instance_root=str(tmp_path),
            team_id="transport-test", timezone="UTC",
        ).model_dump(mode="json")
    else:
        raw = {"schema_version": CONFIG_SCHEMA_VERSION}
    raw["transport"] = {
        "nats_servers": ["nats://unused.example:4222"],
        "subject_prefix": "transport-test", "storage": "memory", "ack_wait_seconds": 25,
    }
    path = tmp_path / "config.yaml"
    path.write_text(yaml.safe_dump(raw))
    monkeypatch.setenv("TEAM_NATS_LIVE_CONFIG", str(path))

    transport = live_nats_client.resolve_live_transport_settings()
    # Already-resolved settings must not read changing environment values again.
    monkeypatch.setenv("NATS_URL", "nats://unused.example:4444")
    monkeypatch.setenv("NATS_PASSWORD", "changed-password")
    client = live_nats_client.LiveNatsClient("transport-test", transport=transport)
    agent_config = _project_transport(transport_cfg=transport.model_dump())

    assert client.adapter.config.servers == agent_config["nats_servers"] == ["nats://127.0.0.1:44222"]
    assert client.adapter.config.user == agent_config["nats_user"] == "test-user"
    assert client.adapter.config.password == agent_config["nats_password"] == "test-password"
    assert client.adapter.config.credentials_file == agent_config["nats_credentials_file"] == str(tmp_path / "test.creds")
    assert client.mapper.subject_prefix == agent_config["nats_subject_prefix"] == "transport-test"
    assert client.adapter.config.storage == agent_config["nats_storage"] == "memory"
    assert client.adapter.config.ack_wait_seconds == agent_config["nats_ack_wait_seconds"] == 25


def test_live_transport_server_list_overrides_single_url(tmp_path, monkeypatch):
    monkeypatch.setattr(live_nats_client, "PROJECT_ROOT", tmp_path)
    monkeypatch.delenv("TEAM_NATS_LIVE_ENV", raising=False)
    monkeypatch.delenv("TEAM_NATS_LIVE_CONFIG", raising=False)
    monkeypatch.delenv("UA_LIVE_CONFIG", raising=False)
    monkeypatch.setenv("NATS_URL", "nats://unused.example:4222")
    monkeypatch.setenv("NATS_SERVERS", " nats://one:44222, nats://two:44222 ")

    assert live_nats_client.resolve_live_transport_settings().nats_servers == [
        "nats://one:44222", "nats://two:44222",
    ]


def test_extract_test_number_from_template_name():
    assert live_nats_client.extract_test_number("tests/message_templates/5_skill_creation.json") == "5"


def test_prepare_test_result_dir_creates_per_test_directory(tmp_path, monkeypatch):
    test_results_root = tmp_path / "test_results"
    monkeypatch.setattr(live_nats_client, "TEST_RESULTS_ROOT", test_results_root)

    result_dir = live_nats_client.prepare_test_result_dir("tests/message_templates/4_agent_creation.json")

    assert result_dir == test_results_root / "test_4"
    assert result_dir.exists()
    assert result_dir.is_dir()


def test_prepare_test_result_dir_clears_existing_artifacts(tmp_path, monkeypatch):
    test_results_root = tmp_path / "test_results"
    monkeypatch.setattr(live_nats_client, "TEST_RESULTS_ROOT", test_results_root)
    existing_dir = test_results_root / "test_5"
    existing_dir.mkdir(parents=True)
    (existing_dir / "stale.txt").write_text("old", encoding="utf-8")

    result_dir = live_nats_client.prepare_test_result_dir("tests/message_templates/5_skill_creation.json")

    assert result_dir == existing_dir
    assert result_dir.exists()
    assert not (result_dir / "stale.txt").exists()


def test_get_live_test_task_history_db_path_uses_shared_test_data_dir(tmp_path, monkeypatch):
    test_results_root = tmp_path / "test_results"
    monkeypatch.setattr(live_nats_client, "TEST_RESULTS_ROOT", test_results_root)
    monkeypatch.setattr(live_nats_client, "TEST_DATA_ROOT", test_results_root / "test_data")

    db_path = live_nats_client.get_live_test_task_history_db_path()

    assert db_path == test_results_root / "test_data" / "task_history.db"
    assert db_path.parent.exists()


def test_cleanup_live_test_artifacts_removes_weather_skill(tmp_path, monkeypatch):
    project_root = tmp_path / "repo"
    skill_dir = project_root / ".skills" / "weather-checker"
    skill_dir.mkdir(parents=True)
    (skill_dir / "SKILL.md").write_text("# weather", encoding="utf-8")

    monkeypatch.setattr(live_nats_client, "PROJECT_ROOT", project_root)
    monkeypatch.setattr(
        live_nats_client,
        "LIVE_TEST_SKILL_ARTIFACT_DIRS",
        {"5": [project_root / ".skills" / "weather-checker"]},
    )

    removed = live_nats_client.cleanup_live_test_artifacts("tests/message_templates/5_skill_creation.json")

    assert removed == [str(skill_dir)]
    assert not skill_dir.exists()


def test_persist_final_answer_writes_per_test_artifacts(tmp_path):
    response_envelope = MessageEnvelope(
        msg_content=Task(
            task_type=MessageMetaType.RESPONSE,
            answer="done",
        ),
        msg_meta=MessageMeta(
            from_agent_name="TeamArchitect",
            msg_correlation_id="corr_id_test1234",
        ),
        msg_header=MessageHeader(
            from_agent_id="agent_id_test",
            to_routing_path="team_303_user_interact_queue",
            agent_signature="",
        ),
    )

    live_nats_client.persist_final_answer(
        result_dir=tmp_path,
        correlation_id="corr_id_test1234",
        task_text="Do the thing",
        answer_text="done",
        response_envelope=response_envelope,
    )

    final_answer = json.loads((tmp_path / "final_answer.json").read_text(encoding="utf-8"))
    final_answer_text = (tmp_path / "final_answer.txt").read_text(encoding="utf-8")
    final_response = json.loads((tmp_path / "final_response_envelope.json").read_text(encoding="utf-8"))

    assert final_answer["correlation_id"] == "corr_id_test1234"
    assert final_answer["task"] == "Do the thing"
    assert final_answer["answer"] == "done"
    assert final_answer_text == "done"
    assert final_response["msg_meta"]["msg_correlation_id"] == "corr_id_test1234"
