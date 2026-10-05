"""What a tool's static HTTP path may be (DESIGN §30): one rule for the compiler and the adapter.

The path is joined onto a connection's base URL, so it must stay UNDER it: no scheme or host, no
query or fragment, no dot segments (plain or percent-encoded) that a URL stack would resolve to a
different place, no encoded separators. `{name}` segments are path parameters; their VALUES are
checked separately where they are filled in.
"""

from __future__ import annotations

import re
from urllib.parse import unquote


def http_path_problem(path: str) -> str | None:
    """Why `path` is not acceptable, or None."""
    if not path.startswith("/"):
        return "must start with '/' (a path under the connection, never a URL)"
    if path.startswith("//"):
        return "must not start with '//' (that names a host)"
    if any(ch in path for ch in "?#\\"):
        return "must not contain '?', '#' or '' (query and fragment come from the tool spec)"
    if any(ord(ch) < 0x21 or ord(ch) == 0x7F for ch in path):
        return "must not contain spaces or control characters"
    segments = path.split("/")[1:]
    for index, segment in enumerate(segments):
        if segment == "" and index != len(segments) - 1:
            return "must not contain empty segments ('//')"
        if re.search(r"%(?![0-9A-Fa-f]{2})", segment):
            return "must not contain a malformed percent-escape"
        decoded = unquote(segment)
        if decoded in (".", ".."):
            return "must not contain '.' or '..' segments (even percent-encoded)"
        if any(ch in decoded for ch in ("/", "\\", "?", "#")):
            return "must not contain an encoded separator, query or fragment mark"
        if any(ord(ch) < 0x21 or ord(ch) == 0x7F for ch in decoded):
            return "must not contain encoded spaces or control characters"
    return None
