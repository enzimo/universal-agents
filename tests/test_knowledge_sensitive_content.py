"""Fixed rules for what the knowledge base never stores, and what it flags for review."""

from __future__ import annotations

import pytest

from knowledge_base.sensitive_content import SensitiveContentRules

RULES = SensitiveContentRules()

BLOCKED = {
    "password assignment": "password: hunter2",
    "password in prose": "the password is P@ssw0rd!",
    "PIN in prose": "My PIN is 4821",
    "quoted api key": "api_key = 'sk-abcdefghijklmnopqrstuv'",
    "client secret yaml": 'client_secret: "b1946ac92492d2347c6235b4d2611184"',
    "GitHub token": "use ghp_abcdefghijklmnopqrstuvwxyz0123 for the bot",
    "AWS access key": "AKIAABCDEFGHIJKLMNOP",
    "bearer header": "Authorization: Bearer abcdefghijklmnopqrstuvwxyz123456",
    "JWT": "eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiIxMjM0NTY3ODkwIn0.dozjgNryP4J3jVmNHl0w5N_XgL0n3I9PlFUP0THsR8U",
    "private key": "-----BEGIN OPENSSH PRIVATE KEY-----\nb3BlbnNzaC1rZXktdjEAAAAA",
    "connection string": "postgresql://team:s3cret@db.internal:5432/app",
    "SSN with dashes": "SSN 123-45-6789",
    "SSN labelled digits": "social security number 123456789",
    "Visa": "card 4111 1111 1111 1111",
    "Mastercard with dashes": "5500-0000-0000-0004",
    "Amex": "378282246310005",
    "IBAN": "IBAN GB82 WEST 1234 5698 7654 32",
}

ALLOWED = {
    "prose about passwords": "password: rotate monthly",
    "instructions": "Set the password with /auth model, never in a page.",
    "placeholder": "token = <your token>",
    "environment reference": "password=${DB_PASSWORD}",
    "template": "api_key: {{ secrets.api_key }}",
    "token stored elsewhere": "the token is stored in the broker",
    "commit hash": "fixed in commit 4f2a9c1e8b7d6a5f4e3d2c1b0a9f8e7d6c5b4a3f",
    "uuid": "id 550e8400-e29b-41d4-a716-446655440000",
    "version and date": "version 1.2.3 released 2026-10-08",
    "ports and timeouts": "use port 5432 and a 30000 ms timeout",
    "placeholder connection string": "postgresql://user:<password>@host/db",
    "non-card long number": "order 1234567890123456 shipped",
    "ip address": "call 192.168.1.10",
    "pin a dependency": "pin the dependency to 2.0",
    "isbn": "ISBN 978-3-16-148410-0",
    "invalid ssn area": "case 000-12-3456",
}

REVIEW = {
    "email": ("mail alice@example.com", "email_address"),
    "phone": ("call +1 415-555-0132", "phone_number"),
    "street address": ("lives at 42 Marrowind Lane", "street_address"),
    "birth date": ("born on 3 May 1990", "birth_date"),
}


@pytest.mark.parametrize("text", BLOCKED.values(), ids=BLOCKED.keys())
def test_blocked(text):
    scan = RULES.scan(text)
    assert scan.is_blocked, text


@pytest.mark.parametrize("text", ALLOWED.values(), ids=ALLOWED.keys())
def test_allowed(text):
    scan = RULES.scan(text)
    assert not scan.blocked, [finding.describe() for finding in scan.blocked]


@pytest.mark.parametrize(("text", "kind"), REVIEW.values(), ids=REVIEW.keys())
def test_review(text, kind):
    scan = RULES.scan(text)
    assert not scan.blocked and kind in scan.review_kinds()


def test_findings_never_carry_the_value():
    secret = "Zq9!vT4#pL2s"
    scan = RULES.scan(f"line one\nadmin password: {secret}")
    assert scan.is_blocked
    message = scan.blocked_message()
    assert secret not in message and secret not in repr(scan)
    finding = scan.blocked[0]
    assert (finding.line, finding.kind) == (2, "password")


def test_administrators_add_blocked_patterns():
    rules = SensitiveContentRules.from_patterns([("project codename", r"(?i)\bbluebird\b")])
    scan = rules.scan("Project Bluebird ships in May.")
    assert [finding.kind for finding in scan.blocked] == ["custom:project codename"]
    assert "Bluebird" not in scan.blocked_message()
