"""Pre-registered external connections and secrets (DESIGN §30-31).

A connection is configuration owned by the operator, per tenant. Neither the LLM nor a tool
definition ever supplies a URL, a host, a credential or a header.
"""

from __future__ import annotations

import re
from urllib.parse import urlsplit

from pydantic import BaseModel, ConfigDict, SecretStr, field_validator

from conversation_agent.core.canonical import stable_hash


class _Frozen(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")


# Headers the RUNTIME owns: a credential must never overwrite them (a shared secret in
# `Idempotency-Key` would break INV-021; in `Host`/`X-*` it would forge identity or routing).
RUNTIME_OWNED_HEADERS: frozenset[str] = frozenset(
    {
        "host",
        "content-type",
        "content-length",
        "transfer-encoding",
        "connection",
        "idempotency-key",
        "x-tenant-id",
        "x-contact-id",
        "x-conversation-id",
        "x-trace-id",
        "x-invocation-id",
    }
)
_HEADER_NAME = re.compile(r"^[A-Za-z0-9!#$%&'*+.^_`|~-]+$")


class AuthSpec(_Frozen):
    """How to authenticate: the *reference* to a secret, never the secret itself."""

    secret_ref: str
    header: str = "Authorization"
    scheme: str | None = "Bearer"  # None -> the raw secret is the header value

    @field_validator("header")
    @classmethod
    def _header_is_not_runtime_owned(cls, value: str) -> str:
        if not _HEADER_NAME.fullmatch(value):
            raise ValueError(f"{value!r} is not a valid HTTP header name")
        if value.casefold() in RUNTIME_OWNED_HEADERS:
            raise ValueError(f"auth cannot use the runtime-owned header {value!r}")
        return value


class ResolvedConnection(_Frozen):
    """Secure by default: HTTPS only, no private/reserved destinations, bounded sizes."""

    connection_id: str
    base_url: str
    allowed_hosts: frozenset[str] = frozenset()  # empty -> only the host of `base_url`
    allow_private_networks: bool = False
    tls_required: bool = True
    max_timeout_seconds: float = 10.0
    max_request_bytes: int = 256 * 1024
    max_response_bytes: int = 1024 * 1024
    auth: AuthSpec | None = None
    # The contact's identifier (a phone number, often) is personal data: it reaches this external
    # system as `X-Contact-Id` only when the OPERATOR turns it on for this connection.
    send_contact_id: bool = False

    @field_validator("base_url")
    @classmethod
    def _base_url_is_only_where(cls, value: str) -> str:
        """scheme + host + port + base path, nothing else: userinfo, query and fragment are not
        part of the frozen destination (INV-027), so they cannot exist. Credentials come from the
        SecretProvider; query parameters from the tool's HTTP spec."""
        parts = urlsplit(value)
        if parts.scheme not in ("http", "https") or not parts.hostname:
            raise ValueError(f"base_url {value!r} must be an absolute http(s) URL with a host")
        if parts.username is not None or parts.password is not None or "@" in parts.netloc:
            raise ValueError("base_url cannot carry credentials (use auth/secret_ref)")
        if parts.query or "?" in value:
            raise ValueError("base_url cannot carry a query string (use the tool's query spec)")
        if parts.fragment or "#" in value:
            raise ValueError("base_url cannot carry a fragment")
        try:
            port = parts.port  # parsing the port is what raises on `:abc` and `:99999`
        except ValueError as exc:
            raise ValueError(f"base_url {value!r} has an invalid port: {exc}") from exc
        if port is not None and not 1 <= port <= 65535:
            raise ValueError(f"base_url port {port} is out of range (1-65535)")
        return value

    def fingerprint(self) -> str:
        """Identity of WHERE requests go and under which network/TLS policy (INV-027).

        Frozen at PREPARE and re-checked before every send. It contains no secret material: a
        credential can rotate without moving the operation; a changed host/port/path/policy
        cannot silently redirect an already-prepared operation to another system.
        """
        parts = urlsplit(self.base_url)
        return stable_hash(
            parts.scheme,
            parts.hostname,
            parts.port,
            parts.path.rstrip("/"),
            self.tls_required,
            sorted(self.allowed_hosts),
            self.allow_private_networks,
            [self.auth.header, self.auth.scheme, self.auth.secret_ref] if self.auth else None,
        )


class SecretValue:
    """A secret that cannot leak through repr/str/logging by accident."""

    __slots__ = ("_secret",)

    def __init__(self, value: str) -> None:
        self._secret = SecretStr(value)

    def reveal(self) -> str:
        return self._secret.get_secret_value()

    def __repr__(self) -> str:
        return "SecretValue(**********)"

    __str__ = __repr__
