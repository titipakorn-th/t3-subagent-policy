#!/usr/bin/env bash
# Run the doctor with whatever node this machine has.
#
# A machine without a system node still has the one the mcode runtime ships.
#
#   ./patches/doctor.sh
set -uo pipefail

DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

find_node() {
  if [ -n "${MCODE_NODE:-}" ] && [ -x "$MCODE_NODE" ]; then echo "$MCODE_NODE"; return 0; fi
  if command -v node >/dev/null 2>&1; then command -v node; return 0; fi
  for candidate in "$HOME"/.minimax-code/runtime/node-*/bin/node; do
    [ -x "$candidate" ] && echo "$candidate" && return 0
  done
  return 1
}

NODE="$(find_node)" || {
  echo "no node found on this machine (looked for a system node and ~/.minimax-code/runtime/node-*/bin/node)" >&2
  exit 127
}

exec "$NODE" "$DIR/doctor.mjs" "$@"
