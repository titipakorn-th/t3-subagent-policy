#!/usr/bin/env bash
# acp-mcp-call wrapper that finds a reachable T3 Code server and a valid MCP credential.
#
# Why this exists:
#   1. Two T3 servers can share one port. VS Code's extension host runs its own T3
#      server on 127.0.0.1:3773 while the desktop app binds *:3773; loopback traffic
#      goes to the VS Code instance, so every call to http://127.0.0.1:3773/mcp
#      answers 401 invalid_mcp_credential - even for the right token.
#      The desktop server is still reachable on a non-loopback address of the same port.
#   2. The endpoint the agent was launched with goes stale whenever T3 restarts.
#   3. A credential minted before a restart is rejected afterwards.
#
# So: resolve the port from server-runtime.json, try each candidate address, and
# mint a fresh mcp-client session when the inherited credential is refused.
#
# Usage: t3-acp.sh <tool> <arguments-json>
set -uo pipefail

RUNTIME_FILE="${T3_RUNTIME_FILE:-$HOME/.t3/userdata/server-runtime.json}"

read_json() {
  python3 - "$RUNTIME_FILE" "$1" <<'PY' 2>/dev/null || true
import json, os, sys
try:
    print(json.load(open(os.path.expanduser(sys.argv[1])))[sys.argv[2]])
except Exception:
    pass
PY
}

PORT="$(read_json port)"
ORIGIN="$(read_json origin)"

# Candidate addresses for the T3 server, most specific first.
ENDPOINTS=()
add_endpoint() {
  local host="$1" port="$2"
  [ -n "$host" ] && [ -n "$port" ] && ENDPOINTS+=("http://$host:$port/mcp")
}

if [ -n "$ORIGIN" ]; then
  ENDPOINTS+=("${ORIGIN%/}/mcp")
  ORIGIN_HOST="$(printf '%s' "$ORIGIN" | sed -E 's#^[a-z]+://([^:/]+).*#\1#')"
  ORIGIN_PORT="$(printf '%s' "$ORIGIN" | sed -E 's#.*:([0-9]+)/?$#\1#')"
  [ "$ORIGIN_PORT" = "$ORIGIN" ] && ORIGIN_PORT="$PORT"
else
  ORIGIN_HOST=127.0.0.1
  ORIGIN_PORT="$PORT"
fi

# A non-loopback address of the same port reaches the real server.
TS_IP=""
for ts in tailscale /Applications/Tailscale.app/Contents/MacOS/Tailscale; do
  if command -v "$ts" >/dev/null 2>&1 || [ -x "$ts" ]; then
    TS_IP="$("$ts" ip -4 2>/dev/null | head -1)"
    [ -n "$TS_IP" ] && break
  fi
done
LAN_IP=""
if command -v ipconfig >/dev/null 2>&1; then
  for i in en0 en1 en2; do LAN_IP="$(ipconfig getifaddr "$i" 2>/dev/null)"; [ -n "$LAN_IP" ] && break; done
elif command -v hostname >/dev/null 2>&1; then
  LAN_IP="$(hostname -I 2>/dev/null | awk '{print $1}')"
fi

add_endpoint "$TS_IP" "$PORT"
add_endpoint "$LAN_IP" "$PORT"

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
  local endpoint="$1" auth="$2"
  shift 2
  T3_ACP_MCP_ENDPOINT="$endpoint" T3_ACP_MCP_AUTHORIZATION="$auth" \
  ELECTRON_RUN_AS_NODE=1 "$T3_ACP_MCP_NODE" \
    ${T3_ACP_MCP_ENTRYPOINT:+"$T3_ACP_MCP_ENTRYPOINT"} \
    acp-mcp-call "$@"
}

TOKENS=()
[ -n "${T3_ACP_MCP_AUTHORIZATION:-}" ] && TOKENS+=("$T3_ACP_MCP_AUTHORIZATION")
FRESH="$(mint_token || true)"
[ -n "$FRESH" ] && TOKENS+=("Bearer $FRESH")

LAST=""
for auth in "${TOKENS[@]}"; do
  for endpoint in "${ENDPOINTS[@]}"; do
    OUT="$(call "$endpoint" "$auth" "$@" 2>&1)"
    if [ $? -eq 0 ]; then
      printf '%s\n' "$OUT"
      exit 0
    fi
    LAST="$OUT"
  done
done

printf '%s\n' "$LAST" >&2
exit 1
