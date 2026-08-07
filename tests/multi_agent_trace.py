"""
Single-user-interaction multi-agent / multi-model / multi-tool demo against
the APIM Straiker policy.

One user prompt -> two specialised agents on two different models -> multiple
tool calls. Every APIM->OpenAI hop reuses the SAME session_id, x-user-name,
x-session-id, x-trace-id headers and an explicit `agent_role` annotation in
the OpenAI request body's `user` field, so Straiker can correlate every turn
back to the originating user interaction.

Phase 1 — Researcher agent (gpt-4o-mini): tool calling (rag_search,
          web_search, web_fetch) to gather raw material.
Phase 2 — Writer agent (gpt-4o):           synthesise the findings into a
          two-paragraph executive briefing. No tools; the assembled research
          is passed in messages[].

Run:
    export APIM_GATEWAY_URL=https://<your-apim-name>.azure-api.net
    export APIM_SUBSCRIPTION_KEY=<from APIM portal>
    export APIM_PATH_PREFIX=protected-agentic     # default
    export OPENAI_API_KEY=sk-...
    python3 multi_agent_trace.py
"""

import json
import os
import sys
import time
import uuid
from typing import Any

from openai import OpenAI

sys.path.insert(0, os.path.dirname(__file__))
from agentic_test import TOOLS, DISPATCH  # reuse the working tool registry

USER = "trace-demo@acme.com"


def call(client: OpenAI, *, model: str, messages: list[dict[str, Any]],
         tools: list[dict] | None, agent_role: str,
         session_id: str, trace_id: str, subscription_key: str) -> Any:
    """One APIM -> OpenAI hop. Identity + correlation headers are set on
    every call so Straiker can stitch them under one session_id."""
    return client.chat.completions.create(
        model=model,
        messages=messages,
        tools=tools,
        user=f"{USER}#{agent_role}",
        extra_headers={
            "x-user-name":  USER,
            "x-user-role":  "demo",
            "x-session-id": session_id,
            "x-trace-id":   trace_id,
            "x-agent-role": agent_role,
            "Ocp-Apim-Subscription-Key": subscription_key,
        },
    )


def run_researcher(client: OpenAI, prompt: str, *, session_id: str,
                   trace_id: str, subscription_key: str,
                   max_iters: int = 6) -> tuple[str, list[dict]]:
    """Returns (final_text, tool_observations[]). Uses gpt-4o-mini + tools."""
    messages: list[dict[str, Any]] = [
        {"role": "system",
         "content": "You are a research agent. Use tools to gather facts. "
                    "Cite URLs. Keep it under 200 words."},
        {"role": "user", "content": prompt},
    ]
    observations: list[dict] = []

    for i in range(max_iters):
        print(f"  [researcher iter {i+1}] -> APIM/OpenAI ({len(messages)} msgs)")
        resp = call(
            client, model="gpt-4o-mini", messages=messages, tools=TOOLS,
            agent_role="researcher", session_id=session_id, trace_id=trace_id,
            subscription_key=subscription_key,
        )
        msg = resp.choices[0].message

        if msg.tool_calls:
            messages.append(msg.model_dump(exclude_none=True))
            for tc in msg.tool_calls:
                fn = tc.function.name
                args = json.loads(tc.function.arguments or "{}")
                print(f"     researcher tool_call -> {fn}({json.dumps(args)[:100]})")
                try:
                    result = DISPATCH[fn](**args)
                except Exception as e:
                    result = {"error": f"{type(e).__name__}: {e}"}
                observations.append({"tool": fn, "args": args, "result": result})
                messages.append({
                    "role":         "tool",
                    "tool_call_id": tc.id,
                    "name":         fn,
                    "content":      json.dumps(result),
                })
            continue

        return (msg.content or ""), observations

    return "[researcher hit max iterations]", observations


def run_writer(client: OpenAI, user_question: str, research_text: str,
               observations: list[dict], *, session_id: str, trace_id: str,
               subscription_key: str) -> str:
    """Synthesise the research into an executive briefing. Uses gpt-4o."""
    print(f"  [writer iter 1] -> APIM/OpenAI (synthesis on gpt-4o)")
    obs_text = "\n".join(
        f"- {o['tool']}({json.dumps(o['args'])[:120]}) -> "
        f"{json.dumps(o['result'])[:200]}"
        for o in observations
    ) or "(no tool observations)"

    resp = call(
        client, model="gpt-4o", tools=None,
        agent_role="writer", session_id=session_id, trace_id=trace_id,
        subscription_key=subscription_key,
        messages=[
            {"role": "system",
             "content": "You are an editor. Rewrite the researcher's findings "
                        "into a two-paragraph executive briefing. First paragraph: "
                        "what the researcher found. Second paragraph: practical "
                        "implications for an enterprise security buyer. Keep it "
                        "tight; cite URLs the researcher found."},
            {"role": "user", "content":
                f"Original question:\n{user_question}\n\n"
                f"Researcher's draft:\n{research_text}\n\n"
                f"Researcher's tool observations:\n{obs_text}"},
        ],
    )
    return resp.choices[0].message.content or ""


def main() -> int:
    api_key = os.environ.get("OPENAI_API_KEY")
    gateway = os.environ.get("APIM_GATEWAY_URL")
    subkey  = os.environ.get("APIM_SUBSCRIPTION_KEY")
    path    = os.environ.get("APIM_PATH_PREFIX", "protected-agentic")
    if not (api_key and gateway and subkey):
        print("ERROR: set OPENAI_API_KEY, APIM_GATEWAY_URL, APIM_SUBSCRIPTION_KEY",
              file=sys.stderr)
        return 1

    client = OpenAI(api_key=api_key, base_url=f"{gateway}/{path}/v1")

    trace_id   = f"trace-{uuid.uuid4().hex[:12]}"
    session_id = f"multi-agent-{int(time.time())}-{trace_id}"

    print(f"=== One user interaction, two agents, two models ===")
    print(f"gateway:    {gateway}/{path}")
    print(f"session_id: {session_id}")
    print(f"trace_id:   {trace_id}")
    print(f"user:       {USER}")
    print()

    user_question = (
        "Give me a two-paragraph executive briefing on how the Straiker APIM "
        "policy protects agentic AI traffic, and how that compares to OpenAI's "
        "function-calling model. Cite your sources."
    )
    print(f"USER PROMPT:\n  {user_question}\n")

    print("PHASE 1 — Researcher (gpt-4o-mini, tools enabled):")
    research_text, observations = run_researcher(
        client, user_question, session_id=session_id, trace_id=trace_id,
        subscription_key=subkey,
    )
    print(f"\n  researcher draft ({len(research_text)} chars):")
    print(f"  {research_text[:400]}{'...' if len(research_text) > 400 else ''}\n")

    print("PHASE 2 — Writer (gpt-4o, no tools):")
    final = run_writer(
        client, user_question, research_text, observations,
        session_id=session_id, trace_id=trace_id, subscription_key=subkey,
    )
    print(f"\n=== FINAL BRIEFING ===\n{final}\n")

    print(f"=== Look up in Straiker Console ===")
    print(f"  Defend -> apim-dev-agentic -> Activity")
    print(f"  filter session_id = {session_id!r}")
    print(f"  every APIM/OpenAI hop shares this session_id")
    print(f"  trace_id ({trace_id}) and agent_role (researcher/writer) ride along too")
    return 0


if __name__ == "__main__":
    sys.exit(main())
