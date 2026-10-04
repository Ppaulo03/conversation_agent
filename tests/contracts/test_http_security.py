"""HTTPToolProvider hardening: per-tenant connections, secrets, SSRF, DNS rebinding, limits.
(DESIGN §30-31; ROADMAP Phase 3 "HTTPToolProvider hardened" + "ConnectionResolver/SecretProvider").
"""

from __future__ import annotations

import json
from typing import Any

import httpx
import pytest

from conftest import ApiHandle
from conversation_agent.adapters.connections.static import StaticConnectionResolver
from conversation_agent.adapters.secrets.providers import EnvSecretProvider, InMemorySecretProvider
from conversation_agent.adapters.tools.http import HTTPToolProvider, local_dev_connection
from conversation_agent.core.errors import ConnectionNotFoundError, SecretNotFoundError
from conversation_agent.core.models.connections import AuthSpec, ResolvedConnection, SecretValue
from vertical_slice.definitions import CONNECTION, build_agent

from .test_tool_provider_contract import CONTEXT, tool_args

OK_BODY = {"items": [], "pagination": {"next_cursor": None}}
SECRET = "s3cr3t-token-VALUE-12345"


def availability() -> Any:
    resolved = build_agent().resolve("scheduling.availability")
    assert resolved is not None
    return resolved


class Capture:
    """A MockTransport that records what would have gone on the wire."""

    def __init__(self, body: Any = OK_BODY, status: int = 200) -> None:
        self.requests: list[httpx.Request] = []
        self._body, self._status = body, status

    def client(self) -> httpx.AsyncClient:
        def handler(request: httpx.Request) -> httpx.Response:
            self.requests.append(request)
            return httpx.Response(self._status, json=self._body)

        return httpx.AsyncClient(transport=httpx.MockTransport(handler))


def resolver_of(*answers: list[str]):  # type: ignore[no-untyped-def]
    """A DNS stub returning successive answers; counts how often it is asked."""
    calls: list[str] = []
    queue = list(answers)

    async def resolve(host: str, port: int) -> list[str]:
        calls.append(host)
        return queue.pop(0) if len(queue) > 1 else queue[0]

    resolve.calls = calls  # type: ignore[attr-defined]
    return resolve


def provider(
    connection: ResolvedConnection,
    capture: Capture,
    dns: Any = None,
    secrets: Any = None,
) -> HTTPToolProvider:
    return HTTPToolProvider.static(
        {CONNECTION: connection}, secrets, capture.client(), dns or resolver_of(["93.184.216.34"])
    )


def strict(base_url: str = "https://api.example.com", **kw: Any) -> ResolvedConnection:
    return ResolvedConnection(connection_id=CONNECTION, base_url=base_url, **kw)


async def run(p: HTTPToolProvider, args: dict[str, Any] | None = None) -> Any:
    return await p.execute(availability(), args or tool_args(), CONTEXT)


# --- destination policy ---


async def test_a_public_https_destination_works_and_pins_the_validated_ip() -> None:
    cap = Capture()
    result = await run(provider(strict(), cap))
    assert result.status == "success"
    (request,) = cap.requests
    assert request.url.host == "93.184.216.34"  # connects to the address that was validated...
    assert request.headers["host"] == "api.example.com"  # ...but speaks to the original name
    assert request.extensions["sni_hostname"] == "api.example.com"  # and verifies TLS against it


@pytest.mark.parametrize(
    "address",
    [
        "10.0.0.5", "192.168.1.10", "172.16.0.9", "127.0.0.1", "169.254.169.254",  # metadata
        "0.0.0.0", "::1", "fe80::1", "fc00::1", "::ffff:10.0.0.1", "224.0.0.1",
    ],
)  # fmt: skip
async def test_private_reserved_and_metadata_destinations_are_refused(address: str) -> None:
    cap = Capture()
    result = await run(provider(strict(), cap, resolver_of([address])))
    assert result.status == "technical_error" and result.error.code == "DESTINATION_NOT_ALLOWED"
    assert cap.requests == []  # nothing was sent


async def test_one_bad_address_among_good_ones_rejects_the_destination() -> None:
    cap = Capture()
    result = await run(provider(strict(), cap, resolver_of(["93.184.216.34", "10.0.0.1"])))
    assert result.error.code == "DESTINATION_NOT_ALLOWED" and cap.requests == []


async def test_a_connection_can_explicitly_allow_private_networks() -> None:
    cap = Capture()
    result = await run(
        provider(strict(allow_private_networks=True), cap, resolver_of(["10.0.0.5"]))
    )
    assert result.status == "success" and cap.requests[0].url.host == "10.0.0.5"


async def test_dns_rebinding_cannot_redirect_the_request_after_validation() -> None:
    cap = Capture()
    dns = resolver_of(["93.184.216.34"], ["169.254.169.254"])  # 2nd answer would be the metadata IP
    result = await run(provider(strict(), cap, dns))
    assert result.status == "success"
    assert dns.calls == ["api.example.com"]  # resolved ONCE per request
    assert cap.requests[0].url.host == "93.184.216.34"  # the pinned, validated answer is used


async def test_a_dns_failure_is_a_safe_retryable_error_that_sent_nothing() -> None:
    async def broken(host: str, port: int) -> list[str]:
        raise OSError("name resolution failed")

    cap = Capture()
    result = await run(provider(strict(), cap, broken))
    assert result.status == "technical_error" and result.error.retryable
    assert cap.requests == []


async def test_http_is_refused_when_tls_is_required() -> None:
    cap = Capture()
    result = await run(provider(strict("http://api.example.com"), cap))
    assert result.error.code == "INSECURE_CONNECTION" and cap.requests == []


async def test_the_host_must_be_in_the_connection_allowlist() -> None:
    cap = Capture()
    other = strict(allowed_hosts=frozenset({"api.partner.com"}))  # base_url host is not allowed
    result = await run(provider(other, cap))
    assert result.error.code == "HOST_NOT_ALLOWED" and cap.requests == []
    ok = strict(allowed_hosts=frozenset({"api.example.com"}))
    assert (await run(provider(ok, Capture()))).status == "success"


async def test_redirects_are_never_followed_even_to_a_public_host() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(302, headers={"location": "http://169.254.169.254/latest/meta-data"})

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler), follow_redirects=True)
    p = HTTPToolProvider.static(
        {CONNECTION: strict()}, None, client, resolver_of(["93.184.216.34"])
    )
    result = await run(p)
    assert result.status == "technical_error" and result.error.code == "EXTERNAL_UNEXPECTED_STATUS"


# --- limits ---


async def test_oversized_responses_are_not_buffered_and_never_pass_as_success() -> None:
    cap = Capture(body={"items": ["x" * 5000], "pagination": {"next_cursor": None}})
    result = await run(provider(strict(max_response_bytes=1000), cap))
    assert result.status == "technical_error"  # a read; for a write this would be `unknown`
    assert result.error.code == "EXTERNAL_INVALID_RESPONSE"


async def test_an_oversized_request_is_refused_before_sending() -> None:
    resolved = build_agent().resolve("scheduling.create")
    assert resolved is not None
    cap = Capture()
    p = provider(strict(max_request_bytes=10), cap)
    args = {"service_code": "HC-01", "starts_at": "2026-10-06T13:00:00+00:00", "hours": 0.5}
    result = await p.execute(resolved, args, CONTEXT)
    assert result.error.code == "REQUEST_TOO_LARGE" and cap.requests == []


async def test_the_connection_can_tighten_the_timeout_but_a_tool_cannot_widen_it(
    api: ApiHandle,
) -> None:
    api.state.fault = {"delay": 1.0}
    p = HTTPToolProvider.static(
        {
            CONNECTION: local_dev_connection(api.base_url).model_copy(
                update={"max_timeout_seconds": 0.2}
            )
        }
    )
    result = await run(p)  # the tool asks for 5s; the connection caps it at 0.2s
    assert result.status == "timeout"


# --- per-tenant connections and secrets ---


async def test_each_tenant_uses_its_own_connection() -> None:
    cap = Capture()
    resolver = StaticConnectionResolver(
        {
            ("tenant-1", CONNECTION): strict("https://t1.example.com"),
            ("tenant-2", CONNECTION): strict("https://t2.example.com"),
        }
    )
    p = HTTPToolProvider(resolver, None, cap.client(), resolver_of(["93.184.216.34"]))
    await p.execute(availability(), tool_args(), CONTEXT)  # CONTEXT.tenant_id == "t1" -> no entry
    other = CONTEXT.model_copy(update={"tenant_id": "tenant-2"})
    await p.execute(availability(), tool_args(), other)
    assert [r.headers["host"] for r in cap.requests] == ["t2.example.com"]


async def test_a_tenant_without_the_connection_gets_a_safe_configuration_error() -> None:
    cap = Capture()
    p = HTTPToolProvider(StaticConnectionResolver({}), None, cap.client())
    result = await run(p)
    assert result.error.code == "CONNECTION_NOT_CONFIGURED" and cap.requests == []
    with pytest.raises(ConnectionNotFoundError):
        await StaticConnectionResolver({}).resolve("t", "c")


def authenticated() -> ResolvedConnection:
    return strict(auth=AuthSpec(secret_ref="erp-token"))


async def test_the_credential_is_injected_at_call_time_and_never_returned() -> None:
    cap = Capture()
    secrets = InMemorySecretProvider({(CONTEXT.tenant_id, "erp-token"): SECRET})
    result = await run(provider(authenticated(), cap, secrets=secrets))
    assert cap.requests[0].headers["authorization"] == f"Bearer {SECRET}"
    assert SECRET not in result.model_dump_json()


async def test_the_credential_never_leaks_through_error_results() -> None:
    cap = Capture(body={"detail": f"bad token {SECRET}"}, status=500)
    secrets = InMemorySecretProvider({(CONTEXT.tenant_id, "erp-token"): SECRET})
    result = await run(provider(authenticated(), cap, secrets=secrets))
    assert result.status == "technical_error"
    assert SECRET not in result.model_dump_json()  # neither the body echo nor the header


async def test_a_missing_credential_is_a_configuration_error_and_nothing_is_sent() -> None:
    cap = Capture()
    empty = InMemorySecretProvider({})
    for secrets in (empty, None):
        result = await run(provider(authenticated(), cap, secrets=secrets))
        assert result.error.code == "SECRET_NOT_AVAILABLE"
    assert cap.requests == []


async def test_a_custom_auth_header_without_a_scheme() -> None:
    cap = Capture()
    connection = strict(auth=AuthSpec(secret_ref="erp-token", header="X-Api-Key", scheme=None))
    secrets = InMemorySecretProvider({(CONTEXT.tenant_id, "erp-token"): SECRET})
    await run(provider(connection, cap, secrets=secrets))
    assert cap.requests[0].headers["x-api-key"] == SECRET


def test_secret_values_cannot_leak_through_repr_str_or_formatting() -> None:
    secret = SecretValue(SECRET)
    for rendered in (repr(secret), str(secret), f"{secret}", json.dumps({"s": str(secret)})):
        assert SECRET not in rendered
    assert secret.reveal() == SECRET


async def test_env_secret_provider_naming_and_absence(monkeypatch: pytest.MonkeyPatch) -> None:
    env = {"CA_SECRET__TENANT_1__ERP_TOKEN": SECRET}
    provider_ = EnvSecretProvider(env)
    assert (await provider_.get("tenant-1", "erp-token")).reveal() == SECRET
    with pytest.raises(SecretNotFoundError) as info:
        await provider_.get("tenant-2", "erp-token")  # another tenant cannot read it
    assert SECRET not in str(info.value)
