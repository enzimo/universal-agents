"""Bucket listings and watches decide where the first values end from the stream's own key list.

No server: a fake JetStream context hands the test the subscription's callback, and the
test sends messages through it in the order a server could. The live checks against
nats-server 2.15.1 under concurrent writers are in test_live_bucket_reads.py.
"""

from __future__ import annotations

import asyncio
import logging
from contextlib import aclosing
from types import SimpleNamespace
from typing import Any, Iterable, Optional, Union

import pytest
from nats.errors import ConnectionClosedError, TimeoutError as NatsTimeoutError
from nats.js.api import DeliverPolicy

from custom_transport.protocol1 import bucket_reads
from custom_transport.protocol1.bucket_reads import (
    BucketReadTimedOut, BucketTooLarge, KeyChange, list_keys, watch_keys,
)

BUCKET = "UA1_EXAMPLE"
PREFIX = f"$KV.{BUCKET}."
# What one stream information request answers: the keys the stream holds, or an error.
Reply = Union[Iterable[str], BaseException]


class FakeSubscription:
    def __init__(self, error: Optional[BaseException] = None) -> None:
        self.unsubscribed = 0
        self.error = error

    async def unsubscribe(self) -> None:
        self.unsubscribed += 1
        if self.error is not None:
            raise self.error


class FakeJetStream:
    """The two requests a bucket read makes; the test sends the subscription's messages itself.

    Each stream information request takes the next reply; the last one repeats.
    """

    def __init__(self, *replies: Reply, unsubscribe_error: Optional[BaseException] = None) -> None:
        self.replies = list(replies)
        self.subscription = FakeSubscription(unsubscribe_error)
        self.subscribed: list[tuple[str, dict[str, Any]]] = []
        self.asked: list[tuple[str, Optional[str]]] = []
        self.callback: Any = None
        self.answered = asyncio.Event()

    async def subscribe(self, subject: str, **options: Any) -> FakeSubscription:
        self.subscribed.append((subject, options))
        self.callback = options["cb"]
        return self.subscription

    async def stream_info(self, stream: str, subjects_filter: Optional[str] = None) -> Any:
        self.asked.append((stream, subjects_filter))
        reply = self.replies.pop(0) if len(self.replies) > 1 else self.replies[0]
        self.answered.set()
        if isinstance(reply, BaseException):
            raise reply
        subjects = {f"{PREFIX}{key}": 1 for key in reply}
        return SimpleNamespace(state=SimpleNamespace(subjects=subjects or None))

    async def send(self, key: str, value: bytes = b"v", *, headers: Optional[dict[str, str]] = None) -> None:
        await self.callback(SimpleNamespace(subject=f"{PREFIX}{key}", data=value, headers=headers))

    async def delete(self, key: str, operation: str = "DEL") -> None:
        await self.send(key, b"", headers={"KV-Operation": operation})


async def settle() -> None:
    """Let every ready task run until it waits on something the test has not sent yet."""

    for _ in range(20):
        await asyncio.sleep(0)


async def listing(js: FakeJetStream, prefix: str = "") -> asyncio.Task[list[str]]:
    """A listing running in a task that has asked the stream and now waits for messages."""

    task = asyncio.create_task(list_keys(js, BUCKET, prefix))
    await js.answered.wait()
    await settle()
    return task


async def first_values(js: FakeJetStream) -> asyncio.Task[list[Optional[KeyChange]]]:
    """A watch running in a task that collects its changes up to the end of its first values."""

    async def read() -> list[Optional[KeyChange]]:
        seen: list[Optional[KeyChange]] = []
        async with aclosing(watch_keys(js, BUCKET)) as changes:
            async for change in changes:
                seen.append(change)
                if change is None:
                    break
        return seen

    task = asyncio.create_task(read())
    await js.answered.wait()
    await settle()
    return task


@pytest.fixture
def short_waits(monkeypatch: pytest.MonkeyPatch) -> pytest.MonkeyPatch:
    """A quiet period of a hundredth of a second and a half-second deadline, so the tests that reach them are quick."""

    monkeypatch.setattr(bucket_reads, "QUIET_SECONDS", 0.01)
    monkeypatch.setattr(bucket_reads, "FIRST_VALUES_SECONDS", 0.5)
    return monkeypatch


@pytest.mark.asyncio
async def test_a_listing_waits_for_every_key_the_stream_held_even_after_a_message_the_server_marked_last() -> None:
    # The server can stamp "0 pending" on b after a rewrite of c (cause 2); the stream
    # still holds c, so the listing must not end before c.
    js = FakeJetStream(["a", "b", "c"])
    task = await listing(js)
    await js.send("a")
    await js.send("b")
    await settle()
    assert not task.done()
    await js.send("c")
    assert await task == ["a", "b", "c"]
    assert js.subscription.unsubscribed == 1


@pytest.mark.asyncio
async def test_messages_that_arrive_after_the_stream_answered_are_all_first_values() -> None:
    # nats-py's race (cause 1): the answer about the consumer came before every message.
    js = FakeJetStream(["a", "b", "c"])
    task = await first_values(js)
    assert not task.done()
    for key in ("a", "b", "c"):
        await js.send(key, key.encode())
    assert await task == [KeyChange("a", b"a"), KeyChange("b", b"b"), KeyChange("c", b"c"), None]


@pytest.mark.asyncio
async def test_messages_sent_before_the_stream_answered_count_toward_the_first_values() -> None:
    js = FakeJetStream(["a", "b"])
    original = js.stream_info

    async def answer_after_a_message(stream: str, subjects_filter: Optional[str] = None) -> Any:
        await js.send("a")
        return await original(stream, subjects_filter)

    js.stream_info = answer_after_a_message  # type: ignore[method-assign]
    task = await listing(js)
    await js.send("b")
    assert await task == ["a", "b"]


@pytest.mark.asyncio
async def test_an_empty_bucket_ends_the_first_values_at_once_and_later_writes_follow() -> None:
    js = FakeJetStream([])
    async with aclosing(watch_keys(js, BUCKET)) as changes:
        assert await anext(changes) is None
        await js.send("late", b"x")
        assert await anext(changes) == KeyChange("late", b"x")
    assert js.subscription.unsubscribed == 1
    assert await list_keys(FakeJetStream([]), BUCKET) == []


@pytest.mark.asyncio
@pytest.mark.parametrize("headers", [
    {"KV-Operation": "DEL"},
    {"KV-Operation": "PURGE"},
    {"Nats-Marker-Reason": "MaxAge"},
    {"Nats-Marker-Reason": "Purge"},
    {"Nats-Marker-Reason": "Remove"},
])
async def test_a_deleted_key_ends_the_wait_for_it_and_is_left_out_of_a_listing(headers: dict[str, str]) -> None:
    js = FakeJetStream(["a", "gone"])
    task = await listing(js)
    await js.send("a")
    await js.send("gone", b"", headers=headers)
    assert await task == ["a"]

    js = FakeJetStream(["gone"])
    task = await first_values(js)
    await js.send("gone", b"", headers=headers)
    assert await task == [KeyChange("gone", None), None]


@pytest.mark.asyncio
async def test_a_key_deleted_during_a_listing_is_left_out_and_one_written_again_is_listed_once() -> None:
    js = FakeJetStream(["a", "b", "c"])
    task = await listing(js)
    await js.send("a")
    await js.send("b")
    await js.delete("a")  # a later change of a, after the listing passed it
    await js.send("b")  # b rewritten: listed once
    await js.send("c")
    assert await task == ["b", "c"]


@pytest.mark.asyncio
async def test_an_unknown_server_marker_is_skipped_with_a_warning_but_ends_the_wait_for_its_key(
        caplog: pytest.LogCaptureFixture) -> None:
    js = FakeJetStream(["a", "odd"])
    caplog.set_level(logging.WARNING, logger=bucket_reads.__name__)
    task = await listing(js)
    await js.send("a")
    await js.send("odd", b"", headers={"Nats-Marker-Reason": "SomethingNew"})
    assert await task == ["a"]
    assert "event=bucket_read_marker_unknown" in caplog.text and "key=odd reason=SomethingNew" in caplog.text


@pytest.mark.asyncio
async def test_a_key_the_stream_dropped_by_age_is_no_longer_awaited(
        short_waits: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture) -> None:
    # The first answer holds "aged"; when it never arrives, the stream is asked again and no longer holds it.
    js = FakeJetStream(["a", "aged"], ["a"])
    caplog.set_level(logging.INFO, logger=bucket_reads.__name__)
    task = await listing(js)
    await js.send("a")
    assert await task == ["a"]
    assert len(js.asked) == 2
    assert "event=bucket_read_keys_expired" in caplog.text and "dropped=1" in caplog.text


@pytest.mark.asyncio
async def test_a_key_the_stream_still_holds_is_awaited_after_a_quiet_period(short_waits: pytest.MonkeyPatch) -> None:
    js = FakeJetStream(["a", "slow"])
    task = await listing(js)
    await js.send("a")
    await asyncio.sleep(0.03)  # longer than the quiet period: the stream is asked again
    assert not task.done() and len(js.asked) >= 2
    await js.send("slow")
    assert await task == ["a", "slow"]


@pytest.mark.asyncio
async def test_a_listing_whose_keys_never_arrive_raises_and_unsubscribes(short_waits: pytest.MonkeyPatch) -> None:
    short_waits.setattr(bucket_reads, "FIRST_VALUES_SECONDS", 0.05)
    js = FakeJetStream(["a", "never"])
    task = await listing(js)
    await js.send("a")
    with pytest.raises(BucketReadTimedOut) as raised:
        await task
    assert (raised.value.bucket, raised.value.awaited, raised.value.received) == (BUCKET, 1, 1)
    assert raised.value.subject == f"{PREFIX}>"
    assert js.subscription.unsubscribed == 1


@pytest.mark.asyncio
async def test_a_watch_whose_first_values_never_end_raises(short_waits: pytest.MonkeyPatch) -> None:
    short_waits.setattr(bucket_reads, "FIRST_VALUES_SECONDS", 0.05)
    js = FakeJetStream(["never"])
    with pytest.raises(BucketReadTimedOut):
        async with aclosing(watch_keys(js, BUCKET)) as changes:
            async for _change in changes:
                pass
    assert js.subscription.unsubscribed == 1


@pytest.mark.asyncio
async def test_the_readers_own_time_between_changes_does_not_count_toward_the_deadline(
        short_waits: pytest.MonkeyPatch) -> None:
    short_waits.setattr(bucket_reads, "FIRST_VALUES_SECONDS", 0.05)
    js = FakeJetStream(["a", "b"])
    seen: list[Optional[KeyChange]] = []

    async def read() -> None:
        async with aclosing(watch_keys(js, BUCKET)) as changes:
            async for change in changes:
                seen.append(change)
                if change is None:
                    return
                await asyncio.sleep(0.1)  # longer than the deadline: verifying a record, fetching a certificate

    task = asyncio.create_task(read())
    await js.answered.wait()
    await js.send("a")
    await js.send("b")
    await task
    assert seen == [KeyChange("a", b"v"), KeyChange("b", b"v"), None]


@pytest.mark.asyncio
async def test_a_reply_that_lists_as_many_subjects_as_the_server_allows_raises(
        monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(bucket_reads, "MAX_SUBJECTS", 3)
    js = FakeJetStream(["a", "b", "c"])
    with pytest.raises(BucketTooLarge):
        await list_keys(js, BUCKET)
    assert js.subscription.unsubscribed == 1


def test_the_subject_limit_is_the_servers() -> None:
    assert bucket_reads.MAX_SUBJECTS == 100_000  # nats-server JSMaxSubjectDetails


@pytest.mark.asyncio
async def test_a_failed_stream_information_request_raises_and_unsubscribes() -> None:
    js = FakeJetStream(NatsTimeoutError())
    with pytest.raises(NatsTimeoutError):
        await list_keys(js, BUCKET)
    assert js.subscription.unsubscribed == 1


@pytest.mark.asyncio
async def test_closing_or_cancelling_a_watch_unsubscribes() -> None:
    js = FakeJetStream([])
    changes = watch_keys(js, BUCKET)
    assert await anext(changes) is None
    await changes.aclose()
    assert js.subscription.unsubscribed == 1

    js = FakeJetStream(["a"])

    async def read() -> None:
        async with aclosing(watch_keys(js, BUCKET)) as changes:
            async for _change in changes:
                pass

    task = asyncio.create_task(read())
    await js.answered.wait()
    await settle()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert js.subscription.unsubscribed == 1


@pytest.mark.asyncio
async def test_a_closed_connection_does_not_hide_the_reads_own_result() -> None:
    js = FakeJetStream(["a"], unsubscribe_error=ConnectionClosedError())
    task = await listing(js)
    await js.send("a")
    assert await task == ["a"]


@pytest.mark.asyncio
async def test_listings_and_watches_subscribe_as_nats_py_s_watch_does() -> None:
    js = FakeJetStream([])
    await list_keys(js, BUCKET)
    async with aclosing(watch_keys(js, BUCKET, "team_one.>")) as changes:
        await anext(changes)
    (listing_subject, listing_options), (watch_subject, watch_options) = js.subscribed
    common = {"stream": f"KV_{BUCKET}", "ordered_consumer": True, "deliver_policy": DeliverPolicy.LAST_PER_SUBJECT}
    assert listing_subject == f"{PREFIX}>" and watch_subject == f"{PREFIX}team_one.>"
    assert {name: listing_options[name] for name in common} == common
    assert {name: watch_options[name] for name in common} == common
    # A listing reads headers only and its consumer leaves the server soon after; a watch's stays.
    assert (listing_options["headers_only"], listing_options["inactive_threshold"]) == (True, None)
    assert (watch_options["headers_only"], watch_options["inactive_threshold"]) == (False, 300.0)
    # The stream is asked under the subscription's own filter.
    assert js.asked == [(f"KV_{BUCKET}", f"{PREFIX}>"), (f"KV_{BUCKET}", f"{PREFIX}team_one.>")]


@pytest.mark.asyncio
@pytest.mark.parametrize(("prefix", "subject"), [
    ("", f"{PREFIX}>"),
    ("team_one.", f"{PREFIX}team_one.>"),
    ("session.install-1.team_one.", f"{PREFIX}session.install-1.team_one.>"),
    ("team_one.agent-", f"{PREFIX}>"),  # not whole tokens: the whole bucket, then filtered
    ("team.*.", f"{PREFIX}>"),  # a wildcard never widens or narrows the server's filter
    ("team.>.", f"{PREFIX}>"),
    (".team.", f"{PREFIX}>"),
    ("team..", f"{PREFIX}>"),
])
async def test_a_prefix_of_whole_tokens_narrows_the_servers_filter(prefix: str, subject: str) -> None:
    js = FakeJetStream([])
    await list_keys(js, BUCKET, prefix)
    assert js.subscribed[0][0] == subject
    assert js.asked[0][1] == subject


@pytest.mark.asyncio
async def test_a_listing_keeps_only_keys_that_start_with_the_prefix() -> None:
    js = FakeJetStream(["team_one.agent-1", "team_one.lead", "team_one.agent-2"])
    task = await listing(js, "team_one.agent-")
    for key in ("team_one.agent-1", "team_one.lead", "team_one.agent-2"):
        await js.send(key)
    assert await task == ["team_one.agent-1", "team_one.agent-2"]
