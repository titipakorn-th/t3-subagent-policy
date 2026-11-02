## Subagent cost policy

This policy governs **subagents only** — the child agents you spawn, through a native subagent tool
or the orchestrator's `delegate_task`. It never restricts which model or effort the user picks for
the main thread; that choice stays the user's.

- Cheapest sufficient model for every child: `gpt-6-luna` (codex), `claude-haiku-5-5` (claudeAgent),
  or the current provider's own small model otherwise. Do not spawn `gpt-6.1-sol`, `gpt-6-astra`,
  `gpt-6-sol`, `claude-opus-5-5` or `claude-sonnet-5-5` for a child unless the user named that model.
- Reasoning effort for every child: `low` or `medium`. Never `max`, `ultra` or `xhigh` for a child.
- One child over a fan-out: a scan, lookup or read goes to one cheap child; escalate only that child
  if its result is ambiguous.
- Prefer the same provider for children; it is cheaper and inherits this thread's conventions.
- Never change a child's model or effort silently: state the reason in one line before spawning.
- Keep the parent thread's own reasoning effort at `low`/`medium`: T3 launches every child session
  with the thread's model selection (`--model` + `--config model_reasoning_effort=...`), so a
  thread set to `high`/`max` propagates to every descendant.
