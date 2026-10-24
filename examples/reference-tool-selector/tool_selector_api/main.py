"""Reference tool-selector server: an EXTERNAL system that ranks tools for a message.

It speaks contract v1 (`docs/TOOL_SELECTOR_CONTRACT.md`) and knows nothing about
conversation_agent. The ranking is deliberately naive (stems shared between the message and each
tool's name/description) so it runs anywhere; a real server would put a classifier or embeddings
behind the same endpoint.

    uv run uvicorn tool_selector_api.main:app --port 8002
    SELECTOR_TOKEN=secret uv run uvicorn tool_selector_api.main:app --port 8002   # needs a bearer
"""

from __future__ import annotations

import os
import re
import unicodedata
from typing import Any

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

VERSION = 1
STEM = 5  # compare the first 5 letters: "horarios" ~ "horario", "cancela" ~ "cancel"
_STOP = {"para", "pra", "uma", "umas", "uns", "com", "que", "por", "meu", "minha", "dos", "das"}


def _stems(text: str) -> set[str]:
    plain = "".join(
        c for c in unicodedata.normalize("NFKD", text.lower()) if not unicodedata.combining(c)
    )
    words = re.findall(r"[a-z0-9]{3,}", plain)
    return {w[:STEM] for w in words if w not in _STOP}


def rank(message: str, tools: list[dict[str, Any]]) -> list[dict[str, Any]]:
    wanted = _stems(message)
    scores = []
    for tool in tools:
        hits = len(wanted & _stems(f"{tool['name'].replace('_', ' ')} {tool['description']}"))
        scores.append({"name": tool["name"], "score": round(1 - 0.5**hits, 4)})
    return scores


def create_app(token: str | None = None) -> FastAPI:
    app = FastAPI(title="reference tool selector")

    @app.post("/tool-selection")
    async def select(request: Request) -> JSONResponse:
        if token is not None and request.headers.get("authorization") != f"Bearer {token}":
            return JSONResponse({"error": "unauthorized"}, status_code=401)
        body = await request.json()
        if body.get("version") != VERSION:
            return JSONResponse({"error": "unsupported version"}, status_code=400)
        return JSONResponse({"version": VERSION, "scores": rank(body["message"], body["tools"])})

    return app


app = create_app(os.environ.get("SELECTOR_TOKEN"))
