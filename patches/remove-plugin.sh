#!/usr/bin/env bash
# Remove a MiniMax Code plugin and stop the MCP servers it left running.
#
# `mcode plugin remove` uninstalls the files but does not stop a server that is
# already running: the process keeps serving the deleted code from memory and
# even rewrites its state file. The agent then still offers the plugin's tools.
#
#   ./patches/remove-plugin.sh goal-mode@local
set -uo pipefail

PLUGIN="${1:-}"
if [ -z "$PLUGIN" ]; then
  echo "usage: remove-plugin.sh <plugin[@marketplace]>" >&2
  exit 2
fi

MCODE="$HOME/.minimax-code/bin/mcode"
[ -x "$MCODE" ] || MCODE="$(command -v mcode || true)"
if [ -z "$MCODE" ]; then
  echo "mcode not found" >&2
  exit 127
fi

NAME="${PLUGIN%@*}"
PLUGIN_DIR="$HOME/.minimax/plugins/$NAME"

# Remember the processes that belong to the plugin before its files disappear.
PIDS="$(ps -eo pid=,command= | awk -v p="$PLUGIN_DIR/" 'index($0, p) {print $1}')"

"$MCODE" plugin remove "$PLUGIN" || exit $?

for pid in $PIDS; do
  if kill "$pid" 2>/dev/null; then
    echo "stopped the leftover server: $pid"
  fi
done

echo "removed $PLUGIN"
