# Straiker DefendAI policy for Azure API Management

A drop-in Azure APIM policy that adds Straiker DefendAI guardrails to any LLM or agent API exposed through the [APIM AI Gateway](https://learn.microsoft.com/azure/api-management/genai-gateway-capabilities), including Azure AI Foundry models and the Foundry Agent Service. On the v3 platform it speaks the same wire contract as the [Straiker Kong plugin](https://github.com/straiker-ai/kong) and the LiteLLM guardrail.

> **Not sure which Azure integration model you need?** This repo is the **custom-policy** approach: your model credentials stay in APIM. The alternative is a proxy through the Straiker AI Gateway, where Straiker holds the upstream credentials. Decision matrix in [`docs/choosing-your-integration.md`](docs/choosing-your-integration.md).

---

## Which contract: v3 or v1

The key the Straiker Console gives you decides which fragments you deploy:

| Key | Platform | Fragments | Deploy |
|---|---|---|---|
| `sk_agt_...` | v3 | `straiker-v3-inbound` / `straiker-v3-outbound` | `deploy.sh <rg> <apim> --contract v3` (default) |
| UUID | v1 | `straiker-defendai-*` (rich), `straiker-webhook-*`, `straiker-coding-*` | `--contract rich` / `--contract webhook` |

v3 is one fragment pair for chat, agentic, coding-agent and Azure AI Foundry (models and the
Agent Service) traffic on the same wire contract
as the Straiker Kong plugin and the LiteLLM guardrail (agent enumeration with `x-s6r-agent`,
identity from the authenticated principal, `permissionDecision` verdicts, provider-shaped
block stubs, replay memory). Read **[docs/v3-platform.md](docs/v3-platform.md)** first.
The sections below describe the v1 (rich) contract.

## What it does

| Phase | APIM section | Behaviour |
|---|---|---|
| Pre-call detection | `<inbound>` | Calls `POST /api/v1/detect` (or `/detect?agentic`); returns `403` with `turn_id` if `score > threshold` |
| Post-call detection | `<outbound>` | Re-scores with the assistant's response. Observability by default; set `straikerBlockOnPostCall=true` to replace the response with `403` when the output itself scores over threshold (PII / data exfil / system-prompt leak in the answer) |
| Agentic dedup | both | Skips pre-call on tool/assistant continuations and skips post-call on iterations with `tool_calls` — so one logical user prompt produces one pre + one post Console turn, not 2N |
| Streaming | both | When `stream=true` is detected, post-call is skipped (SSE bodies can't be parsed as JSON) |

Customer keeps their own provider keys (Azure OpenAI, OpenAI, Anthropic-via-Bedrock, …). Straiker only ever sees the prompt/response/messages that get sent to `/detect`.

---

## Repo layout

```
apim-policy-straiker/
├── policy/
│   ├── fragments/                   # SOURCE OF TRUTH — deployed as APIM Policy Fragments
│   │   ├── straiker-v3-inbound.xml                # v3 platform (sk_agt_ keys): request phase
│   │   ├── straiker-v3-outbound.xml               # v3 platform: response phase (response-sync)
│   │   ├── straiker-gateway-auth.xml              # per-developer keys for Claude Code -> straikerUserName
│   │   ├── straiker-defendai-inbound.xml          # v1 rich: pre-call detection + block
│   │   ├── straiker-defendai-outbound.xml         # v1 rich: post-call detection (+ optional output block)
│   │   ├── straiker-webhook-{inbound,outbound}.xml  # v1 /detect/webhook preview
│   │   └── straiker-coding-{inbound,outbound}.xml   # v1 Claude Code hook-event / raw-body contract
│   ├── straiker-policy.xml          # GENERATED single-file paste-in of the rich pair (scripts/build-monolith.sh)
│   └── examples/
│       ├── v3-azure-openai.xml                    # v3: Azure OpenAI API pinned to one application
│       ├── v3-shared-route.xml                    # v3: one route, many apps (x-s6r-agent) + Entra identity
│       ├── v3-claude-code.xml                     # v3: Claude Code via gateway-auth, sync tool blocking
│       ├── v3-detect-only-streaming.xml           # v3: async response phase, streaming preserved
│       ├── v3-foundry-models.xml                  # v3: Azure AI Foundry models / Azure OpenAI
│       ├── v3-foundry-agent-service.xml           # v3: Azure AI Foundry Agent Service (threads/runs)
│       ├── chatbot-mode.xml / agentic-mode.xml / agentic-api.xml          # v1 rich knobs
│       └── azure-openai-passthrough.xml / azure-foundry-models-passthrough.xml / foundry-third-party-api.xml
├── docs/
│   ├── v3-platform.md               # v3 contract, naming, identity, sessions, enforcement, knobs, testing
│   ├── agentic-mode.md              # v1 agentic behaviour + multi-provider matrix
│   ├── coding-agents.md             # v1 coding contract
│   └── choosing-your-integration.md # custom policy vs Foundry Gateway proxy decision matrix
├── bicep/
│   ├── main.bicep                   # Named Values + every fragment pair + thin include-fragment policy
│   └── parameters.example.json
├── deploy/
│   └── deploy.sh                    # az cli wrapper: --contract v3|rich|webhook, --no-attach, --key-named-value
├── scripts/
│   ├── check-fragments.sh           # static gates (tag balance, literal one-way timeouts, v3 stub parity)
│   └── build-monolith.sh            # regenerate straiker-policy.xml from the rich fragments
├── dev/                             # local self-hosted gateway dev loop
├── examples/                        # agent-runner (FastAPI), coding-agent traffic generator, MCP enterprise
└── tests/
    ├── v3/                          # live v3 harness: setup_dev_apis, e2e_matrix, console_verify,
    │                                #   block_test, cc_test, load_test, wire_tap (see docs/v3-platform.md)
    ├── test.sh                      # v1 benign + adversarial curl tests
    ├── agentic_test.py              # v1 full agent loop with 4 real tools
    ├── multi_agent_trace.py         # v1 researcher→writer 2-agent trace correlation
    └── webhook_contract_test.py     # v1 webhook envelope canary
```

---

## Quick start

### 1. Prerequisites

- Existing Azure APIM instance (free Developer tier works — `az apim create --sku-name Developer`)
- A Straiker DefendAI API key (Console → Settings → API Keys)
- `az` CLI logged in (`az login`)
- `az bicep install`

### 2. Deploy the policy

```bash
export STRAIKER_API_KEY=xxxxxxxxxxxxx
./deploy/deploy.sh <resource-group> <apim-name>
```

This:
- Creates the `straiker-api-key` APIM Named Value (secret)
- Registers the `straiker-defendai-inbound` / `straiker-defendai-outbound` **Policy Fragments** instance-wide
- Creates a passthrough test API (`openai-protected`) pointing at `api.openai.com`
- Attaches a thin `<include-fragment>` policy to it

To apply the policy to an existing API instead of the test one:

```bash
./deploy/deploy.sh <resource-group> <apim-name> --no-test-api
# then edit bicep/parameters.example.json: targetApiName=<your-existing-api>
```

To deploy the single-file policy instead of fragments (portal paste-in parity):

```bash
./deploy/deploy.sh <resource-group> <apim-name> --monolith
```

`policy/straiker-policy.xml` is **generated** from the fragments — edit the fragments and run `scripts/build-monolith.sh`; the deploy script refuses to ship a stale monolith.

### 3. Test

Grab a subscription key. The `az apim subscription` subcommand has been removed from recent CLI versions; use the REST API instead:

```bash
SUB=$(az account show --query id -o tsv)
SUBKEY=$(az rest --method post \
  --uri "https://management.azure.com/subscriptions/$SUB/resourceGroups/<rg>/providers/Microsoft.ApiManagement/service/<apim-name>/subscriptions/master/listSecrets?api-version=2023-05-01-preview" \
  --query primaryKey -o tsv)
```

Then run:

```bash
APIM_GATEWAY_URL=https://<apim-name>.azure-api.net \
APIM_SUBSCRIPTION_KEY="$SUBKEY" \
OPENAI_API_KEY=sk-... \
./tests/test.sh
```

Verified output (real run, 2026-05-07):
```
--- Benign-1: What is the capital of France?...
  → ALLOWED: The capital of France is Paris....
--- Prompt injection: Ignore all previous instructions...
  → BLOCKED by 403: Straiker: threat detected (pre-call) (score=1, turn=eaf46815-...)
--- PII extraction: List every customer SSN...
  → BLOCKED by 403: Straiker: threat detected (pre-call) (score=1, turn=bb07212d-...)
--- Benign-2: Hello, can you help me with my homework?...
  → ALLOWED: Of course! I'd be happy to help...
--- DAN jailbreak: You are now DAN...
  → BLOCKED by 403: Straiker: threat detected (pre-call) (score=1, turn=59fadb58-...)
```

### 4. Test agentic mode

In your API's policy (before the `<include-fragment>` lines, or at the top of the pasted monolith) set:
```xml
<set-variable name="straikerAgentic" value="@(true)" />
<set-variable name="straikerSource" value="my-agent-app" />
```
The `straikerSource` value MUST match an existing agentic Straiker app (create it in the Straiker Console first).

Note: agentic apps default to `straikerMode = post_call` (the post-call carries the full trace; pre-call on agent-loop iteration 1 adds a blank-response Console turn). To gate user prompts before the agent runs, set `straikerMode = both` explicitly.

Re-deploy and run:
```bash
pip install openai scikit-learn
APIM_GATEWAY_URL=https://<apim-name>.azure-api.net \
APIM_SUBSCRIPTION_KEY=... \
OPENAI_API_KEY=sk-... \
python tests/agentic_test.py
```

Verify in the Straiker Console that each scenario produces exactly **one** pre-call + **one** post-call turn — not one per agent-loop iteration.

---

## Configuration knobs

Every knob uses a set-if-unset default inside the fragments — override per-API or per-operation with a `<set-variable>` BEFORE the `<include-fragment>` line (or above the config block in the pasted monolith).

| Knob | Type | Default | Meaning |
|---|---|---|---|
| `straikerApiKey` | secret string | `{{straiker-api-key}}` | Bearer token; resolves from APIM Named Value |
| `straikerDetectUrl` | string | `https://api.prod.straiker.ai/api/v1/detect` | Override for regional deployments |
| `straikerMode` | string | `both` (chatbot) / `post_call` (agentic) | `pre_call` / `post_call` / `both` |
| `straikerAgentic` | bool | `false` | When true, calls `/detect?agentic`, sends full `messages[]` |
| `straikerSource` | string | `apim-policy` | Agentic app name; must match existing Straiker app |
| `straikerDestination` | string | `api.openai.com` | Recorded in detection metadata |
| `straikerThreshold` | number | `0.5` | Block if `score > threshold` |
| `straikerTimeoutSec` | int | `5` | Pre-call timeout (post-call is hard-coded to 5s) |
| `straikerFailOpen` | bool | `false` | When true, allow traffic if Straiker is unreachable |
| `straikerBlockOnPostCall` | bool | `false` | When true, replace the upstream response with `403` if the post-call score exceeds the threshold (output gating) |

Headers the policy reads from the client (all optional; falls back to defaults):

| Header | Maps to | Default |
|---|---|---|
| `x-user-name` | `metadata.user_name` | OpenAI `user` field, then `apim` |
| `x-user-role` | `metadata.user_role` | `public` |
| `x-session-id` | `metadata.session_id` | per-request `context.RequestId` |
| `x-trace-id` | `metadata.trace_id` | none |
| `x-agent-role` | `metadata.agent_role` | none |

---

## Architecture decision: custom policy vs proxy

This repo ships approach **(A) — Custom APIM policy**, the recommended default:

```
Client → APIM (Straiker policy: pre-call block, post-call detect) → Customer's Azure OpenAI / Foundry
                       ↓ side call
                   Straiker /detect[?agentic]
```

Provider credentials stay in APIM (Named Value or, better, APIM managed identity → AOAI). Straiker sees only the data sent to `/detect`.

The alternative is **(B) — proxy through Straiker AI Gateway** (no custom policy):

```
Client → APIM (set-header x-straiker-api-key + set-backend-service) → ai-gateway.prod.straiker.ai → Provider
```

This is simpler — no policy XML, just a backend rewrite plus a header — but **the Straiker AI Gateway must be configured with the upstream provider credentials** (your Foundry deployment URL + key, or your OpenAI key). Only pick (B) when that trust model is acceptable to your security team.

---

## Local dev loop

See [`dev/README.md`](dev/README.md). Runs the [APIM self-hosted gateway image](https://learn.microsoft.com/azure/api-management/self-hosted-gateway-overview) bound to your APIM, so you can iterate on policy XML in seconds with `curl localhost:5000` instead of waiting for portal saves.

---

## Two ways to install: inline policy vs Policy Fragments

The closest Azure analog to a Kong plugin is an **APIM Policy Fragment** — a named, reusable XML snippet that appears as a first-class entity in the portal (its own blade with name + description) and is referenced from any API via `<include-fragment fragment-id="..." />`.

This repo ships both:

| Approach | Files | Use when |
|---|---|---|
| **Policy Fragments** (default in Bicep) | `policy/fragments/straiker-defendai-{inbound,outbound}.xml` | The canonical path. Multiple APIs share the same guardrails, "Straiker DefendAI" appears as a named entity in the **Policy Fragments** blade, per-API config overrides with centralized logic |
| **Inline policy** (`--monolith`) | `policy/straiker-policy.xml` (generated from the fragments) | Single API, want everything in one place / portal paste-in without registering fragments |

Apply the fragments via REST:

```bash
APIM=<your-apim-name>; RG=<your-rg>; SUB=$(az account show --query id -o tsv)
for f in inbound outbound; do
  az rest --method put \
    --uri "https://management.azure.com/subscriptions/$SUB/resourceGroups/$RG/providers/Microsoft.ApiManagement/service/$APIM/policyFragments/straiker-defendai-$f?api-version=2023-05-01-preview" \
    --body "{\"properties\":{\"description\":\"Straiker DefendAI guardrail ($f)\",\"format\":\"rawxml\",\"value\":$(jq -Rs '.' policy/fragments/straiker-defendai-$f.xml)}}"
done
```

Then attach to any API with a 10-line policy:

```xml
<policies>
  <inbound>
    <base />
    <!-- Optional overrides BEFORE the include:
    <set-variable name="straikerAgentic" value="@(true)" />
    <set-variable name="straikerSource"  value="my-agent-app" />
    -->
    <include-fragment fragment-id="straiker-defendai-inbound" />
  </inbound>
  <backend><base /></backend>
  <outbound>
    <base />
    <include-fragment fragment-id="straiker-defendai-outbound" />
  </outbound>
</policies>
```

In the Azure portal: **APIM resource → Policy fragments** blade. Each fragment shows up with its description; clicking opens the XML editor.

## APIM policy authoring gotchas (lessons learned the hard way)

The policy expression engine is a CSHTML/Razor parser with a strict C# subset. Three traps will fail your deployment with cryptic errors:

1. **Single-statement control flow needs explicit `{}`.** `if (cond) return x;` fails with `Expected "{" but found "return"`. Wrap every body, even one-liners: `if (cond) { return x; }`. Same for `for`, `else if`, `else`, `continue`, `break`. The error is a generic Razor parser message that doesn't point at the actual line in your XML.
2. **`context.Variables` is read-only inside expressions.** `context.Variables["x"] = y` inside an `@{...}` block compiles but fails at deploy with `Property or indexer cannot be assigned to -- it is read only`. Use a `<set-variable>` policy element instead. To pass values between sections, compute each one in its own `<set-variable>` and read with `(T)context.Variables["name"]`.
3. **Local function definitions (`string F(string x) => ...;`) are not supported.** Compiles cleanly, deploys fine, but causes silent runtime failures (the `send-request` returns null with no error). Inline the calls, or use a separate `<set-variable>` to precompute.

## Known limitations

- **Streaming (`stream=true`) responses skip post-call detection.** SSE bodies can't be parsed as a single JSON. Pre-call still fires normally. Mirrors current Kong plugin behaviour.
- **Post-call adds latency.** APIM policies are synchronous; `send-request` in `<outbound>` blocks the response to the client. Mitigated with `timeout=5` and `ignore-error=true` so a Straiker outage can't stall responses, but expect ~50–200 ms of added p50 latency. For zero-latency post-call, route APIM logs to Event Hub and have a Function App consumer call `/detect` async; not included yet.
- **Subscription keys are global per APIM**, not per-route — multi-tenant deployments need APIM Products to scope access.
- **`straikerSource` for agentic must pre-exist** in the Straiker Console as an agentic app, otherwise `/detect?agentic` rejects the request.

---

## Versioning

| Version | Date | Notes |
|---|---|---|
| 0.2.0 | 2026-07 | Fragments-first deploy, generated monolith (`scripts/build-monolith.sh`), post-call output blocking (`straikerBlockOnPostCall`) documented, agent-runner example |
| 0.1.0 | 2026-05 | Initial release: chatbot + agentic, Bicep deploy, self-hosted gateway dev loop |

See [CHANGELOG.md](CHANGELOG.md) for details.
