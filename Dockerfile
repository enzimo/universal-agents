FROM python:3.13-slim-trixie AS user-docs-builder

WORKDIR /app
COPY requirements-dev.lock.txt /app/requirements-dev.lock.txt
RUN python -m pip install --no-cache-dir --require-hashes -r /app/requirements-dev.lock.txt
COPY mkdocs.yml /app/mkdocs.yml
COPY scripts/build_user_docs.py /app/scripts/build_user_docs.py
COPY user_docs_source /app/user_docs_source
COPY user_docs/theme /app/user_docs/theme
COPY assistant_gateway/onboarding_web/assets/metallic_mesh.js /app/assistant_gateway/onboarding_web/assets/metallic_mesh.js
RUN python /app/scripts/build_user_docs.py

FROM rust:1.95.0-slim-trixie AS rust-tools-builder

ARG CARGO_BUILD_JOBS=2

WORKDIR /src

RUN apt-get update \
    && apt-get install -y --no-install-recommends build-essential pkg-config \
    && rm -rf /var/lib/apt/lists/*

COPY Cargo.toml Cargo.lock /src/
COPY rust/vendor /src/rust/vendor
COPY rust/ua-anydoc-cli/Cargo.toml /src/rust/ua-anydoc-cli/Cargo.toml
COPY rust/ua-anydoc-cli/src /src/rust/ua-anydoc-cli/src
COPY rust/ua-cedar-authorizer/Cargo.toml /src/rust/ua-cedar-authorizer/Cargo.toml
COPY rust/ua-cedar-authorizer/src /src/rust/ua-cedar-authorizer/src
RUN cargo build --locked --release --bin anydoc --bin ua-cedar-authorizer

FROM python:3.13-slim-trixie

ARG CARGO_BUILD_JOBS=2

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    DEBIAN_FRONTEND=noninteractive \
    PIP_NO_CACHE_DIR=1 \
    TEAM_DOCKER_IMAGE_MODE=standard \
    CARGO_HOME=/usr/local/cargo \
    RUSTUP_HOME=/usr/local/rustup \
    PATH=/root/.local/bin:/usr/local/cargo/bin:${PATH}

ARG INSTALL_OPENSHELL=false
ARG OPENSHELL_VERSION=latest
ARG INSTALL_NATS=true
ARG NATS_SERVER_VERSION=v2.14.1
ARG INSTALL_POSTGRES=false
ARG INSTALL_SQLITE=false
ARG INSTALL_KUBECTL=false
ARG KUBECTL_VERSION=v1.34.1
ARG XURL_VERSION=1.3.1
ARG XURL_SHA256_AMD64=ccfc4b3ca5240565da650ef003b51d7f887eabe0c6e3797802410240289d4feb
ARG XURL_SHA256_ARM64=22474398fb55fe2ae242cc1b940dd5d92c0083508d108ec95922d1adacd2bb76
ARG INSTALL_CREDENTIAL_CLIS=sealbox
ARG INSTALL_NODEJS=false
ARG NODEJS_VERSION=24.20.0
ARG NODEJS_SHA256_AMD64=855d581f8a4eb1a8117e3426de25fe02770592febcfb31369aee1ffbfee9e8ec
ARG NODEJS_SHA256_ARM64=3515603e2487879a39bc75716f1a2affd027500c64ba50e845cf72cb33219013
ARG STRIP_SOURCE_CODE=true
ARG YQ_VERSION=v4.44.3
ARG POSTGRES_MAJOR=18
ARG POSTGRES_VERSION=18.4-1.pgdg13+1
ARG PGVECTOR_PACKAGE_VERSION=0.8.5-1.pgdg13+1
# Live Debian mirrors replace old revisions. Follow Trixie updates unless explicitly pinned.
ARG SQLITE_VERSION=
ARG BW_VERSION=2025.10.0
ARG BW_FILE_URL=
ARG BW_FILE_URL_AMD64=
ARG BW_FILE_URL_ARM64=
ARG BW_FILE_SHA256=
ARG BW_FILE_SHA256_AMD64=
ARG BW_FILE_SHA256_ARM64=
ARG SEALBOX_REPO_URL=https://github.com/enzimo/sealbox.git
ARG SEALBOX_GIT_REF=main

WORKDIR /app

COPY --from=rust-tools-builder /src/target/release/anydoc /usr/local/bin/anydoc
COPY --from=rust-tools-builder /src/target/release/ua-cedar-authorizer /usr/local/bin/ua-cedar-authorizer

RUN anydoc --version

RUN set -eux; \
    case "${INSTALL_NODEJS}" in true|false) ;; *) echo "INSTALL_NODEJS must be true or false, got: ${INSTALL_NODEJS}" >&2; exit 2 ;; esac; \
    apt-get update; \
    apt-get install -y --no-install-recommends ca-certificates curl; \
    if [ "${INSTALL_POSTGRES}" = "true" ]; then \
        install -d -m 0755 /usr/share/postgresql-common/pgdg; \
        curl -fsSL https://www.postgresql.org/media/keys/ACCC4CF8.asc -o /usr/share/postgresql-common/pgdg/apt.postgresql.org.asc; \
        echo "deb [signed-by=/usr/share/postgresql-common/pgdg/apt.postgresql.org.asc] https://apt.postgresql.org/pub/repos/apt trixie-pgdg main" > /etc/apt/sources.list.d/pgdg.list; \
        apt-get update; \
    fi; \
    packages="\
        bash \
        build-essential \
        ca-certificates \
        curl \
        dnsutils \
        ffmpeg \
        git \
        iputils-ping \
        jq \
        nano \
        openssh-client \
        pkg-config \
        sed \
        tini \
        tzdata \
        wget \
        yt-dlp"; \
    if [ "${INSTALL_SQLITE}" = "true" ]; then \
        sqlite_version_spec="${SQLITE_VERSION:+=$SQLITE_VERSION}"; \
        packages="${packages} sqlite3${sqlite_version_spec} libsqlite3-0${sqlite_version_spec} libsqlite3-dev${sqlite_version_spec}"; \
    fi; \
    if [ "${INSTALL_POSTGRES}" = "true" ]; then \
        packages="${packages} postgresql-${POSTGRES_MAJOR}=${POSTGRES_VERSION} postgresql-client-${POSTGRES_MAJOR}=${POSTGRES_VERSION} postgresql-server-dev-${POSTGRES_MAJOR}=${POSTGRES_VERSION} postgresql-${POSTGRES_MAJOR}-pgvector=${PGVECTOR_PACKAGE_VERSION}"; \
    fi; \
    apt-get install -y --no-install-recommends ${packages}; \
    if [ "${INSTALL_NODEJS}" = "true" ]; then \
        case "$(dpkg --print-architecture)" in \
            amd64) node_arch="x64"; node_sha256="${NODEJS_SHA256_AMD64}" ;; \
            arm64) node_arch="arm64"; node_sha256="${NODEJS_SHA256_ARM64}" ;; \
            *) echo "unsupported Node.js architecture: $(dpkg --print-architecture)" >&2; exit 1 ;; \
        esac; \
        node_archive="node-v${NODEJS_VERSION}-linux-${node_arch}.tar.gz"; \
        curl -fsSL "https://nodejs.org/dist/v${NODEJS_VERSION}/${node_archive}" -o "/tmp/${node_archive}"; \
        echo "${node_sha256}  /tmp/${node_archive}" | sha256sum -c -; \
        tar -xzf "/tmp/${node_archive}" -C /usr/local --strip-components=1; \
        rm -f "/tmp/${node_archive}"; \
        test "$(node --version)" = "v${NODEJS_VERSION}"; \
        node --version; \
        npm --version; \
    fi; \
    rm -rf /var/lib/apt/lists/*

RUN if [ "${INSTALL_KUBECTL}" = "true" ]; then \
        set -e; \
        case "$(dpkg --print-architecture)" in \
            amd64) kubectl_arch="amd64" ;; \
            arm64) kubectl_arch="arm64" ;; \
            *) echo "unsupported kubectl architecture: $(dpkg --print-architecture)" >&2; exit 1 ;; \
        esac; \
        kubectl_url="https://dl.k8s.io/release/${KUBECTL_VERSION}/bin/linux/${kubectl_arch}/kubectl"; \
        curl -fsSL "${kubectl_url}" -o /tmp/kubectl; \
        curl -fsSL "${kubectl_url}.sha256" -o /tmp/kubectl.sha256; \
        echo "$(cat /tmp/kubectl.sha256)  /tmp/kubectl" | sha256sum -c -; \
        install -m 755 /tmp/kubectl /usr/local/bin/kubectl; \
        rm -f /tmp/kubectl /tmp/kubectl.sha256; \
        kubectl version --client=true; \
    else \
        echo "Skipping kubectl install because INSTALL_KUBECTL=${INSTALL_KUBECTL}"; \
    fi

RUN set -e; \
    case "$(dpkg --print-architecture)" in \
        amd64) yq_arch="amd64" ;; \
        arm64) yq_arch="arm64" ;; \
        armhf) yq_arch="arm" ;; \
        *) echo "unsupported yq architecture: $(dpkg --print-architecture)" >&2; exit 1 ;; \
    esac; \
    curl -fsSL "https://github.com/mikefarah/yq/releases/download/${YQ_VERSION}/yq_linux_${yq_arch}" -o /usr/local/bin/yq; \
    chmod +x /usr/local/bin/yq; \
    yq --version

RUN if [ "${INSTALL_POSTGRES}" = "true" ]; then \
        set -e; \
        "/usr/lib/postgresql/${POSTGRES_MAJOR}/bin/postgres" --version; \
        test -f "/usr/share/postgresql/${POSTGRES_MAJOR}/extension/vector.control"; \
    else \
        echo "Skipping pgvector install because INSTALL_POSTGRES=${INSTALL_POSTGRES}"; \
    fi

RUN if [ "${INSTALL_NATS}" = "true" ]; then \
        set -e; \
        case "$(uname -m)" in \
            x86_64|amd64) nats_arch="amd64" ;; \
            aarch64|arm64) nats_arch="arm64" ;; \
            armv7l) nats_arch="arm7" ;; \
            *) echo "unsupported NATS server architecture: $(uname -m)" >&2; exit 1 ;; \
        esac; \
        nats_tmp="$(mktemp -d)"; \
        nats_archive="nats-server-${NATS_SERVER_VERSION}-linux-${nats_arch}.tar.gz"; \
        curl -fsSL "https://github.com/nats-io/nats-server/releases/download/${NATS_SERVER_VERSION}/${nats_archive}" -o "${nats_tmp}/${nats_archive}"; \
        tar -xzf "${nats_tmp}/${nats_archive}" -C "${nats_tmp}" --no-same-owner --no-same-permissions; \
        install -m 755 "${nats_tmp}/nats-server-${NATS_SERVER_VERSION}-linux-${nats_arch}/nats-server" /usr/local/bin/nats-server; \
        rm -rf "${nats_tmp}"; \
        nats-server --version; \
    else \
        echo "Skipping NATS server install because INSTALL_NATS=${INSTALL_NATS}"; \
    fi

COPY docker/install_xurl.sh /usr/local/bin/install_xurl.sh
RUN sh /usr/local/bin/install_xurl.sh

COPY docker/install_credential_clis.sh /usr/local/bin/install_credential_clis.sh
RUN chmod +x /usr/local/bin/install_credential_clis.sh \
    && /usr/local/bin/install_credential_clis.sh

RUN curl -fsSL https://sh.rustup.rs | sh -s -- -y --profile minimal \
    && RUSTFLAGS="-A warnings" cargo install --git https://github.com/enzimo/rtk rtk --locked --root /usr/local \
    && credential_selection="$(printf '%s' "${INSTALL_CREDENTIAL_CLIS:-}" | tr '[:upper:]' '[:lower:]' | tr '[]; ' ',,,,')" \
    && case ",${credential_selection}," in \
        *,all,*|*,sealbox,*|*,sealbox-cli,*) \
            sealbox_tmp="$(mktemp -d)" \
            && git init "${sealbox_tmp}" \
            && git -C "${sealbox_tmp}" remote add origin "${SEALBOX_REPO_URL}" \
            && git -C "${sealbox_tmp}" fetch --depth 1 origin "${SEALBOX_GIT_REF}" \
            && git -C "${sealbox_tmp}" checkout --detach FETCH_HEAD \
            && echo "Building Sealbox ref $(git -C "${sealbox_tmp}" rev-parse --short HEAD)" \
            && cargo build --manifest-path "${sealbox_tmp}/Cargo.toml" --release --locked -p sealbox-cli -p sealbox-server \
            && install -m 755 "${sealbox_tmp}/target/release/sealbox-cli" /usr/local/bin/sealbox-cli \
            && install -m 755 "${sealbox_tmp}/target/release/sealbox-server" /usr/local/bin/sealbox-server \
            && rm -rf "${sealbox_tmp}" \
            && sealbox-cli --help >/dev/null \
            && sealbox-cli credential delete --help >/dev/null \
            && sealbox-cli secret delete --help | grep -F "If omitted, all versions are deleted" >/dev/null \
            ;; \
        *) echo "Skipping Sealbox install because INSTALL_CREDENTIAL_CLIS=${INSTALL_CREDENTIAL_CLIS:-<empty>}" ;; \
    esac \
    && rtk --version \
    && rm -rf "${CARGO_HOME}" "${RUSTUP_HOME}" \
    && for profile in /root/.bashrc /root/.profile; do \
        if [ -f "${profile}" ]; then sed -i '\|/usr/local/cargo/env|d' "${profile}"; fi; \
    done

RUN if [ "${INSTALL_OPENSHELL}" = "true" ]; then \
        set -e; \
        openshell_installer="$(mktemp)"; \
        curl -fsSL https://raw.githubusercontent.com/NVIDIA/OpenShell/main/install.sh -o "${openshell_installer}"; \
        if [ "${OPENSHELL_VERSION}" = "latest" ]; then \
            unset OPENSHELL_VERSION; \
            OPENSHELL_INSTALL_DIR=/usr/local/bin sh "${openshell_installer}"; \
        else \
            OPENSHELL_VERSION="${OPENSHELL_VERSION}" OPENSHELL_INSTALL_DIR=/usr/local/bin sh "${openshell_installer}"; \
        fi; \
        rm -f "${openshell_installer}"; \
        openshell --version; \
        installed_openshell_version="$(openshell --version | awk '{print $2}')"; \
        case "${installed_openshell_version}" in v*) gateway_version="${installed_openshell_version}" ;; *) gateway_version="v${installed_openshell_version}" ;; esac; \
        case "$(uname -m)" in \
            x86_64|amd64) gateway_target="x86_64-unknown-linux-gnu" ;; \
            aarch64|arm64) gateway_target="aarch64-unknown-linux-gnu" ;; \
            *) echo "unsupported OpenShell gateway architecture: $(uname -m)" >&2; exit 1 ;; \
        esac; \
        gateway_filename="openshell-gateway-${gateway_target}.tar.gz"; \
        gateway_tmp="$(mktemp -d)"; \
        curl -fsSL "https://github.com/NVIDIA/OpenShell/releases/download/${gateway_version}/${gateway_filename}" -o "${gateway_tmp}/${gateway_filename}"; \
        curl -fsSL "https://github.com/NVIDIA/OpenShell/releases/download/${gateway_version}/openshell-gateway-checksums-sha256.txt" -o "${gateway_tmp}/openshell-gateway-checksums-sha256.txt"; \
        grep -F "${gateway_filename}" "${gateway_tmp}/openshell-gateway-checksums-sha256.txt" | (cd "${gateway_tmp}" && sha256sum -c -); \
        tar -xzf "${gateway_tmp}/${gateway_filename}" -C "${gateway_tmp}" --no-same-owner --no-same-permissions openshell-gateway; \
        install -m 755 "${gateway_tmp}/openshell-gateway" /usr/local/bin/openshell-gateway; \
        rm -rf "${gateway_tmp}"; \
        openshell-gateway --version; \
    fi

COPY requirements.txt /app/requirements.txt
COPY requirements.lock.txt /app/requirements.lock.txt
COPY requirements.skillspector.txt /app/requirements.skillspector.txt
COPY requirements.skillspector.lock.txt /app/requirements.skillspector.lock.txt
RUN pip install --require-hashes -r /app/requirements.lock.txt
RUN hf version && hf skills preview >/dev/null

RUN python -m venv /opt/skillspector \
    && /opt/skillspector/bin/python -m pip install --require-hashes -r /app/requirements.skillspector.lock.txt \
    && ln -s /opt/skillspector/bin/skillspector /usr/local/bin/skillspector \
    && skillspector --version
RUN if [ "${INSTALL_SQLITE}" = "true" ]; then \
        python -c "import sqlite3, sqlite_vec; conn = sqlite3.connect(':memory:'); sqlite_vec.load(conn); print(conn.execute('SELECT vec_version()').fetchone()[0])"; \
    else \
        echo "Skipping sqlite-vec smoke check because INSTALL_SQLITE=${INSTALL_SQLITE}"; \
    fi
RUN python -c "import audioop, discord, nacl, nats"

COPY assistant_gateway/interactive_apps/photon_sidecar/package.json assistant_gateway/interactive_apps/photon_sidecar/package-lock.json /app/assistant_gateway/interactive_apps/photon_sidecar/
RUN if [ "${INSTALL_NODEJS}" = "true" ]; then \
        cd /app/assistant_gateway/interactive_apps/photon_sidecar \
        && npm ci --omit=dev --no-audit --no-fund \
        && npm cache clean --force; \
    else \
        echo "Skipping Photon sidecar npm install because INSTALL_NODEJS=${INSTALL_NODEJS}"; \
    fi

COPY . /app

# The access service runs this evaluator with the shipped Cedar files; refuse
# to build an image whose policies do not validate strictly.
RUN ua-cedar-authorizer validate-access \
    --policy /app/ua_definitions/access/cedar/policies.cedar \
    --schema /app/ua_definitions/access/cedar/schema.cedarschema

RUN test -f /app/ua_definitions/agent_definitions/builtins.yaml \
    && test -f /app/ua_definitions/agent_bundles/practical_assistants/agent.yaml \
    && test -f /app/ua_definitions/agent_bundles/code_implementation/agent.yaml \
    && test -f /app/ua_definitions/credential_templates/builtins.yaml \
    && test -f /app/ua_definitions/objective_execution/default.yaml \
    && test -f /app/ua_definitions/access/starter_profiles.yaml \
    && test -f /app/custom_agents/skill_templates/long-horizon-plan-execution/SKILL.md

COPY --from=user-docs-builder /app/user_docs/site /app/user_docs/site

RUN chmod +x /app/docker/entrypoint.sh /app/docker/strip_python_sources.sh \
    && case "${STRIP_SOURCE_CODE}" in \
        true) /app/docker/strip_python_sources.sh /app ;; \
        false) echo "Keeping Python source files because STRIP_SOURCE_CODE=${STRIP_SOURCE_CODE}" ;; \
        *) echo "STRIP_SOURCE_CODE must be true or false, got: ${STRIP_SOURCE_CODE}" >&2; exit 2 ;; \
    esac

RUN python -c "from time_presentation import format_user_timestamp; assert 'CDT' in format_user_timestamp('2026-08-03T21:21:01+00:00', timezone_name='America/Chicago')"

VOLUME ["/data"]

ENTRYPOINT ["/usr/bin/tini", "--", "/app/docker/entrypoint.sh"]
CMD ["run"]
