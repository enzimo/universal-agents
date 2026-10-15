"""Every process runs from the one saved instance_config.yaml (owner's decision 2026-09-29).

Before: the Docker bundle ran every process from a copy of the saved file
written at container start, and every deployment handed each child
environment variables copied from the file at launch, which win over the file
and were reused on every in-app restart. A setting saved while the runtime ran
(TeamLead's model change, the configuration editor's website port) did not
take effect even after the restart it asked for.

These tests replay what the bundle and the headless launcher do, without
starting processes: save a setting, restart in-app, and check what the new
process reads. They also check that the bundle's own NATS and PostgreSQL
details reach every process without being written to any file.
"""

from __future__ import annotations

import logging
import os
from pathlib import Path
from unittest import mock

import pytest

from assistant_gateway.config import GatewayInstanceConfig
from assistant_gateway.config_manager_route import ConfigGatewayManagerRouteWriter
from assistant_gateway.runtime_config import build_runtime_config_from_instance_config
from docker.launch_headless import (
    build_instance_env_defaults,
    build_process_env,
    run_supervised,
    scoped_environment,
)
from docker.nats_service import INTERNAL_NATS_HOST, INTERNAL_NATS_PORT, apply_local_nats_env
from docker.run_bundle import INTERNAL_POSTGRES_HOST, INTERNAL_POSTGRES_PORT, build_bundle_runtime_env
from docker.settings_environment import SettingsEnvironment
from env_secrets import LISTED_VARIABLES, PROCESS_GATEWAY, PROCESS_MANAGER

OPERATOR_DSN = "postgresql://team:secret@db.example:5544/team_agents"
BUNDLED_DSN = f"postgresql://team:secret@{INTERNAL_POSTGRES_HOST}:{INTERNAL_POSTGRES_PORT}/team_agents"
LOCAL_NATS = f"nats://{INTERNAL_NATS_HOST}:{INTERNAL_NATS_PORT}"
CONTAINER_ENV = {"PATH": os.environ.get("PATH", "/usr/bin"), "POSTGRES_DSN": OPERATOR_DSN}


def _saved_instance(tmp_path: Path) -> Path:
    config = GatewayInstanceConfig.create(
        instance_name="saved-settings",
        instance_root=str(tmp_path / "instance"),
        team_id="team_saved",
        timezone="America/Chicago",
        manager_storage_backend="postgres",
    )
    config.llm.provider = "litellm"
    config.llm.model_id = "old/model"
    config.transport.nats_url = "nats://nats.operator:4333"
    config.transport.nats_servers = ["nats://nats.operator:4333"]
    return config.save()


def _bundle_launcher_env(saved_path: Path) -> dict[str, str]:
    """What docker/run_bundle.py hands the headless launcher."""

    config = GatewayInstanceConfig.load_saved(saved_path)
    effective = build_process_env(config, env_path=None, base_env=CONTAINER_ENV)
    apply_local_nats_env(effective)
    return build_bundle_runtime_env(config, base_env=effective, ambient_env=CONTAINER_ENV)


def _launch(saved_path: Path, ambient: dict[str, str]) -> tuple[SettingsEnvironment, dict[str, dict[str, str]]]:
    """What docker/launch_headless.py main computes before it starts processes."""

    with mock.patch.dict(os.environ, ambient, clear=True):
        config = GatewayInstanceConfig.load_for_process(saved_path)
        runtime_env = build_process_env(config, env_path=None)
        settings = SettingsEnvironment.at_launch(
            config,
            defaults=build_instance_env_defaults,
            launch_env=runtime_env,
            operator_keys=frozenset(os.environ),
        )
        envs = {
            "manager": scoped_environment(PROCESS_MANAGER, runtime_env),
            "gateway": scoped_environment(PROCESS_GATEWAY, runtime_env),
        }
    return settings, envs


def _process_reads(saved_path: Path, env: dict[str, str]):
    """The runtime configuration a process started with ``env`` builds (strands_main)."""

    with mock.patch.dict(os.environ, env, clear=True):
        return build_runtime_config_from_instance_config(GatewayInstanceConfig.load_for_process(saved_path))


def _save_while_running(saved_path: Path, **changes) -> None:
    """A settings writer: load the saved file, change only its fields, save (RuntimeSettingsService)."""

    saved = GatewayInstanceConfig.load_saved(saved_path)
    saved.llm.model_id = changes.get("model_id", saved.llm.model_id)
    saved.interactive_apps.internal_website.bind_port = changes.get(
        "bind_port", saved.interactive_apps.internal_website.bind_port
    )
    saved.save()


@pytest.mark.parametrize("deployment", ["bundle", "plain"])
def test_a_setting_saved_while_running_applies_after_the_in_app_restart(tmp_path, deployment):
    saved_path = _saved_instance(tmp_path)
    ambient = _bundle_launcher_env(saved_path) if deployment == "bundle" else dict(CONTAINER_ENV)
    settings, envs = _launch(saved_path, ambient)
    assert _process_reads(saved_path, envs["manager"]).get("llm.model_id") == "old/model"

    _save_while_running(saved_path, model_id="new/model", bind_port=18801)
    saved_bytes = saved_path.read_bytes()
    # Reusing the launch environment, as restarts did, keeps the old model.
    assert _process_reads(saved_path, envs["manager"]).get("llm.model_id") == "old/model"
    manager_env = settings.refresh(envs["manager"], process="manager")
    gateway_env = settings.refresh(envs["gateway"], process="gateway")

    assert _process_reads(saved_path, manager_env).get("llm.model_id") == "new/model"
    assert gateway_env["INTERNAL_WEBSITE_BIND_PORT"] == "18801"
    if deployment == "bundle":
        # The bundle's own services still win, and are nowhere in the saved file.
        assert manager_env["NATS_URL"] == LOCAL_NATS and manager_env["POSTGRES_DSN"] == BUNDLED_DSN
        reads = _process_reads(saved_path, manager_env)
        assert reads.get("transport.nats_url") == LOCAL_NATS
        loaded = GatewayInstanceConfig.load_for_process(saved_path, env=manager_env)
        assert loaded.resolve_manager_storage_settings(env=manager_env).postgres_dsn == BUNDLED_DSN
        assert GatewayInstanceConfig.load_saved(saved_path).transport.nats_url == "nats://nats.operator:4333"
    # Loading and restarting never write the saved file.
    assert saved_path.read_bytes() == saved_bytes


def test_variables_the_operator_set_are_never_replaced(tmp_path):
    saved_path = _saved_instance(tmp_path)
    settings, envs = _launch(saved_path, {**CONTAINER_ENV, "LLM_MODEL_ID": "operator/model"})

    _save_while_running(saved_path, model_id="new/model")
    refreshed = settings.refresh(envs["manager"], process="manager")

    assert refreshed["LLM_MODEL_ID"] == "operator/model"
    assert _process_reads(saved_path, refreshed).get("llm.model_id") == "operator/model"


def test_secrets_are_never_recomputed(tmp_path):
    saved_path = _saved_instance(tmp_path)
    settings, envs = _launch(saved_path, dict(CONTAINER_ENV))

    assert not any(entry.covers(key) for key in settings.derived_keys for entry in LISTED_VARIABLES)
    refreshed = settings.refresh(envs["manager"], process="manager")
    assert {key for key in refreshed if any(entry.covers(key) for entry in LISTED_VARIABLES)} == {
        key for key in envs["manager"] if any(entry.covers(key) for entry in LISTED_VARIABLES)
    }
    assert all(refreshed[key] == envs["manager"][key] for key in envs["manager"] if key not in settings.derived_keys)


def test_the_supervisor_restarts_a_process_with_the_saved_settings(tmp_path, monkeypatch):
    saved_path = _saved_instance(tmp_path)
    settings, envs = _launch(saved_path, dict(CONTAINER_ENV))
    launched: list[tuple[str, dict[str, str]]] = []
    requests = iter([None, {"scope": "manager", "reason": "apply the model", "request_path": "r"}])

    class FakeProcess:
        pid = 1

        def __init__(self, name: str):
            self.name = name
            self.terminated = False

        def poll(self):
            if self.terminated:
                return 0
            restarted = [name for name, _ in launched].count("manager") > 1
            return 0 if self.name == "manager" and restarted else None

        def terminate(self) -> None:
            self.terminated = True

        def kill(self) -> None:
            self.terminated = True

        def wait(self, timeout=None):
            return 0

    def fake_popen(command, **kwargs):
        launched.append((command[0], dict(kwargs["env"])))
        return FakeProcess(command[0])

    def next_request(_path):
        request = next(requests, None)
        if request is not None:
            _save_while_running(saved_path, model_id="new/model")
        return request

    monkeypatch.setattr("docker.launch_headless.subprocess.Popen", fake_popen)
    monkeypatch.setattr("docker.launch_headless.time.sleep", lambda _seconds: None)
    monkeypatch.setattr("docker.launch_headless.pop_runtime_restart_request", next_request)

    run_supervised(
        manager_command=["manager"],
        gateway_command=["gateway"],
        activity_collector_command=["activity"],
        manager_env=envs["manager"],
        gateway_env=envs["gateway"],
        activity_collector_env=envs["gateway"],
        settings_environment=settings,
    )

    manager_starts = [env for name, env in launched if name == "manager"]
    assert [env["LLM_MODEL_ID"] for env in manager_starts] == ["old/model", "new/model"]


def test_a_broken_saved_file_restarts_the_process_with_its_previous_environment(tmp_path, caplog):
    saved_path = _saved_instance(tmp_path)
    settings, envs = _launch(saved_path, dict(CONTAINER_ENV))
    saved_path.write_text("schema_version: [not valid\n", encoding="utf-8")

    with caplog.at_level(logging.ERROR, logger="docker.settings_environment"):
        refreshed = settings.refresh(envs["manager"], process="manager")

    assert refreshed == envs["manager"]
    assert "event=process_settings_environment_refresh_failed process=manager" in caplog.text


def test_the_manager_route_is_saved_without_deployment_details_or_older_settings(tmp_path):
    saved_path = _saved_instance(tmp_path)
    running = GatewayInstanceConfig.load_for_process(saved_path, env=_bundle_launcher_env(saved_path))
    assert running.transport.nats_url == LOCAL_NATS
    _save_while_running(saved_path, model_id="new/model")

    ConfigGatewayManagerRouteWriter(running)("manager_live")

    saved = GatewayInstanceConfig.load_saved(saved_path)
    assert saved.manager.manager_agent_id == "manager_live"
    assert saved.llm.model_id == "new/model"
    assert saved.transport.nats_url == "nats://nats.operator:4333"
    assert running.manager.manager_agent_id == "manager_live"
