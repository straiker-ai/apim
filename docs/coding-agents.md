> **v3 tenants (`sk_agt_` keys):** use `straiker-v3-inbound` / `straiker-v3-outbound` instead — see [v3-platform.md](v3-platform.md). This page describes the v1 coding contract.

# Coding agents (Claude Code) through Azure APIM

Protect developers' coding agents — Claude Code and other Anthropic-Messages-shaped
agents — by routing them through Azure API Management with the Straiker coding
fragments. Straiker sees the same events the natively installed Straiker hooks
see, so detection is at parity, with nothing to install on developer laptops.

```
Claude Code ──▶ Azure APIM ──▶ Anthropic / Bedrock
                    │
                    └──▶ Straiker  /api/v1/detect   (hook events)
```

## What gets detected

Every coding turn is decomposed into the events Straiker's coding-agent pipeline scores:

| Event | What it carries | Catches |
|---|---|---|
| `UserPromptSubmit` | the developer's prompt | jailbreaks, policy-violating requests |
| `PostToolUse` | content a tool returned | **indirect prompt injection** (poisoned file / web page / MCP result) |
| `PreToolUse` | the command the model wants to run | RCE (`curl \| sh`, base64→bash, reverse shells), destructive commands, credential exfiltration |
| `Stop` | the assistant's final answer | monitor-only |

MCP tool calls (`mcp__server__tool`) are attributed with their server and tool name.

## Choose a mode

| | `detect` (default) | `enforce` |
|---|---|---|
| Latency added | **~0** (fire-and-forget) | one detect round trip per call |
| **Streaming** | ✅ **preserved** — `stream:true` passes straight through | ❌ response is buffered |
| Can block | no (monitor-only) | ✅ blocks prompts **and** tool calls before they run |
| Use it for | rollout, visibility, latency-sensitive teams | enforcement on sensitive repos/environments |

`detect` is deliberately asynchronous: the policy never waits for a verdict, so the
developer's experience is unchanged and streaming keeps working. `enforce` trades
streaming for the ability to stop a destructive command *before the agent runs it* —
that is only possible if the response is held until it has been scored.

## Setup

**1. Create the Straiker application.** In the Console: **Defend → Add Agent →** coding
agent. Copy its API key.

**2. Store the key as an APIM Named Value** called `straiker-coding-api-key`
(Secret ✓; Key Vault-backed for production).

**3. Register the two policy fragments** — **APIM → Policy fragments → + Create** —
pasting the XML from the repo:
- `straiker-coding-inbound` ← [`policy/fragments/straiker-coding-inbound.xml`](../policy/fragments/straiker-coding-inbound.xml)
- `straiker-coding-outbound` ← [`policy/fragments/straiker-coding-outbound.xml`](../policy/fragments/straiker-coding-outbound.xml)

**4. Create the API** that fronts the model provider (`https://api.anthropic.com`,
operation `POST /v1/messages`) and attach this policy:

```xml
<policies>
  <inbound>
    <base />
    <set-variable name="straikerCodingMode" value="detect" />
    <include-fragment fragment-id="straiker-coding-inbound" />
  </inbound>
  <backend><base /></backend>
  <outbound>
    <base />
    <include-fragment fragment-id="straiker-coding-outbound" />
  </outbound>
  <on-error><base /></on-error>
</policies>
```

**5. Point the agent at APIM.** For Claude Code:

```bash
export ANTHROPIC_BASE_URL="https://<your-apim>.azure-api.net/<api-path>"
export ANTHROPIC_CUSTOM_HEADERS="Ocp-Apim-Subscription-Key: <apim subscription key>"
claude
```

**6. Turn on the controls.** In the Console, on that application, set the coding-agent
controls (Remote Code Execution, Destructive Commands, Data Exfiltration, Indirect
Prompt Injection) to **Block** — a new app starts in detect mode and will not block.
Allow ~5 minutes for a control change to reach the runtime.

## Configuration reference

Set any of these with `<set-variable>` **before** `<include-fragment>`. Every knob is
optional; string values work (`value="detect"`) as well as typed expressions.

| Variable | Default | Description |
|---|---|---|
| `straikerCodingMode` | `detect` | `detect` = async, streaming preserved, monitor-only. `enforce` = synchronous, blocks prompts and tool calls, buffers the response. |
| `straikerCodingKey` | `{{straiker-coding-api-key}}` | Straiker coding application key. Resolves from the Named Value; override to use a different app per API. |
| `straikerCodingContract` | `hooks` | `hooks` = APIM reconstructs the hook events (**works today**). `gateway` = forward the raw request and let Straiker parse it server-side (requires the gateway parser on your tenant). |
| `straikerXTool` | derived | The `x-tool` header. Defaults to `claude-code` for `hooks`, `kong-claude-code` for `gateway`. Override only if Straiker support tells you to. |
| `straikerDetectUrl` | `https://api.prod.straiker.ai/api/v1/detect` | Detect endpoint. Change for a regional or dedicated tenant. |
| `straikerBlockMode` | `anthropic-stub` | How a block is returned in `enforce` mode. `anthropic-stub` = HTTP 200 carrying a well-formed Anthropic message (the agent shows the reason and ends the turn cleanly — recommended). `http-403` = a JSON error, which most agents surface as a connection failure. |
| `straikerTimeoutSec` | `5` | Timeout for the detect call in `enforce` mode. |
| `straikerFailOpen` | `true` | Documented as fail-open/fail-closed, but the v1 coding fragment never reads it: it always fails open (a non-200 from Straiker is simply not a block). For a real fail-closed use the v3 fragments' `straikerFailClosed`. |

### Request headers the policy honours

| Header | Purpose |
|---|---|
| `x-claude-code-session-id` | Groups a coding session in the Console. If absent, the session id is read from `metadata.user_id` (Claude Code sets this itself), then the APIM request id. |
| `x-straiker-user` / `x-user-name` | Developer identity for attribution. Falls back to the APIM subscription id. |

## What the policy skips (and why)

- **Utility calls.** Claude Code makes title-generation and similar side calls with no
  tools; roughly a quarter of all traffic. They carry no user intent and are dropped —
  they are the main false-positive source.
- **Non-coding traffic.** If the request has none of the Claude Code fingerprints (the
  `Bash`/`Read`/`Edit`/`TodoWrite` tool set, a Claude Code system prompt, or a
  `claude-cli/` user agent), it passes through untouched.

## Limits

- **`enforce` mode expects `stream:false`** on the guarded route. APIM policies cannot
  inspect an SSE stream chunk-by-chunk, so blocking requires a buffered response. Use
  `detect` mode when streaming matters, `enforce` where prevention matters.
- **One event per phase per call.** The policy scores the newest tool result on the
  request side and the pending tool call on the response side. A turn that emits several
  parallel tool calls has its first one scored. (Gateway-contract mode has no such limit
  once available — Straiker expands the whole transcript server-side.)
- **Severity gating.** RCE patterns score ~0.59 at severity *low*; depending on your
  Console policy that may detect rather than block. Confirm your control thresholds with
  Straiker if you need hard enforcement on those.
