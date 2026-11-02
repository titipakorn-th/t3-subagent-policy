"""Self-check for the effort routing in acp_clip_proxy_shim.py.

Run: python3 test_acp_clip_proxy_shim.py [path-to-acp_clip_proxy_shim.py]
Defaults to the deployed T3 antigravity clip proxy shim.
"""
import importlib.util
import json
import os
import sys

DEFAULT = os.path.expanduser("~/.t3/tools/antigravity-cliproxy/acp_clip_proxy_shim.py")
target = sys.argv[1] if len(sys.argv) > 1 else DEFAULT
if not os.path.exists(target):
    sys.exit(f"shim not found: {target}")

spec = importlib.util.spec_from_file_location("shim", target)
shim = importlib.util.module_from_spec(spec)
spec.loader.exec_module(shim)


def check(label, got, want):
    assert got == want, f"{label}: got {got!r}, want {want!r}"
    print(f"ok  {label}: {got!r}")


check("shim under test", target, target)

# Effort must be derived for every model whose name states a level.
check("flash-low", shim.thinking_level_for("gemini-3.8-flash-low"), "low")
check("flash-medium", shim.thinking_level_for("gemini-3.8-flash-medium"), "medium")
check("flash-high", shim.thinking_level_for("gemini-3.8-flash-high"), "high")
check("argon-low", shim.thinking_level_for("gemini-4-argon-low"), "low")
check("claude-opus-low", shim.thinking_level_for("claude-opus-5-5-low"), "low")

# claude-*-high intentionally leaves the upstream level untouched.
check("claude-opus-high", shim.thinking_level_for("claude-opus-5-5-high"), None)
check("unknown", shim.thinking_level_for("some-model"), None)
check("none", shim.thinking_level_for(None), None)

# The level must reach the request payload.
body = json.dumps({"contents": [{"role": "user"}]}).encode()
out = json.loads(shim.apply_thinking_level(body, "low"))
check("payload level", out["generationConfig"]["thinkingConfig"]["thinkingLevel"], "low")
check("payload contents kept", out["contents"], [{"role": "user"}])
check("no level -> unchanged", shim.apply_thinking_level(body, None), body)
check("bad json -> unchanged", shim.apply_thinking_level(b"not json", "low"), b"not json")

print("\nall checks passed")
