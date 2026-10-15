from __future__ import annotations

import logging
from pathlib import Path

import pytest
import yaml

from assistant_gateway.config import GatewayInstanceConfig, INSTANCE_CONFIG_SCHEMA_VERSION
from assistant_gateway.onboarding_engine import load_base_onboarding_template
from runtime_settings.appearance import (
    DARK_CSS_VARIABLES,
    DarkMetalAppearanceSettings,
    OnboardingAppearanceSettings,
    dark_appearance_css_declarations,
)
from runtime_settings.governance import ToolPolicySettings


def _new_instance_config(tmp_path: Path) -> GatewayInstanceConfig:
    return GatewayInstanceConfig.create(
        instance_name="previewbot",
        instance_root=str(tmp_path / "instance"),
        team_id="team_preview",
        timezone="America/Chicago",
    )


def test_instance_config_persists_onboarding_appearance_settings(tmp_path: Path) -> None:
    config = _new_instance_config(tmp_path)
    config.ui.onboarding.appearance = OnboardingAppearanceSettings.model_validate(
        {
            "color_mode": "dark",
            "light_colors": ["#0011aa", "#ffee00", "#20f05a", "#ff3030"],
            "light_diffusion": 12,
            "polygon_opacity_low": 16,
            "polygon_opacity_high": 64,
            "polygon_line_color": "#123456",
            "polygon_line_opacity": 31,
            "polygon_seed": 8675309,
            "dynamic_background": False,
            "interface_animation": False,
            "dark": {
                "detail": "basic",
                "surface_color": "#1d2027",
                "metal_color": "#b8bec6",
                "facet_size": 260,
                "facet_depth": 34,
                "chrome_faces": 11,
                "shine": 62,
                "edge_highlights": 44,
                "surface_grain": 36,
                "light_x": -40,
                "light_y": 60,
                "light_softness": 70,
                "light_motion": False,
                "light_speed": 125,
                "mesh_motion": True,
                "mesh_speed": 75,
                "seed": 42,
                "accents": {
                    "gold": {"color": "#8a6a22", "share": 12},
                    "bronze": {"color": "#9d5738", "share": 5},
                },
            },
        }
    )

    config_path = config.save()
    raw = yaml.safe_load(config_path.read_text(encoding="utf-8"))
    appearance = raw["ui"]["onboarding"]["appearance"]

    assert raw["schema_version"] == INSTANCE_CONFIG_SCHEMA_VERSION
    assert appearance["light_colors"] == ["#0011aa", "#ffee00", "#20f05a", "#ff3030"]
    assert appearance["light_diffusion"] == 12
    assert appearance["polygon_opacity_low"] == 16
    assert appearance["polygon_opacity_high"] == 64
    assert appearance["polygon_line_color"] == "#123456"
    assert appearance["polygon_line_opacity"] == 31
    assert appearance["polygon_seed"] == 8675309
    assert appearance["dynamic_background"] is False
    assert appearance["interface_animation"] is False
    assert appearance["color_mode"] == "dark"
    assert appearance["dark"]["detail"] == "basic"
    assert appearance["dark"]["facet_size"] == 260
    assert appearance["dark"]["light_x"] == -40
    assert appearance["dark"]["light_y"] == 60
    assert appearance["dark"]["light_softness"] == 70
    assert appearance["dark"]["light_motion"] is False
    assert appearance["dark"]["mesh_speed"] == 75
    assert appearance["dark"]["accents"]["bronze"] == {
        "color": "#9d5738",
        "share": 5,
    }

    loaded = GatewayInstanceConfig.load(config_path)

    assert loaded.ui.onboarding.appearance.polygon_line_color == "#123456"
    assert loaded.ui.onboarding.appearance.polygon_line_opacity == 31
    assert loaded.ui.onboarding.appearance.polygon_seed == 8675309
    assert loaded.ui.onboarding.appearance.dark.seed == 42
    assert loaded.ui.onboarding.appearance.dark.chrome_faces == 11


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("light_x", 301),
        ("light_y", -301),
        ("light_softness", 101),
        ("light_speed", 5),
        ("mesh_speed", 401),
        ("chrome_faces", 21),
        ("facet_size", 139),
    ],
)
def test_dark_appearance_rejects_out_of_range_settings(field: str, value: int) -> None:
    with pytest.raises(ValueError, match=f"ui.onboarding.appearance.dark.{field}"):
        OnboardingAppearanceSettings.model_validate({"dark": {field: value}})


def test_dark_appearance_rejects_unknown_detail_level() -> None:
    with pytest.raises(ValueError, match="detail"):
        DarkMetalAppearanceSettings.model_validate({"detail": "ultra"})


def test_dark_section_of_the_earlier_renderer_is_ignored_with_a_warning(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Old dark sections keep working configs loadable but never steer the new look.

    Onboarding wrote every dark key, so each existing instance holds them. Their
    surviving names also meant something else (a near-black base color), so the
    whole section falls back to the defaults and the warning names what was set
    aside.
    """

    previous = {
        "surface_color": "#030202",
        "facet_depth": 22,
        "facet_size": 220,
        "metallic_seams": 30,
        "shimmer_count": 9,
        "mesh_morphing": False,
        "seed": 9187,
        "accents": {
            "gold": {"color": "#618500", "strength": 89},
            "silver": {"color": "#1a1414", "strength": 65},
        },
    }

    with caplog.at_level(logging.WARNING, logger="runtime_settings.appearance"):
        appearance = OnboardingAppearanceSettings.model_validate(
            {"color_mode": "light", "light_diffusion": 30, "dark": previous}
        )

    assert appearance.dark == DarkMetalAppearanceSettings()
    assert appearance.color_mode == "light"
    assert appearance.light_diffusion == 30
    message = caplog.records[-1].getMessage()
    assert "ui.onboarding.appearance.dark" in message
    for name in ("metallic_seams", "shimmer_count", "mesh_morphing", "accents.silver", "accents.gold.strength"):
        assert name in message


def test_dark_section_tuned_for_the_four_light_studio_is_ignored_with_a_warning(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """light_angle aimed the main light of the four-light studio.

    Values chosen under that lighting look different under the single
    positioned light, so the section is set aside rather than half kept.
    """

    four_light = DarkMetalAppearanceSettings(seed=77).model_dump(mode="json")
    for name in ("light_x", "light_y", "light_softness"):
        four_light.pop(name)
    four_light["light_angle"] = 200

    with caplog.at_level(logging.WARNING, logger="runtime_settings.appearance"):
        loaded = DarkMetalAppearanceSettings.model_validate(four_light)

    assert loaded == DarkMetalAppearanceSettings()
    assert "light_angle" in caplog.records[-1].getMessage()


def test_current_dark_section_loads_without_a_warning(caplog: pytest.LogCaptureFixture) -> None:
    current = DarkMetalAppearanceSettings(seed=77, facet_size=300).model_dump(mode="json")

    with caplog.at_level(logging.WARNING, logger="runtime_settings.appearance"):
        loaded = DarkMetalAppearanceSettings.model_validate(current)

    assert loaded.seed == 77
    assert loaded.facet_size == 300
    assert not caplog.records


def test_dark_css_declarations_cover_every_setting_and_fall_back_to_defaults(
    caplog: pytest.LogCaptureFixture,
) -> None:
    declarations = dark_appearance_css_declarations({"facet_size": 300, "light_motion": False})

    assert "  --ua-dark-facet-size: 300;" in declarations
    assert "  --ua-dark-light-motion: false;" in declarations
    assert "  --ua-dark-gold-share: 10;" in declarations
    assert len(declarations) == len(DARK_CSS_VARIABLES) + 2 * 2

    with caplog.at_level(logging.WARNING, logger="runtime_settings.appearance"):
        fallback = dark_appearance_css_declarations({"facet_size": "huge"})

    assert fallback == dark_appearance_css_declarations(None)
    assert "event=dark_appearance_css_defaults" in caplog.records[-1].getMessage()


def test_light_appearance_rejects_out_of_range_mesh_seed() -> None:
    with pytest.raises(ValueError, match="polygon_seed"):
        OnboardingAppearanceSettings.model_validate({"polygon_seed": 0})


def test_onboarding_carries_tool_policy_without_data_folders(tmp_path: Path) -> None:
    """The template carries tool_policy, and data folders are never part of it.

    Data folders live in the access store (``/access folders add``), so a base
    config that still lists ``tool_policy.data_folders`` is refused by name
    instead of being copied into a new instance.
    """

    base_config = tmp_path / "config.yaml"
    base_config.write_text("schema_version: 1\ntool_policy: {}\n", encoding="utf-8")

    template = load_base_onboarding_template(str(base_config))

    assert template.tool_policy == ToolPolicySettings()
    assert "data_folders" not in template.tool_policy.model_dump()

    base_config.write_text(
        "schema_version: 1\n"
        "tool_policy:\n"
        "  data_folders:\n"
        f"    - {{name: reports, path: {tmp_path / 'reports'}, writable: false}}\n",
        encoding="utf-8",
    )
    with pytest.raises(ValueError, match="tool_policy.data_folders has been removed"):
        load_base_onboarding_template(str(base_config))


def test_web_chat_appearance_without_interface_animation_keeps_it_on() -> None:
    """A person who never chose has interface animation on."""

    from assistant_gateway.application.website_application import WebAppearanceV1

    saved = OnboardingAppearanceSettings().model_dump(mode="json")
    saved.pop("interface_animation")
    assert WebAppearanceV1.model_validate(saved).interface_animation is True
