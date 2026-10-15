"""Read an operator UI entry point and its explicit ES module dependencies."""

from pathlib import Path
import re


def module_source(root: Path, name: str) -> str:
    visited: set[str] = set()

    def read(filename: str) -> str:
        if filename in visited:
            return ""
        visited.add(filename)
        source = (root / filename).read_text(encoding="utf-8")
        imports = re.findall(r'from "\./([\w.]+)"', source)
        return source + "\n" + "\n".join(read(value) for value in imports)

    return read(name)
