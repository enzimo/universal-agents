"""Install the pinned compiler cache from a checksum-verified upstream archive."""

import argparse
import hashlib
import io
import platform
import tarfile
import urllib.request
import zipfile
from pathlib import Path

VERSION = "0.17.0"
ARCHIVES = {
    ("Linux", "x86_64"): ("x86_64-unknown-linux-musl.tar.gz", "67c4a96dd237c1f518f6b36083f270f9976d516f1e57fce891755ea782e50006"),
    ("Darwin", "arm64"): ("aarch64-apple-darwin.tar.gz", "0c560bfba31aef5bdfb4fb3d2677f6e61d71c5c00952f2a83344f47aa31f00f1"),
    ("Windows", "amd64"): ("x86_64-pc-windows-msvc.zip", "e94cfc5b58cbe439302f586c1d1bd7980c2cd371d47bdf385ade657411e6f3ac"),
}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("destination", type=Path, help="Dedicated runner tools directory")
    args = parser.parse_args()
    suffix, expected = ARCHIVES[(platform.system(), platform.machine().lower())]
    name = f"sccache-v{VERSION}-{suffix}"
    url = f"https://github.com/mozilla/sccache/releases/download/v{VERSION}/{name}"
    with urllib.request.urlopen(url, timeout=120) as response:
        data = response.read(100 * 1024**2)
    if hashlib.sha256(data).hexdigest() != expected:
        raise ValueError("Upstream compiler cache archive checksum mismatch")
    executable = "sccache.exe" if platform.system() == "Windows" else "sccache"
    member = name.removesuffix(".tar.gz").removesuffix(".zip") + "/" + executable
    if name.endswith(".zip"):
        with zipfile.ZipFile(io.BytesIO(data)) as archive:
            binary = archive.read(member)
    else:
        with tarfile.open(fileobj=io.BytesIO(data), mode="r:gz") as archive:
            binary = archive.extractfile(member).read()
    args.destination.mkdir(parents=True, exist_ok=True)
    target = args.destination / executable
    staging = args.destination / (executable + ".new")
    staging.write_bytes(binary)
    staging.chmod(0o755)
    staging.replace(target)
    print(f"Installed sccache {VERSION}: {target}")


if __name__ == "__main__":
    main()
