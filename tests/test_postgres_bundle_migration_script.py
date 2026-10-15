from __future__ import annotations

from pathlib import Path
import subprocess


SCRIPT = Path("scripts/migrate_bundled_postgres_15_to_18.sh")


def test_migration_script_has_valid_shell_syntax() -> None:
    subprocess.run(["bash", "-n", str(SCRIPT)], check=True)


def test_migration_script_documents_export_import_modes() -> None:
    content = SCRIPT.read_text(encoding="utf-8")

    assert "export --data-dir <dir> --old-image <image>" in content
    assert "import --data-dir <dir> --new-image <image>" in content
    assert "migrate --data-dir <dir> --old-image <image> --new-image <image>" in content
    assert "--env-file <team.env>" in content
    assert "Expected source PostgreSQL major 15" in content
    assert "docker_env_args+=(--env-file" in content
    assert "POSTGRES_MIGRATION_DSN" in content
    assert "POSTGRES_SUPERUSER_DSN" in content
    assert 'No bundled Postgres data directory found at ${data}' in content
    assert 'backup_container_path="$(container_path_for_dump "${backup_dir}")"' in content
    assert 'grep -E "PostgreSQL 18\\\\."' in content
