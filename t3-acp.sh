#!/usr/bin/env bash
# acp-mcp-call wrapper that resolves the LIVE T3 Code server and a valid MCP
# credential before each call.
#
# Why this exists:
#   T3 spawns agents with T3_ACP_MCP_ENDPOINT pointing at whatever port was
#   current when the session started. If the desktop app restarts (update,
#   crash, or another T3 server squatting on the port), the server moves and
#   the stale endpoint answers 401 invalid_mcp_credential forever.
#   This wrapper resolves the endpoint from ~/.t3/userdata/server-runtime.json
#   at call time, and if the call still 401s, mints a fresh mcp-client session
#   with the local CLI and retries once.
#
# Usage: t3-acp.sh <tool> <arguments-json>
set -euo pipefail

RUNTIME_FILE="${T3_RUNTIME_FILE:-$HOME/.t3/userdata/server-runtime.json}"

resolve_origin() {
  if [ -f "$RUNTIME_FILE" ]; then
    python3 - "$RUNTIME_FILE" <<'PY' 2>/dev/null || true
import json, sys
print(json.load(open(sys.argv[1])).get("origin", "").rstrip("/"))
PY
  fi
}

ORIGIN="$(resolve_origin)"
if [ -n "$ORIGIN" ]; then
  ENDPOINT="$ORIGIN/mcp"
else
  ENDPOINT="${T3_ACP_MCP_ENDPOINT:-http://127.0.0.1:3773/mcp}"
fi

mint_token() {
  local cli="${T3_CODE_CLI:-}"
  if [ -z "$cli" ]; then
    if [ -x "$HOME/.t3/bin/t3" ]; then
      cli="$HOME/.t3/bin/t3"
    else
      cli="$(command -v t3 2>/dev/null || true)"
    fi
  fi
  [ -n "$cli" ] || return 1
  "$cli" auth session issue \
    --subject mcp-client --label "acp-auto" \
    --scope orchestration:read --scope orchestration:operate \
    --ttl 1h --token-only 2>/dev/null | grep -E '^[A-Za-z0-9._-]{40,}$' | head -1
}

call() {
  ELECTRON_RUN_AS_NODE=1 "$T3_ACP_MCP_NODE" \
    ${T3_ACP_MCP_ENTRYPOINT:+"$T3_ACP_MCP_ENTRYPOINT"} \
    acp-mcp-call "$@"
}

export T3_ACP_MCP_ENDPOINT="$ENDPOINT"

OUT="$(call "$@" 2>&1)" && { printf '%s\n' "$OUT"; exit 0; }

if printf '%s' "$OUT" | grep -q "401"; then
  NEW_TOKEN="$(mint_token)"
  if [ -n "$NEW_TOKEN" ]; then
    export T3_ACP_MCP_AUTHORIZATION="Bearer $NEW_TOKEN"
    OUT2="$(call "$@" 2>&1)" && { printf '%s\n' "$OUT2"; exit 0; }
    printf '%s\n' "$OUT2" >&2
    exit 1
  fi
fi

printf '%s\n' "$OUT" >&2
exit 1
