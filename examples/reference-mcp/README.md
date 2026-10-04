# reference-mcp

A reference **MCP server** (Streamable HTTP, JSON-RPC 2.0) with a small FAQ/ticket toolset. It is an
external system: it knows nothing about `conversation_agent` and the framework never imports it.
It exists so the MCP provider, discovery and the allowlist can be exercised over real HTTP.

- `POST /mcp`: `initialize` (opens a session, `Mcp-Session-Id`), `notifications/initialized`,
  `tools/list` (paginated on request), `tools/call`;
- tools: `search_faq` (read), `create_ticket` (write, idempotent by `Idempotency-Key`),
  `list_tickets` (read) and `reopen_ticket` (write); the last two are offered but meant NOT to be
  allowed: discovery is not exposure;
- test controls on `app.state`: `tools` (mutate a schema to simulate a server that changes under
  you), `sse`, `page_size`, `next_result`, `call_fault`, `protocol_version`, `sessions`.
