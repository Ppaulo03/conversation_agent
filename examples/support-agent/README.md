# support-agent

A customer-support agent (FAQ answers + support tickets): the **second domain** (Phase 9). It is only
data and exists to prove the framework is not shaped by scheduling:

- `agent.yaml`: the manifest. Capabilities `support.search_faq` (read) and `support.open_ticket`
  (write, needs confirmation), a deterministic `ticket` Flow (subject, priority, then a proposal;
  a failed creation hands the conversation to a person), bindings over an MCP server.
- `allowlist.yaml`: what this agent allows from that server. The server also offers `list_tickets`
  and `reopen_ticket`; they are never imported (discovery is not exposure). A test checks that the
  tools in `agent.yaml`, with their pinned schemas, are exactly what this allowlist imports.

The tools are served by `examples/reference-mcp` (an external system). Nothing about FAQs or tickets
exists in `src/conversation_agent` (the architecture test refuses that vocabulary there).

```bash
uv run python -m conversation_agent.app.compile examples/support-agent/agent.yaml
```
