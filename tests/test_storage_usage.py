"""People's storage notices: the gateway's side (assistant_gateway.storage_usage).

Measuring. Each repository's per-owner byte totals are written through the
real repositories into real databases: SQLite always, and PostgreSQL when
``LIVE_TEST_POSTGRES_DSN`` is set (``tests/knowledge_support.knowledge_storage``
gives each test its own schema). Texts carry characters that take several
bytes in UTF-8, because SQLite's ``LENGTH`` counts characters and would
under-count them. Covered: chat text by owner (transcripts, requests,
replies; records without an owner left out); uploads counted only while kept
in the uploads folder; conversation turns, summaries and completed tasks (the
manager's share, measured by ``custom_agents.storage_usage_runtime``).

Telling people, on the real shared totals table: the gateway adds its share
to the manager's; each crossing announced once, also across restarts; only
the highest step reached; a drop lowers the announced step so passing it
again is announced again; the step follows the setting and 0 turns notices
off; missing or out-of-date manager totals mean no notice, a warning naming
the folder and a Status finding, except for someone just added; the notice's
words; a failed check is logged and retried; the gateway's wiring tells only
active people through the real access store.
"""

from __future__ import annotations

import asyncio
import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from assistant_gateway.domain.transcript import TranscriptAppendRequest
from assistant_gateway.store import GatewayStateStore
from assistant_gateway.storage_usage import (
    STALE_FINDING_ID,
    StorageUsageNotices,
    StoredData,
    build_storage_usage_notices,
    readable_bytes,
    usage_notice,
    usage_notice_id,
)
from custom_messages.contracts.access_v2 import AccessNoticeKind, AccessNoticeV1, PersonStanding, TeamPersonV1
from custom_messages.contracts.status_v1 import StatusSeverity
from runtime_settings.instance_layout import person_folder_name
from storage.config import resolve_storage_settings
from storage.conversation_repository import create_conversation_repository
from storage.person_storage_usage_repository import (
    CONVERSATION_FILES,
    FOLDER,
    TASKS,
    PersonStorageUsageRepository,
    StorageSide,
)
from storage.task_archive_repository import create_task_archive_repository
from tests.knowledge_support import BACKENDS, knowledge_storage

ALICE = "usr_alice"
BOB = "usr_bob"
# 11 characters, 26 bytes in UTF-8.
WIDE = "Grüße 日本語 ✓"


def _bytes(text: str) -> int:
    return len(text.encode("utf-8"))


def _session(store: GatewayStateStore, owner: str) -> str:
    session_key = f"website:web-chat:{owner}"
    store.session_repository.create_session(
        session_key=session_key,
        conversation_id=f"conversation_{owner}",
        manager_agent_id="manager_1",
        channel_name="website",
        user_id=owner,
        chat_id="web-chat",
        owner_canonical_user_id=owner,
    )
    return session_key


def _transcript(store: GatewayStateStore, session_key: str, owner: str, message_id: str, content: str) -> None:
    store.transcript_repository.append_transcript_message(
        TranscriptAppendRequest(
            message_id=message_id,
            session_key=session_key,
            conversation_id=f"conversation_{owner}",
            owner_canonical_user_id=owner,
            channel_name="website",
            chat_id="web-chat",
            role="user",
            content=content,
            message_type="user_message",
            message_level="normal",
            correlation_id=f"corr_{message_id}",
            source_kind="client_request",
            source_id=f"source_{message_id}",
            idempotency_key=f"idempotency_{message_id}",
        )
    )


@pytest.mark.parametrize("backend", BACKENDS)
def test_chat_text_is_counted_in_bytes_for_each_owner(backend: str, tmp_path: Path) -> None:
    with knowledge_storage(backend, tmp_path) as settings:
        store = GatewayStateStore("", storage_settings=settings)
        alice, bob = _session(store, ALICE), _session(store, BOB)
        _transcript(store, alice, ALICE, "m1", WIDE)
        _transcript(store, alice, ALICE, "m2", "plain")
        _transcript(store, bob, BOB, "m3", "hi")
        store.request_repository.record_request(
            correlation_id="corr_a", session_key=alice, request_kind="chat", request_text=WIDE * 2,
            conversation_id=None, followup_to_task_id=None, owner_canonical_user_id=ALICE,
        )
        # A request without an owner (its session is gone, so none is filled in) belongs to nobody.
        store.request_repository.record_request(
            correlation_id="corr_none", session_key="website:web-chat:gone", request_kind="chat", request_text="x" * 500,
            conversation_id=None, followup_to_task_id=None,
        )
        payload = {"text": WIDE}
        store.delivery_repository.add_deferred_delivery(
            session_key=alice, origin_kind="notification", payload=payload, rendered_text=WIDE,
            available_after="2026-10-27T00:00:00+00:00", owner_canonical_user_id=ALICE,
        )

        counted = store.chat_text_bytes_by_owner()

    delivery = _bytes(WIDE) + _bytes(json.dumps(payload, sort_keys=True))
    assert counted == {
        ALICE: _bytes(WIDE) + _bytes("plain") + 2 * _bytes(WIDE) + delivery,
        BOB: _bytes("hi"),
    }
    assert counted[ALICE] > len(WIDE) * 4 + len("plain")  # bytes, not characters


@pytest.mark.parametrize("backend", BACKENDS)
def test_only_uploads_still_in_the_uploads_folder_count(backend: str, tmp_path: Path) -> None:
    uploads = tmp_path / "instance" / "state" / "uploads"
    with knowledge_storage(backend, tmp_path) as settings:
        store = GatewayStateStore("", storage_settings=settings)
        for attachment_id, owner, path, size in (
            ("att_1", ALICE, uploads / "web" / "a.pdf", 1200),
            ("att_2", ALICE, uploads / "web" / "b.png", 300),
            ("att_3", BOB, uploads / "web" / "c.txt", 40),
            # A copy of a file sent to Alice lives in her folder and counts there.
            ("att_4", ALICE, tmp_path / "instance" / "workspaces" / "users" / ALICE / "scratch" / "sent" / "d.txt", 999),
            # A sibling folder whose name starts like the uploads folder.
            ("att_5", ALICE, tmp_path / "instance" / "state" / "uploads_old" / "e.txt", 777),
        ):
            store.delivery_repository.upsert_attachment(
                attachment_id=attachment_id, session_key="website:web-chat:a", conversation_id=None,
                correlation_id=None, filename=path.name, content_type="text/plain", kind="document",
                size_bytes=size, sha256="0" * 64, local_path=str(path), owner_canonical_user_id=owner,
            )

        counted = store.upload_bytes_by_owner(uploads_dir=str(uploads))

    assert counted == {ALICE: 1500, BOB: 40}


@pytest.mark.parametrize("backend", BACKENDS)
def test_conversation_turns_summaries_and_completed_tasks_count_by_owner(backend: str, tmp_path: Path) -> None:
    with knowledge_storage(backend, tmp_path) as settings:
        conversations = create_conversation_repository(settings, artifacts_dir=tmp_path / "artifacts")
        tasks = create_task_archive_repository(settings)
        conversations.ensure_thread(
            conversation_id="conv_a", team_id="team", owner_agent_id="lead", owner_agent_name="Nova",
            requester_name=None, requester_id=None, root_task_id="task_a", owner_canonical_user_id=ALICE,
        )
        conversations.append_turn(
            conversation_id="conv_a", task_id="task_a", followup_to_task_id=None, owner_agent_id="lead",
            requester_name=None, requester_id=None, correlation_id=None, problem_text="question",
            owner_canonical_user_id=ALICE,
        )
        before = conversations.stored_text_bytes_by_owner()
        conversations.update_turn_completion(
            task_id="task_a", task_status="completed", final_answer_text=WIDE * 3, turn_summary_text=WIDE,
        )
        after = conversations.stored_text_bytes_by_owner()
        tasks.save_completed_task(
            archive_id="arch_a", task_id="task_a", owner_agent_id="lead", owner_agent_name="Nova", team_id="team",
            task_json=json.dumps({"answer": WIDE}, ensure_ascii=False), status="completed",
            owner_canonical_user_id=ALICE,
        )
        tasks.save_completed_task(
            archive_id="arch_none", task_id="task_none", owner_agent_id="lead", owner_agent_name="Nova",
            team_id="team", task_json="{}" * 100, status="completed",
        )
        completed = tasks.stored_text_bytes_by_owner()
        owners = conversations.conversation_owners()

    assert set(before) == {ALICE} and before[ALICE] > _bytes("question")
    assert after[ALICE] - before[ALICE] == 4 * _bytes(WIDE)
    assert completed == {ALICE: _bytes(json.dumps({"answer": WIDE}, ensure_ascii=False))}
    assert owners == {"conv_a": ALICE}


# -- telling people ----------------------------------------------------------------------------

NOW = datetime(2026, 10, 27, 12, 0, tzinfo=timezone.utc)
JOINED = NOW - timedelta(days=30)
GB = 1_000_000_000
TEAM = "team_storage"


class _Notices:
    """The access store's rule for notices from trusted code: one per id (create_notice_if_absent)."""

    def __init__(self) -> None:
        self.stored: dict[str, AccessNoticeV1] = {}

    def __call__(self, notice: AccessNoticeV1) -> bool:
        if notice.notice_id in self.stored:
            return False
        self.stored[notice.notice_id] = notice
        return True


class _Clock:
    def __init__(self) -> None:
        self.now = NOW

    def __call__(self) -> datetime:
        return self.now


class _Team:
    """The gateway's share (chats) is set per person; the manager's (folder) is written as the manager would."""

    def __init__(self, tmp_path: Path, clock: _Clock) -> None:
        self.store = PersonStorageUsageRepository(
            resolve_storage_settings({"backend": "sqlite", "sqlite": {"path": str(tmp_path / "manager.db")}})
        )
        self.clock = clock
        self.chats: dict[str, int] = {}
        self.people = [_person(ALICE), _person(BOB)]

    def measure(self, people: list[str]) -> dict[str, dict[str, int]]:
        return {person: {"chats": self.chats.get(person, 0), "uploads": 0} for person in people}

    def manager_measures(self, *, at: datetime | None = None, **folders: int) -> None:
        usage = {person: {TASKS: 0, CONVERSATION_FILES: 0, FOLDER: size} for person, size in folders.items()}
        self.store.record_side(TEAM, StorageSide.MANAGER, usage, measured_at=at or self.clock())

    def loop(self, notices: _Notices, *, step_gb: int = 5) -> StorageUsageNotices:
        """A gateway; a new one is what a restart builds."""

        return StorageUsageNotices(
            team_id=TEAM,
            step_gb=step_gb,
            people=lambda: self.people,
            measure=self.measure,
            store=lambda: self.store,
            send_notice_once=notices,
            now=self.clock,
        )


def _person(person_id: str, *, standing: PersonStanding = PersonStanding.ACTIVE, changed: datetime = JOINED) -> TeamPersonV1:
    return TeamPersonV1(team_id=TEAM, person_id=person_id, standing=standing, changed_at_utc=changed)


def _titles(notices: _Notices) -> list[str]:
    return [notice.title for notice in notices.stored.values()]


@pytest.fixture
def team(tmp_path: Path) -> _Team:
    return _Team(tmp_path, _Clock())


def test_both_sides_are_added_and_each_crossing_is_announced_once_also_after_a_restart(team: _Team) -> None:
    notices = _Notices()
    loop = team.loop(notices)
    team.manager_measures(usr_alice=3 * GB, usr_bob=1 * GB)
    team.chats[ALICE] = 1 * GB

    assert loop.check() == 0  # 4 GB, below the first step
    team.chats[ALICE] = 2 * GB
    assert loop.check() == 1  # 2 GB of chats and a 3 GB folder: 5 GB
    team.chats[ALICE] = int(4.5 * GB)
    assert loop.check() == 0  # still the 5 GB step
    assert team.loop(notices).check() == 0  # a restart
    team.manager_measures(usr_alice=6 * GB, usr_bob=1 * GB)
    assert team.loop(notices).check() == 1  # 10.5 GB

    assert _titles(notices) == ["The data stored for you passed 5 GB", "The data stored for you passed 10 GB"]
    assert list(notices.stored) == ["storage_usage:usr_alice:5000000000:1", "storage_usage:usr_alice:10000000000:2"]
    [first, second] = notices.stored.values()
    assert first.recipient_person_id == ALICE and first.kind is AccessNoticeKind.STORAGE_USAGE and not first.recipient_roles
    assert "- Your folder: 6 GB" in second.summary and "- Chats (your messages and the replies): 4.5 GB" in second.summary


def test_only_the_highest_step_reached_is_announced(team: _Team) -> None:
    notices = _Notices()
    team.manager_measures(usr_alice=16 * GB)

    team.loop(notices).check()

    assert _titles(notices) == ["The data stored for you passed 15 GB"]


def test_after_a_drop_passing_a_step_again_is_announced_again_once(team: _Team) -> None:
    notices = _Notices()
    loop = team.loop(notices)
    team.manager_measures(usr_alice=11 * GB)
    loop.check()  # 10 GB, announcement 1
    team.manager_measures(usr_alice=7 * GB)  # files deleted from the folder
    assert loop.check() == 0
    assert team.store.announcement(TEAM, ALICE).step_bytes == 5 * GB  # lowered to the step reached
    team.manager_measures(usr_alice=12 * GB)
    assert loop.check() == 1  # 10 GB again, announcement 2
    assert team.loop(notices).check() == 0  # a restart does not repeat it

    assert list(notices.stored) == ["storage_usage:usr_alice:10000000000:1", "storage_usage:usr_alice:10000000000:2"]


@pytest.mark.parametrize("backend", BACKENDS)
def test_disabling_and_reenabling_keeps_notice_progress_across_restarts(backend: str, tmp_path: Path) -> None:
    from storage.access_repository import AccessRepository

    with knowledge_storage(backend, tmp_path) as settings:
        access = AccessRepository(settings)
        store = PersonStorageUsageRepository(settings)
        people = [_person(ALICE)]
        clock = _Clock()

        def checker() -> StorageUsageNotices:
            return StorageUsageNotices(
                team_id=TEAM, step_gb=5, people=lambda: people,
                measure=lambda ids: {person: {"chats": 0, "uploads": 0} for person in ids},
                store=lambda: PersonStorageUsageRepository(settings),
                send_notice_once=access.create_notice_if_absent, now=clock,
            )

        def scan(size: int) -> int:
            store.record_side(TEAM, StorageSide.MANAGER, {ALICE: {"folder": size}}, measured_at=clock())
            return checker().check()

        assert scan(5 * GB) == 1
        original = store.side_usage(TEAM, StorageSide.GATEWAY)[ALICE]
        people[0] = _person(ALICE, standing=PersonStanding.DISABLED)
        clock.now += timedelta(days=4)
        for _ in range(2):
            assert checker().check() == 0
            assert checker().stale_finding() is None
            assert store.announcement(TEAM, ALICE).count == 1
            assert store.side_usage(TEAM, StorageSide.GATEWAY)[ALICE] == original
        people[0] = _person(ALICE, changed=clock())
        assert scan(5 * GB) == 0
        assert scan(0) == 0
        assert scan(5 * GB) == 1
        assert len(access.notices_for(TEAM, person_id=ALICE, roles=())) == 2
        assert store.announcement(TEAM, ALICE).count == 2

        # Removal discards notices too, so its measurement/progress row may go.
        access.forget_person_records(TEAM, ALICE, request_ids=())
        people[0] = _person(ALICE, standing=PersonStanding.REMOVED)
        assert checker().check() == 0
        assert store.side_usage(TEAM, StorageSide.GATEWAY) == {}
        people[0] = _person(ALICE, changed=clock())
        assert scan(5 * GB) == 1
        assert len(access.notices_for(TEAM, person_id=ALICE, roles=())) == 1


def test_a_restart_between_the_notice_and_the_announced_step_announces_once(team: _Team, monkeypatch) -> None:
    notices = _Notices()
    team.manager_measures(usr_alice=5 * GB)
    real = PersonStorageUsageRepository.record_announcement
    monkeypatch.setattr(PersonStorageUsageRepository, "record_announcement", lambda *args, **kwargs: (_ for _ in ()).throw(OSError("stopped")))
    with pytest.raises(OSError):
        team.loop(notices).check()  # the notice is stored, the step is not
    monkeypatch.setattr(PersonStorageUsageRepository, "record_announcement", real)

    assert team.loop(notices).check() == 0  # the same id: no second notice
    assert list(notices.stored) == ["storage_usage:usr_alice:5000000000:1"]
    assert team.store.announcement(TEAM, ALICE).count == 1


def test_the_step_is_configurable_and_zero_turns_the_notices_off(team: _Team) -> None:
    notices = _Notices()
    team.manager_measures(usr_alice=3 * GB)
    loop = team.loop(notices, step_gb=0)
    calls: list[list[str]] = []
    measure = team.measure
    loop._measure = lambda people: calls.append(people) or measure(people)

    assert loop.check() == 0 and calls == []  # nothing is even measured
    assert loop.stale_finding() is None
    loop.step_gb = 2  # changed in Settings while the gateway runs
    assert loop.check() == 1

    [notice] = notices.stored.values()
    assert notice.title == "The data stored for you passed 2 GB" and "passes 4 GB" in notice.summary


def test_missing_or_old_manager_totals_send_no_notice_and_show_on_the_status_page(team: _Team, caplog) -> None:
    notices = _Notices()
    loop = team.loop(notices)
    team.chats[ALICE] = 9 * GB  # enough on the gateway's side alone
    team.chats[BOB] = 9 * GB
    team.manager_measures(usr_alice=0, at=NOW - timedelta(days=4))  # Bob: never measured
    team.people.append(_person("usr_carol", changed=NOW - timedelta(hours=2)))  # just added

    with caplog.at_level("INFO", logger="assistant_gateway.storage_usage"):
        assert loop.check() == 0
    finding = loop.stale_finding()

    assert notices.stored == {}
    assert caplog.text.count("event=storage_usage_totals_stale") == 2
    assert "person_folder=usr_alice manager_measured_at=2026-10-23T12:00:00+00:00" in caplog.text
    assert "person_folder=usr_bob manager_measured_at=never" in caplog.text
    assert "event=storage_usage_not_measured_yet person_folder=usr_carol" in caplog.text
    assert finding is not None and finding.finding_id == STALE_FINDING_ID
    assert finding.severity is StatusSeverity.WARNING and finding.title.startswith("Stored-data totals are out of date for 2")
    assert finding.details == "Manager folders: usr_alice, usr_bob"

    team.manager_measures(usr_alice=0, usr_bob=0, usr_carol=0)  # the manager measures again
    assert loop.stale_finding() is None
    assert loop.check() == 2


def test_failed_gateway_measurement_keeps_old_totals_and_identifies_the_gateway_on_status(team: _Team, caplog) -> None:
    team.people = [_person(ALICE)]
    team.manager_measures(usr_alice=0)
    previous = NOW - timedelta(days=4)
    team.store.record_side(TEAM, StorageSide.GATEWAY, {ALICE: {"chats": 6 * GB}}, measured_at=previous)
    notices = _Notices()
    loop = team.loop(notices)

    def broken(people):
        raise OSError("database is locked")

    loop._measure = broken
    assert asyncio.run(loop.run_once()) is None
    finding = loop.stale_finding()
    assert finding is not None
    assert "gateway" in finding.summary.lower() and "gateway" in finding.action.lower()
    assert finding.details == "Gateway folders: usr_alice"
    assert team.store.side_usage(TEAM, StorageSide.GATEWAY)[ALICE].measured_at == previous
    assert notices.stored == {} and "event=storage_usage_failed" in caplog.text

    loop._measure = team.measure
    team.chats[ALICE] = 6 * GB
    assert loop.check() == 1
    assert loop.stale_finding() is None


@pytest.mark.parametrize("joined_ago", [timedelta(days=30), timedelta(hours=2)])
def test_missing_gateway_measurements_allow_startup_and_new_person_grace(team: _Team, joined_ago: timedelta) -> None:
    team.people = [_person(ALICE, changed=NOW - joined_ago)]
    team.manager_measures(usr_alice=0)
    loop = team.loop(_Notices())
    assert loop.stale_finding() is None, "the first gateway measurement waits 15 minutes"

    team.clock.now += timedelta(minutes=16)
    assert (loop.stale_finding() is not None) == (joined_ago > timedelta(days=3))

    team.clock.now += timedelta(days=4)
    team.manager_measures(usr_alice=0)
    finding = loop.stale_finding()
    assert finding is not None and finding.details == "Gateway folders: usr_alice"


def test_the_notice_says_what_is_stored_by_kind_and_when_the_next_one_comes() -> None:
    data = StoredData(chats=2 * GB, uploads=100_000_000, tasks=1_800_000_000, conversation_files=400_000_000, folder=6_100_000_000)

    notice = usage_notice(
        team_id=TEAM, person_id=ALICE, data=data, reached_bytes=10 * GB, step_bytes=5 * GB, number=3, now=NOW
    )

    assert notice.notice_id == "storage_usage:usr_alice:10000000000:3"
    assert notice.title == "The data stored for you passed 10 GB"
    assert notice.summary.splitlines()[:6] == [
        "The team now stores about 10.4 GB for you:",
        "- Your folder: 6.1 GB",
        "- Chats (your messages and the replies): 2 GB",
        "- Completed tasks and conversation records: 1.8 GB",
        "- Long texts saved with your conversations: 400 MB",
        "- Files you sent in chat: 100 MB",
    ]
    assert "Chats and completed tasks are kept. You get the next notice when your stored data passes 15 GB." in notice.summary
    assert "/webchat?view=files" in notice.summary
    assert [readable_bytes(size) for size in (0, 999, 1500, 5 * GB, 12_345_678_901_234)] == [
        "0 bytes", "999 bytes", "1.5 kB", "5 GB", "12.3 TB",
    ]


def test_a_person_whose_id_is_not_a_folder_name_gets_a_short_stable_notice_id() -> None:
    person = "Jordan Example / Ops"

    first, again = usage_notice_id(person, 5 * GB, 1), usage_notice_id(person, 5 * GB, 1)

    assert first == again == f"storage_usage:{person_folder_name(person)}:5000000000:1"
    assert len(usage_notice_id(person, 10_000 * GB, 99_999)) <= 120 and "/" not in first.split(":", 1)[1]


def test_a_failed_check_is_logged_and_the_loop_keeps_running(team: _Team, caplog) -> None:
    def broken(people: list[str]) -> dict[str, dict[str, int]]:
        raise RuntimeError("database is locked")

    loop = StorageUsageNotices(
        team_id=TEAM, step_gb=5, people=lambda: team.people, measure=broken, store=lambda: team.store,
        send_notice_once=_Notices(), interval=timedelta(milliseconds=10), first_check_delay=timedelta(milliseconds=1),
    )

    async def run_briefly() -> None:
        stop = asyncio.Event()
        task = asyncio.create_task(loop.run(stop))
        await asyncio.sleep(0.1)
        stop.set()
        await asyncio.wait_for(task, timeout=2)

    with caplog.at_level("ERROR", logger="assistant_gateway.storage_usage"):
        asyncio.run(run_briefly())

    assert caplog.text.count("event=storage_usage_failed") >= 2


def test_the_gateway_tells_active_people_through_the_access_store(tmp_path: Path, monkeypatch) -> None:
    """The gateway's own wiring, on real SQLite stores: only active people are measured and told, once."""

    import assistant_gateway.storage_usage as storage_usage
    from assistant_gateway.config import GatewayInstanceConfig
    from storage.access_repository import AccessRepository

    monkeypatch.setattr(storage_usage, "GIGABYTE", 10)  # a step of 1 "GB" is 10 bytes here
    config = GatewayInstanceConfig.create(
        instance_name="storage", instance_root=str(tmp_path / "instance"), team_id=TEAM, timezone="UTC"
    )
    config.gateway.storage_warnings.person_step_gb = 2
    access = AccessRepository(config.manager_storage_settings)
    for person, standing in ((ALICE, PersonStanding.ACTIVE), (BOB, PersonStanding.DISABLED)):
        access.record_person(TeamPersonV1(team_id=TEAM, person_id=person, standing=standing, changed_at_utc=JOINED))
    chat = GatewayStateStore("", storage_settings=config.state_storage_settings)
    for owner in (ALICE, BOB):
        session = _session(chat, owner)
        _transcript(chat, session, owner, f"m_{owner}", "x" * 25)
    shared = PersonStorageUsageRepository(config.manager_storage_settings)
    shared.record_side(TEAM, StorageSide.MANAGER, {ALICE: {FOLDER: 0}, BOB: {FOLDER: 0}}, measured_at=datetime.now(timezone.utc))
    loop = build_storage_usage_notices(
        config, chat=chat, access_store=lambda: access, send_notice_once=access.create_notice_if_absent
    )

    assert loop.check() == 1
    assert loop.check() == 0

    [(notice, read)] = access.notices_for(TEAM, person_id=ALICE, roles=())
    assert notice.title == "The data stored for you passed 2 GB" and read is False
    assert access.notices_for(TEAM, person_id=BOB, roles=()) == []
    assert set(shared.side_usage(TEAM, StorageSide.GATEWAY)) == {ALICE}  # only active people are recorded
    assert shared.side_usage(TEAM, StorageSide.GATEWAY)[ALICE].bytes_by_kind == {"chats": 25, "uploads": 0}
