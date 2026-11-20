"""The NATS account's JetStream information, read one way for the Status page and the installation authority.

nats-py's ``AccountInfo`` drops ``reserved_storage`` and ``reserved_memory``: what the
account's streams set aside with their byte limits when they were created, which NATS
checks every new or changed stream against. So the Status page's storage check
(``custom_transport.nats_adapter.NatsTransportAdapter.jetstream_account_usage``) and the
installation authority's start checks (``installation_authority.queues``) both send the raw
``$JS.API.INFO`` request with ``request_account_info`` and read its byte figures with
``reported_bytes``. This module opens no connection of its own and imports neither of them,
so the authority does not load the protocol 0 adapter and the gateway does not load the
authority.

Measured on nats-server 2.15.1: an account with one flat set of limits reports
``reserved_storage`` at the top level; an account whose limits are kept per replica tier
(as hosted NATS services set them) reports top-level limits of 0, each tier on its own, and
a top-level ``reserved_storage`` that is the total of its tiers. So the top-level figure is
what the account's streams set aside on either kind of account.
"""

from __future__ import annotations

import json
from typing import Any, Mapping

from nats.js.errors import APIError

# The account information request, the one nats-py's ``account_info`` sends (no JetStream domain).
ACCOUNT_INFO_SUBJECT = "$JS.API.INFO"
# nats-py's own default for JetStream API requests; the Status page waits up to 10 seconds in all.
ACCOUNT_INFO_TIMEOUT_SECONDS = 5.0
# A nats-server whose configuration sets no max_file_store, as the bundled server's does not,
# gives JetStream three quarters of the disk's free space, measured each time it starts
# (measured on 2.15.1: 0.750). So the disk needs a third more free space than the streams set
# aside, at every start of NATS, not only the first. Here, not in the authority or the gateway,
# because both use it and neither imports the other.
BUNDLED_STORAGE_SHARE = 3 / 4


async def request_account_info(nc: Any) -> dict[str, Any]:
    """The account's information as the server sends it, from one ``$JS.API.INFO`` request on ``nc``.

    A refusal raises its ``nats.js.errors.APIError`` kind, as nats-py's own request does;
    no answer in time raises ``TimeoutError`` and no JetStream at all ``NoRespondersError``.
    The caller decides what each means.
    """

    reply: dict[str, Any] = json.loads(
        (await nc.request(ACCOUNT_INFO_SUBJECT, b"", timeout=ACCOUNT_INFO_TIMEOUT_SECONDS)).data
    )
    if "error" in reply:
        APIError.from_error(reply["error"])  # raises the error's kind
    return reply


def reported_bytes(part: Mapping[str, Any], key: str) -> int | None:
    """A byte figure ``part`` of the reply reports under ``key``; None when it is left out or not a whole number of 0 or more.

    None means the server did not say, which callers must not read as 0: an account whose
    streams set aside nothing reports 0. (A JSON ``true`` is refused too, although Python
    counts it as a number.)
    """

    value = part.get(key)
    if type(value) is not int or value < 0:
        return None
    return value
