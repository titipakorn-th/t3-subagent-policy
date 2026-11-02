#!/usr/bin/env bash
# Apply the subagent effort fix to T3's antigravity clip proxy shim.
#
# Bug: a selected model whose name states an effort (e.g. gemini-3.8-flash-low) was
# forwarded without any thinkingLevel, so the run used the upstream default effort.
# Only the argon/claude routes set one. This patch derives the level from the model name.
#
#   ./apply-antigravity-effort.sh [path-to-acp_clip_proxy_shim.py]
set -euo pipefail

TARGET="${1:-$HOME/.t3/tools/antigravity-cliproxy/acp_clip_proxy_shim.py}"
[ -f "$TARGET" ] || { echo "not found: $TARGET"; exit 1; }
python3 "$(dirname "${BASH_SOURCE[0]}")/apply-antigravity-effort.py" "$TARGET"
python3 -m py_compile "$TARGET" && echo "OK: $TARGET patched and compiles"
