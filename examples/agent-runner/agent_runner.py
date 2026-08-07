"""
Reference agent runner that exercises the Straiker DefendAI APIM policy fragments
end-to-end. Exposes a single HTTP endpoint that accepts a user prompt, runs an
OpenAI tool-calling loop through APIM, and returns the final answer (or the
gateway block if Straiker post-call detection fired).

Designed to make a single HTTP request from a client (Postman, curl, a UI)
trigger a complete agent loop so you can see both the input gate (pre-call)
and the output gate (post-call) fire on a single click.

Usage:
    uvicorn agent_runner:app --port 8000 --reload

    curl -sS -X POST localhost:8000/agent/chat \
      -H 'Content-Type: application/json' \
      -d '{"message":"Find me information on the topic Acme Project."}' | jq .

Two registered tools demonstrate different post-call outcomes:
    - rag_search       returns a document containing a personal email so the
                       Email regex blocks at post-call.
    - product_lookup   returns benign data so the loop completes with HTTP 200.

The runner intentionally has no business logic of its own. It is a thin
agent loop wrapper so the policy fragments are what the demo is actually
showing.
"""

import json
import os
import time
import uuid
from typing import Any

import httpx
from fastapi import FastAPI, HTTPException
from pydantic import BaseModel


APIM_BASE = os.environ["APIM_BASE"]
APIM_PATH = os.environ.get("APIM_PATH", "/protected-agentic/v1/chat/completions")
APIM_SUB_KEY = os.environ["APIM_SUB_KEY"]
OPENAI_API_KEY = os.environ["OPENAI_API_KEY"]
MODEL = os.environ.get("AGENT_MODEL", "gpt-4o-mini")
USER_NAME = os.environ.get("AGENT_USER_NAME", "azure-aigw-user-agentic")
MAX_ITERATIONS = int(os.environ.get("AGENT_MAX_ITERATIONS", "6"))
HTTP_TIMEOUT = float(os.environ.get("AGENT_HTTP_TIMEOUT", "60"))


SYSTEM_PROMPT = (
    "You are a research assistant. When the user asks for information about a topic, "
    "person, customer, or record, call rag_search with the topic as the query. "
    "When the user asks about products by SKU or product code, call product_lookup. "
    "Include all retrieved details in your final answer."
)


TOOLS = [
    {
        "type": "function",
        "function": {
            "name": "rag_search",
            "description": "Search the internal knowledge base for information on a topic.",
            "parameters": {
                "type": "object",
                "properties": {"query": {"type": "string"}},
                "required": ["query"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "product_lookup",
            "description": "Look up a product by SKU or product code.",
            "parameters": {
                "type": "object",
                "properties": {"sku": {"type": "string"}},
                "required": ["sku"],
            },
        },
    },
]


def rag_search(query: str) -> str:
    return json.dumps(
        {
            "topic": query,
            "lead_contact": "Jane Doe",
            "personal_email": "jane.doe.personal+1985@example.com",
            "summary": (
                f"{query} is a Q3 customer engagement led by Jane Doe. "
                "Contact lead at the address above for further details."
            ),
        }
    )


def product_lookup(sku: str) -> str:
    return json.dumps(
        {
            "sku": sku,
            "name": "Reference Widget",
            "stock": 42,
            "price_usd": 19.99,
        }
    )


TOOL_IMPLEMENTATIONS = {
    "rag_search": lambda args: rag_search(args.get("query", "")),
    "product_lookup": lambda args: product_lookup(args.get("sku", "")),
}


class ChatRequest(BaseModel):
    message: str
    session_id: str | None = None


app = FastAPI(
    title="Straiker APIM agent runner",
    description=(
        "Reference agent loop that runs OpenAI tool calling through the "
        "Straiker DefendAI APIM policy fragments. One HTTP request in, one "
        "completed agent loop out (or a 403 if Straiker blocked the response)."
    ),
)


@app.get("/health")
def health() -> dict[str, str]:
    return {"status": "ok"}


@app.post("/agent/chat")
async def chat(req: ChatRequest) -> dict[str, Any]:
    session_id = req.session_id or f"agent-runner-{int(time.time())}-{uuid.uuid4().hex[:8]}"
    headers = {
        "Authorization": f"Bearer {OPENAI_API_KEY}",
        "Ocp-Apim-Subscription-Key": APIM_SUB_KEY,
        "Content-Type": "application/json",
        "x-user-name": USER_NAME,
        "x-session-id": session_id,
        "x-trace-id": session_id,
        "x-agent-role": "researcher",
    }
    messages: list[dict[str, Any]] = [
        {"role": "system", "content": SYSTEM_PROMPT},
        {"role": "user", "content": req.message},
    ]
    trace: list[dict[str, Any]] = []

    async with httpx.AsyncClient(timeout=HTTP_TIMEOUT) as client:
        for iteration in range(1, MAX_ITERATIONS + 1):
            payload = {"model": MODEL, "messages": messages, "tools": TOOLS}
            r = await client.post(f"{APIM_BASE}{APIM_PATH}", headers=headers, json=payload)
            trace.append({"iteration": iteration, "status": r.status_code})

            if r.status_code == 403:
                body = r.json()
                err = body.get("error", body)
                raise HTTPException(
                    status_code=403,
                    detail={
                        "blocked_by": "straiker-apim-policy",
                        "phase": err.get("phase", "pre_call"),
                        "score": err.get("score"),
                        "turn_id": err.get("turn_id"),
                        "message": err.get("message"),
                        "session_id": session_id,
                        "iteration": iteration,
                        "trace": trace,
                    },
                )
            if r.status_code >= 400:
                raise HTTPException(
                    status_code=502,
                    detail={"upstream_error": r.text, "session_id": session_id},
                )

            msg = r.json()["choices"][0]["message"]
            messages.append(msg)

            if not msg.get("tool_calls"):
                return {
                    "answer": msg.get("content", ""),
                    "iterations": iteration,
                    "session_id": session_id,
                    "trace": trace,
                }

            for tc in msg["tool_calls"]:
                fn = tc["function"]
                impl = TOOL_IMPLEMENTATIONS.get(fn["name"])
                args = json.loads(fn.get("arguments") or "{}")
                result = impl(args) if impl else json.dumps({"error": f"unknown tool {fn['name']}"})
                messages.append(
                    {
                        "role": "tool",
                        "tool_call_id": tc["id"],
                        "content": result,
                    }
                )

    raise HTTPException(
        status_code=504,
        detail={
            "message": f"agent loop exceeded {MAX_ITERATIONS} iterations without final answer",
            "session_id": session_id,
            "trace": trace,
        },
    )
