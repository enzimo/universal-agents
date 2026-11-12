"""Sample host pressure during a job; these are host readings, not job quotas."""

from __future__ import annotations

import json
import logging
import os
import platform
import re
import subprocess
import sys
import time
from pathlib import Path

from build_cache import reject_link, write_json

LOG = logging.getLogger("ua.ci.metrics")
SAMPLERS: dict[str, subprocess.Popen] = {}


def active(state: Path, identity: str) -> bool:
    try:
        return json.loads((state / "job.json").read_text())["id"] == identity
    except FileNotFoundError:
        return False


def reading(previous: list[int] | None) -> tuple[dict, list[int] | None]:
    system = platform.system()
    if system == "Linux":
        # guest and guest_nice are already included in user and nice.
        cpu = list(map(int, Path("/proc/stat").read_text().splitlines()[0].split()[1:9]))
        memory = {line.split(":")[0]: int(line.split()[1]) * 1024
                  for line in Path("/proc/meminfo").read_text().splitlines()}
        result = {"memory_used_bytes": memory["MemTotal"] - memory["MemAvailable"],
                  "memory_total_bytes": memory["MemTotal"]}
        if previous is not None:
            total = sum(cpu) - sum(previous)
            idle = cpu[3] + cpu[4] - previous[3] - previous[4]
            if total > 0:
                result["cpu_percent"] = 100 * (total - idle) / total
        return result, cpu
    if system == "Darwin":
        output = subprocess.check_output(
            ["rtk", "proxy", "/usr/bin/top", "-l", "2", "-s", "1", "-n", "0"], text=True, timeout=10,
        )
        cpus = re.findall(r"CPU usage:.*?([\d.]+)% idle", output)
        memories = re.findall(r"PhysMem:\s*([\d.]+)([KMGT]) used", output)
        if not cpus or not memories:
            raise RuntimeError("Cannot read macOS host CPU/memory statistics")
        memory, unit = memories[-1]
        return {"cpu_percent": 100 - float(cpus[-1]),
                "memory_used_bytes": int(float(memory) * 1024 ** ("KMGT".index(unit) + 1))}, None
    command = (
        "$ErrorActionPreference='Stop';"
        "$cpu=Get-CimInstance Win32_PerfFormattedData_PerfOS_Processor -Filter \"Name='_Total'\";"
        "$mem=Get-CimInstance Win32_OperatingSystem;"
        "if($null -eq $cpu -or $null -eq $mem){throw 'Host statistics unavailable'};"
        "@{cpu_percent=[double]$cpu.PercentProcessorTime;"
        "memory_used_bytes=([long]$mem.TotalVisibleMemorySize-[long]$mem.FreePhysicalMemory)*1024;"
        "memory_total_bytes=[long]$mem.TotalVisibleMemorySize*1024}|ConvertTo-Json -Compress"
    )
    output = subprocess.check_output(
        ["rtk", "proxy", "powershell", "-NoProfile", "-NonInteractive", "-Command", command],
        text=True, timeout=15,
    )
    return json.loads(output), None


def sample(state: Path, identity: str, interval: float) -> None:
    output = state / "cache" / f"resources-{identity}.json"
    stop = state / "cache" / f"resources-{identity}.stop"
    result = {"job_id": identity, "samples": 0, "scope": "host", "complete": False}
    if platform.system() == "Linux" and "microsoft" in platform.release().lower():
        result["scope"] = "wsl-vm"
    previous = None
    while True:
        if not active(state, identity):
            return
        row, previous = reading(previous)
        # A reader can stall while a separate recovery process moves on. Its
        # files are job-specific, and it must still own the job after reading.
        if not active(state, identity):
            return
        result["samples"] += 1
        for name in ("cpu_percent", "memory_used_bytes"):
            if name in row:
                key = "sampled_peak_" + name
                result[key] = max(result.get(key, row[name]), row[name])
        if "memory_total_bytes" in row:
            result["memory_total_bytes"] = row["memory_total_bytes"]
        result["complete"] = stop.exists()
        write_json(output, result)
        if result["complete"]:
            return
        deadline = time.monotonic() + interval
        while not stop.exists() and time.monotonic() < deadline:
            time.sleep(0.2)


def start(config: dict, job: dict) -> None:
    state = Path(config["state"])
    options = {"start_new_session": True} if os.name != "nt" else {
        "creationflags": subprocess.CREATE_NEW_PROCESS_GROUP | subprocess.DETACHED_PROCESS,
    }
    # Do not hold a job-cache log file open across hook processes. In particular,
    # Windows must be able to remove that directory even after a sampler stalls.
    SAMPLERS[job["id"]] = subprocess.Popen(
        [sys.executable, str(Path(__file__).resolve()), str(state), job["id"],
         str(config["metrics"]["interval_seconds"])],
        stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, **options,
    )


def finish(config: dict, job: dict, *, recovered: bool) -> None:
    state = Path(config["state"])
    cache = state / "cache"
    if not job.get("metrics") or not cache.is_dir():
        return
    (cache / f"resources-{job['id']}.stop").touch()
    output = cache / f"resources-{job['id']}.json"
    result = {}
    for _ in range(100):
        if output.is_file():
            result = json.loads(output.read_text())
            if result.get("job_id") != job["id"]:
                raise ValueError("Resource observations belong to a different job")
            if result.get("complete"):
                break
        time.sleep(0.2)
    if not result.get("complete"):
        LOG.warning("Host resource sampler did not finish; readings are incomplete")
    process = SAMPLERS.pop(job["id"], None)
    if process is not None:
        process.wait(timeout=20)
    result.update({"job": job["job"], "run_id": job["run_id"], "attempt": job["attempt"],
                   "elapsed_seconds": round(time.time() - job["started_at"], 3),
                   "recovered": recovered})
    directory = state.parent / "metrics"
    reject_link(directory)
    directory.mkdir(exist_ok=True)
    write_json(directory / (job["id"] + ".json"), result)
    cutoff = time.time() - config["metrics"]["keep_days"] * 86400
    for path in directory.glob("*.json"):
        reject_link(path)
        if path.stat().st_mtime < cutoff:
            path.unlink()
    LOG.info("Job resource observations: %s", json.dumps(result, sort_keys=True))


if __name__ == "__main__":
    sample(Path(sys.argv[1]), sys.argv[2], float(sys.argv[3]))
