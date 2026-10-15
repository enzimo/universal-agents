import logging

from assistant_gateway.config import GatewayInstanceConfig
from assistant_gateway.gateway import UserGateway
from assistant_gateway.store import GatewayStateStore
from custom_agents.task_manager import TaskManager
from storage.config import resolve_storage_settings


class _FakePostgresCursor:
    def __init__(self, connection: "_FakePostgresConnection") -> None:
        self._connection = connection
        self.description = None
        self.rowcount = 1
        self._row = None

    def execute(self, query, params=None) -> None:
        if "SELECT 1" in query:
            self.description = [("?column?",)]
            self._row = (1,)
            return
        if "INSERT INTO gateway_user_team_memberships" in query:
            self._connection.membership = {
                "canonical_user_id": params[0],
                "team_id": params[1],
                "role": params[2],
                "status": params[3],
                "created_at_utc": params[4],
                "updated_at_utc": params[5],
            }
        elif "SELECT canonical_user_id, team_id, role, status, created_at_utc, updated_at_utc" in query:
            self.description = [
                ("canonical_user_id",),
                ("team_id",),
                ("role",),
                ("status",),
                ("created_at_utc",),
                ("updated_at_utc",),
            ]
            membership = self._connection.membership
            self._row = None if membership is None else tuple(membership[column[0]] for column in self.description)
            return
        elif "INSERT INTO gateway_user_channel_bindings" in query:
            self._connection.binding = {
                "binding_id": params[0],
                "canonical_user_id": params[1],
                "channel_name": params[2],
                "external_user_id": params[3],
                "chat_scope": params[4],
                "status": "active",
                "created_at_utc": params[5],
                "updated_at_utc": params[6],
                "last_seen_at_utc": params[7],
                "replaced_at_utc": None,
                "replaced_by_binding_id": None,
                "metadata_json": params[8],
            }
        elif (
            "SELECT binding_id, canonical_user_id, channel_name, external_user_id, chat_scope" in query
            and "WHERE binding_id = " in query
        ):
            self.description = [
                ("binding_id",),
                ("canonical_user_id",),
                ("channel_name",),
                ("external_user_id",),
                ("chat_scope",),
                ("status",),
                ("created_at_utc",),
                ("updated_at_utc",),
                ("last_seen_at_utc",),
                ("replaced_at_utc",),
                ("replaced_by_binding_id",),
                ("metadata_json",),
            ]
            binding = self._connection.binding
            self._row = None if binding is None else tuple(binding[column[0]] for column in self.description)
            return
        self.description = None
        self._row = None

    def fetchone(self):
        return self._row

    def fetchall(self):
        return []


class _FakePostgresConnection:
    def __init__(self) -> None:
        self.membership = None
        self.binding = None

    def cursor(self):
        return _FakePostgresCursor(self)

    def commit(self) -> None:
        return None

    def rollback(self) -> None:
        return None

    def close(self) -> None:
        return None


def test_task_manager_accepts_postgres_when_driver_is_available(tmp_path, monkeypatch) -> None:
    settings = resolve_storage_settings(
        {
            "backend": "postgres",
            "postgres": {"dsn": "postgresql://team:secret@db/team_agents"},
        },
        base_dir=str(tmp_path),
    )
    monkeypatch.setattr("storage.postgres_connection._connect_psycopg", lambda dsn: _FakePostgresConnection())

    manager = TaskManager(
        db_path="ignored.db",
        logger=logging.getLogger("task_manager_test"),
        storage_settings=settings,
    )

    assert manager.storage_settings == settings
    assert manager.runtime_state_store._storage_settings == settings


def test_gateway_constructs_only_gateway_postgres_storage_from_config(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr("storage.postgres_connection._connect_psycopg", lambda dsn: _FakePostgresConnection())
    config = GatewayInstanceConfig.create(
        instance_name="test_instance",
        instance_root=str(tmp_path / "instance"),
        team_id="team_test",
        timezone="America/Chicago",
        manager_agent_id="manager_1",
        manager_storage_backend="postgres",
        manager_storage_postgres_dsn="postgresql://team:secret@db/team_agents",
    )

    gateway = UserGateway(config, output_sink=None)

    assert gateway._state_storage_settings.backend == "postgres"
    assert not hasattr(gateway, "_manager_storage_settings")
    assert not hasattr(gateway, "_schedule_store")
    assert not hasattr(gateway, "_heartbeat_store")


def test_gateway_state_store_accepts_postgres_when_driver_is_available(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr("storage.postgres_connection._connect_psycopg", lambda dsn: _FakePostgresConnection())
    settings = resolve_storage_settings(
        {
            "backend": "postgres",
            "postgres": {"dsn": "postgresql://team:secret@db/team_agents"},
        },
        base_dir=str(tmp_path),
    )

    store = GatewayStateStore(
        db_path="ignored.db",
        logger_=logging.getLogger("gateway_state_store_test"),
        storage_settings=settings,
    )

    assert store._storage_settings == settings
