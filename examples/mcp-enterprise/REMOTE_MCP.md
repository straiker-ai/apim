# Remote MCP server via the APIM MCP gateway (FQDN → Straiker MCP Scan)

The `enterprise_mcp_server.py` example is a **local stdio** MCP server — the agent runs its tool
calls, but Straiker genericizes them into "tools" because there is **no MCP server endpoint (FQDN)**
to attribute. The Straiker Console's **MCP Servers** inventory is populated by the **MCP Server Scan**
(Defend → MCP Servers → New Scan), which scans a real MCP server **by URL**.

`apim_mcp_gateway_setup.py` stands up a **real, remote MCP server behind Azure APIM's MCP gateway**,
so it has a real FQDN Straiker can scan — no external hosting.

## What it creates

1. A REST API `enterprise-tools` with 6 operations (`search_knowledge_base`, `get_customer_record`,
   `create_support_ticket`, `query_analytics`, `read_document`, `send_notification`), each returning
   a realistic mock response via an APIM `return-response` policy.
2. An **MCP-type API** `enterprise-mcp` (APIM MCP gateway) that exposes those 6 operations as MCP tools.

Run: `python apim_mcp_gateway_setup.py` (requires `az login` with contributor on the APIM RG).

## The MCP server endpoint (verified live)

```
https://<apim>.azure-api.net/enterprise-mcp/mcp     (streamable-http)
header: Ocp-Apim-Subscription-Key: <apim subscription key>
```

Verified: `initialize` → `serverInfo: "Azure API Management"`, and `tools/list` returns all 6 tools.

## Populate the Straiker MCP Servers view

In the Console: **Defend → MCP Servers → New Scan**, paste the endpoint URL above (with the
subscription key if the scan supports auth headers). Straiker scans the server, enumerates its tools,
and the MCP server appears in the **MCP Servers** inventory and on agent cards — because now there is
a real server FQDN to attribute (unlike the local stdio tools).

## Guard the MCP server with Straiker (optional)

Attach the Straiker fragments to the `enterprise-mcp` API policy the same way as any other API, so
MCP `tools/call` traffic is also sent to `/detect`. Note the MCP JSON-RPC body shape differs from
OpenAI Chat Completions, so tool-argument inspection benefits from the webhook contract (raw body →
Bridge) once webhook enforcement is available.
