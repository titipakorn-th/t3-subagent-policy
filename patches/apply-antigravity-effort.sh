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
PATCH="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/antigravity-clip-effort.patch"

[ -f "$TARGET" ] || { echo "not found: $TARGET"; exit 1; }
if grep -q "thinking_level_for" "$TARGET"; then
  echo "already patched: $TARGET"; exit 0
fi

cp "$TARGET" "$TARGET.bak-$(date +%s)"
(cd "$(dirname "$TARGET")" && patch -p0 acp_clip_proxy_shim.py < "$PATCH")
python3 -m py_compile "$TARGET" && echo "OK: $TARGET patched and compiles"
