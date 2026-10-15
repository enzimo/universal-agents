from __future__ import annotations

import re
from pathlib import Path

import yaml

from config_loader import CONFIG_SCHEMA_VERSION


PROJECT_ROOT = Path(__file__).resolve().parents[1]
EXCLUDED_DIR_NAMES = {
    ".git",
    ".mypy_cache",
    ".pytest_cache",
    ".ruff_cache",
    ".venv",
    "__pycache__",
    "docs",
    "plans",
    "tests",
    "user_docs_source",
}


def _runtime_python_files() -> list[Path]:
    files: list[Path] = []
    for path in PROJECT_ROOT.rglob("*.py"):
        if any(part in EXCLUDED_DIR_NAMES for part in path.relative_to(PROJECT_ROOT).parts):
            continue
        if path.is_file():
            files.append(path)
    return sorted(files)


def test_runtime_code_does_not_read_removed_public_url_env_vars() -> None:
    removed_env_read = re.compile(
        r"(?:os\.getenv|os\.environ\.get|env\.get|non_empty_env)\("
        r"[\"'](?:USER_DOCS_|WEBSITE_APP_|SECURE_TOOL_BROKER_PUBLIC_BASE_URL)"
    )
    offenders: list[str] = []
    for path in _runtime_python_files():
        for line_number, line in enumerate(path.read_text(encoding="utf-8", errors="replace").splitlines(), start=1):
            if removed_env_read.search(line):
                offenders.append(f"{path.relative_to(PROJECT_ROOT)}:{line_number}: {line.strip()}")

    assert offenders == []


def test_runtime_code_does_not_use_removed_config_object_paths() -> None:
    removed_path_patterns = (
        re.compile(r"(?<!assistant_gateway)\.interactive_apps\.website\b"),
        re.compile(r"\.user_docs\b"),
        re.compile(r"\.security_layer\.broker_public_base_url\b"),
    )
    offenders: list[str] = []
    for path in _runtime_python_files():
        for line_number, line in enumerate(path.read_text(encoding="utf-8", errors="replace").splitlines(), start=1):
            if any(pattern.search(line) for pattern in removed_path_patterns):
                offenders.append(f"{path.relative_to(PROJECT_ROOT)}:{line_number}: {line.strip()}")

    assert offenders == []


def test_current_config_example_has_only_new_public_url_shape() -> None:
    config = yaml.safe_load((PROJECT_ROOT / "config.example.yaml").read_text(encoding="utf-8"))

    assert config["schema_version"] == CONFIG_SCHEMA_VERSION
    assert config["agent"]["memory"]["plugin_manager"]["enabled"] is True
    assert config["network"]["public_origin"] == "http://127.0.0.1"
    assert "user_docs" not in config
    assert "website" not in config["interactive_apps"]
    assert "internal_website" not in config["interactive_apps"]
    website = config["interactive_apps"]["plugins"][
        "builtin.internal_website"
    ]["settings"]
    assert "broker_public_base_url" not in config["security_layer"]
    assert config["security_layer"]["broker_public_port"] == 18600
    assert website["public_port"] == 18090
