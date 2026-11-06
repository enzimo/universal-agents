"""Live NATS tests remove the streams they created on the shared server (tests.live_nats_support)."""

from __future__ import annotations

import nats
import pytest
from nats.js.api import ObjectStoreConfig, StorageType, StreamConfig

from id_generation import new_id_suffix
from tests.live_nats_client import nats_transport_config_from_env
from tests.live_nats_support import deleting_created_streams

pytestmark = [pytest.mark.live, pytest.mark.nats, pytest.mark.asyncio]


async def test_a_failing_test_removes_what_it_created_and_nothing_that_was_there_before() -> None:
    config = nats_transport_config_from_env()
    connection = await nats.connect(servers=config.servers, user=config.user, password=config.password)
    jetstream = connection.jetstream()
    suffix = new_id_suffix().upper()
    another_runs = f"CLEANUP_OTHER_{suffix}"
    stream = StreamConfig(name=another_runs, subjects=[f"cleanup.other.{suffix}"], storage=StorageType.MEMORY)
    await jetstream.add_stream(stream)
    try:
        with pytest.raises(RuntimeError, match="the test failed"):
            with deleting_created_streams() as created:
                # Creating a stream that already exists is not creating it.
                await jetstream.add_stream(stream)
                await jetstream.add_stream(
                    name=f"CLEANUP_OWN_{suffix}", subjects=[f"cleanup.own.{suffix}"], storage=StorageType.MEMORY
                )
                await jetstream.create_key_value(bucket=f"cleanup_{suffix}", storage=StorageType.MEMORY)
                await jetstream.create_object_store(
                    bucket=f"cleanup_{suffix}", config=ObjectStoreConfig(storage=StorageType.MEMORY)
                )
                raise RuntimeError("the test failed")

        assert created == [f"CLEANUP_OWN_{suffix}", f"KV_cleanup_{suffix}", f"OBJ_cleanup_{suffix}"]
        left = {info.config.name for info in await jetstream.streams_info()}
        assert another_runs in left
        assert not left & set(created)
    finally:
        await jetstream.delete_stream(another_runs)
        await connection.close()
