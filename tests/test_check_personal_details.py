"""Tests for scripts/check_personal_details.py.

Uses only the standard library, so it runs with `python3 -m unittest` in a
project of any language; pytest also collects it.

Sample values are assembled at run time so the scan of this file stays clean.
"""

from __future__ import annotations

import importlib.util
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
SCRIPT = PROJECT_ROOT / "scripts" / "check_personal_details.py"

_spec = importlib.util.spec_from_file_location("check_personal_details", SCRIPT)
checker = importlib.util.module_from_spec(_spec)
sys.modules[_spec.name] = checker  # dataclasses look the module up while the file loads.
_spec.loader.exec_module(checker)

AT = "@"
REAL_ADDRESS = f"pat{AT}mailbox.co"
MAINTAINER = ("example-owner", f"1000+example-owner{AT}users.noreply.github.com")
ALLOWLIST = checker.Allowlist.parse(f"identity: {MAINTAINER[0]} <{MAINTAINER[1]}>\n")


class DetectorTests(unittest.TestCase):
    def test_real_looking_details_are_found(self):
        samples = [
            f"write to {REAL_ADDRESS}",
            f"Pat <pat.lee+ci{AT}corp.example.co.uk>",
            f"1234+pat{AT}users.noreply.github.com",
            "call +49 30 " + "1234 5678",
            "text +1 (312) " + "867-5309",
            "cd /home/" + "pat/projects",
            "open /Users/" + "pat.lee/Desktop",
            r"C:\\Users\\" + "pat\\repo",
        ]
        for text in samples:
            with self.subTest(text=text):
                self.assertNotEqual(checker.personal_details(text, ALLOWLIST), [])

    def test_placeholders_and_non_contact_text_pass(self):
        samples = [
            f"alice{AT}example.com, bob{AT}mail.example.net, ops{AT}acme.example",
            f"person{AT}team.test and nobody{AT}example.invalid",
            f"git clone git{AT}github.com:example-owner/repo.git",
            f"https://user:secret{AT}api.vendor.com/v1",
            f"postgresql://synthetic{AT}raw.example:5432/db",
            f"Co-Authored-By: Assistant <noreply{AT}vendor.com>",
            f"Signed-off-by: dependabot[bot] <support{AT}github.com>",
            f"Co-authored-by: dependabot[bot] <49699333+dependabot[bot]{AT}users.noreply.github.com>",
            f"maintained as {MAINTAINER[1]}",
            "+15551234567, +1 555 000 0001, +1 415-555-0132",
            "version +1.2.3, offset +05:00, count +12345, date 2026-10-14T12:00:00+00:00",
            "/home/node/app, /home/runner/work, /Users/Shared, /Users/developer/project, /home/<user>/repo",
            r"/Users/me/notes, /home/alice/src, C:\Users\YOUR_BUILD_USER\work",
            f"icon{AT}2x.png and logo{AT}3x.webp",
            f"{AT}decorator and npm {AT}scope/package{AT}1.2.3",
        ]
        for text in samples:
            with self.subTest(text=text):
                self.assertEqual(checker.personal_details(text, ALLOWLIST), [])

    def test_account_entries_allow_project_placeholders(self):
        allowlist = checker.Allowlist.parse("account: jane\n")
        self.assertEqual(checker.personal_details("/home/" + "jane and C:\\Users\\" + "Jane", allowlist), [])
        self.assertNotEqual(checker.personal_details("/home/" + "pat", allowlist), [])

    def test_text_entries_allow_specific_values(self):
        allowlist = checker.Allowlist.parse(f"# upstream credit\ntext: {REAL_ADDRESS}\n")
        self.assertEqual(checker.personal_details(f"authors = [<{REAL_ADDRESS}>]", allowlist), [])
        self.assertNotEqual(checker.personal_details(f"other{AT}mailbox.co", allowlist), [])


class IdentityTests(unittest.TestCase):
    def test_github_and_listed_identities_pass(self):
        allowed = [
            ("GitHub", f"noreply{AT}github.com"),
            ("dependabot[bot]", f"49699333+dependabot[bot]{AT}users.noreply.github.com"),
            ("github-actions[bot]", f"41898282+github-actions[bot]{AT}users.noreply.github.com"),
            MAINTAINER,
            (MAINTAINER[0], MAINTAINER[1].upper()),
        ]
        for name, email in allowed:
            with self.subTest(name=name):
                self.assertTrue(checker.identity_allowed(name, email, ALLOWLIST))

    def test_other_identities_fail(self):
        refused = [
            ("pat", REAL_ADDRESS),
            ("Pat Lee", MAINTAINER[1]),  # A real name with the listed address.
            ("pat", f"1234+pat{AT}users.noreply.github.com"),  # Another login's noreply address.
            ("pat", f"49699333+dependabot[bot]{AT}users.noreply.github.com"),
        ]
        for name, email in refused:
            with self.subTest(name=name, email=email):
                self.assertFalse(checker.identity_allowed(name, email, ALLOWLIST))

    def test_malformed_allowlist_lines_are_rejected(self):
        for text in ("identity: no-address", "owner: someone", "text:", "account:"):
            with self.subTest(text=text), self.assertRaises(ValueError):
                checker.Allowlist.parse(text)


class MessageTests(unittest.TestCase):
    def test_comment_lines_and_verbose_diff_are_ignored(self):
        message = "\n".join(
            [
                "Fix the parser",
                "",
                f"Reported by {REAL_ADDRESS}",
                f"# Author: {REAL_ADDRESS}",
                checker.SCISSORS,
                f"+ {REAL_ADDRESS}",
            ]
        )
        findings = checker.scan_message(message, ALLOWLIST)
        self.assertEqual([finding.where for finding in findings], ["commit message:3"])

    def test_redacted_report_omits_the_value(self):
        finding = checker.Finding("README.md:1", "email address", REAL_ADDRESS)
        self.assertNotIn(REAL_ADDRESS, finding.describe(redact=True))
        self.assertIn(REAL_ADDRESS, finding.describe(redact=False))


class RepositoryTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.repo = Path(self._tmp.name) / "repo"
        self.repo.mkdir()
        self.git("init", "--quiet", "--initial-branch=main")
        # Local settings so the developer's own hooks, signing and identity stay out.
        self.git("config", "core.hooksPath", os.devnull)
        self.git("config", "commit.gpgsign", "false")
        self.use_identity(*MAINTAINER)

    def tearDown(self):
        self._tmp.cleanup()

    def git(self, *args: str, cwd: Path | None = None) -> str:
        result = subprocess.run(["git", *args], cwd=cwd or self.repo, capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stderr)
        return result.stdout.strip()

    def use_identity(self, name: str, email: str):
        self.git("config", "user.name", name)
        self.git("config", "user.email", email)

    def write(self, name: str, content: str):
        (self.repo / name).write_text(content, encoding="utf-8")

    def commit(self, message: str, *files: str):
        self.git("add", *files)
        self.git("commit", "--quiet", "-m", message)

    def test_only_tracked_text_files_are_scanned(self):
        self.write("tracked.md", f"contact {REAL_ADDRESS}\n")
        self.write("local.env", f"OWNER={REAL_ADDRESS}\n")
        (self.repo / "image.bin").write_bytes(f"\0{REAL_ADDRESS}".encode())
        (self.repo / "web").mkdir()
        self.write("web/package-lock.json", f'{{"author": "{REAL_ADDRESS}"}}\n')
        self.git("add", "tracked.md", "image.bin", "web/package-lock.json")

        findings = checker.scan_tracked_files(self.repo, ALLOWLIST)
        self.assertEqual([finding.describe(redact=False) for finding in findings], [f"tracked.md:1: email address: {REAL_ADDRESS}"])

    def test_staged_mode_reads_the_staged_version_and_next_identity(self):
        self.write("notes.md", f"owner {REAL_ADDRESS}\n")
        self.git("add", "notes.md")
        self.write("notes.md", "owner alice\n")  # Fixed in the working tree but not staged.
        self.assertEqual(
            [finding.where for finding in checker.scan_staged_files(self.repo, ALLOWLIST)],
            ["notes.md (staged):1"],
        )
        self.assertEqual(checker.scan_next_commit_identity(self.repo, ALLOWLIST), [])

        self.use_identity("pat", REAL_ADDRESS)
        findings = checker.scan_next_commit_identity(self.repo, ALLOWLIST)
        self.assertEqual([finding.where for finding in findings], ["next commit author", "next commit committer"])

    def test_history_keeps_details_that_later_commits_deleted(self):
        self.write("readme.md", "hello\n")
        self.commit("Start", "readme.md")
        self.write("readme.md", f"hello\nmail {REAL_ADDRESS}\n")
        self.commit("Add contact", "readme.md")
        self.write("readme.md", "hello\n")
        self.commit("Remove contact", "readme.md")

        self.assertEqual(checker.scan_tracked_files(self.repo, ALLOWLIST), [])
        findings = checker.scan_commits(self.repo, ["HEAD"], ALLOWLIST)
        self.assertEqual(len(findings), 1)
        self.assertRegex(findings[0].where, r"^commit [0-9a-f]{12} readme\.md:2$")

    def test_history_checks_identities_and_messages(self):
        self.use_identity("pat", REAL_ADDRESS)
        self.write("a.md", "a\n")
        self.commit(f"Start\n\nThanks {REAL_ADDRESS}", "a.md")

        kinds = sorted((finding.where.split(" ", 2)[2], finding.kind) for finding in checker.scan_commits(self.repo, ["HEAD"], ALLOWLIST))
        self.assertEqual(
            kinds,
            [("author", "commit identity"), ("committer", "commit identity"), ("message:3", "email address")],
        )

    def test_pushed_mode_skips_commits_already_on_a_remote(self):
        remote = Path(self._tmp.name) / "remote.git"
        self.git("init", "--quiet", "--bare", str(remote))
        self.git("remote", "add", "origin", str(remote))
        self.use_identity("pat", REAL_ADDRESS)
        self.write("a.md", "a\n")
        self.commit("Published before the rules existed", "a.md")
        self.git("push", "--quiet", "origin", "main")

        self.use_identity(*MAINTAINER)
        self.write("b.md", f"b {REAL_ADDRESS}\n")
        self.commit("New work", "b.md")
        head = self.git("rev-parse", "HEAD")
        tips = checker.pushed_tips(f"refs/heads/main {head} refs/heads/main {'0' * 40}\nrefs/heads/old {'0' * 40} refs/heads/old {head}\n")
        self.assertEqual(tips, [head])

        findings = checker.scan_commits(self.repo, [*tips, "--not", "--remotes"], ALLOWLIST)
        self.assertEqual([finding.kind for finding in findings], ["email address"])

    def test_main_reports_findings_through_the_exit_code(self):
        self.write(checker.ALLOWLIST_FILE, f"identity: {MAINTAINER[0]} <{MAINTAINER[1]}>\n")
        self.write("a.md", "clean\n")
        self.commit("Start", checker.ALLOWLIST_FILE, "a.md")
        self.assertEqual(self.run_script(), 0)

        self.write("a.md", f"clean\n{REAL_ADDRESS}\n")
        self.assertEqual(self.run_script(), 1)

    def run_script(self) -> int:
        return subprocess.run([sys.executable, str(SCRIPT), "--redact"], cwd=self.repo, capture_output=True).returncode


class ThisRepositoryTests(unittest.TestCase):
    @unittest.skipUnless((PROJECT_ROOT / ".git").exists(), "not a Git checkout, so there is no list of tracked files")
    def test_tracked_files_have_no_personal_details(self):
        allowlist = checker.Allowlist.load(PROJECT_ROOT / checker.ALLOWLIST_FILE)
        findings = checker.scan_tracked_files(PROJECT_ROOT, allowlist)
        self.assertEqual([finding.describe(redact=False) for finding in findings], [], checker.RULES)


if __name__ == "__main__":
    unittest.main()
