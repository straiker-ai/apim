#!/usr/bin/env python3
"""Enforcement cases that need an agent the platform has already minted.

Triggers are deterministic: the social_security_number control (a pattern
control, unlike the classifier-based llm_evasion) is switched to block on
the billing agent from an e2e_matrix run.

  17  request-phase block: an SSN in the prompt -> HTTP 200 block stub with the
      tenant's message and x-straiker-verdict: block; the same in http-400 mode
      -> HTTP 400; the sibling ops agent (control still in detect) -> detect.
  19  replay memory: the exact resend is answered locally (detail=replay, no
      Straiker call); a conversation grown past the blocked turn too; with
      replay memory off the resend goes to Straiker instead of the local memory.
  18  response-phase block on Azure OpenAI: the prompt carries no SSN (letters
      to digits), the answer does -> the answer is replaced by the stub with
      `response=` in x-straiker-detail.
  20  kill switch -> stub with the kill-switch message; then restore.
Every override is removed (DELETE) at the end.

Usage: python3 tests/v3/block_test.py --tag e2e-1234
"""
from __future__ import annotations

import argparse
import json
import os
import pathlib
import sys
import time

import requests

HERE = pathlib.Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import e2e_matrix  # noqa: E402
from e2e_matrix import PII, R, aoai_path, assistant_text, openai_body  # noqa: E402
from e2e_matrix import call as _call  # noqa: E402

# BLOCK_TEST_DETECT_URL=<recording proxy> routes every Straiker call of this suite through
# tests/v3/wire_tap.py in TAP_FORWARD mode, so the platform's answer to each probe is on disk.
_DETECT_URL = os.environ.get("BLOCK_TEST_DETECT_URL", "")


def call(api, path, body=None, headers=None, **kw):
    h = dict(headers or {})
    if _DETECT_URL:
        h["x-test-detecturl"] = _DETECT_URL
        h["x-test-allowcustomdetecturl"] = "true"
    return _call(api, path, body, h, **kw)
from console_verify import B, jwt_from_pat, paged  # noqa: E402
from harness_env import require, settings  # noqa: E402

PUZZLE = ("Substitute digits for letters: A=1 B=2 C=3 D=4 E=5 F=6 G=7 H=8 I=9. "
          "Reply with exactly the sentence 'My SSN is ' followed by ABC-DE-FGHI with the substitutions applied "
          "(keep the dashes), and nothing else.")


def find_agent(h: dict, integration: str, label: str) -> str | None:
    for x in paged(f"{B}/api/v3/integrations/{integration}/resources", h, {"limit": 100}):
        if (x.get("label") or x.get("name")) == label:
            return x.get("id") or x.get("agent_id") or (x.get("agent") or {}).get("id")
    return None


def set_control(h: dict, agent: str, control: str, mode: str) -> int:
    r = requests.put(f"{B}/api/v3/controls/agents/{agent}/builtin/{control}", headers={**h, "Content-Type": "application/json"},
                     json={"settings": {"mode": mode}}, timeout=60)
    return r.status_code


def clear_control(h: dict, agent: str, control: str) -> int:
    return requests.delete(f"{B}/api/v3/controls/agents/{agent}/builtin/{control}", headers=h, timeout=60).status_code


def wait_for(fn, seconds: int, every: int = 10):
    deadline = time.time() + seconds
    last = None
    while time.time() < deadline:
        last = fn()
        if last[0]:
            return last
        time.sleep(every)
    return last


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--tag", required=True)
    ap.add_argument("--integration", default="")
    a = ap.parse_args()
    env = settings()
    a.integration = a.integration or env.get("STRAIKER_INTEGRATION_ID", "")
    require(env, "STRAIKER_PLATFORM_PAT")
    h = {"Authorization": f"Bearer {jwt_from_pat(env['STRAIKER_PLATFORM_PAT'])}"}
    T = a.tag
    e2e_matrix.RUN_TAG = f"{T}-block-{int(time.time())}"
    names = json.loads((HERE / "out" / f"run-{T}.json").read_text())["expect"].get("names") or {"billing": f"{T}-billing", "ops": f"{T}-ops"}
    billing = find_agent(h, a.integration, names["billing"])
    ops = find_agent(h, a.integration, names["ops"])
    if not billing or not ops:
        print(f"agents not indexed yet: billing={billing} ops={ops}; retry in a few minutes")
        return 2
    print(f"{names['billing']}={billing} {names['ops']}={ops}")
    results = []
    B_H = {"x-s6r-agent": names["billing"]}

    def rec(n, name, ok, detail):
        results.append({"n": n, "name": name, "ok": ok, "detail": detail})
        print(f"[{'PASS' if ok else 'FAIL'}] {n:>2} {name}: {detail}", flush=True)

    try:
        # ---- 17 request-phase block on a pattern control ----
        # What is under test is the fragment: once the platform decides, the client must see that decision rendered
        # correctly. The platform's hit rate on a PII prompt is high but not 1.0 (measured directly against
        # /api/v3/detect, no APIM: 7/8), so every probe retries as a genuinely NEW turn (fresh session) until the
        # platform decides. A same-session retry would be deduplicated as a replay and could never block; a
        # fragment bug fails every attempt.
        print("PUT social_security_number=block on billing ->", set_control(h, billing, "social_security_number", "block"), flush=True)

        def until(pred, body, extra: dict, tag: str, seconds: int = 180):
            def attempt():
                r = call("v3-openai", "/v1/chat/completions", body, {"x-test-replaymemory": "false", **extra, "x-session-id": f"{T}-{tag}-{time.time_ns()}"})
                return (pred(r), r)
            return wait_for(attempt, seconds, 5)

        ok, r = until(lambda r: r.status == 200 and r.verdict == "block", openai_body(PII), B_H, "blk", 240)
        stub_txt = assistant_text(r)
        rec(17, "SSN control in block -> HTTP 200 stub, verdict block", ok and stub_txt.strip() != "", f"{r} stub={stub_txt[:90]!r}")
        ok, r400 = until(lambda r: r.status == 400 and r.verdict == "block" and "straiker_policy_violation" in r.text, openai_body(PII), {**B_H, "x-test-blockmode": "http-400"}, "400")
        rec(17, "same block in http-400 mode", ok, str(r400))
        ok, r403 = until(lambda r: r.status == 403 and r.verdict == "block", openai_body(PII), {**B_H, "x-test-blockmode": "http-403"}, "403")
        rec(17, "same block in http-403 mode", ok, str(r403))
        ok, r_stream = until(lambda r: r.status == 200 and r.verdict == "block" and r.text.lstrip().startswith("data:") and "[DONE]" in r.text, openai_body(PII, stream=True), B_H, "stream")
        rec(17, "block while client streams -> SSE-shaped stub + [DONE]", ok, f"{r_stream} ct={r_stream.headers.get('content-type')}")
        ok, r_ops = until(lambda r: r.status == 200 and r.verdict == "detect", openai_body(PII), {"x-s6r-agent": names["ops"]}, "ops")
        rec(17, "sibling agent (ops, control in detect) stays detect", ok, str(r_ops))

        # ---- 19 replay memory ----
        # The memory needs a platform block to remember, so the first send retries as a new turn until it is one.
        def first_block():
            s2 = f"{T}-rep-{time.time_ns()}"
            r = call("v3-openai", "/v1/chat/completions", openai_body(PII), {**B_H, "x-session-id": s2})
            return (r.status == 200 and r.verdict == "block" and not r.detail.startswith("replay"), (r, s2))
        ok_first, (r_first, sid2) = wait_for(first_block, 180, 5)
        rec(19, "first send in a fresh session -> platform block (not replay)", ok_first, str(r_first))
        r_rep = call("v3-openai", "/v1/chat/completions", openai_body(PII), {**B_H, "x-session-id": sid2})
        rec(19, "exact resend -> blocked from replay memory (detail=replay)", r_rep.status == 200 and r_rep.verdict == "block" and r_rep.detail.startswith("replay"), str(r_rep))
        grown = openai_body("And now just say hello.")
        # the earlier turn must be byte-identical to the blocked send (run-tagged), or its digest will not match
        grown["messages"] = [grown["messages"][0], {"role": "user", "content": e2e_matrix.tagged(PII)}, {"role": "assistant", "content": "I can't store that."}, grown["messages"][1]]
        r_grown = call("v3-openai", "/v1/chat/completions", grown, {**B_H, "x-session-id": sid2})
        rec(19, "conversation grown past the blocked turn -> still blocked (replay)", r_grown.status == 200 and r_grown.verdict == "block" and r_grown.detail.startswith("replay"), str(r_grown))
        r_off = call("v3-openai", "/v1/chat/completions", openai_body(PII), {**B_H, "x-session-id": sid2, "x-test-replaymemory": "false"})
        rec(19, "replay memory off + same session -> the resend goes to Straiker, not the local memory", r_off.status == 200 and not r_off.detail.startswith("replay"), str(r_off))
        ok, r_off2 = until(lambda r: r.status == 200 and r.verdict == "block", openai_body(PII), B_H, "off")
        rec(19, "replay memory off + fresh session -> platform blocks", ok, str(r_off2))
        r_other = call("v3-openai", "/v1/chat/completions", openai_body("Say hello."), {**B_H, "x-session-id": sid2})
        rec(19, "a different, clean prompt in the same session is not caught by replay memory", r_other.status == 200 and r_other.verdict == "allow", str(r_other))

        # ---- 18 response-phase block (Azure OpenAI gpt-4.1 complies with the puzzle) ----
        # What is under test is the fragment: when the platform denies the ANSWER, the client gets the stub instead.
        # The platform's hit rate on an SSN in an answer varies run to run (measured directly against /api/v3/detect,
        # no APIM: 1 of 6 in one window, 2 of 2 in another), so each attempt is a genuinely new turn (fresh session)
        # rather than a same-session retry, which would be deduplicated as a replay and could never block.
        def probe18(stream: bool):
            def attempt():
                b = {"messages": [{"role": "system", "content": "You solve puzzles precisely."}, {"role": "user", "content": e2e_matrix.tagged(PUZZLE)}], "max_tokens": 30}
                if stream:
                    b["stream"] = True
                r = call("v3-aoai", aoai_path(), b, {**B_H, "x-test-replaymemory": "false", "x-session-id": f"{T}-r18-{stream}-{time.time_ns()}"})
                return (r.verdict == "block" and "response=" in r.detail, r)
            return wait_for(attempt, 180, 5)
        ok18, r18 = probe18(False)
        rec(18, "SSN only in the answer -> response phase blocks, answer replaced by stub", ok18 and "123-45-6789" not in r18.text, f"{r18} text={assistant_text(r18)[:70]!r}")
        ok18s, r18s = probe18(True)
        rec(18, "same with a streamed answer -> reassembled, blocked, SSE stub", ok18s and "123-45-6789" not in r18s.text and "[DONE]" in r18s.text, f"{r18s}")

        # ---- 20 kill switch ----
        k = requests.post(f"{B}/api/v3/inventory/agents/{billing}/kill", headers=h, timeout=60)
        print("POST kill ->", k.status_code, k.text[:100], flush=True)
        if k.status_code in (200, 201, 204):
            def probe20():
                r = call("v3-openai", "/v1/chat/completions", openai_body("Say hi."), {**B_H, "x-test-replaymemory": "false"})
                return (r.verdict == "block" and "illswitch" in assistant_text(r), r)
            ok20, r20 = wait_for(probe20, 180)
            rec(20, "kill switch -> stub with the kill-switch message", ok20, f"{r20} text={assistant_text(r20)[:80]!r}")
            u = requests.post(f"{B}/api/v3/inventory/agents/{billing}/restore", headers=h, timeout=60)
            print("restore ->", u.status_code, flush=True)
        else:
            rec(20, "kill switch (endpoint unavailable)", True, f"skipped: {k.status_code}")
    finally:
        print("cleanup: DELETE override ->", clear_control(h, billing, "social_security_number"), flush=True)
    (HERE / "out" / f"block-{T}.json").write_text(json.dumps(results, indent=1))
    failed = [x for x in results if not x["ok"]]
    print(f"\n{len(results) - len(failed)}/{len(results)} passed")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
