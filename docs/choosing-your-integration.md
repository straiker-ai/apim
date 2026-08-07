# Choosing your Azure + Straiker integration model

Two production-ready paths exist for putting Straiker DefendAI in front of Azure-hosted models. Pick based on your trust model, your existing infrastructure, and how much code you want to own.

---

## Decision matrix

| Question | apim-policy-straiker (this repo) | Foundry Gateway proxy |
|---|---|---|
| Where do my Azure model keys live? | In **my** APIM (Named Value or managed identity) | In the **Straiker AI Gateway** |
| Do I already run APIM? | Required | Not required |
| Do I want custom policy XML I maintain? | Yes — full control | No — drop-in proxy, no XML |
| What's the call path? | Client → APIM (policy + side-call to /detect) → my Azure OpenAI / Foundry | Client → Azure Function → Straiker AI Gateway → my Azure OpenAI / Foundry |
| Can I use `/detect?agentic`? | Yes, native (this repo) | Indirect (the AI Gateway invokes its own internal detect) |
| Setup time | ~30 min APIM provision + `bicep` deploy | ~10 min `func` deploy + Straiker collection setup |
| Multi-model catalog (gpt-* + Mistral/Phi/etc)? | Build per-API in APIM (one Backend each) | One model catalog in the proxy |
| Customer-side blocking semantics | HTTP 403 with `turn_id` from `/detect` | `finish_reason: content_filter` set by Straiker AI Gateway |
| Latency overhead | One side-call to /detect (~80–150 ms) on pre-call | One additional hop through Straiker AI Gateway |
| Best for | Customers who must keep model creds in-house | Quick PoC + customers OK with Straiker holding upstream creds |

---

## Pattern A — apim-policy-straiker (custom policy)

```
Client → APIM (Straiker policy) → Customer's Azure OpenAI / Foundry
                ↓ side call
            Straiker /detect[?agentic]
```

This repo. Customer's APIM holds the upstream credentials. The Straiker policy XML calls `/api/v1/detect[?agentic]` and blocks with HTTP 403 when `score > threshold`.

Use when:
- Compliance/legal requires upstream credentials stay in customer infrastructure
- Customer is already standardised on APIM for API governance
- You want full control over policy logic (custom thresholds, header rewriting, multi-tenant routing)

Setup: see the main [README](../README.md).

---

## Pattern B — Foundry Gateway proxy (existing)

```
Client → Azure Function → ai-gateway.prod.straiker.ai → Customer's Azure OpenAI / Foundry
```

An Azure Function exposes `/v1/chat/completions` (direct) and `/v1/protected/chat/completions` (Straiker). Behind the protected endpoint, the OpenAI SDK is reconfigured with `base_url = https://ai-gateway.prod.straiker.ai/app/<APP>/provider/<PROVIDER>` and an `x-straiker-api-key` header. Straiker AI Gateway runs its own detection and forwards to the customer's Foundry resource using credentials configured in the Straiker app.

Use when:
- You want the fastest possible PoC (deploy in ~10 minutes)
- You're OK with Straiker holding the upstream model credentials
- You need built-in support for both Azure OpenAI (cognitiveservices) and Foundry third-party models (services.ai.azure.com/models) via one model catalog

---

## Pattern C — Defense in depth (advanced)

```
Client → APIM (Straiker policy) → ai-gateway.prod.straiker.ai → Customer's Foundry
                ↓ side call
            Straiker /detect[?agentic]
```

Run Pattern A's APIM policy in front of the Foundry Gateway from Pattern B. You get APIM's enterprise governance (subscriptions, quota, OAuth) plus the Straiker AI Gateway's built-in multi-model routing AND a side-channel `/detect` call for additional Console visibility. Cost of one extra hop; rarely worth the complexity outside of regulated-industry setups.

---

## Which pattern matches your situation?

| If you... | Use |
|---|---|
| Are integrating with a regulated-industry Azure customer who runs APIM | A (this repo) |
| Need a 10-minute Straiker PoC against Azure Foundry models | B (Foundry Gateway) |
| Want to run Straiker against `gpt-4.1`, Mistral, Phi, DeepSeek, and Grok with one config file | B (Foundry Gateway) |
| Need agentic / multi-turn / tool-calling protection at the gateway layer | A (this repo — `/detect?agentic` is native) |
| Want both APIM-level policies AND Straiker AI Gateway routing | C (defense in depth) |
| Don't have APIM and don't want to provision one | B (Foundry Gateway) |
