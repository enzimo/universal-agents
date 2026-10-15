from pathlib import Path

from assistant_gateway.onboarding_reconciliation import (
    OnboardingReport,
    onboarding_report_detail_path,
    read_yaml_mapping,
    write_onboarding_report,
)


def test_report_path_and_writer_omit_secret_values(tmp_path: Path) -> None:
    config_path = tmp_path / "instance.yaml"
    detail_path = onboarding_report_detail_path(str(tmp_path))
    report = OnboardingReport(
        mode="created",
        config_path=config_path,
        detail_path=detail_path,
        config_added_paths=("llm.provider",),
        config_updated_paths=(),
        config_removed_paths=(),
        env_added_keys=("PROVIDER_API_KEY",),
        env_updated_keys=(),
        env_removed_keys=(),
        created_paths=(config_path, detail_path),
    )

    write_onboarding_report(report)

    content = detail_path.read_text(encoding="utf-8")
    assert detail_path.parent == tmp_path / "onboarding_reports"
    assert "`PROVIDER_API_KEY`" in content
    assert "Secret values are intentionally omitted" in content


def test_read_yaml_mapping_rejects_non_mapping_documents(tmp_path: Path) -> None:
    path = tmp_path / "config.yaml"
    path.write_text("- list\n- document\n", encoding="utf-8")

    assert read_yaml_mapping(path) == {}
    assert read_yaml_mapping(tmp_path / "missing.yaml") == {}
