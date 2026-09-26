#!/usr/bin/env python3
"""Mutation check for the inspection-bypass and key-safety fixes.

For each fix: deploy the fragment (or test-API policy) with that one fix reverted, prove APIM stored the
mutant (read back), run the matrix case that pins the fix and require it to FAIL, then restore the original
and read it back. A test that still passes against its mutant does not test the fix.

Usage: python3 tests/v3/mutation_check.py --tap https://<wire-tap-url>   (needs AZURE_SUBSCRIPTION_ID,
       APIM_RG, APIM_NAME as for setup_dev_apis.py; WIRE_TAP_LOCAL=http://127.0.0.1:8798 when the tunnel
       hostname does not resolve locally)
"""
from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
import time

import requests

import setup_dev_apis as S

HERE = S.HERE
INBOUND = "straiker-v3-inbound"

GATE = '@(context.Request.Headers.GetValueOrDefault("x-test-token", "") == "{{straiker-test-token}}")'

# (name, target, [(old, new), ...], cases that must fail)
MUTATIONS = [
    ("route-anchor", INBOUND, [("        if (!endsScorable) {", "        if (true) {")], [33]),
    ("compact-size", INBOUND, [('        if (!(bool)context.Variables["straikerV3Parsed"]) { return false; }\n'
                                '        return ((JObject)context.Variables["straikerV3Body"]).ToString(Newtonsoft.Json.Formatting.None).Length > cap;',
                                "        return true;")], [32]),
    ("unparseable-refused", INBOUND, [
        ('<set-variable name="straikerV3Unscannable" value="@(string.IsNullOrEmpty((string)context.Variables["straikerV3RouteReason"]) && '
         '!string.IsNullOrEmpty(((string)context.Variables["straikerV3Raw"]).Trim()) && !(bool)context.Variables["straikerV3Parsed"])" />',
         '<set-variable name="straikerV3Unscannable" value="@(false)" />'),
        ('if (!(bool)context.Variables["straikerV3Parsed"]) { return ""; }', 'if (!(bool)context.Variables["straikerV3Parsed"]) { return "unparseable"; }'),
    ], [26, 31]),
    ("detect-url-allowlist", INBOUND, [('            return !(u.Host == "straiker.ai" || u.Host.EndsWith(".straiker.ai", StringComparison.OrdinalIgnoreCase));', "            return false;")], [34]),
    ("detect-url-https-only", INBOUND, [('            if (u.Scheme != "https") { return true; }\n', "")], [34]),
    ("agent-sanitize", INBOUND, [(
        '    <set-variable name="straikerV3Agent" value="@{\n'
        '        var s = (string)context.Variables["straikerV3Agent"] ?? "";\n'
        '        var sb = new System.Text.StringBuilder();\n'
        '        foreach (var ch in s) { if (ch >= \' \' && ch != \'\\u007f\') { sb.Append(ch); if (sb.Length >= 200) { break; } } }\n'
        '        return sb.ToString().Trim();\n'
        '    }" />',
        '    <set-variable name="straikerV3Agent" value="@((string)context.Variables["straikerV3Agent"])" />')], [36]),
    ("test-token-gate", "api:v3-openai", [(GATE, "@(true)")], [35]),
]


def norm(x: str) -> str:
    return re.sub(r"\s+", "", x)


def read_back(arm: S.Arm, target: str) -> str:
    if target.startswith("api:"):
        # An API policy GET with format=rawxml can answer with the XML itself rather than a JSON envelope.
        r = requests.get(f"{arm.base}/apis/{target[4:]}/policies/policy?api-version={S.API_VERSION}&format=rawxml", headers=arm.h, timeout=60)
        t = r.text.lstrip("\ufeff")
        try:
            return (json.loads(t).get("properties") or {}).get("value", "")
        except ValueError:
            return t
    return ((arm.get(f"policyFragments/{target}", "&format=rawxml") or {}).get("properties") or {}).get("value", "")


def put(arm: S.Arm, target: str, xml: str) -> None:
    if target.startswith("api:"):
        arm.put(f"apis/{target[4:]}/policies/policy", {"properties": {"format": "rawxml", "value": xml}})
    else:
        arm.put(f"policyFragments/{target}", {"properties": {"format": "rawxml", "value": xml}})
    if norm(read_back(arm, target)) != norm(xml):
        raise RuntimeError(f"{target}: stored value differs from what was PUT")


def original(target: str) -> str:
    if target.startswith("api:"):
        return S.policy_openai("straiker-v3-api-key", "Authorization", "Bearer {{openai-backend-key}}")
    return (S.FRAGMENTS / f"{target}.xml").read_text()


def run_cases(cases: list[int], tag: str, tap: str) -> dict[int, bool]:
    subprocess.run([sys.executable, "-B", str(HERE / "e2e_matrix.py"), "--only", ",".join(map(str, cases)), "--tag", tag, "--tap", tap],
                   capture_output=True, text=True, timeout=900)
    res = json.loads((HERE / "out" / f"run-{tag}.json").read_text())["results"]
    return {r["n"]: r["ok"] for r in res}


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--tap", required=True)
    ap.add_argument("--settle", type=int, default=20, help="seconds for the gateway to pick up a policy change")
    ap.add_argument("--only", default="", help="comma-separated mutation names")
    a = ap.parse_args()
    sub, rg, apim = os.environ.get("AZURE_SUBSCRIPTION_ID", ""), os.environ.get("APIM_RG", ""), os.environ.get("APIM_NAME", "")
    if not (sub and rg and apim):
        print("need AZURE_SUBSCRIPTION_ID / APIM_RG / APIM_NAME")
        return 2
    arm = S.Arm(sub, rg, apim)
    stamp = time.strftime("%H%M")
    verdicts = []
    chosen = [m for m in MUTATIONS if not a.only or m[0] in a.only.split(",")]
    for name, target, edits, cases in chosen:
        orig = original(target)
        if norm(read_back(arm, target)) != norm(orig):
            print(f"[ABORT] {target} on the instance is not the repo version; run setup_dev_apis.py first")
            return 2
        mutant = orig
        for old, new in edits:
            if mutant.count(old) != 1:
                print(f"[ABORT] {name}: anchor not found exactly once: {old[:70]!r}")
                return 2
            mutant = mutant.replace(old, new)
        try:
            put(arm, target, mutant)
            time.sleep(a.settle)
            got = run_cases(cases, f"mut-{name}-{stamp}", a.tap)
            caught = all(not got.get(c, True) for c in cases)
            verdicts.append((name, caught))
            print(f"[{'CAUGHT' if caught else 'MISSED'}] {name}: cases {cases} -> {'FAIL (as required)' if caught else got}", flush=True)
        finally:
            put(arm, target, orig)
            print(f"         restored {target} (read back identical)", flush=True)
            time.sleep(a.settle)
    all_cases = sorted({c for *_, cs in chosen for c in cs})
    back = run_cases(all_cases, f"mut-restored-{stamp}", a.tap)
    restored_ok = all(back.get(c) for c in all_cases)
    print(f"[{'PASS' if restored_ok else 'FAIL'}] after restore, cases {all_cases} pass again: {back}")
    missed = [n for n, c in verdicts if not c]
    print(f"\n{len(verdicts) - len(missed)}/{len(verdicts)} mutations caught" + (f"; MISSED: {missed}" if missed else ""))
    return 1 if missed or not restored_ok else 0


if __name__ == "__main__":
    sys.exit(main())
