#!/usr/bin/env python3
"""Automatic agent enumeration (straikerAgentNameFrom) and the one-shape-per-agent rule, live.

Sends UNNAMED traffic (no straikerAgentRef, no x-s6r-agent, no coding-agent User-Agent) and
checks, through the Platform API after indexing, which agent each call was filed under:

  api (default)  through v3-enum, whose display name is "Contoso Claims Assistant"
                 -> agent "Contoso Claims Assistant" (the API display name, not the id)
  subscription   -> the APIM subscription's display name
  product        -> the APIM product's display name
  jwt-app        through v3-entra with an app-only Entra token -> the calling application id
  none           -> the platform catch-all "Autonomous (<gateway type>)"

and that chat AND text-completion traffic on the same auto-named agent are both scored (the
fragment relays both as messages, so the agent's archetype never mismatches a shape).

Usage: python3 tests/v3/enumeration_test.py [--wait 240]
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
import e2e_matrix  # noqa: E402
from console_verify import B, jwt_from_pat, paged  # noqa: E402
from e2e_matrix import GW, PII, SUB, TEST_TOKEN, call, openai_body  # noqa: E402
from harness_env import require, settings  # noqa: E402


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--wait", type=int, default=240)
    a = ap.parse_args()
    s = settings()
    require(s, "STRAIKER_PLATFORM_PAT", "STRAIKER_INTEGRATION_ID", "FOUNDRY_TENANT_ID", "FOUNDRY_CLIENT_ID", "FOUNDRY_CLIENT_SECRET")
    T = f"enum-{int(time.time())}"
    e2e_matrix.RUN_TAG = T
    started = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(time.time() - 30))
    results: list[tuple[str, bool, str]] = []

    def rec(name: str, ok: bool, detail: str) -> None:
        results.append((name, ok, detail))
        print(f"[{'PASS' if ok else 'FAIL'}] {name}: {detail}", flush=True)

    # --- traffic (all unnamed)
    r = call("v3-enum", "/v1/chat/completions", openai_body("What is 2+2? One number."), {"x-session-id": f"{T}-api-chat"})
    rec("api mode: chat call scored", r.status == 200 and r.verdict in ("allow", "detect"), str(r))
    r = call("v3-enum", "/v1/chat/completions", openai_body(PII), {"x-session-id": f"{T}-api-chat-pii"})
    rec("api mode: chat PII detected on the auto-named agent", r.verdict == "detect", str(r))
    r = call("v3-enum", "/v1/completions", {"model": "gpt-3.5-turbo-instruct", "prompt": e2e_matrix.tagged(PII), "max_tokens": 10}, {"x-session-id": f"{T}-api-cmpl-pii"})
    rec("api mode: text-completion PII ALSO detected on the same agent (relayed as messages)", r.verdict == "detect", str(r))
    for mode in ("subscription", "product", "none"):
        r = call("v3-enum", "/v1/chat/completions", openai_body(f"Name a colour ({mode})."), {"x-session-id": f"{T}-{mode}", "x-test-agentnamefrom": mode})
        rec(f"{mode} mode: call scored", r.status == 200 and r.verdict in ("allow", "detect"), str(r))
    tenant, client, secret = s["FOUNDRY_TENANT_ID"], s["FOUNDRY_CLIENT_ID"], s["FOUNDRY_CLIENT_SECRET"]
    tok = requests.post(f"https://login.microsoftonline.com/{tenant}/oauth2/v2.0/token",
                        data={"grant_type": "client_credentials", "client_id": client, "client_secret": secret, "scope": "https://management.azure.com/.default"}, timeout=30).json()["access_token"]
    jr = requests.post(f"{GW}/v3-entra/v1/chat/completions", headers={"Authorization": f"Bearer {tok}", "Content-Type": "application/json", "x-session-id": f"{T}-jwt", "x-test-agentnamefrom": "jwt-app", "x-test-token": TEST_TOKEN},
                       json=openai_body("Say hi."), timeout=120)
    rec("jwt-app mode: call through the Entra-validated API scored", jr.status_code == 200 and jr.headers.get("x-straiker-verdict") in ("allow", "detect"), f"HTTP {jr.status_code} verdict={jr.headers.get('x-straiker-verdict')}")

    # --- platform view
    print(f"waiting {a.wait}s for estate indexing…", flush=True)
    time.sleep(a.wait)
    h = {"Authorization": f"Bearer {jwt_from_pat(s['STRAIKER_PLATFORM_PAT'])}"}
    arm_sub = requests.get(f"{GW}/v3-enum/v1/models", headers={"Ocp-Apim-Subscription-Key": SUB}, timeout=60)  # noqa: F841  (keeps the subscription warm)
    res = paged(f"{B}/api/v3/integrations/{s['STRAIKER_INTEGRATION_ID']}/resources", h, {"limit": 100})
    by = {(x.get("label") or x.get("name")): x for x in res}
    # The catch-all is named after the integration's gateway type; find it rather than assume the type.
    catchall = next((lbl for lbl in by if (lbl or "").startswith("Autonomous (")), "Autonomous (<gateway type>)")

    def owned(label: str) -> list[dict]:
        node = by.get(label)
        if not node:
            return []
        return [s for s in paged(f"{B}/api/v3/activity/sessions", h, {"agent_id": node["id"], "limit": 100}) if (s.get("started_at") or "") >= started]

    expect = {
        "api (display name)": "Contoso Claims Assistant",
        "subscription": "e2e-v3",
        "product": "Unlimited",
        "none (catch-all)": catchall,
        "jwt-app": client,
    }
    for mode, label in expect.items():
        ss = owned(label)
        rec(f"{mode} -> agent {label!r} owns sessions", len(ss) > 0, f"sessions={len(ss)} minted={'yes' if label in by else 'no'}")
    rec("the API display name (not the id 'v3-enum') is the name", "Contoso Claims Assistant" in by and "v3-enum" not in by, f"display-name agent minted={'Contoso Claims Assistant' in by}")
    api_sessions = owned("Contoso Claims Assistant")
    rec("chat + completion both landed on the auto-named agent", len(api_sessions) >= 3, f"sessions={len(api_sessions)}")
    failed = [n for n, ok, _ in results if not ok]
    (HERE / "out" / f"enumeration-{time.strftime('%H%M')}.json").write_text(json.dumps([{"name": n, "ok": ok, "detail": d} for n, ok, d in results], indent=1))
    print(f"\n{len(results) - len(failed)}/{len(results)} passed")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
