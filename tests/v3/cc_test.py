#!/usr/bin/env python3
"""Drive real Claude Code through the v3-anthropic test API.

Claude Code authenticates with a per-developer gateway key (ANTHROPIC_AUTH_TOKEN)
that straiker-gateway-auth maps to an email; the v3 fragment recognises the
`claude-cli/` User-Agent and names the application "Claude (APIM)" with
x-s6r-client: claude on EVERY call, so the sidecar calls (title generation,
summaries) stay in the same session instead of splitting into "Autonomous".

Runs N headless sessions (`claude -p`, stdin from /dev/null) that each read a
file with the Read tool, records the run tag, and writes a run file for
console_verify.py (expects agent "Claude (APIM)" typed coding_agent with
tool_call/tool_result events and identity alice@e2e.test).

Usage: python3 tests/v3/cc_test.py [--sessions 3] [--concurrency 3] [--user alice|raj]
"""
from __future__ import annotations

import argparse
import concurrent.futures as cf
import json
import os
import pathlib
import subprocess
import sys
import tempfile
import time

HERE = pathlib.Path(__file__).resolve().parent
OUT = HERE / "out"
OUT.mkdir(exist_ok=True)


sys.path.insert(0, str(HERE))
from harness_env import require, settings  # noqa: E402


def one_session(i: int, gw: str, key: str, model: str, prompt: str) -> dict:
    d = tempfile.mkdtemp(prefix=f"cc-e2e-{i}-")
    pathlib.Path(d, "config.yaml").write_text(f"port: {8000 + i}\nowner: team-{i}\nnote: session {i}\n")
    env = {k: v for k, v in os.environ.items() if k != "ANTHROPIC_API_KEY"}
    env.update({"ANTHROPIC_BASE_URL": gw, "ANTHROPIC_AUTH_TOKEN": key, "CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC": "0"})
    t = time.time()
    p = subprocess.run(["claude", "-p", prompt, "--model", model, "--allowedTools", "Read", "--output-format", "json"],
                       cwd=d, env=env, stdin=subprocess.DEVNULL, capture_output=True, text=True, timeout=300)
    out = {"i": i, "rc": p.returncode, "elapsed": round(time.time() - t, 1), "stderr": p.stderr[-400:]}
    try:
        j = json.loads(p.stdout)
        out.update({"is_error": j.get("is_error"), "result": (j.get("result") or "")[:160], "num_turns": j.get("num_turns"), "session_id": j.get("session_id")})
    except ValueError:
        out["stdout"] = p.stdout[-400:]
    return out


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--sessions", type=int, default=3)
    ap.add_argument("--concurrency", type=int, default=3)
    ap.add_argument("--user", default="alice")
    ap.add_argument("--model", default="claude-haiku-4-5")
    ap.add_argument("--tag", default=time.strftime("cc-%H%M"))
    a = ap.parse_args()
    env = settings()
    require(env, "APIM_GATEWAY_URL", "E2E_KEY_ALICE", "E2E_KEY_RAJ")
    gw = env["APIM_GATEWAY_URL"].rstrip("/") + "/v3-anthropic"
    key = env["E2E_KEY_ALICE"] if a.user == "alice" else env["E2E_KEY_RAJ"]
    prompt = "Read config.yaml and tell me the port and owner. Answer in one line."
    started = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    with cf.ThreadPoolExecutor(max_workers=a.concurrency) as ex:
        results = list(ex.map(lambda i: one_session(i, gw, key, a.model, prompt), range(a.sessions)))
    ok = [r for r in results if r.get("rc") == 0 and not r.get("is_error") and "port" in (r.get("result") or "").lower()]
    for r in results:
        print(json.dumps(r))
    run = {"expect": {"tag": a.tag, "started_at": started, "agents": {"Claude (APIM)": {"cases": [11], "identity": f"{a.user}@e2e.test", "expect_type": "coding_agent", "min_sessions": len(ok)}},
                      "sessions": {"claude_session_ids": [r.get("session_id") for r in results]}, "notes": []}, "results": results}
    (OUT / f"run-{a.tag}.json").write_text(json.dumps(run, indent=1))
    print(f"\n{len(ok)}/{len(results)} Claude Code sessions completed through APIM; run file run-{a.tag}.json")
    return 0 if len(ok) == len(results) else 1


if __name__ == "__main__":
    sys.exit(main())
