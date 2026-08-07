# Agentic mode for the APIM Straiker policy

Same fragments, one config flag, completely different runtime behaviour. This doc explains what changes when you flip `straikerAgentic` to `true`, why the policy dedupes agent-loop iterations, and how to wire a second API for agentic traffic alongside your chatbot one.

---

## TL;DR

```xml
<!-- Chatbot API (single-turn) -->
<inbound>
  <base />
  <include-fragment fragment-id="straiker-defendai-inbound" />
</inbound>

<!-- Agentic API (multi-turn, tool-calling) -->
<inbound>
  <base />
  <set-variable name="straikerAgentic" value="@(true)" />
  <set-variable name="straikerSource"  value="my-agent-app" />   <!-- distinct Console app -->
  <include-fragment fragment-id="straiker-defendai-inbound" />
</inbound>
```

That's it. Same fragments, two API behaviours.

---

## What the `straikerAgentic` flag actually changes

| Behaviour | `straikerAgentic = false` (chatbot) | `straikerAgentic = true` (agentic) |
|---|---|---|
| Endpoint called | `POST /api/v1/detect` | `POST /api/v1/detect?agentic` |
| Payload shape | `prompt` + `app_response` (single strings) | full `messages[]` array |
| Tool calls | not transmitted | reshaped from OpenAI `function:{name,arguments:JSONstring}` → Straiker `{id,name,input:object}` |
| Pre-call fires when | every request | only iteration 1 of an agent loop (`messages[-1].role == "user"`) |
| Post-call fires when | every successful response | only the final iteration (response has no `tool_calls`) |
| Console grouping | one app, one turn pair per request | one app, one turn pair per **logical user prompt** (regardless of N agent iterations) |

The **`source`** variable should also change for agentic — it identifies the app in the Straiker Console. Pick a distinct value (e.g. `apim-dev-agentic` vs `apim-dev-chatbot`) so chatbot and agentic flows show up as separate apps in `https://app.straiker.ai/applications/defend/...`.

---

## Why dedup matters

An agent loop with N tool-calling iterations means N separate `/chat/completions` calls hit APIM:

```
User: "What is 17 * 19 + 23, and √144?"

iteration 1: messages = [system, user]
             → model returns tool_calls=[calculate, calculate]
iteration 2: messages = [system, user, assistant+tool_calls, tool, tool]
             → model returns tool_calls=[calculate]   (retry with different syntax)
iteration 3: messages = [system, user, assistant+tool_calls, tool, tool, assistant+tool_calls, tool]
             → model returns content="The answers are 346 and 12.0"
```

Without dedup, that single user prompt would fire **3 pre-call + 3 post-call detections** in Straiker → 6 noisy Console turns for one logical interaction.

The fragment's dedup rules:

| Phase | Skip when | Result |
|---|---|---|
| **Pre-call** (inbound) | `messages[-1].role` is `tool` or `assistant` | Skip iterations 2..N — pre-call only fires on iteration 1 (last role is `user`) |
| **Post-call** (outbound) | `response.choices[0].message.tool_calls` is non-empty | Skip iterations 1..(N-1) — post-call only fires on the final iteration |

Per logical user prompt: **exactly 1 pre-call + 1 post-call detection**, no matter how many iterations the agent runs. The final post-call's `messages[]` carries the entire conversation including every tool call and tool result, so Straiker has the full agent trace in one turn.

---

## Set up an agentic endpoint alongside chatbot

This mirrors the kong-plugin-demo's two-route setup. Customers can hit one API for chatbot traffic and another for agentic, both protected by the same fragments.

### One-time: create the agentic API

```bash
APIM=<your-apim-name>; RG=<your-rg>; SUB=$(az account show --query id -o tsv)

# 1. Create the API
az rest --method put \
  --uri "https://management.azure.com/subscriptions/$SUB/resourceGroups/$RG/providers/Microsoft.ApiManagement/service/$APIM/apis/openai-protected-agentic?api-version=2023-05-01-preview" \
  --body '{"properties":{"displayName":"openai-protected-agentic","path":"protected-agentic","protocols":["https"],"serviceUrl":"https://api.openai.com","subscriptionRequired":true}}'

# 2. Add the chat-completions operation
az rest --method put \
  --uri "https://management.azure.com/subscriptions/$SUB/resourceGroups/$RG/providers/Microsoft.ApiManagement/service/$APIM/apis/openai-protected-agentic/operations/chat-completions?api-version=2023-05-01-preview" \
  --body '{"properties":{"displayName":"POST chat/completions","method":"POST","urlTemplate":"/v1/chat/completions"}}'

# 3. Apply the agentic policy (uses the same fragments, with agentic=true)
az rest --method put \
  --uri "https://management.azure.com/subscriptions/$SUB/resourceGroups/$RG/providers/Microsoft.ApiManagement/service/$APIM/apis/openai-protected-agentic/policies/policy?api-version=2023-05-01-preview" \
  --body "{\"properties\":{\"format\":\"rawxml\",\"value\":$(jq -Rs '.' policy/examples/agentic-api.xml)}}"
```

Now you have two endpoints on the same APIM:
- `/protected/v1/chat/completions` — chatbot
- `/protected-agentic/v1/chat/completions` — agentic

Both use `straiker-defendai-inbound` + `straiker-defendai-outbound` fragments. Both read the same `straiker-api-key` Named Value. Different runtime behaviour controlled entirely by `<set-variable name="straikerAgentic" value="@(true)" />` in the agentic API's policy.

### What the agentic API's policy looks like in full

`policy/examples/agentic-api.xml`:

```xml
<policies>
  <inbound>
    <base />
    <set-variable name="straikerAgentic" value="@(true)" />
    <set-variable name="straikerSource"  value="apim-dev-agentic" />
    <set-variable name="straikerDestination" value="api.openai.com" />
    <include-fragment fragment-id="straiker-defendai-inbound" />
  </inbound>
  <backend><base /></backend>
  <outbound>
    <base />
    <include-fragment fragment-id="straiker-defendai-outbound" />
  </outbound>
  <on-error><base /></on-error>
</policies>
```

That's the entire delta from chatbot. Two `<set-variable>` lines.

---

## Test it

```bash
APIM_GATEWAY_URL=https://<your-apim-name>.azure-api.net \
APIM_SUBSCRIPTION_KEY=<from-portal> \
APIM_PATH_PREFIX=protected-agentic \
OPENAI_API_KEY=sk-... \
python3 tests/agentic_test.py
```

The test client runs 9 scenarios with 4 real tools (web_search, web_fetch, rag_search, calculate). Each scenario is one logical user prompt that may trigger several agent iterations.

Sample run output (verified 2026-05-07 against the dev APIM):

```
-- scenario 02 : carol@acme.com : session=agentic-suite-...-s02-carol --
user[1]> What is 17 * 19 + 23, and √144? Use the calculator.
  [iter 1] -> APIM/OpenAI (2 messages)
     tool_call -> calculate({"expression": "17 * 19 + 23"})
     tool_result <- {"result": 346}
     tool_call -> calculate({"expression": "sqrt(144)"})
     tool_result <- {"error": "expression contains disallowed characters"}
  [iter 2] -> APIM/OpenAI (5 messages)
     tool_call -> calculate({"expression": "144**0.5"})
     tool_result <- {"result": 12.0}
  [iter 3] -> APIM/OpenAI (7 messages)
agent[1]> The result of 17 × 19 + 23 is 346. The square root of 144 is 12.0.
```

3 iterations, 3 tool calls. In the Straiker Console under the `apim-dev-agentic` app, you should see **exactly 2 turns** for this scenario (1 pre + 1 post). The post-call turn's `messages[]` field carries all 7 messages including every tool invocation.

---

## What you'll see in Straiker Console

| App in Console | Source flag | What it shows |
|---|---|---|
| `apim-dev-chatbot` (or whatever you named the chatbot app) | `straikerSource = "apim-dev-chatbot"` | One pre + one post per chat request, scored as single-turn |
| `apim-dev-agentic` (created automatically when first agentic call lands) | `straikerSource = "apim-dev-agentic"` | One pre + one post per logical user prompt, full agent trace in the post-call's messages[] |

Filter by `session_id` in the Console to see all turns from one conversation grouped together.

---

## Headers the agent test client sends

The test client (`tests/agentic_test.py`) sets these headers on every `/chat/completions` call. The fragment maps them into the Straiker `metadata` envelope:

| Header | Maps to | Example |
|---|---|---|
| `x-user-name` | `metadata.user_name` | `alice@acme.com` |
| `x-session-id` | `metadata.session_id` | `agentic-suite-1778205650-s01-alice` |
| `x-user-role` | `metadata.user_role` | `demo` |

Set `x-trace-id` and `x-agent-role` if you want to correlate multi-agent flows (e.g. researcher hop → writer hop) in the Console.

---

## Production tuning: LLM Evasion on agentic

`LLM Evasion` is one of the agentic detection categories. It scores user inputs based on signals associated with prompt-injection / jailbreak attempts — but the signal is shaped for chatbot-style single-turn inputs, not for the user turns of an agent loop. **In Block mode on agentic apps, it over-fires on benign content** like "What is 17 * 19?" or "What is the capital of France?".

Recommended defaults:

| App type | LLM Evasion mode |
|---|---|
| Chatbot (`/api/v1/detect`) | **Block** — well-tuned for single-turn inputs |
| Agentic (`/api/v1/detect?agentic`) | **Detect** — keeps the signal in Activity for review without false-positive blocks |

You can flip it on for adversarial testing in a non-prod app (which is what we did during integration verification). For real customer-facing deployments, leave LLM Evasion in Detect on agentic apps until the agentic-aware tune is shipped.

The other agentic categories (PII subcategories, Tool Misuse, Resource Exhaustion, Data Exfiltration) are safe to enable in Block mode on agentic apps from day one.

## Common gotchas

1. **Source must be distinct from your chatbot app.** If both apps use `straikerSource = "apim-dev-chatbot"`, agentic and chatbot turns mix together in one Console app and you can't tell them apart. Pick `apim-dev-agentic` (or anything else) for the agentic API.
1. **Renaming a Console app does NOT rebind its source.** The `source` string → app mapping is fixed at creation; after a display-name rename, the next agentic call with a *new* `straikerSource` value auto-creates a *new* app (verified 2026-07-22 — renames produced duplicate apps until the old ones were deleted). To rename cleanly: pick the new `straikerSource`, let the first call auto-create the app, re-apply its control settings (they start from tenant defaults — detect mode), then delete the old app.
2. **Agentic detection controls might differ from chatbot controls.** The Straiker Console treats agentic apps as a separate app type. Enable `tool_misuse`, `data_exfiltration`, `excessive_agency` controls on the agentic app — they're the categories that fire on tool actions, not the chatbot prompt-injection / PII categories.
3. **Streaming (`stream: true`) is skipped.** Pre-call still fires, but post-call is skipped because SSE bodies aren't JSON-parseable. Same limitation as Kong.
4. **Agent loops outside the gateway need session correlation.** If your agent runs client-side (which is normal), set a stable `x-session-id` so all iterations of one logical user prompt land under the same Console session. Without it, each iteration looks like a fresh conversation.

---

## Jailbreak/evasion blocking: chatbot vs agentic (important)

Verified on Azure 2026-07-25 across all routes: **`LLM Evasion` (jailbreak / DAN / prompt-injection *text*) hard-blocks on chatbot apps but is downgraded to detect-only on agentic apps** — the `/api/v1/detect?agentic` endpoint returns `score_block=0` for `llm_evasion` even when the app's control is set to Block. This is deliberate (LLM Evasion over-fires on benign agent content), but it means:

| Route | `straikerAgentic` | DAN/jailbreak user prompt | Blocks via |
|---|---|---|---|
| Chatbot (e.g. `/protected`) | `false` | **403 at the gateway** | `llm_evasion` (block) |
| Agentic (e.g. `/protected-agentic`, Foundry) | `true` | not blocked by `llm_evasion`; model usually self-refuses (200) | agentic controls on the *tool trace* (tool misuse, data exfil, indirect prompt injection) + downstream model |

**Guidance for customers:** if the requirement is to hard-block adversarial *prompt text* at the gateway, front it with a **non-agentic (chatbot) app** (`straikerAgentic=false`), which blocks `llm_evasion` in Block mode. Agentic apps are scoped to catch agentic-shaped risks (a tool call that exfiltrates, an indirect injection in tool output) — enable those controls in Block mode and treat single-turn jailbreak text as detect/observe. Where the upstream is Azure OpenAI or Foundry, the provider's own content filter is a second layer (observed returning `400` on jailbreak prompts that Straiker allowed through on the agentic path).

## Attachments / multimodal

The **webhook contract** fragments (`straiker-webhook-*.xml`) forward image/file attachments; the rich `/detect` fragments do not (they extract text only and drop non-text content parts). Verified on Azure 2026-07-25: a multimodal request (`content: [{type:text}, {type:image_url, image_url:{url:"data:image/png;base64,…"}}]`) is forwarded through APIM to the upstream vision model, and the raw body — image bytes included — reaches Straiker.

<Warning>
**Image attachments are not inspected through APIM.** The rich contract drops non-text content parts, and the webhook contract is a preview that forwards them without inspection. The size budget below only makes sure an image reaches Straiker intact.
</Warning>

Key detail: a single real image is easily >512 KB once base64-encoded, which would trip the default `straikerMaxBodyBytes` (512 KiB) and, under the default `straikerOversizePolicy=bypass`, silently skip detection. The webhook fragment therefore detects attachment content parts and applies a larger `straikerMaxImageBytes` budget (default **4 MB**) instead. A ~1.96 MB image was forwarded end-to-end through APIM without being skipped. Notes:
- Straiker caps attachments at 5 MB each / 10 per request and downscales images to 384 px, so there is no benefit to forwarding enormous images.
- APIM buffers the whole body in policy memory (no stream/tempfile spill like Kong), so very large multi-MB bodies add latency/memory pressure per request — size `straikerMaxImageBytes` deliberately.
- The rich `/detect` contract cannot carry inline images (that API takes attachments only via multipart or server-side extraction), so **use the webhook contract for any multimodal route.**

## Multi-provider compatibility

The Straiker fragments parse OpenAI Chat Completions JSON shape. Anything that exposes that contract works through the same fragments — verified end-to-end against an Azure APIM instance:

| Provider | Endpoint | Verified |
|---|---|---|
| OpenAI | `api.openai.com/v1/chat/completions` | ✅ |
| Azure OpenAI (gpt-*) | `<resource>.cognitiveservices.azure.com/openai/deployments/<id>/chat/completions` | ✅ same shape as OpenAI |
| Azure AI Foundry — Mistral-Large-3 | `<resource>.services.ai.azure.com/models/chat/completions` | ✅ re-verified 2026-07-22 |
| Azure AI Foundry — Phi-4 | same as above | ✅ re-verified 2026-07-22 — slow: adversarial prompts can exceed 170s upstream; size client timeouts accordingly |
| Azure AI Foundry — DeepSeek-V3-0324 | same as above | ⚠️ was verified May 2026; the Foundry deployment now returns `410 model_deprecated` (Azure retired it) |
| Azure AI Foundry — grok-3 | same as above | ⚠️ `410 model_deprecated` (Azure retired the deployment before we could verify) |
| Anthropic Messages API | `api.anthropic.com/v1/messages` | ❌ different message shape — needs separate handling |
| Anthropic via Bedrock | same Anthropic shape | ❌ same |
| Google Gemini native | `generativelanguage.googleapis.com/v1beta/models/...:generateContent` | ❌ different shape |

For Foundry third-party models, see [`policy/examples/foundry-third-party-api.xml`](../policy/examples/foundry-third-party-api.xml). The only deltas from the OpenAI-agentic API policy are:
1. Backend URL points at `services.ai.azure.com/models` instead of `api.openai.com`
2. `<rewrite-uri template="/chat/completions" />` to strip the `/v1` prefix Foundry doesn't use
3. `<set-query-parameter name="api-version" exists-action="override"><value>2024-05-01-preview</value></set-query-parameter>` (Foundry requires it)
4. Distinct `straikerSource` so the Console shows Foundry traffic as its own app

The Straiker fragments themselves (`straiker-defendai-inbound`, `straiker-defendai-outbound`) are **unchanged across providers**. That's the win — one shared guardrail, multiple LLM backends.

## Cross-gateway parity check

Run the same prompts through APIM (`tests/agentic_test.py`) and through the Straiker Kong plugin and LiteLLM guardrail. The Straiker Console should produce equivalent verdicts and turn counts across all three for each prompt — proves payload shape and dedup logic match.
