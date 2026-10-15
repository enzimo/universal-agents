"""Startup refuses saved credential-binding data and prints steps that fix it."""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

from runtime_settings.access_cutover import obsolete_binding_data, obsolete_binding_message
from runtime_settings.instance_layout import InstanceLayout


def old_instance(root: Path) -> InstanceLayout:
    layout = InstanceLayout(root)
    records = root / "runtime" / "mcp-connections" / "records"
    records.mkdir(parents=True)
    (records / "mcp_conn_crawl4ai_1.json").write_text(
        json.dumps({"descriptor": {"connection_name": "crawl4ai", "credential_binding_ids": []}}), encoding="utf-8"
    )
    return layout


def run_steps(message: str) -> None:
    """Run the printed commands exactly as an operator would paste them."""

    steps = message.split("then start it again:", 1)[1]
    script = "\n".join(line for line in steps.splitlines() if line and not line[0].isdigit())
    subprocess.run(["bash", "-e", "-c", script.replace("python3 -", f"{sys.executable} -")], check=True)


def test_old_binding_data_is_found_and_the_printed_steps_fix_it(tmp_path: Path) -> None:
    layout = old_instance(tmp_path / "instance")

    found = obsolete_binding_data(layout.root)
    assert found.present and found.mcp_registry is not None
    message = obsolete_binding_message(found)
    assert "obsolete_credential_binding_data_present" in message and "/link mcp:<name>" in message

    run_steps(message)

    assert not obsolete_binding_data(layout.root).present
    assert (layout.root / "runtime" / "mcp-connections.before-account-links" / "records").is_dir()


def test_a_clean_instance_passes(tmp_path: Path) -> None:
    layout = InstanceLayout(tmp_path / "instance")
    layout.control_dir.mkdir(parents=True)

    assert not obsolete_binding_data(layout.root).present
