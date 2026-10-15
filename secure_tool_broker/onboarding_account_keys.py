"""Link the keys typed during onboarding as the team's accounts or the person's own.

Onboarding asks for the Exa and Brave Search keys but cannot store them: the
broker, which owns the credential store, is not running yet. It leaves them in
a private hand-over file, and at start the gateway sends them here on a
gateway-only route (``ONBOARDING_ACCOUNT_KEYS_ROUTE``, ``OnboardingAccountKeysV1``)
for the person who onboarded, with that person's current team roles.

Each key goes through the same path as a person typing a new credential into
the account link form (``account_links.link_account``) for the owner the
gateway named: the team's account when the team shares the service, the
person's own when it keeps it private (``access_control.account_sharing``).
The tool must use a linked account, a team link needs an administrator and a
shared service (``linkable_tool``), the value is stored in that owner's
credential collection (Sealbox by default) and the link to it is created.
Values are never logged or returned; the result names tools only.
"""

from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timezone
from typing import Any, Awaitable, Callable

from aiohttp import web

from custom_messages.contracts.access_v2 import (
    AccountLinkRequestV1,
    OnboardingAccountKeyFailureV1,
    OnboardingAccountKeysResultV1,
    OnboardingAccountKeysV1,
    OnboardingAccountKeyV1,
)
from id_generation import new_id
from access_control.account_sharing import TeamAccountSharing
from access_control.people import is_administrator
from secure_tool_broker.model_key_page import team_model_key_store
from secure_tool_broker.model_keys import ModelKeyStoreError, StoredModelKey
from secure_tool_broker.account_links import (
    ACCOUNT_LINK_FAILED,
    AccountLinkRefused,
    LinkChoice,
    link_account,
    linkable_tool,
    required_fields,
)

logger = logging.getLogger(__name__)

ONBOARDING_LINK_REASON = "Typed during onboarding."
REASON_FIELDS_NOT_ONE = "onboarding_key_tool_needs_other_fields"


def _link_one(store: Any, catalog: Any, request: OnboardingAccountKeysV1, key: OnboardingAccountKeyV1) -> str:
    """Store one key in its owner's collection and link it; return the link id (raises AccountLinkRefused)."""

    link_request = AccountLinkRequestV1(
        request_id=new_id("onboarding_key"),
        team_id=request.team_id,
        tool_id=key.tool_id,
        owner_kind=key.owner_kind,
        person_id=request.person_id,
        person_roles=request.person_roles,
        reason=ONBOARDING_LINK_REASON,
    )
    fields = sorted(required_fields(linkable_tool(catalog, link_request, TeamAccountSharing(store, request.team_id))))
    if len(fields) != 1:
        raise AccountLinkRefused(
            f"{key.tool_id} needs {len(fields)} credential fields, so one typed key cannot fill it.",
            reason_code=REASON_FIELDS_NOT_ONE,
        )
    outcome = link_account(
        store,
        catalog,
        link_request,
        LinkChoice(new_values={fields[0]: key.value}),
        now=datetime.now(timezone.utc),
    )
    return outcome.link.link_id


def link_onboarding_keys(store: Any, catalog: Any, request: OnboardingAccountKeysV1) -> OnboardingAccountKeysResultV1:
    """Link every key for the owner it names; a key that fails leaves the others linked."""

    linked: list[str] = []
    failed: list[OnboardingAccountKeyFailureV1] = []
    for key in request.keys:
        try:
            link_id = _link_one(store, catalog, request, key)
        except AccountLinkRefused as exc:
            failed.append(OnboardingAccountKeyFailureV1(tool_id=key.tool_id, reason_code=exc.reason_code, message=str(exc)))
            logger.warning(
                "An onboarding key was not linked. event=onboarding_key_link_refused tool_id=%s person_id=%s "
                "reason_code=%s",
                key.tool_id,
                request.person_id,
                exc.reason_code,
            )
            continue
        except Exception:
            failed.append(
                OnboardingAccountKeyFailureV1(
                    tool_id=key.tool_id, reason_code=ACCOUNT_LINK_FAILED, message="The broker could not link the key."
                )
            )
            logger.exception(
                "Linking an onboarding key failed. event=onboarding_key_link_failed tool_id=%s person_id=%s",
                key.tool_id,
                request.person_id,
            )
            continue
        linked.append(key.tool_id)
        logger.info(
            "Linked an onboarding key. event=onboarding_key_linked tool_id=%s owner_kind=%s link_id=%s person_id=%s",
            key.tool_id,
            key.owner_kind.value,
            link_id,
            request.person_id,
        )
    return OnboardingAccountKeysResultV1(linked=tuple(linked), failed=tuple(failed))


def save_onboarding_model_keys(
    request: OnboardingAccountKeysV1,
) -> tuple[tuple[str, ...], tuple[OnboardingAccountKeyFailureV1, ...]]:
    """Store the model keys typed during onboarding as the team's (administrators only).

    Onboarding tested each key with a real request before the person could
    finish (``assistant_gateway.onboarding_web.provider_test``), so they are
    stored as given; ``/auth model`` tests any later change.
    """

    if not request.model_keys:
        return (), ()
    if not is_administrator(request.person_roles):
        logger.warning(
            "Onboarding model keys were not saved: the person who onboarded is not an administrator. "
            "event=onboarding_model_keys_admin_required person_id=%s",
            request.person_id,
        )
        return (), tuple(
            OnboardingAccountKeyFailureV1(
                tool_id=key.address, reason_code="admin_required", message="Only an administrator saves the team's model keys."
            )
            for key in request.model_keys
        )
    saved: list[str] = []
    failed: list[OnboardingAccountKeyFailureV1] = []
    store = team_model_key_store(request.team_id)
    for key in request.model_keys:
        try:
            store.save(
                StoredModelKey(
                    address=key.address,
                    kind=key.kind,
                    key=key.value,
                    saved_at=datetime.now(timezone.utc),
                    saved_by_person_id=request.person_id,
                    tested_model_id=key.model_id,
                )
            )
        except ModelKeyStoreError as exc:
            failed.append(OnboardingAccountKeyFailureV1(tool_id=key.address, reason_code="store_failed", message=str(exc)))
            continue
        saved.append(key.address)
    return tuple(saved), tuple(failed)


def onboarding_keys_handler(gate: Any) -> Callable[[web.Request], Awaitable[web.Response]]:
    """The gateway-only route's handler (``ONBOARDING_ACCOUNT_KEYS_ROUTE``, under ``/v1/account-link/``).

    The caller check happens in the broker's middleware. The body holds key
    values, so neither it nor a validation error (which could quote them) is
    logged or returned.
    """

    async def handle(request: web.Request) -> web.Response:
        try:
            handover = OnboardingAccountKeysV1.model_validate(await request.json())
        except Exception as exc:
            raise web.HTTPBadRequest(text="Invalid onboarding key request.") from exc
        if handover.team_id != gate.team_id:
            raise web.HTTPForbidden(text="The onboarding key request names another team.")
        if handover.keys and gate.links is None:
            raise web.HTTPServiceUnavailable(text="The broker has no access store for account links.")
        linked = (
            await asyncio.to_thread(link_onboarding_keys, gate.links, gate.catalog, handover)
            if handover.keys
            else OnboardingAccountKeysResultV1()
        )
        saved, failed = await asyncio.to_thread(save_onboarding_model_keys, handover)
        result = linked.model_copy(update={"model_keys_saved": saved, "model_keys_failed": failed})
        return web.json_response(result.model_dump(mode="json"))

    return handle


__all__ = ["ONBOARDING_LINK_REASON", "link_onboarding_keys", "onboarding_keys_handler"]
