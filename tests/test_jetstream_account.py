"""The account information reader the Status page and the installation authority share (custom_transport.jetstream_account).

Covered: one raw ``$JS.API.INFO`` request with its timeout, the reply returned as the server
sent it, a refusal raised as its nats-py error kind, and byte figures read so that a figure
the server left out, or sent malformed, is None and never 0. The live tests read the figure
from nats-server 2.15.1 (``test_storage_capacity``, ``test_live_queue_streams``).
"""

from __future__ import annotations

import asyncio
import json
from types import SimpleNamespace
from typing import Any

import pytest
from nats.js.errors import APIError, ServiceUnavailableError

from custom_transport.jetstream_account import (
    ACCOUNT_INFO_SUBJECT,
    ACCOUNT_INFO_TIMEOUT_SECONDS,
    reported_bytes,
    request_account_info,
)


class _Connection:
    def __init__(self, reply: dict[str, Any]) -> None:
        self.reply = reply
        self.requests: list[tuple[str, bytes, float]] = []

    async def request(self, subject: str, data: bytes, timeout: float) -> Any:
        self.requests.append((subject, data, timeout))
        return SimpleNamespace(data=json.dumps(self.reply).encode())


def test_the_account_information_is_one_raw_request_returned_as_the_server_sent_it() -> None:
    reply = {"storage": 10, "reserved_storage": 5 << 20, "limits": {"max_storage": -1}, "api": {"total": 3}}
    connection = _Connection(reply)

    assert asyncio.run(request_account_info(connection)) == reply
    assert connection.requests == [(ACCOUNT_INFO_SUBJECT, b"", ACCOUNT_INFO_TIMEOUT_SECONDS)]
    assert ACCOUNT_INFO_SUBJECT == "$JS.API.INFO"


@pytest.mark.parametrize(("code", "kind"), [(503, ServiceUnavailableError), (400, APIError)])
def test_a_refusal_raises_its_error_kind_rather_than_reading_as_an_account(code: int, kind: type) -> None:
    error = {"code": code, "err_code": 10039, "description": "jetstream not enabled for account"}

    with pytest.raises(kind) as raised:
        asyncio.run(request_account_info(_Connection({"type": "io.nats.jetstream.api.v1.account_info_response",
                                                      "error": error})))
    assert raised.value.err_code == 10039


@pytest.mark.parametrize(("part", "expected"), [
    ({"reserved_storage": 5_242_880}, 5_242_880),
    ({"reserved_storage": 0}, 0),  # nothing set aside, which is not the same as not reported
    ({}, None),
    ({"reserved_storage": None}, None),
    ({"reserved_storage": -1}, None),
    ({"reserved_storage": True}, None),
    ({"reserved_storage": "5242880"}, None),
    ({"reserved_storage": 5.0}, None),
])
def test_a_figure_the_server_left_out_or_sent_malformed_is_unknown_never_zero(part: dict, expected: int | None) -> None:
    assert reported_bytes(part, "reserved_storage") == expected
