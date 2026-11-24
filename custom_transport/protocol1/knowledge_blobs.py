"""Inline-or-blob encoding and verified decoding for protocol 1 Knowledge requests and replies.

Implements decision 77 (``contracts/messages/v1/README.md``, "Knowledge"):

- Every ``text_content`` field (page body, note body, revision body, proposal content,
  search snippet) travels inline when it is at most ``INLINE_TEXT_MAX_CHARS`` (32,768)
  characters and the frame fits within the frame budget, and as a ``text/plain`` blob in
  ``UA1_BLOBS`` otherwise.
- When a detailed ``page`` (or ``contribution.current``) still exceeds the frame budget
  even after spilling its ``text_content`` fields to blobs (for example with many links,
  backlinks, tags, and open notes), it travels as ``{"type": "blob", "ref": ...}`` with
  ``media_type: "application/json"``; receivers validate the fetched JSON against the
  contract's ``PageInline`` model before resolving its inner ``text_content`` blobs.
- Receivers enforce hard byte caps (``max_chars * 4`` for text, ``MAX_PAGE_JSON_BYTES``
  for page JSON) BEFORE downloading chunks and enforce exact character ceilings after
  strict UTF-8 decoding.
"""

from __future__ import annotations

import copy
import json
from typing import Any, Awaitable, Callable, Mapping, Optional, Protocol, TypeGuard

from pydantic import ValidationError

from custom_messages.wire.models import PageInline
from custom_messages.wire.sizes import INLINE_TEXT_MAX_CHARS, REPLY_BODY_MAX_BYTES, encoded_size
from custom_transport.protocol1.blobs import (
    BlobConcurrentWrite,
    BlobCorrupted,
    BlobError,
    BlobOwner,
    BlobTooLarge,
    get_blob,
    put_blob,
    sha256_unpadded,
)

TEXT_PLAIN_MEDIA_TYPE = "text/plain"
APPLICATION_JSON_MEDIA_TYPE = "application/json"
REQUEST_BODY_MAX_BYTES = REPLY_BODY_MAX_BYTES
MAX_BODY_CHARS_CEILING = 500_000
MAX_NOTE_CHARS_CEILING = 50_000
MAX_SNIPPET_CHARS_CEILING = 500_000
MAX_PAGE_JSON_BYTES = 4 * 1024 * 1024
MAX_REGISTER_ATTEMPTS = 3

_REQUEST_TEXT_FIELDS = ("content", "note")


class KnowledgeBlobUnavailable(BlobError):
    """Long Knowledge text or an oversized page needs ``UA1_BLOBS``, which is not configured here."""


class BlobStore(Protocol):
    """Minimal async put/get port for protocol 1 ``UA1_BLOBS`` objects."""

    async def put(
        self,
        data: bytes,
        *,
        media_type: str,
        owner: BlobOwner,
        name: Optional[str] = None,
    ) -> dict[str, Any]:
        """Store ``data`` and register ownership; return a protocol 1 ``blob_ref`` mapping."""

    async def get(self, ref: Mapping[str, Any], *, max_bytes: Optional[int] = None) -> bytes:
        """Fetch and verify ``ref``, enforcing ``max_bytes`` before and during download."""


class TeamServicesBlobStore:
    """In-process ``BlobStore`` adapter over ``team_services.blobs.BlobService``."""

    def __init__(self, service: Any) -> None:
        self._service = service

    async def put(
        self,
        data: bytes,
        *,
        media_type: str,
        owner: BlobOwner,
        name: Optional[str] = None,
    ) -> dict[str, Any]:
        return dict(await self._service.put_and_register(data, owner, media_type=media_type, name=name))

    async def get(self, ref: Mapping[str, Any], *, max_bytes: Optional[int] = None) -> bytes:
        return bytes(await self._service.get(ref, max_bytes=max_bytes))


class ClientBlobStore:
    """Client-side ``BlobStore`` using ``blobs.register`` on ``svc.team`` plus ``put_blob``/``get_blob``."""

    def __init__(
        self,
        *,
        get_js: Callable[[], Awaitable[Any]],
        team: str,
        register: Callable[[dict[str, Any]], Awaitable[dict[str, Any]]],
        request_ids: Callable[[], str],
    ) -> None:
        self._get_js = get_js
        self._team = team
        self._register = register
        self._request_ids = request_ids

    async def _register_once(
        self,
        *,
        sha256: str,
        size: int,
        reg_owner: dict[str, Any],
        person_ref: Optional[str],
    ) -> bool:
        reg_body: dict[str, Any] = {
            "request_id": self._request_ids(),
            "sha256": sha256,
            "size": size,
            "owner": reg_owner,
        }
        if person_ref is not None:
            reg_body["person"] = person_ref
        reply = await self._register(reg_body)
        outcome = str(reply.get("outcome") or "")
        if outcome == "not_allowed":
            raise PermissionError(str(reply.get("message") or "Blob registration was not allowed."))
        if outcome != "ok":
            raise BlobError(str(reply.get("message") or "Blob registration failed."))
        return reply.get("present") is True

    async def put(
        self,
        data: bytes,
        *,
        media_type: str,
        owner: BlobOwner,
        name: Optional[str] = None,
    ) -> dict[str, Any]:
        payload = bytes(data)
        sha256 = sha256_unpadded(payload)
        size = len(payload)
        reg_owner = (
            {"type": "request", "request_id": owner.request_id}
            if owner.request_id is not None
            else {"type": "agent", "agent_id": owner.agent_id}
        )

        for _attempt in range(MAX_REGISTER_ATTEMPTS):
            if await self._register_once(
                sha256=sha256, size=size, reg_owner=reg_owner, person_ref=owner.person_ref
            ):
                ref: dict[str, Any] = {"sha256": sha256, "size": size, "media_type": media_type}
                if name is not None:
                    ref["name"] = name
                return ref
            js = await self._get_js()
            await put_blob(js, self._team, payload, owner)

        raise BlobConcurrentWrite(
            f"Blob {self._team}~{sha256} could not be registered after {MAX_REGISTER_ATTEMPTS} attempts."
        )

    async def get(self, ref: Mapping[str, Any], *, max_bytes: Optional[int] = None) -> bytes:
        js = await self._get_js()
        return await get_blob(
            js,
            self._team,
            str(ref["sha256"]),
            expected_size=int(ref["size"]),
            max_bytes=max_bytes,
        )


def _require_store(blobs: Optional[BlobStore]) -> BlobStore:
    if blobs is None:
        raise KnowledgeBlobUnavailable(
            "Long Knowledge text requires the team's blob store (UA1_BLOBS), which is not available here."
        )
    return blobs


async def _put_cached(
    data: bytes,
    *,
    media_type: str,
    blobs: Optional[BlobStore],
    owner: BlobOwner,
    cache: dict[tuple[str, bytes], dict[str, Any]],
) -> dict[str, Any]:
    key = (media_type, data)
    cached = cache.get(key)
    if cached is not None:
        return dict(cached)
    store = _require_store(blobs)
    ref = await store.put(data, media_type=media_type, owner=owner)
    cache[key] = dict(ref)
    return dict(ref)


async def encode_text_content(
    text: str,
    blobs: Optional[BlobStore],
    owner: BlobOwner,
    *,
    inline_limit: int = INLINE_TEXT_MAX_CHARS,
    force_blob: bool = False,
    cache: Optional[dict[tuple[str, bytes], dict[str, Any]]] = None,
) -> dict[str, Any]:
    """Encode ``text`` as inline ``text_content`` or a ``text/plain`` blob reference."""

    if not force_blob and len(text) <= inline_limit:
        return {"type": "inline", "text": text}
    resolved_cache = cache if cache is not None else {}
    ref = await _put_cached(
        text.encode("utf-8"),
        media_type=TEXT_PLAIN_MEDIA_TYPE,
        blobs=blobs,
        owner=owner,
        cache=resolved_cache,
    )
    return {"type": "blob", "ref": ref}


async def decode_text_content(
    value: Mapping[str, Any],
    blobs: Optional[BlobStore],
    *,
    max_chars: int,
    cache: Optional[dict[tuple[str, int], bytes]] = None,
) -> str:
    """Decode one ``text_content`` mapping, enforcing byte and character caps."""

    kind = value.get("type")
    if kind == "inline":
        text = str(value["text"])
        if len(text) > max_chars:
            raise BlobTooLarge(f"Knowledge text is {len(text):,} characters; at most {max_chars:,} are allowed.")
        return text
    if kind != "blob":
        raise BlobCorrupted(f"Unsupported Knowledge text_content type: {kind!r}")

    ref = value.get("ref")
    if not isinstance(ref, Mapping):
        raise BlobCorrupted("Knowledge text blob is missing its blob reference.")
    if ref.get("media_type") != TEXT_PLAIN_MEDIA_TYPE:
        raise BlobCorrupted(
            f"Knowledge text blob must use media_type {TEXT_PLAIN_MEDIA_TYPE!r}, got {ref.get('media_type')!r}."
        )
    max_bytes = max_chars * 4
    ref_size = int(ref.get("size", -1))
    if ref_size < 0 or ref_size > max_bytes:
        raise BlobTooLarge(
            f"Knowledge text blob size {ref_size:,} bytes exceeds the maximum {max_bytes:,} bytes for {max_chars:,} characters."
        )
    raw = await _get_cached(ref, blobs, max_bytes=max_bytes, cache=cache)
    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise BlobCorrupted("Knowledge text blob is not valid UTF-8.") from exc
    if len(text) > max_chars:
        raise BlobTooLarge(f"Knowledge text is {len(text):,} characters; at most {max_chars:,} are allowed.")
    return text


async def _get_cached(
    ref: Mapping[str, Any],
    blobs: Optional[BlobStore],
    *,
    max_bytes: int,
    cache: Optional[dict[tuple[str, int], bytes]],
) -> bytes:
    key = (str(ref["sha256"]), int(ref["size"]))
    if cache is not None and key in cache:
        raw = cache[key]
        if len(raw) > max_bytes:
            raise BlobTooLarge(f"Blob size {len(raw):,} bytes exceeds limit {max_bytes:,}.")
        return raw
    store = _require_store(blobs)
    raw = await store.get(ref, max_bytes=max_bytes)
    if cache is not None:
        cache[key] = raw
    return raw


def _is_inline_text(value: Any) -> TypeGuard[Mapping[str, Any]]:
    return isinstance(value, Mapping) and value.get("type") == "inline" and isinstance(value.get("text"), str)


def _is_blob_entry(value: Any) -> TypeGuard[Mapping[str, Any]]:
    return isinstance(value, Mapping) and value.get("type") == "blob"


def request_needs_blobs(
    body: Mapping[str, Any],
    *,
    max_bytes: int = REQUEST_BODY_MAX_BYTES,
    inline_limit: int = INLINE_TEXT_MAX_CHARS,
) -> bool:
    """Whether ``body`` has inline text over ``inline_limit`` or exceeds ``max_bytes``."""

    for field in _REQUEST_TEXT_FIELDS:
        value = body.get(field)
        if _is_inline_text(value) and len(value["text"]) > inline_limit:
            return True
    return encoded_size(body) > max_bytes


def request_has_blobs(body: Mapping[str, Any]) -> bool:
    """Whether ``body`` carries any blob-backed ``text_content`` field."""

    return any(_is_blob_entry(body.get(field)) for field in _REQUEST_TEXT_FIELDS)


def _page_has_blobs(page: Any) -> bool:
    if not isinstance(page, Mapping):
        return False
    if _is_blob_entry(page):
        return True
    if _is_blob_entry(page.get("content")):
        return True
    return any(_is_blob_entry(note.get("content")) for note in (page.get("open_notes") or ()) if isinstance(note, Mapping))


def reply_has_blobs(reply: Mapping[str, Any]) -> bool:
    """Whether a Knowledge reply carries any blob-backed ``text_content`` or ``page``."""

    if _page_has_blobs(reply.get("page")):
        return True
    note = reply.get("note")
    if isinstance(note, Mapping) and _is_blob_entry(note.get("content")):
        return True
    contribution = reply.get("contribution")
    if isinstance(contribution, Mapping):
        if _is_blob_entry(contribution.get("content")) or _page_has_blobs(contribution.get("current")):
            return True
    pages = reply.get("pages")
    if isinstance(pages, list):
        if any(isinstance(item, Mapping) and _is_blob_entry(item.get("snippet")) for item in pages):
            return True
    return False


async def fit_request_body(
    body: Mapping[str, Any],
    blobs: Optional[BlobStore],
    owner: BlobOwner,
    *,
    max_bytes: int = REQUEST_BODY_MAX_BYTES,
    inline_limit: int = INLINE_TEXT_MAX_CHARS,
) -> dict[str, Any]:
    """Encode over-limit or over-budget ``text_content`` fields of a Knowledge request body as blobs."""

    fitted = copy.deepcopy(dict(body))
    cache: dict[tuple[str, bytes], dict[str, Any]] = {}

    for field in _REQUEST_TEXT_FIELDS:
        value = fitted.get(field)
        if _is_inline_text(value) and len(value["text"]) > inline_limit:
            fitted[field] = await encode_text_content(
                value["text"], blobs, owner, inline_limit=inline_limit, force_blob=True, cache=cache
            )

    if encoded_size(fitted) <= max_bytes:
        return fitted

    candidates = [
        (len(fitted[field]["text"].encode("utf-8")), field)
        for field in _REQUEST_TEXT_FIELDS
        if _is_inline_text(fitted.get(field))
    ]
    candidates.sort(reverse=True)
    for _byte_len, field in candidates:
        if encoded_size(fitted) <= max_bytes:
            break
        fitted[field] = await encode_text_content(
            fitted[field]["text"], blobs, owner, inline_limit=inline_limit, force_blob=True, cache=cache
        )
    return fitted


async def decode_request_body(
    action: str,
    body: Mapping[str, Any],
    blobs: Optional[BlobStore],
    *,
    max_body_chars: int = MAX_BODY_CHARS_CEILING,
    max_note_chars: int = MAX_NOTE_CHARS_CEILING,
) -> dict[str, Any]:
    """Resolve any blob-backed ``text_content`` fields in a Knowledge request to inline dicts."""

    if not request_has_blobs(body):
        return dict(body)
    decoded = dict(body)
    cache: dict[tuple[str, int], bytes] = {}
    for field in _REQUEST_TEXT_FIELDS:
        value = decoded.get(field)
        if not _is_blob_entry(value):
            continue
        # publish_contribution carries payload.body in both content and note for edited page proposals,
        # and KnowledgeService.publish_contribution enforces max_note_chars vs max_body_chars by kind.
        limit = max_note_chars if (field == "note" and action == "add_note") else max_body_chars
        text = await decode_text_content(value, blobs, max_chars=limit, cache=cache)
        decoded[field] = {"type": "inline", "text": text}
    return decoded


def _collect_inline_text_slots(reply: dict[str, Any]) -> list[tuple[dict[str, Any], str]]:
    """Return every mutable ``(container, key)`` holding an inline ``text_content`` in ``reply``."""

    slots: list[tuple[dict[str, Any], str]] = []
    _collect_page_slots(reply.get("page"), slots)
    note = reply.get("note")
    if isinstance(note, dict) and _is_inline_text(note.get("content")):
        slots.append((note, "content"))
    contribution = reply.get("contribution")
    if isinstance(contribution, dict):
        if _is_inline_text(contribution.get("content")):
            slots.append((contribution, "content"))
        _collect_page_slots(contribution.get("current"), slots)
    pages = reply.get("pages")
    if isinstance(pages, list):
        for item in pages:
            if isinstance(item, dict) and _is_inline_text(item.get("snippet")):
                slots.append((item, "snippet"))
    return slots


def _collect_page_slots(page: Any, slots: list[tuple[dict[str, Any], str]]) -> None:
    if not isinstance(page, dict) or page.get("type") != "inline":
        return
    if _is_inline_text(page.get("content")):
        slots.append((page, "content"))
    for note in page.get("open_notes") or ():
        if isinstance(note, dict) and _is_inline_text(note.get("content")):
            slots.append((note, "content"))


async def _spill_over_inline_limit(
    reply: dict[str, Any],
    blobs: Optional[BlobStore],
    owner: BlobOwner,
    *,
    inline_limit: int,
    cache: dict[tuple[str, bytes], dict[str, Any]],
) -> None:
    for container, key in _collect_inline_text_slots(reply):
        text = container[key]["text"]
        if len(text) > inline_limit:
            container[key] = await encode_text_content(
                text, blobs, owner, inline_limit=inline_limit, force_blob=True, cache=cache
            )


async def _spill_largest_inline_texts(
    reply: dict[str, Any],
    blobs: Optional[BlobStore],
    owner: BlobOwner,
    *,
    max_bytes: int,
    inline_limit: int,
    cache: dict[tuple[str, bytes], dict[str, Any]],
) -> None:
    slots = _collect_inline_text_slots(reply)
    ranked = sorted(
        slots,
        key=lambda item: len(item[0][item[1]]["text"].encode("utf-8")),
        reverse=True,
    )
    for container, key in ranked:
        if encoded_size(reply) <= max_bytes:
            return
        text = container[key]["text"]
        # Spilling very tiny strings cannot save bytes over a ~150-byte blob_ref.
        if len(text.encode("utf-8")) <= 160:
            continue
        container[key] = await encode_text_content(
            text, blobs, owner, inline_limit=inline_limit, force_blob=True, cache=cache
        )


async def _encode_page_blob(
    page: Mapping[str, Any],
    blobs: Optional[BlobStore],
    owner: BlobOwner,
    *,
    cache: dict[tuple[str, bytes], dict[str, Any]],
) -> dict[str, Any]:
    raw = json.dumps(page, separators=(",", ":"), ensure_ascii=False).encode("utf-8")
    ref = await _put_cached(
        raw,
        media_type=APPLICATION_JSON_MEDIA_TYPE,
        blobs=blobs,
        owner=owner,
        cache=cache,
    )
    return {"type": "blob", "ref": ref}


async def _spill_oversized_pages(
    reply: dict[str, Any],
    blobs: Optional[BlobStore],
    owner: BlobOwner,
    *,
    max_bytes: int,
    cache: dict[tuple[str, bytes], dict[str, Any]],
) -> None:
    page = reply.get("page")
    if isinstance(page, dict) and page.get("type") == "inline" and encoded_size(reply) > max_bytes:
        reply["page"] = await _encode_page_blob(page, blobs, owner, cache=cache)
    contribution = reply.get("contribution")
    if isinstance(contribution, dict):
        current = contribution.get("current")
        if isinstance(current, dict) and current.get("type") == "inline" and encoded_size(reply) > max_bytes:
            contribution["current"] = await _encode_page_blob(current, blobs, owner, cache=cache)


def _paged_reply(reply: dict[str, Any], key: str, offset: int, total: int) -> dict[str, Any]:
    reply["total"] = int(total)
    following = int(offset) + len(reply[key])
    if reply[key] and following < total:
        reply["next_offset"] = following
    return reply


def fit_list_rows(
    reply: dict[str, Any],
    key: str,
    *,
    offset: int,
    max_bytes: int = REPLY_BODY_MAX_BYTES,
) -> dict[str, Any]:
    """Keep the leading rows of ``reply[key]`` that fit in ``max_bytes`` and set ``next_offset``."""

    if encoded_size(reply) <= max_bytes:
        return reply
    rows = list(reply[key])
    base = encoded_size({**reply, key: [], "next_offset": offset + len(rows)})
    used, kept = base, 0
    for row in rows:
        added = encoded_size(row) + (1 if kept else 0)
        if used + added > max_bytes:
            break
        used += added
        kept += 1
    if kept == 0 and rows:
        raise BlobTooLarge(
            f"One {key} entry takes {encoded_size({**reply, key: rows[:1]}):,} bytes, exceeding {max_bytes:,}."
        )
    fitted = {name: value for name, value in reply.items() if name != "next_offset"}
    fitted[key] = rows[:kept]
    return _paged_reply(fitted, key, offset, int(reply["total"]))


async def fit_reply_body(
    reply: Mapping[str, Any],
    blobs: Optional[BlobStore],
    owner: BlobOwner,
    *,
    max_bytes: int = REPLY_BODY_MAX_BYTES,
    inline_limit: int = INLINE_TEXT_MAX_CHARS,
    list_key: Optional[str] = None,
    offset: int = 0,
) -> dict[str, Any]:
    """Fit a Knowledge reply within ``max_bytes`` using ``text/plain`` and ``application/json`` blobs."""

    if reply.get("outcome") != "ok":
        return dict(reply)

    fitted = copy.deepcopy(dict(reply))
    cache: dict[tuple[str, bytes], dict[str, Any]] = {}

    await _spill_over_inline_limit(fitted, blobs, owner, inline_limit=inline_limit, cache=cache)

    if encoded_size(fitted) > max_bytes:
        await _spill_largest_inline_texts(
            fitted, blobs, owner, max_bytes=max_bytes, inline_limit=inline_limit, cache=cache
        )

    if list_key is None:
        if encoded_size(fitted) > max_bytes:
            await _spill_oversized_pages(fitted, blobs, owner, max_bytes=max_bytes, cache=cache)
        return fitted

    if list_key == "revisions":
        revs = fitted.get("revisions") or []
        probe = {**fitted, "revisions": revs[:1], "next_offset": offset + len(revs)}
        page = fitted.get("page")
        if isinstance(page, dict) and page.get("type") == "inline" and encoded_size(probe) > max_bytes:
            fitted["page"] = await _encode_page_blob(page, blobs, owner, cache=cache)

    return fit_list_rows(fitted, list_key, offset=offset, max_bytes=max_bytes)


async def _fetch_page_json_blob(
    page: Mapping[str, Any],
    blobs: Optional[BlobStore],
    cache: dict[tuple[str, int], bytes],
) -> dict[str, Any]:
    ref = page.get("ref")
    if not isinstance(ref, Mapping):
        raise BlobCorrupted("Knowledge page blob is missing its blob reference.")
    if ref.get("media_type") != APPLICATION_JSON_MEDIA_TYPE:
        raise BlobCorrupted(
            f"Knowledge page blob must use media_type {APPLICATION_JSON_MEDIA_TYPE!r}, got {ref.get('media_type')!r}."
        )
    raw = await _get_cached(ref, blobs, max_bytes=MAX_PAGE_JSON_BYTES, cache=cache)
    try:
        parsed = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise BlobCorrupted("Knowledge page blob is not valid UTF-8 JSON.") from exc
    try:
        PageInline.model_validate(parsed)
    except ValidationError as exc:
        raise BlobCorrupted(f"Knowledge page blob failed contract schema validation: {exc}") from exc
    return copy.deepcopy(dict(parsed))


async def _resolve_page(
    page: Mapping[str, Any],
    blobs: Optional[BlobStore],
    *,
    max_body_chars: int,
    max_note_chars: int,
    cache: dict[tuple[str, int], bytes],
) -> dict[str, Any]:
    resolved = (
        await _fetch_page_json_blob(page, blobs, cache)
        if page.get("type") == "blob"
        else copy.deepcopy(dict(page))
    )
    content = resolved.get("content")
    if _is_blob_entry(content):
        text = await decode_text_content(content, blobs, max_chars=max_body_chars, cache=cache)
        resolved["content"] = {"type": "inline", "text": text}
    for note in resolved.get("open_notes") or ():
        if isinstance(note, dict):
            note_content = note.get("content")
            if _is_blob_entry(note_content):
                text = await decode_text_content(note_content, blobs, max_chars=max_note_chars, cache=cache)
                note["content"] = {"type": "inline", "text": text}
    return resolved


async def _resolve_contribution(
    contribution: dict[str, Any],
    blobs: Optional[BlobStore],
    *,
    max_body_chars: int,
    max_note_chars: int,
    cache: dict[tuple[str, int], bytes],
) -> None:
    content = contribution.get("content")
    if _is_blob_entry(content):
        limit = max_note_chars if contribution.get("kind") == "note" else max_body_chars
        text = await decode_text_content(content, blobs, max_chars=limit, cache=cache)
        contribution["content"] = {"type": "inline", "text": text}
    current = contribution.get("current")
    if isinstance(current, Mapping):
        contribution["current"] = await _resolve_page(
            current, blobs, max_body_chars=max_body_chars, max_note_chars=max_note_chars, cache=cache
        )


async def _resolve_pages_snippets(
    pages: list[Any],
    blobs: Optional[BlobStore],
    *,
    max_snippet_chars: int,
    cache: dict[tuple[str, int], bytes],
) -> None:
    for item in pages:
        if isinstance(item, dict):
            snippet = item.get("snippet")
            if _is_blob_entry(snippet):
                text = await decode_text_content(snippet, blobs, max_chars=max_snippet_chars, cache=cache)
                item["snippet"] = {"type": "inline", "text": text}


async def resolve_reply_body(
    reply: Mapping[str, Any],
    blobs: Optional[BlobStore],
    *,
    max_body_chars: int = MAX_BODY_CHARS_CEILING,
    max_note_chars: int = MAX_NOTE_CHARS_CEILING,
    max_snippet_chars: int = MAX_SNIPPET_CHARS_CEILING,
) -> dict[str, Any]:
    """Fetch, verify, and inline every blob-backed ``page`` and ``text_content`` in a Knowledge reply."""

    if reply.get("outcome") != "ok" or not reply_has_blobs(reply):
        return dict(reply)

    resolved = copy.deepcopy(dict(reply))
    cache: dict[tuple[str, int], bytes] = {}

    if isinstance(resolved.get("page"), Mapping):
        resolved["page"] = await _resolve_page(
            resolved["page"], blobs, max_body_chars=max_body_chars, max_note_chars=max_note_chars, cache=cache
        )
    note = resolved.get("note")
    if isinstance(note, dict):
        note_content = note.get("content")
        if _is_blob_entry(note_content):
            text = await decode_text_content(note_content, blobs, max_chars=max_note_chars, cache=cache)
            note["content"] = {"type": "inline", "text": text}
    contribution = resolved.get("contribution")
    if isinstance(contribution, dict):
        await _resolve_contribution(
            contribution, blobs, max_body_chars=max_body_chars, max_note_chars=max_note_chars, cache=cache
        )
    pages = resolved.get("pages")
    if isinstance(pages, list):
        await _resolve_pages_snippets(pages, blobs, max_snippet_chars=max_snippet_chars, cache=cache)
    return resolved


__all__ = [
    "APPLICATION_JSON_MEDIA_TYPE",
    "BlobStore",
    "ClientBlobStore",
    "KnowledgeBlobUnavailable",
    "MAX_BODY_CHARS_CEILING",
    "MAX_NOTE_CHARS_CEILING",
    "MAX_PAGE_JSON_BYTES",
    "MAX_SNIPPET_CHARS_CEILING",
    "REQUEST_BODY_MAX_BYTES",
    "TEXT_PLAIN_MEDIA_TYPE",
    "TeamServicesBlobStore",
    "decode_request_body",
    "decode_text_content",
    "encode_text_content",
    "fit_list_rows",
    "fit_reply_body",
    "fit_request_body",
    "reply_has_blobs",
    "request_has_blobs",
    "request_needs_blobs",
    "resolve_reply_body",
]
