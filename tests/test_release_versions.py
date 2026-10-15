from __future__ import annotations

from datetime import datetime, timedelta, timezone
import io

import pytest

from scripts.release_versions import (
    main,
    next_versions,
    nightly_tag,
    previous_stable_tag,
    semver_key,
)


def test_semver_order_puts_prereleases_between_releases() -> None:
    ordered = [
        "0.9.1",
        "0.9.2-beta.2",
        "0.9.2-beta.10",
        "0.9.2-nightly.202610120317",
        "0.9.2-nightly.202610130317",
        "0.9.2-rc.1",
        "0.9.2",
        "0.10.0-beta.1",
        "0.10.0",
    ]
    assert sorted(reversed(ordered), key=semver_key) == ordered
    with pytest.raises(ValueError):
        semver_key("0.9")


def test_next_versions_are_patch_minor_and_major() -> None:
    assert next_versions("0.9.1") == ("0.9.2", "0.10.0", "1.0.0")
    with pytest.raises(ValueError):
        next_versions("0.9.1-beta.1")


def test_nightly_tag_uses_next_patch_and_utc_minute() -> None:
    eastern = timezone(timedelta(hours=-4))
    built = datetime(2026, 10, 11, 23, 17, 59, tzinfo=eastern)

    assert nightly_tag("0.9.1", built) == "v0.9.2-nightly.202610120317"
    with pytest.raises(ValueError):
        nightly_tag("0.9.1", datetime(2026, 10, 12, 3, 17))


def test_previous_stable_tag_ignores_prereleases_and_newer_releases() -> None:
    tags = ["v0.9.0", "v0.9.2-beta.1", "v0.9.1", "v0.10.0", "not-a-tag", "v0.8.5"]

    assert previous_stable_tag("0.9.2", tags) == "v0.9.1"
    assert previous_stable_tag("0.9.2-rc.1", tags) == "v0.9.1"
    assert previous_stable_tag("0.8.5", tags) is None


def test_previous_stable_cli_reads_tags_from_stdin(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setattr("sys.stdin", io.StringIO("v0.9.0\nv0.9.1\nv0.9.2\n"))
    assert main(["release_versions.py", "previous-stable", "0.9.2"]) == 0
    assert capsys.readouterr().out == "v0.9.1\n"

    monkeypatch.setattr("sys.stdin", io.StringIO(""))
    assert main(["release_versions.py", "previous-stable", "0.9.2"]) == 0
    assert capsys.readouterr().out == ""

    assert main(["release_versions.py"]) == 2
