"""Delete the credentials and sign-ins a person stored, after an administrator removed them.

Removing a person (``/user remove``) deletes, at once and for good, what the
broker holds for them alone. The gateway revokes their account links in the
access store and then asks for this on a gateway-only route
(``PersonCredentialDeletionV1``, ``SecureToolBrokerService``). In order:

1. **Stored credentials**: every credential in the person's own scope
   (``CredentialOwnerScope.user``) and under their links, deleted the way a
   person's own delete form deletes it (``credential_catalog.delete_credential``:
   the secret in Sealbox, then the catalog entry), so nothing is left that a
   later link could reach.
2. **Sign-ins**: every per-person credential-state key (GitHub, Google and X
   sign-ins and the person's own Google client,
   ``credential_state_store.STATE_KEY_PATHS``), deleted from the configured
   store and from the person's files (``delete_credential_state``).
3. **Private credentials folder** (``<credentials dir>/users/owner-<sha>``,
   ``credential_state.remove_person_credentials_folder``), deleted without
   following links. It is kept when a sign-in could not be deleted, because
   it may hold what a retry needs (a Vault token or Bitwarden session), and
   the administrator is told to run ``/user remove`` again.

Disabling a person never comes here: their sign-ins are kept. A person
restored within the hold signs in again with ``/link``. The team's
credentials, sign-ins and folder are never touched: only the person's own
scope is named.
"""

from __future__ import annotations

import logging

from custom_messages.contracts.access_v2 import (
    CredentialsFolderOutcome,
    PersonCredentialDeletionResultV1,
    PersonCredentialDeletionV1,
    SignInService,
)
from secure_tool_broker.credential_catalog import delete_credential, load_catalog
from secure_tool_broker.credential_catalog_store import CredentialOwnerScope
from secure_tool_broker.credential_state import remove_person_credentials_folder
from secure_tool_broker.credential_state_store import (
    STATE_FOUND,
    STATE_KEY_PATHS,
    STATE_KEY_SERVICES,
    delete_credential_state,
)

logger = logging.getLogger(__name__)

_SUCCESS = "success"


def _delete_stored_credentials(request: PersonCredentialDeletionV1) -> tuple[list[str], list[str]]:
    """Delete the person's catalog credentials; which keys were deleted and which failed."""

    scope = CredentialOwnerScope.user(request.person_id)
    keys = sorted(set(load_catalog(scope=scope).credentials) | set(request.credential_keys))
    deleted: list[str] = []
    failed: list[str] = []
    for key in keys:
        result = delete_credential({"credential_key": key}, scope=scope)
        if result.status == _SUCCESS:
            deleted.append(key)
            continue
        failed.append(key)
        logger.error(
            "Could not delete a removed person's credential. event=departed_person_credential_delete_failed "
            "person_id=%s credential_key=%s error=%s",
            request.person_id,
            key,
            result.error,
        )
    return deleted, failed


def _delete_sign_ins(person_id: str) -> tuple[tuple[SignInService, ...], tuple[SignInService, ...]]:
    """Delete every per-person sign-in; the services found and deleted, and those that failed."""

    deleted: set[SignInService] = set()
    failed: set[SignInService] = set()
    for key in STATE_KEY_PATHS:
        result = delete_credential_state(key, owner_id=person_id)  # logs its own failure
        if result.status != _SUCCESS:
            failed.add(STATE_KEY_SERVICES[key])
        elif result.content.get(STATE_FOUND):
            deleted.add(STATE_KEY_SERVICES[key])
    return tuple(sorted(deleted - failed)), tuple(sorted(failed))


def _delete_folder(person_id: str, *, sign_ins_failed: bool) -> CredentialsFolderOutcome:
    if sign_ins_failed:
        logger.warning(
            "Kept a removed person's credentials folder until their sign-ins are deleted. "
            "event=person_credentials_folder_kept person_id=%s",
            person_id,
        )
        return CredentialsFolderOutcome.KEPT
    try:
        removed = remove_person_credentials_folder(person_id)
    except (OSError, RuntimeError) as exc:  # safe_open.UnsafePath is an OSError
        logger.error(
            "Could not delete a removed person's credentials folder. "
            "event=person_credentials_folder_delete_failed person_id=%s reason=%s error=%s",
            person_id,
            getattr(exc, "reason", type(exc).__name__),
            exc,
        )
        return CredentialsFolderOutcome.FAILED
    return CredentialsFolderOutcome.DELETED if removed else CredentialsFolderOutcome.ABSENT


def delete_person_credentials(request: PersonCredentialDeletionV1) -> PersonCredentialDeletionResultV1:
    """Delete the person's stored credentials, sign-ins and credentials folder (module docstring)."""

    deleted, failed = _delete_stored_credentials(request)
    sign_ins_deleted, sign_ins_failed = _delete_sign_ins(request.person_id)
    folder = _delete_folder(request.person_id, sign_ins_failed=bool(sign_ins_failed))
    logger.info(
        "Deleted a removed person's credentials. event=departed_person_credentials_deleted person_id=%s "
        "deleted=%d failed=%d sign_ins_deleted=%s sign_ins_failed=%s folder=%s",
        request.person_id,
        len(deleted),
        len(failed),
        ",".join(service.value for service in sign_ins_deleted) or "none",
        ",".join(service.value for service in sign_ins_failed) or "none",
        folder.value,
    )
    return PersonCredentialDeletionResultV1(
        deleted=tuple(deleted),
        failed=tuple(failed),
        sign_ins_deleted=sign_ins_deleted,
        sign_ins_failed=sign_ins_failed,
        folder=folder,
    )


__all__ = ["delete_person_credentials"]
