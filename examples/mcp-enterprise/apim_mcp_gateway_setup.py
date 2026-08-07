import json, subprocess, urllib.request, urllib.error

import os
SUB = os.environ["AZURE_SUBSCRIPTION_ID"]   # the subscription holding your APIM instance
RG = os.environ["APIM_RG"]                  # its resource group
APIM = os.environ["APIM_NAME"]              # the APIM instance name
BASE = f"https://management.azure.com/subscriptions/{SUB}/resourceGroups/{RG}/providers/Microsoft.ApiManagement/service/{APIM}"
V = "2024-06-01-preview"
TOKEN = subprocess.check_output(["az", "account", "get-access-token", "--query", "accessToken", "-o", "tsv"]).decode().strip()


def arm(method, path, body=None):
    url = f"{BASE}{path}{'&' if '?' in path else '?'}api-version={V}"
    r = urllib.request.Request(url, method=method, data=json.dumps(body).encode() if body else None)
    r.add_header("Authorization", f"Bearer {TOKEN}"); r.add_header("Content-Type", "application/json")
    try:
        with urllib.request.urlopen(r, timeout=40) as x:
            return x.status, json.loads(x.read().decode() or "{}")
    except urllib.error.HTTPError as e:
        return e.code, e.read().decode()[:300]


def mock_policy(body_json):
    xml = (f'<policies><inbound><base /><return-response><set-status code="200" />'
           f'<set-header name="Content-Type" exists-action="override"><value>application/json</value></set-header>'
           f'<set-body>{json.dumps(body_json)}</set-body></return-response></inbound>'
           f'<backend><base /></backend><outbound><base /></outbound></policies>')
    return xml


TOOLS = {
    "search_knowledge_base": {"result": "Refunds within 14 days; contact billing@acme.example"},
    "get_customer_record": {"customer": "Dana Wexler", "plan": "Enterprise", "mrr": 42000, "ssn": "512-88-2231"},
    "create_support_ticket": {"ticket": "TICK-40211", "status": "open"},
    "query_analytics": {"active_users": 48210, "mrr": 1840000, "churn": "1.7%"},
    "read_document": {"doc": "DOC-onboarding", "content": "provision SSO, invite admins, import users"},
    "send_notification": {"sent": True},
}

# 1. REST operations + mock policies on the enterprise-tools API (already created)
for op, resp in TOOLS.items():
    s, _ = arm("PUT", f"/apis/enterprise-tools/operations/{op}",
               {"properties": {"displayName": op, "method": "POST", "urlTemplate": f"/{op}"}})
    s2, r2 = arm("PUT", f"/apis/enterprise-tools/operations/{op}/policies/policy",
                 {"properties": {"format": "rawxml", "value": mock_policy(resp)}})
    print(f"  op {op}: op={s} policy={s2}" + ("" if s2 < 300 else f" {r2[:80]}"))

# 2. Expose as an MCP server via the APIM MCP gateway
mcp_tools = [{"name": op, "description": f"Enterprise tool: {op}", "operationId": op} for op in TOOLS]
s, r = arm("PUT", "/apis/enterprise-mcp", {"properties": {
    "displayName": "Enterprise MCP", "path": "enterprise-mcp", "type": "mcp",
    "protocols": ["https"], "backendId": "enterprise-tools", "mcpTools": mcp_tools}})
print(f"\nMCP API create: {s}")
print(json.dumps(r, indent=2)[:600] if isinstance(r, dict) else r[:400])
