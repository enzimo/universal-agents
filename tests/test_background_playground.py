"""Contract checks for the dark background lab (ui/background-playground)."""

from html.parser import HTMLParser
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[1]
LAB = PROJECT_ROOT / "ui" / "background-playground"
RENDERER = PROJECT_ROOT / "assistant_gateway" / "onboarding_web" / "assets" / "metallic_mesh.js"


class _PageParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self.ids: set[str] = set()
        self.scripts: list[str] = []
        self.canvas_attributes: list[dict[str, str | None]] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        attributes = dict(attrs)
        if element_id := attributes.get("id"):
            self.ids.add(element_id)
        if tag == "script" and (source := attributes.get("src")):
            self.scripts.append(source)
        if tag == "canvas":
            self.canvas_attributes.append(attributes)


def test_lab_loads_the_product_renderer_and_keeps_no_copy() -> None:
    parser = _PageParser()
    page = (LAB / "index.html").read_text(encoding="utf-8")
    parser.feed(page)

    # appearance.js supplies the one settings text form shared with
    # Customize Theme, so text copies between the two.
    assert parser.scripts == [
        "../../assistant_gateway/onboarding_web/assets/appearance.js",
        "../../assistant_gateway/onboarding_web/assets/metallic_mesh.js",
        "lab.js",
    ]
    assert (LAB / parser.scripts[1]).resolve() == RENDERER.resolve()
    assert sorted(path.name for path in LAB.iterdir()) == ["README.md", "index.html", "lab.js"]
    assert any("data-ua-metallic-mesh" in attributes for attributes in parser.canvas_attributes)
    assert {"controls", "regenerate", "mock-toggle", "freeze", "copy", "apply", "settings-text", "reset", "status"} <= parser.ids


def test_lab_offers_the_documented_switches_and_measurements() -> None:
    page = (LAB / "index.html").read_text(encoding="utf-8")
    lab = (LAB / "lab.js").read_text(encoding="utf-8")
    readme = (LAB / "README.md").read_text(encoding="utf-8")

    for switch in ("nowebgl", "light2d"):
        assert switch in page
        assert f"?{switch}" in readme
    for helper in ("window.labSizeStats", "window.labCapture"):
        assert helper in lab
    # The lab edits settings through the renderer's public update and reads
    # its lab hooks; it never reaches into renderer internals.
    assert "mesh.update(" in lab
    assert "mesh.lab.settings()" in lab
    assert "python -m http.server 18100 --bind 127.0.0.1" in readme
