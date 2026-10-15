"""Appearance configuration and contracts."""

from __future__ import annotations
import copy
import logging
from collections.abc import Mapping
from typing import Any, Literal, cast
from pydantic import BaseModel, Field, ValidationError, field_validator, model_validator
from default_values import UI_DEFAULTS

logger = logging.getLogger(__name__)


def _normalize_hex_color(value: Any, *, field_name: str) -> str:
    rendered = str(value or "").strip()
    if len(rendered) != 7 or not rendered.startswith("#"):
        raise ValueError(f"{field_name} must be a #RRGGBB color.")
    try:
        int(rendered[1:], 16)
    except ValueError as exc:
        raise ValueError(f"{field_name} must be a #RRGGBB color.") from exc
    return rendered.lower()


DARK_APPEARANCE_PATH = "ui.onboarding.appearance.dark"
DARK_DETAIL_LEVELS = ("full", "basic")

# Integer dark-background settings and their inclusive ranges. The browser
# renderer (assistant_gateway/onboarding_web/assets/metallic_mesh.js) clamps
# to the same ranges, and the per-person Web Chat contract reuses them.
DARK_INTEGER_RANGES: dict[str, tuple[int, int]] = {
    "facet_size": (140, 520),
    "facet_depth": (0, 100),
    "chrome_faces": (0, 20),
    "shine": (0, 100),
    "edge_highlights": (0, 100),
    "surface_grain": (0, 100),
    "light_x": (-300, 300),
    "light_y": (-300, 300),
    "light_softness": (0, 100),
    "light_wander_distance": (0, 150),
    "light_speed": (10, 400),
    "mesh_speed": (10, 400),
    "seed": (1, 4_294_967_295),
}
ACCENT_SHARE_RANGE = (0, 50)

# Dark settings that no longer exist. A dark section that still holds any of
# them was tuned for a lighting that is gone, so the whole section is set
# aside with a warning and the defaults apply until new dark settings are
# saved. Keeping only the surviving values would mix two tunings: the earlier
# renderer's near-black base color would make the new metal almost black, and
# values chosen under the four-light studio (light_angle) look different under
# the single positioned light.
RETIRED_DARK_KEYS = frozenset(
    {
        # The earlier renderer.
        "metallic_seams",
        "shimmer_count",
        "shimmer_speed",
        "shimmer_width",
        "mesh_morphing",
        "mesh_morph_speed",
        # The four-light studio, replaced by light_x, light_y and light_softness.
        "light_angle",
    }
)
RETIRED_DARK_ACCENTS = frozenset({"silver"})
RETIRED_DARK_ACCENT_FIELDS = frozenset({"strength"})


def _retired_accent_keys(accents: dict[str, Any]) -> list[str]:
    found = [f"accents.{name}" for name in sorted(RETIRED_DARK_ACCENTS & accents.keys())]
    for name in sorted(accents):
        accent = accents[name]
        if isinstance(accent, dict):
            fields = sorted(RETIRED_DARK_ACCENT_FIELDS & accent.keys())
            found.extend(f"accents.{name}.{field}" for field in fields)
    return found


def retired_dark_keys(value: Any) -> list[str]:
    """Names of retired settings in a dark appearance mapping."""

    if not isinstance(value, dict):
        return []
    found = sorted(RETIRED_DARK_KEYS & value.keys())
    accents = value.get("accents")
    if isinstance(accents, dict):
        found.extend(_retired_accent_keys(accents))
    return found


def _normalize_ranged_int(value: Any, *, field_name: str, bounds: tuple[int, int]) -> int:
    try:
        parsed = int(value)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{field_name} must be a whole number.") from exc
    low, high = bounds
    if parsed < low or parsed > high:
        raise ValueError(f"{field_name} must be between {low} and {high}.")
    return parsed


class MetallicAccentSettings(BaseModel):
    """A metal tint and the share of faces, in percent, that use it."""

    color: str
    share: int

    @field_validator("color", mode="before")
    @classmethod
    def _normalize_color(cls, value: Any) -> str:
        return _normalize_hex_color(value, field_name=f"{DARK_APPEARANCE_PATH}.accents.color")

    @field_validator("share", mode="before")
    @classmethod
    def _normalize_share(cls, value: Any) -> int:
        return _normalize_ranged_int(
            value,
            field_name=f"{DARK_APPEARANCE_PATH}.accents.share",
            bounds=ACCENT_SHARE_RANGE,
        )


def _default_metallic_accent(name: str) -> MetallicAccentSettings:
    defaults = UI_DEFAULTS.onboarding_dark_appearance.value["accents"][name]
    return MetallicAccentSettings.model_validate(copy.deepcopy(defaults))


class MetallicAccentPaletteSettings(BaseModel):
    gold: MetallicAccentSettings = Field(
        default_factory=lambda: _default_metallic_accent("gold")
    )
    bronze: MetallicAccentSettings = Field(
        default_factory=lambda: _default_metallic_accent("bronze")
    )


_DARK_DEFAULTS = UI_DEFAULTS.onboarding_dark_appearance.value


class DarkMetalAppearanceSettings(BaseModel):
    """Dark metallic background: polished metal plates in a dark studio."""

    detail: Literal["full", "basic"] = cast(Literal["full", "basic"], _DARK_DEFAULTS["detail"])
    surface_color: str = _DARK_DEFAULTS["surface_color"]
    metal_color: str = _DARK_DEFAULTS["metal_color"]
    facet_size: int = _DARK_DEFAULTS["facet_size"]
    facet_depth: int = _DARK_DEFAULTS["facet_depth"]
    chrome_faces: int = _DARK_DEFAULTS["chrome_faces"]
    shine: int = _DARK_DEFAULTS["shine"]
    edge_highlights: int = _DARK_DEFAULTS["edge_highlights"]
    surface_grain: int = _DARK_DEFAULTS["surface_grain"]
    light_x: int = _DARK_DEFAULTS["light_x"]
    light_y: int = _DARK_DEFAULTS["light_y"]
    light_softness: int = _DARK_DEFAULTS["light_softness"]
    light_motion: bool = _DARK_DEFAULTS["light_motion"]
    light_wander: bool = _DARK_DEFAULTS["light_wander"]
    light_wander_distance: int = _DARK_DEFAULTS["light_wander_distance"]
    light_speed: int = _DARK_DEFAULTS["light_speed"]
    mesh_motion: bool = _DARK_DEFAULTS["mesh_motion"]
    mesh_speed: int = _DARK_DEFAULTS["mesh_speed"]
    seed: int = _DARK_DEFAULTS["seed"]
    accents: MetallicAccentPaletteSettings = Field(
        default_factory=MetallicAccentPaletteSettings
    )

    @model_validator(mode="before")
    @classmethod
    def _set_aside_retired_settings(cls, value: Any) -> Any:
        retired = retired_dark_keys(value)
        if not retired:
            return value
        logger.warning(
            "Ignoring %s: it holds dark background settings that no longer exist (%s). "
            "The dark background defaults apply. To stop this warning, replace that "
            "section in instance_config.yaml (for example in the config editor) with the "
            "dark section from config.example.yaml, or remove it.",
            DARK_APPEARANCE_PATH,
            ", ".join(retired),
        )
        return {}

    @field_validator("surface_color", "metal_color", mode="before")
    @classmethod
    def _normalize_colors(cls, value: Any, info: Any) -> str:
        return _normalize_hex_color(value, field_name=f"{DARK_APPEARANCE_PATH}.{info.field_name}")

    @field_validator(*DARK_INTEGER_RANGES, mode="before")
    @classmethod
    def _normalize_ranges(cls, value: Any, info: Any) -> int:
        return _normalize_ranged_int(
            value,
            field_name=f"{DARK_APPEARANCE_PATH}.{info.field_name}",
            bounds=DARK_INTEGER_RANGES[info.field_name],
        )


# CSS custom properties that carry saved dark settings to Docs and broker
# pages. appearance.js reads them back through its DARK_FIELDS table; a test
# keeps the two lists equal. Accent metals use --ua-dark-<name> for the tint
# and --ua-dark-<name>-share for the share of faces.
DARK_CSS_VARIABLES: dict[str, str] = {
    "detail": "--ua-dark-detail",
    "surface_color": "--ua-dark-surface",
    "metal_color": "--ua-dark-metal",
    "facet_size": "--ua-dark-facet-size",
    "facet_depth": "--ua-dark-facet-depth",
    "chrome_faces": "--ua-dark-chrome-faces",
    "shine": "--ua-dark-shine",
    "edge_highlights": "--ua-dark-edge-highlights",
    "surface_grain": "--ua-dark-surface-grain",
    "light_x": "--ua-dark-light-x",
    "light_y": "--ua-dark-light-y",
    "light_softness": "--ua-dark-light-softness",
    "light_motion": "--ua-dark-light-motion",
    "light_wander": "--ua-dark-light-wander",
    "light_wander_distance": "--ua-dark-light-wander-distance",
    "light_speed": "--ua-dark-light-speed",
    "mesh_motion": "--ua-dark-mesh-motion",
    "mesh_speed": "--ua-dark-mesh-speed",
    "seed": "--ua-dark-seed",
}


def dark_appearance_css_declarations(dark: Any) -> list[str]:
    """CSS declarations for saved dark settings, one per line.

    The baseline normally comes from a validated config; anything invalid is
    reported and replaced by the defaults so a page still renders.
    """

    try:
        settings = DarkMetalAppearanceSettings.model_validate(
            dark if isinstance(dark, Mapping) else {}
        )
    except ValidationError as exc:
        logger.warning(
            "Dark background settings for a Docs or broker page are invalid; the defaults "
            "apply. event=dark_appearance_css_defaults problem=%s",
            exc.errors()[0].get("msg", "invalid"),
        )
        settings = DarkMetalAppearanceSettings()
    values = settings.model_dump(mode="json")
    lines: list[str] = []
    for name, variable in DARK_CSS_VARIABLES.items():
        value = values[name]
        rendered = ("true" if value else "false") if isinstance(value, bool) else str(value)
        lines.append(f"  {variable}: {rendered};")
    for name, accent in values["accents"].items():
        lines.append(f"  --ua-dark-{name}: {accent['color']};")
        lines.append(f"  --ua-dark-{name}-share: {accent['share']};")
    return lines


class OnboardingAppearanceSettings(BaseModel):
    color_mode: Literal["light", "dark"] = cast(
        Literal["light", "dark"], UI_DEFAULTS.onboarding_color_mode.value
    )
    light_colors: list[str] = Field(
        default_factory=lambda: list(UI_DEFAULTS.onboarding_light_colors.value)
    )
    light_diffusion: int = UI_DEFAULTS.onboarding_light_diffusion.value
    polygon_opacity_low: int = UI_DEFAULTS.onboarding_polygon_opacity_low.value
    polygon_opacity_high: int = UI_DEFAULTS.onboarding_polygon_opacity_high.value
    polygon_line_color: str = UI_DEFAULTS.onboarding_polygon_line_color.value
    polygon_line_opacity: int = UI_DEFAULTS.onboarding_polygon_line_opacity.value
    polygon_seed: int = UI_DEFAULTS.onboarding_polygon_seed.value
    dynamic_background: bool = UI_DEFAULTS.onboarding_dynamic_background.value
    interface_animation: bool = UI_DEFAULTS.onboarding_interface_animation.value
    dark: DarkMetalAppearanceSettings = Field(
        default_factory=DarkMetalAppearanceSettings
    )

    @field_validator("light_colors", mode="before")
    @classmethod
    def _normalize_light_colors(cls, value: Any) -> list[str]:
        raw_values = (
            value
            if isinstance(value, list)
            else UI_DEFAULTS.onboarding_light_colors.value
        )
        normalized = [
            _normalize_hex_color(
                color, field_name="ui.onboarding.appearance.light_colors"
            )
            for color in raw_values[:4]
        ]
        defaults = list(UI_DEFAULTS.onboarding_light_colors.value)
        while len(normalized) < 4:
            normalized.append(defaults[len(normalized)])
        return normalized

    @field_validator("polygon_line_color", mode="before")
    @classmethod
    def _normalize_polygon_line_color(cls, value: Any) -> str:
        return _normalize_hex_color(
            value or UI_DEFAULTS.onboarding_polygon_line_color.value,
            field_name="ui.onboarding.appearance.polygon_line_color",
        )

    @field_validator(
        "light_diffusion",
        "polygon_opacity_low",
        "polygon_opacity_high",
        "polygon_line_opacity",
        mode="before",
    )
    @classmethod
    def _normalize_percent(cls, value: Any, info: Any) -> int:
        try:
            parsed = int(value)
        except (TypeError, ValueError) as exc:
            raise ValueError(
                f"ui.onboarding.appearance.{info.field_name} must be an integer percentage."
            ) from exc
        if parsed < 0 or parsed > 100:
            raise ValueError(
                f"ui.onboarding.appearance.{info.field_name} must be between 0 and 100."
            )
        return parsed

    @field_validator("polygon_seed", mode="before")
    @classmethod
    def _normalize_polygon_seed(cls, value: Any) -> int:
        parsed = int(value)
        if parsed < 1 or parsed > 4_294_967_295:
            raise ValueError(
                "ui.onboarding.appearance.polygon_seed must be between 1 and 4294967295."
            )
        return parsed

    @model_validator(mode="after")
    def _validate_opacity_range(self) -> "OnboardingAppearanceSettings":
        if self.polygon_opacity_low > self.polygon_opacity_high:
            raise ValueError(
                "ui.onboarding.appearance.polygon_opacity_low must be <= polygon_opacity_high."
            )
        return self


class OnboardingUISettings(BaseModel):
    appearance: OnboardingAppearanceSettings = Field(
        default_factory=OnboardingAppearanceSettings
    )


class UISettings(BaseModel):
    onboarding: OnboardingUISettings = Field(default_factory=OnboardingUISettings)
