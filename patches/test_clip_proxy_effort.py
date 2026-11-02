"""Integration check for the antigravity clip proxy effort routing.

Runs the shim's proxy in front of a fake upstream and inspects the request the
shim actually forwards. Proves that selecting 'gemini-3.8-flash-low' sends
thinkingLevel=low upstream (before the fix, no thinkingLevel was sent at all).

Run: python3 test_clip_proxy_effort.py [path-to-acp_clip_proxy_shim.py]
"""
import importlib.util
import json
import os
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

DEFAULT = os.path.expanduser("~/.t3/tools/antigravity-cliproxy/acp_clip_proxy_shim.py")
target = sys.argv[1] if len(sys.argv) > 1 else DEFAULT
spec = importlib.util.spec_from_file_location("shim", target)
if spec is None or spec.loader is None:
    from importlib.machinery import SourceFileLoader

    spec = importlib.util.spec_from_loader("shim", SourceFileLoader("shim", target))
shim = importlib.util.module_from_spec(spec)
spec.loader.exec_module(shim)

received = {}


class FakeUpstream(BaseHTTPRequestHandler):
    def do_POST(self):
        length = int(self.headers.get("Content-Length", "0") or 0)
        received["path"] = self.path
        received["body"] = self.rfile.read(length)
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.end_headers()
        self.wfile.write(b"{}")

    def log_message(self, *args):
        pass


upstream = ThreadingHTTPServer(("127.0.0.1", 0), FakeUpstream)
threading.Thread(target=upstream.serve_forever, daemon=True).start()
shim.ProxyHandler.upstream = f"http://127.0.0.1:{upstream.server_port}"

proxy = ThreadingHTTPServer(("127.0.0.1", 0), shim.ProxyHandler)
threading.Thread(target=proxy.serve_forever, daemon=True).start()

import urllib.request

failures = []


def probe(selected_model, expect_level):
    received.clear()
    shim.state.select(selected_model)
    body = json.dumps({"contents": [{"role": "user", "parts": [{"text": "hi"}]}]}).encode()
    req = urllib.request.Request(
        f"http://127.0.0.1:{proxy.server_port}/v1beta/models/gemini-3.8-flash-high:generateContent",
        data=body,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    with urllib.request.urlopen(req) as resp:
        resp.read()
    forwarded = json.loads(received["body"])
    level = forwarded.get("generationConfig", {}).get("thinkingConfig", {}).get("thinkingLevel")
    ok = level == expect_level
    print(f"{'ok ' if ok else 'FAIL'} {selected_model}: thinkingLevel={level!r} (expected {expect_level!r})")
    if not ok:
        failures.append(selected_model)
    print(f"     forwarded path: {received['path']}")


probe("gemini-3.8-flash-low", "low")
probe("gemini-3.8-flash-medium", "medium")
probe("gemini-3.8-flash-high", "high")
probe("gemini-4-argon-low", "low")

proxy.shutdown()
upstream.shutdown()

if failures:
    sys.exit(f"failed: {failures}")
print("\nall checks passed")
