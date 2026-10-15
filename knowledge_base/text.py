"""Text helpers shared by the knowledge service and store.

- Page names (slugs) are lowercase words joined by hyphens, so agents and
  people can write ``[[github-release-process]]`` and get the same page.
- Links are ``[[slug]]`` or ``[[slug|label]]``; ``[[team/slug]]`` points at the
  team space from a personal page.
- Search terms are plain words only. The store builds its own query syntax
  from them, so nothing an agent types is ever executed as query syntax.
- Text fingerprints ignore whitespace and case, so an edit that only restores
  an earlier revision's text is recognized as a repeat.
- Word runs (shingles) find personal-memory text copied into team pages.
"""

from __future__ import annotations

import hashlib
import re
import unicodedata

from knowledge_base.vocabulary import KnowledgeSpace

SLUG_MAX_CHARS = 80
_SLUG_SEPARATORS_RE = re.compile(r"[^a-z0-9]+")
_LINK_RE = re.compile(r"\[\[([^\[\]|\n]{1,200})(?:\|[^\[\]\n]{0,200})?\]\]")
_WORD_RE = re.compile(r"[^\W_]+", re.UNICODE)
MAX_QUERY_TERMS = 12
# Shingle length for the personal-memory overlap check: long enough that
# common phrases ("in the team space") do not match, short enough that a
# copied preference ("prefers vegan meals and avoids peanuts") does.
OVERLAP_SHINGLE_WORDS = 4
_STOP_WORDS = frozenset(
    "a an and are as at be by for from has have i in is it its my of on or our that the their them they "
    "this to was we were will with you your".split()
)


def slugify(value: str) -> str:
    normalized = unicodedata.normalize("NFKD", value or "").encode("ascii", "ignore").decode("ascii").lower()
    slug = _SLUG_SEPARATORS_RE.sub("-", normalized).strip("-")
    return slug[:SLUG_MAX_CHARS].strip("-")


def written_links(body: str) -> list[tuple[str, str]]:
    """Each distinct ``[[link]]`` target as written (before any ``|label``) and as ``parse_links`` names it."""

    found: dict[str, str] = {}
    for raw in _LINK_RE.findall(body or ""):
        written = raw.strip()
        named = parse_links(f"[[{written}]]")
        if written not in found and named:
            found[written] = named[0]
    return list(found.items())


def parse_links(body: str) -> list[str]:
    """Link targets in order of first appearance, as ``slug`` or ``team/slug``."""

    targets: list[str] = []
    for raw in _LINK_RE.findall(body or ""):
        target = raw.strip()
        prefix = ""
        if "/" in target:
            space, _, rest = target.partition("/")
            prefix = f"{slugify(space)}/" if slugify(space) else ""
            target = rest
        slug = slugify(target)
        if slug and f"{prefix}{slug}" not in targets:
            targets.append(f"{prefix}{slug}")
    return targets


def link_targets(space: KnowledgeSpace, body: str) -> tuple[tuple[KnowledgeSpace, str], ...]:
    """Resolve links identically for normal writes and transfer validation.

    Bare names stay in the page's space. Team pages cannot link to a person's
    personal space, and unknown space names do not create stored links.
    """

    targets: list[tuple[KnowledgeSpace, str]] = []
    for target in parse_links(body):
        space_text, _, slug = target.rpartition("/")
        try:
            target_space = KnowledgeSpace(space_text) if space_text else space
        except ValueError:
            continue
        if target_space is KnowledgeSpace.PERSONAL and space is KnowledgeSpace.TEAM:
            continue
        targets.append((target_space, slug))
    return tuple(targets)


def text_fingerprint(*parts: str) -> str:
    """A digest that ignores case and whitespace differences."""

    normalized = "\x1f".join(" ".join((part or "").lower().split()) for part in parts)
    return hashlib.sha256(normalized.encode("utf-8")).hexdigest()


def query_terms(text: str) -> list[str]:
    """Plain search words: letters and digits only, lowercase, at most ``MAX_QUERY_TERMS``."""

    terms: list[str] = []
    for word in _WORD_RE.findall((text or "").lower()):
        if len(word) < 2 or word in _STOP_WORDS or word in terms:
            continue
        terms.append(word)
        if len(terms) == MAX_QUERY_TERMS:
            break
    return terms


def words(text: str) -> list[str]:
    return _WORD_RE.findall((text or "").lower())


def shingles(text: str, size: int = OVERLAP_SHINGLE_WORDS) -> set[tuple[str, ...]]:
    """Runs of ``size`` consecutive words, skipping runs made only of stop words."""

    tokens = words(text)
    runs: set[tuple[str, ...]] = set()
    for index in range(len(tokens) - size + 1):
        run = tuple(tokens[index : index + size])
        if not all(token in _STOP_WORDS for token in run):
            runs.add(run)
    return runs


def overlaps(candidate: str, private_text: str, size: int = OVERLAP_SHINGLE_WORDS) -> bool:
    """Whether ``candidate`` repeats a run of ``size`` words from ``private_text``.

    A private text shorter than ``size`` words is compared as a whole word
    sequence, so short preferences ("vegan diet") are still caught.
    """

    private_words = words(private_text)
    if not private_words:
        return False
    if len(private_words) < size:
        candidate_words = words(candidate)
        width = len(private_words)
        if width < 2:
            return False
        target = tuple(private_words)
        return any(tuple(candidate_words[i : i + width]) == target for i in range(len(candidate_words) - width + 1))
    return bool(shingles(private_text, size) & shingles(candidate, size))
