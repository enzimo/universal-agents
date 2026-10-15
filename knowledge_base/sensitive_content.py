"""Fixed rules for content the knowledge base never stores or holds for review.

Two outcomes, decided by trusted code on every write (agent tools, curator
publishing, people's edits in the gateway, notes):

- **blocked** content is refused in every space: credentials (passwords,
  API keys and tokens, private keys, connection strings with a password) and
  identifiers that enable fraud (US social security numbers, payment card
  numbers, IBANs), plus administrators' own patterns
  (``agent.knowledge.sensitive_content.blocked_patterns``). The knowledge base
  is not a credential store; credentials belong in the broker.
- **review** content is allowed in a person's own space but holds a team-space
  change for the curator or the person: contact details (email addresses,
  phone numbers, street addresses) and birth dates. Overlap with the person's
  own memories is checked by the service, which knows the person.

A finding names the kind and where it is (line and column); it never carries
the matched value, so refusals, logs and tool results cannot repeat a secret.

The rules favour few false positives on ordinary technical text (commit
hashes, ids, version numbers, example placeholders such as ``<password>``)
because a refused write costs an agent a retry, while the curator and the
personal-memory overlap check cover what fixed rules miss.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Iterable, Iterator

from secure_tool_broker.redaction import SECRET_VALUE_RE


@dataclass(frozen=True)
class ContentFinding:
    """One rule match, by kind and position only."""

    kind: str
    label: str
    line: int
    column: int

    def describe(self) -> str:
        return f"{self.label} (line {self.line}, column {self.column})"


@dataclass(frozen=True)
class ContentScan:
    blocked: tuple[ContentFinding, ...] = ()
    review: tuple[ContentFinding, ...] = ()

    @property
    def is_blocked(self) -> bool:
        return bool(self.blocked)

    def blocked_message(self) -> str:
        found = "; ".join(finding.describe() for finding in self.blocked[:5])
        return (
            "The knowledge base never stores credentials or identity numbers. "
            f"Remove them and try again: {found}. "
            "Credentials belong in the broker's account links, not in pages or notes."
        )

    def review_kinds(self) -> list[str]:
        return sorted({finding.kind for finding in self.review})


@dataclass(frozen=True)
class _Rule:
    kind: str
    label: str
    pattern: re.Pattern[str]
    # Returns the span of the sensitive part, or None when the match is a false positive.
    accept: object = None
    group: int = 0


# Words that introduce a secret in "key = value" or prose ("my password is ...").
_SECRET_WORDS = (
    r"pass(?:word|wd|phrase|code)?|pwd|secret|client[_ -]?secret|api[_ -]?key|access[_ -]?key|"
    r"secret[_ -]?key|private[_ -]?key|access[_ -]?token|auth[_ -]?token|refresh[_ -]?token|token|pin"
)
_ASSIGNED_SECRET_RE = re.compile(
    rf"(?i)\b(?:{_SECRET_WORDS})\b[\"']?\s*(?:[:=]|=>)\s*[\"']?(?P<value>[^\s\"'`,;]{{4,}})"
)
_PROSE_SECRET_RE = re.compile(
    rf"(?i)\b(?:{_SECRET_WORDS})\s+(?:is|was|=|:)\s+[\"']?(?P<value>[^\s\"'`,;]{{4,}})"
)
# Placeholders and references are not secrets.
_PLACEHOLDER_RE = re.compile(
    r"(?i)^(?:<.*|\$.*|\{.*|%.*%|\*+|x{3,}|\.{3,}|"
    r"(?:your|my|the|a|an|example|sample|dummy|placeholder|redacted|changeme|none|null|"
    r"required|optional|set|unset|stored|hidden|secret|env|true|false)[\w-]*)$"
)
_PRIVATE_KEY_RE = re.compile(r"-----BEGIN (?:[A-Z0-9]+ )*PRIVATE KEY(?: BLOCK)?-----")
_AWS_KEY_ID_RE = re.compile(r"\b(?:AKIA|ASIA)[0-9A-Z]{16}\b")
_BEARER_RE = re.compile(r"(?i)\bbearer\s+[A-Za-z0-9._~+/-]{20,}=*")
_JWT_RE = re.compile(r"\beyJ[A-Za-z0-9_-]{8,}\.eyJ[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}")
_URL_CREDENTIAL_RE = re.compile(r"\b[a-z][a-z0-9+.-]{1,20}://[^\s:/@]+:(?P<value>[^\s/@]+)@[^\s/@]+")
# US SSN: separated form anywhere; nine digits only when introduced as an SSN.
_SSN_SEPARATED_RE = re.compile(r"(?<![\d-])(?!000|666|9\d\d)\d{3}([- ])(?!00)\d{2}\1(?!0000)\d{4}(?![\d-])")
_SSN_LABELLED_RE = re.compile(
    r"(?i)\b(?:ssn|social security(?: number| no\.?)?)\b\W{0,5}(?!000|666|9\d\d)\d{3}(?!00)\d{2}(?!0000)\d{4}\b"
)
_CARD_CANDIDATE_RE = re.compile(r"(?<![\d-])\d(?:[ -]?\d){12,18}(?![\d-])")
_IBAN_RE = re.compile(r"\b[A-Z]{2}\d{2}(?: ?[A-Z0-9]{4}){2,7}(?: ?[A-Z0-9]{1,3})?\b")
# Review rules (team space).
_EMAIL_RE = re.compile(r"\b[A-Za-z0-9._%+-]+@[A-Za-z0-9-]+(?:\.[A-Za-z0-9-]+)*\.[A-Za-z]{2,}\b")
_PHONE_RE = re.compile(r"(?<![\w.])(?:\+\d{1,3}[ .-]?)?(?:\(\d{2,4}\)[ .-]?)?\d{2,4}[ .-]\d{3,4}(?:[ .-]\d{2,4})?(?![\w.])")
_STREET_RE = re.compile(
    r"\b\d{1,5}\s+(?:[A-Z][a-z]+\s){1,3}(?:Street|St|Avenue|Ave|Road|Rd|Lane|Ln|Drive|Dr|Boulevard|Blvd|"
    r"Court|Ct|Way|Place|Pl|Terrace|Close)\b\.?"
)
_BIRTH_DATE_RE = re.compile(r"(?i)\b(?:born|date of birth|d\.?o\.?b\.?|birthday)\b[^.\n]{0,24}?\d{1,4}")


def _looks_secret(value: str) -> bool:
    """A value after a secret word is a secret only when it looks like one.

    Prose such as "password: rotate monthly" or "token = stored in the broker"
    is not; "hunter2", "P@ssw0rd!" and long random strings are.
    """

    if _PLACEHOLDER_RE.match(value):
        return False
    has_digit = any(ch.isdigit() for ch in value)
    has_symbol = any(not ch.isalnum() for ch in value)
    return has_digit or has_symbol or len(value) >= 16


def _luhn_ok(digits: str) -> bool:
    total = 0
    for index, char in enumerate(reversed(digits)):
        value = int(char)
        if index % 2 == 1:
            value *= 2
            if value > 9:
                value -= 9
        total += value
    return total % 10 == 0


def _card_brand_ok(digits: str) -> bool:
    """Known card number prefixes and lengths, so arbitrary long numbers pass."""

    length = len(digits)
    if digits[0] == "4":
        return length in (13, 16, 19)
    two, four = int(digits[:2]), int(digits[:4])
    if 51 <= two <= 55 or 2221 <= four <= 2720:
        return length == 16
    if two in (34, 37):
        return length == 15
    if digits.startswith(("6011", "65")) or 644 <= int(digits[:3]) <= 649:
        return length in (16, 19)
    if 3528 <= four <= 3589:
        return 16 <= length <= 19
    if two in (36, 38, 39) or 300 <= int(digits[:3]) <= 305:
        return 14 <= length <= 19
    return False


def _iban_ok(value: str) -> bool:
    compact = value.replace(" ", "")
    if not 15 <= len(compact) <= 34:
        return False
    rearranged = compact[4:] + compact[:4]
    try:
        number = int("".join(str(int(ch, 36)) for ch in rearranged))
    except ValueError:
        return False
    return number % 97 == 1


def _position(text: str, offset: int) -> tuple[int, int]:
    line = text.count("\n", 0, offset) + 1
    column = offset - (text.rfind("\n", 0, offset) + 1) + 1
    return line, column


def _finding(text: str, kind: str, label: str, offset: int) -> ContentFinding:
    line, column = _position(text, offset)
    return ContentFinding(kind=kind, label=label, line=line, column=column)


_SIMPLE_BLOCKED = (
    ("private_key", "a private key", _PRIVATE_KEY_RE),
    ("api_token", "an API key or token", SECRET_VALUE_RE),
    ("api_token", "an AWS access key", _AWS_KEY_ID_RE),
    ("api_token", "a bearer token", _BEARER_RE),
    ("api_token", "a signed token (JWT)", _JWT_RE),
    ("national_id", "a social security number", _SSN_SEPARATED_RE),
    ("national_id", "a social security number", _SSN_LABELLED_RE),
)


def _pattern_findings(text: str) -> Iterator[ContentFinding]:
    """Shapes that are secrets or identity numbers wherever they appear."""

    for kind, label, pattern in _SIMPLE_BLOCKED:
        for match in pattern.finditer(text):
            yield _finding(text, kind, label, match.start())


def _secret_value_findings(text: str) -> Iterator[ContentFinding]:
    """Values introduced as secrets ("password: ...", "my PIN is ...", "user:pass@host")."""

    for pattern in (_ASSIGNED_SECRET_RE, _PROSE_SECRET_RE):
        for match in pattern.finditer(text):
            if _looks_secret(match.group("value")):
                yield _finding(text, "password", "a password or secret value", match.start("value"))
    for match in _URL_CREDENTIAL_RE.finditer(text):
        if not _PLACEHOLDER_RE.match(match.group("value")):
            yield _finding(text, "password", "a password in a connection address", match.start("value"))


def _account_number_findings(text: str) -> Iterator[ContentFinding]:
    """Card numbers and IBANs, confirmed by their check digits."""

    for match in _CARD_CANDIDATE_RE.finditer(text):
        digits = re.sub(r"\D", "", match.group(0))
        if _card_brand_ok(digits) and _luhn_ok(digits):
            yield _finding(text, "payment_card", "a payment card number", match.start())
    for match in _IBAN_RE.finditer(text):
        if _iban_ok(match.group(0)):
            yield _finding(text, "bank_account", "a bank account number (IBAN)", match.start())


def _blocked_findings(text: str) -> Iterator[ContentFinding]:
    yield from _pattern_findings(text)
    yield from _secret_value_findings(text)
    yield from _account_number_findings(text)


# Content held for review, by kind: what a finding and a stored contribution's
# flags are called for people (the Web Chat Knowledge page shows these).
REVIEW_KIND_LABELS = {
    "email_address": "an email address",
    "phone_number": "a phone number",
    "street_address": "a street address",
    "birth_date": "a birth date",
}


def _review(text: str, kind: str, offset: int) -> ContentFinding:
    return _finding(text, kind, REVIEW_KIND_LABELS[kind], offset)


def _review_findings(text: str) -> Iterator[ContentFinding]:
    for match in _EMAIL_RE.finditer(text):
        yield _review(text, "email_address", match.start())
    for match in _PHONE_RE.finditer(text):
        if len(re.sub(r"\D", "", match.group(0))) >= 10:
            yield _review(text, "phone_number", match.start())
    for match in _STREET_RE.finditer(text):
        yield _review(text, "street_address", match.start())
    for match in _BIRTH_DATE_RE.finditer(text):
        yield _review(text, "birth_date", match.start())


@dataclass(frozen=True)
class SensitiveContentRules:
    """The fixed rules plus administrators' blocked patterns."""

    extra_blocked: tuple[tuple[str, re.Pattern[str]], ...] = field(default=())

    @classmethod
    def from_patterns(cls, patterns: Iterable[tuple[str, str]]) -> "SensitiveContentRules":
        return cls(extra_blocked=tuple((name, re.compile(pattern)) for name, pattern in patterns))

    def scan(self, *texts: str) -> ContentScan:
        """Scan each text separately; positions are within the text that matched."""

        blocked: list[ContentFinding] = []
        review: list[ContentFinding] = []
        for text in texts:
            if not text:
                continue
            blocked.extend(_dedupe(_blocked_findings(text)))
            for name, pattern in self.extra_blocked:
                for match in pattern.finditer(text):
                    blocked.append(_finding(text, f"custom:{name}", f"blocked pattern '{name}'", match.start()))
            review.extend(_dedupe(_review_findings(text)))
        return ContentScan(blocked=tuple(blocked), review=tuple(review))


def _dedupe(findings: Iterable[ContentFinding]) -> list[ContentFinding]:
    seen: set[tuple[str, int, int]] = set()
    unique: list[ContentFinding] = []
    for finding in findings:
        key = (finding.kind, finding.line, finding.column)
        if key not in seen:
            seen.add(key)
            unique.append(finding)
    return unique
