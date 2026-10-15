"""Exercise archive verification and architecture handling without root/network."""

import hashlib
import io
import os
import subprocess
import sys
import tarfile
from pathlib import Path

import pytest


@pytest.mark.parametrize(
    "architecture,valid,expected",
    [
        ("amd64", True, 0),
        ("arm64", True, 0),
        ("amd64", False, 1),
        ("s390x", True, 2),
    ],
)
def test_installer_checks_archive_before_install(
    tmp_path, architecture, valid, expected
):
    archive = tmp_path / "release.tar.gz"
    with tarfile.open(archive, "w:gz") as handle:
        for name, content in {
            "xurl": b"#!/bin/sh\necho 'xurl version 1.3.1'\n",
            "LICENSE": b"MIT license fixture\n",
        }.items():
            info = tarfile.TarInfo(name)
            info.size = len(content)
            info.mode = 0o755 if name == "xurl" else 0o644
            handle.addfile(info, io.BytesIO(content))
    binary_dir = tmp_path / "bin"
    binary_dir.mkdir()
    programs = {
        "dpkg": "import os; print(os.environ['TEST_ARCH'])",
        "curl": "import os,shutil,sys; shutil.copyfile(os.environ['TEST_ARCHIVE'],sys.argv[-1])",
        "install": "import os,shutil,sys; from pathlib import Path; target=Path(os.environ['TEST_DEST'])/Path(sys.argv[-1]).name; shutil.copyfile(sys.argv[-2],target); target.chmod(0o755)",
    }
    for name, body in programs.items():
        path = binary_dir / name
        path.write_text(f"#!{sys.executable}\n{body}\n")
        path.chmod(0o755)
    checksum = hashlib.sha256(archive.read_bytes()).hexdigest() if valid else "0" * 64
    result = subprocess.run(
        ["sh", str(Path(__file__).resolve().parents[1] / "docker/install_xurl.sh")],
        env={
            **os.environ,
            "PATH": f"{binary_dir}:{os.environ['PATH']}",
            "TEST_ARCH": architecture,
            "TEST_ARCHIVE": str(archive),
            "TEST_DEST": str(binary_dir),
            "XURL_SHA256_AMD64": checksum,
            "XURL_SHA256_ARM64": checksum,
        },
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == expected, result.stderr
    if expected == 0:
        assert "xurl version 1.3.1" in result.stdout
        assert (binary_dir / "LICENSE").read_text() == "MIT license fixture\n"
    else:
        assert not (binary_dir / "xurl").exists()
