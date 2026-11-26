"""Ranking catalog tools for ``find_tools``: weighted words with rarity, not substring counts.

Why: ``find_tools`` used to count how many query words appeared anywhere in a
tool's text as substrings, so short words ("a", "on", "my") matched nearly
every tool and tools with long operation lists (the Google CLI) came first for
almost any request. Measured on ``tests/data/tool_finding_queries.yaml``
(180 plain-language requests, 2026-11-26), the right tool was in the top five
for 78% of requests. With a complete tool index in the prompt
(``custom_agents.tool_index``) search matters for tools an agent does not hold
yet, which is exactly when the model cannot see their names.

How a tool is scored for a query:

- Words are lowercased, split on anything not a letter or digit, common
  filler words are dropped, and each word is normalized (``normalize``): a
  plural ending is removed and the word is cut to six letters, so "schedule",
  "scheduled" and "schedules" match. A few everyday words map to the words
  the catalog uses (``SYNONYMS``: "remember" to memory, "tweet" to post).
- Each tool's text comes in fields with weights: its id and display name and
  its function names (3), its operation names and description (2), and the
  descriptions of its functions (1).
- A query word adds the weight of the best field it appears in times its
  rarity across tools (inverse document frequency), so a word only a few
  tools use counts more than one every tool uses.
- A query that names a function exactly puts that function's tool first.

Ties keep the catalog order (tool id), so the ranking is deterministic.
"""

from __future__ import annotations

import math
import re
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from typing import Any

# Words that say nothing about which tool is meant.
STOPWORDS = frozenset(
    """
    a an the and or of to for in on at by with from into about as is are be been was were do does did
    can could would should will shall may might must i me my mine we our you your it its this that these
    those there here what which who whom whose how when where why some any all one ones more most
    need needs want wants please using use used new get let make up out over under via so if than then
    just also right now no not such anything anywhere something somewhere thing things
    """.split()
)
# Everyday words mapped to the words the catalog uses for the same thing.
SYNONYMS: Mapping[str, tuple[str, ...]] = {
    "remember": ("memory", "fact"),
    "recall": ("memory",),
    "remind": ("schedule",),
    "reminder": ("schedule",),
    "recurring": ("schedule",),
    "wiki": ("knowledge", "page"),
    "article": ("knowledge", "page"),
    "tweet": ("post", "x"),
    "twitter": ("x", "post"),
    "email": ("gmail", "mail"),
    "mail": ("gmail",),
    "password": ("credential",),
    "secret": ("credential",),
    "picture": ("image",),
    "photo": ("image",),
    "upload": ("attachment",),
    "uploaded": ("attachment",),
    "chat": ("conversation",),
    "talk": ("conversation",),
    "terminal": ("shell", "command"),
    "bash": ("shell", "command"),
    "percent": ("calculate",),
    "math": ("calculate",),
    "equation": ("calculate",),
    "permission": ("access",),
    "allowed": ("access",),
    "worker": ("agent", "worker"),
    "busy": ("pool", "worker"),
    "site": ("website", "page"),
    "website": ("web", "site"),
    "llm": ("model",),
    "notify": ("notification",),
    "tell": ("notification", "user"),
}
FIELD_WEIGHTS = {"name": 3.0, "operations": 2.0, "description": 2.0, "function_text": 1.0}
EXACT_FUNCTION_BONUS = 100.0
STEM_LENGTH = 6
_WORD = re.compile(r"[a-z0-9]+")


def normalize(word: str) -> str:
    """A word's comparison form: no plural ending, at most six letters."""

    if len(word) > 4 and word.endswith("ies"):
        word = word[:-3] + "y"
    elif len(word) > 3 and word.endswith("s") and not word.endswith("ss"):
        word = word[:-1]
    return word[:STEM_LENGTH]


def words(text: str) -> list[str]:
    """The normalized, meaningful words of a text, in order."""

    return [normalize(word) for word in _WORD.findall(str(text or "").lower()) if word not in STOPWORDS]


def query_words(query: str) -> set[str]:
    """The query's normalized words plus the catalog words its everyday words stand for."""

    raw = [word for word in _WORD.findall(str(query or "").lower()) if word not in STOPWORDS]
    found = {normalize(word) for word in raw}
    for word in raw:
        for synonym in SYNONYMS.get(word, ()):
            found.add(normalize(synonym))
    return found


@dataclass(frozen=True)
class ToolDocument:
    """One catalog tool's searchable words by field."""

    tool_id: str
    functions: tuple[str, ...]
    fields: Mapping[str, frozenset[str]]


def document(tool_def: Any, functions: Sequence[str], function_texts: Iterable[str]) -> ToolDocument:
    """The searchable fields of one tool: its names, operations, description and its functions' descriptions."""

    names = " ".join([tool_def.tool_id, tool_def.display_name, *functions])
    operations = " ".join(operation.name for operation in tool_def.operations)
    return ToolDocument(
        tool_id=tool_def.tool_id,
        functions=tuple(functions),
        fields={
            "name": frozenset(words(names)),
            "operations": frozenset(words(operations)),
            "description": frozenset(words(tool_def.description)),
            "function_text": frozenset(words(" ".join(function_texts))),
        },
    )


def _frequencies(documents: Sequence[ToolDocument], wanted: set[str]) -> dict[str, int]:
    """How many tools use each query word, in any field."""

    frequency: dict[str, int] = {}
    for doc in documents:
        present = set().union(*doc.fields.values())
        for word in wanted & present:
            frequency[word] = frequency.get(word, 0) + 1
    return frequency


def _score(doc: ToolDocument, wanted: set[str], frequency: Mapping[str, int], total: int, exact: set[str]) -> float:
    """One tool's score: each word's best field weight times its rarity, plus the exact-name bonus."""

    score = 0.0
    for word in wanted:
        weight = max((FIELD_WEIGHTS[field] for field, terms in doc.fields.items() if word in terms), default=0.0)
        if weight:
            score += weight * math.log(1.0 + total / frequency[word])
    if exact & set(doc.functions):
        score += EXACT_FUNCTION_BONUS
    return score


def rank(documents: Sequence[ToolDocument], query: str) -> list[tuple[float, ToolDocument]]:
    """Documents with a positive score for the query, best first (ties by tool id)."""

    wanted = query_words(query)
    exact = set(_WORD.findall(str(query or "").lower())) | set(str(query or "").lower().split())
    if not wanted and not exact:
        return [(0.0, doc) for doc in documents]
    total = len(documents) or 1
    frequency = _frequencies(documents, wanted)
    scored = [(score, doc) for doc in documents if (score := _score(doc, wanted, frequency, total, exact)) > 0]
    scored.sort(key=lambda item: (-item[0], item[1].tool_id))
    return scored


__all__ = ["FIELD_WEIGHTS", "STOPWORDS", "SYNONYMS", "ToolDocument", "document", "normalize", "query_words", "rank", "words"]
