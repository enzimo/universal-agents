"""Prove a real Rust compiler cache hit without preserving target output."""

import json
import os
import shutil
import subprocess
import tempfile
from pathlib import Path


def run(*args):
    return subprocess.check_output(["rtk", "proxy", *args], text=True, timeout=120)


def main():
    if os.environ.get("UA_CI_BUILD_CACHE_VERSION") != "1":
        raise RuntimeError("Install the reviewed bounded-cache hooks first")
    wrapper = os.environ["RUSTC_WRAPPER"]
    compiler = run("rustup", "which", "rustc", "--toolchain", "1.95.0").strip()
    with tempfile.TemporaryDirectory(prefix="compiler-probe-", dir=os.environ["UA_CI_CACHE"]) as tmp:
        path = Path(tmp)
        source = path / "probe.rs"
        source.write_text("pub fn answer() -> u32 { 42 }\n")
        target = path / "out"
        before = json.loads(run(wrapper, "--show-stats", "--stats-format", "json"))
        for _ in range(2):
            target.mkdir()
            run(wrapper, compiler, "--crate-name", "ua_ci_cache_probe", "--crate-type", "rlib",
                "--emit", "link,metadata,dep-info", "--out-dir", str(target), str(source))
            assert (target / "libua_ci_cache_probe.rlib").is_file()
            shutil.rmtree(target)
        after = json.loads(run(wrapper, "--show-stats", "--stats-format", "json"))
        hits = sum(after["stats"]["cache_hits"]["counts"].values()) - sum(before["stats"]["cache_hits"]["counts"].values())
        if hits < 1:
            raise RuntimeError(f"Second compilation did not hit cache: {after}")
        print(json.dumps({"compiler_cache_hits": hits, "target_deleted_between_builds": True}))


if __name__ == "__main__":
    main()
