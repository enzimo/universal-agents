"""The refusal a receiver gives for a frame or a state record (registry verification_refusals)."""

from __future__ import annotations

from typing import Any, Mapping, Optional


class FrameRefused(ValueError):
    """A receiver refuses a frame or record.

    ``code`` is one of the registry's verification refusals, checked in the
    registry's order. ``at`` is the JSON pointer of the field the refusal is about
    (empty for the document as a whole); the invalid examples name both.
    ``known_at`` is the same pointer up to the last field the schema knows: for an
    unknown field it names the object that holds it, so a name the sender chose stays
    out of a dead-letter notice (README, "Dead letters").
    """

    def __init__(self, code: str, detail: str = "", at: str = "", known_at: Optional[str] = None) -> None:
        super().__init__(f"{code}: {detail}" if detail else code)
        self.code = code
        self.detail = detail
        self.at = at
        self.known_at = at if known_at is None else known_at


# No shape is known: keep every location item.
UNKNOWN_SHAPE: Any = object()


def refused_by(code: str, error: Any, shape: Any = UNKNOWN_SHAPE, prefix: str = "") -> FrameRefused:
    """The refusal for a Pydantic validation error: its first line, its pointer and its known pointer."""

    first = error.errors()[0]
    return FrameRefused(code, str(error).splitlines()[0], at=pointer(first, shape, prefix),
                        known_at=pointer(first, shape, prefix, known_only=True))


def pointer(error: Mapping[str, Any], shape: Any = UNKNOWN_SHAPE, prefix: str = "", known_only: bool = False) -> str:
    """A JSON pointer for one Pydantic validation error.

    Pydantic's locations also hold labels that are not JSON keys: the tag of a
    discriminated union and the name of a union member. ``shape`` (the generated
    shape of the frame, a body or a reply) says which items are keys, so labels
    are left out. Without a shape every item is kept.
    """

    location, parts = list(error["loc"]), []
    if known_only and error.get("type") == "extra_forbidden":
        location = location[:-1]  # the unknown field's own name
    for index, item in enumerate(location):
        if shape is UNKNOWN_SHAPE:
            parts.append(item)
        elif isinstance(shape, dict) and "one_of" in shape:
            shape = shape["variants"].get(str(item))
        elif isinstance(shape, dict) and "properties" in shape:
            if str(item) in shape["properties"]:
                parts.append(item)
                shape = shape["properties"][str(item)]
            elif index == len(location) - 1 and error.get("type") == "extra_forbidden":
                parts.append(item)
        elif isinstance(shape, dict) and "items" in shape and isinstance(item, int):
            parts.append(item)
            shape = shape["items"]
    return prefix + "".join("/" + str(part).replace("~", "~0").replace("/", "~1") for part in parts)
