"""Pre-registered external connections and secrets (DESIGN §30-31).

A connection is configuration owned by the operator, per tenant. Neither the LLM nor a tool
definition ever supplies a URL, a host, a credential or a header.
"""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, SecretStr


class _Frozen(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")


class AuthSpec(_Frozen):
    """How to authenticate: the *reference* to a secret, never the secret itself."""

    secret_ref: str
    header: str = "Authorization"
    scheme: str | None = "Bearer"  # None -> the raw secret is the header value


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
