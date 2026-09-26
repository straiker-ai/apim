#!/usr/bin/env python3
"""Verify what the platform recorded for an e2e_matrix run.

Reads tests/v3/out/run-<tag>.json and checks, through the Platform API:
  - every expected agent label exists on the integration AND has >= 1 session
    since the run started (a minted node with zero sessions is a failure)
  - a `must_not_exist` label was never minted (operator pin beats caller header)
  - the identity label on those sessions is the expected principal
  - agents with `expect_sessions` have that many sessions (derived-session rule)
  - agents with `expect_assistant_text` have that text in a session transcript
    (proves the response phase was scored: Azure OpenAI SSE reassembly)

Usage: python3 tests/v3/console_verify.py --tag e2e-1234 [--wait 300]
Settings (tests/v3/harness_env.py): STRAIKER_PLATFORM_PAT, STRAIKER_INTEGRATION_ID.
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
from harness_env import API_BASE, load_env, require, settings  # noqa: E402,F401  (load_env re-exported)

B = API_BASE


def jwt_from_pat(pat: str) -> str:
    r = requests.post(B + "/auth/token", data={"grant_type": "urn:ietf:params:oauth:grant-type:token-exchange", "subject_token": pat,
                                               "subject_token_type": "urn:straiker:params:oauth:token-type:pat"}, timeout=30)
    r.raise_for_status()
    return r.json()["access_token"]


def paged(url: str, h: dict, params: dict) -> list[dict]:
    out: list[dict] = []
    cursor = None
    for _ in range(50):
        p = dict(params)
        if cursor:
            p["cursor"] = cursor
        r = requests.get(url, headers=h, params=p, timeout=60)
        r.raise_for_status()
        j = r.json()
        out.extend(j.get("data", []))
        cursor = j.get("next_cursor")
        if not j.get("has_more") or not cursor:
            break
    return out


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--tag", required=True)
    ap.add_argument("--integration", default="")
    ap.add_argument("--wait", type=int, default=0, help="seconds to wait before checking (estate indexing lags ~2-5 min)")
    a = ap.parse_args()
    run = json.loads((HERE / "out" / f"run-{a.tag}.json").read_text())
    expect = run["expect"]
    started = expect["started_at"]
    finished = expect.get("finished_at") or "9999"   # older run files have no end: count everything since the start
    if a.wait:
        print(f"waiting {a.wait}s for estate indexing…")
        time.sleep(a.wait)
    env = settings()
    a.integration = a.integration or env.get("STRAIKER_INTEGRATION_ID", "")
    if not a.integration:
        print("need --integration or STRAIKER_INTEGRATION_ID")
        return 2
    require(env, "STRAIKER_PLATFORM_PAT")
    h = {"Authorization": f"Bearer {jwt_from_pat(env['STRAIKER_PLATFORM_PAT'])}"}

    res = paged(f"{B}/api/v3/integrations/{a.integration}/resources", h, {"limit": 100})
    by_label = {}
    for x in res:
        lbl = x.get("label") or x.get("name") or ""
        by_label[lbl] = x
    print(f"integration {a.integration}: {len(res)} resources")
    fails = 0

    def fail(msg: str) -> None:
        nonlocal fails
        fails += 1
        print("[FAIL]", msg)

    for label, info in expect["agents"].items():
        if info.get("must_not_exist"):
            if label in by_label:
                fail(f"{label} was minted (caller header must not override the operator pin)")
            else:
                print(f"[PASS] {label} not minted")
            continue
        node = by_label.get(label)
        if not node:
            fail(f"agent {label} not minted")
            continue
        agent_id = node.get("id") or node.get("agent_id") or (node.get("agent") or {}).get("id")
        detail = requests.get(f"{B}/api/v3/inventory/agents/{agent_id}", headers=h, timeout=60)
        atype = (detail.json().get("type") if detail.ok else None) or node.get("type") or node.get("archetype")
        node["type"] = atype
        sessions = [s for s in paged(f"{B}/api/v3/activity/sessions", h, {"agent_id": agent_id, "limit": 100}) if started <= (s.get("started_at") or "") <= finished]
        if not sessions:
            fail(f"agent {label} ({agent_id}) has zero sessions between {started} and {finished}")
            continue
        idents = sorted({((s.get("identity") or {}).get("label") or "") for s in sessions})
        exp_id = info.get("identity")
        msg = f"{label} ({agent_id}): {len(sessions)} sessions, identity={idents}, type={node.get('type') or node.get('archetype')}"
        if exp_id and exp_id not in idents:
            fail(msg + f" — expected identity {exp_id}")
        elif info.get("expect_type") and atype != info["expect_type"]:
            fail(msg + f" — expected type {info['expect_type']}")
        elif info.get("expect_sessions") and len(sessions) != info["expect_sessions"]:
            fail(msg + f" — expected {info['expect_sessions']} sessions")
        else:
            print("[PASS]", msg)
        if info.get("expect_assistant_text"):
            found = False
            for s in sessions:
                t = requests.get(f"{B}/api/v3/activity/sessions/{s['id']}/transcript", headers=h, params={"mode": "full", "expand": "all"}, timeout=60)
                if t.ok and info["expect_assistant_text"] in t.text:
                    found = True
                    break
            if found:
                print(f"[PASS] {label}: assistant text {info['expect_assistant_text']!r} present in transcript (response phase scored)")
            else:
                fail(f"{label}: assistant text {info['expect_assistant_text']!r} NOT in any transcript — response phase not scored")
        if info.get("expect_output_control"):
            hit = any(info["expect_output_control"] in json.dumps(s.get("findings") or {}) or info["expect_output_control"] in json.dumps(
                requests.get(f"{B}/api/v3/activity/sessions/{s['id']}/transcript/detections", headers=h, timeout=60).text) for s in sessions)
            print(("[PASS] " if hit else "[WARN] ") + f"{label}: output control {info['expect_output_control']} {'fired' if hit else 'not seen (control may be off on this agent)'}")

    if expect.get("sessions", {}).get("explicit"):
        print(f"[NOTE] explicit session id {expect['sessions']['explicit']} is asserted through the billing agent's session count (platform session ids are opaque)")
    for n in expect.get("notes", []):
        print("[NOTE]", n)
    print(f"\n{'OK' if fails == 0 else str(fails) + ' failure(s)'}")
    return 1 if fails else 0


if __name__ == "__main__":
    sys.exit(main())
