"""Dynamic user-facing documentation for installed skills."""

from __future__ import annotations

import html
import logging
import os
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from urllib.parse import quote

import yaml

from runtime_settings.instance_layout import InstanceLayout


_FRONTMATTER_PATTERN = re.compile(r"\A---\s*\n(.*?)\n---\s*\n?", re.DOTALL)
_SLUG_PATTERN = re.compile(r"[^a-z0-9]+")
_LOGGER = logging.getLogger(__name__)
# These tracked instructions ship with the application, independently of a
# workspace mount or reviewed managed installs.
BUNDLED_SKILL_DOCS_ROOT = Path(__file__).resolve().parent.parent / "custom_agents" / "skill_templates"



@dataclass(frozen=True)
class SkillDoc:
    name: str
    slug: str
    description: str
    instructions_markdown: str
    skill_root: Path
    skill_file: Path
    metadata: dict[str, Any]

    @property
    def docs_path(self) -> str:
        return f"/docs/skills/{quote(self.slug)}"


def slugify_skill_name(name: str) -> str:
    slug = _SLUG_PATTERN.sub("-", str(name).strip().lower()).strip("-")
    return slug or "skill"


def resolve_user_docs_skills_dir(skills_dir: str | Path | None = None) -> Path:
    """Team skills for the docs: control/skills, or a folder relative to control/."""

    layout = InstanceLayout.at(os.getenv("TEAM_INSTANCE_ROOT") or os.getcwd())
    raw = str(skills_dir or "").strip()
    if not raw:
        return layout.skills_dir
    path = Path(raw).expanduser()
    if not path.is_absolute():
        path = layout.control_dir / path
    return path.resolve()


def _parse_skill_file(skill_file: Path) -> SkillDoc | None:
    text = skill_file.read_text(encoding="utf-8")
    metadata: dict[str, Any] = {}
    body = text
    match = _FRONTMATTER_PATTERN.match(text)
    if match:
        parsed = yaml.safe_load(match.group(1)) or {}
        if isinstance(parsed, dict):
            metadata = dict(parsed)
        body = text[match.end() :]
    name = str(metadata.get("name") or skill_file.parent.name).strip()
    if not name:
        return None
    description = str(metadata.get("description") or "").strip()
    return SkillDoc(
        name=name,
        slug=slugify_skill_name(name),
        description=description,
        instructions_markdown=body.strip(),
        skill_root=skill_file.parent.resolve(),
        skill_file=skill_file.resolve(),
        metadata=metadata,
    )


def _physical_file_identity(path: Path) -> tuple[object, ...]:
    """Identify aliases of the same file across case-insensitive mounts and symlinks."""
    try:
        stat = path.stat()
        return ("inode", stat.st_dev, stat.st_ino)
    except OSError:
        return ("path", os.path.normcase(str(path.resolve())))


def _canonical_skill_files(root: Path) -> list[Path]:
    """Return at most one canonical SKILL file per directory and physical file."""
    skill_files: list[Path] = []
    seen_files: set[tuple[object, ...]] = set()
    for skill_root in sorted(root.iterdir(), key=lambda path: path.name.casefold()):
        if not skill_root.is_dir():
            continue
        candidates = [
            candidate
            for filename in ("SKILL.md", "skill.md")
            if (candidate := skill_root / filename).is_file()
        ]
        if not candidates:
            continue

        canonical = candidates[0]
        canonical_identity = _physical_file_identity(canonical)
        distinct_aliases = {
            _physical_file_identity(candidate)
            for candidate in candidates[1:]
        } - {canonical_identity}
        if distinct_aliases:
            _LOGGER.warning(
                "Multiple distinct skill instruction files found under %s; using %s",
                skill_root,
                canonical.name,
            )
        if canonical_identity in seen_files:
            _LOGGER.warning("Ignoring duplicate physical skill file alias at %s", canonical)
            continue
        seen_files.add(canonical_identity)
        skill_files.append(canonical)
    return skill_files


def list_skill_docs(skills_dir: str | Path | None = None) -> list[SkillDoc]:
    root = resolve_user_docs_skills_dir(skills_dir)
    if not root.exists():
        return []
    docs: list[SkillDoc] = []
    seen_slugs: dict[str, SkillDoc] = {}
    for path in _canonical_skill_files(root):
        doc = _parse_skill_file(path)
        if doc is None:
            continue
        semantic_key = doc.slug.casefold()
        existing = seen_slugs.get(semantic_key)
        if existing is not None:
            _LOGGER.warning(
                "Ignoring duplicate semantic skill slug %s from %s; canonical source is %s",
                doc.slug,
                doc.skill_file,
                existing.skill_file,
            )
            continue
        seen_slugs[semantic_key] = doc
        docs.append(doc)
    return sorted(docs, key=lambda doc: doc.name.casefold())


def find_skill_doc(slug_or_name: str, skills_dir: str | Path | None = None) -> SkillDoc | None:
    requested = slugify_skill_name(slug_or_name)
    for doc in list_skill_docs(skills_dir):
        if doc.slug == requested or slugify_skill_name(doc.name) == requested:
            return doc
    return None


def _render_inline_markdown(text: str) -> str:
    escaped = html.escape(text)
    escaped = re.sub(r"`([^`]+)`", r"<code>\1</code>", escaped)
    escaped = re.sub(r"\*\*([^*]+)\*\*", r"<strong>\1</strong>", escaped)
    return escaped


def markdown_to_html(markdown_text: str) -> str:
    """Render a conservative Markdown subset used by skill docs."""
    blocks: list[str] = []
    paragraph: list[str] = []
    list_items: list[str] = []
    in_code = False
    code_lines: list[str] = []

    def flush_paragraph() -> None:
        if paragraph:
            blocks.append(f"<p>{_render_inline_markdown(' '.join(paragraph))}</p>")
            paragraph.clear()

    def flush_list() -> None:
        if list_items:
            blocks.append("<ul>" + "".join(f"<li>{item}</li>" for item in list_items) + "</ul>")
            list_items.clear()

    for raw_line in markdown_text.splitlines():
        line = raw_line.rstrip()
        stripped = line.strip()
        if stripped.startswith("```"):
            if in_code:
                blocks.append("<pre><code>" + html.escape("\n".join(code_lines)) + "</code></pre>")
                code_lines.clear()
                in_code = False
            else:
                flush_paragraph()
                flush_list()
                in_code = True
            continue
        if in_code:
            code_lines.append(line)
            continue
        if not stripped:
            flush_paragraph()
            flush_list()
            continue
        heading = re.match(r"^(#{1,4})\s+(.+)$", stripped)
        if heading:
            flush_paragraph()
            flush_list()
            level = min(len(heading.group(1)) + 1, 5)
            blocks.append(f"<h{level}>{_render_inline_markdown(heading.group(2))}</h{level}>")
            continue
        bullet = re.match(r"^[-*]\s+(.+)$", stripped)
        numbered = re.match(r"^\d+\.\s+(.+)$", stripped)
        if bullet or numbered:
            flush_paragraph()
            item = (bullet or numbered).group(1)
            list_items.append(_render_inline_markdown(item))
            continue
        paragraph.append(stripped)

    if in_code:
        blocks.append("<pre><code>" + html.escape("\n".join(code_lines)) + "</code></pre>")
    flush_paragraph()
    flush_list()
    return "\n".join(blocks)
