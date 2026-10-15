from __future__ import annotations

import base64
import hashlib
import json
from pathlib import Path
import subprocess
from typing import Any
from urllib.parse import parse_qs, unquote, urlsplit

import pytest

from scripts.github_api import GhCliApi, GitHubError
from scripts.propose_desktop_compatibility import (
    LAUNCHER_REPOSITORY,
    LIST_PATH,
    ProposalError,
    ProposalRequest,
    add_runtime,
    build_request,
    main,
    propose,
)


IMAGE_REPOSITORY = "ghcr.io/enzimo/universal-agents"
DIGEST_085 = "sha256:d5d41b6946ddfbada847036405500f773d949b0cafdc02ea9268bc86941c0197"
DIGEST_090 = "sha256:f8d5d761cab9b226943830645f8e0ed5608a003c096499f03c1b983241d591f0"
DIGEST_092 = "sha256:" + "a" * 64
IMAGE_092 = f"{IMAGE_REPOSITORY}@{DIGEST_092}"

# The launcher repository's list as written there: 2-space JSON, trailing newline.
LAUNCHER_LIST = f"""{{
  "schema_version": 1,
  "runtimes": [
    {{
      "ua_version": "0.8.5",
      "host_contract_version": 3,
      "image": "{IMAGE_REPOSITORY}@{DIGEST_085}"
    }},
    {{
      "ua_version": "0.9.0",
      "host_contract_version": 3,
      "image": "{IMAGE_REPOSITORY}@{DIGEST_090}"
    }}
  ]
}}
"""

CONTAINER_IMAGE_TXT = (
    "Release: v0.9.2\n"
    "Source commit: 0123456789abcdef0123456789abcdef01234567\n"
    f"Image: {IMAGE_REPOSITORY}:0.9.2\n"
    f"Immutable image: {IMAGE_092}\n"
    "Platforms: linux/amd64, linux/arm64\n"
    f"Pull: docker pull {IMAGE_092}\n"
)


def _blob_sha(text: str) -> str:
    return hashlib.sha1(text.encode("utf-8")).hexdigest()


class FakeLauncherRepository:
    """In-memory launcher repository answering the REST calls the script makes."""

    def __init__(self, list_text: str | None = LAUNCHER_LIST) -> None:
        self.commits: dict[str, str | None] = {"c-main": list_text}
        self.branches: dict[str, str] = {"main": "c-main"}
        self.pulls: list[dict[str, Any]] = []
        self.calls: list[tuple[str, str]] = []
        self._next_commit = 0

    # -- helpers for tests -------------------------------------------------
    def file_on(self, branch: str) -> str | None:
        return self.commits[self.branches[branch]]

    def writes(self) -> list[tuple[str, str]]:
        return [call for call in self.calls if call[0] != "GET"]

    def add_branch(self, name: str, text: str | None) -> None:
        commit = self._commit(text)
        self.branches[name] = commit

    def _commit(self, text: str | None) -> str:
        self._next_commit += 1
        commit = f"c-{self._next_commit}"
        self.commits[commit] = text
        return commit

    def _not_found(self, path: str) -> GitHubError:
        return GitHubError(f"GET {path} failed: gh: Not Found (HTTP 404)", 404)

    # -- GitHubApi ---------------------------------------------------------
    def request(self, method: str, path: str, body: dict[str, Any] | None = None) -> Any:
        self.calls.append((method, path))
        url = urlsplit(path)
        query = {key: values[0] for key, values in parse_qs(url.query).items()}
        prefix = f"repos/{LAUNCHER_REPOSITORY}/"
        assert url.path.startswith(prefix), path
        route = url.path.removeprefix(prefix)

        if method == "GET" and route.startswith("git/ref/heads/"):
            branch = route.removeprefix("git/ref/heads/")
            if branch not in self.branches:
                raise self._not_found(path)
            return {"object": {"sha": self.branches[branch]}}
        if method == "GET" and route == f"contents/{LIST_PATH}":
            ref = unquote(query["ref"])
            commit = self.branches.get(ref, ref)
            text = self.commits.get(commit)
            if text is None:
                raise self._not_found(path)
            return {
                "sha": _blob_sha(text),
                "content": base64.b64encode(text.encode("utf-8")).decode("ascii"),
            }
        if method == "GET" and route == "pulls":
            owner, branch = query["head"].split(":", 1)
            assert owner == LAUNCHER_REPOSITORY.split("/")[0]
            return [
                pull
                for pull in self.pulls
                if pull["head"] == branch and pull["state"] == query["state"]
            ]
        if method == "POST" and route == "git/refs":
            branch = body["ref"].removeprefix("refs/heads/")
            assert branch not in self.branches
            self.branches[branch] = body["sha"]
            return {}
        if method == "PATCH" and route.startswith("git/refs/heads/"):
            branch = route.removeprefix("git/refs/heads/")
            assert body["force"] is True
            self.branches[branch] = body["sha"]
            return {}
        if method == "PUT" and route == f"contents/{LIST_PATH}":
            current = self.file_on(body["branch"])
            if current is None or _blob_sha(current) != body["sha"]:
                raise GitHubError("PUT failed: sha does not match (HTTP 409)", 409)
            text = base64.b64decode(body["content"]).decode("utf-8")
            self.branches[body["branch"]] = self._commit(text)
            self.last_commit_message = body["message"]
            return {}
        if method == "POST" and route == "pulls":
            pull = {
                "head": body["head"],
                "base": body["base"],
                "title": body["title"],
                "body": body["body"],
                "state": "open",
                "html_url": f"https://github.com/{LAUNCHER_REPOSITORY}/pull/{len(self.pulls) + 1}",
            }
            self.pulls.append(pull)
            return pull
        raise AssertionError(f"unexpected call {method} {path}")


def _request(text: str | None = CONTAINER_IMAGE_TXT) -> ProposalRequest:
    return build_request(
        "v0.9.2",
        digest=DIGEST_092,
        image_repository=IMAGE_REPOSITORY,
        container_image_text=text,
    )


def test_appends_the_release_in_order_with_the_newest_contract() -> None:
    repository = FakeLauncherRepository()
    messages: list[str] = []

    propose(repository, _request(), out=messages.append)

    new_list = repository.file_on("ua-release/v0.9.2")
    expected = json.loads(LAUNCHER_LIST)
    expected["runtimes"].append(
        {"ua_version": "0.9.2", "host_contract_version": 3, "image": IMAGE_092}
    )
    assert new_list == json.dumps(expected, indent=2) + "\n"
    assert new_list.startswith(LAUNCHER_LIST.removesuffix("\n  ]\n}\n"))
    assert repository.file_on("main") == LAUNCHER_LIST
    assert repository.last_commit_message == "Add UA 0.9.2 to the compatibility list"
    [pull] = repository.pulls
    assert pull["title"] == "Add UA 0.9.2 to the compatibility list"
    assert pull["head"] == "ua-release/v0.9.2"
    assert pull["base"] == "main"
    assert "https://github.com/enzimo/universal-agents/releases/tag/v0.9.2" in pull["body"]
    assert CONTAINER_IMAGE_TXT.rstrip() in pull["body"]
    assert "merges this pull request automatically" in pull["body"]
    assert any("Opened pull request" in message for message in messages)


def test_contract_comes_from_the_newest_entry() -> None:
    document = json.loads(LAUNCHER_LIST)
    document["runtimes"][0]["host_contract_version"] = 2
    document["runtimes"][1]["host_contract_version"] = 4
    text = json.dumps(document, indent=2) + "\n"

    update = add_runtime(text, "0.9.2", IMAGE_092)

    assert update.host_contract_version == 4
    assert update.newest_listed_version == "0.9.0"
    assert json.loads(update.text)["runtimes"][-1]["host_contract_version"] == 4


def test_rerun_with_the_same_image_changes_nothing() -> None:
    document = json.loads(LAUNCHER_LIST)
    document["runtimes"].append(
        {"ua_version": "0.9.2", "host_contract_version": 3, "image": IMAGE_092}
    )
    repository = FakeLauncherRepository(json.dumps(document, indent=2) + "\n")
    messages: list[str] = []

    propose(repository, _request(), out=messages.append)

    assert repository.writes() == []
    assert messages == [
        f"UA 0.9.2 is already on the launcher's compatibility list with image {IMAGE_092}; nothing to do."
    ]


def test_a_listed_version_with_a_different_image_fails_loudly() -> None:
    other = f"{IMAGE_REPOSITORY}@sha256:{'b' * 64}"
    document = json.loads(LAUNCHER_LIST)
    document["runtimes"].append(
        {"ua_version": "0.9.2", "host_contract_version": 3, "image": other}
    )
    repository = FakeLauncherRepository(json.dumps(document, indent=2) + "\n")

    with pytest.raises(ProposalError, match="already listed with image"):
        propose(repository, _request())
    assert repository.writes() == []


def test_an_image_listed_for_another_version_is_refused() -> None:
    with pytest.raises(ProposalError, match="already listed for UA 0.9.0"):
        add_runtime(LAUNCHER_LIST, "0.9.2", f"{IMAGE_REPOSITORY}@{DIGEST_090}")


def test_an_older_unlisted_release_is_not_inserted(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    # Repairing an old release's image must not fail the run or rewrite the list.
    repository = FakeLauncherRepository()
    digest = "sha256:" + "c" * 64

    monkeypatch.delenv("GITHUB_ACTIONS", raising=False)
    assert main(["propose", "v0.8.4", "--digest", digest], api=repository) == 0
    assert "older than the newest listed version 0.9.0" in capsys.readouterr().err

    monkeypatch.setenv("GITHUB_ACTIONS", "true")
    assert main(["propose", "v0.8.4", "--digest", digest], api=repository) == 0
    assert capsys.readouterr().out.startswith(
        "::warning title=Launcher compatibility entry not proposed::UA 0.8.4 is older"
    )
    assert repository.writes() == []


def test_dry_run_prints_the_change_and_writes_nothing(capsys: pytest.CaptureFixture[str]) -> None:
    repository = FakeLauncherRepository()

    code = main(
        ["propose", "v0.9.2", "--digest", DIGEST_092, "--dry-run"],
        api=repository,
    )

    out = capsys.readouterr().out
    assert code == 0
    assert repository.writes() == []
    assert "Dry run: nothing was written" in out
    assert "Branch: ua-release/v0.9.2 (would be created from main)" in out
    assert "Commit message and pull request title: Add UA 0.9.2 to the compatibility list" in out
    assert add_runtime(LAUNCHER_LIST, "0.9.2", IMAGE_092).text in out


def test_missing_list_on_main_is_reported_clearly(capsys: pytest.CaptureFixture[str]) -> None:
    repository = FakeLauncherRepository(list_text=None)

    code = main(["propose", "v0.9.2", "--digest", DIGEST_092, "--dry-run"], api=repository)

    assert code == 1
    assert "main branch has no compatible-runtimes.json yet" in capsys.readouterr().err
    assert repository.writes() == []


def test_existing_branch_and_open_pull_request_are_reused() -> None:
    repository = FakeLauncherRepository()
    propose(repository, _request())
    first_branch_commit = repository.branches["ua-release/v0.9.2"]
    messages: list[str] = []

    propose(repository, _request(), out=messages.append)

    assert repository.branches["ua-release/v0.9.2"] == first_branch_commit
    assert len(repository.pulls) == 1
    assert "Branch ua-release/v0.9.2 already has this change; reusing it." in messages
    assert f"Reusing open pull request {repository.pulls[0]['html_url']}" in messages


def test_stale_existing_branch_is_restarted_from_main_and_gets_a_pull_request() -> None:
    repository = FakeLauncherRepository()
    repository.add_branch("ua-release/v0.9.2", "{}\n")
    messages: list[str] = []

    propose(repository, _request(), out=messages.append)

    assert repository.file_on("ua-release/v0.9.2") == add_runtime(LAUNCHER_LIST, "0.9.2", IMAGE_092).text
    assert ("PATCH", f"repos/{LAUNCHER_REPOSITORY}/git/refs/heads/ua-release/v0.9.2") in repository.calls
    assert ("POST", f"repos/{LAUNCHER_REPOSITORY}/git/refs") not in repository.calls
    assert len(repository.pulls) == 1


def test_container_image_file_must_name_the_same_image() -> None:
    other = CONTAINER_IMAGE_TXT.replace(DIGEST_092, "sha256:" + "d" * 64)

    with pytest.raises(ProposalError, match="check which image was published"):
        build_request("v0.9.2", digest=DIGEST_092, image_repository=IMAGE_REPOSITORY, container_image_text=other)

    request = build_request(
        "v0.9.2", digest=None, image_repository=IMAGE_REPOSITORY, container_image_text=CONTAINER_IMAGE_TXT
    )
    assert request.image == IMAGE_092


@pytest.mark.parametrize("tag", ["v0.9.2-beta.1", "v0.9.2-nightly.202610120317", "0.9.2"])
def test_only_stable_tags_are_proposed(tag: str) -> None:
    with pytest.raises((ProposalError, ValueError)):
        build_request(tag, digest=DIGEST_092, image_repository=IMAGE_REPOSITORY, container_image_text=None)


def test_digest_must_be_a_full_sha256() -> None:
    with pytest.raises(ProposalError, match="not sha256"):
        build_request("v0.9.2", digest="sha256:abc", image_repository=IMAGE_REPOSITORY, container_image_text=None)


def test_main_reads_the_container_image_file(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    reference = tmp_path / "container-image.txt"
    reference.write_text(CONTAINER_IMAGE_TXT, encoding="utf-8")
    repository = FakeLauncherRepository()

    code = main(
        ["propose", "v0.9.2", "--digest", DIGEST_092, "--container-image-file", str(reference)],
        api=repository,
    )

    assert code == 0
    assert CONTAINER_IMAGE_TXT.rstrip() in repository.pulls[0]["body"]


def test_gh_cli_api_reports_status_and_never_receives_the_token_as_an_argument(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("GH_TOKEN", "secret-token-value")
    seen: list[list[str]] = []

    def runner(command: list[str], **kwargs: Any) -> subprocess.CompletedProcess:
        seen.append(command)
        if command[-1] == "-":
            assert json.loads(kwargs["input"]) == {"x": 1}
            return subprocess.CompletedProcess(command, 0, stdout='{"ok": true}', stderr="")
        return subprocess.CompletedProcess(command, 1, stdout="", stderr="gh: Not Found (HTTP 404)")

    api = GhCliApi(runner=runner)

    assert api.request("POST", "repos/o/r/thing", {"x": 1}) == {"ok": True}
    with pytest.raises(GitHubError) as missing:
        api.request("GET", "repos/o/r/missing")
    assert missing.value.status == 404
    assert seen[0] == ["gh", "api", "--method", "POST", "repos/o/r/thing", "--input", "-"]
    assert all("secret-token-value" not in " ".join(command) for command in seen)
    assert "secret-token-value" not in str(missing.value)


@pytest.mark.parametrize(
    ("mutate", "message"),
    [
        (lambda doc: doc.update(schema_version=2), "uses schema 2"),
        (lambda doc: doc.update(runtimes=[]), "lists no runtimes"),
        (lambda doc: doc["runtimes"].reverse(), "not in ascending version order"),
        (lambda doc: doc["runtimes"][0].pop("image"), "has an entry without exactly"),
    ],
)
def test_a_list_breaking_its_rules_is_never_extended(mutate: Any, message: str) -> None:
    document = json.loads(LAUNCHER_LIST)
    mutate(document)

    with pytest.raises(ProposalError, match=message):
        add_runtime(json.dumps(document, indent=2) + "\n", "0.9.2", IMAGE_092)
    with pytest.raises(ProposalError, match="not valid JSON"):
        add_runtime("{", "0.9.2", IMAGE_092)
