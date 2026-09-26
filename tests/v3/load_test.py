#!/usr/bin/env python3
"""Volume and latency: N concurrent chat requests through the v3 test API.

Reports, from the run only: request count, HTTP status mix, x-straiker-verdict
mix (any `degraded` = a detect timeout/failure that let traffic through
uninspected), and end-to-end latency p50/p95/max for guarded requests versus
the same requests with scoring switched off (x-test-scorerequest/scoreresponse
false), so the guardrail's added latency is a measured number.

Usage: python3 tests/v3/load_test.py [--n 200] [--concurrency 30] [--api v3-openai|v3-aoai]
"""
from __future__ import annotations

import argparse
import concurrent.futures as cf
import json
import pathlib
import statistics
import sys
import time

HERE = pathlib.Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import e2e_matrix  # noqa: E402
from e2e_matrix import PII, aoai_path, call, openai_body  # noqa: E402

RUN = time.strftime("%H%M%S")
e2e_matrix.RUN_TAG = f"load-{RUN}"  # run-unique prompts: identical turns in a known session are replay-deduped by the platform

PROMPTS = [
    "Give me one word that rhymes with cat.",
    "What is 12 times 12? Number only.",
    "Name a primary colour.",
    PII,
    "Summarise in five words: the quick brown fox jumps over the lazy dog.",
]


def one(i: int, api: str, path: str, guarded: bool, agent: str) -> dict:
    body = openai_body(PROMPTS[i % len(PROMPTS)])
    if api == "v3-aoai":
        body.pop("model", None)
    h = {"x-s6r-agent": agent, "x-session-id": f"load-{RUN}-{agent}-{i}"}  # run-unique session ids, same reason
    if not guarded:
        h.update({"x-test-scorerequest": "false", "x-test-scoreresponse": "false"})
    try:
        r = call(api, path, body, h, timeout=120)
        return {"i": i, "status": r.status, "verdict": r.verdict, "elapsed": r.elapsed, "detail": r.detail[:40]}
    except Exception as e:  # noqa: BLE001
        return {"i": i, "status": 0, "verdict": "exception", "elapsed": 0.0, "detail": str(e)[:80]}


def pct(xs: list[float], p: float) -> float:
    if not xs:
        return 0.0
    xs = sorted(xs)
    k = max(0, min(len(xs) - 1, round(p * (len(xs) - 1))))
    return xs[k]


def run(n: int, conc: int, api: str, path: str, guarded: bool, agent: str) -> dict:
    t = time.time()
    with cf.ThreadPoolExecutor(max_workers=conc) as ex:
        rs = list(ex.map(lambda i: one(i, api, path, guarded, agent), range(n)))
    wall = time.time() - t
    lat = [r["elapsed"] for r in rs if r["status"] == 200]
    return {
        "guarded": guarded, "n": n, "concurrency": conc, "wall_s": round(wall, 1),
        "status": {str(k): sum(1 for r in rs if r["status"] == k) for k in sorted({r["status"] for r in rs})},
        "verdict": {k: sum(1 for r in rs if r["verdict"] == k) for k in sorted({r["verdict"] for r in rs})},
        "p50": round(pct(lat, 0.5), 2), "p95": round(pct(lat, 0.95), 2), "max": round(max(lat), 2) if lat else 0, "mean": round(statistics.mean(lat), 2) if lat else 0,
        "samples": rs,
    }


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--n", type=int, default=100)
    ap.add_argument("--concurrency", type=int, default=8, help="the dev instance is a Developer SKU; 30 produced connection errors even with scoring off")
    ap.add_argument("--api", default="v3-openai")
    ap.add_argument("--agent", default="APIM Load Test")
    a = ap.parse_args()
    path = aoai_path() if a.api == "v3-aoai" else "/v1/chat/completions"
    base = run(a.n, a.concurrency, a.api, path, False, a.agent + " (unguarded)")
    guarded = run(a.n, a.concurrency, a.api, path, True, a.agent)
    for label, r in (("unguarded", base), ("guarded", guarded)):
        print(f"{label:9} n={r['n']} conc={r['concurrency']} wall={r['wall_s']}s status={r['status']} verdict={r['verdict']} p50={r['p50']}s p95={r['p95']}s max={r['max']}s mean={r['mean']}s")
    added = {"p50": round(guarded["p50"] - base["p50"], 2), "p95": round(guarded["p95"] - base["p95"], 2), "mean": round(guarded["mean"] - base["mean"], 2)}
    degraded = guarded["verdict"].get("degraded", 0)
    for label, r in (("unguarded", base), ("guarded", guarded)):
        errs = [x["detail"] for x in r["samples"] if x["status"] == 0]
        if errs:
            print(f"{label} exceptions ({len(errs)}): {errs[0][:160]}")
    print(f"added latency p50={added['p50']}s p95={added['p95']}s mean={added['mean']}s; degraded={degraded}/{a.n} ({100.0 * degraded / a.n:.1f}%)")
    out = HERE / "out" / f"load-{a.api}-{time.strftime('%H%M')}.json"
    out.write_text(json.dumps({"unguarded": {k: v for k, v in base.items() if k != 'samples'}, "guarded": {k: v for k, v in guarded.items() if k != 'samples'}, "added": added, "samples": guarded["samples"]}, indent=1))
    print("wrote", out)
    return 0 if degraded == 0 and guarded["status"].get("200", 0) == a.n else 1


if __name__ == "__main__":
    sys.exit(main())
