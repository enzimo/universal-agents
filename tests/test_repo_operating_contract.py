from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[1]


def test_repo_contract_requires_rtk_for_every_shell_command() -> None:
    agents_contract = (PROJECT_ROOT / "AGENTS.md").read_text(encoding="utf-8")

    assert "Route every shell command through RTK" in agents_contract
    assert "Use `rtk proxy" in agents_contract
    assert "Do not add an RTK prefix" in agents_contract
    assert "@RTK.md" not in agents_contract
    assert not (PROJECT_ROOT / "RTK.md").exists()
