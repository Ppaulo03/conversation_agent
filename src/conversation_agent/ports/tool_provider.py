from __future__ import annotations

from typing import Any, Protocol

from conversation_agent.core.definitions.binding import ResolvedToolBinding
from conversation_agent.core.models.tooling import ToolContext, ToolResult


class ToolProvider(Protocol):
    """Technical execution only. Normative contract: ADR-024 and `RUNTIME_PROTOCOL.md` §9.

    Result. Return a canonical `ToolResult` for every expected execution failure; do not raise
    (transport exceptions are converted, using the binding's `error_map` where it applies). If
    `execute` does raise, the framework is defensive (a write becomes `unknown`, a read a
    non-retryable `technical_error`), but that is a contract violation, not an API.

    Identity. `context.invocation_id` is the one logical identity of a tool invocation. The
    framework hands the SAME value to the first attempt, every technical retry, every
    reconciliation resend and every recovery after a restart. Never create a fresh dedupe identity
    per attempt.

    Idempotency. For a non-read tool with `idempotency_supported`, bind `context.invocation_id` to
    the external system's own deduplication/replay mechanism (a header, a token, a native field,
    ...; the mechanism is yours, no transport detail is prescribed). If the external protocol
    cannot carry the literal value, use a deterministic encoding of it: the same external identity
    on every attempt, no per-attempt randomness. `idempotency_supported` is an assertion that the
    external operation really dedupes/replays, for every resend the tool's `RetryPolicy` and
    `RecoverySpec` may perform; the framework cannot verify it.

    Classification of a write (effective risk other than `read`), by what is known:
      - proven that the effect did not happen -> `technical_error`; `retryable=True` only then
        (it means "this attempt did not produce the effect", not "a network error happened");
      - the system refused it as a known domain outcome -> `business_error` /
        `validation_error` / `policy_denied`, never transport ambiguity;
      - the request may have reached the system and the effect cannot be proven -> `unknown`,
        always, whatever caused it (timeout, reset, unusable answer), and even when the tool
        supports idempotency: idempotency lets recovery repeat the call safely, it does not turn
        an ambiguous observation into proof. Never report it as `technical_error` or `timeout`.
    `unknown` is never retried by the runner; it goes to the frozen `RecoverySpec`. An
    operator `error_map` rule is an assertion held to this same contract.
    """

    async def destination_fingerprint(
        self, binding: ResolvedToolBinding, context: ToolContext
    ) -> str | None:
        """Identity of the concrete destination this provider would use right now (no secrets),
        or None when the notion does not apply. Frozen at PREPARE (INV-027)."""
        ...

    async def execute(
        self,
        binding: ResolvedToolBinding,
        args: dict[str, Any],
        context: ToolContext,
        *,
        destination_fingerprint: str | None = None,
    ) -> ToolResult:
        """When `destination_fingerprint` is given and no longer matches, nothing is sent and
        the result is a technical_error `DESTINATION_CHANGED`."""
        ...
