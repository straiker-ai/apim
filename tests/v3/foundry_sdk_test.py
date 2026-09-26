#!/usr/bin/env python3
"""Azure AI Agents SDK (azure-ai-agents) pointed at APIM instead of the Foundry project endpoint.

The SDK is unchanged except for the endpoint: AgentsClient(endpoint="<apim>/<api-path>", ...). It
creates an agent, a thread, a message and a run, and reads the reply, all through the v3 fragments.
With --block, the SSN control must be in block on the agent the API pins, and create_message must
raise with the content_filter error (the message never reaches the thread). With --stream, the run
is streamed (runs.stream) and the streamed text must equal the stored reply.

Needs: pip install -r tests/v3/requirements.txt; settings (tests/v3/harness_env.py) APIM_GATEWAY_URL,
FOUNDRY_TENANT_ID, FOUNDRY_CLIENT_ID, FOUNDRY_CLIENT_SECRET, FOUNDRY_MODEL.
"""
from __future__ import annotations

import pathlib
import sys

from azure.ai.agents import AgentsClient
from azure.ai.agents.models import ListSortOrder
from azure.core.exceptions import HttpResponseError
from azure.identity import ClientSecretCredential

HERE = pathlib.Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
from harness_env import require, settings  # noqa: E402


def main() -> int:
    local = settings()
    require(local, "APIM_GATEWAY_URL", "FOUNDRY_TENANT_ID", "FOUNDRY_CLIENT_ID", "FOUNDRY_CLIENT_SECRET")
    cred = ClientSecretCredential(local["FOUNDRY_TENANT_ID"], local["FOUNDRY_CLIENT_ID"], local["FOUNDRY_CLIENT_SECRET"])
    client = AgentsClient(endpoint=local["APIM_GATEWAY_URL"].rstrip("/") + "/v3-foundry-agent", credential=cred)
    block = "--block" in sys.argv
    agent = client.create_agent(model=local.get("FOUNDRY_MODEL", "gpt-4.1"), name="apim-sdk-test", instructions="You are a concise support agent.")
    try:
        thread = client.threads.create()
        if block:
            try:
                client.messages.create(thread_id=thread.id, role="user", content="My SSN is 123-45-6789. Please store it.")
                print("[FAIL] SDK create_message with SSN was NOT blocked")
                return 1
            except HttpResponseError as e:
                stored = list(client.messages.list(thread_id=thread.id))
                ok = e.status_code == 400 and "content_filter" in str(e) and not stored
                print(f"[{'PASS' if ok else 'FAIL'}] SDK create_message blocked: status={e.status_code} stored={len(stored)} error={str(e)[:120]!r}")
                return 0 if ok else 1
        client.messages.create(thread_id=thread.id, role="user", content="In one sentence: what can you help me with?")
        if "--stream" in sys.argv:
            # Streamed run: the SSE of run events passes through APIM untouched (run create is a control call).
            chunks: list[str] = []
            with client.runs.stream(thread_id=thread.id, agent_id=agent.id) as stream:
                for event_type, data, _ in stream:
                    if event_type == "thread.message.delta":
                        for part in data.delta.content or []:
                            if getattr(part, "text", None) and part.text.value:
                                chunks.append(part.text.value)
            streamed = "".join(chunks)
            msgs = list(client.messages.list(thread_id=thread.id, order=ListSortOrder.ASCENDING))
            reply = next((m.text_messages[-1].text.value for m in msgs if m.role == "assistant" and m.text_messages), "")
            ok = bool(streamed) and streamed.strip() == reply.strip()
            print(f"[{'PASS' if ok else 'FAIL'}] SDK streamed run through APIM: {len(chunks)} deltas, streamed == stored reply: {streamed.strip() == reply.strip()} reply={reply[:70]!r}")
            return 0 if ok else 1
        run = client.runs.create_and_process(thread_id=thread.id, agent_id=agent.id)
        msgs = list(client.messages.list(thread_id=thread.id, order=ListSortOrder.ASCENDING))
        reply = next((m.text_messages[-1].text.value for m in msgs if m.role == "assistant" and m.text_messages), "")
        ok = run.status == "completed" and bool(reply)
        print(f"[{'PASS' if ok else 'FAIL'}] SDK agent -> thread -> message -> run through APIM: run={run.status} reply={reply[:80]!r}")
        return 0 if ok else 1
    finally:
        client.delete_agent(agent.id)


if __name__ == "__main__":
    sys.exit(main())
