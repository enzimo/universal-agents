from __future__ import annotations

from pathlib import Path
import shutil
import subprocess
import sys

from scripts.build_user_docs import prepare_github_pages_artifact


def test_docker_docs_stage_has_complete_build_inputs(tmp_path: Path) -> None:
    """Build from the declared Docker inputs without borrowing repository files."""
    root = Path(__file__).resolve().parents[1]
    stages = [
        (root / name).read_text().split("FROM rust:", 1)[0]
        for name in ("Dockerfile", "Dockerfile.bundle")
    ]
    assert stages[0] == stages[1]
    for line in stages[0].splitlines():
        if not line.startswith("COPY "):
            continue
        _, source, destination = line.split()
        target = tmp_path / Path(destination).relative_to("/app")
        target.parent.mkdir(parents=True, exist_ok=True)
        if (root / source).is_dir():
            shutil.copytree(root / source, target)
        else:
            shutil.copy2(root / source, target)
    subprocess.run(
        [sys.executable, str(tmp_path / "scripts/build_user_docs.py")],
        cwd=tmp_path,
        check=True,
        capture_output=True,
        text=True,
    )
    assert (tmp_path / "user_docs/site/index.html").is_file()
    assert (tmp_path / "user_docs/site/search/search_index.json").is_file()
    # The dark background renderer has one source; the build copies it in.
    renderer = "assistant_gateway/onboarding_web/assets/metallic_mesh.js"
    assert (tmp_path / "user_docs/site/assets/metallic_mesh.js").read_bytes() == (root / renderer).read_bytes()


def test_prepare_github_pages_artifact_rewrites_repository_level_docs(
    tmp_path: Path,
) -> None:
    site_dir = tmp_path / "site"
    onboarding_output = site_dir / "onboarding" / "index.html"
    onboarding_output.parent.mkdir(parents=True)
    onboarding_output.write_text(
        '<a href="/docker-compose-setup.html">setup</a>',
        encoding="utf-8",
    )
    nested = site_dir / "guides" / "quickstart" / "index.html"
    nested.parent.mkdir(parents=True)
    nested.write_text('<a href="/docker-compose-setup.html">setup</a>')
    compose_source = tmp_path / "docker-compose-setup.html"
    compose_source.write_text(
        """
        <a href="/" aria-label="Universal Agents home">
        """,
        encoding="utf-8",
    )

    prepare_github_pages_artifact(
        site_dir,
        compose_setup_source=compose_source,
    )

    setup_output = (site_dir / "docker-compose-setup.html").read_text(encoding="utf-8")
    onboarding_content = onboarding_output.read_text(encoding="utf-8")
    assert 'href="./" aria-label="Universal Agents home"' in setup_output
    assert 'href="../docker-compose-setup.html"' in onboarding_content
    assert 'href="../../docker-compose-setup.html"' in nested.read_text()
    assert (site_dir / ".nojekyll").is_file()
