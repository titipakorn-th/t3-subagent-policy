"""Self-check for the T3 MCP endpoint repair in acp_clip_proxy_shim.py.

Two T3 Code servers can share a port: the desktop app binds *:PORT while VS
Code's extension host binds 127.0.0.1:PORT, and the loopback bind wins for
loopback traffic. The 't3-code' MCP server is then loaded against the wrong
server, which refuses the credential with 401 and the whole turn dies with
"The MCP server 't3-code' failed to initialize".

The shim repairs this by probing the candidate addresses of the same port and
keeping the one that accepts the credential.

Run: python3 test_acp_clip_proxy_mcp.py [path-to-acp_clip_proxy_shim.py]
Defaults to the deployed T3 antigravity clip proxy shim.
"""
import importlib.util
import os
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

DEFAULT = os.path.expanduser("~/.t3/tools/antigravity-cliproxy/acp_clip_proxy_shim.py")


def target_from_argv():
    """The shim to test.

    Under pytest sys.argv holds the test files, so only a file that does not
    look like a test is taken as the shim.
    """
    for arg in sys.argv[1:]:
        name = os.path.basename(arg)
        if name.endswith(".py") and not name.startswith("test_") and os.path.exists(arg):
            return arg
    return DEFAULT


target = target_from_argv()
if not os.path.exists(target):
    sys.exit(f"shim not found: {target}")

spec = importlib.util.spec_from_file_location("shim", target)
shim = importlib.util.module_from_spec(spec)
spec.loader.exec_module(shim)

TOKEN = "Bearer test-token"


class FakeMcpServer(BaseHTTPRequestHandler):
    """Answers like T3 Code's MCP endpoint: 405 on GET, 401 on a bad token."""

    accepts = TOKEN

    def do_GET(self):
        if self.headers.get("authorization") != FakeMcpServer.accepts:
            self.send_response(401)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(b'{"error":"invalid_mcp_credential"}')
            return
        self.send_response(405)
        self.send_header("allow", "POST, DELETE")
        self.end_headers()

    def log_message(self, *args):
        pass


def check(label, got, want):
    assert got == want, f"{label}: got {got!r}, want {want!r}"
    print(f"ok  {label}: {got!r}")


def stdio_entry(endpoint, authorization=TOKEN):
    return {
        "name": "t3-code",
        "type": "stdio",
        "command": "/bin/true",
        "args": [],
        "env": [
            {"name": "ELECTRON_RUN_AS_NODE", "value": "1"},
            {"name": "T3_ACP_MCP_ENDPOINT", "value": endpoint},
            {"name": "T3_ACP_MCP_AUTHORIZATION", "value": authorization},
        ],
    }


def endpoint_of(entry):
    for item in entry["env"]:
        if item["name"] == "T3_ACP_MCP_ENDPOINT":
            return item["value"]
    return None


def test_endpoint_resolution():
    good = ThreadingHTTPServer(("127.0.0.1", 0), FakeMcpServer)
    threading.Thread(target=good.serve_forever, daemon=True).start()
    good_url = f"http://127.0.0.1:{good.server_port}/mcp"

    try:
        # The credential is accepted here, so the endpoint stays as it is.
        check("good endpoint kept", shim.resolve_mcp_endpoint(good_url, TOKEN), good_url)

        # A refused credential must not be resolved to a working endpoint:
        # the credential belongs to one server only.
        check("refused credential unresolved",
              shim.resolve_mcp_endpoint(good_url, "Bearer wrong"), good_url)

        # A dead endpoint is left alone rather than guessed at.
        dead = "http://127.0.0.1:1/mcp"
        check("dead endpoint unchanged", shim.resolve_mcp_endpoint(dead, TOKEN), dead)

        # The candidate list keeps the original endpoint first and adds the
        # machine's other addresses on the same port.
        candidates = shim.endpoint_candidates("http://127.0.0.1:3773/mcp")
        check("original endpoint is first", candidates[0], "http://127.0.0.1:3773/mcp")
        check("candidates keep the port", all(":3773/mcp" in c for c in candidates), True)
        check("candidates stay unique", len(candidates) == len(set(candidates)), True)
    finally:
        good.shutdown()


def test_payload_repair():
    good = ThreadingHTTPServer(("127.0.0.1", 0), FakeMcpServer)
    threading.Thread(target=good.serve_forever, daemon=True).start()
    good_url = f"http://127.0.0.1:{good.server_port}/mcp"

    try:
        # A stdio entry whose endpoint already accepts the credential is
        # left untouched.
        entry = stdio_entry(good_url)
        params = {"mcpServers": [entry]}
        check("working stdio entry unchanged", shim.rewrite_mcp_endpoint(params), False)
        check("working stdio endpoint kept", endpoint_of(entry), good_url)

        # An entry pointing at a dead endpoint cannot be repaired, so it
        # stays as it is.
        entry = stdio_entry("http://127.0.0.1:1/mcp")
        params = {"mcpServers": [entry]}
        check("unrepairable stdio entry unchanged", shim.rewrite_mcp_endpoint(params), False)
        check("unrepairable stdio endpoint kept", endpoint_of(entry), "http://127.0.0.1:1/mcp")

        # An HTTP entry is repaired the same way.
        params = {"mcpServers": [{
            "name": "t3-code",
            "type": "http",
            "url": good_url,
            "headers": [{"name": "Authorization", "value": TOKEN}],
        }]}
        check("working http entry unchanged", shim.rewrite_mcp_endpoint(params), False)
        check("working http url kept", params["mcpServers"][0]["url"], good_url)

        # A payload without MCP servers is left alone.
        check("no mcpServers", shim.rewrite_mcp_endpoint({}), False)
        check("mcpServers is null", shim.rewrite_mcp_endpoint({"mcpServers": None}), False)
    finally:
        good.shutdown()


def test_shadowed_loopback():
    """The real failure: another server holds the loopback port.

    The endpoint the host hands over answers 401, while the same port on the
    machine's other address answers with the credential. The shim must move
    the endpoint to the one that works.
    """
    accepted = {}
    original = shim.accepts_credential

    def fake_accepts(endpoint, authorization, timeout=2.0):
        if endpoint in accepted:
            return accepted[endpoint]
        return original(endpoint, authorization, timeout)

    def fake_candidates(endpoint):
        return [endpoint, "http://192.168.11.6:3773/mcp"]

    accepted["http://127.0.0.1:3773/mcp"] = False
    accepted["http://192.168.11.6:3773/mcp"] = True
    shim.accepts_credential = fake_accepts
    shim.endpoint_candidates = fake_candidates
    shim._mcp_endpoints.clear()

    try:
        entry = stdio_entry("http://127.0.0.1:3773/mcp")
        params = {"mcpServers": [entry]}
        check("shadowed stdio entry reported", shim.rewrite_mcp_endpoint(params), True)
        check("shadowed endpoint moved", endpoint_of(entry), "http://192.168.11.6:3773/mcp")

        # The same holds for the HTTP form.
        params = {"mcpServers": [{
            "name": "t3-code",
            "type": "http",
            "url": "http://127.0.0.1:3773/mcp",
            "headers": [{"name": "Authorization", "value": TOKEN}],
        }]}
        check("shadowed http entry reported", shim.rewrite_mcp_endpoint(params), True)
        check("shadowed http url moved",
              params["mcpServers"][0]["url"], "http://192.168.11.6:3773/mcp")
    finally:
        shim.accepts_credential = original
        shim.endpoint_candidates = fake_candidates
        shim._mcp_endpoints.clear()


def main():
    test_endpoint_resolution()
    test_payload_repair()
    test_shadowed_loopback()
    print("\nall checks passed")


if __name__ == "__main__":
    main()
