"""Phase 5.4: a connection is only WHERE (scheme/host/port/path), and a credential can never
overwrite a header the runtime owns (INV-021, INV-027)."""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from conversation_agent.adapters.secrets.providers import InMemorySecretProvider
from conversation_agent.core.models.connections import AuthSpec, ResolvedConnection
from vertical_slice.definitions import CONNECTION, build_agent

from .test_http_security import SECRET, Capture, provider, strict
from .test_tool_provider_contract import CONTEXT

CREATE_ARGS = {"service_code": "HC-01", "starts_at": "2026-10-06T13:00:00+00:00", "hours": 0.5}


@pytest.mark.parametrize(
    "header",
    ["Idempotency-Key", "idempotency-key", "Host", "Content-Type", "X-Tenant-Id", "x-invocation-id",
     "X-Conversation-Id", "X-Trace-Id", "Content-Length", "Transfer-Encoding", "Connection"],
)  # fmt: skip
def test_auth_cannot_name_a_runtime_owned_header(header: str) -> None:
    with pytest.raises(ValidationError, match="runtime-owned"):
        AuthSpec(secret_ref="x", header=header)


@pytest.mark.parametrize("header", ["Bad Header", "X:Y", "", "Auth\nHeader"])
def test_auth_needs_a_valid_header_name(header: str) -> None:
    with pytest.raises(ValidationError, match="valid HTTP header"):
        AuthSpec(secret_ref="x", header=header)


async def test_even_a_forged_auth_spec_never_overwrites_the_idempotency_key() -> None:
    forged = AuthSpec.model_construct(secret_ref="erp-token", header="Idempotency-Key", scheme=None)
    cap = Capture()
    secrets = InMemorySecretProvider({(CONTEXT.tenant_id, "erp-token"): SECRET})
    connection = ResolvedConnection.model_construct(
        connection_id=CONNECTION,
        base_url="https://api.example.com",
        allowed_hosts=frozenset(),
        allow_private_networks=False,
        tls_required=True,
        max_timeout_seconds=10.0,
        max_request_bytes=256 * 1024,
        max_response_bytes=1024 * 1024,
        auth=forged,
    )  # bypasses validation on purpose: the adapter must defend itself too
    resolved = build_agent().resolve("scheduling.create")
    result = await provider(connection, cap, secrets=secrets).execute(
        resolved, CREATE_ARGS, CONTEXT
    )
    assert result.error is not None and result.error.code == "INVALID_AUTH_CONFIGURATION"
    assert cap.requests == []  # nothing was sent, and the secret never became a header value


async def test_a_normal_credential_and_the_idempotency_key_coexist() -> None:
    cap = Capture(body={"id": "b1", "state": "ok"})
    secrets = InMemorySecretProvider({(CONTEXT.tenant_id, "erp-token"): SECRET})
    connection = strict(auth=AuthSpec(secret_ref="erp-token"))
    resolved = build_agent().resolve("scheduling.create")
    await provider(connection, cap, secrets=secrets).execute(resolved, CREATE_ARGS, CONTEXT)
    sent = cap.requests[0].headers
    assert sent["idempotency-key"] == CONTEXT.invocation_id  # the runtime's, untouched
    assert sent["authorization"] == f"Bearer {SECRET}"


@pytest.mark.parametrize(
    ("url", "message"),
    [
        ("https://user:pass@api.example.com/v1", "credentials"),
        ("https://user@api.example.com/v1", "credentials"),
        ("https://api.example.com/v1?tenant=A", "query"),
        ("https://api.example.com/v1?", "query"),
        ("https://api.example.com/v1#frag", "fragment"),
        ("api.example.com/v1", "absolute"),
        ("ftp://api.example.com/v1", "absolute"),
        ("https:///v1", "absolute"),
    ],
)
def test_base_url_is_only_scheme_host_port_and_path(url: str, message: str) -> None:
    with pytest.raises(ValidationError, match=message):
        ResolvedConnection(connection_id="c", base_url=url)


def test_a_clean_base_url_is_accepted_and_fully_covered_by_the_fingerprint() -> None:
    a = ResolvedConnection(connection_id="c", base_url="https://api.example.com:8443/v1/")
    b = ResolvedConnection(connection_id="c", base_url="https://api.example.com:8443/v1")
    other = ResolvedConnection(connection_id="c", base_url="https://api.example.com:8443/v2")
    assert a.fingerprint() == b.fingerprint() != other.fingerprint()
