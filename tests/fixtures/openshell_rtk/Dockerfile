ARG UA_BASE_IMAGE=ua-bundle:latest
FROM ${UA_BASE_IMAGE}

# OpenShell's network proxy requires a trusted iproute2 helper in the sandbox.
RUN apt-get update \
    && apt-get install -y --no-install-recommends iproute2 \
    && rm -rf /var/lib/apt/lists/* \
    && (getent group sandbox >/dev/null || groupadd --system sandbox) \
    && (id -u sandbox >/dev/null 2>&1 || useradd --system --gid sandbox --create-home --shell /bin/sh sandbox) \
    && mkdir -p /workspace \
    && chown sandbox:sandbox /workspace

# OpenShell supplies its own supervisor lifecycle. The production UA entrypoint
# expects instance configuration and must not run in this execution-only fixture.
ENTRYPOINT []
CMD ["sleep", "infinity"]
