#!/usr/bin/env python3
"""Check that the repository carries no personal details.

Everything tracked here may end up in front of other people, and Git history
keeps every old version of every file. CONTRIBUTING.md ("Personal details")
lists what stays out and which made-up placeholders to use. This script catches
what a machine can recognize without a list of real values:

- email addresses outside the reserved example domains
- phone numbers outside the fictional 555 ranges
- home folder paths that name a real account
- commit author and committer identities other than GitHub's own and the ones
  listed in `.personal-details-allowlist`

Names, handles and copied chats cannot be recognized without listing them, so
they still need a reviewer.

Modes:
  (no option)      tracked files, plus every commit reachable from HEAD:
                   identities, messages and every line the commits added
  --staged         staged files and the identity the next commit will use
                   (pre-commit hook)
  --message FILE   a commit message (commit-msg hook)
  --pushed         commits a push would upload, read from the pre-push hook's
                   standard input

Needs only Python 3.9+ and Git, so it works in projects in any language.
"""

from __future__ import annotations

import argparse
import re
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path

RULES = 'See CONTRIBUTING.md, "Personal details".'
IDENTITY_HINT = (
    "Commit only with an identity listed in .personal-details-allowlist "
    "(git config user.name / user.email)."
)
HISTORY_HINT = (
    "Commits that are not pushed yet can be fixed with git commit --amend or git rebase. "
    "Once pushed, deleting the detail in a new commit is not enough: "
    "see docs/personal-details-cleanup.md."
)
ALLOWLIST_FILE = ".personal-details-allowlist"

# A local part right after ":" or "/" is a URL's user info
# (https://user:secret@host), not an address.
EMAIL = re.compile(r"(?<![\w.%+:/-])[A-Za-z0-9._%+-]+@((?:[A-Za-z0-9-]+\.)+[A-Za-z]{2,})\b")
# Domains reserved for examples (RFC 2606, RFC 6761) or private networks.
EXAMPLE_DOMAINS = ("example.com", "example.net", "example.org")
EXAMPLE_TOP_LEVEL_DOMAINS = frozenset({"example", "test", "invalid", "localhost", "internal", "local"})
# Service mailboxes that never belong to a person, such as the address in an
# assistant's Co-Authored-By trailer.
SERVICE_LOCAL_PARTS = frozenset({"noreply", "no-reply", "donotreply", "do-not-reply"})
# Image files named like addresses, such as "icon@2x.png".
FILE_EXTENSION_DOMAINS = frozenset({"png", "jpg", "jpeg", "gif", "svg", "webp", "avif", "ico", "icns"})
# GitHub's shared SSH address, and the one in Dependabot's Signed-off-by lines.
SHARED_ADDRESSES = frozenset({"git@github.com", "support@github.com"})

PHONE = re.compile(r"(?<![\w.+/-])\+[1-9][\d ().-]{8,18}\d")
# North American numbers set aside for examples: area code 555, or the
# 555-0100 to 555-0199 lines in any area code.
FICTIONAL_PHONE = re.compile(r"1555\d{7}|1\d{3}55501\d{2}")

HOME_FOLDER = re.compile(r"(?:/home/|/Users/|[A-Za-z]:\\+Users\\+)([A-Za-z0-9._-]+)")
# Generic accounts: placeholders, container and CI users, and shared folders.
# Projects add their own placeholder names with "account:" allowlist lines.
GENERIC_ACCOUNTS = frozenset(
    {
        "alice",
        "bob",
        "jordan",
        "me",
        "person",
        "user",
        "username",
        "you",
        "example",
        "exampleuser",
        "developer",
        "runner",
        "node",
        "ubuntu",
        "vscode",
        "codespace",
        "shared",
        "public",
    }
)
# Placeholders written in capitals with underscores, such as YOUR_USER.
PLACEHOLDER_ACCOUNT = re.compile(r"[A-Z][A-Z0-9]*(?:_[A-Z0-9]+)+")

# Generated lock files repeat third-party package metadata, such as a package
# author's published address, and are not edited by hand.
LOCK_FILES = frozenset(
    {
        "package-lock.json",
        "npm-shrinkwrap.json",
        "yarn.lock",
        "pnpm-lock.yaml",
        "bun.lock",
        "Cargo.lock",
        "poetry.lock",
        "uv.lock",
        "Pipfile.lock",
        "Gemfile.lock",
        "composer.lock",
        "go.sum",
    }
)

# Identities GitHub itself commits with: web merges and edits are committed by
# "GitHub <noreply@github.com>", and apps such as Dependabot commit as
# "<name>[bot] <<id>+<name>[bot]@users.noreply.github.com>".
GITHUB_WEB_IDENTITY = ("GitHub", "noreply@github.com")
GITHUB_APP_EMAIL = re.compile(r"\d+\+(?P<name>[A-Za-z0-9-]+\[bot\])@users\.noreply\.github\.com")
# The output of `git var GIT_AUTHOR_IDENT`: "Name <email> 1760000000 +0000".
GIT_IDENT = re.compile(r"(?P<name>.*) <(?P<email>[^>]*)> \d+ [+-]\d{4}")
HUNK_START = re.compile(r"@@ -\S+ \+(?P<line>\d+)")
SCISSORS = "# ------------------------ >8 ------------------------"


@dataclass(frozen=True)
class Finding:
    where: str
    kind: str
    value: str

    def describe(self, redact: bool) -> str:
        if redact:
            return f"{self.where}: {self.kind}"
        return f"{self.where}: {self.kind}: {self.value}"


@dataclass(frozen=True)
class Allowlist:
    """Real values the project has decided may appear.

    `identity:` lines name commit identities ("Name <email>"); their addresses
    may also appear in files. `text:` lines name other values that may appear in
    files, such as an upstream author's published credit. `account:` lines name
    made-up account names used in example home folder paths.
    """

    identities: frozenset = frozenset()
    values: frozenset = frozenset()
    accounts: frozenset = frozenset()

    @classmethod
    def parse(cls, text: str) -> Allowlist:
        identities = set()
        values = set()
        accounts = set()
        for number, raw in enumerate(text.splitlines(), start=1):
            line = raw.strip()
            if not line or line.startswith("#"):
                continue
            keyword, _, value = (part.strip() for part in line.partition(":"))
            if keyword == "identity":
                match = re.fullmatch(r"(?P<name>.+?) <(?P<email>[^>]+)>", value)
                if match is None:
                    raise ValueError(f"{ALLOWLIST_FILE}:{number}: expected 'identity: Name <email>'")
                identities.add((match["name"], match["email"].lower()))
                values.add(match["email"].lower())
            elif keyword == "text" and value:
                values.add(value.lower())
            elif keyword == "account" and value:
                accounts.add(value.lower())
            else:
                raise ValueError(f"{ALLOWLIST_FILE}:{number}: expected 'identity: ...', 'text: ...' or 'account: ...'")
        return cls(frozenset(identities), frozenset(values), frozenset(accounts))

    @classmethod
    def load(cls, path: Path) -> Allowlist:
        return cls.parse(path.read_text(encoding="utf-8")) if path.is_file() else cls()

    def allows(self, value: str) -> bool:
        return value.lower() in self.values


def _is_allowed_email(address: str, allowlist: Allowlist) -> bool:
    local_part, _, domain = address.lower().rpartition("@")
    if address.lower() in SHARED_ADDRESSES or local_part in SERVICE_LOCAL_PARTS or allowlist.allows(address):
        return True
    if any(domain == example or domain.endswith(f".{example}") for example in EXAMPLE_DOMAINS):
        return True
    top_level = domain.rsplit(".", 1)[-1]
    return top_level in EXAMPLE_TOP_LEVEL_DOMAINS or top_level in FILE_EXTENSION_DOMAINS


def _is_placeholder_account(account: str, allowlist: Allowlist) -> bool:
    lowered = account.lower()
    return lowered in GENERIC_ACCOUNTS or lowered in allowlist.accounts or PLACEHOLDER_ACCOUNT.fullmatch(account) is not None


def _is_lock_file(relative_path: str) -> bool:
    return relative_path.rsplit("/", 1)[-1] in LOCK_FILES


def _is_real_phone(number: str) -> bool:
    digits = re.sub(r"\D", "", number)
    if not 10 <= len(digits) <= 15:
        return False  # Too short or long for a phone number: versions, offsets, counts.
    return FICTIONAL_PHONE.fullmatch(digits) is None


def personal_details(text: str, allowlist: Allowlist = Allowlist()) -> list[tuple[str, str]]:
    """Return (kind, value) for each real-looking address, phone number or home path in text."""
    found = [
        ("email address", match.group(0))
        for match in EMAIL.finditer(text)
        if not _is_allowed_email(match.group(0), allowlist)
    ]
    found += [
        ("phone number", match.group(0))
        for match in PHONE.finditer(text)
        if _is_real_phone(match.group(0)) and not allowlist.allows(match.group(0))
    ]
    found += [
        ("home folder", match.group(0))
        for match in HOME_FOLDER.finditer(text)
        if not _is_placeholder_account(match.group(1), allowlist) and not allowlist.allows(match.group(0))
    ]
    return found


def identity_allowed(name: str, email: str, allowlist: Allowlist) -> bool:
    if (name, email.lower()) == GITHUB_WEB_IDENTITY:
        return True
    app = GITHUB_APP_EMAIL.fullmatch(email)
    if app is not None and app["name"] == name:
        return True
    return (name, email.lower()) in allowlist.identities


def git(repo: Path, *args: str) -> bytes:
    return subprocess.run(["git", *args], cwd=repo, check=True, capture_output=True).stdout


def _text(content: bytes) -> str | None:
    if b"\0" in content:
        return None  # Binary file.
    return content.decode("utf-8", errors="replace")


def scan_lines(where: str, text: str, allowlist: Allowlist) -> list[Finding]:
    return [
        Finding(f"{where}:{number}", kind, value)
        for number, line in enumerate(text.splitlines(), start=1)
        for kind, value in personal_details(line, allowlist)
    ]


def scan_tracked_files(repo: Path, allowlist: Allowlist) -> list[Finding]:
    # Only tracked files are shared; ignored local configuration stays local.
    findings = []
    for relative_path in filter(None, git(repo, "ls-files", "-z").decode().split("\0")):
        path = repo / relative_path
        if _is_lock_file(relative_path) or not path.is_file():
            continue  # Deleted in the working tree but not yet committed.
        text = _text(path.read_bytes())
        if text is not None:
            findings += scan_lines(relative_path, text, allowlist)
    return findings


def scan_staged_files(repo: Path, allowlist: Allowlist) -> list[Finding]:
    # Read the staged version, which is what the commit will contain.
    staged = git(repo, "diff", "--cached", "--name-only", "-z", "--diff-filter=ACMR").decode()
    findings = []
    for relative_path in filter(None, staged.split("\0")):
        if _is_lock_file(relative_path):
            continue
        text = _text(git(repo, "cat-file", "blob", f":{relative_path}"))
        if text is not None:
            findings += scan_lines(f"{relative_path} (staged)", text, allowlist)
    return findings


def scan_next_commit_identity(repo: Path, allowlist: Allowlist) -> list[Finding]:
    findings = []
    for role, variable in (("author", "GIT_AUTHOR_IDENT"), ("committer", "GIT_COMMITTER_IDENT")):
        ident = git(repo, "var", variable).decode().strip()
        match = GIT_IDENT.fullmatch(ident)
        name, email = (match["name"], match["email"]) if match else (ident, "")
        if not identity_allowed(name, email, allowlist):
            findings.append(Finding(f"next commit {role}", "commit identity", f"{name} <{email}>"))
    return findings


def scan_message(text: str, allowlist: Allowlist, where: str = "commit message") -> list[Finding]:
    kept = []
    for line in text.splitlines():
        if line.startswith(SCISSORS):
            break  # Git drops everything below this line (commit --verbose).
        kept.append("" if line.startswith("#") else line)  # Keep line numbers.
    return scan_lines(where, "\n".join(kept), allowlist)


def scan_commits(repo: Path, revisions: list[str], allowlist: Allowlist) -> list[Finding]:
    """Check identities, messages and every added line of the selected commits.

    A detail that a later commit deleted is still in history, so the whole
    history is read, not only the latest files.
    """
    findings = []
    log = git(repo, "log", "-z", "--format=%H%x1f%an%x1f%ae%x1f%cn%x1f%ce%x1f%B", *revisions, "--")
    for record in filter(None, log.decode("utf-8", errors="replace").split("\0")):
        sha, author, author_email, committer, committer_email, message = record.split("\x1f", 5)
        commit = f"commit {sha[:12]}"
        for role, name, email in (("author", author, author_email), ("committer", committer, committer_email)):
            if not identity_allowed(name, email, allowlist):
                findings.append(Finding(f"{commit} {role}", "commit identity", f"{name} <{email}>"))
        findings += scan_message(message, allowlist, f"{commit} message")

    # Merges are compared with their first parent so conflict resolutions are read too.
    patches = git(
        repo,
        "log",
        "--patch",
        "--unified=0",
        "--no-color",
        "--no-ext-diff",
        "--no-textconv",
        "--no-renames",
        "--diff-merges=first-parent",
        "--src-prefix=a/",
        "--dst-prefix=b/",
        "--format=%x00%H",
        *revisions,
        "--",
    )
    commit = path = ""
    line_number = 0
    in_hunk = False
    for line in patches.decode("utf-8", errors="replace").split("\n"):
        if line.startswith("\0"):
            commit, in_hunk = f"commit {line[1:13]}", False
        elif line.startswith("diff --git "):
            in_hunk = False
        elif line.startswith("@@"):
            hunk = HUNK_START.match(line)
            line_number = int(hunk["line"]) if hunk else 0
            in_hunk = True
        elif not in_hunk:
            if line.startswith("+++ "):
                path = line[len("+++ b/") :] if line.startswith("+++ b/") else line[len("+++ ") :]
        elif line.startswith("+"):
            if _is_lock_file(path):
                continue
            for kind, value in personal_details(line[1:], allowlist):
                findings.append(Finding(f"{commit} {path}:{line_number}", kind, value))
            line_number += 1
    return findings


def pushed_tips(push_info: str) -> list[str]:
    """Commits being pushed, from pre-push input lines "<ref> <sha> <remote ref> <remote sha>"."""
    tips = []
    for line in push_info.splitlines():
        parts = line.split()
        if len(parts) == 4 and set(parts[1]) != {"0"}:  # All zeros: a branch deletion.
            tips.append(parts[1])
    return tips


def has_commits(repo: Path) -> bool:
    result = subprocess.run(["git", "rev-parse", "--verify", "--quiet", "HEAD"], cwd=repo, capture_output=True)
    return result.returncode == 0


def report(findings: list[Finding], redact: bool) -> int:
    if not findings:
        return 0
    print("Personal details found:", file=sys.stderr)
    for finding in findings:
        print(f"  {finding.describe(redact)}", file=sys.stderr)
    print(RULES, file=sys.stderr)
    if any(finding.kind == "commit identity" for finding in findings):
        print(IDENTITY_HINT, file=sys.stderr)
    if any(finding.where.startswith("commit ") for finding in findings):
        print(HISTORY_HINT, file=sys.stderr)
    return 1


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Check that the repository carries no personal details.")
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--staged", action="store_true", help="check staged files and the next commit's identity")
    mode.add_argument("--message", metavar="FILE", type=Path, help="check a commit message file")
    mode.add_argument("--pushed", action="store_true", help="check the commits a push would upload (pre-push input)")
    parser.add_argument(
        "--redact",
        action="store_true",
        help="print where each detail is and its kind, but not the detail itself (for shared logs such as CI)",
    )
    args = parser.parse_args(argv)

    repo = Path(git(Path.cwd(), "rev-parse", "--show-toplevel").decode().strip())
    allowlist = Allowlist.load(repo / ALLOWLIST_FILE)
    if args.staged:
        findings = scan_staged_files(repo, allowlist) + scan_next_commit_identity(repo, allowlist)
    elif args.message is not None:
        findings = scan_message(args.message.read_text(encoding="utf-8", errors="replace"), allowlist)
    elif args.pushed:
        tips = pushed_tips(sys.stdin.read())
        findings = scan_commits(repo, [*tips, "--not", "--remotes"], allowlist) if tips else []
    else:
        findings = scan_tracked_files(repo, allowlist)
        if has_commits(repo):
            findings += scan_commits(repo, ["HEAD"], allowlist)
    return report(findings, args.redact)


if __name__ == "__main__":
    sys.exit(main())
