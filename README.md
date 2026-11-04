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

Writes one managed section into the global instruction file of every provider family T3 can drive:

| File | Provider | Note |
|---|---|---|
| `~/.codex/AGENTS.md` | codex | also read by codex's own internal subagents |
| `~/.claude/CLAUDE.md` | claudeAgent, claude-sdk agents (mcode/minimax) | `CLAUDE.local.md` is user-local, not global |
| `~/.config/opencode/AGENTS.md` | opencode | opencode resolves its global instructions to `<config dir>/AGENTS.md` |
| `~/.gemini/GEMINI.md` | gemini CLI | only when that gemini home exists |
| `~/.t3/tools/antigravity-cliproxy/gemini-home/GEMINI.md` | antigravity via T3's clip proxy | the proxy sets `GEMINI_HOME` to its own directory, so the plain `~/.gemini/GEMINI.md` is **not** read by it |

The installer discovers the gemini homes that actually exist on the machine (`GEMINI_HOME`,
`~/.gemini`, and T3's clip-proxy home) and writes the policy into each of them. A file for a
provider that isn't installed is never created.

Existing content is preserved (e.g. your `@RTK.md` include, the graphify skill, the RTK notes in
`GEMINI.md`). The installer replaces only its own marked block
(`<!-- t3-subagent-cost-policy:begin -->` … `end`), creates the file when the provider has none
yet, and writes a `.bak-<timestamp>` before changing anything.

## Files

| File | Purpose |
|---|---|
| `policy.md` | Single source of truth for the policy text |
| `install.sh` | Idempotent installer (`--check`, `--uninstall`, `--print`) |
| `t3-acp.sh` | ACP call wrapper: live-endpoint resolution + 401 self-heal |
| `patches/apply-mcode-acp-steer.py` | Patches the deployed mcode ACP proxy: steering, goals, system prompt |
| `patches/mcode-acp-proxy-steer.js` | The known-good patched proxy (the patcher's target state) |
| `patches/sync-patcher-blocks.py` | Regenerates the patcher's code blocks from the known-good proxy |
| `patches/remove-plugin.sh` | Removes a MiniMax Code plugin and stops the MCP servers it leaves behind |

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
It also patches the mcode ACP proxy, and `--uninstall` puts the proxy back the way it was.

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

That check covers both halves: the instruction-file policy **and** the mcode ACP proxy patch
(the last line reports the proxy). The proxy is what makes steering, goals and the system prompt
work in T3; without it the policy text is all a machine gets.

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

## Provider-level fix: antigravity clip ignored the requested effort

T3's antigravity clip proxy (`~/.t3/tools/antigravity-cliproxy/acp_clip_proxy_shim.py`) only sent
an effort upstream for the models in its `PROXY_MODELS` table (`gemini-4-argon-*`,
`claude-*-*`). Every other model — including `gemini-3.8-flash-low` — was forwarded untouched, so
requesting a low-effort model still ran at the upstream default. Asking for "low" was cosmetic.

```bash
./patches/apply-antigravity-effort.sh            # patches the deployed shim (idempotent)
python3 patches/apply-antigravity-effort.py       # same fix, works on any shim revision
python3 patches/test_acp_clip_proxy_shim.py       # unit self-check for the routing logic
python3 patches/test_clip_proxy_effort.py         # integration check: forwards thinkingLevel
```

Every machine runs a slightly different shim revision, so the patcher edits the code by shape
(`class State:` plus the forward block) instead of by byte offset, and refuses to touch a file it
does not recognise. `antigravity-clip-effort.patch` is the raw diff for the macOS revision.

The patch derives the effort from the selected model's name suffix and sends it as
`generationConfig.thinkingConfig.thinkingLevel`, leaving the routed argon/claude behaviour
untouched. New provider sessions pick it up — no T3 restart needed.

## Provider-level fix: force sending a message broke the thread

Symptom: in T3 Code, force sending a message while the agent was still working failed and the
thread became unusable — every later message in that thread failed too.

Cause: T3 declares ACP providers as `supportsActiveSteering: false`, so a force send is an
interrupt plus a restart: `session/cancel` and the new `session/prompt` go out back to back. The
agent runs one prompt turn per session, so the new prompt arrived while the cancelled turn was
still winding down and was rejected:

```
-32602 Invalid params: A prompt is already active for this session.
```

and once that turn had ended, the runtime still owned it for a moment and answered:

```
-32603 Session already has an active Turn. Use queue send to deliver the message after it.
```

Fix: `~/.minimax-code/bin/mcode-acp-proxy.js` now holds a prompt that arrives during a running
turn, releases it once that turn has finished, and resends it while the runtime still reports the
turn as busy. Duplicate and idle cancels are dropped, because the agent uses them to swallow the
next prompt of that session.

The agent also ships native ACP extension methods (`mcode/session/steer`, `mcode/session/queue/*`,
`mcode/session/goal/*`), which T3 Code knows nothing about. The proxy uses `mcode/session/steer`
when the client sends a message **without** cancelling: the message then joins the running turn
instead of replacing it, and the client gets the turn's own result back. When the client cancels
first — T3's force send — the cancel is passed straight through and the message runs as the next
turn, because the native steer cannot interrupt a running tool call. If the agent refuses the
steer, the proxy falls back to holding the prompt.

```bash
./patches/apply-mcode-acp-steer.py            # patches the deployed proxy (idempotent)
python3 patches/apply-mcode-acp-steer.py       # same fix, works on any proxy revision
python3 patches/sync-patcher-blocks.py         # copy the patcher's blocks from the known-good file
python3 patches/test_mcode_acp_steer.py        # unit self-check for the patcher
node patches/test_mcode_acp_steer.mjs          # scenario check against a live agent
node patches/test_mcode_acp_steer_fallback.mjs # the agent refuses the steer -> fallback
node patches/test_mcode_acp_native_steer_bridge.mjs  # the native steer is really used
node patches/test_mcode_acp_proxy_resend.mjs   # rejection-path check, no login needed
node patches/test_mcode_acp_native_extensions.mjs    # the native extension API
node patches/test_mcode_acp_elicitation.mjs    # the elicitation rewrite and the chunk patch
node patches/test_mcode_acp_goal_bridge.mjs    # the goal commands, end to end
node patches/test_mcode_acp_goal_continuation.mjs    # the goal working on its own
node patches/test_mcode_acp_goal_prompt_collision.mjs  # a prompt during a goal turn
./patches/remove-plugin.sh goal-mode@local     # remove a plugin and stop its leftover servers
```

If the agent keeps refusing the prompt, the proxy resends it with a growing backoff and gives up
after `MCODE_RESEND_MAX_WAIT_MS` (10 minutes by default). The agent's error is then passed to the
client and the session is released, so the turn fails visibly instead of hanging, and the next
prompt in that session still works. The budget has to outlast a goal turn, which owns the session
for minutes at a time.
`test_mcode_acp_proxy_resend.mjs` drives that path against a fake agent, so it needs no login.

The proxy is the same script on every machine, and machines run different revisions of it (the
recovery handling differs between them), so the patcher edits it by shape instead of by byte offset
and refuses to touch a file it does not recognise. The fix sits in the transport layer — the stdin
and stdout plumbing — because that part is identical in every revision;
`mcode-acp-proxy-steer.js` is the known-good patched file.

The running ACP process keeps the old code in memory, so **restart T3 Code or open a new thread**
after patching — T3 spawns the ACP runtime per thread.

## The system prompt is a real system prompt

T3 Code sends its own instructions as the first user message, because the ACP
wire has no system prompt field. The proxy can do better: it starts the agent
with `--append-system-prompt-file`, so the text lands in the agent's **actual
system prompt** rather than in a user turn.

The proxy writes that file itself. It always carries the T3 orchestration
directive, so T3 stays above mcode's own delegation:

```
[MANDATORY T3 ORCHESTRATION DIRECTIVE]
... ALWAYS use T3 Code's orchestrator tool `delegate_task` (call it through
mcp_invoke with tool_name "mcp__t3-code__delegate_task") ...
```

Set `MCODE_APPEND_SYSTEM_PROMPT_FILE` to a file to append more of your own.

## Goals: the native API, not the plugin

The `goal-mode` plugin (a local plugin with its own MCP server, its own state file and its own
hooks) was a parallel implementation of Goal Mode. It has been removed: `mcode plugin remove
goal-mode@local`. Goals now live in the runtime's own goal store and are reached over the ACP
extension methods the agent advertises in `initialize._meta`:

```
mcode/session/goal/create   { sessionId, objective, tokenBudget? }
mcode/session/goal/get      { sessionId }
mcode/session/goal/patch    { sessionId, status?, objective?, tokenBudget? }
mcode/session/goal/clear    { sessionId }
```

The proxy turns a `/goal ...` prompt from the client into those calls, so the goal never reaches
the model as an ordinary turn:

| Prompt | Native call |
| --- | --- |
| `/goal <objective>` | `goal/create` |
| `/goal status` | `goal/get` |
| `/goal pause` | `goal/patch` with `status: "paused"` |
| `/goal resume` | `goal/patch` with `status: "active"` |
| `/goal stop` | `goal/clear` |
| `/goal edit <text>` | `goal/patch` with the new objective |

The natural language the goal-mode plugin used to detect works too, so removing the plugin did
not cost the phrasing: "set my goal to ...", "what is my current goal?", "pause my goal",
"cancel my goal", "update my goal to ...". `/goal history` and the plan family have no native
equivalent and stay ordinary prompts.

The proxy answers the client with an `agent_message_chunk` that reports what happened, and with
`stopReason: "end_turn"` for the prompt itself. `/goal history` has no native equivalent and is
passed through to the model.

The proxy opts in to the agent's extension notifications, so it sees `mcode/session/goal_update`
and reports the goal state to the client ("Goal tg_xxx is now complete"). Those notifications are
translated, never forwarded: T3 does not know these methods.

The agent has no goal slash command, so the proxy advertises one: `/goal` shows up in the
client's command list and in the session result, with the description
"Set an autonomous goal loop (works systematically until the goal is achieved)".

The native goal has no plan field, so a plan carried over from the plugin is appended to the
objective — the objective is what the runtime injects into every continuation turn. The plugin's
own state (goal history, plans, criteria) is kept in `~/Workspaces/TestSpaces/goal-mode-backup/`.

```bash
node patches/test_mcode_acp_goal_bridge.mjs  # the goal commands, end to end
node patches/test_mcode_acp_goal_command.mjs  # the goal command is advertised to the client
node patches/test_mcode_acp_goal_phrasing.mjs   # the natural language phrasings
node patches/migrate_goal_to_native.mjs       # carry a plugin goal over to the native store
```

## The native queue cannot own the client's prompts

The agent has `mcode/session/queue/*`, and it looks like the right owner for the messages the
proxy holds. It is not, for the path T3 uses:

- a **cancelled** session has its queue **paused**, and the queued message sits there as
  `status: "paused"` and is never dispatched;
- a paused queue refuses every later turn: the next prompt fails with
  `Internal error: ... The session Queue is paused`;
- a queued turn produces **no** `session/prompt` response, so the client could not be
  correlated with it anyway.

So the proxy keeps its own queue for the client's messages. It preserves the request id, which
means the agent's real answer still reaches the client. The native queue stays unused.

## Creating a goal from the agent

The runtime defines a `create_goal` tool but never wires it into the agent's tool set, and the
version it defines would create a goal that never runs. The working creation path is the ACP
extension the proxy uses. Details and the upstream request:
[`patches/UPSTREAM-create-goal.md`](patches/UPSTREAM-create-goal.md).

## Known limits

- This policy steers **subagents only**. The model and effort the user picks for the main thread are
  never restricted, and no models are hidden from the picker — the goal is that *children* spawn on
  cheap models at low/medium effort, not that you lose access to any model.
- A thread whose model selection is set to `high`/`max` in the T3 UI still propagates that effort to
  every child session — T3 passes it via `--config model_reasoning_effort=...` before any
  instruction file is read. Keep the thread at `low`/`medium`.
- The policy steers *model and effort choices*; it cannot block a provider from escalating an
  internal "strong" subtask on its own.
- `t3-acp.sh` resolves the endpoint for the environment that owns `~/.t3/userdata`. If you run the
  VS Code-hosted T3 as your primary, point `T3_RUNTIME_FILE` at that instance's runtime file.
