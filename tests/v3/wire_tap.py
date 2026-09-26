#!/usr/bin/env python3
"""Capture exactly what APIM posts to Straiker.

Runs a local HTTP capture server and exposes it through a cloudflared quick
tunnel; point a test API at it with the `x-test-detecturl` header (or set
straikerDetectUrl). Every POST is stored under tests/v3/out/wire/<n>.json as
{headers, body}; GET /__last returns the most recent one. Answers every POST
with an allow verdict so the gateway keeps working.

Usage: python3 tests/v3/wire_tap.py            (prints the public URL; Ctrl-C to stop)
       TAP_FORWARD=https://api.prod.straiker.ai/api/v3/detect python3 tests/v3/wire_tap.py
         -> recording proxy: every POST is forwarded to the real platform with the same
            headers and body, and the platform's status + body are stored next to the
            request (`response` key), so what APIM sent AND what it got back are on disk.
            A POST whose path starts with /capture-only is recorded but never forwarded
            (for a probe that must not create anything on the tenant); /capture-only/delay
            also waits 10 s before answering (a detect timeout that never leaves this host).
"""
from __future__ import annotations

import http.server
import json
import os
import pathlib
import re
import subprocess
import sys
import threading
import time

HERE = pathlib.Path(__file__).resolve().parent
WIRE = HERE / "out" / "wire"
WIRE.mkdir(parents=True, exist_ok=True)
PORT = 8798
FORWARD = os.environ.get("TAP_FORWARD", "")
_last: dict = {}
_n = 0


class H(http.server.BaseHTTPRequestHandler):
    def do_POST(self):
        global _last, _n
        raw = self.rfile.read(int(self.headers.get("content-length", 0)))
        try:
            body = json.loads(raw)
        except ValueError:
            body = raw.decode("utf-8", "replace")
        hdrs = {k: (v[:18] + "…" if k.lower() == "authorization" else v) for k, v in self.headers.items()}
        _last = {"ts": time.time(), "path": self.path, "headers": hdrs, "body": body}
        _n += 1
        status = 200
        if self.path.startswith("/capture-only/delay"):
            time.sleep(10)
        if FORWARD and not self.path.startswith("/capture-only"):
            import requests  # local import: only the proxy mode needs it
            fwd_h = {k: v for k, v in self.headers.items() if k.lower() not in ("host", "content-length", "cf-connecting-ip", "cf-ray", "cf-visitor", "cf-ipcountry", "cf-warp-tag-id", "cf-worker", "cf-ew-via", "cdn-loop", "x-forwarded-for", "x-forwarded-proto", "connection")}
            try:
                up = requests.post(FORWARD, headers=fwd_h, data=raw, timeout=60)
                out = up.content
                status = up.status_code
                try:
                    _last["response"] = {"status": up.status_code, "body": up.json()}
                except ValueError:
                    _last["response"] = {"status": up.status_code, "body": up.text[:2000]}
            except Exception as e:  # noqa: BLE001
                out = json.dumps({"error": str(e)}).encode()
                status = 502
                _last["response"] = {"status": 502, "error": str(e)}
        else:
            out = json.dumps({"hookSpecificOutput": {"hookEventName": "GatewayRequest", "permissionDecision": "allow", "permissionDecisionReason": "allow"},
                              "straiker": {"turn_id": f"tap-{_n}", "action": "allow", "controls": [], "blocked_by": [], "events_scored": 1}}).encode()
        (WIRE / f"{_n:03d}.json").write_text(json.dumps(_last, indent=1))
        self.send_response(status)
        self.send_header("content-type", "application/json")
        self.send_header("content-length", str(len(out)))
        self.end_headers()
        self.wfile.write(out)

    def do_GET(self):
        out = json.dumps(_last if self.path.startswith("/__last") else {"captured": _n}).encode()
        self.send_response(200)
        self.send_header("content-type", "application/json")
        self.send_header("content-length", str(len(out)))
        self.end_headers()
        self.wfile.write(out)

    def log_message(self, *a):
        pass


def main() -> int:
    srv = http.server.ThreadingHTTPServer(("127.0.0.1", PORT), H)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    proc = subprocess.Popen(["cloudflared", "tunnel", "--url", f"http://127.0.0.1:{PORT}"], stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
    url = None
    deadline = time.time() + 40
    while time.time() < deadline and url is None:
        line = proc.stdout.readline()
        m = re.search(r"https://[a-z0-9-]+\.trycloudflare\.com", line)
        if m:
            url = m.group(0)
    if not url:
        print("cloudflared did not report a URL", file=sys.stderr)
        return 1
    (HERE / "out" / "wire_tap_url.txt").write_text(url)
    print(f"WIRE_TAP_URL={url}", flush=True)
    try:
        while True:
            time.sleep(3600)
    except KeyboardInterrupt:
        pass
    finally:
        proc.terminate()
    return 0


if __name__ == "__main__":
    sys.exit(main())
