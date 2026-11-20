"""The bundled NATS server's store folder reaches the Status page only from that server's own start.

``start_local_nats`` sets ``BUNDLED_NATS_STORE_DIR_ENV`` in the environment the runtime's
processes get; the gateway measures the free space on that folder's disk to tell whether the
server will have room for what its streams set aside. A value from anywhere else, or one beside
another server's address, must not count: the page would measure a folder that server does not
use. The launchers' own hand-off is tested in test_docker_run_bundle and
test_docker_launch_headless.
"""

from __future__ import annotations

import logging
from pathlib import Path

import pytest

from docker.nats_service import (
    BUNDLED_NATS_STORE_DIR_ENV,
    LOCAL_NATS_ENABLED_ENV,
    bundled_nats_store_dir,
    drop_supplied_bundled_store_dir,
    local_nats_url,
)

LOCAL = local_nats_url()
STORE = "/data/services/nats/data"


def _env(**values: str) -> dict[str, str]:
    return {"NATS_URL": LOCAL, "NATS_SERVERS": LOCAL, BUNDLED_NATS_STORE_DIR_ENV: STORE, **values}


def test_the_folder_counts_beside_the_bundled_servers_address(caplog: pytest.LogCaptureFixture) -> None:
    # Under the bundle the headless launcher runs with the local start turned off; the address decides.
    with caplog.at_level(logging.WARNING):
        assert bundled_nats_store_dir(_env(**{LOCAL_NATS_ENABLED_ENV: "false"})) == Path(STORE)
    assert not caplog.records


def test_no_folder_gives_none_without_a_log_line(caplog: pytest.LogCaptureFixture) -> None:
    with caplog.at_level(logging.WARNING):
        assert bundled_nats_store_dir({"NATS_URL": LOCAL, "NATS_SERVERS": LOCAL}) is None
        assert bundled_nats_store_dir(_env(**{BUNDLED_NATS_STORE_DIR_ENV: "  "})) is None
    assert not caplog.records


@pytest.mark.parametrize(
    "values",
    [
        # A value left in the container's environment of an instance that uses its own server.
        {LOCAL_NATS_ENABLED_ENV: "false", "NATS_URL": "nats://nats.example.test:4222",
         "NATS_SERVERS": "nats://nats.example.test:4222"},
        {"NATS_SERVERS": f"{LOCAL},nats://nats.example.test:4222"},
        {"NATS_SERVERS": ""},
        {"NATS_URL": "nats://user:secret@127.0.0.1:4222"},
        {BUNDLED_NATS_STORE_DIR_ENV: "services/nats/data"},
    ],
    ids=["external-server", "another-server-too", "no-servers", "other-url", "relative-folder"],
)
def test_a_folder_beside_another_address_or_not_absolute_is_ignored_and_named(
    values: dict[str, str], caplog: pytest.LogCaptureFixture
) -> None:
    with caplog.at_level(logging.WARNING):
        assert bundled_nats_store_dir(_env(**values)) is None
    [record] = caplog.records
    assert "event=bundled_nats_marker_ignored" in record.getMessage()
    # An operator's address can carry a password, so no address of theirs is logged.
    assert "secret" not in record.getMessage() and "example.test" not in record.getMessage()


def test_a_supplied_folder_is_dropped_and_named(caplog: pytest.LogCaptureFixture) -> None:
    logger = logging.getLogger("test_bundled_nats_store_dir")
    values = {BUNDLED_NATS_STORE_DIR_ENV: STORE, "OTHER": "kept"}
    with caplog.at_level(logging.WARNING, logger=logger.name):
        drop_supplied_bundled_store_dir(values, source="team.env", logger=logger)
        drop_supplied_bundled_store_dir(values, source="team.env", logger=logger)
    assert values == {"OTHER": "kept"}
    [record] = caplog.records  # nothing left to drop the second time, so nothing said
    assert "event=bundled_nats_marker_dropped source=team.env" in record.getMessage()
