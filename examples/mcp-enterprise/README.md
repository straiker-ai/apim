# Enterprise MCP agent → APIM → Straiker

A realistic end-to-end test: a **real MCP server** exposing enterprise tools, driven by
**agentic workflows** whose every LLM call is routed **through Azure APIM** so Straiker sees
the full agentic trace with rich metadata.

```
enterprise_agent.py ──(OpenAI tool-calling loop)──▶ APIM (Straiker policy) ──▶ OpenAI
        │                                                   │
        └──(real MCP protocol, stdio)──▶ enterprise_mcp_server.py   └──▶ Straiker /detect?agentic
```

## What it exercises

- **Real MCP**: `enterprise_mcp_server.py` is a FastMCP stdio server with tools an internal
  agent would use — `search_knowledge_base`, `get_customer_record` (returns PII), `create_support_ticket`,
  `query_analytics`, `read_document`, `send_email`. The agent connects over the real MCP
  protocol, lists tools, and calls them.
- **Real agent architectures**: multi-turn tool-calling loops per scenario, multiple agent
  types (`support-agent`, `research-agent`, `ops-agent`) each routed to its **own themed APIM
  API → its own Straiker Console app**, multiple users and roles, and multi-agent traces
  sharing a `trace_id`.
- **Rich Straiker metadata** on every request: `x-session-id`, `x-user-name`, `x-user-role`,
  `x-trace-id`, `x-agent-role` → populates the Console's session/user/agent views.
- **Attack scenarios** mixed with benign work: prompt injection, **MCP tool-description
  poisoning** (a `mcp__vault__read_secret` tool whose description tells the model to exfil
  SSNs), **indirect injection** via a poisoned document returned by a tool, and **data
  exfiltration** (get a customer's SSN and email it out).

## Run

```bash
pip install "mcp>=1.0" openai
export OPENAI_API_KEY=sk-...
export APIM_SUB_KEY=<apim subscription key>
export APIM_BASE=https://<your-apim>.azure-api.net   # default: the dev instance
export ROUNDS=4                                       # scenarios x rounds
python enterprise_agent.py
```

Requires the themed agent APIs (`mcp-support`, `mcp-research`, `mcp-ops`) on the APIM
instance, each attaching the Straiker fragments with a distinct `straikerSource`. Without
them the agent falls back to the shared `/protected-agentic` route.

## What to look for in the Straiker Console

- Distinct app cards: `apim-dev-support-agent`, `apim-dev-research-agent`, `apim-dev-ops-agent`.
- Per-app **Activity** with many sessions, each showing the user, role, and the **agentic
  steps** (the MCP tool calls + results) inline.
- Detections on the attack scenarios (prompt injection, tool poisoning, indirect injection,
  exfiltration). Note: agentic apps run `llm_evasion` in detect mode — to hard-*block* these,
  front the traffic with a chatbot-mode app (`straikerAgentic=false`).
