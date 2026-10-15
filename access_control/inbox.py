"""The access store's side of the Inbox.

A waiting access request *is* its review item: people list waiting requests
from the access store, and the gateway announces each one once
(``AccessRepository.claim_unannounced``). Notices are stored for the people
they are meant for and are silent (decision D3): the Inbox shows them, and
nothing is pushed to a chat. When a request is decided, ``on_finished`` tells
the waiting agent (the gateway publishes ``AccessRequestResolvedV1``); a
request withdrawn by the runtime needs no message, because its task ended.
"""

from __future__ import annotations

from typing import Callable

from custom_messages.contracts.access_v2 import AccessNoticeV1, AccessRequestV1
from storage.access_repository import AccessRepository


class StoredAccessInbox:
    def __init__(
        self, store: AccessRepository, *, on_finished: Callable[[AccessRequestV1], None] | None = None
    ) -> None:
        self.store = store
        self.on_finished = on_finished

    def open_review(self, request: AccessRequestV1) -> None:
        """Nothing beyond the stored request; the gateway announces it."""

    def send_notice(self, notice: AccessNoticeV1) -> None:
        self.store.create_notice(notice)

    def request_finished(self, request: AccessRequestV1) -> None:
        if self.on_finished is not None:
            self.on_finished(request)


__all__ = ["StoredAccessInbox"]
