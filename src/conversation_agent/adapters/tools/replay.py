"""Record and replay tool results (DESIGN §42.1): deterministic evals without the outside world.

`RecordingToolProvider` wraps a real provider and keeps what it answered; `ReplayToolProvider`
serves those answers back by (tool, arguments) and CANNOT reach anything: it has no transport.
A call nobody recorded is an explicit `REPLAY_MISS`, never an invented answer; the same call made
twice is served in recorded order and then runs out (a write that replays more often than it was
recorded is a behaviour change worth failing on).

A cassette is a fixture, so recording redacts free text (emails, phones, tokens, CPF/CNPJ) by
default (`core.redaction`): record from synthetic or staging traffic, and treat a cassette made
from production as personal data regardless.
"""

from __future__ import annotations

import json
from collections import defaultdict, deque
from pathlib import Path
from typing import Any

from conversation_agent.core.canonical import stable_hash
from conversation_agent.core.definitions.binding import ResolvedToolBinding
from conversation_agent.core.models.tooling import ToolContext, ToolError, ToolResult
from conversation_agent.core.redaction import redact
from conversation_agent.ports.tool_provider import ToolProvider

Cassette = dict[str, list[ToolResult]]


def call_key(tool_name: str, args: dict[str, Any]) -> str:
    return stable_hash(tool_name, args)


def _redacted(value: Any) -> Any:
    if isinstance(value, str):
        return redact(value)
    if isinstance(value, dict):
        return {k: _redacted(v) for k, v in value.items()}
    if isinstance(value, list):
        return [_redacted(v) for v in value]
    return value


class RecordingToolProvider:
    def __init__(self, inner: ToolProvider, *, redact_data: bool = True) -> None:
        self._inner = inner
        self._redact = redact_data
        self.cassette: Cassette = defaultdict(list)
        self._names: dict[str, str] = {}

    async def destination_fingerprint(
        self, binding: ResolvedToolBinding, context: ToolContext
    ) -> str | None:
        return await self._inner.destination_fingerprint(binding, context)

    async def execute(
        self,
        binding: ResolvedToolBinding,
        args: dict[str, Any],
        context: ToolContext,
        *,
        destination_fingerprint: str | None = None,
    ) -> ToolResult:
        result = await self._inner.execute(
            binding, args, context, destination_fingerprint=destination_fingerprint
        )
        kept = result.model_copy(update={"provider_metadata": {}})  # operational noise, not data
        if self._redact and kept.data is not None:
            kept = kept.model_copy(update={"data": _redacted(kept.data)})
        key = call_key(binding.tool.name, args)
        self.cassette[key].append(kept)
        self._names[key] = binding.tool.name
        return result

    def save(self, path: Path) -> None:
        entries = [
            {
                "tool": self._names[key],
                "key": key,
                "results": [r.model_dump(mode="json") for r in results],
            }
            for key, results in sorted(self.cassette.items())
        ]
        path.write_text(json.dumps(entries, indent=2, ensure_ascii=False), encoding="utf-8")


def load_cassette(path: Path) -> Cassette:
    entries = json.loads(path.read_text(encoding="utf-8"))
    return {e["key"]: [ToolResult.model_validate(r) for r in e["results"]] for e in entries}


class ReplayToolProvider:
    def __init__(self, cassette: Cassette) -> None:
        self._remaining: dict[str, deque[ToolResult]] = {
            key: deque(results) for key, results in cassette.items()
        }
        self.calls = 0

    async def destination_fingerprint(
        self, binding: ResolvedToolBinding, context: ToolContext
    ) -> str | None:
        return None  # there is no destination: nothing here can reach one

    async def execute(
        self,
        binding: ResolvedToolBinding,
        args: dict[str, Any],
        context: ToolContext,
        *,
        destination_fingerprint: str | None = None,
    ) -> ToolResult:
        self.calls += 1
        queue = self._remaining.get(call_key(binding.tool.name, args))
        if not queue:
            return ToolResult(
                status="technical_error",
                error=ToolError(
                    code="REPLAY_MISS",
                    message_safe="No recorded answer for this call.",
                    retryable=False,
                ),
            )
        return queue.popleft()
