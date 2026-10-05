"""Capability inputs that take a file the contact sent.

A capability field typed `media` is a `MediaArg` inside the runtime, but the MODEL only ever sees
(and writes) a handle: a short string such as `media_3` that names one of the conversation's own
files. The runtime turns the handle into the file's reference; the model cannot invent a file, an
id or a checksum (INV-002).
"""

from __future__ import annotations

from typing import Any, Union, get_args, get_origin

from pydantic import BaseModel

from conversation_agent.core.models.media import MEDIA_HANDLE_PATTERN, MediaArg


def _is_media(annotation: Any) -> bool:
    if annotation is MediaArg:
        return True
    if (
        get_origin(annotation) is Union
        or str(get_origin(annotation)) == "<class 'types.UnionType'>"
    ):
        return any(arg is MediaArg for arg in get_args(annotation))
    return False


def media_fields(model: type[BaseModel]) -> tuple[str, ...]:
    """Names of the fields of `model` that take a file."""
    return tuple(name for name, info in model.model_fields.items() if _is_media(info.annotation))


def llm_input_schema(model: type[BaseModel]) -> dict[str, Any]:
    """The JSON schema the model sees: every media field is a handle string, not an object."""
    schema = model.model_json_schema()
    names = media_fields(model)
    if not names:
        return schema
    properties = schema.get("properties", {})
    for name in names:
        original = properties.get(name, {})
        note = "Handle of a file the contact sent in this conversation, such as media_1."
        properties[name] = {
            "type": "string",
            "pattern": MEDIA_HANDLE_PATTERN,
            "description": f"{original['description']} {note}"
            if "description" in original
            else note,
        }
        if "title" in original:
            properties[name]["title"] = original["title"]
    schema.get("$defs", {}).pop("MediaArg", None)
    if not schema.get("$defs"):
        schema.pop("$defs", None)
    return schema
