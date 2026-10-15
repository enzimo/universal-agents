"""Exercise the shared appearance controller without a browser dependency."""

import shutil
import subprocess
from pathlib import Path

import pytest


def test_shared_appearance_only_announces_effective_changes():
    node = shutil.which("node")
    if node is None:
        pytest.skip("node is required to execute the shared appearance controller")
    asset = Path(__file__).resolve().parents[1] / "user_docs_source/assets/appearance.js"
    subprocess.run(
        [node, str(Path(__file__).with_name("shared_appearance_probe.cjs")), str(asset)],
        text=True,
        capture_output=True,
        check=True,
        timeout=15,
    )


def test_runtime_tabs_preserve_content_and_request_ownership():
    node = shutil.which("node")
    if node is None:
        pytest.skip("node is required to execute Runtime Control views")
    subprocess.run(
        [node, "--test", str(Path(__file__).with_name("runtime_views.test.mjs"))],
        text=True,
        capture_output=True,
        check=True,
        timeout=15,
    )


@pytest.mark.parametrize("filename", ["index.html", "chat.html", "config_editor.html"])
def test_pages_apply_the_theme_before_rendering_the_body(filename):
    asset = Path(__file__).resolve().parents[1] / "assistant_gateway/onboarding_web/assets" / filename
    html = asset.read_text(encoding="utf-8")
    assert html.count('<script src="/appearance.js"></script>') == 1
    assert html.index('/styles.css') < html.index('/appearance.js') < html.index('<body')


def test_share_settings_text_loads_as_the_appearance_section_of_a_config_file(tmp_path):
    """The text Copy produces works where its header says to put it.

    The owner pasted it under ``appearance:`` and startup failed on uneven
    indentation (2026-10-04); this pastes it the documented way, every line
    moved right together, and loads the file like startup does.
    """

    from assistant_gateway.config import GatewayInstanceConfig, InstanceConfigFileError
    from assistant_gateway.config import inspect_instance_config_version
    from runtime_settings.appearance import OnboardingAppearanceSettings
    import yaml

    node = shutil.which("node")
    if node is None:
        pytest.skip("node is required to run the shared appearance code")
    asset = Path(__file__).resolve().parents[1] / "assistant_gateway/onboarding_web/assets/appearance.js"
    text = subprocess.run(
        [node, str(Path(__file__).with_name("appearance_settings_text_probe.cjs")), str(asset)],
        text=True,
        capture_output=True,
        check=True,
        timeout=15,
    ).stdout
    indented = "\n".join(("      " + line) if line else line for line in text.splitlines())
    config_text = f"schema_version: 1\nui:\n  onboarding:\n    appearance:\n{indented}\n"

    appearance = yaml.safe_load(config_text)["ui"]["onboarding"]["appearance"]
    loaded = OnboardingAppearanceSettings.model_validate(appearance)
    assert loaded.dark.light_x == -40
    assert loaded.dark.accents.bronze.share == 39

    # The same block with one accent indented unevenly, as in the report,
    # is refused with its line and a hint instead of a parser traceback.
    broken = config_text.replace("        gold:", "          gold:", 1)
    broken = broken.replace('{color: "', '\n            color: "', 1)
    path = tmp_path / "instance_config.yaml"
    path.write_text(broken, encoding="utf-8")
    with pytest.raises(InstanceConfigFileError, match=r"not valid YAML at line \d+, column \d+.*indentation"):
        inspect_instance_config_version(path)
    with pytest.raises(InstanceConfigFileError):
        GatewayInstanceConfig.load(path)
