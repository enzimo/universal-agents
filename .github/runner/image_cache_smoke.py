"""Prove local layer reuse after deleting and recreating the Buildx builder."""

import logging
import os
import re
import subprocess
import sys
from pathlib import Path

LOG = logging.getLogger("ua.ci.image-cache-probe")


def main() -> None:
    logging.basicConfig(level=logging.INFO)
    script = Path(__file__).with_name("image_cache.py").resolve()
    context = Path(os.environ["UA_CI_CACHE"]) / "image-cache-probe"
    context.mkdir()
    instruction = "RUN echo UA_CI_CACHE_OK > /cache-marker"
    (context / "Dockerfile.cache-probe").write_text(
        f"FROM busybox:1.37 AS cache-probe\n{instruction}\n",
    )
    builder = os.environ["UA_CI_BUILDER"]
    tag = "ua-ci-cache-probe:" + os.environ["UA_CI_ID"]
    command = ["rtk", "proxy", sys.executable, str(script),
               "--dockerfile", "Dockerfile.cache-probe", "--target", "cache-probe", "--tag", tag]
    subprocess.run(command, check=True, cwd=context, timeout=180)
    subprocess.run(["rtk", "proxy", "docker", "buildx", "rm", "--force", builder], check=True)
    subprocess.run(["rtk", "proxy", "docker", "buildx", "create", "--name", builder,
                    "--driver", "docker-container"], check=True)
    result = subprocess.run(command, check=True, cwd=context, timeout=180,
                            stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
    LOG.info("Fresh builder cache probe:\n%s", result.stdout)
    step = re.search(r"(#\d+) \[.*\] " + re.escape(instruction), result.stdout)
    if not step or step[1] + " CACHED" not in result.stdout:
        raise RuntimeError("Fresh builder did not reuse the exported RUN layer")
    output = subprocess.check_output(["rtk", "proxy", "docker", "run", "--rm", tag,
                                      "cat", "/cache-marker"], text=True, timeout=30)
    if output.strip() != "UA_CI_CACHE_OK":
        raise RuntimeError("Cached image did not contain the expected build output")
    LOG.info("Local image cache reused a RUN layer across disposable builders")


if __name__ == "__main__":
    main()
