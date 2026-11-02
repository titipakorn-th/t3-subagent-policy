#!/usr/bin/env python3
"""Apply the subagent effort fix to T3's antigravity clip proxy shim.

The shim only sent an effort upstream for the models in its PROXY_MODELS table
(argon, claude). Every other model - including gemini-3.8-flash-low - was
forwarded untouched, so requesting a low-effort model still ran at the upstream
default. The fix derives the effort from the selected model's name suffix.

Different machines run slightly different shim revisions, so this edits the code
by shape rather than by byte offset.

    python3 apply-antigravity-effort.py [path-to-acp_clip_proxy_shim.py]
"""
import os
import shutil
import sys
import time

DEFAULT = os.path.expanduser("~/.t3/tools/antigravity-cliproxy/acp_clip_proxy_shim.py")
TARGET = sys.argv[1] if len(sys.argv) > 1 else DEFAULT

HELPERS = '''
THINKING_LEVELS = ("low", "medium", "high")


def thinking_level_for(model):
    """Effort to send upstream for the model the client selected.

    Routed models (argon, claude) carry their level in PROXY_MODELS. Anything else is
    forwarded as-is, so the requested suffix is the only effort signal available:
    without it 'gemini-3.8-flash-low' silently ran at the upstream default.
    """
    if not isinstance(model, str):
        return None
    if model in PROXY_MODELS:
        return PROXY_MODELS[model][1]
    suffix = model.rsplit("-", 1)[-1]
    return suffix if suffix in THINKING_LEVELS else None


def apply_thinking_level(body, level):
    if body is None or not level:
        return body
    try:
        payload = json.loads(body)
        generation = payload.setdefault("generationConfig", {})
        thinking = generation.setdefault("thinkingConfig", {})
        thinking["thinkingLevel"] = level
        return json.dumps(payload, separators=(",", ":"), ensure_ascii=False).encode("utf-8")
    except (TypeError, ValueError):
        return body


class State:'''

OLD_FORWARD = """                    path = path.replace(old, new, 1)
                    break
        length = int(self.headers.get("Content-Length", "0") or 0)
        body = self.rfile.read(length) if length else None
        if body is not None and body_level:
            try:
                payload = json.loads(body)
                generation = payload.setdefault("generationConfig", {})
                thinking = generation.setdefault("thinkingConfig", {})
                thinking["thinkingLevel"] = body_level
                body = json.dumps(payload, separators=(",", ":"), ensure_ascii=False).encode("utf-8")
            except (TypeError, ValueError):
                pass"""

NEW_FORWARD = """                    path = path.replace(old, new, 1)
                    break
        else:
            body_level = thinking_level_for(selected_model)
        length = int(self.headers.get("Content-Length", "0") or 0)
        body = self.rfile.read(length) if length else None
        body = apply_thinking_level(body, body_level)"""

if not os.path.exists(TARGET):
    sys.exit(f"not found: {TARGET}")

src = open(TARGET).read()

if "thinking_level_for" in src:
    print(f"already patched: {TARGET}")
    sys.exit(0)

if src.count("\nclass State:") != 1:
    sys.exit(f"unrecognised shim layout (no unique 'class State:'): {TARGET}")
if OLD_FORWARD not in src:
    sys.exit(f"unrecognised shim layout (forward block differs): {TARGET}")

src = src.replace("\nclass State:", HELPERS, 1)
src = src.replace(OLD_FORWARD, NEW_FORWARD, 1)

backup = f"{TARGET}.bak-{time.strftime('%Y%m%d-%H%M%S')}"
shutil.copy2(TARGET, backup)
open(TARGET, "w").write(src)

import ast

ast.parse(open(TARGET).read())
print(f"patched: {TARGET} (backup: {backup})")
