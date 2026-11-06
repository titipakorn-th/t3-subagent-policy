#!/usr/bin/env python3
"""Check that the antigravity clip proxy can reach its upstream.

T3 reports the antigravity provider as "ready" and lists its models even when
every request fails, because the shim answers the model list from its own
tables and only reaches the upstream when a prompt is sent. The upstream is
the CLIProxyAPI hub the launcher points at, so an unreachable hub looks
exactly like a healthy provider until you try to use it.

    python3 check-antigravity-upstream.py

Prints what the launcher points at, whether the host resolves, and whether the
hub answers with its model list.
"""
import argparse
import json
import os
import re
import socket
import ssl
import sys
import urllib.error
import urllib.request

DEFAULT_LAUNCHER = os.path.expanduser(
    "~/.t3/tools/antigravity-cliproxy/antigravity-cliproxy-acp.sh")


def read_launcher(path):
    """The hub url and api key the launcher exports."""
    try:
        src = open(path, encoding="utf-8").read()
    except OSError as exc:
        print(f"cannot read the launcher {path}: {exc}")
        return None, None
    # The launcher spells the hub three ways: with a default, quoted, bare.
    hub = re.search(r'CLIPROXY_HUB_URL="\$\{CLIPROXY_HUB_URL:-([^}]+)\}"'
                    r'|CLIPROXY_HUB_URL="([^"]+)"'
                    r'|CLIPROXY_HUB_URL=(\S+)', src)
    key = re.search(r'CLIPROXY_API_KEY="\$\(cat ([^)]+)\)"', src)
    url = next((g for g in hub.groups() if g), None) if hub else None
    return (url, key.group(1) if key else None)


def check_hub(url, api_key_path, timeout):
    host = url.split("//", 1)[-1].split("/", 1)[0]
    port = 443
    if ":" in host:
        host, _, port_text = host.partition(":")
        port = int(port_text)

    try:
        socket.getaddrinfo(host, port, type=socket.SOCK_STREAM)[0]
    except socket.gaierror as exc:
        return f"FAIL  the hub host {host} does not resolve ({exc}).\n" \
               "      The machine cannot see the tailnet the hub lives on: share the\n" \
               "      hub into this machine's tailnet, or join that tailnet."

    api_key = ""
    if api_key_path and os.path.exists(api_key_path):
        api_key = open(api_key_path, encoding="utf-8").read().strip()

    request = urllib.request.Request(url.rstrip("/") + "/v1/models")
    if api_key:
        request.add_header("Authorization", f"Bearer {api_key}")
    try:
        with urllib.request.urlopen(request, timeout=timeout,
                                    context=ssl.create_default_context()) as response:
            body = json.loads(response.read().decode("utf-8", "replace"))
    except urllib.error.HTTPError as exc:
        if exc.code in (401, 403):
            return f"FAIL  the hub answered {exc.code}: the api key is not accepted."
        return f"FAIL  the hub answered HTTP {exc.code}."
    except ssl.SSLCertVerificationError as exc:
        return f"FAIL  the hub certificate did not verify: {exc}.\n" \
               "      The name in the url must match the certificate."
    except urllib.error.URLError as exc:
        return f"FAIL  the hub did not answer: {exc.reason}."
    except (TimeoutError, socket.timeout):
        return f"FAIL  the hub did not answer within {timeout}s."
    except Exception as exc:
        return f"FAIL  the hub could not be reached: {exc}."

    models = [m.get("id") for m in body.get("data", []) if isinstance(m, dict)]
    return f"OK    the hub answered with {len(models)} model(s): {', '.join(models) or '(none)'}"


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--launcher", default=DEFAULT_LAUNCHER)
    parser.add_argument("--timeout", type=float, default=10)
    args = parser.parse_args()

    if not os.path.exists(args.launcher):
        print(f"FAIL  no antigravity clip proxy launcher at {args.launcher}")
        return 1

    hub, key_path = read_launcher(args.launcher)
    hub = os.environ.get("CLIPROXY_HUB_URL", hub)
    if not hub:
        print(f"FAIL  {args.launcher} does not set CLIPROXY_HUB_URL")
        return 1

    print(f"hub:  {hub}")
    print(f"key:  {key_path or '(none found in the launcher)'}")
    verdict = check_hub(hub, key_path, args.timeout)
    print(verdict)
    return 0 if verdict.startswith("OK") else 1


if __name__ == "__main__":
    sys.exit(main())
