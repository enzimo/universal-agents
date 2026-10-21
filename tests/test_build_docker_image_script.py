from __future__ import annotations

import os
import json
from pathlib import Path
import shutil
import subprocess
import sys

import pytest


def _init_tagged_repo(tmp_path: Path) -> tuple[Path, Path]:
    repo = tmp_path / "repo"
    repo.mkdir()
    script_dir = repo / "scripts"
    script_dir.mkdir()
    shutil.copy2(Path.cwd() / "scripts" / "build_docker_image.sh", script_dir / "build_docker_image.sh")
    (repo / "Dockerfile").write_text("FROM scratch\n", encoding="utf-8")
    (repo / "Dockerfile.bundle").write_text("FROM scratch\n", encoding="utf-8")
    subprocess.run(["git", "init"], cwd=repo, check=True, capture_output=True, text=True)
    subprocess.run(["git", "config", "user.email", "bundle-tests@example.com"], cwd=repo, check=True)
    subprocess.run(["git", "config", "user.name", "Bundle Tests"], cwd=repo, check=True)
    subprocess.run(["git", "config", "commit.gpgSign", "false"], cwd=repo, check=True)
    subprocess.run(["git", "config", "tag.gpgSign", "false"], cwd=repo, check=True)
    subprocess.run(["git", "add", "."], cwd=repo, check=True)
    subprocess.run(["git", "commit", "-m", "initial"], cwd=repo, check=True, capture_output=True, text=True)
    subprocess.run(["git", "tag", "v1"], cwd=repo, check=True)

    capture = tmp_path / "docker_args.txt"
    docker_dir = tmp_path / "bin"
    docker_dir.mkdir()
    (docker_dir / "docker").write_text(
        "#!/bin/sh\n"
        "printf '%s\n' \"$@\" > \"$CAPTURE_FILE\"\n",
        encoding="utf-8",
    )
    (docker_dir / "uname").write_text(
        "#!/bin/sh\n"
        "if [ \"$1\" = \"-m\" ]; then\n"
        "  printf '%s\n' \"${FAKE_UNAME_M:-x86_64}\"\n"
        "else\n"
        "  /usr/bin/uname \"$@\"\n"
        "fi\n",
        encoding="utf-8",
    )
    subprocess.run(["chmod", "+x", str(docker_dir / "docker")], check=True)
    subprocess.run(["chmod", "+x", str(docker_dir / "uname")], check=True)
    return repo, capture


def test_build_docker_image_script_defaults_to_standard_image(tmp_path: Path) -> None:
    repo, capture = _init_tagged_repo(tmp_path)

    subprocess.run(
        ["bash", "scripts/build_docker_image.sh", "v1"],
        cwd=repo,
        env={**os.environ, "PATH": f"{tmp_path / 'bin'}:{os.environ['PATH']}", "CAPTURE_FILE": str(capture)},
        check=True,
        capture_output=True,
        text=True,
    )

    args = capture.read_text(encoding="utf-8").splitlines()
    assert args[0] == "build"
    assert "-t" in args
    assert "team-assistant:v1" in args
    assert any(arg.endswith("/Dockerfile") for arg in args)
    assert "INSTALL_NATS=true" in args
    assert "INSTALL_POSTGRES=false" in args
    assert "INSTALL_SQLITE=false" in args
    assert "INSTALL_NODEJS=false" in args
    assert "STRIP_SOURCE_CODE=true" in args


def test_build_docker_image_script_forwards_xurl_pins(tmp_path: Path) -> None:
    repo, capture = _init_tagged_repo(tmp_path)
    subprocess.run(
        ["bash", "scripts/build_docker_image.sh", "v1"], cwd=repo,
        env={**os.environ, "PATH": f"{tmp_path / 'bin'}:{os.environ['PATH']}",
             "CAPTURE_FILE": str(capture), "XURL_VERSION": "1.3.1",
             "XURL_SHA256_AMD64": "a" * 64, "XURL_SHA256_ARM64": "b" * 64},
        check=True, capture_output=True, text=True,
    )
    args = capture.read_text().splitlines()
    assert "XURL_VERSION=1.3.1" in args
    assert "XURL_SHA256_AMD64=" + "a" * 64 in args
    assert "XURL_SHA256_ARM64=" + "b" * 64 in args


def test_build_docker_image_script_accepts_nocache_alias(tmp_path: Path) -> None:
    repo, capture = _init_tagged_repo(tmp_path)

    subprocess.run(
        ["bash", "scripts/build_docker_image.sh", "--nocache", "v1"],
        cwd=repo,
        env={**os.environ, "PATH": f"{tmp_path / 'bin'}:{os.environ['PATH']}", "CAPTURE_FILE": str(capture)},
        check=True,
        capture_output=True,
        text=True,
    )

    args = capture.read_text(encoding="utf-8").splitlines()
    assert "--no-cache" in args


def test_build_docker_image_script_bundle_mode_uses_bundle_defaults(tmp_path: Path) -> None:
    repo, capture = _init_tagged_repo(tmp_path)

    subprocess.run(
        ["bash", "scripts/build_docker_image.sh", "--bundle", "v1"],
        cwd=repo,
        env={**os.environ, "PATH": f"{tmp_path / 'bin'}:{os.environ['PATH']}", "CAPTURE_FILE": str(capture)},
        check=True,
        capture_output=True,
        text=True,
    )

    args = capture.read_text(encoding="utf-8").splitlines()
    assert args[0] == "build"
    assert "-t" in args
    assert "team-assistant-bundle:v1" in args
    assert any(arg.endswith("/Dockerfile.bundle") for arg in args)
    assert "INSTALL_NATS=true" in args
    assert "INSTALL_POSTGRES=true" in args
    assert "INSTALL_SQLITE=true" in args
    assert "INSTALL_NODEJS=false" in args
    assert not any(arg.startswith("INSTALL_CREDENTIAL_CLIS=") for arg in args)


def test_build_docker_image_script_allows_component_overrides(tmp_path: Path) -> None:
    repo, capture = _init_tagged_repo(tmp_path)

    subprocess.run(
        [
            "bash",
            "scripts/build_docker_image.sh",
            "--bundle",
            "--no-nats",
            "--no-postgres",
            "--no-litesql",
            "v1",
        ],
        cwd=repo,
        env={**os.environ, "PATH": f"{tmp_path / 'bin'}:{os.environ['PATH']}", "CAPTURE_FILE": str(capture)},
        check=True,
        capture_output=True,
        text=True,
    )

    args = capture.read_text(encoding="utf-8").splitlines()
    assert "INSTALL_NATS=false" in args
    assert "INSTALL_POSTGRES=false" in args
    assert "INSTALL_SQLITE=false" in args

    subprocess.run(
        ["bash", "scripts/build_docker_image.sh", "--nats", "--postgres", "--sqlite", "v1"],
        cwd=repo,
        env={**os.environ, "PATH": f"{tmp_path / 'bin'}:{os.environ['PATH']}", "CAPTURE_FILE": str(capture)},
        check=True,
        capture_output=True,
        text=True,
    )

    args = capture.read_text(encoding="utf-8").splitlines()
    assert "INSTALL_NATS=true" in args
    assert "INSTALL_POSTGRES=true" in args
    assert "INSTALL_SQLITE=true" in args


def test_build_docker_image_script_passes_openshell_build_args(tmp_path: Path) -> None:
    repo, capture = _init_tagged_repo(tmp_path)

    subprocess.run(
        ["bash", "scripts/build_docker_image.sh", "--openshell", "--openshell-version", "v0.0.36", "v1"],
        cwd=repo,
        env={**os.environ, "PATH": f"{tmp_path / 'bin'}:{os.environ['PATH']}", "CAPTURE_FILE": str(capture)},
        check=True,
        capture_output=True,
        text=True,
    )

    args = capture.read_text(encoding="utf-8").splitlines()
    assert "INSTALL_OPENSHELL=true" in args
    assert "OPENSHELL_VERSION=v0.0.36" in args


def test_build_docker_image_script_allows_source_retention(tmp_path: Path) -> None:
    repo, capture = _init_tagged_repo(tmp_path)

    subprocess.run(
        ["bash", "scripts/build_docker_image.sh", "--keep-source-code", "v1"],
        cwd=repo,
        env={**os.environ, "PATH": f"{tmp_path / 'bin'}:{os.environ['PATH']}", "CAPTURE_FILE": str(capture)},
        check=True,
        capture_output=True,
        text=True,
    )

    args = capture.read_text(encoding="utf-8").splitlines()
    assert "STRIP_SOURCE_CODE=false" in args

    subprocess.run(
        ["bash", "scripts/build_docker_image.sh", "--strip-source-code", "v1"],
        cwd=repo,
        env={
            **os.environ,
            "PATH": f"{tmp_path / 'bin'}:{os.environ['PATH']}",
            "CAPTURE_FILE": str(capture),
            "STRIP_SOURCE_CODE": "false",
        },
        check=True,
        capture_output=True,
        text=True,
    )

    args = capture.read_text(encoding="utf-8").splitlines()
    assert "STRIP_SOURCE_CODE=true" in args


def test_build_docker_image_script_passes_platform(tmp_path: Path) -> None:
    repo, capture = _init_tagged_repo(tmp_path)

    subprocess.run(
        ["bash", "scripts/build_docker_image.sh", "--platform", "linux/arm64", "v1"],
        cwd=repo,
        env={**os.environ, "PATH": f"{tmp_path / 'bin'}:{os.environ['PATH']}", "CAPTURE_FILE": str(capture)},
        check=True,
        capture_output=True,
        text=True,
    )

    args = capture.read_text(encoding="utf-8").splitlines()
    assert "--platform" in args
    assert "linux/arm64" in args


def test_build_docker_image_script_does_not_force_amd64_for_bitwarden_on_arm_host(tmp_path: Path) -> None:
    repo, capture = _init_tagged_repo(tmp_path)

    subprocess.run(
        ["bash", "scripts/build_docker_image.sh", "--bundle", "--credential-clis", "all", "v1"],
        cwd=repo,
        env={
            **os.environ,
            "PATH": f"{tmp_path / 'bin'}:{os.environ['PATH']}",
            "CAPTURE_FILE": str(capture),
            "FAKE_UNAME_M": "arm64",
        },
        check=True,
        capture_output=True,
        text=True,
    )

    args = capture.read_text(encoding="utf-8").splitlines()
    assert "--platform" not in args


def test_build_docker_image_script_keeps_explicit_platform_on_arm_host(tmp_path: Path) -> None:
    repo, capture = _init_tagged_repo(tmp_path)

    subprocess.run(
        [
            "bash",
            "scripts/build_docker_image.sh",
            "--platform",
            "linux/arm64",
            "--bundle",
            "--credential-clis",
            "all",
            "v1",
        ],
        cwd=repo,
        env={
            **os.environ,
            "PATH": f"{tmp_path / 'bin'}:{os.environ['PATH']}",
            "CAPTURE_FILE": str(capture),
            "FAKE_UNAME_M": "arm64",
        },
        check=True,
        capture_output=True,
        text=True,
    )

    args = capture.read_text(encoding="utf-8").splitlines()
    assert "--platform" in args
    platform_index = args.index("--platform")
    assert args[platform_index + 1] == "linux/arm64"


def test_build_docker_image_script_allows_arm64_bitwarden_artifact_override(tmp_path: Path) -> None:
    repo, capture = _init_tagged_repo(tmp_path)

    subprocess.run(
        ["bash", "scripts/build_docker_image.sh", "--bundle", "--credential-clis", "all", "v1"],
        cwd=repo,
        env={
            **os.environ,
            "PATH": f"{tmp_path / 'bin'}:{os.environ['PATH']}",
            "CAPTURE_FILE": str(capture),
            "FAKE_UNAME_M": "arm64",
            "BW_FILE_URL_ARM64": "https://example.test/bw-linux-arm64.zip",
            "BW_FILE_SHA256_ARM64": "b" * 64,
        },
        check=True,
        capture_output=True,
        text=True,
    )

    args = capture.read_text(encoding="utf-8").splitlines()
    assert "--platform" not in args
    assert "BW_FILE_URL_ARM64=https://example.test/bw-linux-arm64.zip" in args
    assert f"BW_FILE_SHA256_ARM64={'b' * 64}" in args


def test_build_docker_image_script_passes_credential_cli_build_args(tmp_path: Path) -> None:
    repo, capture = _init_tagged_repo(tmp_path)

    subprocess.run(
        ["bash", "scripts/build_docker_image.sh", "--credential-cli", "bw", "--credential-cli", "op", "v1"],
        cwd=repo,
        env={**os.environ, "PATH": f"{tmp_path / 'bin'}:{os.environ['PATH']}", "CAPTURE_FILE": str(capture)},
        check=True,
        capture_output=True,
        text=True,
    )

    args = capture.read_text(encoding="utf-8").splitlines()
    assert "INSTALL_CREDENTIAL_CLIS=bw,op" in args

    subprocess.run(
        ["bash", "scripts/build_docker_image.sh", "--credential-clis", "[keepassxc,vault]", "v1"],
        cwd=repo,
        env={**os.environ, "PATH": f"{tmp_path / 'bin'}:{os.environ['PATH']}", "CAPTURE_FILE": str(capture)},
        check=True,
        capture_output=True,
        text=True,
    )

    args = capture.read_text(encoding="utf-8").splitlines()
    assert "INSTALL_CREDENTIAL_CLIS=[keepassxc,vault]" in args

    subprocess.run(
        ["bash", "scripts/build_docker_image.sh", "--bundle", "--no-credential-clis", "v1"],
        cwd=repo,
        env={**os.environ, "PATH": f"{tmp_path / 'bin'}:{os.environ['PATH']}", "CAPTURE_FILE": str(capture)},
        check=True,
        capture_output=True,
        text=True,
    )

    args = capture.read_text(encoding="utf-8").splitlines()
    assert "INSTALL_CREDENTIAL_CLIS=none" in args


def test_build_docker_image_script_passes_nodejs_build_arg(tmp_path: Path) -> None:
    repo, capture = _init_tagged_repo(tmp_path)

    subprocess.run(
        ["bash", "scripts/build_docker_image.sh", "--nodejs", "v1"],
        cwd=repo,
        env={**os.environ, "PATH": f"{tmp_path / 'bin'}:{os.environ['PATH']}", "CAPTURE_FILE": str(capture)},
        check=True,
        capture_output=True,
        text=True,
    )

    args = capture.read_text(encoding="utf-8").splitlines()
    assert "INSTALL_NODEJS=true" in args

    subprocess.run(
        ["bash", "scripts/build_docker_image.sh", "--no-nodejs", "v1"],
        cwd=repo,
        env={**os.environ, "PATH": f"{tmp_path / 'bin'}:{os.environ['PATH']}", "CAPTURE_FILE": str(capture)},
        check=True,
        capture_output=True,
        text=True,
    )

    args = capture.read_text(encoding="utf-8").splitlines()
    assert "INSTALL_NODEJS=false" in args


def test_build_docker_image_script_accepts_nodejs_env_override(tmp_path: Path) -> None:
    repo, capture = _init_tagged_repo(tmp_path)

    subprocess.run(
        ["bash", "scripts/build_docker_image.sh", "v1"],
        cwd=repo,
        env={
            **os.environ,
            "PATH": f"{tmp_path / 'bin'}:{os.environ['PATH']}",
            "CAPTURE_FILE": str(capture),
            "INSTALL_NODEJS": "true",
        },
        check=True,
        capture_output=True,
        text=True,
    )

    args = capture.read_text(encoding="utf-8").splitlines()
    assert "INSTALL_NODEJS=true" in args


def test_build_docker_image_script_passes_bitwarden_overrides(tmp_path: Path) -> None:
    repo, capture = _init_tagged_repo(tmp_path)

    subprocess.run(
        ["bash", "scripts/build_docker_image.sh", "--bundle", "v1"],
        cwd=repo,
        env={
            **os.environ,
            "PATH": f"{tmp_path / 'bin'}:{os.environ['PATH']}",
            "CAPTURE_FILE": str(capture),
            "BW_VERSION": "2025.10.0",
            "BW_FILE_URL_AMD64": "https://example.test/bw-linux.zip",
            "BW_FILE_URL_ARM64": "https://example.test/bw-linux-arm64.zip",
            "BW_FILE_SHA256_AMD64": "a" * 64,
            "BW_FILE_SHA256_ARM64": "b" * 64,
        },
        check=True,
        capture_output=True,
        text=True,
    )

    args = capture.read_text(encoding="utf-8").splitlines()
    assert "BW_VERSION=2025.10.0" in args
    assert "BW_FILE_URL_AMD64=https://example.test/bw-linux.zip" in args
    assert "BW_FILE_URL_ARM64=https://example.test/bw-linux-arm64.zip" in args
    assert f"BW_FILE_SHA256_AMD64={'a' * 64}" in args
    assert f"BW_FILE_SHA256_ARM64={'b' * 64}" in args


def test_build_docker_image_script_passes_gogcli_overrides(tmp_path: Path) -> None:
    repo, capture = _init_tagged_repo(tmp_path)

    subprocess.run(
        ["bash", "scripts/build_docker_image.sh", "--bundle", "v1"],
        cwd=repo,
        env={
            **os.environ,
            "PATH": f"{tmp_path / 'bin'}:{os.environ['PATH']}",
            "CAPTURE_FILE": str(capture),
            "GOGCLI_VERSION": "0.25.0",
            "GOGCLI_SHA256_AMD64": "c" * 64,
            "GOGCLI_SHA256_ARM64": "d" * 64,
        },
        check=True,
        capture_output=True,
        text=True,
    )

    args = capture.read_text(encoding="utf-8").splitlines()
    assert "GOGCLI_VERSION=0.25.0" in args
    assert f"GOGCLI_SHA256_AMD64={'c' * 64}" in args
    assert f"GOGCLI_SHA256_ARM64={'d' * 64}" in args


def test_build_docker_image_script_passes_database_version_overrides(tmp_path: Path) -> None:
    repo, capture = _init_tagged_repo(tmp_path)

    subprocess.run(
        ["bash", "scripts/build_docker_image.sh", "--bundle", "v1"],
        cwd=repo,
        env={
            **os.environ,
            "PATH": f"{tmp_path / 'bin'}:{os.environ['PATH']}",
            "CAPTURE_FILE": str(capture),
            "POSTGRES_MAJOR": "18",
            "POSTGRES_VERSION": "18.4-1.pgdg13+1",
            "PGVECTOR_PACKAGE_VERSION": "0.8.5-1.pgdg13+1",
            "SQLITE_VERSION": "3.46.1-7+deb13u1",
            "NATS_SERVER_VERSION": "v2.15.1",
        },
        check=True,
        capture_output=True,
        text=True,
    )

    args = capture.read_text(encoding="utf-8").splitlines()
    assert "POSTGRES_MAJOR=18" in args
    assert "POSTGRES_VERSION=18.4-1.pgdg13+1" in args
    assert "PGVECTOR_PACKAGE_VERSION=0.8.5-1.pgdg13+1" in args
    assert "SQLITE_VERSION=3.46.1-7+deb13u1" in args
    assert "NATS_SERVER_VERSION=v2.15.1" in args


def test_build_docker_image_script_passes_sealbox_build_args(tmp_path: Path) -> None:
    repo, capture = _init_tagged_repo(tmp_path)

    subprocess.run(
        ["bash", "scripts/build_docker_image.sh", "v1"],
        cwd=repo,
        env={
            **os.environ,
            "PATH": f"{tmp_path / 'bin'}:{os.environ['PATH']}",
            "CAPTURE_FILE": str(capture),
            "SEALBOX_REPO_URL": "https://example.test/sealbox.git",
            "SEALBOX_GIT_REF": "abc123",
        },
        check=True,
        capture_output=True,
        text=True,
    )

    args = capture.read_text(encoding="utf-8").splitlines()
    assert "SEALBOX_REPO_URL=https://example.test/sealbox.git" in args
    assert "SEALBOX_GIT_REF=abc123" in args


def test_openshell_build_installs_cli_and_gateway_binaries() -> None:
    dockerfile = (Path.cwd() / "Dockerfile").read_text(encoding="utf-8")
    bundle_dockerfile = (Path.cwd() / "Dockerfile.bundle").read_text(encoding="utf-8")

    for content in (dockerfile, bundle_dockerfile):
        assert "openshell --version" in content
        assert "openshell-gateway-checksums-sha256.txt" in content
        assert "install -m 755 \"${gateway_tmp}/openshell-gateway\" /usr/local/bin/openshell-gateway" in content
        assert "openshell-gateway --version" in content


def test_dockerfiles_strip_python_sources_by_default() -> None:
    dockerfile = (Path.cwd() / "Dockerfile").read_text(encoding="utf-8")
    bundle_dockerfile = (Path.cwd() / "Dockerfile.bundle").read_text(encoding="utf-8")
    strip_script = (Path.cwd() / "docker" / "strip_python_sources.sh").read_text(encoding="utf-8")
    entrypoint = (Path.cwd() / "docker" / "entrypoint.sh").read_text(encoding="utf-8")

    for content in (dockerfile, bundle_dockerfile):
        assert "ARG STRIP_SOURCE_CODE=true" in content
        assert "python /app/scripts/build_user_docs.py" in content
        assert "/app/docker/strip_python_sources.sh /app" in content
        assert content.index("python /app/scripts/build_user_docs.py") < content.index(
            "/app/docker/strip_python_sources.sh /app"
        )
        assert "STRIP_SOURCE_CODE must be true or false" in content
    assert "python -m compileall -q -b" in strip_script
    assert "-name '*.py' -o -name '*.pyi'" in strip_script
    assert "exec_python_entrypoint" in entrypoint
    assert 'compiled_path="${script_path}c"' in entrypoint
    assert "--refresh-external-skills" in entrypoint
    assert "--skills-repository URL" in entrypoint


def test_dockerfiles_build_docs_with_build_dependencies_outside_runtime() -> None:
    for filename in ("Dockerfile", "Dockerfile.bundle"):
        content = Path(filename).read_text()
        docs_stage, remaining = content.split("FROM rust:", maxsplit=1)
        runtime = remaining.split("FROM python:", maxsplit=1)[1]

        assert "AS user-docs-builder" in docs_stage
        assert "--require-hashes -r /app/requirements-dev.lock.txt" in docs_stage
        assert "RUN python /app/scripts/build_user_docs.py" in docs_stage
        assert "RUN python /app/scripts/build_user_docs.py" not in runtime
        assert "pip install --require-hashes -r /app/requirements.lock.txt" in runtime
        assert "--require-hashes -r /app/requirements-dev.lock.txt" not in runtime
        copy_site = "COPY --from=user-docs-builder /app/user_docs/site /app/user_docs/site"
        assert copy_site in runtime
        assert runtime.index("COPY . /app") < runtime.index(copy_site)
        assert runtime.index(copy_site) < runtime.index("/app/docker/strip_python_sources.sh /app")


def test_dockerfiles_build_and_validate_rust_runtime_tools() -> None:
    dockerfile = (Path.cwd() / "Dockerfile").read_text(encoding="utf-8")
    bundle_dockerfile = (Path.cwd() / "Dockerfile.bundle").read_text(encoding="utf-8")
    dockerignore = (Path.cwd() / ".dockerignore").read_text(encoding="utf-8")

    for content in (dockerfile, bundle_dockerfile):
        assert "FROM rust:1.95.0-slim-trixie AS rust-tools-builder" in content
        assert "COPY rust/ua-anydoc-cli/Cargo.toml" in content
        assert "COPY rust/vendor /src/rust/vendor" in content
        assert "COPY rust/ua-anydoc-cli/src" in content
        assert "cargo build --locked --release --bin anydoc --bin ua-cedar-authorizer" in content
        assert "target/release/anydoc /usr/local/bin/anydoc" in content
        assert "anydoc --version" in content
        assert "target/release/ua-cedar-authorizer /usr/local/bin/ua-cedar-authorizer" in content
        # The image refuses to build when the shipped access policies do not
        # validate; the old version 1 checks and their scripts are gone.
        assert (
            "RUN ua-cedar-authorizer validate-access \\\n"
            "    --policy /app/ua_definitions/access/cedar/policies.cedar \\\n"
            "    --schema /app/ua_definitions/access/cedar/schema.cedarschema"
        ) in content
        assert "verify_authorization" not in content
        assert "ua_definitions/authorization" not in content
        assert "test -f /app/ua_definitions/access/starter_profiles.yaml" in content
    # Include only release trust metadata; instance data stays out of images.
    assert "data/*" in dockerignore.splitlines()
    assert "!data/\n" not in dockerignore
    assert [line for line in dockerignore.splitlines() if line.startswith("!data/")] == [
        "!data/security/", "!data/security/distrusted_roots.json",
    ]


def test_bundle_dockerfile_uses_configured_bitwarden_file_url() -> None:
    dockerfile = (Path.cwd() / "Dockerfile.bundle").read_text(encoding="utf-8")
    installer = (Path.cwd() / "docker" / "install_credential_clis.sh").read_text(encoding="utf-8")

    assert "ARG BW_VERSION=2025.10.0" in dockerfile
    assert "ARG BW_FILE_URL=" in dockerfile
    assert "ARG BW_FILE_URL_AMD64=" in dockerfile
    assert "ARG BW_FILE_URL_ARM64=" in dockerfile
    assert "ARG BW_FILE_SHA256_AMD64=" in dockerfile
    assert "ARG BW_FILE_SHA256_ARM64=" in dockerfile
    assert "default_bw_version=\"2025.10.0\"" in installer
    assert "apt_prereqs+=(nodejs npm)" not in installer
    assert "bw-linux-${bw_version}.zip" in installer
    assert "bw-linux-arm64-${bw_version}.zip" in installer
    assert "Rebuild with --nodejs or --build-arg INSTALL_NODEJS=true" in installer
    assert 'npm install -g --omit=dev --no-audit --no-fund "@bitwarden/cli@${bw_version}"' in installer
    assert "sha256sum -c -" in installer
    assert "0544c64d3e9932bb5f2a70e819695ea78186a44ac87a0b1d753e9c55217041d9" in installer
    assert "BW_FILE_URL or ${bw_arch_specific_url_var} is required when INSTALL_CREDENTIAL_CLIS includes bw" in installer
    assert '"${bw_file_url}" -o /tmp/bw.zip' in installer
    assert "bitwarden.com/download" not in installer
    assert "snap install bw" not in installer


def test_dockerfiles_install_arch_specific_yq() -> None:
    dockerfile = (Path.cwd() / "Dockerfile").read_text(encoding="utf-8")
    bundle_dockerfile = (Path.cwd() / "Dockerfile.bundle").read_text(encoding="utf-8")

    for content in (dockerfile, bundle_dockerfile):
        assert 'amd64) yq_arch="amd64"' in content
        assert 'arm64) yq_arch="arm64"' in content
        assert "yq_linux_${yq_arch}" in content
        assert "yq --version" in content
        assert "yq_linux_amd64\" -o /usr/local/bin/yq" not in content


def test_bundle_dockerfile_installs_gogcli_without_homebrew() -> None:
    bundle_dockerfile = (Path.cwd() / "Dockerfile.bundle").read_text(encoding="utf-8")

    assert "ARG GOGCLI_VERSION=0.41.0" in bundle_dockerfile
    assert "ARG GOGCLI_SHA256_AMD64=bfdb7e67c904098a34a54eb2ae2b1874c41eda7defd7182e5f2c89a2c2ba44c3" in bundle_dockerfile
    assert "ARG GOGCLI_SHA256_ARM64=a226c1f439f16e946d8d5b73b49da8e65875978adb65d6bc23e7e225e799e966" in bundle_dockerfile
    # 0.41 archives store the binary as ./gog.
    assert "--no-same-permissions ./gog;" in bundle_dockerfile
    assert "gogcli_${GOGCLI_VERSION}_linux_${gogcli_arch}.tar.gz" in bundle_dockerfile
    assert "github.com/openclaw/gogcli/releases/download/v${GOGCLI_VERSION}" in bundle_dockerfile
    assert 'install -m 755 "${gogcli_tmp}/gog" /usr/local/bin/gog' in bundle_dockerfile
    assert "gog --version" in bundle_dockerfile
    assert "Homebrew/install" not in bundle_dockerfile
    assert "brew install gogcli" not in bundle_dockerfile
    assert "linuxbrew" not in bundle_dockerfile


def test_dockerfiles_use_selectable_credential_cli_installer() -> None:
    dockerfile = (Path.cwd() / "Dockerfile").read_text(encoding="utf-8")
    bundle_dockerfile = (Path.cwd() / "Dockerfile.bundle").read_text(encoding="utf-8")
    installer = (Path.cwd() / "docker" / "install_credential_clis.sh").read_text(encoding="utf-8")

    assert "ARG INSTALL_CREDENTIAL_CLIS=sealbox" in dockerfile
    assert "ARG INSTALL_CREDENTIAL_CLIS=sealbox" in bundle_dockerfile
    assert "ARG SEALBOX_REPO_URL=https://github.com/enzimo/sealbox.git" in dockerfile
    for content in (dockerfile, bundle_dockerfile):
        assert "ARG SEALBOX_GIT_REF=main" in content
        assert 'git init "${sealbox_tmp}"' in content
        assert 'git -C "${sealbox_tmp}" remote add origin "${SEALBOX_REPO_URL}"' in content
        assert 'git -C "${sealbox_tmp}" fetch --depth 1 origin "${SEALBOX_GIT_REF}"' in content
        assert 'git -C "${sealbox_tmp}" checkout --detach FETCH_HEAD' in content
        assert 'echo "Building Sealbox ref $(git -C "${sealbox_tmp}" rev-parse --short HEAD)"' in content
        assert "sealbox-cli credential delete --help >/dev/null" in content
        assert (
            'sealbox-cli secret delete --help | grep -F "If omitted, all versions are deleted" >/dev/null'
            in content
        )
    for content in (dockerfile, bundle_dockerfile):
        assert "COPY docker/install_credential_clis.sh /usr/local/bin/install_credential_clis.sh" in content
        assert "/usr/local/bin/install_credential_clis.sh" in content
        assert "cargo build --manifest-path" in content
        assert "-p sealbox-cli -p sealbox-server" in content
        assert 'install -m 755 "${sealbox_tmp}/target/release/sealbox-cli" /usr/local/bin/sealbox-cli' in content
        assert 'install -m 755 "${sealbox_tmp}/target/release/sealbox-server" /usr/local/bin/sealbox-server' in content
    for token in ("bw|bitwarden", "op|1password|onepassword", "keepass|keepassxc|keepassxc-cli", "vault|hashicorp-vault", "sealbox|sealbox-cli"):
        assert token in installer
    assert "1password-cli" in installer
    assert "keepassxc" in installer
    assert "apt.releases.hashicorp.com" in installer
    assert 'setcap -r "$(command -v vault)"' in installer
    assert "Sealbox CLI/server selected" in installer


def test_dockerfiles_install_checksum_pinned_node_24_optionally() -> None:
    dockerfile = (Path.cwd() / "Dockerfile").read_text(encoding="utf-8")
    bundle_dockerfile = (Path.cwd() / "Dockerfile.bundle").read_text(encoding="utf-8")

    for content in (dockerfile, bundle_dockerfile):
        assert "ARG INSTALL_NODEJS=false" in content
        assert "ARG NODEJS_VERSION=24.20.0" in content
        assert (
            "ARG NODEJS_SHA256_AMD64="
            "855d581f8a4eb1a8117e3426de25fe02770592febcfb31369aee1ffbfee9e8ec"
        ) in content
        assert (
            "ARG NODEJS_SHA256_ARM64="
            "3515603e2487879a39bc75716f1a2affd027500c64ba50e845cf72cb33219013"
        ) in content
        assert "INSTALL_NODEJS must be true or false" in content
        assert 'if [ "${INSTALL_NODEJS}" = "true" ]' in content
        assert 'amd64) node_arch="x64"' in content
        assert 'arm64) node_arch="arm64"' in content
        assert "node-v${NODEJS_VERSION}-linux-${node_arch}.tar.gz" in content
        assert "https://nodejs.org/dist/v${NODEJS_VERSION}/${node_archive}" in content
        assert 'echo "${node_sha256}  /tmp/${node_archive}" | sha256sum -c -' in content
        assert 'test "$(node --version)" = "v${NODEJS_VERSION}"' in content
        assert 'packages="${packages} nodejs npm"' not in content
        assert "node --version" in content
        assert "npm --version" in content


def test_dockerfiles_cache_photon_dependencies_before_the_source_copy() -> None:
    dockerignore = (Path.cwd() / ".dockerignore").read_text(encoding="utf-8")
    dockerfile = (Path.cwd() / "Dockerfile").read_text(encoding="utf-8")
    bundle_dockerfile = (Path.cwd() / "Dockerfile.bundle").read_text(encoding="utf-8")
    manifest_copy = (
        "COPY assistant_gateway/interactive_apps/photon_sidecar/package.json "
        "assistant_gateway/interactive_apps/photon_sidecar/package-lock.json "
        "/app/assistant_gateway/interactive_apps/photon_sidecar/"
    )

    assert "**/node_modules" in set(dockerignore.splitlines())
    for content in (dockerfile, bundle_dockerfile):
        assert content.index(manifest_copy) < content.index("npm ci --omit=dev")
        assert content.index("npm ci --omit=dev") < content.index("COPY . /app")
        assert "npm ci --omit=dev --no-audit --no-fund" in content


@pytest.mark.parametrize("dockerfile", ["Dockerfile", "Dockerfile.bundle"])
@pytest.mark.parametrize("install_sqlite,version", [
    ("true", None), ("true", "3.46.1-7+deb13u2"),
    ("false", None), ("true", "unavailable"),
])
def test_docker_sqlite_install_uses_repository_candidate_or_explicit_pin(
    tmp_path: Path, dockerfile: str, install_sqlite: str, version: str | None,
) -> None:
    """Execute the real apt RUN block with an isolated package-manager recorder."""
    content = Path(dockerfile).read_text()
    default_version = next(
        line.removeprefix("ARG SQLITE_VERSION=")
        for line in content.splitlines() if line.startswith("ARG SQLITE_VERSION=")
    )
    run = "set -eux; " + content.split("RUN set -eux; \\\n", 1)[1].split("\n\n", 1)[0]
    capture = tmp_path / "apt.jsonl"
    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    apt = fake_bin / "apt-get"
    apt.write_text(
        f"#!{sys.executable}\n"
        "import json, os, sys\n"
        "with open(os.environ['APT_CAPTURE'], 'a') as stream:\n"
        "    stream.write(json.dumps(sys.argv[1:]) + '\\n')\n"
        "if 'sqlite3=unavailable' in sys.argv:\n"
        "    sys.exit(100)\n"
    )
    apt.chmod(0o755)
    cleanup = fake_bin / "rm"
    cleanup.write_text("#!/bin/sh\nexit 0\n")
    cleanup.chmod(0o755)
    # This harness exercises package selection, never the host trust store.
    # test_system_certificate_store runs the real helper in isolated folders.
    python = fake_bin / "python"
    python.write_text(
        '#!/bin/sh\n[ "$#" -eq 1 ] && [ "$1" = /app/docker/distrust_compromised_roots.py ]\n'
    )
    python.chmod(0o755)
    result = subprocess.run(
        ["sh", "-c", run], text=True, capture_output=True,
        env={**os.environ, "PATH": f"{fake_bin}:{os.environ['PATH']}",
             "APT_CAPTURE": str(capture), "INSTALL_SQLITE": install_sqlite,
             "SQLITE_VERSION": default_version if version is None else version,
             "INSTALL_POSTGRES": "false", "INSTALL_NODEJS": "false"},
    )
    assert result.returncode == (100 if version == "unavailable" else 0), result.stderr
    calls = [json.loads(line) for line in capture.read_text().splitlines()]
    packages = [arg for arg in calls[-1] if arg.startswith(("sqlite3", "libsqlite3"))]
    suffix = f"={version}" if version else ""
    assert packages == (
        [name + suffix for name in ("sqlite3", "libsqlite3-0", "libsqlite3-dev")]
        if install_sqlite == "true" else []
    )
    assert len(calls) == 3  # A failed explicit pin must not retry without it.


def test_dockerfiles_make_database_packages_optional() -> None:
    dockerfile = (Path.cwd() / "Dockerfile").read_text(encoding="utf-8")
    bundle_dockerfile = (Path.cwd() / "Dockerfile.bundle").read_text(encoding="utf-8")

    assert "ARG INSTALL_POSTGRES=false" in dockerfile
    assert "ARG INSTALL_SQLITE=false" in dockerfile
    assert "ARG POSTGRES_MAJOR=18" in dockerfile
    assert "ARG POSTGRES_VERSION=18.4-1.pgdg13+1" in dockerfile
    assert "ARG PGVECTOR_PACKAGE_VERSION=0.8.5-1.pgdg13+1" in dockerfile
    assert "ARG INSTALL_POSTGRES=true" in bundle_dockerfile
    assert "ARG INSTALL_SQLITE=true" in bundle_dockerfile
    for content in (dockerfile, bundle_dockerfile):
        assert "ARG SQLITE_VERSION=\n" in content
        assert 'if [ "${INSTALL_SQLITE}" = "true" ]' in content
        assert 'if [ "${INSTALL_POSTGRES}" = "true" ]' in content
        assert "https://apt.postgresql.org/pub/repos/apt trixie-pgdg main" in content
        assert "postgresql-${POSTGRES_MAJOR}=${POSTGRES_VERSION}" in content
        assert "postgresql-client-${POSTGRES_MAJOR}=${POSTGRES_VERSION}" in content
        assert "postgresql-server-dev-${POSTGRES_MAJOR}=${POSTGRES_VERSION}" in content
        assert "postgresql-${POSTGRES_MAJOR}-pgvector=${PGVECTOR_PACKAGE_VERSION}" in content
        assert 'test -f "/usr/share/postgresql/${POSTGRES_MAJOR}/extension/vector.control"' in content
        assert "Skipping pgvector install because INSTALL_POSTGRES=" in content
        assert "sqlite_vec.load(conn)" in content
        assert "Skipping sqlite-vec smoke check because INSTALL_SQLITE=" in content


def test_dockerfiles_make_nats_server_optional() -> None:
    dockerfile = (Path.cwd() / "Dockerfile").read_text(encoding="utf-8")
    bundle_dockerfile = (Path.cwd() / "Dockerfile.bundle").read_text(encoding="utf-8")

    assert "ARG INSTALL_NATS=true" in dockerfile
    assert "ARG INSTALL_NATS=true" in bundle_dockerfile
    assert "ARG NATS_SERVER_VERSION=v2.15.1" in dockerfile
    assert "ARG NATS_SERVER_VERSION=v2.15.1" in bundle_dockerfile
    assert "ENV TEAM_BUNDLE_INSTALL_NATS=${INSTALL_NATS}" in bundle_dockerfile
    for content in (dockerfile, bundle_dockerfile):
        assert 'if [ "${INSTALL_NATS}" = "true" ]' in content
        assert "nats-server-${NATS_SERVER_VERSION}-linux-${nats_arch}.tar.gz" in content
        assert "install -m 755" in content
        assert "nats-server --version" in content
        assert "Skipping NATS server install because INSTALL_NATS=" in content


def test_messaging_client_python_packages_are_not_optional() -> None:
    requirements = (Path.cwd() / "requirements.txt").read_text(encoding="utf-8")
    dockerfile = (Path.cwd() / "Dockerfile").read_text(encoding="utf-8")
    bundle_dockerfile = (Path.cwd() / "Dockerfile.bundle").read_text(encoding="utf-8")

    assert "aio-pika" not in requirements
    assert "nats-py" in requirements
    assert "PyNaCl>=1.6.2" in requirements
    for content in (dockerfile, bundle_dockerfile):
        assert "COPY requirements.lock.txt /app/requirements.lock.txt" in content
        assert "pip install --require-hashes -r /app/requirements.lock.txt" in content
        assert "nats" in content


def test_mcp_image_uses_the_repository_dependency_lock() -> None:
    dockerfile = (Path.cwd() / "mcp" / "Dockerfile").read_text(encoding="utf-8")

    assert "COPY requirements.txt requirements.lock.txt ./" in dockerfile
    assert "pip install --no-cache-dir --require-hashes -r requirements.lock.txt" in dockerfile
    assert "COPY config_loader.py default_values.py id_generation.py ./" in dockerfile
    assert "COPY storage/__init__.py storage/backends.py storage/" in dockerfile
    assert "COPY custom_agents/skill_templates/ .skills/" in dockerfile
    assert "COPY .skills/ .skills/" not in dockerfile
    assert 'RUN python -c "import skills_mcp"' in dockerfile


def test_dependency_audit_workflow_checks_locked_graph() -> None:
    workflow = (Path.cwd() / ".github" / "workflows" / "dependency-audit.yml").read_text(encoding="utf-8")

    assert "python -m pip_audit -r requirements.txt --progress-spinner off" in workflow
    assert "python -m pip_audit -r requirements.lock.txt --no-deps --progress-spinner off" in workflow
    assert "python -m pip install --require-hashes -r requirements-dev.lock.txt" in workflow
    assert "python -m pip_audit -r requirements-dev.lock.txt --no-deps --progress-spinner off" in workflow
    assert "python -m pip_audit --local --progress-spinner off" in workflow


def test_dockerfiles_include_media_download_dependencies() -> None:
    dockerfile = (Path.cwd() / "Dockerfile").read_text(encoding="utf-8")
    bundle_dockerfile = (Path.cwd() / "Dockerfile.bundle").read_text(encoding="utf-8")

    for content in (dockerfile, bundle_dockerfile):
        assert "ffmpeg" in content
        assert "yt-dlp" in content


def test_dockerfiles_include_operator_cli_and_network_tools() -> None:
    dockerfile = (Path.cwd() / "Dockerfile").read_text(encoding="utf-8")
    bundle_dockerfile = (Path.cwd() / "Dockerfile.bundle").read_text(encoding="utf-8")

    for content in (dockerfile, bundle_dockerfile):
        for package in (
            "curl",
            "dnsutils",
            "iputils-ping",
            "jq",
            "nano",
            "sed",
            "wget",
        ):
            assert package in content
        assert "yq_linux_${yq_arch}" in content


def test_docker_build_context_keeps_static_definition_catalogs() -> None:
    dockerignore = (Path.cwd() / ".dockerignore").read_text(encoding="utf-8")
    dockerfile = (Path.cwd() / "Dockerfile").read_text(encoding="utf-8")
    bundle_dockerfile = (Path.cwd() / "Dockerfile.bundle").read_text(encoding="utf-8")

    assert "data/*" in set(dockerignore.splitlines())
    assert "ua_definitions" not in dockerignore
    for content in (dockerfile, bundle_dockerfile):
        assert "test -f /app/ua_definitions/agent_definitions/builtins.yaml" in content
        assert "test -f /app/ua_definitions/agent_bundles/practical_assistants/agent.yaml" in content
        assert "test -f /app/ua_definitions/agent_bundles/code_implementation/agent.yaml" in content
        assert "test -f /app/ua_definitions/credential_templates/builtins.yaml" in content
