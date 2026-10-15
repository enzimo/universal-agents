"""Make a job-owned setup-python installation usable with a restricted env."""
import os
from pathlib import Path
import subprocess
import sys


def normalize(prefix: Path, tool_cache: Path) -> None:
    prefix, tool_cache = prefix.resolve(), tool_cache.resolve()
    if prefix == tool_cache or not prefix.is_relative_to(tool_cache):
        raise ValueError("Python must be inside this runner's disposable tool cache")
    binaries = list((prefix / "bin").glob("python3.*"))
    binaries = [p for p in binaries if p.name.removeprefix("python3.").isdigit()]
    if len(binaries) != 1:
        raise ValueError("Expected one versioned Python executable")
    binary = binaries[0].resolve()
    if not binary.is_relative_to(prefix) or binary.samefile(sys.executable):
        raise ValueError("Refusing to modify an external or running Python")
    subprocess.run(["rtk", "proxy", "patchelf", "--set-rpath", "$ORIGIN/../lib", str(binary)], check=True)
    environment = {"PATH": os.defpath, "LANG": "C.UTF-8"}
    subprocess.run(["rtk", "proxy", str(binary), "-I", "-c",
                    "import sys,ssl,venv; print(sys.version.split()[0])"],
                   env=dict(environment, PATH=os.environ["PATH"]), check=True)


if __name__ == "__main__":
    normalize(Path(os.environ["pythonLocation"]), Path(os.environ["RUNNER_TOOL_CACHE"]))
