## Subagent cost policy

Applies to every child session this agent spawns, native subagent tool or orchestrator `delegate_task`.

- Cheapest sufficient model first: `gpt-6-luna` (codex), `claude-haiku-5-5` (claudeAgent), or the
  current provider's own small model otherwise. Reserve `gpt-6.1-sol`, `gpt-6-astra`, `gpt-6-sol`,
  `claude-opus-5-5`, `claude-sonnet-5-5` for hard debugging, architecture, or an adversarial review
  the user asked for.
- Always pass reasoning effort explicitly on a delegated child: `low` or `medium`.
  Never `max`/`ultra` unless the user asks.
- One child over a fan-out: delegate a scan, lookup, or read to one child, then escalate only that
  child if its result is ambiguous.
- Prefer the same provider for children; it is cheaper and inherits this thread's conventions.
- Never change a child's model or effort silently: state the reason in one line before spawning.
- Keep the parent thread's own reasoning effort at `low`/`medium`: T3 launches every child session
  with the thread's model selection (`--model` + `--config model_reasoning_effort=...`), so a
  thread set to `high`/`max` propagates to every descendant.
