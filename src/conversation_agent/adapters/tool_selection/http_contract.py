"""Wire contract v1 between the framework and an external tool-ranking server.

    POST {connection.base_url}{http.path}
    Content-Type: application/json
    <credential header from the connection's AuthSpec, if any>
    X-Tenant-Id: <tenant>

    {"version": 1,
     "message": "quero marcar um corte amanhã",
     "tools": [{"name": "agenda_create_booking", "description": "..."}, ...],
     "top_n": 8}

    200 {"version": 1, "scores": [{"name": "agenda_create_booking", "score": 0.93}, ...]}

Rules: `score` is in [0, 1]; a tool left out of `scores` is read as 0; names the server invents are
ignored by the framework (the answer can only rank the tools it was sent); the answer must have
the same `version`. Anything else (another version, a bad shape, non-200, timeout, oversize body)
is a failed call and the framework sends the model the full list. `top_n` is a hint: the framework
applies its own `top_n` and `min_score`. Only the message text and the tool names/descriptions
leave the process: no history, contact id or tool schemas.
"""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field

CONTRACT_VERSION = 1


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid")


class WireTool(_Strict):
    name: str
    description: str


class SelectRequest(_Strict):
    version: int = CONTRACT_VERSION
    message: str
    tools: list[WireTool]
    top_n: int


class WireScore(_Strict):
    name: str
    score: float = Field(ge=0.0, le=1.0)


class SelectResponse(_Strict):
    version: int
    scores: list[WireScore]
