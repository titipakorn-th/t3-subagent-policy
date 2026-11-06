#!/usr/bin/env python3
"""Install the repo's antigravity clip proxy shim, with the effort fix applied.

Two things are wrong with the shim T3 ships on a machine:

1. It only sent an effort upstream for the models in its PROXY_MODELS table
   (argon, claude). Every other model - including gemini-3.8-flash-low - was
   forwarded untouched, so requesting a low-effort model still ran at the upstream
   default. The fix derives the effort from the selected model's name suffix.

2. Every machine runs a different shim revision, and the revisions disagree on
   the model list: an older one offers claude-opus-5-5-low/medium/high as three
   separate models, a newer one offers claude-opus-5-5 and adds a thinking level
   selector next to it. The repo carries the known-good shim so every machine
   ends up with the same one.

So this installs the known-good file over any older revision it recognises, and
falls back to editing the code by shape for a revision it does not carry.

    python3 apply-antigravity-effort.py [path-to-acp_clip_proxy_shim.py]
"""
import ast
import hashlib
import os
import re
import shutil
import sys
import time

DEFAULT = os.path.expanduser("~/.t3/tools/antigravity-cliproxy/acp_clip_proxy_shim.py")
ARGS = [a for a in sys.argv[1:] if a != "--check"]
CHECK = "--check" in sys.argv
TARGET = ARGS[0] if ARGS else DEFAULT
KNOWN_GOOD = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                          "acp_clip_proxy_shim.py")

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


def install(target, replacement, what):
    """Put replacement in place, keep a backup, and never leave a broken file."""
    backup = f"{target}.bak-{time.strftime('%Y%m%dT%H%M%S')}"
    shutil.copy2(target, backup)
    open(target, "w").write(replacement)
    try:
        ast.parse(open(target).read())
    except SyntaxError:
        shutil.copy2(backup, target)
        sys.exit(f"{what} does not parse; restored backup: {target}")
    print(f"backup: {backup}")


if not os.path.exists(TARGET):
    sys.exit(f"not found: {TARGET}")
if not os.path.exists(KNOWN_GOOD):
    sys.exit(f"known-good shim is missing: {KNOWN_GOOD}")

src = open(TARGET).read()
good = open(KNOWN_GOOD).read()

if "class State:" not in src or "PROXY_MODELS" not in src:
    sys.exit(f"unrecognised shim layout (not the clip proxy shim): {TARGET}")


def features(text):
    """The top-level names a shim revision defines."""
    return set(re.findall(r"^(?:def|class)\s+([A-Za-z_]\w*)", text, re.M))


missing = features(good) - features(src)

if CHECK:
    if missing:
        print(f"MISS {TARGET} (older revision, missing: {', '.join(sorted(missing))})")
        sys.exit(1)
    if "thinking_level_for" not in src:
        print(f"MISS {TARGET} (effort fix missing)")
        sys.exit(1)
    print(f"OK   {TARGET} (known-good shim)")
    sys.exit(0)

if src == good:
    print(f"already patched: {TARGET}")
    sys.exit(0)
if missing:
    # An older revision: it does not have everything the known-good has. The
    # known-good file replaces it exactly.
    install(TARGET, good, "the known-good shim")
    print(f"installed the known-good shim over an older revision: {TARGET}")
    print(f"missing from the deployed revision: {', '.join(sorted(missing))}")
    print("new ACP processes pick this up; restart T3 Code or open a new thread.")
    sys.exit(0)

# A revision at least as new as the known-good: keep it, and only make sure the
# effort fix is in it. Replacing it would lose whatever it added.
if "thinking_level_for" in src:
    print(f"already patched: {TARGET}")
    sys.exit(0)

if src.count("\nclass State:") != 1:
    sys.exit(f"unrecognised shim layout (no unique 'class State:'): {TARGET}")
if OLD_FORWARD not in src:
    sys.exit(f"unrecognised shim layout (forward block differs): {TARGET}")

src = src.replace("\nclass State:", HELPERS, 1)
src = src.replace(OLD_FORWARD, NEW_FORWARD, 1)

install(TARGET, src, "the patched shim")
print(f"patched: {TARGET}")
print("new ACP processes pick this up; restart T3 Code or open a new thread.")
