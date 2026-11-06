#!/usr/bin/env bash
# Install the repo's antigravity clip proxy shim on this machine.
#
# The shim T3 ships here may be an older revision: it lists claude-*-high and
# friends as separate models instead of offering a thinking level selector, and
# it only sent an effort upstream for the models in its PROXY_MODELS table. The
# repo carries the known-good shim, which fixes both.
#
#   ./apply-antigravity-effort.sh [path-to-acp_clip_proxy_shim.py]
set -euo pipefail

TARGET="${1:-$HOME/.t3/tools/antigravity-cliproxy/acp_clip_proxy_shim.py}"
[ -f "$TARGET" ] || { echo "not found: $TARGET"; exit 1; }
python3 "$(dirname "${BASH_SOURCE[0]}")/apply-antigravity-effort.py" "$TARGET"
python3 -m py_compile "$TARGET" && echo "OK: $TARGET installed and compiles"
