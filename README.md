# T3 subagent cost policy + ACP self-heal — rollout to other machines

## Why ACP subagents fail at first (and what fixes it)

Symptom: every ACP/MCP call returns `401 invalid_mcp_credential` — including T3's own client
(`t3 thread list`) and the credential the agent was launched with. Subagents simply never spawn.

Cause found on a real machine: **two independent T3 Code environments were listening at once.**

| Listener | Environment | Policy | Cookie |
|---|---|---|---|
| `127.0.0.1:3773` | T3 running **inside VS Code** (`Code Helper (Plugin)`) | `loopback-browser` | `t3_session_3773_<env-id>` |
| `*:3774` | T3 Code **desktop app** | `remote-reachable` | `t3_session_3774` |

The desktop app's server picks a free port at startup, so it landed on 3774 while VS Code's
instance kept 3773. The agent was launched with `T3_ACP_MCP_ENDPOINT=…:3773/mcp` but a credential
belonging to the **desktop** environment — endpoint and credential from different environments, so
every call 401s. It also recurs whenever the desktop app restarts and shifts ports, because the
agent's env var still points at the old port.

Two fixes, both in this repo:

1. **`t3-acp.sh` — resolve the live server at call time.** It reads
   `~/.t3/userdata/server-runtime.json` (the authoritative origin of the environment that owns your
   credential) on *every* call, so a port change or a stale env var no longer matters.
2. **`t3-acp.sh` — self-heal on 401.** If the call still fails, it mints a fresh `mcp-client`
   session with the local CLI (`t3 auth session issue --subject mcp-client`, same data dir) and
   retries once.

```bash
./t3-acp.sh orchestrator_capabilities '{}'
./t3-acp.sh delegate_task '{"task":"…","target":{"providerInstanceId":"codex","model":"gpt-6-luna","options":{"reasoningEffort":"low"}}}'
```

Use `t3-acp.sh` in place of the raw `acp-mcp-call` invocation on every machine.

> The minted fallback session is *not* bound to the calling thread (`parentThreadId: null`), so
> thread-scoped delegation prefers the inherited token. If the inherited token is dead, start a new
> T3 thread/session after the app is up so the harness mints a fresh thread-bound one.

Optional machine-side cleanup: don't run two T3 servers at once (close the VS Code instance or the
desktop app), so the port confusion doesn't recur. The wrapper makes it moot for agent calls.

## What the policy does

Writes one managed section into the two global instruction files every T3-spawned agent reads:

| File | Read by |
|---|---|
| `~/.codex/AGENTS.md` | codex sessions, and codex's own internal subagents |
| `~/.claude/CLAUDE.md` | claudeAgent sessions and Claude-SDK agents (including `mcode`/minimax) |

The installer is **idempotent** and **non-destructive**: it replaces only its own marked block
(`<!-- t3-subagent-cost-policy:begin -->` … `end`), leaves machine-specific content alone
(e.g. your `~/.codex/RTK.md` include), and writes a `.bak-<timestamp>` before changing
anything.

## Files

| File | Purpose |
|---|---|
| `policy.md` | Single source of truth for the policy text |
| `install.sh` | Idempotent installer (`--check`, `--uninstall`, `--print`) |
| `t3-acp.sh` | ACP call wrapper: live-endpoint resolution + 401 self-heal |

## Roll out

### A. Private git repo (recommended)

On this machine:

```bash
cd /path/to/t3-subagent-policy
gh repo create t3-subagent-policy --public --source=. --remote=origin --push
```

On every other machine with T3:

```bash
git clone <your-account>/t3-subagent-policy.git ~/t3-subagent-policy
~/t3-subagent-policy/install.sh
```

Re-run `install.sh` any time `policy.md` changes; it re-syncs the block without duplicating it.

### B. Copy over SSH (no repo)

```bash
scp -r /path/to/t3-subagent-policy user@other-host:~/
ssh user@other-host '~/t3-subagent-policy/install.sh'
```

### C. From an agent on the target machine

Ask the agent on that machine to run `~/t3-subagent-policy/install.sh`.

## Verify on each machine

1. Policy installed:

```bash
~/t3-subagent-policy/install.sh --check    # OK/MISS per file, exit 1 if missing
```

2. T3's own default model is cheap (should be `gpt-6-luna` + `reasoningEffort=medium`):

```bash
python3 -c "import json,os;print(json.load(open(os.path.expanduser('~/.t3/userdata/settings.json')))['defaultModelSelection'])"
```

3. ACP path works:

```bash
~/t3-subagent-policy/t3-acp.sh orchestrator_capabilities '{}'   # should print capabilities
```

4. A child session actually loads the policy — delegate a cheap child
   (`codex` / `gpt-6-luna` / `reasoningEffort: low`) and read the child thread; it should answer
   "Subagent cost policy / gpt-6-luna / low or medium (never max/ultra)".

5. If calls still 401, check for a second T3 server on the machine:

```bash
lsof -nP -iTCP -sTCP:LISTEN | grep -E '3773|3774'
curl -s http://127.0.0.1:3773/api/auth/session | head -c 200   # which environment is this?
```

## Uninstall

```bash
~/t3-subagent-policy/install.sh --uninstall
```

## Known limits

- A thread whose model selection is set to `high`/`max` in the T3 UI still propagates that effort to
  every child session — T3 passes it via `--config model_reasoning_effort=...` before any
  instruction file is read. Keep the thread at `low`/`medium`.
- The policy steers *model and effort choices*; it cannot block a provider from escalating an
  internal "strong" subtask on its own.
- `t3-acp.sh` resolves the endpoint for the environment that owns `~/.t3/userdata`. If you run the
  VS Code-hosted T3 as your primary, point `T3_RUNTIME_FILE` at that instance's runtime file.
