"""Tests for scripts/check_personal_details.py.

Uses only the standard library, so it runs with `python3 -m unittest` in a
project of any language; pytest also collects it.

Sample values are assembled at run time so the scan of this file stays clean.
"""

from __future__ import annotations

import contextlib
import importlib.util
import io
import json
import os
import subprocess
import sys
import tempfile
import unittest
import unittest.mock
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
ALLOWLIST = checker.Allowlist()


class DetectorTests(unittest.TestCase):
    def test_real_looking_details_are_found(self):
        samples = [
            f"write to {REAL_ADDRESS}",
            f"Pat <pat.lee+ci{AT}corp.example.co.uk>",
            f"1234+pat{AT}users.noreply.github.com.mailbox.co",
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
            f"Co-authored-by: pat <1234+pat{AT}users.noreply.github.com>, legacy pat{AT}users.noreply.github.com",
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


class EmailTests(unittest.TestCase):
    def test_anonymized_emails_pass(self):
        allowed = [
            f"noreply{AT}github.com",  # GitHub's own web merges and edits.
            f"49699333+dependabot[bot]{AT}users.noreply.github.com",
            f"41898282+github-actions[bot]{AT}users.noreply.github.com",
            MAINTAINER[1],
            MAINTAINER[1].upper(),
            f"1234+pat{AT}users.noreply.github.com",  # Any contributor's noreply address.
            f"pat{AT}users.noreply.github.com",  # GitHub's older noreply form.
            f"noreply{AT}vendor.com",
        ]
        for email in allowed:
            with self.subTest(email=email):
                self.assertTrue(checker.is_anonymized_email(email))

    def test_other_emails_fail(self):
        refused = [
            REAL_ADDRESS,
            f"1234+pat{AT}users.noreply.github.com.mailbox.co",
            f"noreply.pat{AT}mailbox.co",
            f"pat{AT}github.com",
            "noreply",
            "",
        ]
        for email in refused:
            with self.subTest(email=email):
                self.assertFalse(checker.is_anonymized_email(email))

    def test_malformed_allowlist_lines_are_rejected(self):
        for text in ("owner: someone", "text:", "account:"):
            with self.subTest(text=text), self.assertRaises(ValueError):
                checker.Allowlist.parse(text)

    def test_old_identity_lines_are_rejected_with_a_fix(self):
        with self.assertRaisesRegex(ValueError, "no longer used.*Remove this line"):
            checker.Allowlist.parse(f"identity: {MAINTAINER[0]} <{MAINTAINER[1]}>\n")


class EventScopeTests(unittest.TestCase):
    TIP = "a" * 40

    def test_pull_request_checks_commits_after_its_base(self):
        scope = checker.event_scope("pull_request", {"pull_request": {"base": {"sha": self.TIP}}})
        self.assertEqual(scope.since, self.TIP)

    def test_push_checks_commits_after_the_previous_tip(self):
        scope = checker.event_scope("push", {"before": self.TIP, "ref": "refs/heads/main"})
        self.assertEqual(scope.since, self.TIP)

    def test_new_branch_checks_commits_not_on_the_default_branch(self):
        event = {"before": "0" * 40, "ref": "refs/heads/topic", "repository": {"default_branch": "main"}}
        self.assertEqual(checker.event_scope("push", event).since, "origin/main")

    def test_first_push_of_the_default_branch_checks_everything(self):
        event = {"before": "0" * 40, "ref": "refs/heads/main", "repository": {"default_branch": "main"}}
        scope = checker.event_scope("push", event)
        self.assertIsNone(scope.since)
        self.assertFalse(scope.nothing)

    def test_deletion_adds_no_commits(self):
        self.assertTrue(checker.event_scope("push", {"deleted": True, "before": self.TIP}).nothing)

    def test_manual_and_scheduled_runs_check_everything(self):
        for name in ("workflow_dispatch", "schedule", ""):
            with self.subTest(name=name):
                self.assertIsNone(checker.event_scope(name, {}).since)


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
        self.assertEqual(checker.scan_next_commit_email(self.repo), [])

        self.use_identity("Contributor", MAINTAINER[1])  # Names are not checked.
        self.assertEqual(checker.scan_next_commit_email(self.repo), [])

        self.use_identity("pat", REAL_ADDRESS)
        findings = checker.scan_next_commit_email(self.repo)
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

    def test_history_checks_emails_and_messages(self):
        self.use_identity("pat", REAL_ADDRESS)
        self.write("a.md", "a\n")
        self.commit(f"Start\n\nThanks {REAL_ADDRESS}", "a.md")

        kinds = sorted((finding.where.split(" ", 2)[2], finding.kind) for finding in checker.scan_commits(self.repo, ["HEAD"], ALLOWLIST))
        self.assertEqual(
            kinds,
            [("author", checker.COMMIT_EMAIL), ("committer", checker.COMMIT_EMAIL), ("message:3", "email address")],
        )

    def test_history_accepts_any_name_with_an_anonymized_email(self):
        self.use_identity("Pat Lee", f"1234+pat{AT}users.noreply.github.com")
        self.write("a.md", "a\n")
        self.commit("Start", "a.md")
        self.assertEqual(checker.scan_commits(self.repo, ["HEAD"], ALLOWLIST), [])

    def make_history_with_an_old_detail(self) -> str:
        """Commit a detail, then a clean commit; return the first commit's SHA."""
        self.write("a.md", f"a {REAL_ADDRESS}\n")
        self.commit("Published before the rules existed", "a.md")
        old = self.git("rev-parse", "HEAD")
        self.write("a.md", "a\n")
        self.commit("Remove the detail", "a.md")
        return old

    def scan_event(self, event_name: str, event: dict) -> tuple[list, str, str]:
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err), unittest.mock.patch.dict(os.environ, {"GITHUB_ACTIONS": ""}):
            findings = checker.scan_github_event(self.repo, event_name, event, ALLOWLIST)
        return findings, out.getvalue(), err.getvalue()

    def test_github_event_checks_only_new_commits(self):
        old = self.make_history_with_an_old_detail()
        for event_name, event in (
            ("push", {"before": old, "ref": "refs/heads/main"}),
            ("pull_request", {"pull_request": {"base": {"sha": old}}}),
        ):
            with self.subTest(event_name=event_name):
                findings, out, err = self.scan_event(event_name, event)
                self.assertEqual(findings, [])
                self.assertIn(old, out)
                self.assertEqual(err, "")

        self.write("b.md", f"b {REAL_ADDRESS}\n")
        self.commit("New detail", "b.md")
        findings, _, _ = self.scan_event("push", {"before": old, "ref": "refs/heads/main"})
        self.assertEqual(len(findings), 2)  # The new file, and the commit that added it.

    def test_github_event_with_a_missing_base_warns_and_checks_everything(self):
        self.make_history_with_an_old_detail()
        findings, _, err = self.scan_event("push", {"before": "b" * 40, "ref": "refs/heads/main"})
        self.assertEqual([finding.kind for finding in findings], ["email address"])
        self.assertIn("Warning:", err)
        self.assertIn("b" * 40, err)

    def test_manual_github_event_checks_everything(self):
        self.make_history_with_an_old_detail()
        findings, out, err = self.scan_event("workflow_dispatch", {})
        self.assertEqual([finding.kind for finding in findings], ["email address"])
        self.assertIn("every commit", out)
        self.assertEqual(err, "")

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
        self.write(checker.ALLOWLIST_FILE, "account: jane\n")
        self.write("a.md", "clean /home/" + "jane\n")
        self.commit("Start", checker.ALLOWLIST_FILE, "a.md")
        self.assertEqual(self.run_script().returncode, 0)

        self.write("a.md", f"clean\n{REAL_ADDRESS}\n")
        self.assertEqual(self.run_script().returncode, 1)

    def test_main_explains_an_old_identity_line(self):
        self.write(checker.ALLOWLIST_FILE, f"identity: {MAINTAINER[0]} <{MAINTAINER[1]}>\n")
        self.commit("Start", checker.ALLOWLIST_FILE)
        result = self.run_script()
        self.assertEqual(result.returncode, 1)
        self.assertIn("Remove this line", result.stderr)

    def test_main_github_event_mode_reads_the_event_file(self):
        old = self.make_history_with_an_old_detail()
        event_file = Path(self._tmp.name) / "event.json"
        event_file.write_text(json.dumps({"before": old, "ref": "refs/heads/main"}), encoding="utf-8")
        environment = {"GITHUB_EVENT_NAME": "push", "GITHUB_EVENT_PATH": str(event_file)}
        self.assertEqual(self.run_script("--github-event", environment=environment).returncode, 0)
        environment["GITHUB_EVENT_NAME"] = "workflow_dispatch"
        self.assertEqual(self.run_script("--github-event", environment=environment).returncode, 1)

    def run_script(self, *args: str, environment: dict | None = None) -> subprocess.CompletedProcess:
        return subprocess.run(
            [sys.executable, str(SCRIPT), "--redact", *args],
            cwd=self.repo,
            capture_output=True,
            text=True,
            env={**os.environ, "GITHUB_ACTIONS": "", **(environment or {})},
        )


class ThisRepositoryTests(unittest.TestCase):
    @unittest.skipUnless((PROJECT_ROOT / ".git").exists(), "not a Git checkout, so there is no list of tracked files")
    def test_tracked_files_have_no_personal_details(self):
        allowlist = checker.Allowlist.load(PROJECT_ROOT / checker.ALLOWLIST_FILE)
        findings = checker.scan_tracked_files(PROJECT_ROOT, allowlist)
        self.assertEqual([finding.describe(redact=False) for finding in findings], [], checker.RULES)


if __name__ == "__main__":
    unittest.main()
