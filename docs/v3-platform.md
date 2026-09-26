# Straiker v3 platform: `straiker-v3-inbound` / `straiker-v3-outbound`

One fragment pair for chat, agentic, and coding-agent traffic on a tenant that
issues `sk_agt_` integration keys. It speaks the same wire contract as the
Straiker Kong plugin (v0.12) and the LiteLLM guardrail, so the Console shows
the same agent inventory, identities, sessions, and verdicts whichever
gateway a customer runs.

## Which contract am I on?

| Key the Console gives you | Fragments |
|---|---|
| `sk_agt_...` (v3 integration key) | `straiker-v3-*` — this page |
| UUID (v1 application key) | `straiker-defendai-*` (rich), `straiker-webhook-*`, `straiker-coding-*` |

The v3 fragment refuses a UUID key with an explicit HTTP 503
(`x-straiker-verdict: config-error`) rather than 401-ing on every call and
silently failing open. `deploy.sh --contract v3` checks the key prefix too.

## Wire contract

**Request phase** (`straiker-v3-inbound`, `POST /api/v3/detect`):

- The provider body, allowlisted to the fields the LiteLLM guardrail relays
  (`model`, `messages`, `tools`, `system`, `input`, `instructions`, `prompt`,
  sampling parameters, ...). Client `metadata` is dropped. Credentials inside
  `tools[]` / `mcp_servers[]` entries (`headers`, `authorization`,
  `authorization_token`) are replaced with `[redacted]`, one level deep.
- `session_id` — the platform reads it from the body only.
- `original.processed.Meta.user` and `user_name` — the authenticated principal
  (the gateway path reads the former, the flat text-completion path the latter).
- `network` — `ip` and `user_agent`, recorded on the turn, never scored.
- Embeddings, `count_tokens`, `/models`, audio, images, moderations, files,
  batches and realtime routes are never scored (`x-straiker-verdict: unknown`,
  `x-straiker-detail: route`).
- A Responses call with a string `input` is normalised to the message form
  (the platform does not score a string input).
- Headers: `Authorization: Bearer sk_agt_...`, `X-S6r-Ingress: gw-azure-apim`,
  `x-s6r-agent` (when the application is named), `x-s6r-client` (when known),
  `x-s6r-format` (only when set), `x-claude-code-session-id` (forwarded).
- Never: `x-tool`, `x-straiker-phase`, `x-straiker-user`, `Straiker-Debug`,
  a flat `prompt`/`app_response`, the APIM subscription key, the client's
  `Authorization`.

**Response phase** (`straiker-v3-outbound`):

```json
{ "straiker_phase": "response-sync", "sse": "<answer>", "model": "...",
  "request": { ...the relayed request... }, "session_id": "...",
  "original": { "processed": { "Meta": { "user": "..." } } } }
```

`sse` is the upstream body verbatim for JSON answers and Anthropic SSE. An
OpenAI `chat.completion.chunk` stream is reassembled into one
`chat.completion` object first (the platform does not read that stream
format; measured 2026-09-26). A Responses stream is reduced to its
`response.completed` object.

**Verdict:** deny when `hookSpecificOutput.permissionDecision == "deny"`,
`action ∈ {block, deny}`, or `blocked_by` is non-empty. `action: "detect"`
records a finding and blocks nothing.

## How agents are named (first match wins)

1. `straikerAgentRef` set in the API policy — pins the application; the
   caller's header is ignored.
2. The caller's `x-s6r-agent` header (unpinned APIs; turn off with
   `straikerAllowCallerAgent=false`).
3. A coding agent recognised from its User-Agent: Claude Code (`claude-cli/`)
   → `Claude (APIM)` (`straikerClaudeAgentName`), Codex CLI (`codex_exec/`,
   `codex_cli`) → `Codex (APIM)`, OpenCode → `OpenCode (APIM)`, Cursor
   (`cursor/`) → `Cursor (APIM)`; `x-s6r-client` is sent on every call so
   sidecar calls stay in the same session. Cursor and GitHub Copilot cannot
   be told apart by User-Agent in practice: set `straikerClient` on their
   route.
4. **Automatic**, from the APIM context (`straikerAgentNameFrom`, default
   `api`): the API's display name, or the subscription (`subscription`),
   product (`product`) or calling Entra application (`jwt-app`: claims
   `app_displayname` → `azp` → `appid` from `validatedJwt`).
5. `straikerAgentNameFrom=none` → the platform's catch-all,
   `Autonomous (<gateway type>)`.

A new name becomes a new agent on first use. The catch-all is named after
the integration's gateway type; named agents are unaffected. Measured:
unnamed traffic through an API whose display name is
"Contoso Claims Assistant" (id `v3-enum`) minted an agent with that display
name; `subscription`, `product`, `jwt-app` and `none` each minted the expected
name, and every one owned its sessions (`tests/v3/enumeration_test.py`).

### One shape per agent

An agent's type is set by its **first** traffic, and the platform expects
the matching body shape from then on. So the fragment relays every
non-coding request as a `messages` array (the Kong/LiteLLM convention,
typed `autonomous_agent`), and an agent never sees two shapes:

- OpenAI text completions (`/v1/completions`): `prompt` → one user message;
  the `text_completion` answer is reassembled as a `chat.completion`.
- Azure AI Foundry Agent Service messages (below) → user / tool messages.
- Responses API string `input` → the message form.

Do not reuse an agent name that another integration minted with a
different shape.

### Enumeration checklist

1. A **gateway** integration (shared key, agents discovered from traffic);
   a single-agent tile files every call under one agent and ignores names.
2. A naming source: default `api` needs nothing; `subscription` / `product` /
   `jwt-app` for shared APIs; `straikerAgentRef` to pin.
3. One name per application (the fragment keeps one shape per name).
4. New agents start on tenant-default controls (mostly detect) — set the
   controls to enforce to block.
5. Indexing takes a few minutes; `x-straiker-verdict` / `x-straiker-detail`
   confirm scoring immediately.

## Azure AI Foundry

**Models** (`https://<resource>.services.ai.azure.com/models/...`, Azure
OpenAI deployments): an ordinary relayed conversation — prompt, tool calls
and results, and the answer (streaming reassembled) are scored.

**Agent Service** (`https://<resource>.services.ai.azure.com/api/projects/<project>`,
the Assistants-shaped API the Azure AI Agents SDK calls): the run executes
inside Foundry, so the fragment guards the calls that carry content and
passes the rest through (`shape = assistant.message | assistant.control`):

| Call | Fragment |
|---|---|
| `POST /threads/{id}/messages` (`content`) | scored; block → `400 content_filter`, message never stored |
| `POST /threads` with `messages` | scored; block → `400` |
| `POST /threads/runs` (`thread.messages`) | scored; block → `400`, no thread or run |
| `POST /threads/{id}/runs` with `additional_messages` | scored; block → `400`, no run |
| `POST /threads/{id}/runs/{run}/submit_tool_outputs` | scored as `role: tool` messages; block → `400` |
| run create without messages, polling, listing, assistant CRUD | passed through (`unknown`, `detail=agent-control` / `method`) |
| the run's model calls and Foundry-executed tools | not visible to APIM |

Session = the Foundry thread id (from the path); identity = the caller's
Entra principal (`straikerIdentityMode=jwt`). Verified against a real agent
with a function tool (`tests/v3/foundry_agent_test.py`, 16 checks) and with
the Azure AI Agents SDK pointed at the APIM URL (`tests/v3/foundry_sdk_test.py`).

## Who the user is

`straikerIdentityMode`:

| Mode | Principal |
|---|---|
| `subscription` (default) | `context.User.Email` → subscription display name → `apim-sub-<id>` |
| `jwt` | `validatedJwt` claims `email` → `preferred_username` → `upn` → `unique_name` → `app_displayname` → `azp` → `appid` → `sub` (set `output-token-variable-name="validatedJwt"` on `validate-jwt` / `validate-azure-ad-token`; app-only tokens resolve to the application id) |
| `headers` | `x-user-name` |

A `straikerUserName` variable already set by `straiker-gateway-auth` (per-
developer keys for Claude Code) or by your own policy is preserved. The
request body's `user` is a last resort and Claude Code's hashed
`metadata.user_id` is never used.

## Sessions

`x-claude-code-session-id` header → Claude Code `metadata.user_id`
(`session_id` inside) → `metadata.session_id` → `x-session-id` header →
Codex's `session-id` header → derived `apim-` + sha256(principal, application name, preamble, first user
turn) → per request. The derivation uses the `system` field, else
`instructions`, else the first `system`/`developer` message, and joins every
text block of the first user turn, so two chats behind one system prompt are
two sessions and an image-first turn does not collide. The application name
is in the seed (the LiteLLM seed is principal + preamble + first turn)
because one APIM key can front many applications: a session belongs to the
application that opened it, so without the name two applications sharing a
system prompt would merge the same person's identical first message into
one session. Send `x-session-id` from the application when you can; the
derived id is deterministic, so an identical conversation re-sent later
joins its earlier session and the platform treats repeated turns as replays.

## Enforcement

- `straikerBlockMode=stub` (default): HTTP **200** with a provider-shaped
  assistant turn carrying the tenant's block message — Anthropic `end_turn`
  message (SSE when the client streams), OpenAI `chat.completion` (or
  `chat.completion.chunk` + `[DONE]`), Responses, or text completion. This
  is the Kong plugin's rule: a 403 makes Claude Code prompt for `/login`, a
  refusal ends the session, 5xx gets retried.
- `http-400` / `http-403`: `{"error":{"type":"straiker_policy_violation","message":...}}`.
- Response-phase denies (a tool call or an answer) replace the upstream
  response with the same stub.
- **Replay memory** (`straikerReplayMemory`, 24 h): a control-named block is
  remembered in APIM's cache under the session (else the principal); a
  resend, or a conversation that has grown past the blocked turn, is
  re-blocked at the gateway with `x-straiker-detail: replay` and no second
  detect call. Coding agents auto-retry a turn after a block, so this keeps
  the retry blocked without another round trip.
- Every response carries `x-straiker-verdict` (`allow | detect | block |
  degraded | unknown`) and `x-straiker-detail` (turn ids, or why the
  verdict is `degraded`/`unknown`).

## Failure policy

`straikerFailClosed=false` (default): a Straiker timeout, network error,
5xx, 401, unparseable answer, or an oversize body (> `straikerMaxBodyBytes`,
4 MiB, measured on the compact JSON so whitespace padding does not count)
forwards the request with `x-straiker-verdict: degraded`. Alert on that
header. `true`: the same stub / error body with "Straiker is unreachable and
this gateway is fail-closed." `straikerTimeoutSec` defaults to 30 (Claude
Code contexts need it); 502/503/504 are retried once.

A POST body the gateway cannot read as a JSON object, on a route it scores
(not JSON, trailing bytes, or nested more than 64 levels, the limit of
APIM's JSON parser), is **refused with HTTP 400**
(`{"error":{"type":"straiker_uninspectable_request"}}`,
`x-straiker-verdict: block`, `x-straiker-detail: unparseable`) whatever
`straikerFailClosed` says, because the caller controls that condition.
`straikerBlockUnparseable=false` forwards it with `degraded` instead.

## Security

- The integration key and the relayed conversation are only sent to
  `https://*.straiker.ai`. Any other `straikerDetectUrl` is refused with
  HTTP 503 `config-error` unless `straikerAllowCustomDetectUrl=true` (a
  self-hosted detect endpoint you control); a relative or plain-`http` URL is
  refused even then.
- Store `straiker-api-key` as a **secret** Named Value (or a Key Vault
  reference). The fragments never write it to a trace or a response.
- Relayed bodies are allowlisted; `tools[]` / `mcp_servers[]` credentials are
  redacted; the APIM subscription key and the client's `Authorization` are
  never relayed.
- `x-s6r-agent` from the caller, and every principal value, are stripped of
  control characters and length-capped before they are sent.
- Route skips apply only to operations that never carry a turn (embeddings,
  audio, images, files, ...). A path that ends in a chat, completion,
  response, message, thread or run operation is always scored, so a
  deployment or resource named like a skipped route (an Azure OpenAI
  deployment called `audio`) cannot skip inspection.
- Only `jwt` and `subscription` identities are authenticated by APIM;
  `x-user-name` (`headers` mode, and the fallback when neither is present)
  and the body's `user` are asserted by the caller.
- `x-straiker-verdict` / `x-straiker-detail` are returned to clients (turn
  ids, not content). Remove them in the API's outbound policy after the
  fragment if clients must not see them; block responses are generated in
  inbound and always carry them.

## Streaming

`straikerResponsePhase=sync` (default) reads the upstream body to score it,
which makes APIM buffer the response: a denied answer or tool call never
reaches the client, and token-by-token streaming is lost on that route (the
same trade-off as Kong buffered mode and LiteLLM). `async` posts the answer
one-way and keeps streaming; nothing on the response phase can block.
`off` scores the request only.

## Knobs

| Variable | Default |
|---|---|
| `straikerApiKey` | `{{straiker-api-key}}` |
| `straikerDetectUrl` | `https://api.prod.straiker.ai/api/v3/detect` |
| `straikerAllowCustomDetectUrl` | `false` (a non-`*.straiker.ai` detect URL is refused; `http` always is) |
| `straikerResponsePhase` | `sync` (`async`, `off`) |
| `straikerScoreRequest` / `straikerScoreResponse` | `true` |
| `straikerFailClosed` | `false` |
| `straikerBlockUnparseable` | `true` (refuse an unreadable POST body with 400) |
| `straikerTimeoutSec` | `30` |
| `straikerMaxBodyBytes` | `4194304` |
| `straikerBlockMode` | `stub` (`http-400`, `http-403`) |
| `straikerAgentRef` | unset |
| `straikerAgentNameFrom` | `api` (`subscription`, `product`, `jwt-app`, `none`) |
| `straikerAllowCallerAgent` | `true` |
| `straikerClient` | unset (`claude`, `cursor`, `codex`, `opencode`, `copilot`) |
| `straikerClaudeAgentName` | `Claude (APIM)` |
| `straikerFormatHint` | unset (`anthropic.messages`, `openai.chat`) |
| `straikerIdentityMode` | `subscription` (`jwt`, `headers`) |
| `straikerReplayMemory` / `straikerReplayTtlSec` | `true` / `86400` |

Set knobs with `<set-variable>` before `<include-fragment fragment-id="straiker-v3-inbound" />`.
Examples: `policy/examples/v3-*.xml`.

## Testing

`tests/v3/` is a live harness against a dev APIM instance and a Straiker
tenant. Settings come from `tests/v3/.env` (gitignored; `setup_dev_apis.py`
writes the gateway entries), an optional file named by `$STRAIKER_ENV_FILE`,
and the environment; `tests/v3/harness_env.py` lists every key. Install with
`pip install -r tests/v3/requirements.txt`.

- `setup_dev_apis.py` registers the fragments and follows APIM's
  asynchronous validation to the end (a rejected fragment fails the run
  instead of leaving the previous version live), then reads each fragment
  back to prove the new content is what runs. It creates the `v3-*` test
  APIs, whose policies accept `x-test-<knob>` overrides only together with
  `x-test-token` (the secret Named Value `straiker-test-token`), so a
  subscription key alone cannot repoint the detect URL or swap the key.
- `e2e_matrix.py` drives the client-visible matrix; `console_verify.py`
  checks what the platform recorded (agents, sessions, identity,
  transcripts); `block_test.py` covers block / post-call block / replay /
  kill switch; `cc_test.py` runs real Claude Code; `load_test.py` measures
  concurrency and added latency; `wire_tap.py` captures the exact bytes
  APIM posts (fixtures come from it, never from hand-written JSON).
- Matrix cases 31-36 pin the security fixes: a body nested past the parser's
  limit is refused, whitespace padding past the size cap is still scored, a
  deployment named like a skipped route is still scored, a foreign detect URL
  is refused, `x-test-*` overrides need the token, and the caller's agent name
  is sanitised. `mutation_check.py` deploys each fix reverted and requires its
  case to fail, then restores and reads the original back.
