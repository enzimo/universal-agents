"""Read the visible text of the Telegram message a person replied to.

Bot API rich messages contain blocks rather than Message.text or caption.
The pinned python-telegram-bot SDK keeps this field in api_kwargs. Normalize
only visible content, never media identifiers, callback data or link targets.
See https://core.telegram.org/bots/api#richmessage and #richtext.
"""

from __future__ import annotations

import logging
from collections.abc import Mapping
from typing import Any

logger = logging.getLogger(__name__)

_SUPPORTED_BLOCK_TYPES = frozenset({
    "paragraph", "heading", "pre", "footer", "divider", "mathematical_expression",
    "anchor", "list", "blockquote", "expandable_blockquote", "pullquote", "collage",
    "slideshow", "table", "details", "map", "buttons", "animation", "audio",
    "document", "photo", "video", "voice_note", "thinking",
})


def replied_message_text(message: Any) -> str:
    """Text, caption or rich-message blocks, in the order the chat displays them."""
    text = getattr(message, "text", None) or getattr(message, "caption", None)
    if text:
        return str(text).strip()
    rich = (getattr(message, "api_kwargs", None) or {}).get("rich_message")
    if rich is None:
        return ""
    if not isinstance(rich, Mapping) or not isinstance(rich.get("blocks"), (list, tuple)):
        logger.warning(
            "Cannot read Telegram rich reply. event=telegram_reply_rich_message_invalid message_id=%s",
            getattr(message, "message_id", "-"),
        )
        return ""
    return _blocks_text(rich["blocks"]).strip()


def _rich_text(value: Any) -> str:
    """RichText is a string, concatenated array, or an inline formatting entity."""
    if isinstance(value, str):
        return value
    if isinstance(value, (list, tuple)):
        return "".join(_rich_text(part) for part in value)
    if not isinstance(value, Mapping):
        return ""
    for field in ("text", "alternative_text", "expression"):
        if field in value:
            return _rich_text(value[field])
    if "button" in value:
        return _rich_text(value["button"].get("text"))
    if value.get("type") not in {None, "anchor"}:
        logger.warning(
            "Cannot read Telegram rich inline text. event=telegram_reply_rich_text_unsupported type=%s",
            value.get("type"),
        )
    return ""


def _blocks_text(blocks: Any) -> str:
    return "\n".join(text for block in blocks if (text := _block_text(block)))


def _block_text(block: Any) -> str:
    if not isinstance(block, Mapping):
        logger.warning("Cannot read Telegram rich block. event=telegram_reply_rich_block_invalid")
        return ""
    if block.get("type") not in _SUPPORTED_BLOCK_TYPES:
        logger.warning(
            "Cannot fully read Telegram rich block. event=telegram_reply_rich_block_unsupported type=%s",
            block.get("type"),
        )
    # Structural fields are explicit so unseen media metadata cannot become
    # model context. Each block's caption/credit follows its content.
    parts = [_rich_text(block.get(field)) for field in ("summary", "text", "expression")]
    if "blocks" in block:
        parts.append(_blocks_text(block["blocks"]))
    parts.extend(_list_item_text(item) for item in block.get("items", ()))
    for row in block.get("cells", ()):
        parts.append(" | ".join(_rich_text(cell.get("text")) for cell in row))
    if "buttons" in block:
        parts.append(" | ".join(_rich_text(button.get("text")) for button in block["buttons"]))
    parts.append(_rich_text(block.get("credit")))
    caption = block.get("caption")
    parts.append(_rich_text(caption))
    if isinstance(caption, Mapping):
        parts.append(_rich_text(caption.get("credit")))
    return "\n".join(part for part in parts if part)


def _list_item_text(item: Mapping[str, Any]) -> str:
    label = str(item.get("label") or "-")
    if item.get("has_checkbox"):
        label += " [x]" if item.get("is_checked") else " [ ]"
    return f"{label} {_blocks_text(item.get('blocks', ()))}"
