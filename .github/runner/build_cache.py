"""Bounded, runner-owned compiler and package download caches.

Only one job may use this cache root at a time. Installed hooks own admission,
server lifecycle, and eviction; checkouts cannot choose cache paths or limits.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import shutil
import socket
import subprocess
import time
from pathlib import Path

LOG = logging.getLogger("ua.ci.cache")
OWNER = "universal-agents-ci-build-cache-v1"
# Files a desktop's file browser leaves in any folder someone opens: macOS
# Finder's .DS_Store and ._ resource forks, Windows Explorer's Thumbs.db and
# desktop.ini. A .DS_Store in the cache root stopped the macOS release build
# (v0.8.4, 2026-10-09). They hold no cache data, so cleanup removes them with
# a warning; any other unexpected file in the root still stops the job.
DESKTOP_METADATA_FILES = frozenset({".DS_Store", "Thumbs.db", "desktop.ini"})


def is_desktop_metadata(path: Path) -> bool:
    return path.name in DESKTOP_METADATA_FILES or path.name.startswith("._")


def reject_link(path: Path) -> None:
    if path.is_symlink() or path.is_junction():
        raise ValueError(f"Links are not allowed in the managed build cache: {path}")


def root(config: dict) -> Path:
    path = Path(config["state"]).parent / "build-cache-v1"
    reject_link(path)
    marker = path / ".owner"
    if not path.exists():
        path.mkdir()
        marker.write_text(OWNER)
    reject_link(marker)
    if not marker.is_file() or marker.read_text() != OWNER:
        raise ValueError("Refusing an unowned build cache directory")
    return path


def key(repository: str, scope: str) -> str:
    return hashlib.sha256(f"{repository}\n{scope}".encode()).hexdigest()


def scope() -> str:
    # PR merge refs, feature pushes and tag builds never write main's cache.
    ref = os.environ.get("GITHUB_REF", "")
    event = os.environ.get("GITHUB_EVENT_NAME", "")
    if ref == "refs/heads/main" and event in {"push", "workflow_dispatch"}:
        return "main"
    return f"{event}:{ref}"


def size(path: Path) -> int:
    total = 0
    if os.name == "nt" and not str(path).startswith("\\\\?\\"):
        absolute = str(path.absolute())
        path = Path("\\\\?\\UNC\\" + absolute[2:] if absolute.startswith("\\\\")
                    else "\\\\?\\" + absolute)
    reject_link(path)

    def unreadable(error):
        raise error

    for directory, folders, files in os.walk(path, onerror=unreadable):
        for name in folders + files:
            entry = Path(directory) / name
            reject_link(entry)
            if name in files:
                total += entry.stat().st_size
    return total


def entries(config: dict, remove) -> list[tuple[Path, dict, int]]:
    result = []
    for path in root(config).iterdir():
        if path.name == ".owner":
            continue
        reject_link(path)
        if not path.is_dir():
            if is_desktop_metadata(path):
                LOG.warning("Removed a file browser's metadata file from the build cache: %s", path)
                path.unlink()
                continue
            raise ValueError(f"Unexpected file in the managed build cache: {path}")
        metadata = path / "entry.json"
        reject_link(metadata)
        row = json.loads(metadata.read_text())
        if (row["repository"] not in config["repositories"]
                or path.name != key(row["repository"], row["scope"])):
            raise ValueError("Invalid build cache ownership")
        # npm's cache option also redirects npx execution environments. Keep
        # only download data between jobs. rmtree removes internal links without
        # following them; reject links at the deletion boundary itself.
        reject_link(path / "npm")
        for name in ("_npx", "_logs"):
            disposable = path / "npm" / name
            reject_link(disposable)
            remove(disposable)
        result.append((path, row, size(path)))
    return sorted(result, key=lambda entry: entry[1]["last_used"])


def low_disk(config: dict) -> bool:
    paths = [Path(config["state"]), Path(config["work"])]
    paths.extend(Path(path) for path in config.get("reserve_paths", []))
    return any(shutil.disk_usage(path).free < config.get("min_free_gib", 30) * 1024**3
               for path in paths)


def prune(config: dict, remove) -> None:
    rows = entries(config, remove)
    total = sum(row[2] for row in rows)
    limit = config["build_cache"]["max_bytes"]
    cutoff = time.time() - config["build_cache"]["max_age_days"] * 86400
    for path, metadata, used in rows:
        if metadata["last_used"] < cutoff or total > limit or low_disk(config):
            LOG.info("Evicting cache %s (%d bytes)", path.name, used)
            remove(path)
            total -= used
    LOG.info("Retained build caches: %d bytes / %d bytes", total, limit)


def prepare(config: dict) -> dict[str, str]:
    repository = os.environ["GITHUB_REPOSITORY"]
    cache_scope = scope()
    path = root(config) / key(repository, cache_scope)
    reject_link(path)
    path.mkdir(exist_ok=True)
    (path / "entry.json").write_text(json.dumps({
        "repository": repository, "scope": cache_scope, "last_used": time.time(),
    }))
    for name in ("compiler", "pip", "npm"):
        (path / name).mkdir(exist_ok=True)
    state = Path(config["state"])
    # An empty job-owned config prevents loading personal/remote cache settings.
    conf = state / "cache" / "sccache.toml"
    conf.write_text("")
    env = {
        "UA_CI_BUILD_CACHE_VERSION": "1",
        "RUSTC_WRAPPER": config["build_cache"]["sccache"],
        "SCCACHE_CONF": str(conf),
        "SCCACHE_DIR": str(path / "compiler"),
        "SCCACHE_CACHE_SIZE": "3G",
        "SCCACHE_IDLE_TIMEOUT": "0",
        "SCCACHE_CACHED_CONF": str(state / "cache" / "sccache-state"),
        "PIP_CACHE_DIR": str(path / "pip"),
        "npm_config_cache": str(path / "npm"),
    }
    if os.name == "nt":
        env["SCCACHE_SERVER_PORT"] = str(config["build_cache"]["server_port"])
    else:
        env["SCCACHE_SERVER_UDS"] = str(state / "sccache.sock")
    LOG.info("Using build cache %s, scope %s", path.name, cache_scope)
    return env


def command(env: dict, operation: str) -> subprocess.CompletedProcess:
    process_env = {k: v for k, v in os.environ.items() if not k.startswith("SCCACHE_")}
    process_env.update(env)
    return subprocess.run(["rtk", "proxy", env["RUSTC_WRAPPER"], operation],
                          env=process_env, capture_output=True, text=True, timeout=30, check=False)


def listening(env: dict) -> bool:
    if "SCCACHE_SERVER_UDS" in env:
        endpoint = env["SCCACHE_SERVER_UDS"]
        family = socket.AF_UNIX
    else:
        endpoint = ("127.0.0.1", int(env["SCCACHE_SERVER_PORT"]))
        family = socket.AF_INET
    with socket.socket(family, socket.SOCK_STREAM) as connection:
        connection.settimeout(2)
        return connection.connect_ex(endpoint) == 0


def start(env: dict) -> None:
    if listening(env):
        raise RuntimeError("Dedicated CI compiler-cache endpoint is already occupied")
    result = command(env, "--start-server")
    if result.returncode:
        raise RuntimeError(f"Compiler cache startup failed: {result.stderr}")


def stop(env: dict) -> None:
    if listening(env):
        result = command(env, "--stop-server")
        if result.returncode:
            raise RuntimeError(f"Compiler cache shutdown failed: {result.stderr}")
        LOG.info("Compiler cache statistics:\n%s", result.stdout.strip())
        for _ in range(50):
            if not listening(env):
                break
            time.sleep(0.1)
        else:
            raise RuntimeError("Compiler cache server did not stop")
    if "SCCACHE_SERVER_UDS" in env:
        Path(env["SCCACHE_SERVER_UDS"]).unlink(missing_ok=True)
