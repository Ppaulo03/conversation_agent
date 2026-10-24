# Tool selector: external server contract (v1)

For the `http` provider of tool selection (ADR-028 for the filter, ADR-029 for this provider). A
server that ranks the tools relevant to a message; the framework asks it only when the allowed
tool list exceeds the agent's limits.

## Configuration

Agent manifest:

```yaml
tool_selection:
  enabled: true
  provider: http
  max_tools_per_call: 12          # and/or max_tool_tokens
  http:
    connection: tool-ranker       # a connection id the OPERATOR registered for the tenant
    path: /tool-selection         # default
    timeout_seconds: 2            # capped by the connection's max_timeout_seconds
```

The URL, host allowlist, TLS policy, size limits and the credential (a `secret_ref` resolved by the
`SecretProvider`, sent as the connection's auth header) live in the **connection**, exactly as for
tools: a manifest never carries a URL or a secret. The deployment passes the same
`GuardedTransport` it uses for tools (`Runtime.build(tool_selection_guard=...)`), so the checks
are the same: HTTPS unless the connection allows plain HTTP, SSRF and DNS-rebinding checks, no
redirects, bounded response.

## Request

```
POST {base_url}{path}
Content-Type: application/json
X-Tenant-Id: <tenant>
<auth header from the connection, e.g. Authorization: Bearer ...>

{"version": 1,
 "message": "quero marcar um corte amanhã",
 "tools": [{"name": "agenda_create_booking", "description": "..."}],
 "top_n": 8}
```

Only the message text and the candidate tools' names and descriptions leave the process: no
history, contact id, tool schemas or conversation id. **The user's text is sent to this server**:
register a connection only to a system you trust with it.

## Response

`200` with `{"version": 1, "scores": [{"name": "agenda_create_booking", "score": 0.93}]}`.

- `score` is in `[0, 1]`; a tool left out counts as 0.
- Names the server invents are ignored: the answer can only rank the tools it was sent.
- The framework applies its own `min_score` and `top_n` (`top_n` in the request is a hint).

## Failure

Anything else is a failed call, and the model gets the full tool list (never less than today):
non-200, redirect, another `version`, a body that does not match, a score outside `[0, 1]`, an
oversize body, a timeout, a refused or missing connection, an unavailable secret. Failures count as
`provider_error` in `tool_selections_total` and never carry the message text or a secret. The
decision (not the server's answer) is journaled, so a replay does not call the server again.

## Evolving

`version` is an integer. A server must answer with the version it was sent; a framework that
supports a new version keeps `1` until the manifest asks for another.
