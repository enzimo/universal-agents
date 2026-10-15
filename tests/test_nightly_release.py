from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlsplit

import pytest

from scripts.github_api import GitHubError
from scripts.nightly_release import main, run


REPOSITORY = "enzimo/universal-agents"
NOW = datetime(2026, 10, 12, 3, 17, 42, tzinfo=timezone.utc)


def _release(release_id: int, tag: str, sha: str, *, prerelease: bool = True, draft: bool = False) -> dict[str, Any]:
    return {"id": release_id, "tag_name": tag, "prerelease": prerelease, "draft": draft, "sha": sha}


class FakeUaRepository:
    """In-memory releases, tags and workflow dispatches of the UA repository."""

    def __init__(self, releases: list[dict[str, Any]], page_size: int = 100) -> None:
        self.releases = list(releases)
        self.tags = {release["tag_name"]: release["sha"] for release in releases}
        self.dispatches: list[dict[str, Any]] = []
        self.calls: list[tuple[str, str]] = []
        self.page_size = page_size
        self.missing_tag_refs: set[str] = set()
        self.fail_once: tuple[str, str] | None = None

    def request(self, method: str, path: str, body: dict[str, Any] | None = None) -> Any:
        self.calls.append((method, path))
        if self.fail_once == (method, path):
            self.fail_once = None
            raise GitHubError("Temporary service failure (HTTP 503)", 503)
        url = urlsplit(path)
        query = {key: values[0] for key, values in parse_qs(url.query).items()}
        prefix = f"repos/{REPOSITORY}/"
        assert url.path.startswith(prefix), path
        route = url.path.removeprefix(prefix)

        if method == "GET" and route == "releases":
            assert int(query["per_page"]) == 100
            page = int(query["page"])
            # Serve real pages of self.page_size, scaled so the script's 100-per-page logic pages.
            start = (page - 1) * self.page_size
            chunk = self.releases[start : start + self.page_size]
            return chunk + ([{"tag_name": "padding"}] * (100 - self.page_size) if len(chunk) == self.page_size else [])
        if method == "GET" and route.startswith("commits/"):
            return {"sha": self.tags[route.removeprefix("commits/")]}
        if method == "GET" and route.startswith("git/ref/tags/"):
            tag = route.removeprefix("git/ref/tags/")
            if tag not in self.tags:
                raise GitHubError("Reference not found (HTTP 404)", 404)
            return {"object": {"sha": self.tags[tag]}}
        if method == "POST" and route == "releases":
            assert body["tag_name"] not in self.tags
            self.tags[body["tag_name"]] = body["target_commitish"]
            release = {**body, "id": 1000 + len(self.releases), "draft": False, "sha": body["target_commitish"]}
            self.releases.insert(0, release)
            return release
        if method == "POST" and route == "actions/workflows/publish-container.yml/dispatches":
            self.dispatches.append(body)
            return None
        if method == "DELETE" and route.startswith("releases/"):
            release_id = int(route.removeprefix("releases/"))
            self.releases = [release for release in self.releases if release["id"] != release_id]
            return None
        if method == "DELETE" and route.startswith("git/refs/tags/"):
            tag = route.removeprefix("git/refs/tags/")
            if tag in self.missing_tag_refs or tag not in self.tags:
                raise GitHubError("DELETE failed: Reference does not exist (HTTP 422)", 422)
            del self.tags[tag]
            return None
        raise AssertionError(f"unexpected call {method} {path}")

    def tag_names(self) -> set[str]:
        return {release["tag_name"] for release in self.releases if release["tag_name"] != "padding"}


@pytest.fixture
def version_file(tmp_path: Path) -> Path:
    path = tmp_path / "VERSION"
    path.write_text("0.9.1\n", encoding="utf-8")
    return path


def _nightly(day: int, sha: str, release_id: int | None = None) -> dict[str, Any]:
    return _release(release_id or day, f"v0.9.2-nightly.202610{day:02d}0317", sha)


def test_creates_tagged_prerelease_and_dispatches_the_container_build(version_file: Path) -> None:
    repository = FakeUaRepository([_release(1, "v0.9.1", "s-091", prerelease=False), _nightly(11, "old")])

    tag = run(repository, repository=REPOSITORY, target_sha="new", version_path=version_file, now=NOW)

    assert tag == "v0.9.2-nightly.202610120317"
    created = repository.releases[0]
    assert created["tag_name"] == tag
    assert created["target_commitish"] == "new"
    assert created["prerelease"] is True
    assert created["make_latest"] == "false"
    assert created["name"] == "Universal Agents 0.9.2 nightly (2026-10-12 03:17 UTC)"
    assert created["body"] == (
        "Nightly build of main at new. Not checked for desktop launcher compatibility; for testing only."
    )
    assert repository.dispatches == [{"ref": "main", "inputs": {"release_tag": tag}}]


def test_skips_when_main_is_already_the_newest_nightly(version_file: Path) -> None:
    repository = FakeUaRepository([_nightly(10, "older"), _nightly(11, "same")])
    notices: list[str] = []

    tag = run(
        repository,
        repository=REPOSITORY,
        target_sha="same",
        version_path=version_file,
        now=NOW,
        notice=notices.append,
    )

    assert tag is None
    assert repository.dispatches == []
    assert not any(method == "POST" for method, _ in repository.calls)
    assert notices == [
        "main is still at same, already built as nightly v0.9.2-nightly.202610110317; no new nightly was created."
    ]


def test_newest_nightly_is_chosen_by_build_minute_not_list_order(version_file: Path) -> None:
    # Only the newest nightly decides; an older one at the same commit does not skip.
    repository = FakeUaRepository([_nightly(9, "same"), _nightly(11, "other"), _nightly(10, "x")])

    assert run(repository, repository=REPOSITORY, target_sha="same", version_path=version_file, now=NOW)


def test_keeps_seven_nightlies_and_never_touches_other_releases(version_file: Path) -> None:
    stable = _release(500, "v0.9.1", "s-091", prerelease=False)
    beta = _release(501, "v0.9.2-beta.1", "s-beta")
    rc = _release(502, "v0.9.2-rc.1", "s-rc")
    stable_named_like_nightly = _release(503, "v0.9.2-nightly.202609300317", "s-odd", prerelease=False)
    nightlies = [_nightly(day, f"s-{day}") for day in range(1, 12)]
    repository = FakeUaRepository([stable, beta, rc, stable_named_like_nightly, *nightlies])

    tag = run(repository, repository=REPOSITORY, target_sha="new", version_path=version_file, now=NOW)

    remaining = repository.tag_names()
    kept_nightlies = sorted(name for name in remaining if "-nightly." in name and name != stable_named_like_nightly["tag_name"])
    assert kept_nightlies == sorted([tag] + [f"v0.9.2-nightly.202610{day:02d}0317" for day in range(6, 12)])
    assert {"v0.9.1", "v0.9.2-beta.1", "v0.9.2-rc.1", "v0.9.2-nightly.202609300317"} <= remaining
    for day in range(1, 6):
        assert f"v0.9.2-nightly.202610{day:02d}0317" not in repository.tags
    assert "v0.9.2-nightly.202609300317" in repository.tags  # not a pre-release, so untouched


def test_skipped_run_still_prunes_to_seven(version_file: Path) -> None:
    nightlies = [_nightly(day, f"s-{day}") for day in range(1, 10)]
    repository = FakeUaRepository(nightlies)

    assert run(repository, repository=REPOSITORY, target_sha="s-9", version_path=version_file, now=NOW, notice=lambda _: None) is None

    assert sorted(repository.tag_names()) == [f"v0.9.2-nightly.202610{day:02d}0317" for day in range(3, 10)]


def test_missing_tag_during_cleanup_is_reported_not_fatal(version_file: Path) -> None:
    nightlies = [_nightly(day, f"s-{day}") for day in range(1, 9)]
    repository = FakeUaRepository(nightlies)
    repository.missing_tag_refs.add("v0.9.2-nightly.202610010317")
    del repository.tags["v0.9.2-nightly.202610010317"]
    messages: list[str] = []

    run(repository, repository=REPOSITORY, target_sha="s-8", version_path=version_file, now=NOW, out=messages.append, notice=lambda _: None)

    assert "Tag v0.9.2-nightly.202610010317 was already gone while pruning its release." in messages
    assert "v0.9.2-nightly.202610010317" not in repository.tag_names()


@pytest.mark.parametrize("failed_operation", ["tag", "release"])
def test_interrupted_cleanup_retries_without_orphaning_tags(
    version_file: Path, failed_operation: str
) -> None:
    repository = FakeUaRepository([_nightly(day, f"s-{day}") for day in range(1, 9)])
    old_tag = "v0.9.2-nightly.202610010317"
    route = f"git/refs/tags/{old_tag}" if failed_operation == "tag" else "releases/1"
    repository.fail_once = ("DELETE", f"repos/{REPOSITORY}/{route}")
    arguments = dict(repository=REPOSITORY, target_sha="s-8", version_path=version_file, now=NOW)

    with pytest.raises(GitHubError, match="503"):
        run(repository, **arguments)

    # Keep the release as the durable cleanup record until both deletions succeed.
    assert old_tag in repository.tag_names()
    assert (old_tag in repository.tags) == (failed_operation == "tag")
    run(repository, **arguments)
    assert len(repository.tag_names()) == len(repository.tags) == 7
    assert old_tag not in repository.tags
    assert old_tag not in repository.tag_names()


def test_invalid_tag_deletion_is_not_mistaken_for_an_absent_tag(version_file: Path) -> None:
    repository = FakeUaRepository([_nightly(day, f"s-{day}") for day in range(1, 9)])
    old_tag = "v0.9.2-nightly.202610010317"
    # HTTP 422 can also mean validation refused deletion while the tag still exists.
    repository.missing_tag_refs.add(old_tag)
    with pytest.raises(GitHubError, match="422"):
        run(repository, repository=REPOSITORY, target_sha="s-8", version_path=version_file, now=NOW)
    assert old_tag in repository.tag_names()
    assert old_tag in repository.tags


def test_reads_every_release_page(version_file: Path) -> None:
    nightlies = [_nightly(day, f"s-{day}") for day in range(1, 10)]
    repository = FakeUaRepository(nightlies, page_size=4)

    run(repository, repository=REPOSITORY, target_sha="s-9", version_path=version_file, now=NOW, notice=lambda _: None)

    pages = [path for method, path in repository.calls if method == "GET" and "/releases?" in path]
    assert len(pages) == 3
    assert len(repository.tag_names()) == 7


def test_first_nightly_has_nothing_to_compare(version_file: Path) -> None:
    repository = FakeUaRepository([_release(1, "v0.9.1", "s-091", prerelease=False)])

    assert run(repository, repository=REPOSITORY, target_sha="s-091", version_path=version_file, now=NOW)
    assert len(repository.dispatches) == 1


def test_main_reports_failures(version_file: Path, capsys: pytest.CaptureFixture[str]) -> None:
    class FailingApi:
        def request(self, method: str, path: str, body: dict[str, Any] | None = None) -> Any:
            raise GitHubError(f"{method} {path} failed: gh: Forbidden (HTTP 403)", 403)

    code = main(
        ["nightly", "--repository", REPOSITORY, "--target-sha", "abc", "--version-file", str(version_file)],
        api=FailingApi(),
    )

    assert code == 1
    assert "Nightly release failed" in capsys.readouterr().err


def test_keep_must_be_positive(version_file: Path) -> None:
    with pytest.raises(ValueError):
        run(FakeUaRepository([]), repository=REPOSITORY, target_sha="a", version_path=version_file, now=NOW, keep=0)
