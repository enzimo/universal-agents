from pathlib import Path
import subprocess


def test_offline_setup_guide_commands_and_input_boundaries() -> None:
    root = Path(__file__).resolve().parents[1]
    subprocess.run(["rtk", "proxy", "node", "--test", "tests/setup_guide.test.cjs"],
                   cwd=root, check=True, capture_output=True, text=True)
