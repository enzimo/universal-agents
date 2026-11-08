"""Conversation topic tags shared by commands, agent tools and automatic tagging.

The limits match the topics.* wire contract. Keep validation independent of the
generated models so normal agent startup does not load the transport contract.
"""

import re
from logging import Logger

TOPIC_TAG_MAX = 64
TOPIC_TAGS_MAX = 20


class TopicTagInputError(ValueError):
    """A topic tag or tag list the caller must correct."""


def normalize_topic_tag(raw_value: str) -> str:
    normalized = re.sub(r"[^a-z0-9_-]+", "-", str(raw_value or "").strip().lower())
    normalized = re.sub(r"-+", "-", normalized).strip("-_")
    if not normalized:
        raise TopicTagInputError("Topic tag must contain at least one letter or number.")
    if len(normalized) > TOPIC_TAG_MAX:
        raise TopicTagInputError(f"Topic tags must be {TOPIC_TAG_MAX} characters or fewer.")
    return normalized


def normalize_topic_tags(tags: list[str]) -> list[str]:
    if not 1 <= len(tags) <= TOPIC_TAGS_MAX:
        raise TopicTagInputError(f"Use between 1 and {TOPIC_TAGS_MAX} topic tags at a time.")
    return list(dict.fromkeys(normalize_topic_tag(tag) for tag in tags))


def extract_topic_tags(text: str, logger: Logger) -> list[str]:
    """Explicit hashtags from ordinary prose; invalid ones are skipped without quoting their text."""

    tags: list[str] = []
    for raw in re.findall(r"#([A-Za-z0-9_-]+)", text):
        try:
            tag = normalize_topic_tag(raw)
        except TopicTagInputError:
            logger.warning("Skipped an invalid automatic topic tag. event=topic_tag_skipped length=%s", len(raw))
            continue
        if tag not in tags:
            tags.append(tag)
    return tags
