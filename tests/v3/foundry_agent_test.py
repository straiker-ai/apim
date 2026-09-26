#!/usr/bin/env python3
"""Azure AI Foundry Agent Service through APIM, every entry point, against a real agent.

The agent runs server-side, so what APIM sees is thread and run management. This drives the
Assistants-shaped API (the one the Azure AI Agents SDK calls) through the `v3-foundry-agent`
test API and checks, per call, what the fragment did (x-straiker-verdict / x-straiker-detail):

  control calls (no content)     POST /threads {}, POST /threads/{id}/runs {assistant_id},
                                 GET polling / listing        -> skipped ("unknown"), never blocked
  content calls (guarded inline) POST /threads {messages}, POST /threads/{id}/messages,
                                 POST /threads/runs {thread.messages},
                                 POST /threads/{id}/runs {additional_messages},
                                 POST /threads/{id}/runs/{run}/submit_tool_outputs
                                                              -> scored (allow | detect, with a turn id)
  block (SSN control in block on the agent) -> HTTP 400 {"error":{"code":"content_filter"}} and the
                                 blocked content never reaches the thread

Settings (tests/v3/harness_env.py): APIM_GATEWAY_URL, FOUNDRY_TENANT_ID, FOUNDRY_CLIENT_ID,
FOUNDRY_CLIENT_SECRET, FOUNDRY_MODEL; STRAIKER_PLATFORM_PAT and STRAIKER_INTEGRATION_ID for the block step.

Usage: python3 tests/v3/foundry_agent_test.py [--agent "APIM Foundry Agent"] [--skip-block]
"""
from __future__ import annotations

import argparse
import json
import pathlib
import sys
import time

import requests

HERE = pathlib.Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
from block_test import clear_control, find_agent, set_control, wait_for  # noqa: E402
from console_verify import jwt_from_pat  # noqa: E402
from harness_env import require, settings  # noqa: E402

AV = "2025-05-01"
SSN = "My SSN is 123-45-6789. Please store it and look up my orders."


class Foundry:
    def __init__(self, gw: str, token: str):
        self.gw = gw.rstrip("/")
        self.h = {"Authorization": f"Bearer {token}", "Content-Type": "application/json"}

    def req(self, method: str, path: str, body=None) -> requests.Response:
        sep = "&" if "?" in path else "?"
        return requests.request(method, f"{self.gw}{path}{sep}api-version={AV}", headers=self.h, json=body, timeout=90)


def verdict(r: requests.Response) -> str:
    return f"HTTP {r.status_code} verdict={r.headers.get('x-straiker-verdict', '-')} detail={r.headers.get('x-straiker-detail', '-')[:44]}"


def scored(r: requests.Response) -> bool:
    d = r.headers.get("x-straiker-detail", "")
    return r.headers.get("x-straiker-verdict") in ("allow", "detect") and len(d) >= 36 and "-" in d[:36]


def skipped(r: requests.Response) -> bool:
    return r.headers.get("x-straiker-verdict") == "unknown"


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--agent", default="APIM Foundry Agent", help="the Straiker agent the v3-foundry-agent API pins (straikerAgentRef)")
    ap.add_argument("--skip-block", action="store_true")
    a = ap.parse_args()
    local = settings()
    require(local, "APIM_GATEWAY_URL", "FOUNDRY_TENANT_ID", "FOUNDRY_CLIENT_ID", "FOUNDRY_CLIENT_SECRET")
    secret = local["FOUNDRY_CLIENT_SECRET"]
    tok = requests.post(f"https://login.microsoftonline.com/{local['FOUNDRY_TENANT_ID']}/oauth2/v2.0/token",
                        data={"grant_type": "client_credentials", "client_id": local["FOUNDRY_CLIENT_ID"], "client_secret": secret, "scope": "https://ai.azure.com/.default"}, timeout=30).json()["access_token"]
    f = Foundry(local["APIM_GATEWAY_URL"] + "/v3-foundry-agent", tok)
    results: list[tuple[str, bool, str]] = []

    def rec(name: str, ok: bool, detail: str) -> None:
        results.append((name, ok, detail))
        print(f"[{'PASS' if ok else 'FAIL'}] {name}: {detail}", flush=True)

    # --- an agent with a client-side function tool, so a run can reach requires_action
    asst = f.req("POST", "/assistants", {"model": local.get("FOUNDRY_MODEL", "gpt-4.1"), "name": "apim-guardrail-test",
                                          "instructions": "You are an order-support agent. Always call lookup_order to answer order questions.",
                                          "tools": [{"type": "function", "function": {"name": "lookup_order", "description": "Look up an order by id",
                                                                                      "parameters": {"type": "object", "properties": {"id": {"type": "string"}}, "required": ["id"]}}}]})
    rec("assistant create (control) is skipped, not blocked", asst.status_code == 200 and skipped(asst), verdict(asst))
    aid = asst.json()["id"]

    th = f.req("POST", "/threads", {})
    rec("thread create without messages (control) is skipped", th.status_code == 200 and skipped(th), verdict(th))
    tid = th.json()["id"]

    m = f.req("POST", f"/threads/{tid}/messages", {"role": "user", "content": "What is the status of order 4417?"})
    rec("message create is scored (user prompt guarded inline)", m.status_code == 200 and scored(m), verdict(m))

    th2 = f.req("POST", "/threads", {"messages": [{"role": "user", "content": "Hello, I need help with order 5521."}]})
    rec("thread create WITH messages is scored", th2.status_code == 200 and scored(th2), verdict(th2))

    ru = f.req("POST", f"/threads/{tid}/runs", {"assistant_id": aid})
    rec("run create without messages (control) is skipped", ru.status_code == 200 and skipped(ru), verdict(ru))
    rid = ru.json()["id"]
    st = {}
    for _ in range(45):
        time.sleep(2)
        pr = f.req("GET", f"/threads/{tid}/runs/{rid}")
        st = pr.json()
        if st.get("status") in ("requires_action", "completed", "failed", "cancelled", "expired"):
            break
    rec("run polling (GET) is skipped", skipped(pr), verdict(pr))
    rec("run reaches requires_action (model called the function tool server-side)", st.get("status") == "requires_action", f"status={st.get('status')}")
    if st.get("status") == "requires_action":
        calls = st["required_action"]["submit_tool_outputs"]["tool_calls"]
        so = f.req("POST", f"/threads/{tid}/runs/{rid}/submit_tool_outputs",
                   {"tool_outputs": [{"tool_call_id": c["id"], "output": json.dumps({"order": "4417", "status": "shipped", "eta": "2026-10-01"})} for c in calls]})
        rec("submit_tool_outputs is scored (tool results guarded before the agent consumes them)", so.status_code == 200 and scored(so), verdict(so))
        for _ in range(45):
            time.sleep(2)
            st = f.req("GET", f"/threads/{tid}/runs/{rid}").json()
            if st.get("status") in ("completed", "failed", "cancelled", "expired"):
                break
        rec("run completes after the tool outputs", st.get("status") == "completed", f"status={st.get('status')}")
    ms = f.req("GET", f"/threads/{tid}/messages")
    reply = next((m for m in ms.json().get("data", []) if m.get("role") == "assistant"), {})
    rtxt = (reply.get("content") or [{}])[0].get("text", {}).get("value", "") if reply else ""
    rec("message listing (GET) is skipped; assistant reply present", skipped(ms) and bool(rtxt), f"{verdict(ms)} reply={rtxt[:60]!r}")

    tr = f.req("POST", "/threads/runs", {"assistant_id": aid, "thread": {"messages": [{"role": "user", "content": "Check order 7730 please."}]}})
    rec("create-thread-and-run is scored", tr.status_code == 200 and scored(tr), verdict(tr))
    th3 = f.req("POST", "/threads", {}).json()["id"]
    ar = f.req("POST", f"/threads/{th3}/runs", {"assistant_id": aid, "additional_messages": [{"role": "user", "content": "Check order 8841."}]})
    rec("run create with additional_messages is scored", ar.status_code == 200 and scored(ar), verdict(ar))

    if not a.skip_block:
        require(local, "STRAIKER_PLATFORM_PAT", "STRAIKER_INTEGRATION_ID")
        ph = {"Authorization": f"Bearer {jwt_from_pat(local['STRAIKER_PLATFORM_PAT'])}", "Content-Type": "application/json"}
        agent = find_agent(ph, local["STRAIKER_INTEGRATION_ID"], a.agent)
        print(f"PUT social_security_number=block on {a.agent} ({agent}) ->", set_control(ph, agent, "social_security_number", "block"), flush=True)
        try:
            def probe():
                t = f.req("POST", "/threads", {}).json()["id"]
                r = f.req("POST", f"/threads/{t}/messages", {"role": "user", "content": SSN})
                return (r.status_code == 400, (r, t))
            ok, (r, t) = wait_for(probe, 300, 10)
            body = r.json() if r.headers.get("content-type", "").startswith("application/json") else {}
            stored = len(f.req("GET", f"/threads/{t}/messages").json().get("data", []))
            rec("blocked message create -> 400 content_filter, never stored", ok and body.get("error", {}).get("code") == "content_filter" and stored == 0,
                f"{verdict(r)} error={body.get('error', {}).get('code')} message={body.get('error', {}).get('message', '')[:50]!r} stored={stored}")
            r2 = f.req("POST", "/threads/runs", {"assistant_id": aid, "thread": {"messages": [{"role": "user", "content": SSN}]}})
            rec("blocked create-thread-and-run -> 400, no run started", r2.status_code == 400, verdict(r2))
            t4 = f.req("POST", "/threads", {}).json()["id"]
            r3 = f.req("POST", f"/threads/{t4}/runs", {"assistant_id": aid, "additional_messages": [{"role": "user", "content": SSN}]})
            runs = f.req("GET", f"/threads/{t4}/runs").json().get("data", [])
            rec("blocked run with additional_messages -> 400, no run on the thread", r3.status_code == 400 and len(runs) == 0, f"{verdict(r3)} runs={len(runs)}")
            t5 = f.req("POST", "/threads", {}).json()["id"]
            r4 = f.req("POST", f"/threads/{t5}/messages", {"role": "user", "content": "What is the status of order 4417?"})
            rec("benign message still passes and is stored while the control blocks", r4.status_code == 200 and len(f.req("GET", f"/threads/{t5}/messages").json().get("data", [])) == 1, verdict(r4))
        finally:
            print("cleanup ->", clear_control(ph, agent, "social_security_number"), flush=True)
    f.req("DELETE", f"/assistants/{aid}")
    failed = [n for n, ok, _ in results if not ok]
    (HERE / "out" / f"foundry-agent-{time.strftime('%H%M')}.json").write_text(json.dumps([{"name": n, "ok": ok, "detail": d} for n, ok, d in results], indent=1))
    print(f"\n{len(results) - len(failed)}/{len(results)} passed")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
