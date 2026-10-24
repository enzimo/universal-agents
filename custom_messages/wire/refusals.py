"""The refusal a receiver gives for a frame or a state record (registry verification_refusals)."""

from __future__ import annotations

from typing import Any, Mapping


class FrameRefused(ValueError):
    """A receiver refuses a frame or record.

    ``code`` is one of the registry's verification refusals, checked in the
    registry's order. ``at`` is the JSON pointer of the field the refusal is about
    (empty for the document as a whole); the invalid examples name both.
    """

    def __init__(self, code: str, detail: str = "", at: str = "") -> None:
        super().__init__(f"{code}: {detail}" if detail else code)
        self.code = code
        self.detail = detail
        self.at = at


# No shape is known: keep every location item.
UNKNOWN_SHAPE: Any = object()


def pointer(error: Mapping[str, Any], shape: Any = UNKNOWN_SHAPE, prefix: str = "") -> str:
    """A JSON pointer for one Pydantic validation error.

    Pydantic's locations also hold labels that are not JSON keys: the tag of a
    discriminated union and the name of a union member. ``shape`` (the generated
    shape of the frame, a body or a reply) says which items are keys, so labels
    are left out. Without a shape every item is kept.
    """

    location, parts = list(error["loc"]), []
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
