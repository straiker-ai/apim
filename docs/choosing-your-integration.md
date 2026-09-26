# Choosing your Azure + Straiker integration model

Two ways to put Straiker in front of Azure-hosted models. Pick based on where your model credentials must live and whether you already run APIM.

---

## Decision matrix

| Question | Custom APIM policy (this repo) | Straiker AI Gateway proxy |
|---|---|---|
| Where do my Azure model credentials live? | In **my** APIM (Named Value, Key Vault reference or managed identity) | In the **Straiker AI Gateway** |
| Do I need APIM? | Yes | No |
| Do I maintain policy XML? | Yes: fragments plus a short per-API policy | No: a base URL and a header |
| Call path | Client → APIM (policy + side call to Straiker) → my Azure OpenAI / Foundry | Client → Straiker AI Gateway → my Azure OpenAI / Foundry |
| Agentic, tool-calling and coding-agent traffic | Yes: the v3 fragments relay the whole conversation, tool calls and tool results | Chat traffic through the gateway |
| Azure AI Foundry Agent Service | Yes: messages and tool outputs guarded before they reach the thread | No |
| Blocking | A provider-shaped response from the policy (v3), or an HTTP error you choose | Set by the Straiker AI Gateway |
| Latency | One side call to Straiker per phase | One additional network hop |
| Best for | Keeping credentials, routing and governance in your own infrastructure | A quick evaluation when Straiker may hold the upstream credentials |

---

## Pattern A — custom APIM policy (this repo)

```
Client → APIM (Straiker fragments) → your Azure OpenAI / Foundry
                ↓ side call
            Straiker /api/v3/detect
```

Your APIM holds the upstream credentials. The fragments send the request (and, by default, the answer) to Straiker and act on the verdict. See [v3-platform.md](v3-platform.md) and the main [README](../README.md).

Use when:
- Compliance requires upstream credentials to stay in your infrastructure
- You already standardise on APIM for API governance (subscriptions, quotas, OAuth)
- You need agent, tool-call or coding-agent traffic inspected at the gateway

---

## Pattern B — Straiker AI Gateway proxy

```
Client → ai-gateway.prod.straiker.ai → your Azure OpenAI / Foundry
```

Point the OpenAI SDK (or an APIM backend) at `https://ai-gateway.prod.straiker.ai/app/<APP>/provider/<PROVIDER>` with an `x-straiker-api-key` header. The gateway runs detection and forwards the call using the provider credentials configured in the Straiker application.

Use when:
- You want the fastest evaluation, with no policy to deploy
- Straiker may hold the upstream model credentials
- You do not run APIM

---

## Pattern C — both (defense in depth)

```
Client → APIM (Straiker fragments) → ai-gateway.prod.straiker.ai → your Foundry
                ↓ side call
            Straiker /api/v3/detect
```

APIM governance plus the Straiker AI Gateway's routing, at the cost of one extra hop. Rarely worth the complexity outside regulated environments.

---

## Which pattern matches your situation?

| If you... | Use |
|---|---|
| Must keep model credentials in your own infrastructure | A |
| Already run APIM for API governance | A |
| Need agentic, tool-calling, coding-agent or Foundry Agent Service protection | A |
| Want a quick evaluation without deploying a policy | B |
| Do not run APIM and do not want to provision one | B |
| Want APIM policies and Straiker AI Gateway routing | C |
