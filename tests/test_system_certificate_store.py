from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
import shutil
import ssl
import subprocess

import pytest

from docker.distrust_compromised_roots import certificate_fingerprints, distrust_roots

FIXTURES = Path(__file__).parent / "fixtures/tls"
BLOCKED = FIXTURES / "distrusted-emsign-g3.crt"
RETAINED = FIXTURES / "retained-emsign-g1.crt"


@dataclass
class CertificateStore:
    config: Path
    certificates: Path
    local_certificates: Path
    output: Path
    hooks: Path

    def arguments(self) -> dict[str, Path]:
        return vars(self)

    def rebuild(self) -> None:
        subprocess.run(
            [
                "update-ca-certificates", "--fresh", "--certsconf", str(self.config),
                "--certsdir", str(self.certificates), "--localcertsdir", str(self.local_certificates),
                "--etccertsdir", str(self.output), "--hooksdir", str(self.hooks),
            ],
            check=True, capture_output=True, text=True,
        )


@pytest.fixture
def store(tmp_path: Path) -> CertificateStore:
    if shutil.which("update-ca-certificates") is None or shutil.which("openssl") is None:
        pytest.skip("Debian ca-certificates and openssl are required")
    result = CertificateStore(
        tmp_path / "ca-certificates.conf", tmp_path / "certificates",
        tmp_path / "local", tmp_path / "output", tmp_path / "hooks",
    )
    for path in (result.certificates, result.local_certificates, result.output, result.hooks):
        path.mkdir()
    # The filename deliberately differs from Debian's: removal follows bytes.
    shutil.copyfile(BLOCKED, result.certificates / "renamed.crt")
    shutil.copyfile(RETAINED, result.certificates / "retained.crt")
    result.config.write_text("# preserve this comment\nrenamed.crt\nretained.crt\n")
    return result


@pytest.mark.parametrize("legacy_wrapper", [False, True])
def test_system_store_removes_only_distrusted_root_and_rebuilds_hashes(
    store: CertificateStore, legacy_wrapper: bool,
) -> None:
    if legacy_wrapper:
        path = store.certificates / "renamed.crt"
        path.write_text(path.read_text().replace("CERTIFICATE", "X509 CERTIFICATE"))
    store.rebuild()
    bundle = store.output / "ca-certificates.crt"
    blocked = certificate_fingerprints(BLOCKED)
    retained = certificate_fingerprints(RETAINED)
    assert blocked | retained == certificate_fingerprints(bundle)

    distrust_roots(**store.arguments())
    assert store.config.read_text() == "# preserve this comment\n!renamed.crt\nretained.crt\n"
    assert certificate_fingerprints(bundle) == retained
    context = ssl.create_default_context(cafile=str(bundle), capath=str(store.output))
    assert len(context.get_ca_certs()) == 1
    # Exercise the actual OpenSSL hash directory with the bundle disabled.
    for certificate, expected in ((BLOCKED, False), (RETAINED, True)):
        check = subprocess.run(
            ["openssl", "verify", "-no-CAfile", "-no-CAstore", "-CApath", str(store.output), str(certificate)],
            capture_output=True, text=True,
        )
        assert (check.returncode == 0) is expected, check.stderr
    distrust_roots(**store.arguments())
    assert certificate_fingerprints(bundle) == retained


@pytest.mark.parametrize("trusted_wrapper", [False, True])
def test_system_store_preserves_local_ca_and_refuses_distrusted_local_copy(
    store: CertificateStore, trusted_wrapper: bool,
) -> None:
    shutil.copyfile(RETAINED, store.local_certificates / "private.crt")
    distrust_roots(**store.arguments())
    assert (store.output / "private.pem").is_file()
    original = store.config.read_text()
    copy = store.local_certificates / "copied.crt"
    if trusted_wrapper:
        subprocess.run(
            ["openssl", "x509", "-in", str(BLOCKED), "-trustout", "-addtrust", "serverAuth", "-out", str(copy)],
            check=True,
        )
        assert certificate_fingerprints(copy) == certificate_fingerprints(BLOCKED)
    else:
        shutil.copyfile(BLOCKED, copy)
    with pytest.raises(RuntimeError, match="Remove the distrusted local certificate first"):
        distrust_roots(**store.arguments())
    assert store.config.read_text() == original


@pytest.mark.parametrize("dockerfile", ["Dockerfile", "Dockerfile.bundle", "mcp/Dockerfile"])
def test_each_shipped_image_stage_corrects_system_trust(dockerfile: str) -> None:
    root = Path(__file__).resolve().parents[1]
    for stage in (root / dockerfile).read_text().split("FROM ")[1:]:
        assert "COPY data/security/distrusted_roots.json " in stage
        assert "COPY docker/distrust_compromised_roots.py " in stage
        assert "python /app/docker/distrust_compromised_roots.py" in stage or (
            "python3 /src/docker/distrust_compromised_roots.py" in stage
        )
    ignore = (root / ".dockerignore").read_text()
    assert "!data/security/\ndata/security/*\n!data/security/distrusted_roots.json" in ignore


@pytest.mark.parametrize("filename", ["manual.crt", "manual.cer", "14bc7599.0"])
def test_system_store_refuses_distrusted_manual_ca_directory_entries(
    store: CertificateStore, filename: str,
) -> None:
    shutil.copyfile(BLOCKED, store.output / filename)
    with pytest.raises(RuntimeError, match="Distrusted root remains in system certificate store"):
        distrust_roots(**store.arguments())
